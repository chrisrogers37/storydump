"""W1 gate — the worker composition against the real machinery (#942, #903).

The unit tier proves the loop's logic against fakes; this proves the assembled
thing against the replayed schema and the real doors: fn_claim_job grants the
lease, the adapter resolves and mints, finalize/reschedule CAS on the token,
parking leaves the job alive with its attempt restored, and a lost fence is
counted rather than fatal. Every scenario ends with the no-stranded-lease
assertion — a leased row nobody owns is the failure class the whole design
exists to prevent.
"""

import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target.work_loop import WorkerConfig
from src.worker import compose
from tests.scripts.conftest import seed_workspace_chain
from tests.scripts.test_lineage_lane import run_lane

pytestmark = [pytest.mark.integration]


def _async_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture()
def lane_db(bootstrapped_db):
    """The full-lineage world (legacy schema + target public), once per test."""
    run_lane(bootstrapped_db)
    return bootstrapped_db


@pytest.fixture()
def sync_conn(lane_db):
    conn = psycopg2.connect(lane_db)
    yield conn
    conn.close()


def _insert_job(
    conn,
    *,
    kind: str,
    workspace_id,
    lane: str = "bulk",
    serialization_key: str | None = None,
    payload: str = '{"v": 1}',
):
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
            " run_at, max_attempts, payload)"
            " VALUES (%s, %s, %s, %s, now(), 3, %s::jsonb) RETURNING id",
            (
                kind,
                workspace_id,
                lane,
                serialization_key or f"{kind}:{uuid.uuid4()}",
                payload,
            ),
        )
        job_id = cur.fetchone()[0]
    conn.commit()
    return job_id


def _job_row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, attempts, run_at, lease_token FROM jobs WHERE id = %s",
            (str(job_id),),
        )
        state, attempts, run_at, token = cur.fetchone()
    return {
        "state": state,
        "attempts": attempts,
        "run_at": run_at,
        "lease_token": token,
    }


def _assert_no_stranded_lease(conn, *, allowed: int = 0):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE state = 'leased'")
        leased = cur.fetchone()[0]
    assert leased == allowed, f"{leased} leased rows linger (allowed {allowed})"


async def _run_once(lane_db, *, registry_override=None, config=None):
    """One WorkLoop cycle on the bulk lane against the real database."""
    engine = create_async_engine(_async_url(lane_db))
    try:
        app = compose(engine=engine, config=config or WorkerConfig(), env={})
        wl = next(wl_ for wl_ in app.loops if wl_.lane == "bulk")
        if registry_override:
            wl._registry = {**app.registry, **registry_override}
        conn = await engine.connect()
        try:
            wl.bind_claim_conn(conn)
            claimed = await wl.run_once()
        finally:
            await conn.close()
        return wl, claimed
    finally:
        await engine.dispose()


class TestPlanSlotEndToEnd:
    async def test_a_claimed_plan_slot_job_mints_the_intent_and_finalizes(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w1gate")
        # The chain's one media item is bound to the chain's own live intent;
        # selection wants an eligible free item, so seed a second one.
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO media_items (workspace_id, source_id, content_hash,"
                " file_name, media_kind, provider_file_ref)"
                " VALUES (%s, %s, 'hash-w1gate-free', 'g.jpg', 'image', 'ref-w1gate-free')",
                (chain["ws"], chain["src"]),
            )
        sync_conn.commit()
        slot = datetime.now(timezone.utc) + timedelta(hours=1)
        job_id = _insert_job(
            sync_conn,
            kind="plan_slot",
            workspace_id=chain["ws"],
            serialization_key=f"acct:{chain['iga']}",
            payload='{"v": 1, "ig_account_id": "%s", "slot_at": "%s"}'
            % (chain["iga"], slot.isoformat()),
        )

        wl, claimed = await _run_once(lane_db)

        assert claimed is True and wl.processed == 1
        row = _job_row(sync_conn, job_id)
        assert row["state"] == "succeeded"
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM post_intents WHERE ig_account_id = %s"
                " AND schedule_slot_at = %s",
                (chain["iga"], slot),
            )
            minted = cur.fetchall()
        assert minted == [("scheduled",)]
        _assert_no_stranded_lease(sync_conn)


class TestParkingOnTheRealMachinery:
    async def test_an_executor_less_kind_is_rescheduled_alive_with_attempt_restored(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w1park")
        job_id = _insert_job(
            sync_conn,
            kind="sync_media_source",
            workspace_id=chain["ws"],
            payload='{"v": 1, "source_id": "%s", "reason": "baseline"}' % uuid.uuid4(),
        )

        wl, claimed = await _run_once(lane_db)

        assert claimed is True and wl.parked == 1
        row = _job_row(sync_conn, job_id)
        assert row["state"] == "ready", "a parked job must stay claimable"
        assert row["attempts"] == 0, "parking is not a failure; the attempt is restored"
        assert row["lease_token"] is None
        eta = (row["run_at"] - datetime.now(timezone.utc)).total_seconds()
        assert 800 < eta <= WorkerConfig().park_seconds + 60
        _assert_no_stranded_lease(sync_conn)


class TestFailureBackoffOnTheRealMachinery:
    async def test_an_adapter_failure_keeps_the_consumed_attempt_and_backs_off(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w1fail")
        job_id = _insert_job(
            sync_conn,
            kind="plan_slot",
            workspace_id=chain["ws"],
            payload='{"v": 1, "ig_account_id": "%s", "slot_at": "2026-01-01T00:00:00+00:00"}'
            % uuid.uuid4(),  # no such account -> the adapter raises
        )

        wl, claimed = await _run_once(lane_db)

        assert claimed is True and wl.failures == 1
        row = _job_row(sync_conn, job_id)
        assert row["state"] == "ready"
        assert row["attempts"] == 1, "a retryable failure keeps its consumed attempt"
        eta = (row["run_at"] - datetime.now(timezone.utc)).total_seconds()
        assert 30 < eta <= WorkerConfig().retry_backoff_seconds + 30
        _assert_no_stranded_lease(sync_conn)


class TestFencingOnTheRealMachinery:
    async def test_a_lease_lost_mid_run_is_counted_and_the_row_left_to_its_new_owner(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w1fence")
        job_id = _insert_job(
            sync_conn,
            kind="plan_slot",
            workspace_id=chain["ws"],
            payload='{"v": 1, "ig_account_id": "%s", "slot_at": "2026-01-01T00:00:00+00:00"}'
            % chain["iga"],
        )
        foreign = str(uuid.uuid4())

        async def usurped(session, job):
            with psycopg2.connect(lane_db) as c2, c2.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET lease_token = %s WHERE id = %s",
                    (foreign, str(job["id"])),
                )
                c2.commit()

        wl, claimed = await _run_once(lane_db, registry_override={"plan_slot": usurped})

        assert claimed is True and wl.fenced == 1 and wl.processed == 0
        row = _job_row(sync_conn, job_id)
        assert row["lease_token"] == foreign, "the row belongs to its new owner"
        assert row["state"] == "leased"
        _assert_no_stranded_lease(sync_conn, allowed=1)  # the usurper's, not ours


class TestIdleClaim:
    async def test_an_empty_lane_claims_nothing_and_strands_nothing(
        self, lane_db, sync_conn
    ):
        wl, claimed = await _run_once(lane_db)
        assert claimed is False and wl.processed == 0
        _assert_no_stranded_lease(sync_conn)


class TestTheWorkerIdlesVisibly:
    """The W1 done-bar in miniature: the ASSEMBLED worker — clock election,
    tick, mint, claim, execute, park, heartbeat — against the real database,
    ending with a clean stop and zero stranded leases. The Neon-branch soak is
    this same shape at full scale."""

    async def test_clock_mints_loops_process_parked_parks_and_nothing_strands(
        self, lane_db, sync_conn
    ):
        import asyncio

        chain = seed_workspace_chain(sync_conn, "w1soak")
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO media_items (workspace_id, source_id, content_hash,"
                " file_name, media_kind, provider_file_ref)"
                " VALUES (%s, %s, 'hash-w1soak-free', 'g.jpg', 'image', 'ref-w1soak-free')",
                (chain["ws"], chain["src"]),
            )
            cur.execute(
                "UPDATE ig_accounts SET next_slot_at = now() - interval '1 minute'"
                " WHERE id = %s",
                (chain["iga"],),
            )
        sync_conn.commit()
        _insert_job(
            sync_conn,
            kind="sync_media_source",
            workspace_id=chain["ws"],
            payload='{"v": 1, "source_id": "%s", "reason": "baseline"}' % uuid.uuid4(),
        )

        from src.worker import run

        cfg = WorkerConfig(
            lease_seconds=10.0,
            claim_idle_seconds=0.2,
            clock_interval_seconds=0.5,
            heartbeat_interval_seconds=0.5,
        )
        engine = create_async_engine(_async_url(lane_db))
        app = compose(engine=engine, config=cfg, env={})
        stop = asyncio.Event()
        runner = asyncio.create_task(run(app, stop=stop))
        try:
            await asyncio.sleep(6.0)
        finally:
            stop.set()
            # run() raising WorkerTaskDied here would mean a background task
            # died mid-soak — the supervision converts silence into failure.
            await asyncio.wait_for(runner, timeout=15.0)

        assert app.clock is not None and app.clock.elected is True
        assert app.clock.ticks >= 3, "the clock must tick on its cadence"
        # Positive controls (#958 review): counters whose ABSENCE of movement
        # is a failure — a dead task can no longer hide behind a quiet pass.
        assert app.sweeper is not None and app.sweeper.sweeps >= 2, (
            "the sender sweeper must be alive and sweeping for the whole run"
        )
        assert app.prompt_sweeper is not None and app.prompt_sweeper.sweeps >= 1, (
            "the prompt sweeper must be alive too (W3)"
        )
        bulk = next(wl_ for wl_ in app.loops if wl_.lane == "bulk")
        assert bulk.processed >= 1, "the clock-minted plan_slot job must be run"
        assert bulk.parked >= 1, "the executor-less kind must park, not vanish"
        assert app.heartbeat.consecutive_failures == 0

        with sync_conn.cursor() as cur:
            # The slot was due when it was minted, so the W3 fast path prompted
            # it in the minting pass — and since #1033 the web queue is a
            # surface every workspace has, so a workspace with no push binding
            # (this one) still advances to `awaiting_approval` there and then.
            # Before #1033 this intent parked in `scheduled` for the reaper.
            cur.execute(
                "SELECT count(*) FROM post_intents WHERE ig_account_id = %s"
                " AND state = 'awaiting_approval'",
                (chain["iga"],),
            )
            minted = cur.fetchone()[0]
        assert minted >= 1, (
            "clock -> plan_slot -> intent -> prompted and actionable on the web"
            " must complete unaided"
        )
        _assert_no_stranded_lease(sync_conn)


class TestJsonbTimestampWidths:
    """#969 — deterministic by construction: Postgres strips trailing zeros
    rendering timestamptz into jsonb, CPython's fromisoformat is version-
    sensitive about fraction widths (3.10, the declared floor, rejects most
    of them), and the old adapter parsed in Python — so ~1 in 10 clock ticks
    stranded on backoff, passing locally on 3.11 by luck. The resolve query
    now casts server-side; every width that Postgres can render must mint.
    The incident's exact 5-digit string is among the constructed cases."""

    WIDTHS = [
        "2026-08-21T16:44:41+00:00",  # no fraction
        "2026-08-21T16:44:41.1+00:00",
        "2026-08-21T16:44:41.05+00:00",
        "2026-08-21T16:44:41.123+00:00",
        "2026-08-21T16:44:41.1234+00:00",
        "2026-08-21T16:44:41.05024+00:00",  # the incident string's shape
        "2026-08-21T16:44:41.123456+00:00",
    ]

    async def test_every_fraction_width_postgres_renders_mints_the_intent(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w1widths")
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            for i in range(len(self.WIDTHS)):
                cur.execute(
                    "INSERT INTO media_items (workspace_id, source_id, content_hash,"
                    " file_name, media_kind, provider_file_ref)"
                    " VALUES (%s, %s, %s, 'w.jpg', 'image', %s)",
                    (chain["ws"], chain["src"], f"hash-w{i}", f"ref-w{i}"),
                )
        sync_conn.commit()

        for width in self.WIDTHS:
            _insert_job(
                sync_conn,
                kind="plan_slot",
                workspace_id=chain["ws"],
                serialization_key=f"acct:{chain['iga']}:{width[-12:]}",
                payload='{"v": 1, "ig_account_id": "%s", "slot_at": "%s"}'
                % (chain["iga"], width),
            )

        for _ in self.WIDTHS:
            wl, claimed = await _run_once(lane_db)
            assert claimed is True
            assert wl.failures == 0, (
                "a fraction width Postgres rendered must never strand the clock"
            )

        with sync_conn.cursor() as cur:
            # Assert each CONSTRUCTED slot minted exactly once — the seed
            # chain carries its own intent, so a bare count would be off-by-one.
            # No state predicate: every slot here is in the past, so the W3
            # fast path prompts and (since #1033) advances each intent in the
            # minting pass; the question this test asks is whether it MINTED.
            for width in self.WIDTHS:
                cur.execute(
                    "SELECT count(*) FROM post_intents WHERE ig_account_id = %s"
                    " AND schedule_slot_at = CAST(%s AS timestamptz)",
                    (chain["iga"], width),
                )
                assert cur.fetchone()[0] == 1, f"width {width!r} did not mint"
        _assert_no_stranded_lease(sync_conn)
