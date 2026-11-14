"""`worker_freshness`'s SQL, against a REAL Postgres (#1120).

## Why this exists

`tests/src/services/target/test_scheduling_health.py` stubs `executor.execute()`
with a canned mapping, so it proves the Python-side handling — `None` staying
`None`, floats truncating to ints — and **nothing whatever about the query that
produces those values.**

That is a fixture that cannot produce the condition it certifies. A hand-built
dict can carry *any* age, which is exactly why it cannot tell you whether
`min(EXTRACT(EPOCH FROM now() - updated_at)) FILTER (WHERE state = 'succeeded')`
computes one.

## What is genuinely at stake, not just "the SQL runs"

**`updated_at` versus `created_at` is a real fork, and production shows the gap.**
System jobs are enqueued ~6h before they are worked, so a query reading
`created_at` returns an age one full cadence too old and would fire `WORKER_DOWN`
against a perfectly healthy worker. Every row seeded here therefore carries a
`created_at` and an `updated_at` that are far apart, and in the direction that
makes the wrong column *look* alarming.
"""

from __future__ import annotations

import uuid

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from storydump_cli.health_classify.scheduling import DEFAULT_WORKER_STALE_S
from tests.scripts.conftest import async_url, seed_workspace_chain
from src.services.target import scheduling_health


@pytest.fixture()
def conn(world_db):
    c = psycopg2.connect(world_db)
    yield c
    c.close()


def _seed_system_job(
    conn,
    *,
    kind: str,
    state: str,
    run_at: str,
    created_at: str,
    updated_at: str,
    locked_by: str | None = None,
):
    """One system job (`workspace_id IS NULL`, per `ck_jobs_system_kinds`).

    **All three timestamps are set in the INSERT, and that is forced rather than
    stylistic.** `jobs` carries `tg_touch_jobs`, a BEFORE UPDATE trigger running
    `trg_touch_updated_at` — so seeding an age with a follow-up UPDATE stamps
    `updated_at` to `now()`, the row reads as zero seconds old, and every age
    assertion reads `0`. There is no INSERT trigger, so an explicit value
    overrides the column default and survives.

    Offsets are passed already signed (`"- interval '14 hours'"`); the caller
    owns the sign, because a helper that prepended one would produce
    `now() + - interval` for a future-dated row.
    """
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
            " run_at, max_attempts, payload, state, locked_by, created_at,"
            " updated_at)"
            f" VALUES (%s, NULL, 'bulk', %s, now() {run_at}, 3,"
            " '{\"v\": 1}'::jsonb, %s, %s, now() "
            + created_at
            + ", now() "
            + updated_at
            + ") RETURNING id",
            (kind, f"{kind}:{uuid.uuid4()}", state, locked_by),
        )
        job_id = cur.fetchone()[0]
    conn.commit()
    return job_id


async def _freshness(world_db):
    engine = create_async_engine(async_url(world_db))
    try:
        async with engine.connect() as c:
            return await scheduling_health.worker_freshness(c)
    finally:
        await engine.dispose()


class TestTheAgeIsComputedFromRealTimestamps:
    @pytest.mark.asyncio
    async def test_a_fourteen_hour_old_success_reads_as_over_the_threshold(
        self, conn, world_db
    ):
        """The arithmetic the alert rests on, executed against real rows.

        14h is far past the poller's default (`DEFAULT_WORKER_STALE_S`), so this
        is the reading that produces `WORKER_DOWN` — the one shape neither the
        stubbed unit tests nor the healthy production read had ever exercised.
        """
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="succeeded",
            run_at="- interval '20 hours'",
            created_at="- interval '20 hours'",
            updated_at="- interval '14 hours'",
        )

        out = await _freshness(world_db)

        assert out["succeeded_ever"] == 1
        age = out["last_success_age_seconds"]
        assert age is not None
        # 14h == 50400s, bounded rather than exact so clock drift cannot flake it.
        assert 50000 < age < 51000, age
        assert age > DEFAULT_WORKER_STALE_S, (
            "would not have tripped the shipped default threshold"
        )

    @pytest.mark.asyncio
    async def test_the_age_is_finish_time_not_enqueue_time(self, conn, world_db):
        """`updated_at`, never `created_at` — and production is why.

        A system job is enqueued a full cadence before it is worked. Reading
        `created_at` here would report 20h against a job that finished two
        minutes ago and fire the alert on a healthy worker.
        """
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="succeeded",
            run_at="- interval '20 hours'",
            created_at="- interval '20 hours'",
            updated_at="- interval '2 minutes'",
        )

        age = (await _freshness(world_db))["last_success_age_seconds"]

        assert 90 < age < 200, age
        assert age < DEFAULT_WORKER_STALE_S, (
            "a healthy worker would have been reported down"
        )

    @pytest.mark.asyncio
    async def test_only_succeeded_rows_count_toward_freshness(self, conn, world_db):
        """The FILTER, exercised. A recent row in another state must not be
        read as proof of life — a worker that claims jobs and never finishes
        them is precisely the outage this axis exists to catch.

        `leased` rather than an invented state name: `ck_jobs_state` admits only
        ready / leased / succeeded / failed / review_required / cancelled, and
        `leased` is exactly the claimed-but-unfinished shape this asserts on."""
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="succeeded",
            run_at="- interval '20 hours'",
            created_at="- interval '20 hours'",
            updated_at="- interval '14 hours'",
        )
        _seed_system_job(
            conn,
            kind="reap_transit_assets",
            state="leased",
            run_at="- interval '1 hour'",
            created_at="- interval '1 hour'",
            updated_at="- interval '1 minute'",
        )

        out = await _freshness(world_db)

        assert out["succeeded_ever"] == 1
        assert out["last_success_age_seconds"] > DEFAULT_WORKER_STALE_S, out


class TestTheBacklogIsComputedFromRealTimestamps:
    @pytest.mark.asyncio
    async def test_a_due_unclaimed_job_reads_as_overdue(self, conn, world_db):
        _seed_system_job(
            conn,
            kind="alert_stranded_sources",
            state="ready",
            run_at="- interval '2 hours'",
            created_at="- interval '8 hours'",
            updated_at="- interval '8 hours'",
        )

        out = await _freshness(world_db)

        assert out["overdue_ready"] == 1
        assert 7000 < out["max_overdue_seconds"] < 7400, out
        assert out["max_overdue_seconds"] > 900, "below the shipped default"

    @pytest.mark.asyncio
    async def test_a_future_run_at_is_not_a_backlog(self, conn, world_db):
        """Production's healthy shape: jobs are minted ~6h ahead, so `ready`
        rows normally sit with `run_at` in the FUTURE. Counting those would
        report a permanent backlog on a perfectly healthy estate."""
        _seed_system_job(
            conn,
            kind="alert_stranded_sources",
            state="ready",
            run_at="+ interval '6 hours'",
            created_at="- interval '1 minute'",
            updated_at="- interval '1 minute'",
        )

        out = await _freshness(world_db)

        assert out["overdue_ready"] == 0
        assert out["max_overdue_seconds"] is None

    @pytest.mark.asyncio
    async def test_an_empty_jobs_table_reports_none_not_zero(self, conn, world_db):
        """The never-run reading, from a real empty table rather than a dict
        that was handed the answer."""
        out = await _freshness(world_db)

        assert out == {
            "succeeded_ever": 0,
            "last_success_age_seconds": None,
            "overdue_ready": 0,
            "max_overdue_seconds": None,
            "commit": None,
        }

    @pytest.mark.asyncio
    async def test_the_freshest_success_names_the_commit_its_claimer_ran(
        self, conn, world_db
    ):
        """`commit` is the stamp the worker puts last on its claimer name,
        read from the freshest succeeded system job. A fresher row that has
        not succeeded is not read. A row claimed by an unstamped worker, or
        before the stamp, reads None, never a guess. Future-dated, so a
        sibling's row can never be the fresher one."""
        for minutes, name in (
            (1, "host-7-bulk-0@aaaaaaa"),
            (2, "host-7-bulk-0@abc1234"),
        ):
            _seed_system_job(
                conn,
                kind="reap_expired",
                state="succeeded",
                run_at=f"+ interval '{minutes} minutes'",
                created_at=f"+ interval '{minutes} minutes'",
                updated_at=f"+ interval '{minutes} minutes'",
                locked_by=name,
            )
        assert (await _freshness(world_db))["commit"] == "abc1234"
        # The clock mints each recurring kind's next run unclaimed, so the
        # newest row is often one with no claimer at all. Due tomorrow, so it
        # never counts as an overdue backlog for a later test.
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="ready",
            run_at="+ interval '1 day'",
            created_at="+ interval '150 seconds'",
            updated_at="+ interval '150 seconds'",
        )
        assert (await _freshness(world_db))["commit"] == "abc1234"
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="succeeded",
            run_at="+ interval '3 minutes'",
            created_at="+ interval '3 minutes'",
            updated_at="+ interval '3 minutes'",
            locked_by="host-7-bulk-0",
        )
        assert (await _freshness(world_db))["commit"] is None


class TestTheColumnChoiceRestsOnATriggerThatIsMeasuredHere:
    @pytest.mark.asyncio
    async def test_a_state_transition_stamps_updated_at(self, conn, world_db):
        """`updated_at` IS finish time — because a trigger makes it so.

        The whole axis reads `updated_at` on `succeeded` rows and calls the
        result "how long since a job finished". That is only true if the column
        tracks the transition, and it does: `tg_touch_jobs` is a BEFORE UPDATE
        trigger running `trg_touch_updated_at`. A job reaches `succeeded`
        through an UPDATE, so the stamp lands at the moment it finishes.

        Asserted rather than assumed, because the whole column choice rests on
        it and nothing else in the suite would notice if the trigger were
        dropped — the age would silently become "time since enqueue" and the
        alert would fire a cadence early on a healthy worker.
        """
        job_id = _seed_system_job(
            conn,
            kind="reap_expired",
            state="ready",
            run_at="- interval '20 hours'",
            created_at="- interval '20 hours'",
            updated_at="- interval '20 hours'",
        )
        before = (await _freshness(world_db))["last_success_age_seconds"]
        assert before is None, "a ready row must not count as a success"

        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (job_id,))
        conn.commit()

        after = (await _freshness(world_db))["last_success_age_seconds"]

        assert after is not None
        # Seeded 20h back, finished just now: the trigger moved the stamp.
        assert after < 60, f"updated_at did not track the transition: {after}s"


class TestWhichSuccessProvesFreshness:
    """Each of these kills a mutant the tests above let live."""

    @pytest.mark.asyncio
    async def test_the_freshest_success_is_reported_not_the_oldest(
        self, conn, world_db
    ):
        """`min(age)`, not `max(age)`.

        Every earlier test seeds ONE succeeded row, and with one row min and max
        are the same number — so swapping them survives every test above.
        The defect it hides is not subtle: `max` reports the OLDEST success as
        "time since last success", so a worker with any history at all reads
        permanently stale and pages continuously.
        """
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="succeeded",
            run_at="- interval '30 hours'",
            created_at="- interval '30 hours'",
            updated_at="- interval '30 hours'",
        )
        _seed_system_job(
            conn,
            kind="reap_transit_assets",
            state="succeeded",
            run_at="- interval '2 hours'",
            created_at="- interval '2 hours'",
            updated_at="- interval '2 minutes'",
        )

        out = await _freshness(world_db)

        assert out["succeeded_ever"] == 2
        age = out["last_success_age_seconds"]
        # ~2 min (the fresher). `max` would answer ~30h and trip the threshold.
        assert 90 < age < 200, age
        assert age < DEFAULT_WORKER_STALE_S, (
            "the oldest success was reported as the latest"
        )

    @pytest.mark.asyncio
    async def test_a_tenant_jobs_success_is_not_proof_the_system_lane_lives(
        self, conn, world_db
    ):
        """`WHERE workspace_id IS NULL` — the scope, which no other test exercises.

        Every other fixture here is system-only, so dropping the scope
        predicate changes no answer for them. It changes one as soon as a
        tenant job exists: a busy tenant lane would supply a constant stream of
        fresh successes and mask a system lane that had stopped entirely. The
        axis is *tenant-independent*, and this is the assertion that says so.
        """
        chain = seed_workspace_chain(conn, "wfgate")
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="succeeded",
            run_at="- interval '20 hours'",
            created_at="- interval '20 hours'",
            updated_at="- interval '14 hours'",
        )
        # A tenant job, finished seconds ago. `ck_jobs_system_kinds` is a
        # biconditional, so a workspace-bearing row MUST carry a non-system
        # kind — `plan_slot` is the one the core loop uses.
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " run_at, max_attempts, payload, state, created_at, updated_at)"
                " VALUES ('plan_slot', %s, 'bulk', %s, now(), 3,"
                " '{\"v\": 1}'::jsonb, 'succeeded', now(), now())",
                (chain["ws"], f"plan_slot:{uuid.uuid4()}"),
            )
        conn.commit()

        out = await _freshness(world_db)

        assert out["succeeded_ever"] == 1, "a tenant job was counted"
        assert out["last_success_age_seconds"] > DEFAULT_WORKER_STALE_S, (
            "a tenant job's success was read as proof the system lane is alive"
        )
