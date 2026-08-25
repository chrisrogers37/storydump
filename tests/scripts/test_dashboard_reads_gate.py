"""The dashboard reads (#1044), measured as `svc_ingress` on the replayed schema.

Three reads virgil's held screens need and the merged API did not serve: the
media pool, a server-side stats aggregate, and a multi-state intents filter.
Each is asserted with exact numbers against seeded rows — a media item with
no intent (the case that made the pool invisible), two states in one intents
call, and a `posts_by_day` row read from the cap ledger rather than from a
list — and every read is tenant-bound: workspace B sees none of A's rows.
"""

from __future__ import annotations

import asyncio

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import workspaces
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_intent_chain,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """Two workspaces. A carries: the chain's scheduled intent; one posted
    intent (category 'food'); one skipped intent (category 'travel'); one
    media item with NO intent (category NULL, never posted); one cap-ledger
    row for today. B carries only its chain."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, "reads-a")
            b = seed_workspace_chain(conn, "reads-b")
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                posted = seed_intent_chain(
                    cur, a["ws"], "reads-a-posted", state="awaiting_approval"
                )
                skipped = seed_intent_chain(
                    cur, a["ws"], "reads-a-skipped", state="awaiting_approval"
                )
                cur.execute(
                    "UPDATE media_items SET category = 'food', times_posted = 1 WHERE id = %s",
                    (posted["media"],),
                )
                cur.execute(
                    "UPDATE media_items SET category = 'travel' WHERE id = %s",
                    (skipped["media"],),
                )
                # legal edges only: awaiting_approval → posted needs the manual proof
                cur.execute(
                    "UPDATE post_intents SET state = 'posted', published_via = 'manual',"
                    " cap_consumed_on = current_date WHERE id = %s",
                    (posted["intent"],),
                )
                cur.execute(
                    "UPDATE post_intents SET state = 'skipped' WHERE id = %s",
                    (skipped["intent"],),
                )
                cur.execute(
                    "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
                    " media_kind, provider_file_ref)"
                    " VALUES (%s, %s, 'hash-orphan', 'orphan.jpg', 'image', 'ref-orphan')"
                    " RETURNING id",
                    (a["ws"], a["src"]),
                )
                a["orphan_media"] = str(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO daily_post_counts"
                    " (workspace_id, ig_account_id, local_date, count, cap_at_write)"
                    " VALUES (%s, %s, current_date, 2, 3)",
                    (a["ws"], posted["iga"]),
                )
            conn.commit()
            a["posted"] = posted
            a["skipped"] = skipped
        finally:
            conn.close()
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress"), "a": a, "b": b}
    finally:
        gen.close()


def _read(world, ids, fn, **kwargs):
    async def main():
        engine = create_async_engine(asyncpg_url(world["ingress"]), poolclass=NullPool)
        try:
            uow = unit_of_work(
                engine,
                str(ids["ws"]),
                actor_kind="user",
                actor_user_id=str(ids["user"]),
                channel="web",
            )
            async with uow.begin() as session:
                return await fn(session, workspace_id=str(ids["ws"]), **kwargs)
        finally:
            await engine.dispose()

    return asyncio.run(main())


class TestTheMediaPool:
    def test_lists_the_whole_library_including_items_with_no_intent(self, world):
        a = world["a"]
        rows = _read(world, a, workspaces.list_media)
        ids = {r["id"] if isinstance(r["id"], str) else str(r["id"]) for r in rows}
        assert a["orphan_media"] in ids, (
            "an item with no intent is invisible — the pool was the gap"
        )
        assert len(rows) == 4  # chain + posted + skipped + orphan

    def test_never_posted_and_state_narrow_it(self, world):
        a = world["a"]
        never = _read(world, a, workspaces.list_media, never_posted=True)
        assert all(r["times_posted"] == 0 for r in never) and len(never) == 3
        assert _read(world, a, workspaces.list_media, state="removed") == []

    def test_get_is_workspace_bound(self, world):
        a, b = world["a"], world["b"]
        assert (
            _read(world, a, workspaces.get_media, media_id=a["orphan_media"])[
                "file_name"
            ]
            == "orphan.jpg"
        )
        assert _read(world, b, workspaces.get_media, media_id=a["orphan_media"]) is None
        assert _read(world, b, workspaces.list_media) and all(
            str(r["id"]) != a["orphan_media"]
            for r in _read(world, b, workspaces.list_media)
        )


class TestMultiStateIntents:
    def test_two_states_in_one_call(self, world):
        a = world["a"]
        rows = _read(world, a, workspaces.list_intents, states=["posted", "skipped"])
        assert sorted(r["state"] for r in rows) == ["posted", "skipped"]
        assert {str(r["id"]) for r in rows} == {
            str(a["posted"]["intent"]),
            str(a["skipped"]["intent"]),
        }
        rows = _read(world, a, workspaces.list_intents)
        assert len(rows) == 3
        # the queue's account column (#1033): present on every row, NULL when
        # the seeded account carries no handle — never a missing key
        assert all("account_handle" in r and "account_display_name" in r for r in rows)


class TestStats:
    def test_counts_are_exact_and_from_the_tables_not_a_list(self, world):
        a = world["a"]
        s = _read(world, a, workspaces.stats)
        assert s["intents_by_state"] == {"scheduled": 1, "posted": 1, "skipped": 1}
        assert s["media_by_state"] == {"available": 4}
        assert s["media_never_posted"] == 3
        assert s["media_by_category"] == {"": 2, "food": 1, "travel": 1}
        assert s["posted_by_category"] == {"food": 1}
        assert s["accounts"] == 3 and s["sources"] == 3
        (day,) = s["posts_by_day"]
        assert (day["count"], day["cap"]) == (2, 3)

    def test_the_other_tenant_counts_only_its_own(self, world):
        s = _read(world, world["b"], workspaces.stats)
        assert s["intents_by_state"] == {"scheduled": 1}
        assert s["media_by_state"] == {"available": 1}
        assert s["posts_by_day"] == [] and s["posted_by_category"] == {}
