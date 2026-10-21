"""Past-posts thumbnails through the real route as `svc_ingress` (#1644).

The admission before any lookup and the fence before any byte is sent are
PostgreSQL's facts, so they are proven here against the replayed schema under
the production role, with a fake Graph/CDN transport that records every
request. The six reads' `thumbnail_version`, provenance and name are read from
the same rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import quote

import httpx
import pytest
from asyncpg import exceptions as pg_errors
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from src.api.app import create_app
from src.api.principal import Principal, current_principal
from src.services.target import (
    past_posts_preview,
    egress,
    prompts,
    readers,
    source_state,
    upcoming,
    workspaces,
)
from src.services.target.instagram_past_posts_adapter import (
    PastPostsAccess,
    InstagramPastPostsAdapter,
)
from src.services.target.instagram_sources import PastPostsCapability
from src.services.target.unit_of_work import in_transaction
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_preparation_gate as prep
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts.conftest import (
    as_user,
    async_url,
    seed_intent_chain,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration]
origin = foundation.origin

CDN = "https://scontent-test.cdninstagram.com/preview.jpg?ephemeral=signed-secret"
HANDLE = "example_handle"
NO_STORE = "private, no-store, max-age=0"
ORIGINAL_AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
#: What every read calls a past-posts item and a Drive item: each is what it is
#: stored as, the past-posts source's being the wire spelling too.
PAST_POSTS_NAME = "Instagram post"
DRIVE_NAME = "f.jpg"
#: The Instagram ids the world observes (`past_posts_item`, `planned_story`), which
#: a past-posts item is stored under, as its file reference, and no read may give.
GRAPH_IDS = ("123", "456")


def expected_provenance(original_at=ORIGINAL_AT) -> dict:
    """A past-posts item's provenance as every read gives it: the provider, the
    handle of the account that posted it and when, which is nothing while the
    item awaits observation again."""
    return {
        "provider": "instagram_past_posts",
        "origin_handle": HANDLE,
        "original_at": original_at,
        "slide": None,
    }


def graph_ids_in(body: str) -> list[str]:
    """The Instagram ids *body* holds as whole tokens. A bare "123" is inside
    UUIDs and fractional seconds, so an id counts only where nothing a UUID, a
    time or a number is made of touches it: as `Instagram 123` (a name stored as
    `Instagram <Graph id>`), or as the whole of a string (`"123"`)."""
    return [g for g in GRAPH_IDS if re.search(rf"(?<![\w.\-]){g}(?![\w.\-])", body)]


def picture() -> bytes:
    out = BytesIO()
    Image.new("RGB", (96, 64), "teal").save(out, format="JPEG")
    return out.getvalue()


@pytest.fixture
async def ingress(world_db, admin_conn):
    """The API's own login: every read and lock below is `svc_ingress`'s."""
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(world_db, "svc_ingress")))
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar_one() == (
                "svc_ingress"
            )
        yield engine
    finally:
        await engine.dispose()


async def past_posts_item(engine, chain) -> dict:
    """An On past-posts source with one observed item (parent ``123``) and a
    story for it; the origin account carries a handle."""
    src, item, intent = await prep.selected(engine, chain)
    async with obs.uow(engine, chain) as conn:
        await conn.execute(
            text("UPDATE ig_accounts SET handle = :handle WHERE id = :id"),
            {"handle": HANDLE, "id": str(chain["iga"])},
        )
    return {**src, "item": item.media_item_id, "intent": intent}


async def media_row(engine, chain, item) -> dict:
    async with obs.uow(engine, chain) as conn:
        return await workspaces.get_media(
            conn, workspace_id=str(chain["ws"]), media_id=item
        )


async def current_pair(engine, chain, world) -> str:
    async with obs.uow(engine, chain) as conn:
        row = await obs.row(
            conn,
            "SELECT current_authorization_generation_id AS gid, source_use_epoch"
            " FROM media_sources WHERE id = :sid",
            sid=world["source_id"],
        )
    return f"{row['gid']}.{row['source_use_epoch']}"


async def switch(engine, chain, world, ratio) -> None:
    """Off (0) or On (1) through the one fence writer, which bumps the epoch."""
    async with obs.uow(engine, chain) as conn:
        await source_state.lock_all_workspace_sources(
            conn, workspace_id=str(chain["ws"])
        )
        before = await source_state.past_posts_state(
            conn, workspace_id=str(chain["ws"]), source_id=world["source_id"]
        )
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio = :ratio WHERE source_id = :sid"
            ),
            {"ratio": ratio, "sid": world["source_id"]},
        )
        await source_state.apply_past_posts_transition(
            conn,
            workspace_id=str(chain["ws"]),
            source_id=world["source_id"],
            before=before,
            reason="test",
        )


#: Every read that emits a picture's `has_thumbnail` and `thumbnail_version`,
#: and with them its provenance and its name: the intents' row is shared by the
#: queue, one story and `upcoming`.
READS = (
    "get_media",
    "list_media",
    "list_intents",
    "get_intent",
    "intent_days",
    "upcoming",
)
DRIVE_LINK = "https://drive.example.test/thumbnail"


async def planned_story(engine, chain, world) -> str:
    """A story born planned, for a second observed item of the same source:
    the one `upcoming` lists (the cadence's own stories are never in it)."""
    second = await obs.observe(engine, chain, world, obs.bundle("456"))
    async with obs.uow(engine, chain) as conn:
        created = await conn.execute(
            text(
                "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,"
                "provider_account_ref,approval_mode,schedule_slot_at,origin) "
                "VALUES(:ws,:acct,:item,'synthetic','manual',now() + interval '1 hour',"
                "'planned') RETURNING id"
            ),
            {
                "ws": str(chain["ws"]),
                "acct": str(chain["iga"]),
                "item": second.media_item_id,
            },
        )
        return str(created.scalar_one())


def linked_drive_story(sync_conn, chain) -> dict:
    """A planned Drive story whose item has a provider link, beside the chain's
    own Drive item, which has none."""
    with sync_conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        story = seed_intent_chain(cur, chain["ws"], "drive-planned", origin="planned")
        cur.execute(
            "UPDATE media_items SET thumbnail_url = %s WHERE id = %s",
            (DRIVE_LINK, story["media"]),
        )
    sync_conn.commit()
    return story


async def pictures(engine, chain, *, item, story, planned=None) -> dict:
    """What each read gives one item, as `(has_thumbnail, thumbnail_version,
    provenance, file_name)`: its row alone and in the library, its story in the
    queue, alone and in the calendar's month, and, with a *planned* story, in
    `upcoming`."""
    ws, today = str(chain["ws"]), date.today()
    async with obs.uow(engine, chain) as conn:
        one = await workspaces.get_media(conn, workspace_id=ws, media_id=item)
        library = await workspaces.list_media(conn, workspace_id=ws)
        queue = await workspaces.list_intents(conn, workspace_id=ws)
        alone = await workspaces.get_intent(conn, workspace_id=ws, intent_id=story)
        days = await workspaces.intent_days(
            conn,
            workspace_id=ws,
            states=workspaces.INTENT_STATES,
            from_date=today - timedelta(days=2),
            to_date=today + timedelta(days=3),
            per_day=10,
        )
        coming = await upcoming.upcoming(
            conn,
            workspace_id=ws,
            from_date=today - timedelta(days=1),
            to_date=today + timedelta(days=3),
        )

    def facts(row):
        return (
            row["has_thumbnail"],
            row["thumbnail_version"],
            row["provenance"],
            row["file_name"],
        )

    month = {str(n["id"]): n for d in days for n in d["newest"]}
    seen = {
        "get_media": facts(one),
        "list_media": facts({str(r["id"]): r for r in library}[item]),
        "list_intents": facts({str(r["id"]): r for r in queue}[story]),
        "get_intent": facts(alone),
        "intent_days": facts(month[story]),
    }
    if planned is not None:
        seen["upcoming"] = facts({str(r["id"]): r for r in coming["planned"]}[planned])
    return seen


class Hold:
    """A CDN that will not answer until released: each download that reaches it
    waits, and `reached` is set once *expect* of them have."""

    def __init__(self, expect):
        self.expect, self.waiting = expect, 0
        self.reached, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self):
        self.waiting += 1
        if self.waiting >= self.expect:
            self.reached.set()
        await self.release.wait()


def ports(
    calls,
    *,
    on_download=None,
    after_fetch=None,
    enabled=True,
    hold=None,
    graph_cap=None,
    carousel=False,
):
    """The composition root's seams over a fake Graph/CDN that records every
    request; *on_download* runs while the CDN answers, *after_fetch* once the
    adapter has returned its bytes. *hold* maps a workspace to a :class:`Hold`
    its downloads wait on, and *graph_cap* gives the Graph a byte cap that its
    reply is one byte over."""
    jpeg = picture()

    async def prepare(capture, admission_provider):
        assert not in_transaction()

        async def access(source_id, *, workspace_id):
            return PastPostsAccess(capture.account_ref, "synthetic-token", capture)

        async def handler(request):
            assert not in_transaction(), "no transaction spans provider I/O"
            calls.append((request.url.host, request.url.path))
            if request.url.host == "graph.instagram.com":
                if graph_cap is not None:
                    return httpx.Response(200, content=b"{" + b" " * graph_cap)
                if carousel and request.url.path.endswith("/children"):
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {"id": child, "media_type": "IMAGE", "media_url": CDN}
                                for child in ("123", "456")
                            ]
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "id": "123",
                        "media_type": "CAROUSEL_ALBUM" if carousel else "IMAGE",
                        "owner": {"id": capture.account_ref},
                        "media_url": CDN,
                    },
                )
            assert "authorization" not in request.headers
            if on_download is not None:
                await on_download()
            if hold and capture.workspace_id in hold:
                await hold[capture.workspace_id]()
            return httpx.Response(
                200, content=jpeg, headers={"content-type": "image/jpeg"}
            )

        adapter = InstagramPastPostsAdapter(
            access_provider=access,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            graph_version="v26.0",
            policy=egress.EgressPolicy(
                **({} if graph_cap is None else {"max_response_bytes": graph_cap})
            ).without(enforce_private_address_block=False),
            admission_provider=admission_provider,
        )
        if after_fetch is None:
            return adapter

        class Late:
            async def fetch_preview(self, *args, **kwargs):
                content = await adapter.fetch_preview(*args, **kwargs)
                await after_fetch()
                return content

        return Late()

    return past_posts_preview.PastPostsPreviewPorts(
        PastPostsCapability(enabled), prepare
    )


def unreachable():
    """Ports whose adapter may never be built: a request that reaches it fails."""

    async def prepare(capture, admission_provider):
        raise AssertionError("a refused preview reached the provider")

    return past_posts_preview.PastPostsPreviewPorts(foundation.capability(), prepare)


@asynccontextmanager
async def api(engine, user, preview_ports=None, *, preview=None):
    """The real app with *user* signed in and the past-posts port injected: one
    built over *preview_ports*, or the *preview* itself, which two apps can
    share as two workers' requests would."""
    app = create_app(engine=engine)
    app.state.email = None
    app.state.past_posts_preview = preview or past_posts_preview.PastPostsPreview(
        preview_ports
    )
    app.dependency_overrides[current_principal] = lambda: Principal(
        session_id=str(uuid.uuid4()), user_id=str(user)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
    ) as client:
        yield client


async def thumbnail(engine, chain, item, version, preview_ports, *, user=None):
    async with api(engine, user or chain["user"], preview_ports) as client:
        return await client.get(
            f"/api/v1/workspaces/{chain['ws']}/media/{item}/thumbnail",
            params={"v": version},
        )


def refused(caplog) -> list[str]:
    return [
        r.getMessage().rsplit(": ", 1)[1]
        for r in caplog.records
        if r.name == past_posts_preview.__name__
    ]


def assert_placeholder(resp):
    assert resp.status_code == 404
    assert resp.json() == {"detail": "not found"}
    assert resp.headers["cache-control"] == NO_STORE


async def test_the_reads_advertise_the_current_pair_and_the_route_serves_it(
    ingress, origin, caplog
):
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)
    assert version.startswith(world["generation_id"] + ".")

    row = await media_row(ingress, origin, world["item"])
    assert (row["has_thumbnail"], row["thumbnail_version"]) == (True, version)
    assert row["provenance"] == {
        "provider": "instagram_past_posts",
        "origin_handle": HANDLE,
        "original_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "slide": None,
    }
    async with obs.uow(ingress, origin) as conn:
        library = {
            str(r["id"]): r
            for r in await workspaces.list_media(conn, workspace_id=str(origin["ws"]))
        }
        intents = {
            str(r["id"]): r
            for r in await workspaces.list_intents(conn, workspace_id=str(origin["ws"]))
        }
        today = date.today()
        days = await workspaces.intent_days(
            conn,
            workspace_id=str(origin["ws"]),
            states=["scheduled"],
            from_date=today - timedelta(days=2),
            to_date=today + timedelta(days=2),
            per_day=10,
        )
    assert library[world["item"]] == row
    # The Drive row beside it has no thumbnail and no provenance.
    drive = library[str(origin["media"])]
    assert (
        drive["has_thumbnail"],
        drive["thumbnail_version"],
        drive["provenance"],
    ) == (
        False,
        "hash-past-posts-service"[: workspaces.THUMBNAIL_VERSION_CHARS],
        None,
    )
    story = intents[world["intent"]]
    assert (story["has_thumbnail"], story["thumbnail_version"]) == (True, version)
    newest = {str(n["id"]): n for d in days for n in d["newest"]}
    assert newest[world["intent"]]["has_thumbnail"] is True
    assert newest[world["intent"]]["thumbnail_version"] == version
    # What the reads add carries no Graph id, permalink or caption.
    assert not {"permalink", "media_id", "child_id", "first_child_id"} & set(row)

    calls = []
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(ingress, origin, world["item"], version, ports(calls))
    assert resp.status_code == 200, resp.text
    assert resp.content == picture()
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["cache-control"] == NO_STORE
    assert "etag" not in resp.headers and "last-modified" not in resp.headers
    assert calls == [
        ("graph.instagram.com", "/v26.0/123"),
        ("scontent-test.cdninstagram.com", "/preview.jpg"),
    ]
    sent = " ".join(f"{k}: {v}" for k, v in resp.headers.items())
    for secret in ("cdninstagram", "graph.instagram", "signed-secret", "synthetic"):
        assert secret not in sent
    assert refused(caplog) == []


async def test_second_slide_has_its_own_name_provenance_and_two_graph_preview(
    ingress, origin
):
    from src.services.target.instagram_catalog import PastPostsAsset

    world = await past_posts_item(ingress, origin)
    carousel = obs.bundle()
    await obs.observe(
        ingress,
        origin,
        world,
        replace(
            carousel,
            assets=(
                PastPostsAsset(selected_asset_id="123", media_kind="image", position=1),
                PastPostsAsset(selected_asset_id="456", media_kind="image", position=2),
            ),
            slide_count=2,
        ),
    )
    async with obs.uow(ingress, origin) as conn:
        slide_id = str(
            await conn.scalar(
                text(
                    "SELECT media_item_id FROM instagram_media_assets WHERE workspace_id=:ws AND source_id=:sid AND asset_media_id='456'"
                ),
                {"ws": str(origin["ws"]), "sid": world["source_id"]},
            )
        )
        post_ref = str(
            await conn.scalar(
                text(
                    "SELECT post_ref FROM instagram_media_index WHERE workspace_id=:ws AND source_id=:sid AND parent_media_id='123'"
                ),
                {"ws": str(origin["ws"]), "sid": world["source_id"]},
            )
        )
    slide = await media_row(ingress, origin, slide_id)
    assert slide["file_name"] == "Instagram post, slide 2 of 2"
    assert slide["provenance"]["slide"] == {
        "post_ref": post_ref,
        "position": 2,
        "count": 2,
    }
    assert slide["provider_file_ref"] is None and slide["caption"] is None
    calls = []
    response = await thumbnail(
        ingress,
        origin,
        slide_id,
        slide["thumbnail_version"],
        ports(calls, carousel=True),
    )
    assert response.status_code == 200 and response.content == picture()
    assert calls == [
        ("graph.instagram.com", "/v26.0/123"),
        ("graph.instagram.com", "/v26.0/123/children"),
        ("scontent-test.cdninstagram.com", "/preview.jpg"),
    ]

    # A partial child list names the position without claiming a total.
    await obs.observe(
        ingress,
        origin,
        world,
        replace(
            carousel,
            assets=(
                PastPostsAsset(selected_asset_id="123", media_kind="image", position=1),
                PastPostsAsset(selected_asset_id="456", media_kind="image", position=2),
            ),
            slide_count=2,
            slides_complete=False,
        ),
    )
    partial = await media_row(ingress, origin, slide_id)
    assert partial["file_name"] == "Instagram post, slide 2"
    assert partial["provenance"]["slide"]["count"] is None


async def test_every_read_agrees_that_a_picture_is_a_current_observation(
    ingress, origin, sync_conn
):
    """`has_thumbnail` is true exactly when a past-posts item has a current
    observation, which is exactly when its `thumbnail_version` is a pair: in
    each of the six reads that emit the two, through every change of the pair.
    The same six give the item's provenance and its name alike, and a Drive
    item, with a link and without, reads the same throughout — the web
    decides to ask for a picture from the flag alone, and how (bounded, or by
    the browser) from the provenance."""
    world = await past_posts_item(ingress, origin)
    plan = await planned_story(ingress, origin, world)
    drive = linked_drive_story(sync_conn, origin)
    chars = workspaces.THUMBNAIL_VERSION_CHARS
    plain, linked = "hash-past-posts-service"[:chars], "hash-drive-planned"[:chars]

    async def seen():
        return {
            "past_posts": await pictures(
                ingress, origin, item=world["item"], story=world["intent"], planned=plan
            ),
            "drive without a link": await pictures(
                ingress, origin, item=str(origin["media"]), story=str(origin["intent"])
            ),
            "drive with a link": await pictures(
                ingress,
                origin,
                item=str(drive["media"]),
                story=str(drive["intent"]),
                planned=str(drive["intent"]),
            ),
        }

    def reads(past_posts):
        return {
            "past_posts": dict.fromkeys(READS, past_posts),
            # `upcoming` lists planned stories only, and this one is not.
            "drive without a link": dict.fromkeys(
                READS[:-1], (False, plain, None, DRIVE_NAME)
            ),
            "drive with a link": dict.fromkeys(READS, (True, linked, None, DRIVE_NAME)),
        }

    def archived(version, original_at=ORIGINAL_AT):
        return (
            version is not None,
            version,
            expected_provenance(original_at),
            PAST_POSTS_NAME,
        )

    gid = world["generation_id"]
    v1 = await current_pair(ingress, origin, world)
    assert v1.startswith(gid + ".")
    assert await seen() == reads(archived(v1))
    # Off, and On again, name new pairs; the item keeps its observation.
    await switch(ingress, origin, world, 0)
    off = await current_pair(ingress, origin, world)
    assert off != v1 and off.startswith(gid + ".")
    assert await seen() == reads(archived(off))
    await switch(ingress, origin, world, 1)
    on = await current_pair(ingress, origin, world)
    assert on not in (v1, off) and on.startswith(gid + ".")
    assert await seen() == reads(archived(on))
    # A reauthorization is a new generation with nothing observed in it yet: no
    # read advertises a picture, and none claims one it cannot name. The item
    # is still the account's own post, and none says when it was posted.
    g2 = await foundation.ensure(ingress, origin, foundation.authorization())
    assert g2["generation_id"] != gid
    assert await seen() == reads(archived(None, original_at=None))
    # Observed again under it, both items advertise its pair.
    await obs.observe(ingress, origin, g2, obs.bundle())
    await obs.observe(ingress, origin, g2, obs.bundle("456"))
    v2 = await current_pair(ingress, origin, world)
    assert v2.startswith(g2["generation_id"] + ".")
    assert await seen() == reads(archived(v2))


def wire(rows) -> str:
    """*rows* as the routes send them, byte for byte (a time as its text)."""
    return json.dumps(rows, default=str)


async def test_the_pool_and_the_month_cut_before_they_join_their_pictures(
    ingress, origin, sync_conn
):
    """`list_media` and `intent_days` cut to the rows they return and only then
    join those rows' pictures, so a load draws `limit` pictures and `per_day` a
    day, never the library's or the month's (the plans are
    `test_intent_slot_index_gate`'s). Not a byte of what they return changes,
    over past-posts items and Drive items alike: each row of the pool is the row
    `get_media` reads for that item, in the pool's order whatever the limit,
    and each day of the month is the day view's (`list_intents`), counted whole
    and cut to its newest."""
    world = await past_posts_item(ingress, origin)
    await planned_story(ingress, origin, world)
    linked_drive_story(sync_conn, origin)
    ws, today = str(origin["ws"]), date.today()
    async with obs.uow(ingress, origin) as conn:
        pool = await workspaces.list_media(conn, workspace_id=ws)
        archived = [r for r in pool if r["provenance"] is not None]
        assert (len(pool), len(archived)) == (4, 2), "two past-posts sources, two Drive"
        assert wire(pool) == wire(
            [
                await workspaces.get_media(conn, workspace_id=ws, media_id=str(r["id"]))
                for r in pool
            ]
        )
        # Newest first, the id between two made at one instant: a stable sort
        # by the id, then by the time, reversed (a reverse sort keeps ties).
        newest_first = sorted(
            sorted(pool, key=lambda r: str(r["id"])),
            key=lambda r: r["created_at"],
            reverse=True,
        )
        assert [r["id"] for r in pool] == [r["id"] for r in newest_first]
        for limit in (1, 2, 3):
            cut = await workspaces.list_media(conn, workspace_id=ws, limit=limit)
            assert wire(cut) == wire(pool[:limit]), limit

        for per_day in (1, 2, 10):
            month = await workspaces.intent_days(
                conn,
                workspace_id=ws,
                states=workspaces.INTENT_STATES,
                from_date=today - timedelta(days=2),
                to_date=today + timedelta(days=3),
                per_day=per_day,
            )
            assert max(d["count"] for d in month) >= 3, "a day the cut can cut"
            for d in month:
                day = date.fromisoformat(d["date"])
                stories = await workspaces.list_intents(
                    conn,
                    workspace_id=ws,
                    newest_first=True,
                    from_date=day,
                    to_date=day + timedelta(days=1),
                )
                assert d["count"] == len(stories), (per_day, d["date"])
                assert len(d["newest"]) == min(per_day, len(stories))
                assert wire(d["newest"]) == wire(
                    [
                        {k: s[k] for k in workspaces.INTENT_DAY_FIELDS}
                        for s in stories[:per_day]
                    ]
                ), (per_day, d["date"])


async def test_the_wire_carries_the_flag_the_version_and_the_provenance(
    ingress, origin, sync_conn
):
    """What the web is handed by the real routes: the flag, the version and the
    provenance of a past-posts item on all six reads, under the name it has on the
    page and with no Instagram id anywhere in any of them, and the picture its
    version asks for, at the URL `thumbnailSrc` builds (`?v=` and
    `encodeURIComponent`). A Drive item beside it reads as it is stored, with no
    provenance."""
    world = await past_posts_item(ingress, origin)
    plan = await planned_story(ingress, origin, world)
    drive = linked_drive_story(sync_conn, origin)
    version = await current_pair(ingress, origin, world)
    today, base = date.today(), f"/api/v1/workspaces/{origin['ws']}"
    window = {
        "from": str(today - timedelta(days=1)),
        "to": str(today + timedelta(days=3)),
    }
    calls = []
    async with api(ingress, origin["user"], ports(calls)) as client:

        async def get(path, **params):
            resp = await client.get(f"{base}/{path}", params=params)
            assert resp.status_code == 200, resp.text
            # The whole body, the envelope and every other row included.
            assert graph_ids_in(resp.text) == [], f"{path} gave an Instagram id"
            return resp.json()

        row = await get(f"media/{world['item']}")
        library = {m["id"]: m for m in (await get("media"))["media"]}
        queue = {i["id"]: i for i in (await get("intents"))["intents"]}
        month = await get("intents/days", state="scheduled", per_day=10, **window)
        newest = {n["id"]: n for d in month["days"] for n in d["newest"]}
        coming = {p["id"]: p for p in (await get("upcoming", **window))["planned"]}

        async def six(item, story, planned):
            """One item's row on each of the six reads (`upcoming` takes a
            planned story, which may be another item's of the same source)."""
            return {
                "get_media": await get(f"media/{item}"),
                "list_media": library[item],
                "list_intents": queue[story],
                "get_intent": await get(f"intents/{story}"),
                "intent_days": newest[story],
                "upcoming": coming[planned],
            }

        wire = await six(world["item"], world["intent"], plan)
        assert wire["get_media"] == row
        assert {
            read: (r["has_thumbnail"], r["thumbnail_version"])
            for read, r in wire.items()
        } == dict.fromkeys(READS, (True, version))
        # Every row of the pool carries `provenance`: the past-posts item's three
        # facts and nothing of Instagram's ids; null for a Drive item.
        assert all("provenance" in m for m in library.values())
        provenance = row["provenance"]
        assert set(provenance) == {"provider", "origin_handle", "original_at", "slide"}
        assert provenance["slide"] is None
        assert provenance["provider"] == "instagram_past_posts"
        assert provenance["origin_handle"] == HANDLE
        assert provenance["original_at"].startswith("2020-01-01T00:00:00")
        assert library[world["item"]]["provenance"] == provenance
        assert library[str(origin["media"])]["provenance"] is None
        assert not {"permalink", "parent_media_id", "first_child_id"} & set(row)
        assert not any(
            "thumbnail_url" in r for r in (*wire.values(), *library.values())
        )
        # All six give the same provenance, so the Queue, the day view and the
        # calendar's chips tell this picture from a Drive picture as the library
        # does, and the same name, never `Instagram <id>` nor the id as a reference.
        assert {read: r["provenance"] for read, r in wire.items()} == dict.fromkeys(
            READS, provenance
        )
        assert {read: r["file_name"] for read, r in wire.items()} == dict.fromkeys(
            READS, PAST_POSTS_NAME
        )
        archived = [m for m in library.values() if m["provenance"] is not None]
        assert len(archived) == 2, "both observed items, whatever their story"
        assert {(m["file_name"], m["provider_file_ref"]) for m in archived} == {
            (PAST_POSTS_NAME, None)
        }
        # The positive control: a Drive item, with a link to draw, on the same
        # six reads, is what it is stored as and has no provenance.
        chars = workspaces.THUMBNAIL_VERSION_CHARS
        plain = await six(
            str(drive["media"]), str(drive["intent"]), str(drive["intent"])
        )
        assert {read: r["provenance"] for read, r in plain.items()} == dict.fromkeys(
            READS, None
        )
        assert {read: r["file_name"] for read, r in plain.items()} == dict.fromkeys(
            READS, DRIVE_NAME
        )
        assert {
            read: (r["has_thumbnail"], r["thumbnail_version"])
            for read, r in plain.items()
        } == dict.fromkeys(READS, (True, "hash-drive-planned"[:chars]))
        assert plain["get_media"]["provider_file_ref"] == "ref-drive-planned"
        assert plain["list_media"]["provider_file_ref"] == "ref-drive-planned"
        assert library[str(origin["media"])]["provider_file_ref"] == (
            "ref-past-posts-service"
        )
        # Advertising a picture asks no provider; only the request for it does.
        assert calls == []
        # The version goes into the URL as the web builds it; the route takes it.
        encoded = quote(row["thumbnail_version"], safe="-_.!~*'()")
        served = await client.get(f"{base}/media/{world['item']}/thumbnail?v={encoded}")
        assert served.status_code == 200 and served.content == picture()
        assert served.headers["cache-control"] == NO_STORE
        assert [host for host, _ in calls] == [
            "graph.instagram.com",
            "scontent-test.cdninstagram.com",
        ]
        # Reauthorized and not yet observed again: no picture is advertised, and
        # the wire says so with false and null, never a version it cannot name.
        await foundation.ensure(ingress, origin, foundation.authorization())
        waiting = await get(f"media/{world['item']}")
        assert (waiting["has_thumbnail"], waiting["thumbnail_version"]) == (False, None)
        assert waiting["provenance"]["original_at"] is None
        assert waiting["provenance"]["origin_handle"] == HANDLE
        assert waiting["file_name"] == PAST_POSTS_NAME
    # The probe could have found one: the ids are what the items are stored
    # under, their file reference, while the name stored is the neutral one
    # (`instagram_catalog._project`) and the reads, a projection, rewrote nothing.
    async with obs.uow(ingress, origin) as conn:
        stored = (
            await conn.execute(
                text(
                    "SELECT file_name, provider_file_ref FROM media_items"
                    " WHERE source_id = :sid ORDER BY provider_file_ref"
                ),
                {"sid": world["source_id"]},
            )
        ).all()
    assert [tuple(r) for r in stored] == [
        (PAST_POSTS_NAME, "123"),
        (PAST_POSTS_NAME, "456"),
    ]
    assert graph_ids_in(" ".join(c for r in stored for c in r)) == list(GRAPH_IDS)


async def test_the_card_and_the_notice_name_a_past_posts_item_as_a_post_never_by_its_id(
    ingress, origin, monkeypatch
):
    """The Telegram surfaces read the stored name, not the web's projection: the
    card from `prompts._CARD_WITH_PROVIDER` and the missed-story notice from
    `prompts._NOTICE_SELECT`, over the rows the catalog stored. Neither says an
    Instagram id, and the Drive story's card beside it names its file and
    carries the reference its bytes are fetched by."""
    world = await past_posts_item(ingress, origin)
    ws, story, drive_story = str(origin["ws"]), world["intent"], str(origin["intent"])
    async with obs.uow(ingress, origin) as conn:
        archived = await readers.row(
            conn, prompts._CARD_WITH_PROVIDER + " WHERE card.id = :id", id=story
        )
        plain = await readers.row(
            conn, prompts._CARD_WITH_PROVIDER + " WHERE card.id = :id", id=drive_story
        )
        notice = await readers.row(
            conn,
            prompts._NOTICE_SELECT + " WHERE i.workspace_id = :ws AND i.id = :id",
            ws=ws,
            id=story,
        )
    # What both surfaces read is the stored name, and the id is only the reference.
    assert archived["file_name"] == notice["file_name"] == PAST_POSTS_NAME
    assert archived["provider_file_ref"] == "123"

    card = prompts._card_for(archived, intent_id=story)
    slot = prompts.stamp(archived["schedule_slot_at"], archived["tz"])
    assert card["text"] == f"📸 {PAST_POSTS_NAME} (image)\nSlot: {slot}"
    assert card["caption"] == f"📸 @{HANDLE}\n{PAST_POSTS_NAME}\nSlot: {slot}"
    assert card["media"]["file_name"] == PAST_POSTS_NAME
    assert card["media"]["intent_id"] == story
    assert "ref" not in card["media"]
    assert graph_ids_in(json.dumps(card, default=str)) == []

    sent = []

    async def fanout(session, **kwargs):
        sent.append(kwargs)
        return 1

    monkeypatch.setattr(prompts.outbox, "fanout_notification", fanout)
    await prompts.say_not_served(
        object(), notice, reason="late", surface=["binding"], by=None
    )
    [told] = sent
    assert told["intent_id"] == story
    assert told["text"] == (
        f"🗓 Not served: {PAST_POSTS_NAME} for @{HANDLE}, scheduled for "
        f"{prompts.stamp(notice['schedule_slot_at'], notice['tz'])}: "
        f"{prompts.MISS_REASONS['late']}. Nothing was posted."
    )
    assert graph_ids_in(told["text"]) == []

    # The positive control: a Drive story's card names its file and still carries
    # the reference its bytes are fetched by, and no intent.
    control = prompts._card_for(plain, intent_id=drive_story)
    assert control["media"]["file_name"] == DRIVE_NAME
    assert control["media"]["ref"] == plain["provider_file_ref"]
    assert "intent_id" not in control["media"]
    assert control["text"].startswith(f"📸 {DRIVE_NAME} (")


@pytest.mark.parametrize("version", ["stale-epoch", "other-generation", "nonsense"])
async def test_a_version_that_is_not_the_current_pair_asks_no_provider(
    ingress, origin, caplog, version
):
    world = await past_posts_item(ingress, origin)
    gid, epoch = (await current_pair(ingress, origin, world)).split(".")
    v = {
        "stale-epoch": f"{gid}.{int(epoch) - 1}",
        "other-generation": f"{uuid.uuid4()}.{epoch}",
        "nonsense": "x",
    }[version]
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(ingress, origin, world["item"], v, unreachable())
    assert_placeholder(resp)
    assert refused(caplog) == ["stale_version"]


async def test_an_off_source_is_refused_before_any_lookup(ingress, origin, caplog):
    world = await past_posts_item(ingress, origin)
    await switch(ingress, origin, world, 0)
    # Off moves the epoch, so the reads name a new pair; even that is refused.
    current = await current_pair(ingress, origin, world)
    assert (await media_row(ingress, origin, world["item"]))["thumbnail_version"] == (
        current
    )
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(ingress, origin, world["item"], current, unreachable())
    assert_placeholder(resp)
    assert refused(caplog) == ["source_off"]


async def test_a_deletion_pending_generation_is_refused_before_any_lookup(
    ingress, origin, caplog
):
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)
    async with obs.uow(ingress, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_past_posts_generations SET lifecycle = 'deletion_pending'"
                " WHERE id = :gid"
            ),
            {"gid": world["generation_id"]},
        )
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(ingress, origin, world["item"], version, unreachable())
    assert_placeholder(resp)
    assert refused(caplog) == ["generation_unavailable"]


async def test_without_the_activation_capability_no_provider_is_asked(
    ingress, origin, caplog
):
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)
    calls = []
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(
            ingress, origin, world["item"], version, ports(calls, enabled=False)
        )
    assert_placeholder(resp)
    assert calls == [] and refused(caplog) == ["past_posts_not_activated"]


@pytest.mark.parametrize("moment", ["during_download", "before_send"])
async def test_off_then_on_after_admission_sends_no_bytes(
    ingress, origin, caplog, moment
):
    """The epoch moves twice while the preview is in flight: the per-call
    admission catches it as the download returns, and the fence catches it
    once every provider request has passed, before a byte is sent."""
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)

    async def off_then_on():
        await switch(ingress, origin, world, 0)
        await switch(ingress, origin, world, 1)

    calls = []
    hook = {"during_download": "on_download", "before_send": "after_fetch"}[moment]
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(
            ingress, origin, world["item"], version, ports(calls, **{hook: off_then_on})
        )
    assert_placeholder(resp)
    assert picture() not in resp.content
    assert [host for host, _ in calls] == [
        "graph.instagram.com",
        "scontent-test.cdninstagram.com",
    ]
    assert refused(caplog) == ["source_epoch_changed"]
    # On again under a new pair: the old one stays refused, the new one serves.
    current = await current_pair(ingress, origin, world)
    assert current != version
    assert (
        await thumbnail(ingress, origin, world["item"], version, unreachable())
    ).status_code == 404
    again = []
    served = await thumbnail(ingress, origin, world["item"], current, ports(again))
    assert served.status_code == 200 and served.content == picture()


async def test_a_reauthorization_makes_the_old_generations_version_stale(
    ingress, origin, caplog
):
    world = await past_posts_item(ingress, origin)
    v1 = await current_pair(ingress, origin, world)
    g2 = await foundation.ensure(ingress, origin, foundation.authorization())
    assert g2["generation_id"] != world["generation_id"]
    # Awaiting reobservation under G2, the item advertises no picture at all.
    row = await media_row(ingress, origin, world["item"])
    assert (row["has_thumbnail"], row["thumbnail_version"]) == (False, None)
    assert row["provenance"]["original_at"] is None
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(ingress, origin, world["item"], v1, unreachable())
    assert_placeholder(resp)
    assert refused(caplog) == ["no_current_observation"]

    landed = await obs.observe(ingress, origin, g2, obs.bundle())
    assert landed.media_item_id == world["item"]
    v2 = await current_pair(ingress, origin, world)
    assert v2.startswith(g2["generation_id"] + ".")
    assert (await media_row(ingress, origin, world["item"]))["thumbnail_version"] == v2
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        stale = await thumbnail(ingress, origin, world["item"], v1, unreachable())
    assert_placeholder(stale)
    assert refused(caplog) == ["stale_version"]
    calls = []
    served = await thumbnail(ingress, origin, world["item"], v2, ports(calls))
    assert served.status_code == 200 and len(calls) == 2


async def test_another_workspaces_member_reaches_nothing(ingress, origin, sync_conn):
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)
    other = seed_workspace_chain(sync_conn, "preview-other")
    # Their own workspace, naming this item: the item is not theirs.
    async with api(ingress, other["user"], unreachable()) as client:
        resp = await client.get(
            f"/api/v1/workspaces/{other['ws']}/media/{world['item']}/thumbnail",
            params={"v": version},
        )
        assert resp.status_code == 404
        # This workspace: they are not a member of it.
        resp = await client.get(
            f"/api/v1/workspaces/{origin['ws']}/media/{world['item']}/thumbnail",
            params={"v": version},
        )
        assert resp.status_code == 404
    calls = []
    served = await thumbnail(ingress, origin, world["item"], version, ports(calls))
    assert served.status_code == 200, "the owner is still served the same item"


@pytest.fixture
def quick_locks(monkeypatch):
    """The preview's lock wait shortened, so a test need not sit through it:
    the bound is the constant's, and only how long it is has changed."""
    monkeypatch.setattr(past_posts_preview, "PREVIEW_LOCK_TIMEOUT", "300ms")


@contextmanager
def writer_holding(sync_conn, chain):
    """The workspace's row locked by another connection, as a writer that has
    not committed holds it, until the block ends."""
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM workspaces WHERE id = %s FOR UPDATE", (chain["ws"],)
        )
    try:
        yield
    finally:
        sync_conn.rollback()


WAITING_AT = {
    "before_admission": [],
    "during_a_request_check": ["graph.instagram.com", "scontent-test.cdninstagram.com"],
    "before_fence": ["graph.instagram.com", "scontent-test.cdninstagram.com"],
}


@pytest.mark.parametrize("moment", list(WAITING_AT))
async def test_a_lock_wait_past_the_timeout_is_the_placeholder_not_a_queue(
    ingress, origin, sync_conn, caplog, quick_locks, moment
):
    """The preview's own transactions and each per-request check wait at most
    the timeout for a writer's lock: the same request with the writer holding the
    workspace at the admission, at a request's check after the CDN answers, and
    at the fence after the bytes are in hand is the placeholder, within the
    bound. No byte is sent, and the writer gone, the request is served."""
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)
    calls = []
    with ExitStack() as locks:

        async def take():
            locks.enter_context(writer_holding(sync_conn, origin))

        if moment == "before_admission":
            await take()
        hooks = {
            "before_admission": {},
            "during_a_request_check": {"on_download": take},
            "before_fence": {"after_fetch": take},
        }[moment]
        began = time.monotonic()
        with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
            # A bound that did not hold would sit here until the writer left.
            resp = await asyncio.wait_for(
                thumbnail(
                    ingress, origin, world["item"], version, ports(calls, **hooks)
                ),
                timeout=20,
            )
        waited = time.monotonic() - began
    assert_placeholder(resp)
    assert picture() not in resp.content
    # 55P03: the database refused the wait. Never the statement's text.
    assert refused(caplog) == ["database_55P03"]
    assert [host for host, _ in calls] == WAITING_AT[moment]
    assert waited < 10

    # Control: nothing else was wrong. Without the writer the same request,
    # under the same pair, is served.
    again = []
    served = await thumbnail(ingress, origin, world["item"], version, ports(again))
    assert served.status_code == 200 and served.content == picture()


@pytest.mark.parametrize(
    "failure,reason",
    [
        (pg_errors.DeadlockDetectedError("deadlock detected"), "database_40P01"),
        (pg_errors.LockNotAvailableError("lock timeout"), "database_55P03"),
        (ConnectionResetError("connection reset"), "database_error"),
    ],
    ids=["a_deadlock", "a_lock_timeout", "a_dropped_connection"],
)
async def test_a_database_failure_in_a_request_is_the_placeholder_whatever_its_cause(
    ingress, origin, caplog, failure, reason
):
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)

    async def prepare(capture, admission_provider):
        raise DBAPIError("SELECT secret_statement", {"v": "secret-bind"}, failure)

    failing = past_posts_preview.PastPostsPreviewPorts(foundation.capability(), prepare)
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(ingress, origin, world["item"], version, failing)
    assert_placeholder(resp)
    assert refused(caplog) == [reason]
    # The statement and its binds are in the exception, and nowhere in the log.
    assert "secret" not in " ".join(r.getMessage() for r in caplog.records)

    # Control: the same item under working ports is served.
    served = await thumbnail(ingress, origin, world["item"], version, ports([]))
    assert served.status_code == 200 and served.content == picture()


async def test_a_graph_reply_over_the_byte_cap_is_the_placeholder(
    ingress, origin, caplog
):
    """An oversized Graph reply is a named, retryable adapter failure and
    becomes the bounded preview placeholder without any CDN download."""
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)
    calls = []
    with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
        resp = await thumbnail(
            ingress, origin, world["item"], version, ports(calls, graph_cap=256)
        )
    assert_placeholder(resp)
    assert [host for host, _ in calls] == ["graph.instagram.com"]
    assert refused(caplog) == ["graph_response_too_large"]

    # Control: a Graph reply inside the cap is served.
    served = await thumbnail(ingress, origin, world["item"], version, ports([]))
    assert served.status_code == 200 and served.content == picture()


def another_origin(sync_conn):
    """A second workspace with its own account and grant, as `origin` is."""
    chain = seed_workspace_chain(sync_conn, "preview-second")
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider,"
            " encrypted_payload) VALUES (%s, %s, 'ig_login', 'test-ciphertext')"
            " RETURNING id",
            (chain["ws"], chain["iga"]),
        )
        chain["credential"] = cur.fetchone()[0]
    sync_conn.commit()
    return chain


async def test_a_third_concurrent_preview_of_a_workspace_is_the_placeholder_and_asks_no_provider(
    ingress, origin, sync_conn, caplog
):
    """Two previews of one workspace in flight, held at the CDN. A third, from
    another tab or a client that is not the web, is the placeholder at once: no
    Graph call, no CDN call, and no statement to queue on the workspace's locks.
    Another workspace, served by the same port, is not held up, and every slot is
    free again once the two finish."""
    world = await past_posts_item(ingress, origin)
    version = await current_pair(ingress, origin, world)
    second = another_origin(sync_conn)
    elsewhere = await past_posts_item(ingress, second)
    elsewhere_version = await current_pair(ingress, second, elsewhere)

    calls = []
    hold = Hold(expect=2)
    port = past_posts_preview.PastPostsPreview(
        ports(calls, hold={str(origin["ws"]): hold})
    )
    here = f"/api/v1/workspaces/{origin['ws']}/media/{world['item']}/thumbnail"
    there = f"/api/v1/workspaces/{second['ws']}/media/{elsewhere['item']}/thumbnail"
    in_flight = []
    try:
        async with api(ingress, origin["user"], preview=port) as client:
            async with api(ingress, second["user"], preview=port) as other:
                with caplog.at_level(logging.INFO, logger=past_posts_preview.__name__):
                    in_flight = [
                        asyncio.create_task(client.get(here, params={"v": version}))
                        for _ in range(2)
                    ]
                    await asyncio.wait_for(hold.reached.wait(), timeout=20)
                    asked = list(calls)
                    assert len(asked) == 4, "both previews are at the CDN"
                    assert port._in_flight == {str(origin["ws"]): 2}

                    # A cap that did not hold would leave this at the held CDN.
                    third = await asyncio.wait_for(
                        client.get(here, params={"v": version}), timeout=20
                    )
                    assert_placeholder(third)
                    assert list(calls) == asked, "the third asked no provider"
                    assert refused(caplog) == ["preview_slots_busy"]

                    served = await other.get(there, params={"v": elsewhere_version})
                    assert served.status_code == 200, served.text
                    assert served.content == picture()
                    assert len(calls) == len(asked) + 2

                    hold.release.set()
                    done = await asyncio.wait_for(asyncio.gather(*in_flight), 20)
                assert [r.status_code for r in done] == [200, 200]
                assert all(r.content == picture() for r in done)
                assert port._in_flight == {}, "nothing is kept once none is in flight"

                # Control: both slots are free again.
                again = await client.get(here, params={"v": version})
                assert again.status_code == 200 and again.content == picture()
    finally:
        hold.release.set()
        for task in in_flight:
            task.cancel()
