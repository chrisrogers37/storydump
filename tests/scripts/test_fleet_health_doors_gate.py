"""The fleet health surfaces read the whole estate under the ingress role (#751).

`/health/scheduling` and `/health/posting` count across every workspace —
stalled cursors, active destinations, landings, the debited cap ledger, the
ready lanes, the pending outbox — and the fleet monitors take their verdicts
from those counts. Until 081 the reads behind them went straight at the tenant
tables with no tenant set, which the owner login could do because it bypasses
row-level security. The runtime login cannot: the first switch of the API to
`svc_ingress` (2026-09-20) turned `posted_ever 104` into `never-posted` and
`accounts_active 2` into `no-signal` while nothing in the estate changed, and
the monitors went blind. Both modules had documented the gap and named #751 as
the place to close it with a definer door.

This gate runs the surfaces AS `svc_ingress` against a seeded estate and
expects the estate. Beside it, a plain read of the same tables as the same
login expects NOTHING — so a harness where the policies were off, or a table
that lost its policy, cannot pass by accident. The owner login is the control:
the doors and the direct reads must agree.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import backpressure, posting_health, scheduling_health
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = pytest.mark.integration


def _async_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """One workspace with an active account, one scheduled and one posted
    intent, one debited cap-ledger day, one ready tenant job on the
    interactive lane and one pending outbox row — seeded as the migration
    actor, the one actor the ledger's insert guard lets write a terminal
    state (055: "post_intents are born scheduled" for the runtime)."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            chain = seed_workspace_chain(conn, "fleet")
            ws, iga, media = chain["ws"], chain["iga"], chain["media"]
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO post_intents (workspace_id, ig_account_id,"
                    " media_item_id, provider_account_ref, approval_mode,"
                    " schedule_slot_at, state, published_via, cap_consumed_on,"
                    " entered_state_at)"
                    " VALUES (%s, %s, %s, 'acct-fleet', 'manual', now(), 'posted',"
                    " 'manual', current_date, now() - interval '5 minutes')",
                    (ws, iga, media),
                )
                cur.execute(
                    "INSERT INTO daily_post_counts (workspace_id, ig_account_id,"
                    " local_date, count, cap_at_write)"
                    " VALUES (%s, %s, current_date, 1, 3)",
                    (ws, iga),
                )
                cur.execute(
                    "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                    " run_at, max_attempts, payload, state)"
                    " VALUES ('publish_pipeline', %s, 'interactive', %s,"
                    " now() - interval '1 minute', 3, '{\"v\": 1}'::jsonb, 'ready')",
                    (ws, f"publish:{uuid.uuid4()}"),
                )
                cur.execute(
                    "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                    " VALUES (%s, 'telegram_group', '-1000000000001') RETURNING id",
                    (ws,),
                )
                binding = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO channel_outbox (workspace_id, binding_id, kind,"
                    " payload, state)"
                    " VALUES (%s, %s, 'approval_prompt', '{\"v\": 1}'::jsonb, 'pending')",
                    (ws, binding),
                )
            conn.commit()
        finally:
            conn.close()
        yield {
            "ingress": as_user(db, "svc_ingress"),
            "worker": as_user(db, "svc_worker"),
            # The database-owner actor: the login production still connects as,
            # which owns the tables (so reads them past the policies) and holds
            # EXECUTE on the doors only through its memberships — the path the
            # deploy of 081 depends on BEFORE any switch, and the control arm.
            "owner": as_user(db, owner_actor),
        }
    finally:
        gen.close()


async def _pressure(c) -> dict:
    return await backpressure.snapshot(
        c,
        now=dt.datetime.now(dt.timezone.utc),
        global_limit=30,
        global_window_seconds=1,
    )


async def _surfaces(dsn: str) -> dict:
    """Everything the API's two health routes read, as one login."""
    engine = create_async_engine(_async_url(dsn))
    try:
        async with engine.connect() as c:
            return {
                "posting": await posting_health.posting_freshness(c),
                "attempts": await posting_health.publish_attempts(c),
                "destinations": await posting_health.destinations(c),
                "lag": await scheduling_health.scheduling_lag(c),
                "pressure": await _pressure(c),
            }
    finally:
        await engine.dispose()


async def _worker_signal(dsn: str) -> dict:
    """What the worker reads: its status line renders the backpressure
    snapshot and nothing else of these (the posting and lag doors are the
    API's alone)."""
    engine = create_async_engine(_async_url(dsn))
    try:
        async with engine.connect() as c:
            return await _pressure(c)
    finally:
        await engine.dispose()


_TABLES = ("post_intents", "ig_accounts", "daily_post_counts", "jobs", "channel_outbox")


async def _plain_counts(dsn: str) -> dict:
    engine = create_async_engine(_async_url(dsn))
    try:
        async with engine.connect() as c:
            return {
                t: (await c.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
                for t in _TABLES
            }
    finally:
        await engine.dispose()


async def _door_source(dsn: str) -> str:
    engine = create_async_engine(_async_url(dsn))
    try:
        async with engine.connect() as c:
            return (
                await c.execute(
                    text(
                        "SELECT prosrc FROM pg_proc"
                        " WHERE proname = 'fn_health_posting_freshness'"
                    )
                )
            ).scalar()
    finally:
        await engine.dispose()


def test_the_plain_reads_are_hidden_from_the_ingress_login(world):
    """The vacuity guard: if `svc_ingress` could read the tables directly the
    doors would prove nothing. It cannot — every table a door reads is
    policy-covered and no tenant is set (the seeded job carries a workspace,
    so `p_jobs` hides it too). The owner sees the estate."""
    assert _run(_plain_counts(world["ingress"])) == {t: 0 for t in _TABLES}
    seen = _run(_plain_counts(world["owner"]))
    assert seen["post_intents"] == 2 and seen["ig_accounts"] == 1
    assert seen["jobs"] == 1 and seen["channel_outbox"] == 1


def test_the_posting_surfaces_see_the_estate_as_svc_ingress(world):
    got = _run(_surfaces(world["ingress"]))
    assert got["posting"]["posted_ever"] == 1
    assert got["posting"]["intents_ever"] == 2
    assert got["posting"]["last_post_age_seconds"] is not None
    assert got["attempts"] == {"debited_total": 1, "ledger_days": 1}
    assert got["destinations"]["accounts_active"] == 1
    assert got["destinations"]["oldest_active_destination_age_seconds"] is not None


def test_the_scheduling_surfaces_see_the_estate_as_svc_ingress(world):
    got = _run(_surfaces(world["ingress"]))
    assert got["lag"]["accounts_active"] == 1
    assert got["pressure"]["lanes"]["interactive"]["ready"] == 1
    assert got["pressure"]["outbox_pending"] == 1
    assert got["pressure"]["ws_oldest_wait"] is not None


def test_the_worker_login_reads_the_backpressure_signal_too(world):
    """The worker's own status line renders the same snapshot (`worker.py`);
    after its switch it runs as `svc_worker`."""
    got = _run(_worker_signal(world["worker"]))
    assert got["lanes"]["interactive"]["ready"] == 1
    assert got["outbox_pending"] == 1
    assert got["ws_oldest_wait"] is not None


def test_the_doors_answer_what_a_direct_read_by_the_owner_answers(world):
    """The control: the counts the doors give `svc_ingress` are the counts the
    owner reads straight off the tables — the doors add reach, never a
    different answer. (Counts, not ages: an age ticks between two reads.)"""
    doors = _run(_surfaces(world["ingress"]))
    direct = _run(_plain_counts(world["owner"]))
    assert doors["posting"]["intents_ever"] == direct["post_intents"]
    assert doors["destinations"]["accounts_active"] == direct["ig_accounts"]
    assert doors["lag"]["accounts_active"] == direct["ig_accounts"]
    assert doors["attempts"]["ledger_days"] == direct["daily_post_counts"]
    assert doors["pressure"]["outbox_pending"] == direct["channel_outbox"]
    ready = sum(lane["ready"] for lane in doors["pressure"]["lanes"].values())
    assert ready == direct["jobs"]


def test_the_owner_login_executes_the_doors_it_deploys(world):
    """Production still connects as the owner; 081 lands under that login and
    the consumers call the doors unconditionally. The owner holds EXECUTE only
    through its memberships (the bootstrap's grants) — proved here on the
    owner actor, not on a superuser."""
    got = _run(_surfaces(world["owner"]))
    assert got["posting"]["posted_ever"] == 1
    assert got["destinations"]["accounts_active"] == 1


def test_the_live_door_filters_landings_as_the_monitor_documents(world):
    """The one `posted` row `ck_posted_complete` accepts with no provider
    evidence (`legacy_backfill`) and a dry run are excluded from every posting
    signal — the filter the monitor's pair-decoupling guard names as its
    precedent. It lives in the door's body, read here from the LIVE function
    in the replayed world rather than from the migration file: a later
    DROP+CREATE with another filter would pass a file pin and fail this one."""
    src = _run(_door_source(world["owner"]))
    assert src.count("published_via NOT IN ('legacy_backfill', 'dry_run')") == 2
