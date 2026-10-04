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

from src.services.target import scheduler
from src.services.target.unit_of_work import make_session_for
from src.services.target.work_loop import WorkerConfig

from tests.scripts.conftest import (
    _scratch,
    as_user,
    execute,
    fetch_all,
    fetch_one,
    ingress_engine,
    replay_advertised_stream,
    run_as_worker,
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
NEW_KEYS = sorted((s, k) for s, k, _ in NEW)


@pytest.fixture(scope="module")
def migrated_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "retention-gate")
            with conn.cursor() as cur:
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


@pytest.fixture()
def sweep_db(migrated_db):
    """The migrated database with `rate_counters` reset to `OLD + NEW`."""
    execute(migrated_db, "DELETE FROM rate_counters")
    for scope, key, age in OLD + NEW:
        execute(
            migrated_db,
            "INSERT INTO rate_counters (scope, key, window_start, count)"
            " VALUES (%s, %s, date_trunc('minute', now()) - %s::interval, 1)",
            (scope, key, age),
        )
    return migrated_db


def _counts(dsn) -> dict:
    """Row count of every table in `public` and `archive`, as the owner."""
    tables = fetch_all(
        dsn,
        "SELECT table_schema, table_name FROM information_schema.tables"
        " WHERE table_schema IN ('public', 'archive') AND table_type = 'BASE TABLE'",
    )
    return {
        f"{t['table_schema']}.{t['table_name']}": fetch_one(
            dsn, f'SELECT count(*) FROM "{t["table_schema"]}"."{t["table_name"]}"'
        )[0]
        for t in tables
    }


def _survivors(dsn) -> list:
    rows = fetch_all(dsn, "SELECT scope, key FROM rate_counters")
    return sorted((r["scope"], r["key"]) for r in rows)


#: A payload that names other classes, a zero keep and no batch. The executor
#: reads none of it: what it deletes is the worker's configuration alone.
HOSTILE = {"class": "audit_events", "p_class": "jobs_ok", "keep": 0, "batch": None}


@pytest.mark.parametrize("payload", [None, HOSTILE], ids=["plain", "hostile_payload"])
def test_the_sweep_deletes_only_rate_counter_rows_older_than_seven_days(
    sweep_db, payload
):
    before = _counts(sweep_db)
    assert before.pop("public.rate_counters") == len(OLD) + len(NEW)

    asyncio.run(run_as_worker(sweep_db, "retention_sweep", payload=payload))

    after = _counts(sweep_db)
    # Exactly the old rows went, every new one survived.
    assert _survivors(sweep_db) == NEW_KEYS
    # And no other table moved: the aged cap-ledger day, the aged job and the
    # aged archive table the door's unbuilt classes would take are all there.
    after.pop("public.rate_counters")
    assert after == before


def test_one_call_deletes_at_most_one_batch(sweep_db):
    """A batch of 2 against 3 aged rows, with the time budget already spent:
    the run makes ONE call, so one aged row is left."""
    one_call = WorkerConfig(retention_batch=2, retention_budget_seconds=0)
    asyncio.run(run_as_worker(sweep_db, "retention_sweep", config=one_call))
    assert len(_survivors(sweep_db)) == len(NEW) + len(OLD) - 2


def test_a_run_keeps_calling_until_a_call_comes_back_short(sweep_db):
    """The same batch of 2 with time left: the run calls again and drains."""
    drain = WorkerConfig(retention_batch=2)
    asyncio.run(run_as_worker(sweep_db, "retention_sweep", config=drain))
    assert _survivors(sweep_db) == NEW_KEYS


def test_each_batch_commits_on_its_own_and_a_short_batch_ends_the_run(sweep_db):
    """Each batch commits on its own; a short batch ends the run (the exact
    call count is what pins that, which the drain test does not)."""
    calls = []

    async def run(fail_second: bool):
        async with ingress_engine(as_user(sweep_db, "svc_worker")) as engine:
            sessions = make_session_for(engine)

            def factory():
                calls.append(None)
                if fail_second and len(calls) == 2:
                    raise RuntimeError("the 2nd batch fails")
                return sessions({})

            await scheduler.execute_retention_sweep(
                factory,
                keep_seconds=WorkerConfig().rate_counters_keep_seconds,
                batch=2,
                budget_seconds=60,
            )

    with pytest.raises(RuntimeError, match="2nd batch"):
        asyncio.run(run(fail_second=True))
    assert len(calls) == 2
    assert len(_survivors(sweep_db)) == len(NEW) + len(OLD) - 2

    calls.clear()
    asyncio.run(run(fail_second=False))
    assert len(calls) == 1
    assert _survivors(sweep_db) == NEW_KEYS
