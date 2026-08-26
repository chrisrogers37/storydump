"""L.7 gate — scheduler-as-clock, dispatcher, recurring system jobs (#864, `04` §L.7).

*"Clock tick is O(due) by EXPLAIN; killing the clock mid-tick loses nothing; a
duplicate `plan_slot` execution mints no second intent (key 1 proves it); the
slot-storm scenario inserts within bounds; reaper sweeps proven bounded (H5) on
seeded fixtures, riding the `02` §3 reap indexes by EXPLAIN."*

THE CLAIM THIS GATE EXISTS TO TEST. L.4 (#897) established that the outbox
needs no guard of its own because `uq_jobs_serialized_lease` admits one live
sender and holding the lease proves the predecessor does not. **That argument
does not extend to recurring work**, and `TestTheClockElectionIsExclusive`
proves the gap rather than asserting it: `fn_clock_tick`'s recurring leg guards
on `NOT EXISTS (… state IN ('ready','leased'))`, a SNAPSHOT read, and the index
that rescues `fn_claim_job` is partial on `state = 'leased'` while the
double-insert happens at `state = 'ready'`. Two concurrent ticks both insert —
measured here with the election removed — so the election is the mechanism, and
it is proven by removal like every other guard in this suite.

Concurrency doctrine, inherited from #883/#890/#897:

* Every race runs on REAL concurrent connections at **READ COMMITTED**,
  asserted in its own test — 19 of L.2's tests still passed at REPEATABLE READ
  including its race, so a concurrency test at the wrong level proves nothing.
* No refusal is inferred from a bare rowcount. Where a refusal is named, it is
  matched on `diag.constraint_name`.
* Every refusal carries a rowcount positive control.
* Every guard is proven load-bearing by removal.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import uuid

import psycopg2
import pytest
from psycopg2 import errors as pg_errors

from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: `05`: tick every 15 s, <= 500 inserts/tick (p_max, a TOTAL across classes);
#: reaper every 60 s at 500 (p_lim, likewise a total). Parameters, never
#: hardcoded in src — the gate uses small values so the bounds are reachable.
TICK_MAX, REFRESH_CADENCE_S = 500, 7 * 24 * 3600
REAPER_LIM, APPROVAL_TTL_S, APPROVED_TTL_S = 500, 24 * 3600, 24 * 3600

#: `uq_intent_slot`, verbatim from `055` — re-added after its drop proof.
UQ_INTENT_SLOT_SQL = (
    "CREATE UNIQUE INDEX uq_intent_slot ON post_intents"
    " (workspace_id, ig_account_id, schedule_slot_at)"
)


@pytest.fixture(scope="module")
def clock_db(admin_conn, owner_actor):
    """Replayed schema + one workspace chain, once.

    Module-scoped for the same reason as L.2's and L.4's (a role-carrying
    template cannot be held); scenarios keep it safe by minting their own
    accounts, kinds and slots rather than sharing.
    """
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "l7-gate")
        finally:
            conn.close()
        yield {
            "worker": as_user(db, "svc_worker"),
            "owner_stream": dsn,
            "ws": str(chain["ws"]),
            "iga": str(chain["iga"]),
            "src": str(chain["src"]),
        }
    finally:
        gen.close()


def _owner_exec(clock_db, sql, params=None, fetch=False):
    """One statement as the schema owner (bypasses RLS; owns every guard)."""
    conn = psycopg2.connect(clock_db["owner_stream"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        conn.close()


def _worker_conn(clock_db, *, autocommit=True):
    conn = psycopg2.connect(clock_db["worker"])
    conn.autocommit = autocommit
    with conn.cursor() as cur:
        cur.execute("SET app.tenant_id = %s", (clock_db["ws"],))
        cur.execute("SET app.actor_kind = 'system'")
    if not autocommit:
        conn.commit()
    return conn


def _kind() -> str:
    """A recurring system kind the registry already allows."""
    return "retention_sweep"


def _raw_tick(cur, *, max_inserts=TICK_MAX, recurring=None):
    """`fn_clock_tick` from a plain cursor — the door, not the wrapper."""
    cur.execute(
        "SELECT o_slot_jobs, o_refresh_jobs, o_sync_jobs, o_recurring_jobs"
        " FROM fn_clock_tick(%s, make_interval(secs => %s), %s::jsonb)",
        (max_inserts, REFRESH_CADENCE_S, json.dumps(recurring or {"v": 1})),
    )
    return cur.fetchone()


def _jobs_of_kind(clock_db, kind):
    return _owner_exec(
        clock_db,
        "SELECT count(*) FROM jobs WHERE kind = %s AND state IN ('ready','leased')",
        (kind,),
        fetch=True,
    )[0][0]


def _new_account(clock_db, *, due_seconds_ago=60):
    """An active account whose slot cursor is already due."""
    return str(
        _owner_exec(
            clock_db,
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref, state,"
            " next_slot_at) VALUES (%s, %s, 'active',"
            " now() - make_interval(secs => %s)) RETURNING id",
            (clock_db["ws"], f"acct-{uuid.uuid4().hex[:10]}", due_seconds_ago),
            fetch=True,
        )[0][0]
    )


def _new_media(clock_db):
    tag = uuid.uuid4().hex[:12]
    return str(
        _owner_exec(
            clock_db,
            "INSERT INTO media_items (workspace_id, source_id, content_hash,"
            " file_name, media_kind, provider_file_ref)"
            " VALUES (%s, %s, %s, %s, 'image', %s) RETURNING id",
            (clock_db["ws"], clock_db["src"], tag, f"{tag}.jpg", tag),
            fetch=True,
        )[0][0]
    )


class TestTheLevelIsReadCommitted:
    """Pinned as a fact: every concurrency claim below holds at the level
    production runs, and #890 measured that most such proofs keep passing at
    stricter levels — so without this they could stop describing production."""

    def test_worker_connections_run_read_committed(self, clock_db):
        conn = _worker_conn(clock_db)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW transaction_isolation")
                assert cur.fetchone()[0] == "read committed"
                cur.execute("SHOW default_transaction_isolation")
                assert cur.fetchone()[0] == "read committed"
        finally:
            conn.close()


class TestTheClockElectionIsExclusive:
    """The answer to "does L.4's lease argument extend to recurring work".

    It does not, and this class shows both halves: the hazard is real when the
    election is absent, and the election is what removes it.
    """

    def test_two_concurrent_ticks_WITHOUT_the_election_double_insert(self, clock_db):
        """The hazard, measured. This is the drop-the-guard proof for the
        election: with no election, `fn_clock_tick`'s `NOT EXISTS` cannot
        serialize two callers, because it is a snapshot read and
        `uq_jobs_serialized_lease` is partial on `state = 'leased'` while the
        insert lands at `state = 'ready'`.
        """
        kind = _kind()
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = %s", (kind,))
        assert _jobs_of_kind(clock_db, kind) == 0, "positive control: none live"

        recurring = {"v": 1, kind: 3600}
        c1 = _worker_conn(clock_db, autocommit=False)
        c2 = _worker_conn(clock_db, autocommit=False)
        second: dict = {}

        def contender():
            with c2.cursor() as cur:
                second["counts"] = _raw_tick(cur, recurring=recurring)
            c2.commit()

        try:
            with c1.cursor() as cur:
                first = _raw_tick(cur, recurring=recurring)
            # BOTH ticks are in flight; neither can see the other's insert.
            t = threading.Thread(target=contender)
            t.start()
            t.join(timeout=20)
            assert not t.is_alive(), "the second tick never returned"
            c1.commit()
        finally:
            c1.close()
            c2.close()

        assert first[3] == 1 and second["counts"][3] == 1, (
            "both ticks must believe they scheduled the singleton — that is"
            " the double-fire this test exists to expose"
        )
        assert _jobs_of_kind(clock_db, kind) == 2, (
            "two live rows for one recurring kind — a clock that can"
            " double-fire is the same defect class as an outbox that can"
            " double-send (#897), and the lease does NOT cover it"
        )
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = %s", (kind,))

    @pytest.mark.asyncio
    async def test_the_election_admits_exactly_one_clock(self, clock_db):
        """And the mechanism that closes it: the loser does not tick at all."""
        from src.services.target.scheduler import ClockElection

        engine_a, engine_b = self._engine(clock_db), self._engine(clock_db)
        try:
            async with engine_a.connect() as a, engine_b.connect() as b:
                first, second = ClockElection(a), ClockElection(b)
                assert await first.try_acquire() is True, "positive control"
                assert await second.try_acquire() is False, (
                    "two processes both elected themselves clock"
                )
                # And it is genuinely handed over rather than held forever.
                await first.release()
                assert await second.try_acquire() is True
                await second.release()
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    @pytest.mark.asyncio
    async def test_a_loser_ticks_nothing_and_it_is_not_a_failure(self, clock_db):
        """Losing the election must not move `consecutive_failures`: on a
        three-replica deployment two processes lose every interval, and a
        supervisor restarting them for it would be the defect."""
        from src.services.target.scheduler import Clock, ClockElection

        kind = _kind()
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = %s", (kind,))
        holder_engine = self._engine(clock_db)
        loser_engine = self._engine(clock_db)
        try:
            async with holder_engine.connect() as held, loser_engine.connect() as lost:
                holder = ClockElection(held)
                assert await holder.try_acquire() is True
                clock = Clock(
                    lost,
                    self._factory(loser_engine, clock_db),
                    interval_seconds=0.05,
                    max_inserts=TICK_MAX,
                    refresh_cadence_seconds=REFRESH_CADENCE_S,
                    recurring={"v": 1, kind: 3600},
                )
                assert await clock.tick_once() is None
                assert await clock.tick_once() is None
                assert clock.ticks == 0, "a loser must not tick"
                assert clock.lost_elections == 2
                assert clock.consecutive_failures == 0, (
                    "losing the election moved the failure counter — every"
                    " non-clock replica would read as dying"
                )
                await holder.release()
        finally:
            await holder_engine.dispose()
            await loser_engine.dispose()
        assert _jobs_of_kind(clock_db, kind) == 0, "the loser inserted nothing"

    def _engine(self, clock_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            clock_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=2,
            max_overflow=0,
        )

    def _factory(self, engine, clock_db):
        from contextlib import asynccontextmanager

        from sqlalchemy import text as _t
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def _f():
            async with maker() as session:
                await session.execute(
                    _t("SELECT set_config('app.tenant_id', :v, false)"),
                    {"v": clock_db["ws"]},
                )
                yield session

        return _f


class TestAFailingTickIsVisibleInBothTheCounterAndTheLog:
    """#1074. Two defects, neither of them the leg ordering.

    `tick_once` swallowed the exception and kept only a count, and the worker
    gate asserted `ticks` — which increments BEFORE the try, so it counts
    ATTEMPTS. A clock failing every single tick satisfied it. That is a check
    which cannot fail in the direction it exists to detect, and it is why
    #1069's symptom surfaced five layers away as `assert bulk.processed >= 1`
    failing `0 >= 1` instead of as a named constraint violation.

    The failure is induced with a recurring kind absent from `ck_jobs_kind`,
    which is not a contrived fault: it is #1069's defect exactly. The positive
    control IS the original incident.
    """

    def _clock(self, engine, clock_db, conn, *, recurring):
        from contextlib import asynccontextmanager

        from src.services.target.scheduler import Clock

        from sqlalchemy import text as _t
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def _factory():
            async with maker() as session:
                await session.execute(
                    _t("SELECT set_config('app.tenant_id', :v, false)"),
                    {"v": clock_db["ws"]},
                )
                yield session

        return Clock(
            conn,
            _factory,
            interval_seconds=0.05,
            max_inserts=TICK_MAX,
            refresh_cadence_seconds=REFRESH_CADENCE_S,
            recurring=recurring,
        )

    @pytest.mark.asyncio
    async def test_a_failing_tick_moves_the_counter_the_gate_now_asserts(
        self, clock_db, caplog
    ):
        """BOTH directions, which is the property the old assertion lacked.

        `ticks` is asserted to move TOO — not because that is desirable, but
        because it is the proof that `ticks >= 3` would have passed here. A
        test that only showed the new counter moving would leave the reader to
        take the old one's blindness on trust.
        """
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            clock_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=2,
            max_overflow=0,
        )
        try:
            async with engine.connect() as conn:
                clock = self._clock(
                    engine,
                    clock_db,
                    conn,
                    # Absent from ck_jobs_kind — leg 1 tries to INSERT it and
                    # the CHECK aborts the whole transaction, all five legs.
                    recurring={"v": 1, "no_such_job_kind": 3600},
                )
                with caplog.at_level(logging.ERROR):
                    assert await clock.tick_once() is None
                    assert await clock.tick_once() is None

                assert clock.consecutive_failures == 2, (
                    "the counter the worker gate now asserts must move"
                )
                assert clock.ticks == 2, (
                    "and `ticks` moved identically — this is exactly why"
                    " `assert clock.ticks >= 3` passed on a clock that never"
                    " successfully ticked"
                )
                assert clock.inserts == 0, "nothing was minted"

                blob = "\n".join(
                    r.getMessage() + str(r.exc_info) for r in caplog.records
                )
                assert "no_such_job_kind" in blob, (
                    "the offending kind must survive the swallow — a count"
                    " cannot say WHICH leg or which value failed, and the"
                    " five legs share one transaction so any of them aborts"
                    " all five identically"
                )
                assert "ck_jobs_kind" in blob, "and the constraint that refused it"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_recovering_clock_clears_the_counter(self, clock_db):
        """The negative half: the counter must not latch, or a clock that
        recovers reads as permanently broken and the gate's `== 0` becomes
        unsatisfiable for the wrong reason."""
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            clock_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=2,
            max_overflow=0,
        )
        try:
            async with engine.connect() as conn:
                broken = self._clock(
                    engine, clock_db, conn, recurring={"v": 1, "no_such_job_kind": 3600}
                )
                assert await broken.tick_once() is None
                assert broken.consecutive_failures == 1

                # Same election connection, a recurring set the schema admits.
                healthy = self._clock(
                    engine, clock_db, conn, recurring={"v": 1, "reap_expired": 3600}
                )
                assert await healthy.tick_once() is not None
                assert healthy.consecutive_failures == 0
        finally:
            await engine.dispose()


class TestKillingTheClockMidTickLosesNothing:
    """The gate's second clause, both halves: no partial state, and the
    election is genuinely released rather than held by a corpse."""

    def test_an_aborted_tick_leaves_no_partial_state(self, clock_db):
        kind = _kind()
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = %s", (kind,))
        account = _new_account(clock_db)
        before = _owner_exec(
            clock_db,
            "SELECT next_slot_at FROM ig_accounts WHERE id = %s",
            (account,),
            fetch=True,
        )[0][0]

        conn = _worker_conn(clock_db, autocommit=False)
        try:
            with conn.cursor() as cur:
                counts = _raw_tick(cur, recurring={"v": 1, kind: 3600})
                assert counts[0] >= 1 and counts[3] == 1, (
                    "positive control: the tick did real work before the kill"
                )
            conn.rollback()  # the kill: the transaction never commits
        finally:
            conn.close()

        assert _jobs_of_kind(clock_db, kind) == 0, "a recurring job survived a kill"
        after = _owner_exec(
            clock_db,
            "SELECT next_slot_at FROM ig_accounts WHERE id = %s",
            (account,),
            fetch=True,
        )[0][0]
        assert after == before, (
            "the slot cursor advanced without the job that advance accounts"
            " for — the account would silently skip a slot"
        )
        assert (
            _owner_exec(
                clock_db,
                "SELECT count(*) FROM jobs WHERE kind = 'plan_slot'"
                " AND payload->>'ig_account_id' = %s",
                (account,),
                fetch=True,
            )[0][0]
            == 0
        )

    @pytest.mark.asyncio
    async def test_a_dead_clocks_election_is_released_by_its_session(self, clock_db):
        """Session-scoped, deliberately: a SIGKILLed clock runs no `finally`,
        so the lock must die with the connection or a successor never runs.

        The kill is `pg_terminate_backend` on the clock's own backend rather
        than an engine dispose. Disposing is a graceful close the pool may
        defer, which is the first version of this test and it failed for that
        reason — proving nothing about the case that matters. Terminating the
        backend is what the server sees when the process is SIGKILLed.
        """
        from sqlalchemy import text as _t
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.scheduler import ClockElection

        def _engine():
            return create_async_engine(
                clock_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
                pool_size=1,
                max_overflow=0,
            )

        dead, successor = _engine(), _engine()
        try:
            conn = await dead.connect()
            held = ClockElection(conn)
            assert await held.try_acquire() is True, "positive control: it elected"
            pid = (await conn.execute(_t("SELECT pg_backend_pid()"))).scalar()

            async with successor.connect() as other:
                assert await ClockElection(other).try_acquire() is False, (
                    "positive control: while the clock lives, nobody else elects"
                )

            # THE KILL. From another session, so nothing in the dead clock
            # runs — and as the SAME role, because terminating another role's
            # backend needs pg_signal_backend membership the owner does not
            # have here. Measured: the owner-issued form raises
            # InsufficientPrivilege, which would have made this test fail for
            # a reason that has nothing to do with the property.
            killer = _worker_conn(clock_db)
            try:
                with killer.cursor() as cur:
                    cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
                    assert cur.fetchone()[0] is True, "the kill did not land"
            finally:
                killer.close()

            async with successor.connect() as other:
                taken = await ClockElection(other).try_acquire()
                assert taken is True, (
                    "the dead clock's election outlived its session — no"
                    " successor could ever take over, and a SIGKILLed clock"
                    " would freeze the deployment's scheduling"
                )
                await other.execute(_t("SELECT pg_advisory_unlock_all()"))

            # Retire the killed connection DETERMINISTICALLY. Left to the
            # garbage collector it raises inside a finalizer, which pytest
            # escalates to PytestUnraisableExceptionWarning and an rc of 1 —
            # 15 passed and a red exit, which CI reads as a failure and which
            # took a full run to attribute.
            with contextlib.suppress(Exception):
                await conn.invalidate()
            with contextlib.suppress(Exception):
                await conn.close()
        finally:
            await dead.dispose()
            await successor.dispose()


class TestADuplicatePlanSlotMintsNoSecondIntent:
    """The gate's third clause. Key 1 is the guard, and the election does not
    cover this edge — a duplicate `plan_slot` JOB is legal and harmless
    precisely because the INTENT insert is idempotent."""

    def _engine(self, clock_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            clock_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=2,
            max_overflow=0,
        )

    async def _tenant(self, conn, clock_db):
        from sqlalchemy import text as _t

        await conn.execute(
            _t("SELECT set_config('app.tenant_id', :v, false)"), {"v": clock_db["ws"]}
        )
        await conn.execute(_t("SELECT set_config('app.actor_kind', 'system', false)"))

    @pytest.mark.asyncio
    async def test_running_the_executor_twice_mints_one_intent(self, clock_db):
        from src.services.target.scheduler import execute_plan_slot

        account = _new_account(clock_db)
        _new_media(clock_db)
        slot = _owner_exec(clock_db, "SELECT now()", fetch=True)[0][0]

        engine = self._engine(clock_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, clock_db)
                first = await execute_plan_slot(
                    conn,
                    workspace_id=clock_db["ws"],
                    ig_account_id=account,
                    slot_at=slot,
                    provider_account_ref=f"ref-{uuid.uuid4().hex[:8]}",
                    approval_mode="manual",
                )
                await conn.commit()
            assert first is not None, "positive control: the first run minted"

            async with engine.connect() as conn:
                await self._tenant(conn, clock_db)
                second = await execute_plan_slot(
                    conn,
                    workspace_id=clock_db["ws"],
                    ig_account_id=account,
                    slot_at=slot,
                    provider_account_ref=f"ref-{uuid.uuid4().hex[:8]}",
                    approval_mode="manual",
                )
                await conn.commit()
        finally:
            await engine.dispose()

        assert second is None, "the duplicate execution minted a second intent"
        assert (
            _owner_exec(
                clock_db,
                "SELECT count(*) FROM post_intents WHERE ig_account_id = %s"
                " AND schedule_slot_at = %s",
                (account, slot),
                fetch=True,
            )[0][0]
            == 1
        )

    def test_key_1_is_what_refuses_and_it_refuses_BY_NAME(self, clock_db):
        """Raw SQL against the shipped index, plus the drop proof."""
        account = _new_account(clock_db)
        media_a, media_b = _new_media(clock_db), _new_media(clock_db)
        slot = _owner_exec(clock_db, "SELECT now()", fetch=True)[0][0]
        insert = (
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state)"
            " VALUES (%s, %s, %s, %s, 'manual', %s, 'scheduled')"
        )
        assert (
            _owner_exec(
                clock_db,
                insert,
                (clock_db["ws"], account, media_a, f"r-{uuid.uuid4().hex[:8]}", slot),
            )
            == 1
        ), "positive control: the first slot insert lands"

        conn = psycopg2.connect(clock_db["owner_stream"])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                with pytest.raises(pg_errors.UniqueViolation) as exc:
                    cur.execute(
                        insert,
                        (
                            clock_db["ws"],
                            account,
                            media_b,
                            f"r-{uuid.uuid4().hex[:8]}",
                            slot,
                        ),
                    )
                assert exc.value.diag.constraint_name == "uq_intent_slot", (
                    exc.value.diag.constraint_name
                )
        finally:
            conn.close()

        _owner_exec(clock_db, "DROP INDEX uq_intent_slot")
        try:
            assert (
                _owner_exec(
                    clock_db,
                    insert,
                    (
                        clock_db["ws"],
                        account,
                        media_b,
                        f"r-{uuid.uuid4().hex[:8]}",
                        slot,
                    ),
                )
                == 1
            ), "with key 1 gone the second slot row must insert — that is what"
            " proves the index was load-bearing"
        finally:
            _owner_exec(
                clock_db,
                "DELETE FROM post_intents WHERE ig_account_id = %s",
                (account,),
            )
            _owner_exec(clock_db, UQ_INTENT_SLOT_SQL)


class TestTheTickAndTheSweepAreBounded:
    """The gate's O(due), slot-storm and H5 clauses."""

    def _plan(self, clock_db, sql):
        conn = psycopg2.connect(clock_db["owner_stream"])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("SET enable_seqscan = off")
                cur.execute("EXPLAIN " + sql)
                return "\n".join(r[0] for r in cur.fetchall())
        finally:
            conn.close()

    def _index_def(self, clock_db, name):
        rows = _owner_exec(
            clock_db,
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            (name,),
            fetch=True,
        )
        return rows[0][0] if rows else None

    def test_the_due_scan_is_index_served_by_ix_ig_accounts_due(self, clock_db):
        """The O(due) clause, scoped to what a gate-sized table can show.

        Two claims, and the split is deliberate. That the predicate is
        INDEX-SERVABLE rather than a sequential scan is checkable here. That
        the planner PICKS a particular index is not — on a handful of rows it
        will choose whatever is cheapest, and the reap leg below demonstrated
        exactly that by picking an unrelated index. Production plan choice is
        an S.1 measurement and is not claimed.
        """
        _new_account(clock_db)
        plan = self._plan(
            clock_db,
            "SELECT a.id FROM ig_accounts a"
            " WHERE a.state = 'active' AND a.next_slot_at IS NOT NULL"
            "   AND a.next_slot_at <= now() ORDER BY a.next_slot_at",
        )
        assert "Seq Scan" not in plan, plan
        definition = self._index_def(clock_db, "ix_ig_accounts_due")
        assert definition is not None, "ix_ig_accounts_due is missing entirely"
        assert "next_slot_at" in definition, definition

    def test_a_slot_storm_inserts_within_the_tick_budget(self, clock_db):
        """More due accounts than `p_max`: the tick must stop at the budget,
        and the budget is a TOTAL across all four classes."""
        kind = _kind()
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = %s", (kind,))
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = 'plan_slot'")
        for _ in range(7):
            _new_account(clock_db)

        budget = 3
        conn = _worker_conn(clock_db)
        try:
            with conn.cursor() as cur:
                counts = _raw_tick(
                    cur, max_inserts=budget, recurring={"v": 1, kind: 3600}
                )
        finally:
            conn.close()

        total = sum(counts)
        assert total <= budget, (
            f"the tick inserted {total} against a budget of {budget} —"
            f" {counts} — the bound is a TOTAL across classes, not per-leg"
        )
        assert counts[3] == 1, "positive control: the recurring leg did run"
        assert counts[0] >= 1, "positive control: the slot leg did run"
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = %s", (kind,))

    @pytest.mark.asyncio
    async def test_the_storm_is_bounded_THROUGH_THE_SERVICE_WRAPPER_TOO(self, clock_db):
        """The same bound, driven through `tick()` rather than the raw door.

        The battery said this was missing: making the wrapper ignore its
        `max_inserts` and pass 10000 left the whole gate green, because the
        storm test above calls the door directly. A wrapper whose parameter
        nothing exercises is a parameter in name only.
        """
        from sqlalchemy import text as _t
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.scheduler import tick as service_tick

        kind = _kind()
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = %s", (kind,))
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = 'plan_slot'")
        for _ in range(7):
            _new_account(clock_db)

        budget = 3
        engine = create_async_engine(
            clock_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=1,
            max_overflow=0,
        )
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    _t("SELECT set_config('app.tenant_id', :v, false)"),
                    {"v": clock_db["ws"]},
                )
                counts = await service_tick(
                    conn,
                    max_inserts=budget,
                    refresh_cadence_seconds=REFRESH_CADENCE_S,
                    recurring={"v": 1, kind: 3600},
                )
                await conn.commit()
        finally:
            await engine.dispose()

        total = sum(counts.values())
        assert total <= budget, (
            f"the wrapper inserted {total} against max_inserts={budget}"
            f" — {counts} — its budget parameter is not reaching the door"
        )
        assert counts["recurring_jobs"] == 1, "positive control: the tick ran"
        _owner_exec(clock_db, "DELETE FROM jobs WHERE kind = %s", (kind,))

    @pytest.mark.asyncio
    async def test_the_reaper_sweep_is_bounded_on_seeded_fixtures(self, clock_db):
        """H5: more expired work than `p_lim`, one sweep, bounded."""
        from src.services.target.scheduler import execute_reap_expired
        from sqlalchemy.ext.asyncio import create_async_engine

        account = _new_account(clock_db)
        for _ in range(6):
            media = _new_media(clock_db)
            _owner_exec(
                clock_db,
                "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
                " provider_account_ref, approval_mode, schedule_slot_at, state)"
                " VALUES (%s, %s, %s, %s, 'manual', now() - interval '2 hours',"
                " 'scheduled')",
                (
                    clock_db["ws"],
                    account,
                    media,
                    f"r-{uuid.uuid4().hex[:8]}",
                ),
            )
        overdue = _owner_exec(
            clock_db,
            "SELECT count(*) FROM post_intents WHERE ig_account_id = %s"
            " AND state = 'scheduled' AND schedule_slot_at < now()",
            (account,),
            fetch=True,
        )[0][0]
        assert overdue >= 6, "positive control: there is more work than the bound"

        engine = create_async_engine(
            clock_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=1,
            max_overflow=0,
        )
        try:
            from sqlalchemy import text as _t

            async with engine.connect() as conn:
                await conn.execute(
                    _t("SELECT set_config('app.tenant_id', :v, false)"),
                    {"v": clock_db["ws"]},
                )
                touched = await execute_reap_expired(
                    conn,
                    limit=2,
                    approval_ttl_seconds=APPROVAL_TTL_S,
                    approved_ttl_seconds=APPROVED_TTL_S,
                )
                await conn.commit()
        finally:
            await engine.dispose()

        assert touched <= 2, (
            f"the sweep touched {touched} rows against p_lim=2 — the bound is"
            " the sweep's TOTAL, and independent per-leg limits are the R6"
            " defect the door's own comment records"
        )
        assert touched >= 1, "positive control: the sweep did reap something"

    def test_the_intent_reap_leg_is_index_served_by_ix_intents_reap_slot(
        self, clock_db
    ):
        """Same scoping as the tick's, and this is the test that earned it.

        The first version asserted the plan named `ix_intents_reap_slot` and
        failed: with `enable_seqscan` off on a handful of rows the planner
        chose `uq_intent_live_subject`, which is a perfectly good plan and
        says nothing about production. Asserting a planner choice at gate
        scale is asserting an artifact of the fixture.
        """
        plan = self._plan(
            clock_db,
            "SELECT id FROM post_intents"
            " WHERE state IN ('scheduled','prompt_pending')"
            "   AND schedule_slot_at < now()",
        )
        assert "Seq Scan" not in plan, plan
        definition = self._index_def(clock_db, "ix_intents_reap_slot")
        assert definition is not None, "ix_intents_reap_slot is missing entirely"
        assert "schedule_slot_at" in definition, definition


class TestTheClockReadsTheDatabaseClock:
    """The skew decision, asserted rather than left to a docstring.

    This estate runs on an RTC-less Pi with real history of skew, so "the
    scheduler is immune to host clock jumps" is a claim that must be checkable.
    It is: the door takes no timestamp, so every `<= now()` is evaluated in
    Postgres and a host whose wall clock moves cannot change which rows are
    due.
    """

    def test_fn_clock_tick_takes_no_timestamp_argument(self, clock_db):
        args = _owner_exec(
            clock_db,
            "SELECT pg_get_function_arguments(oid) FROM pg_proc"
            " WHERE proname = 'fn_clock_tick'",
            fetch=True,
        )[0][0]
        assert "timestamp" not in args.lower(), (
            f"the tick accepts a caller-supplied time ({args}) — the host's"
            " clock would then decide what is due, and this estate's host has"
            " no RTC"
        )
        assert "p_max" in args and "interval" in args

    def test_fn_reaper_sweep_takes_no_timestamp_argument_either(self, clock_db):
        args = _owner_exec(
            clock_db,
            "SELECT pg_get_function_arguments(oid) FROM pg_proc"
            " WHERE proname = 'fn_reaper_sweep'",
            fetch=True,
        )[0][0]
        assert "timestamp" not in args.lower(), args
