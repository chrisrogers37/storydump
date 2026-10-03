"""`retention_sweep` gate — the `rate_counters` class, and nothing else (`05` retention).

The executor is run exactly as the worker's registry runs it: connected as
`svc_worker`, no tenant, the `system` actor, at `WorkerConfig`'s numbers. It
must delete every `rate_counters` row whose window started more than 7 days
ago, keep every newer one, and leave every other table alone — including rows
the door's other classes WOULD delete (an aged cap-ledger day, an aged
succeeded job, an aged archive table), because those classes are not built.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest
from sqlalchemy import text

from tests.scripts.conftest import (
    _scratch,
    as_user,
    ingress_engine,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: (scope, key, age) — ages either side of the 7-day line, across scopes.
OLD = [
    ("preauth_ip", "203.0.113.7", "8 days"),
    ("preauth_ip", "waitlist:visitor:2001:db8:1::/64", "30 days"),
    ("tg_global", "", "7 days 1 minute"),
]
NEW = [
    ("preauth_ip", "203.0.113.7", "6 days 23 hours"),
    ("preauth_ip", "waitlist:accepted", "1 minute"),
    ("tg_chat", "binding-1", "0 seconds"),
]


@pytest.fixture(scope="module")
def sweep_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "retention-gate")
            with conn.cursor() as cur:
                for scope, key, age in OLD + NEW:
                    cur.execute(
                        "INSERT INTO rate_counters (scope, key, window_start, count)"
                        " VALUES (%s, %s, date_trunc('minute', now())"
                        " - %s::interval, 1)",
                        (scope, key, age),
                    )
                # Rows the door's OTHER classes would take, at any keep.
                cur.execute(
                    "INSERT INTO daily_post_counts"
                    " (workspace_id, ig_account_id, local_date, count, cap_at_write)"
                    " VALUES (%s, %s, (now() - interval '900 days')::date, 0, 5)",
                    (chain["ws"], chain["iga"]),
                )
                cur.execute(
                    "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
                    " payload, max_attempts, state, updated_at)"
                    " VALUES (NULL, 'reap_expired', 'bulk', %s, '{\"v\": 1}', 3,"
                    " 'succeeded', now() - interval '900 days')",
                    (f"old:{uuid.uuid4()}",),
                )
                cur.execute(
                    "CREATE TABLE archive.audit_export_20200101_000000_000000 (id int)"
                )
            conn.commit()
        finally:
            conn.close()
        yield dsn
    finally:
        gen.close()


def _counts(dsn) -> dict:
    """Row count of every table in `public` and `archive`, as the owner."""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_schema, table_name FROM information_schema.tables"
                " WHERE table_schema IN ('public', 'archive')"
                " AND table_type = 'BASE TABLE'"
            )
            out = {}
            for schema, table in cur.fetchall():
                cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
                out[f"{schema}.{table}"] = cur.fetchone()[0]
            return out
    finally:
        conn.close()


def _rate_rows(dsn) -> set:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT scope, key, window_start FROM rate_counters")
            return set(cur.fetchall())
    finally:
        conn.close()


async def _sweep_as_worker(owner_dsn: str) -> int:
    from src.services.target import scheduler
    from src.services.target.unit_of_work import apply_gucs
    from src.services.target.work_loop import WorkerConfig, WorkerDeps, build_registry

    async with ingress_engine(as_user(owner_dsn, "svc_worker")) as engine:
        registry = build_registry(WorkerDeps(engine=engine, config=WorkerConfig()))
        async with engine.begin() as conn:
            who = (await conn.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_worker", who
            await apply_gucs(conn, tenant_id="", actor_kind="system")
            await registry["retention_sweep"](conn, {"kind": "retention_sweep"})
        # A second pass reports what is left to delete: nothing.
        async with engine.begin() as conn:
            await apply_gucs(conn, tenant_id="", actor_kind="system")
            cfg = WorkerConfig()
            return await scheduler.execute_retention_sweep(
                conn,
                keep_seconds=cfg.rate_counters_keep_seconds,
                batch=cfg.retention_batch,
            )


def test_the_sweep_deletes_only_rate_counter_rows_older_than_seven_days(sweep_db):
    before_counts = _counts(sweep_db)
    before_rows = _rate_rows(sweep_db)
    assert len(before_rows) == len(OLD) + len(NEW)

    left = asyncio.run(_sweep_as_worker(sweep_db))

    after_counts = _counts(sweep_db)
    after_rows = _rate_rows(sweep_db)
    assert left == 0
    # Positive control: exactly the old rows went, every new one survived.
    assert len(after_rows) == len(NEW)
    assert {(s, k) for s, k, _ in after_rows} == {(s, k) for s, k, _ in NEW}
    # And no other table moved, the aged rows of the unbuilt classes included.
    assert after_counts.pop("public.rate_counters") == len(NEW)
    before_counts.pop("public.rate_counters")
    assert after_counts == before_counts
    assert after_counts["public.daily_post_counts"] >= 1
    assert after_counts["archive.audit_export_20200101_000000_000000"] == 0
    assert "archive.audit_export_20200101_000000_000000" in after_counts
