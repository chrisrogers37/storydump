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
from tests.scripts.conftest import MIGRATIONS_DIR
from tests.scripts.conftest import (
    _dsn,
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
                cur.execute("SET app.actor_kind = 'migration'")
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
            # the suite's superuser: BYPASSRLS like the owner login production
            # connected as before the switch — the control arm
            "bypass": _dsn(db.rsplit("/", 1)[-1]),
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


async def _plain_counts(dsn: str) -> dict:
    engine = create_async_engine(_async_url(dsn))
    try:
        async with engine.connect() as c:
            return {
                t: (await c.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
                for t in (
                    "post_intents",
                    "ig_accounts",
                    "daily_post_counts",
                    "channel_outbox",
                )
            }
    finally:
        await engine.dispose()


def test_the_plain_reads_are_hidden_from_the_ingress_login(world):
    """The vacuity guard: if `svc_ingress` could read the tables directly the
    doors would prove nothing. It cannot — every one is policy-covered and no
    tenant is set."""
    assert _run(_plain_counts(world["ingress"])) == {
        "post_intents": 0,
        "ig_accounts": 0,
        "daily_post_counts": 0,
        "channel_outbox": 0,
    }
    seen = _run(_plain_counts(world["bypass"]))
    assert seen["post_intents"] == 2 and seen["ig_accounts"] == 1


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


def test_the_owner_login_and_the_ingress_login_agree(world):
    """The control: what the doors answer is what a bypassing read answers."""
    ingress = _run(_surfaces(world["ingress"]))
    bypass = _run(_surfaces(world["bypass"]))

    # The COUNTS, not the ages: two reads a few milliseconds apart legitimately
    # disagree on an age by a tick, and an age is not what the door could lie about.
    def counts(got: dict) -> dict:
        return {
            "posted_ever": got["posting"]["posted_ever"],
            "intents_ever": got["posting"]["intents_ever"],
            "attempts": got["attempts"],
            "accounts_active": got["destinations"]["accounts_active"],
            "stalled": got["lag"]["stalled"],
            "lag_accounts": got["lag"]["accounts_active"],
            "ready": {k: v["ready"] for k, v in got["pressure"]["lanes"].items()},
            "outbox_pending": got["pressure"]["outbox_pending"],
        }

    assert counts(ingress) == counts(bypass)


def test_the_door_filters_landings_the_way_the_module_says():
    """`posting_health._REAL_POST` is the spelling the docs and the monitor's
    tests name; the filter itself lives in the door's body since 081. The
    two must not drift: a `posted` row with no provider evidence is excluded
    by exactly that predicate, and nowhere else."""
    ddl = (MIGRATIONS_DIR / "081_fleet_health_doors.sql").read_text()
    body = ddl.split("CREATE FUNCTION fn_health_posting_freshness()", 1)[1].split(
        "$$;", 1
    )[0]
    assert body.count(posting_health._REAL_POST) == 2
