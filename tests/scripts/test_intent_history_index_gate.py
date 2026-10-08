"""The outcomes indexed by slot, and the calendar reads over them (#1640, #1634).

`ix_intents_history_slot` holds the posted, skipped and rejected stories by
`(workspace_id, schedule_slot_at)`. `intent_days` (the calendar's month) and
`list_intents` with `from_date`/`to_date` (its day view) bound the slot to a
workspace's local days, read in the workspace's own zone.

Two workspaces, seeded through the stream login:

- BULK: three thousand posted stories five hours apart, about twenty months of
  posting, so one month on the calendar is a small range of the table. It is
  where #1640's acceptance runs: EXPLAIN of the month read shows the index.
- NEW YORK (`America/New_York`): stories placed on either side of local
  midnight in July, five posted on one day with a skipped story beside them,
  and a story still scheduled.

Every read runs as `svc_ingress` under its workspace's tenant, the API's login.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import unit_of_work, workspaces
from tests.scripts.conftest import (
    _scratch,
    as_user,
    async_url,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

INDEX = "ix_intents_history_slot"
BULK_POSTED = 3000


def _bulk_posted(cur, chain, *, n: int, tag: str) -> None:
    """*n* posted stories, one a media item, five hours apart going back from now."""
    cur.execute(
        "WITH m AS ("
        "  INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
        "                           media_kind, provider_file_ref)"
        "  SELECT %(ws)s, %(src)s, %(tag)s || '-hash-' || g, %(tag)s || '-' || g || '.jpg',"
        "         'image', %(tag)s || '-ref-' || g"
        "    FROM generate_series(1, %(n)s) g"
        "  RETURNING id, content_hash)"
        " INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        "   provider_account_ref, approval_mode, published_via, schedule_slot_at, state)"
        " SELECT %(ws)s, %(iga)s, m.id, %(ref)s, 'manual', 'manual',"
        "        now() - make_interval(hours => 5 * (row_number() OVER"
        "          (ORDER BY m.content_hash))::int), 'posted'"
        "   FROM m",
        {
            "ws": chain["ws"],
            "src": chain["src"],
            "iga": chain["iga"],
            "ref": f"acct-{tag}",
            "tag": tag,
            "n": n,
        },
    )


def _story(cur, chain, tag: str, name: str, *, at: str, state: str) -> str:
    """One story at the instant *at*, under its own media item named *name*."""
    cur.execute(
        "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
        " media_kind, provider_file_ref)"
        " VALUES (%s, %s, %s, %s, 'image', %s) RETURNING id",
        (chain["ws"], chain["src"], f"{tag}-{name}", name, f"{tag}-ref-{name}"),
    )
    media = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, published_via, schedule_slot_at, state)"
        " VALUES (%s, %s, %s, %s, 'manual', 'manual', %s::timestamptz, %s) RETURNING id",
        (chain["ws"], chain["iga"], media, f"acct-{tag}", at, state),
    )
    return str(cur.fetchone()[0])


def _seed(conn) -> dict:
    bulk = seed_workspace_chain(conn, "history-bulk")
    ny = seed_workspace_chain(conn, "history-ny")
    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.actor_kind = 'migration'")
        _bulk_posted(cur, bulk, n=BULK_POSTED, tag="history-bulk")
        cur.execute(
            "UPDATE workspaces SET tz = 'America/New_York' WHERE id = %s", (ny["ws"],)
        )
        stories = {
            # 03:30Z on the 10th is 23:30 on the 9th in New York (EDT, UTC-4).
            "late": _story(
                cur,
                ny,
                "history-ny",
                "late.jpg",
                at="2026-07-10 03:30+00",
                state="posted",
            ),
            # 05:00Z on the 10th is 01:00 on the 10th in New York.
            "early": _story(
                cur,
                ny,
                "history-ny",
                "early.jpg",
                at="2026-07-10 05:00+00",
                state="posted",
            ),
            "skipped": _story(
                cur,
                ny,
                "history-ny",
                "skipped.jpg",
                at="2026-07-15 14:30+00",
                state="skipped",
            ),
            "waiting": _story(
                cur,
                ny,
                "history-ny",
                "waiting.jpg",
                at="2026-07-15 23:00+00",
                state="scheduled",
            ),
        }
        for hour in range(5):
            stories[f"busy-{hour}"] = _story(
                cur,
                ny,
                "history-ny",
                f"busy-{hour}.jpg",
                at=f"2026-07-15 {13 + hour}:00+00",
                state="posted",
            )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("ANALYZE post_intents")
    conn.commit()
    return {"bulk": str(bulk["ws"]), "ny": str(ny["ws"]), "stories": stories}


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            seeded = _seed(conn)
        finally:
            conn.close()
        yield {**seeded, "ingress": as_user(db, "svc_ingress")}
    finally:
        gen.close()


async def _as_tenant(dsn: str, workspace_id: str, read):
    """*read* on one connection under *workspace_id*'s tenant, the way the API
    reads: rolled back, so the estate stays as seeded."""
    engine = create_async_engine(async_url(dsn))
    try:
        async with engine.connect() as c:
            tx = await c.begin()
            await unit_of_work.apply_gucs(
                c, tenant_id=workspace_id, actor_kind="system"
            )
            result = await read(c)
            await tx.rollback()
            return result
    finally:
        await engine.dispose()


class _Explaining:
    """An executor that EXPLAINs each statement before running it, so the plan
    is the one for exactly the SQL the service built, with its parameters."""

    def __init__(self, conn):
        self.conn = conn
        self.plans: list = []

    async def execute(self, statement, params=None):
        plan = (
            await self.conn.execute(
                text("EXPLAIN (FORMAT JSON) " + str(statement)), params
            )
        ).scalar()
        self.plans.append(json.loads(plan) if isinstance(plan, str) else plan)
        return await self.conn.execute(statement, params)


def _index_names(node) -> set:
    """Every index a plan node, or any node under it, reads."""
    found = set()
    if isinstance(node, dict):
        if "Index Name" in node:
            found.add(node["Index Name"])
        for value in node.values():
            found |= _index_names(value)
    elif isinstance(node, list):
        for value in node:
            found |= _index_names(value)
    return found


def _month_read(workspace_id: str, *, explain: bool = False):
    """The calendar's month read for July 2026; with *explain*, each statement
    is EXPLAINed first and its plan returned beside the days."""

    async def read(c):
        executor = _Explaining(c) if explain else c
        days = await workspaces.intent_days(
            executor,
            workspace_id=workspace_id,
            states=["posted"],
            from_date=date(2026, 6, 29),
            to_date=date(2026, 8, 3),
            per_day=3,
        )
        return days, getattr(executor, "plans", None)

    return read


def test_the_month_read_walks_the_index(world):
    """#1640's acceptance: on a workspace with thousands of posted stories, the
    calendar's month read is a scan of `ix_intents_history_slot`."""
    days, plans = asyncio.run(
        _as_tenant(world["ingress"], world["bulk"], _month_read(world["bulk"], explain=True))
    )
    assert days, "positive control: the bulk workspace posted in that month"
    (plan,) = plans
    assert INDEX in _index_names(plan), json.dumps(plan, indent=1)


def test_the_month_counts_each_local_day_in_the_workspaces_zone(world):
    days, _ = asyncio.run(
        _as_tenant(world["ingress"], world["ny"], _month_read(world["ny"]))
    )
    by_date = {d["date"]: d for d in days}
    assert sorted(by_date) == ["2026-07-09", "2026-07-10", "2026-07-15"], (
        "03:30Z on the 10th is the 9th in New York; the skipped and the "
        "scheduled stories are not posted; the bulk workspace is not this tenant"
    )
    assert [n["file_name"] for n in by_date["2026-07-09"]["newest"]] == ["late.jpg"]
    assert [n["file_name"] for n in by_date["2026-07-10"]["newest"]] == ["early.jpg"]
    busy = by_date["2026-07-15"]
    assert busy["count"] == 5, "a count, not the length of the three names"
    assert [n["file_name"] for n in busy["newest"]] == [
        "busy-4.jpg",
        "busy-3.jpg",
        "busy-2.jpg",
    ]
    assert {n["state"] for n in busy["newest"]} == {"posted"}


def test_the_month_counts_whichever_outcomes_it_is_asked_for(world):
    async def read(c):
        return await workspaces.intent_days(
            c,
            workspace_id=world["ny"],
            states=["posted", "skipped"],
            from_date=date(2026, 7, 15),
            to_date=date(2026, 7, 16),
            per_day=10,
        )

    (day,) = asyncio.run(_as_tenant(world["ingress"], world["ny"], read))
    assert day["count"] == 6
    assert "skipped.jpg" in [n["file_name"] for n in day["newest"]]


def test_a_day_read_is_the_workspaces_local_day_in_every_state(world):
    async def read(c):
        return await workspaces.list_intents(
            c,
            workspace_id=world["ny"],
            from_date=date(2026, 7, 15),
            to_date=date(2026, 7, 16),
            limit=50,
        )

    rows = asyncio.run(_as_tenant(world["ingress"], world["ny"], read))
    stories = world["stories"]
    # 23:00Z on the 15th is 19:00 on the 15th in New York: still the 15th.
    assert {str(r["id"]) for r in rows} == {
        stories["skipped"],
        stories["waiting"],
        *(stories[f"busy-{h}"] for h in range(5)),
    }
    assert [r["schedule_slot_at"] for r in rows] == sorted(
        r["schedule_slot_at"] for r in rows
    ), "soonest first by default"
