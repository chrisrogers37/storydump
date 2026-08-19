"""L.2 gate — jobs + leases + fencing + durable counters (#859, `04` §L.2).

THE DATABASE IS THE AUTHORITY, SO THE HEAVY HALF BYPASSES THE SERVICE — the
L.1 doctrine, unchanged: `uq_jobs_serialized_lease`, `ck_jobs_system_kinds`,
the NOT NULL, and the two `059` doors are what these tests prove, from plain
psycopg2 connections as the real `svc_worker` login. A smaller final class
asserts the L.2 service surfaces those verdicts rather than substituting its
own.

Concurrency doctrine, from #883 and it is load-bearing:

* Every race here runs on REAL concurrent connections at **READ COMMITTED** —
  asserted in its own test, not assumed — because #883 measured the same race
  passing at stricter levels while lying at ours.
* No refusal is inferred from a bare rowcount. #883's loser got ``rows=1``.
  Where rowcount IS the discriminator (the finalization CAS), the docstring
  says why it can be: the CAS misses on the token PREDICATE (``rows=0``),
  which is a different mechanism from the self-transition no-op that reported
  a phantom win.
* Every refusal has a rowcount POSITIVE CONTROL — the thing being refused is
  first shown to exist / the accepted variant is shown to write — because two
  of L.1's original probes passed vacuously over empty sets.
* Every DB-enforced guard is proven LOAD-BEARING by dropping it and watching
  the refusal disappear (the alex standard: you cannot mock away a
  constraint), then restoring it.

The serialization race is made DETERMINISTIC rather than probabilistic: the
first claimer holds its transaction open, so the second claimer's index entry
blocks on the uncommitted lease and the violation fires exactly at the
winner's commit — the #883 block-then-reevaluate window, driven on purpose.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime, timezone

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

CLAIM_SQL = "SELECT * FROM fn_claim_job(%s, %s, %s::interval, %s)"
CAS_SQL = (
    "UPDATE jobs SET state = %s WHERE id = %s AND lease_token = %s AND state = 'leased'"
)
_UPSERT_TEMPLATE = (
    "INSERT INTO rate_counters AS rc (scope, key, window_start, count)"
    " VALUES (%s, %s, %s, 1)"
    " ON CONFLICT (scope, key, window_start)"
    "   DO UPDATE SET count = rc.count + 1{guard}"
    " RETURNING count"
)
UPSERT_SQL = _UPSERT_TEMPLATE.format(guard=" WHERE rc.count < %s")
UNGUARDED_UPSERT_SQL = _UPSERT_TEMPLATE.format(guard="")
#: `ck_jobs_system_kinds`, verbatim from 056 — re-added after the drop proof.
PAIRING_CHECK_SQL = (
    "ALTER TABLE jobs ADD CONSTRAINT ck_jobs_system_kinds CHECK ("
    "(workspace_id IS NULL) = (kind IN"
    " ('reconcile_ambiguous','reap_expired','reap_transit_assets',"
    "'retention_sweep','reencrypt_credentials','send_email')))"
)
UQ_LEASE_SQL = (
    "CREATE UNIQUE INDEX uq_jobs_serialized_lease ON jobs (serialization_key)"
    " WHERE state = 'leased'"
)


@pytest.fixture(scope="module")
def jobs_db(admin_conn, owner_actor):
    """The replayed full schema + passwords + one seeded workspace, once.

    Module-scoped on the `target` fixture's reasoning (a role-carrying
    template cannot be held). Tests keep module scope safe by minting a
    UNIQUE serialization key per scenario and cleaning up any guard they
    drop."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "l2-gate")
        finally:
            conn.close()
        yield {
            "worker": as_user(db, "svc_worker"),
            "owner_stream": dsn,
            "ws": chain["ws"],
        }
    finally:
        gen.close()


def _key() -> str:
    return f"acct:{uuid.uuid4()}"


def _owner_exec(jobs_db, sql, params=None, fetch=False):
    """One statement as the schema owner (bypasses RLS; owns every guard)."""
    conn = psycopg2.connect(jobs_db["owner_stream"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        conn.close()


def _worker_conn(jobs_db, *, autocommit=True):
    conn = psycopg2.connect(jobs_db["worker"])
    conn.autocommit = autocommit
    return conn


def _seed_job(
    jobs_db,
    *,
    key,
    kind="publish_pipeline",
    lane="interactive",
    tenant=True,
    run_at_offset_s=0.0,
):
    """Insert one `ready` job as the owner; returns its id."""
    ws = jobs_db["ws"] if tenant else None
    rows = _owner_exec(
        jobs_db,
        "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
        " run_at, payload, max_attempts)"
        " VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s),"
        " '{\"v\": 1}', %s) RETURNING id",
        (str(ws) if ws else None, kind, lane, key, run_at_offset_s, 3),
        fetch=True,
    )
    return rows[0][0]


def _claim(conn, *, worker="w", lease="60 seconds"):
    """Run the claim door on *conn*; returns the job row as a dict or None.

    Does NOT commit — the caller owns the transaction, which is what lets the
    race tests hold a claim open deliberately."""
    with conn.cursor() as cur:
        cur.execute(CLAIM_SQL, ("interactive", worker, lease, 5))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


@pytest.fixture(autouse=True)
def _hermetic_jobs(jobs_db):
    """Every test starts with an empty jobs table. The claim door takes the
    oldest ready row on the lane regardless of key, so under module scope any
    earlier test's leftover would win a later test's claim — hermeticity is a
    module property, so it is autouse rather than an opt-in preamble a new
    test could forget. DELETE as the owner is the retention sweep's own verb
    over these rows; cancelling them would model the leased→cancelled edge
    `02` §5 reserves for the job's own worker."""
    _owner_exec(jobs_db, "DELETE FROM jobs")


def _leased_rows(jobs_db, key):
    return _owner_exec(
        jobs_db,
        "SELECT locked_by FROM jobs WHERE serialization_key = %s AND state = 'leased'",
        (key,),
        fetch=True,
    )


class TestTheLevelIsReadCommitted:
    """#883's second lesson, pinned as a fact rather than assumed: every
    concurrency claim in this file holds at the level production runs."""

    def test_worker_connections_run_read_committed(self, jobs_db):
        conn = _worker_conn(jobs_db)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW transaction_isolation")
                assert cur.fetchone()[0] == "read committed"
                cur.execute("SHOW default_transaction_isolation")
                assert cur.fetchone()[0] == "read committed"
        finally:
            conn.close()


class TestClaimDoor:
    def test_claim_leases_the_ready_job_and_counts_the_attempt(self, jobs_db):
        key = _key()
        job_id = _seed_job(jobs_db, key=key)
        conn = _worker_conn(jobs_db, autocommit=False)
        try:
            row = _claim(conn, worker="w-basic")
            conn.commit()
        finally:
            conn.close()
        assert row is not None and str(row["id"]) == str(job_id)
        assert row["state"] == "leased"
        assert row["locked_by"] == "w-basic"
        assert row["lease_token"] is not None
        assert row["attempts"] == 1, "attempts increments at claim time (059)"

    def test_a_future_run_at_is_not_claimable(self, jobs_db):
        key = _key()
        _seed_job(jobs_db, key=key, run_at_offset_s=3600)
        conn = _worker_conn(jobs_db, autocommit=False)
        try:
            # Positive control for the empty claim below: a claimable job on
            # ANOTHER key is claimed by this same connection and door.
            control_key = _key()
            _seed_job(jobs_db, key=control_key)
            first = _claim(conn, worker="w-future")
            conn.commit()
            assert first is not None and first["serialization_key"] == control_key
            second = _claim(conn, worker="w-future")
            conn.commit()
        finally:
            conn.close()
        assert second is None, "a job an hour in the future must not claim"


class TestSerializationKeyRace:
    """Two claimers, one key, driven deterministically through the #883
    window: the loser BLOCKS on the winner's uncommitted index entry and is
    REFUSED at the winner's commit — then retries clean."""

    def _race(self, jobs_db, key, *, expect_block=True):
        """Returns (winner_row, loser_error, loser_retry_result).

        *expect_block* is asserted only while the guard exists: with the
        index dropped there is no uncommitted entry to block on, so the
        contender legitimately finishes at once."""
        # Two ready jobs, one key; run_at orders A first so conn1 takes A.
        _seed_job(jobs_db, key=key, run_at_offset_s=-2)
        _seed_job(jobs_db, key=key, run_at_offset_s=-1)

        conn1 = _worker_conn(jobs_db, autocommit=False)
        conn2 = _worker_conn(jobs_db, autocommit=False)
        loser: dict = {}

        def contender():
            try:
                loser["row"] = _claim(conn2, worker="w-lose")
                conn2.commit()
            except Exception as exc:  # noqa: BLE001 — the assertion target
                loser["error"] = exc
                conn2.rollback()

        try:
            winner = _claim(conn1, worker="w-win")
            assert winner is not None, "positive control: the winner claimed"
            t = threading.Thread(target=contender)
            t.start()
            time.sleep(0.6)
            if expect_block:
                assert t.is_alive(), (
                    "the loser should be BLOCKED on the winner's uncommitted"
                    " index entry — if it finished already, the race was not"
                    " exercised and this test proves nothing"
                )
            conn1.commit()
            t.join(timeout=10)
            assert not t.is_alive()
            retry = _claim(conn2, worker="w-lose")
            conn2.commit()
            return winner, loser, retry
        finally:
            conn1.close()
            conn2.close()

    def test_one_wins_by_unique_index_and_the_loser_retries_clean(self, jobs_db):
        key = _key()
        winner, loser, retry = self._race(jobs_db, key)

        # The loser was REFUSED — loudly, by name — never told it won (#883).
        assert "row" not in loser, (
            f"the loser was handed a claim: {loser.get('row')} — the #883"
            " phantom-win shape"
        )
        err = loser["error"]
        assert isinstance(err, pg_errors.UniqueViolation), err
        assert err.diag.constraint_name == "uq_jobs_serialized_lease", (
            err.diag.constraint_name
        )

        # Exactly one live owner for the key.
        rows = _leased_rows(jobs_db, key)
        assert len(rows) == 1 and rows[0][0] == "w-win"

        # The clean retry: the winner's committed lease trips the claim
        # query's NOT EXISTS, so the key is excluded and the loser gets
        # None — no exception, no bookkeeping, exactly `02` §5's rule.
        assert retry is None

    def test_the_unique_index_is_what_refuses_the_double_lease(self, jobs_db):
        """Drop the guard, watch the refusal disappear, restore the guard."""
        key = _key()
        _owner_exec(jobs_db, "DROP INDEX uq_jobs_serialized_lease")
        try:
            winner, loser, _ = self._race(jobs_db, key, expect_block=False)
            assert "error" not in loser, (
                "the double-lease was still refused after dropping"
                f" uq_jobs_serialized_lease, so the refusal was NOT the"
                f" index: {loser.get('error')}"
            )
            assert loser["row"] is not None
            assert len(_leased_rows(jobs_db, key)) == 2, (
                "with the guard gone both claimers must hold leases — that"
                " is what proves the index was load-bearing"
            )
        finally:
            # Two leased rows on one key would refuse the index rebuild:
            # retire them first, then restore the guard for later tests.
            _owner_exec(
                jobs_db,
                "UPDATE jobs SET state = 'cancelled'"
                " WHERE serialization_key = %s AND state = 'leased'",
                (key,),
            )
            _owner_exec(jobs_db, UQ_LEASE_SQL)


class TestKillResumeExpiryAndFencing:
    def test_kill_expiry_recovery_resume_and_the_cas_fence(self, jobs_db):
        key = _key()
        job_id = _seed_job(jobs_db, key=key)

        # Claim with a tiny lease, commit, then KILL the owner (close without
        # finalizing). The committed lease survives its owner.
        conn1 = _worker_conn(jobs_db, autocommit=False)
        row1 = _claim(conn1, worker="w-dead", lease="300 milliseconds")
        conn1.commit()
        conn1.close()
        assert row1 is not None
        stale_token = row1["lease_token"]

        # One live owner: while the lease is live nobody else can claim the
        # key, even though the owner is gone.
        conn = _worker_conn(jobs_db, autocommit=False)
        try:
            assert _claim(conn, worker="w-eager") is None
            conn.commit()
        finally:
            conn.close()

        # Expired work recovers — via the reaper's first leg, not by magic.
        time.sleep(0.5)
        recovered = _owner_exec(
            jobs_db,
            "SELECT fn_reaper_sweep(50, '72 hours'::interval, '72 hours'::interval)",
            fetch=True,
        )
        assert recovered[0][0] >= 1, "the sweep must re-ready the expired lease"
        state = _owner_exec(
            jobs_db,
            "SELECT state, lease_token FROM jobs WHERE id = %s",
            (job_id,),
            fetch=True,
        )[0]
        assert state[0] == "ready" and state[1] is None

        # Resume: a new owner claims and mints a NEW token.
        conn2 = _worker_conn(jobs_db, autocommit=False)
        row2 = _claim(conn2, worker="w-live")
        conn2.commit()
        conn2.close()
        assert row2 is not None and str(row2["id"]) == str(job_id)
        live_token = row2["lease_token"]
        assert live_token != stale_token
        assert row2["attempts"] == 2
        rows = _leased_rows(jobs_db, key)
        assert len(rows) == 1 and rows[0][0] == "w-live", "one live owner"

        # THE FENCE. Positive control first: the row being refused exists,
        # leased, visible to the CAS connection — so a zero-rowcount below is
        # a predicate miss on the TOKEN, not a refusal over an empty set.
        cas = _worker_conn(jobs_db, autocommit=False)
        try:
            with cas.cursor() as cur:
                cur.execute("SET app.tenant_id = %s", (str(jobs_db["ws"]),))
                cur.execute(
                    "SELECT count(*) FROM jobs WHERE id = %s AND state = 'leased'",
                    (job_id,),
                )
                assert cur.fetchone()[0] == 1

                cur.execute(CAS_SQL, ("succeeded", job_id, stale_token))
                assert cur.rowcount == 0, (
                    "a resumed stale owner finalized — the CAS did not fence"
                )
            cas.commit()

            after = _owner_exec(
                jobs_db,
                "SELECT state, locked_by FROM jobs WHERE id = %s",
                (job_id,),
                fetch=True,
            )[0]
            assert after == ("leased", "w-live"), (
                "the fenced CAS must leave the live owner untouched"
            )

            # And the live token DOES finalize — the CAS discriminates on the
            # token, which is why rowcount is trustworthy here and was not in
            # #883: this is a WHERE miss, not a self-transition no-op.
            with cas.cursor() as cur:
                cur.execute("SET app.tenant_id = %s", (str(jobs_db["ws"]),))
                cur.execute(CAS_SQL, ("succeeded", job_id, live_token))
                assert cur.rowcount == 1
            cas.commit()
        finally:
            cas.close()
        final = _owner_exec(
            jobs_db,
            "SELECT state FROM jobs WHERE id = %s",
            (job_id,),
            fetch=True,
        )[0][0]
        assert final == "succeeded"


class TestRawSqlInvariants:
    """The gate's pass-3 constraint proofs. Every refusal: positive control,
    named refusal, drop-the-guard acceptance, guard restored."""

    def _insert_job(self, jobs_db, *, kind, ws, key):
        return _owner_exec(
            jobs_db,
            "INSERT INTO jobs (workspace_id, kind, lane, serialization_key,"
            " payload, max_attempts)"
            " VALUES (%s, %s, 'interactive', %s, '{\"v\": 1}', 3)",
            (ws, kind, key),
        )

    def test_the_pairing_equivalence_refuses_both_directions(self, jobs_db):
        ws = str(jobs_db["ws"])

        # Positive controls: both LEGAL pairings insert.
        assert self._insert_job(jobs_db, kind="reap_expired", ws=None, key=_key()) == 1
        assert (
            self._insert_job(jobs_db, kind="publish_pipeline", ws=ws, key=_key()) == 1
        )

        # A system kind carrying a workspace_id.
        with pytest.raises(pg_errors.CheckViolation) as exc1:
            self._insert_job(jobs_db, kind="reap_expired", ws=ws, key=_key())
        assert exc1.value.diag.constraint_name == "ck_jobs_system_kinds"

        # A tenant kind without one.
        with pytest.raises(pg_errors.CheckViolation) as exc2:
            self._insert_job(jobs_db, kind="publish_pipeline", ws=None, key=_key())
        assert exc2.value.diag.constraint_name == "ck_jobs_system_kinds"

    def test_the_pairing_check_is_what_refuses(self, jobs_db):
        ws = str(jobs_db["ws"])
        _owner_exec(jobs_db, "ALTER TABLE jobs DROP CONSTRAINT ck_jobs_system_kinds")
        try:
            marker = _key()
            assert (
                self._insert_job(jobs_db, kind="reap_expired", ws=ws, key=marker) == 1
            ), "with the CHECK gone the illegal pairing must insert"
            _owner_exec(
                jobs_db,
                "DELETE FROM jobs WHERE serialization_key = %s",
                (marker,),
            )
        finally:
            _owner_exec(jobs_db, PAIRING_CHECK_SQL)

    def test_a_null_serialization_key_refuses_and_the_not_null_is_why(self, jobs_db):
        ws = str(jobs_db["ws"])
        # Positive control: the same insert with a key writes.
        assert self._insert_job(jobs_db, kind="publish_pipeline", ws=ws, key=_key())

        with pytest.raises(pg_errors.NotNullViolation):
            self._insert_job(jobs_db, kind="publish_pipeline", ws=ws, key=None)

        _owner_exec(
            jobs_db,
            "ALTER TABLE jobs ALTER COLUMN serialization_key DROP NOT NULL",
        )
        try:
            assert (
                self._insert_job(jobs_db, kind="publish_pipeline", ws=ws, key=None) == 1
            ), "with NOT NULL gone the NULL key must insert"
            _owner_exec(jobs_db, "DELETE FROM jobs WHERE serialization_key IS NULL")
        finally:
            _owner_exec(
                jobs_db,
                "ALTER TABLE jobs ALTER COLUMN serialization_key SET NOT NULL",
            )

    def test_a_manufactured_double_lease_refuses_on_the_unique_index(self, jobs_db):
        """No doors involved: two rows, one key, both forced to `leased` by
        raw UPDATE — the second refuses on `uq_jobs_serialized_lease` (the
        drop-proof for this guard is the race class's second test)."""
        key = _key()
        a = _seed_job(jobs_db, key=key)
        b = _seed_job(jobs_db, key=key)
        assert (
            _owner_exec(
                jobs_db,
                "UPDATE jobs SET state = 'leased' WHERE id = %s",
                (a,),
            )
            == 1
        ), "positive control: the first lease writes"
        with pytest.raises(pg_errors.UniqueViolation) as exc:
            _owner_exec(
                jobs_db,
                "UPDATE jobs SET state = 'leased' WHERE id = %s",
                (b,),
            )
        assert exc.value.diag.constraint_name == "uq_jobs_serialized_lease"
        _owner_exec(
            jobs_db,
            "UPDATE jobs SET state = 'cancelled'"
            " WHERE serialization_key = %s AND state = 'leased'",
            (key,),
        )


class TestWhatTheDatabaseDeliberatelyDoesNotEnforce:
    """The #883 follow-on (branden, from DDL inspection — MEASURED here):
    `jobs` has NO transition guard, and the L.2 service contract is written
    against that fact rather than around an assumption.

    Why no guard is the right reading, argued not assumed: `02` §5 denies
    jobs state the authority role that earned `post_intents` its trigger —
    "execution bookkeeping only; it is never the authority on whether an
    external effect happened" — and the two transitions where concurrency
    could lie are guarded by re-evaluation-correct PREDICATES proven above
    (the partial unique index entering `leased`; the token+state CAS leaving
    it, including the double-finalize case). The residual — a bare UPDATE
    resurrecting a terminal row — has no code path and no silent-success
    flavor: nothing is told it won, because nothing legitimate asks.

    These tests pin the ABSENCE as a tripwire: if an edge guard ever lands
    on `jobs`, they go red and the service contract is revisited
    consciously instead of drifting. Whether the machinery tables deserve
    defense-in-depth edge triggers at all is a plan-amendment question that
    spans L.3/L.4's tables too (`channel_outbox`, `provider_operations`) —
    filed separately, deliberately not smuggled into L.2."""

    def test_an_illegal_edge_is_not_refused_today(self, jobs_db):
        key = _key()
        job = _seed_job(jobs_db, key=key)
        assert (
            _owner_exec(
                jobs_db,
                "UPDATE jobs SET state = 'succeeded' WHERE id = %s",
                (job,),
            )
            == 1
        ), "positive control: the row exists and writes"
        assert (
            _owner_exec(
                jobs_db,
                "UPDATE jobs SET state = 'ready' WHERE id = %s",
                (job,),
            )
            == 1
        ), (
            "succeeded -> ready was REFUSED: a transition guard now exists on"
            " jobs — revisit the L.2 service contract, which was written"
            " against the measured absence"
        )

    def test_a_same_state_write_is_not_refused_today(self, jobs_db):
        key = _key()
        job = _seed_job(jobs_db, key=key)
        assert (
            _owner_exec(
                jobs_db,
                "UPDATE jobs SET state = 'ready' WHERE id = %s AND state = 'ready'",
                (job,),
            )
            == 1
        ), (
            "a same-state write was REFUSED: jobs has grown a #886-style"
            " no-self-transition guard — revisit the service contract"
        )


def _window() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestRateCounters:
    """`02` §6: the WHERE inside the UPSERT is THE guard — there is
    deliberately no database-side limit, so the drop-the-guard proof here is
    running the same statement WITHOUT its WHERE and watching the limit stop
    holding."""

    def _hit(self, conn, key, limit, *, guarded=True):
        sql = UPSERT_SQL if guarded else UNGUARDED_UPSERT_SQL
        params = ("ws_admission", key, _window()) + ((limit,) if guarded else ())
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return int(row[0]) if row else None

    def _count(self, jobs_db, key):
        rows = _owner_exec(
            jobs_db,
            "SELECT count FROM rate_counters WHERE scope = 'ws_admission' AND key = %s",
            (key,),
            fetch=True,
        )
        return rows[0][0] if rows else None

    def test_increments_then_denies_at_the_limit(self, jobs_db):
        key = str(uuid.uuid4())
        conn = _worker_conn(jobs_db)
        try:
            assert [self._hit(conn, key, 3) for _ in range(3)] == [1, 2, 3]
            # Positive control: the row being defended exists at the limit.
            assert self._count(jobs_db, key) == 3
            assert self._hit(conn, key, 3) is None, "over limit must deny"
            assert self._count(jobs_db, key) == 3, "a denied hit writes nothing"
        finally:
            conn.close()

    def test_denies_at_the_limit_under_concurrent_increments(self, jobs_db):
        key = str(uuid.uuid4())
        limit, contenders = 3, 6
        barrier = threading.Barrier(contenders, timeout=30)
        results: list = []
        lock = threading.Lock()

        def hit():
            conn = _worker_conn(jobs_db)
            try:
                barrier.wait()
                value = self._hit(conn, key, limit)
            finally:
                conn.close()
            with lock:
                results.append(value)

        threads = [threading.Thread(target=hit) for _ in range(contenders)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        allowed = [r for r in results if r is not None]
        denied = [r for r in results if r is None]
        assert len(results) == contenders
        assert len(allowed) == limit, (
            f"exactly the limit may pass: {sorted(allowed)} / {len(denied)} denied"
        )
        assert sorted(allowed) == [1, 2, 3], "each success saw a serial count"
        assert self._count(jobs_db, key) == limit

    def test_the_where_clause_is_what_denies(self, jobs_db):
        key = str(uuid.uuid4())
        conn = _worker_conn(jobs_db)
        try:
            for _ in range(3):
                self._hit(conn, key, 3)
            assert self._hit(conn, key, 3) is None, "guarded: denied at limit"
            assert self._hit(conn, key, 3, guarded=False) == 4, (
                "with the WHERE removed the same statement passes the limit —"
                " the clause is the guard"
            )
        finally:
            conn.close()
            _owner_exec(
                jobs_db,
                "DELETE FROM rate_counters WHERE scope = 'ws_admission' AND key = %s",
                (key,),
            )


class TestTheServicePathAgreesWithTheDoors:
    """Deliberately the smaller half: the service wraps the doors and
    translates their verdicts; nothing here may diverge from the raw results
    above without the service having grown a second authority."""

    def _engine(self, jobs_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            jobs_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=1,
            max_overflow=0,
        )

    @pytest.mark.asyncio
    async def test_claim_and_token_cas_finalize_round_trip(self, jobs_db):
        from src.services.target.jobs import JobFenced, claim_job, finalize_job

        key = _key()
        _seed_job(jobs_db, key=key, tenant=False, kind="reap_expired")
        # A system singleton keys on its kind name (`02` §5) — but module
        # scope means reap_expired may already be seeded; use the unique key
        # anyway and accept the registry naming as the doors' concern.
        engine = self._engine(jobs_db)
        try:
            async with engine.connect() as conn:
                row = await claim_job(
                    conn,
                    lane="interactive",
                    worker="w-svc",
                    lease_seconds=60.0,
                    ws_lane_cap=5,
                )
            assert row is not None and row["serialization_key"] == key

            # Stale-token CAS through the service: typed, and it aborts the tx.
            async with engine.connect() as conn:
                with pytest.raises(JobFenced):
                    await finalize_job(conn, row["id"], uuid.uuid4(), "succeeded")
                await conn.rollback()
                await finalize_job(conn, row["id"], row["lease_token"], "succeeded")
                await conn.commit()
        finally:
            await engine.dispose()
        state = _owner_exec(
            jobs_db,
            "SELECT state FROM jobs WHERE serialization_key = %s",
            (key,),
            fetch=True,
        )[0][0]
        assert state == "succeeded"

    @pytest.mark.asyncio
    async def test_a_lost_race_surfaces_as_a_clean_none(self, jobs_db):
        """The service-level half of "the loser retries clean": the raw race
        above proved the REFUSAL; this proves the wrapper converts it into
        the next claim's answer — here None — with no exception escaping."""
        import asyncio

        from src.services.target.jobs import claim_job

        key = _key()
        _seed_job(jobs_db, key=key, run_at_offset_s=-2)
        _seed_job(jobs_db, key=key, run_at_offset_s=-1)

        holder = _worker_conn(jobs_db, autocommit=False)
        engine = self._engine(jobs_db)
        try:
            winner = _claim(holder, worker="w-hold")
            assert winner is not None, "positive control"

            async def lose():
                async with engine.connect() as conn:
                    return await claim_job(
                        conn,
                        lane="interactive",
                        worker="w-async",
                        lease_seconds=60.0,
                        ws_lane_cap=5,
                    )

            task = asyncio.create_task(lose())
            await asyncio.sleep(0.6)
            assert not task.done(), "the contender should be blocked mid-race"
            holder.commit()
            result = await asyncio.wait_for(task, timeout=15)
            assert result is None, (
                "after the lost race the retry must come back clean and"
                f" empty, not {result!r}"
            )
        finally:
            holder.close()
            await engine.dispose()
        assert len(_leased_rows(jobs_db, key)) == 1

    @pytest.mark.asyncio
    async def test_the_heartbeat_extends_and_notices_a_short_count(self, jobs_db):
        from src.services.target.jobs import LeaseHeartbeat, claim_job

        key = _key()
        _seed_job(jobs_db, key=key, tenant=False, kind="retention_sweep")
        engine = self._engine(jobs_db)
        try:
            async with engine.connect() as conn:
                row = await claim_job(
                    conn,
                    lane="interactive",
                    worker="w-beat",
                    lease_seconds=2.0,
                    ws_lane_cap=5,
                )
            assert row is not None
            before = _owner_exec(
                jobs_db,
                "SELECT locked_until FROM jobs WHERE id = %s",
                (str(row["id"]),),
                fetch=True,
            )[0][0]

            shorts: list = []
            hb = LeaseHeartbeat(
                engine.connect,
                interval_seconds=0.05,
                lease_seconds=30.0,
                on_short_count=lambda expected, got: shorts.append((expected, got)),
            )
            hb.register(row["lease_token"])
            assert await hb.beat_once() == 1
            after = _owner_exec(
                jobs_db,
                "SELECT locked_until FROM jobs WHERE id = %s",
                (str(row["id"]),),
                fetch=True,
            )[0][0]
            assert after > before, "the beat must extend locked_until"
            assert hb.short_beats == 0 and shorts == []

            # A token the door no longer honors: the count comes back short,
            # the tripwire fires, and WHICH lease is stale stays the CAS's
            # question — the count alone cannot say (059's contract).
            hb.register(uuid.uuid4())
            assert await hb.beat_once() == 1
            assert hb.short_beats == 1 and shorts == [(2, 1)]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_double_finalize_is_fenced_not_replayed(self, jobs_db):
        """The state predicate the retained token requires: after a successful
        finalize, the SAME live token must not finalize again (a lost commit
        ack + client retry would otherwise overwrite the terminal row and be
        told it won — the #883 phantom-win shape one table over)."""
        from src.services.target.jobs import JobFenced, claim_job, finalize_job

        key = _key()
        _seed_job(jobs_db, key=key, tenant=False, kind="reap_transit_assets")
        engine = self._engine(jobs_db)
        try:
            async with engine.connect() as conn:
                row = await claim_job(
                    conn,
                    lane="interactive",
                    worker="w-double",
                    lease_seconds=60.0,
                    ws_lane_cap=5,
                )
            assert row is not None, "positive control: the job was claimed"
            async with engine.connect() as conn:
                await finalize_job(conn, row["id"], row["lease_token"], "succeeded")
                await conn.commit()
            async with engine.connect() as conn:
                with pytest.raises(JobFenced):
                    await finalize_job(conn, row["id"], row["lease_token"], "failed")
                await conn.rollback()
        finally:
            await engine.dispose()
        state = _owner_exec(
            jobs_db,
            "SELECT state FROM jobs WHERE serialization_key = %s",
            (key,),
            fetch=True,
        )[0][0]
        assert state == "succeeded", "the replayed finalize must not overwrite"

    @pytest.mark.asyncio
    async def test_the_heartbeat_loop_beats_on_its_own_timer(self, jobs_db):
        """The timer half of the task, run for real: start(), a few intervals
        of wall clock, stop() — the loop must have beaten more than once and
        extended the lease past where the claim left it."""
        from src.services.target.jobs import LeaseHeartbeat, claim_job

        key = _key()
        _seed_job(jobs_db, key=key, tenant=False, kind="retention_sweep")
        engine = self._engine(jobs_db)
        try:
            async with engine.connect() as conn:
                row = await claim_job(
                    conn,
                    lane="interactive",
                    worker="w-loop",
                    lease_seconds=2.0,
                    ws_lane_cap=5,
                )
            assert row is not None
            before = _owner_exec(
                jobs_db,
                "SELECT locked_until FROM jobs WHERE id = %s",
                (str(row["id"]),),
                fetch=True,
            )[0][0]

            hb = LeaseHeartbeat(
                engine.connect, interval_seconds=0.05, lease_seconds=30.0
            )
            hb.register(row["lease_token"])
            hb.start()
            await asyncio.sleep(0.3)
            await hb.stop()

            assert hb.beats >= 2, "the timer must have driven repeated beats"
            assert hb.consecutive_failures == 0
            after = _owner_exec(
                jobs_db,
                "SELECT locked_until FROM jobs WHERE id = %s",
                (str(row["id"]),),
                fetch=True,
            )[0][0]
            assert after > before
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_rate_counter_service_allows_then_denies(self, jobs_db):
        from src.services.target.rate_counters import increment, window_start

        engine = self._engine(jobs_db)
        key = str(uuid.uuid4())
        try:
            async with engine.connect() as conn:
                w = _window()
                assert (
                    await increment(
                        conn, scope="preauth_ip", key=key, window_start=w, limit=1
                    )
                    == 1
                )
                assert (
                    await increment(
                        conn, scope="preauth_ip", key=key, window_start=w, limit=1
                    )
                    is None
                )
                await conn.commit()
        finally:
            await engine.dispose()

        # The truncation helper is pure; pin its fixed-window arithmetic.
        t = datetime(2026, 8, 19, 14, 37, 41, tzinfo=timezone.utc)
        assert window_start(t, 60) == datetime(
            2026, 8, 19, 14, 37, 0, tzinfo=timezone.utc
        )
        assert window_start(t, 3600) == datetime(
            2026, 8, 19, 14, 0, 0, tzinfo=timezone.utc
        )
