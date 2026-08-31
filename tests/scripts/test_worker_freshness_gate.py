"""`worker_freshness`'s SQL, against a REAL Postgres (#1120 follow-up).

## Why this exists — the gap rajan named in review

`tests/src/services/target/test_scheduling_health.py` stubs `executor.execute()`
with a canned mapping, so it proves the Python-side handling — `None` staying
`None`, floats truncating to ints — and **nothing whatever about the query that
produces those values.** The one real run was the production read in the PR body,
and production is healthy: `78 | 21052s | 0 | null`. So the over-threshold
arithmetic had never executed against real rows in either place.

That is a fixture that cannot produce the condition it certifies. A hand-built
dict can carry *any* age, which is exactly why it cannot tell you whether
`min(EXTRACT(EPOCH FROM now() - updated_at)) FILTER (WHERE state = 'succeeded')`
computes one.

## What is genuinely at stake, not just "the SQL runs"

**`updated_at` versus `created_at` is a real fork, and production shows the gap.**
System jobs are enqueued ~6h before they are worked: on 2026-08-31 the batch
created at 04:03 ran at ~10:03. So a query reading `created_at` returns an age
one full cadence too old and would fire `WORKER_DOWN` against a perfectly healthy
worker. Every row seeded here therefore carries a `created_at` and an
`updated_at` that are far apart, and in the direction that makes the wrong column
*look* alarming.
"""

from __future__ import annotations

import uuid

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tests.scripts.conftest import seed_workspace_chain
from tests.scripts.test_lineage_lane import run_lane
from src.services.target import scheduling_health


def _async_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture()
def lane_db(bootstrapped_db):
    run_lane(bootstrapped_db)
    return bootstrapped_db


@pytest.fixture()
def conn(lane_db):
    c = psycopg2.connect(lane_db)
    yield c
    c.close()


def _seed_system_job(
    conn, *, kind: str, state: str, run_at: str, created_at: str, updated_at: str
):
    """One system job (`workspace_id IS NULL`, per `ck_jobs_system_kinds`).

    **All three timestamps are set in the INSERT, and that is forced rather
    than stylistic.** `jobs` carries `tg_touch_jobs`, a BEFORE UPDATE trigger
    running `trg_touch_updated_at` — so seeding an age with a follow-up UPDATE
    stamps `updated_at` to `now()` and the row reads as zero seconds old. The
    first version of this file did exactly that and every age assertion came
    back `0`. There is no INSERT trigger, so an explicit value overrides the
    column default and survives.

    Offsets are passed already signed (`"- interval '14 hours'"`); the caller
    owns the sign, because a helper that prepended one produced `now() + -
    interval` on the first future-dated row.
    """
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
            " run_at, max_attempts, payload, state, created_at, updated_at)"
            f" VALUES (%s, NULL, 'bulk', %s, now() {run_at}, 3,"
            " '{\"v\": 1}'::jsonb, %s, now() "
            + created_at
            + ", now() "
            + updated_at
            + ") RETURNING id",
            (kind, f"{kind}:{uuid.uuid4()}", state),
        )
        job_id = cur.fetchone()[0]
    conn.commit()
    return job_id


async def _freshness(lane_db):
    engine = create_async_engine(_async_url(lane_db))
    try:
        async with engine.connect() as c:
            return await scheduling_health.worker_freshness(c)
    finally:
        await engine.dispose()


class TestTheAgeIsComputedFromRealTimestamps:
    @pytest.mark.asyncio
    async def test_a_fourteen_hour_old_success_reads_as_over_the_threshold(
        self, conn, lane_db
    ):
        """The arithmetic the alert rests on, executed against real rows.

        14h is past the poller's 13h (46800s) default, so this is the reading
        that produces `WORKER_DOWN` — the one shape neither the stubbed unit
        tests nor the healthy production read had ever exercised.
        """
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="succeeded",
            run_at="- interval '20 hours'",
            created_at="- interval '20 hours'",
            updated_at="- interval '14 hours'",
        )

        out = await _freshness(lane_db)

        assert out["succeeded_ever"] == 1
        age = out["last_success_age_seconds"]
        assert age is not None
        # 14h == 50400s, bounded rather than exact so clock drift cannot flake it.
        assert 50000 < age < 51000, age
        assert age > 46800, "would not have tripped the shipped default threshold"

    @pytest.mark.asyncio
    async def test_the_age_is_finish_time_not_enqueue_time(self, conn, lane_db):
        """`updated_at`, never `created_at` — and production is why.

        A system job is enqueued a full cadence before it is worked. Reading
        `created_at` here would report 20h against a job that finished 1h ago
        and fire the alert on a healthy worker.
        """
        _seed_system_job(
            conn,
            kind="reap_expired",
            state="succeeded",
            run_at="- interval '20 hours'",
            created_at="- interval '20 hours'",
            updated_at="- interval '1 hour'",
        )

        age = (await _freshness(lane_db))["last_success_age_seconds"]

        assert 3000 < age < 4200, age
        assert age < 46800, "a healthy worker would have been reported down"

    @pytest.mark.asyncio
    async def test_only_succeeded_rows_count_toward_freshness(self, conn, lane_db):
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

        out = await _freshness(lane_db)

        assert out["succeeded_ever"] == 1
        assert out["last_success_age_seconds"] > 46800, out


class TestTheBacklogIsComputedFromRealTimestamps:
    @pytest.mark.asyncio
    async def test_a_due_unclaimed_job_reads_as_overdue(self, conn, lane_db):
        _seed_system_job(
            conn,
            kind="alert_stranded_sources",
            state="ready",
            run_at="- interval '2 hours'",
            created_at="- interval '8 hours'",
            updated_at="- interval '8 hours'",
        )

        out = await _freshness(lane_db)

        assert out["overdue_ready"] == 1
        assert 7000 < out["max_overdue_seconds"] < 7400, out
        assert out["max_overdue_seconds"] > 900, "below the shipped default"

    @pytest.mark.asyncio
    async def test_a_future_run_at_is_not_a_backlog(self, conn, lane_db):
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

        out = await _freshness(lane_db)

        assert out["overdue_ready"] == 0
        assert out["max_overdue_seconds"] is None

    @pytest.mark.asyncio
    async def test_an_empty_jobs_table_reports_none_not_zero(self, conn, lane_db):
        """The never-run reading, from a real empty table rather than a dict
        that was handed the answer."""
        out = await _freshness(lane_db)

        assert out == {
            "succeeded_ever": 0,
            "last_success_age_seconds": None,
            "overdue_ready": 0,
            "max_overdue_seconds": None,
        }


class TestTheColumnChoiceRestsOnATriggerThatIsMeasuredHere:
    @pytest.mark.asyncio
    async def test_a_state_transition_stamps_updated_at(self, conn, lane_db):
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
        before = (await _freshness(lane_db))["last_success_age_seconds"]
        assert before is None, "a ready row must not count as a success"

        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (job_id,))
        conn.commit()

        after = (await _freshness(lane_db))["last_success_age_seconds"]

        assert after is not None
        # Seeded 20h back, finished just now: the trigger moved the stamp.
        assert after < 60, f"updated_at did not track the transition: {after}s"


class TestTheTwoSurvivorsFromTheFirstMutationRun:
    """Both of these were written because a mutant lived, not because the shape
    occurred to me — the first matrix killed 3 of 5 and these close the rest."""

    @pytest.mark.asyncio
    async def test_the_freshest_success_is_reported_not_the_oldest(self, conn, lane_db):
        """`min(age)`, not `max(age)`.

        Every earlier test seeds ONE succeeded row, and with one row min and max
        are the same number — so swapping them survived the whole first matrix.
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
            updated_at="- interval '1 hour'",
        )

        out = await _freshness(lane_db)

        assert out["succeeded_ever"] == 2
        age = out["last_success_age_seconds"]
        # ~1h (the fresher). `max` would answer ~30h and trip the threshold.
        assert 3000 < age < 4200, age
        assert age < 46800, "the oldest success was reported as the latest"

    @pytest.mark.asyncio
    async def test_a_tenant_jobs_success_is_not_proof_the_system_lane_lives(
        self, conn, lane_db
    ):
        """`WHERE workspace_id IS NULL` — the scope, which nothing exercised.

        Production has zero tenants today, so every fixture here was
        system-only and dropping the scope predicate changed no answer. It will
        the moment a workspace exists: a busy tenant lane would supply a
        constant stream of fresh successes and mask a system lane that had
        stopped entirely. The axis is *tenant-independent*, and this is the
        assertion that says so.
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

        out = await _freshness(lane_db)

        assert out["succeeded_ever"] == 1, "a tenant job was counted"
        assert out["last_success_age_seconds"] > 46800, (
            "a tenant job's success was read as proof the system lane is alive"
        )
