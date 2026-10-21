"""Fake providers for the full-composition gate (#1644).

Four stand-ins, each at the seam the production object is built over, so the
real code above it runs unchanged and nothing reaches a socket:

* `FakeInstagram`: Instagram's Graph and its media CDN, as one
  `httpx.MockTransport`, serving the generated onboarding past-posts sources
  (`tests.fixtures.instagram_onboarding`) the way Meta pages them: by time
  window (or, with no `since`, everything older than `until`: the history
  walk's one-row gap probe), newest first, a caption only to the read that
  asks for it, UTC offsets without a colon (`+0000`, which Python 3.10's
  `fromisoformat` rejects). Every request is recorded with the account its
  bearer token names, and one made inside a tenant transaction fails the
  test.
* `FakeTransit`: the transit store, with one asset registry behind the upload
  the publish pipeline makes and the typed lookup and destroy the deletion
  cleanup makes.
* `FakeTelegram`: the Bot API, recording every call; it also builds the real
  `TelegramTransport` over itself.
* `jpeg`: a real, decodable image, so the real validator has something to judge.

Every token, id and URL is synthetic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import urlsplit

import httpx
from PIL import Image

from src.services.target import egress
from src.services.target.transit import PastPostsUploadEvidence
from src.services.target.unit_of_work import in_transaction

#: No DNS: the egress floor's private-address check resolves the host, and a
#: gate that must open no socket resolves nothing. Every fake client is plain
#: and in-process, so there is nothing left for that check to guard.
OFFLINE = egress.EgressPolicy().without(enforce_private_address_block=False)

GRAPH_HOST = "graph.instagram.com"
CDN_HOST = "scontent-fixture.cdninstagram.com"


def jpeg(color: str = "red") -> bytes:
    """A small JPEG the real validator accepts as a supported image."""
    output = BytesIO()
    Image.new("RGB", (64, 128), color).save(output, format="JPEG")
    return output.getvalue()


def token_for(ref: str) -> str:
    """The access token the fake exchange hands an account (the OAuth gate's)."""
    return f"token-for-{ref}"


def graph_time(at: int) -> str:
    """Epoch seconds as Graph writes them: `2022-06-15T09:30:00+0000`."""
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def retarget(past_posts, *, owner: str, offset: int):
    """*past-posts source* as another account's: the same posts under ids shifted by
    *offset* (an id is unique within a workspace, and two accounts of one
    workspace must not share them) and owned by *owner*."""

    def shift(value: str) -> str:
        return str(int(value) + offset)

    posts = []
    for post in past_posts.posts:
        parent = {**post.parent, "id": shift(post.parent["id"]), "owner": {"id": owner}}
        children = tuple({**c, "id": shift(c["id"])} for c in post.children)
        posts.append(replace(post, parent=parent, children=children))
    return replace(past_posts, owner_id=owner, posts=tuple(posts))


@dataclass(frozen=True)
class Seen:
    """One request the fake served: where it went and for whom."""

    host: str
    path: str
    account: Optional[str]


class FakeInstagram:
    """Graph and CDN for the accounts it is given a past-posts source for."""

    def __init__(self):
        #: account ref -> its past-posts source; the bearer token names the account.
        self.past_posts_sources: dict = {}
        self.requests: list = []

    def add(self, past_posts) -> None:
        self.past_posts_sources[past_posts.owner_id] = past_posts

    def client(self) -> httpx.AsyncClient:
        """The plain client the past-posts composition is handed: no credentials,
        cookies or default parameters (the adapter refuses anything else)."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    # -- what was asked -------------------------------------------------------

    def asked(self, host=None, *, account=None) -> list:
        return [
            s
            for s in self.requests
            if (host is None or s.host == host)
            and (account is None or s.account == account)
        ]

    @property
    def graph(self) -> list:
        return self.asked(GRAPH_HOST)

    @property
    def cdn(self) -> list:
        return self.asked(CDN_HOST)

    # -- the server -----------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert not in_transaction(), "a provider call held a tenant transaction"
        host, path = request.url.host, request.url.path
        if host == CDN_HOST:
            self.requests.append(Seen(host, path, None))
            return self._cdn(request)
        assert host == GRAPH_HOST, f"a request to {host} left the composition"
        bearer = request.headers.get("authorization", "")
        account = bearer.removeprefix("Bearer token-for-") if bearer else None
        self.requests.append(Seen(host, path, account))
        past_posts = self.past_posts_sources.get(account)
        if past_posts is None:
            return self._error(401, 190, "Invalid OAuth access token")
        parts = path.strip("/").split("/")[1:]  # without the version
        fields = set(request.url.params.get("fields", "").split(","))
        if len(parts) == 2 and parts[1] == "media":
            assert parts[0] == account, "a listing named another account's id"
            return self._listing(request, past_posts, fields)
        if len(parts) == 2 and parts[1] == "children":
            post = self._post(past_posts, parts[0])
            if post is None:
                return self._error(400, 100, "Unsupported get request")
            rows = [
                {k: v for k, v in c.items() if k in fields | {"id"}}
                for c in post.children
            ]
            limit = int(request.url.params["limit"])
            body = {"data": rows[:limit]}
            if len(rows) > limit:
                body["paging"] = {
                    "next": "https://ignored.example/never-followed",
                    "cursors": {"after": str(limit)},
                }
            return httpx.Response(200, json=body)
        if len(parts) == 1:
            post = self._post(past_posts, parts[0])
            if post is None:
                return self._error(400, 100, "Unsupported get request")
            return httpx.Response(200, json=self._row(post, fields))
        return self._error(400, 100, "Unsupported get request")

    @staticmethod
    def _post(past_posts, media_id: str):
        return next((p for p in past_posts.posts if p.parent["id"] == media_id), None)

    @staticmethod
    def _row(post, fields) -> dict:
        row = {k: v for k, v in post.parent.items() if k in fields or k == "id"}
        if "timestamp" in row:
            row["timestamp"] = graph_time(post.at)
        if "caption" in fields and post.caption is not None:
            row["caption"] = post.caption
        return row

    def _listing(self, request, past_posts, fields) -> httpx.Response:
        params = request.url.params
        # A window is `since` up to `until`. The walk's gap probe sends `until`
        # alone, a one-row listing: the newest posts strictly before `until`,
        # as many as `limit` allows, the way a window with no lower bound is
        # assumed to answer (the budget harness's fake answers the same).
        since = int(params["since"]) if "since" in params else 0
        until = int(params["until"])
        limit, after = int(params["limit"]), int(params.get("after", 0))
        inside = [p for p in past_posts.posts if since <= p.at < until]
        page = inside[after : after + limit]
        body = {"data": [self._row(p, fields) for p in page]}
        if after + limit < len(inside):
            body["paging"] = {
                "cursors": {"after": str(after + limit)},
                "next": str(request.url),
            }
        return httpx.Response(200, json=body)

    @staticmethod
    def _error(status: int, code: int, message: str) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": code, "message": message}}
        )

    @staticmethod
    def _cdn(request) -> httpx.Response:
        if not request.url.path.endswith(".jpg"):
            return httpx.Response(404)
        return httpx.Response(
            200, content=jpeg(), headers={"Content-Type": "image/jpeg"}
        )


class FakeTransit:
    """The transit store: one registry behind the pipeline's upload and the
    cleanup's lookup and destroy. Every call asserts it holds no transaction."""

    def __init__(self):
        self.assets: dict = {}
        self.uploads: list = []
        self.destroyed: list = []
        self.lookups: list = []

    def delivery_url(self, ref, *, media_kind, variant=0):
        return f"https://fake-transit.invalid/{ref}"

    async def upload(self, data, *, workspace_id, media_kind, preallocated_ref):
        assert not in_transaction()
        self.uploads.append(preallocated_ref)
        self.assets[preallocated_ref] = PastPostsUploadEvidence(
            preallocated_ref,
            "asset-" + str(len(self.uploads)).zfill(12),
            len(data),
            media_kind,
            "authenticated",
            "c" * 40,
        )
        return self.assets[preallocated_ref]

    async def destroy(self, ref, *, media_kind):
        assert not in_transaction()
        self.destroyed.append(ref)
        self.assets.pop(ref, None)
        return True

    async def past_posts_lookup(self, public_id, *, resource_type):
        assert not in_transaction()
        self.lookups.append(public_id)
        return self.assets.get(public_id)

    async def destroy_past_posts(self, public_id, *, resource_type):
        assert not in_transaction()
        self.destroyed.append(public_id)
        return "destroyed" if self.assets.pop(public_id, None) else "not_found"


@dataclass(frozen=True)
class Call:
    """One Bot API call: its method, and its body (JSON, or the raw multipart)."""

    method: str
    body: bytes

    @property
    def json(self) -> dict:
        return json.loads(self.body)


class FakeTelegram:
    """The Bot API: every call answered `ok` with a fresh message id."""

    def __init__(self):
        self.calls: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        # No transaction check: the outbox's send holds one by design, so that
        # the send's state and its settlement commit together.
        method = urlsplit(str(request.url)).path.rsplit("/", 1)[-1]
        self.calls.append(Call(method, request.read()))
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 1000 + len(self.calls)}}
        )

    def transport(self, *, media_fetch):
        """The real `TelegramTransport`, over this fake and without DNS."""
        from src.channels.telegram_transport import TelegramTransport

        return TelegramTransport(
            "synthetic-bot-token",
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            policy=OFFLINE,
            media_fetch=media_fetch,
        )

    def sent(self, method: str) -> list:
        return [c for c in self.calls if c.method == method]


def multipart_field(call: Call, name: str) -> Optional[str]:
    """A text field of a multipart body, or None."""
    found = re.search(
        rb'name="' + name.encode() + rb'"\r\n\r\n(.*?)\r\n--', call.body, re.S
    )
    return found.group(1).decode() if found else None
