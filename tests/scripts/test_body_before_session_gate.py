"""A request's body is read before the request takes a pooled connection
(#1644), as the API's own role over the real pool.

Reading the body inside the tenant's unit of work holds a connection for as long
as the client takes to send it. The pool is ten connections and the wait for one
is a second, so a few direct clients that dribble a body can leave every other
request refused. Three routes take a body: the two Instagram connect starts,
through their shared tail, and the source policy write. Each reads, and for the
connect starts parses, its body first (`tests/src/api/
test_no_body_read_in_a_session.py` pins that for every route, from the source).

Here it is measured: the pool is counted while a body is half sent, and a body
that is refused is refused with no connection ever taken. Each has its control,
the same request with a body that is fine, which is served and does take one.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, contextmanager

import httpx
import pytest
from sqlalchemy import event

from src.api.app import create_app
from src.api.principal import Principal, current_principal
from src.config.settings import settings
from tests.scripts import test_past_posts_preview_gate as preview_gate
from tests.scripts import test_instagram_sources_gate as foundation

pytestmark = [pytest.mark.integration]
#: The API's own login (`svc_ingress`), over the pool its requests share.
ingress = preview_gate.ingress
origin = foundation.origin

JSON = {"content-type": "application/json"}


@pytest.fixture
def instagram_configured(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "app-1", raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)
    monkeypatch.setattr(
        settings, "OAUTH_REDIRECT_BASE_URL", "https://api.example.test", raising=False
    )


@asynccontextmanager
async def api(engine, user):
    app = create_app(engine=engine, env={})
    app.state.email = None
    app.state.past_posts_capability = foundation.capability()
    app.dependency_overrides[current_principal] = lambda: Principal(
        session_id=str(uuid.uuid4()), user_id=str(user)
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class Connections:
    """The engine's pool, counted: the connections out right now, and every one
    handed out since counting began."""

    def __init__(self, engine):
        self.pool = engine.sync_engine.pool
        self.ever = 0

    def _handed_out(self, *args):
        self.ever += 1

    @property
    def now(self) -> int:
        return self.pool.checkedout()


@contextmanager
def counting(engine):
    counted = Connections(engine)
    event.listen(counted.pool, "checkout", counted._handed_out)
    try:
        yield counted
    finally:
        event.remove(counted.pool, "checkout", counted._handed_out)


class SlowBody:
    """A request body that arrives in two parts. `blocked` is set at the moment
    the app has taken the first and asked for the rest; the rest follows
    `release`."""

    def __init__(self, first: bytes, rest: bytes):
        self.first, self.rest = first, rest
        self.blocked, self.release = asyncio.Event(), asyncio.Event()

    def __aiter__(self):
        return self._chunks()

    async def _chunks(self):
        yield self.first
        self.blocked.set()
        await self.release.wait()
        yield self.rest


def reason(response) -> str:
    return response.json()["reason"]


#: Each route under test: how it is called, a body split in two that is fine
#: once whole, what a good answer says, and the refusal a malformed body gets.
ROUTES = {
    "header_add": dict(
        method="POST",
        path="accounts/connect",
        first=b'{"past_posts_choice": ',
        rest=b"null}",
        refused="invalid_past_posts_choice",
    ),
    "row_start": dict(
        method="POST",
        path="accounts/{account}/connect",
        first=b'{"past_posts_choice": ',
        rest=b"null}",
        refused="invalid_past_posts_choice",
    ),
    "policy_put": dict(
        method="PUT",
        path="sources/{source}/policy",
        first=b'{"schema_version": 1, "expected_revision": 1, ',
        rest=b'"minimum_age_days": 90, "include_credited": false,'
        b' "include_unknown": false}',
        refused="invalid_source_policy",
    ),
}


async def world(ingress, origin, route):
    """The URL of *route* in the origin's workspace, and a source for the policy
    write to name (created before any counting begins)."""
    source = await foundation.ensure(ingress, origin, foundation.authorization())
    path = ROUTES[route]["path"].format(
        account=origin["iga"], source=source["source_id"]
    )
    return f"/api/v1/workspaces/{origin['ws']}/{path}"


@pytest.mark.parametrize("route", ROUTES)
async def test_a_body_still_arriving_holds_no_connection(
    ingress, origin, instagram_configured, route
):
    spec = ROUTES[route]
    url = await world(ingress, origin, route)
    async with api(ingress, origin["user"]) as client:
        with counting(ingress) as connections:
            body = SlowBody(spec["first"], spec["rest"])
            request = asyncio.create_task(
                client.request(spec["method"], url, content=body, headers=JSON)
            )
            try:
                await asyncio.wait_for(body.blocked.wait(), timeout=10)
                # The app has the first part and is waiting for the rest: the
                # session must not be open, nor have been.
                assert connections.now == 0
                assert connections.ever == 0
            finally:
                # Whatever was found, the request is let finish before the app
                # and the pool are closed under it.
                body.release.set()
                [outcome] = await asyncio.wait_for(
                    asyncio.gather(request, return_exceptions=True), timeout=20
                )

            # Control: the whole body, once it is in, is served, and only then
            # does the request take its connection.
            assert not isinstance(outcome, BaseException), outcome
            assert outcome.status_code == 200, outcome.text
            assert connections.ever > 0 and connections.now == 0


@pytest.mark.parametrize("route", ROUTES)
async def test_a_refused_body_takes_no_connection(
    ingress, origin, instagram_configured, route
):
    spec = ROUTES[route]
    url = await world(ingress, origin, route)
    async with api(ingress, origin["user"]) as client:
        with counting(ingress) as connections:
            unreadable = await client.request(
                spec["method"], url, content=b"{not json", headers=JSON
            )
            assert unreadable.status_code == 400, unreadable.text
            assert reason(unreadable) == spec["refused"]
            assert connections.ever == 0

            # Control: the same request with a body that is fine takes one.
            fine = await client.request(
                spec["method"], url, content=spec["first"] + spec["rest"], headers=JSON
            )
            assert fine.status_code == 200, fine.text
            assert connections.ever > 0


@pytest.mark.parametrize("route", ["header_add", "row_start"])
@pytest.mark.parametrize(
    "content",
    [
        b'{"past_posts_choice": {"use_history": "yes", "expected_mix_revision": 8,'
        b' "preview_version": 1}}',
        b'{"past_posts_choice": {"use_history": true, "expected_mix_revision": 8,'
        b' "preview_version": 2}}',
        b'{"purpose": "connect"}',
        b"[1]",
    ],
    ids=["wrong_type", "another_preview", "unknown_key", "not_an_object"],
)
async def test_a_choice_that_is_not_the_contracts_shape_takes_no_connection(
    ingress, origin, instagram_configured, route, content
):
    """The shape is `parse_choice`'s, judged with the body in hand and before
    the session, so a start that cannot be valid costs the pool nothing."""
    url = await world(ingress, origin, route)
    async with api(ingress, origin["user"]) as client:
        with counting(ingress) as connections:
            refused = await client.post(url, content=content, headers=JSON)
            assert refused.status_code == 400, refused.text
            assert reason(refused) == "invalid_past_posts_choice"
            assert connections.ever == 0
