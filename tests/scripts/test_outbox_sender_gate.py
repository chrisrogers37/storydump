"""L.4 gate — channel outbox + Telegram sender (#861, `04` §L.4).

*"Stopped-sender and lost-ack injections strand nothing; duplicate sends
bounded exactly as the policy states (≤1 extra notification; prompts converge
on supersede)."*

THE HAZARD THIS INCREMENT EXISTS TO CLOSE. `channel_outbox` carries no unique
index and no lease token (#886, DDL inspection; #887 ruling: the argument that
made `jobs` safe does NOT transfer, because `jobs` has both and this table has
neither). An outbox exists to make a send happen exactly once, so #883's
failure — two writers both told they won — reads here as the machine believing
it sent something it did not, or sending twice while believing it sent once.

So the mechanism is proven, not argued. Two facts, each tested against the
real database as the real `svc_worker` login:

1. `uq_jobs_serialized_lease` on `tg:<binding_id>` admits ONE live sender per
   chat, writer-independently — `TestOneSenderPerBinding`.
2. Every outbox write is a CAS on the state it leaves, so a loser matches zero
   rows rather than being handed a phantom win — `TestTheClaimIsSingleWinner`,
   `TestAStaleSenderCannotOverwriteALiveOne`.

Concurrency doctrine, inherited from #883/#890 and load-bearing:

* Every race runs on REAL concurrent connections at **READ COMMITTED**,
  asserted in its own test — 19 of the L.2 gate's tests still passed at
  REPEATABLE READ, including its race, so a concurrency test at the wrong
  level proves nothing about production.
* No refusal is inferred from a bare rowcount alone. Where rowcount IS the
  discriminator, the docstring says why it can be: a predicate miss on a
  column the winner changed, not #883's self-transition no-op.
* Every refusal carries a positive control — the row being refused exists and
  is visible to the refusing connection.
* Every guard is proven load-bearing by removing it and watching the refusal
  disappear.
"""

from __future__ import annotations

import itertools
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

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

#: `05`: 20 msgs/min/group, 30/s global. Parameters, never hardcoded in src.
CHAT_LIMIT, CHAT_WINDOW_S = 20, 60
GLOBAL_LIMIT, GLOBAL_WINDOW_S = 30, 1

#: The claim CAS, as a template so the unguarded variant is the SAME statement
#: minus its clause rather than string surgery on it (the #890 idiom).
_CLAIM_TEMPLATE = (
    "UPDATE channel_outbox SET state = 'sending', attempts = attempts + 1"
    " WHERE id = (SELECT id FROM channel_outbox"
    "             WHERE binding_id = %s AND state = 'pending'"
    "             ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED){guard}"
    " RETURNING id"
)
CLAIM_SQL = _CLAIM_TEMPLATE.format(guard=" AND state = 'pending'")
UNGUARDED_CLAIM_SQL = _CLAIM_TEMPLATE.format(guard="")

#: `uq_jobs_serialized_lease`, verbatim from `057` — re-added after its drop.
UQ_LEASE_SQL = (
    "CREATE UNIQUE INDEX uq_jobs_serialized_lease ON jobs (serialization_key)"
    " WHERE state = 'leased'"
)


@pytest.fixture(scope="module")
def outbox_db(admin_conn, owner_actor):
    """Replayed schema + one workspace + one telegram binding, once.

    The binding is seeded HERE and not taken from `seed_workspace_chain`,
    which creates no `channel_bindings` row — the exact vacuity the L.1 gate
    caught, where a refusal test matched zero rows and passed having refused
    nothing. Every test below asserts a rowcount, so a missing binding would
    report as "matched 0 rows" rather than hiding.
    """
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "l4-gate")
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "INSERT INTO channel_bindings"
                    " (workspace_id, channel, external_ref)"
                    " VALUES (%s, 'telegram_group', %s) RETURNING id",
                    (chain["ws"], f"tg-{uuid.uuid4().hex[:8]}"),
                )
                binding = cur.fetchone()[0]
        finally:
            conn.close()
        yield {
            "worker": as_user(db, "svc_worker"),
            "owner_stream": dsn,
            "ws": str(chain["ws"]),
            "binding": str(binding),
        }
    finally:
        gen.close()


def _owner_exec(outbox_db, sql, params=None, fetch=False):
    """One statement as the schema owner (bypasses RLS; owns every guard)."""
    conn = psycopg2.connect(outbox_db["owner_stream"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # channel_bindings is one of the five governance-audited tables:
            # `trg_governance_audit` refuses an actor-less mutation, in the
            # database, for every writer including this one.
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        conn.close()


def _worker_conn(outbox_db, *, autocommit=True):
    conn = psycopg2.connect(outbox_db["worker"])
    conn.autocommit = autocommit
    with conn.cursor() as cur:
        cur.execute("SET app.tenant_id = %s", (outbox_db["ws"],))
        cur.execute("SET app.actor_kind = 'system'")
    if not autocommit:
        conn.commit()
    return conn


_WINDOW_SLOT = itertools.count()


def _now() -> "datetime":
    """A clock in a window no other test shares.

    `tg_global` is keyed on `''` by design — one row for the whole fleet — so
    two pacing scenarios cannot be isolated by key the way `tg_chat` ones are.
    They are isolated by WINDOW instead: each call returns an instant days
    apart, so `window_start` truncates them into disjoint rows. This is why
    `deliver` takes `now` as a parameter rather than reading the clock.
    """
    return datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(
        days=next(_WINDOW_SLOT)
    )


def _tenant_session_factory(outbox_db, sessionmaker):
    """A zero-arg factory yielding a session that already carries the tenant.

    `async with obj` resolves `__aenter__` on the TYPE, so patching it on an
    instance is silently ignored — the first version of this did exactly that
    and the poller drained nothing while every assertion still ran. A real
    context manager is the only shape that works, and it is also the contract
    `OutboxPoller` documents.
    """
    from contextlib import asynccontextmanager

    from sqlalchemy import text as _t

    @asynccontextmanager
    async def _factory():
        async with sessionmaker() as session:
            await session.execute(
                _t("SELECT set_config('app.tenant_id', :v, false)"),
                {"v": outbox_db["ws"]},
            )
            yield session

    return _factory


def _new_binding(outbox_db) -> str:
    """A binding nobody else is using.

    The fixture is module-scoped (a role-carrying template cannot be held),
    so every scenario mints its own binding rather than sharing one — the
    same rule the L.2 gate applies to serialization keys. Without it a test
    claims whichever pending row is oldest across the whole module, which is
    a cross-test dependency wearing a passing assertion.
    """
    return str(
        _owner_exec(
            outbox_db,
            "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
            " VALUES (%s, 'telegram_group', %s) RETURNING id",
            (outbox_db["ws"], f"tg-{uuid.uuid4().hex[:10]}"),
            fetch=True,
        )[0][0]
    )


def _enqueue(outbox_db, *, kind="notification", intent_id=None, binding=None) -> str:
    """One `pending` row as the owner; returns its id."""
    return _owner_exec(
        outbox_db,
        "INSERT INTO channel_outbox"
        " (workspace_id, binding_id, kind, intent_id, payload)"
        " VALUES (%s, %s, %s, %s, '{\"v\": 1}') RETURNING id",
        (outbox_db["ws"], binding or outbox_db["binding"], kind, intent_id),
        fetch=True,
    )[0][0]


def _state(outbox_db, outbox_id):
    return _owner_exec(
        outbox_db,
        "SELECT state, attempts, external_message_ref FROM channel_outbox"
        " WHERE id = %s",
        (outbox_id,),
        fetch=True,
    )[0]


def _seed_sender_job(outbox_db, binding=None):
    """A `deliver_outbox` job keyed `tg:<binding_id>` — `02` §5's registry."""
    return _owner_exec(
        outbox_db,
        "INSERT INTO jobs (workspace_id, kind, lane, serialization_key, run_at,"
        " payload, max_attempts) VALUES (%s, 'deliver_outbox', 'interactive',"
        " %s, now(), '{\"v\": 1}', 3) RETURNING id",
        (outbox_db["ws"], f"tg:{binding or outbox_db['binding']}"),
        fetch=True,
    )[0][0]


class TestTheLevelIsReadCommitted:
    """Pinned as a fact. Every claim below holds at the level production runs,
    and #890 measured that most of these proofs keep passing at stricter
    levels — so without this they could silently stop describing production."""

    def test_worker_connections_run_read_committed(self, outbox_db):
        conn = _worker_conn(outbox_db)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW transaction_isolation")
                assert cur.fetchone()[0] == "read committed"
                cur.execute("SHOW default_transaction_isolation")
                assert cur.fetchone()[0] == "read committed"
        finally:
            conn.close()


class TestOneSenderPerBinding:
    """Mechanism 1, and the direct answer to #886/#887 for this table.

    `channel_outbox` needs no guard of its own for concurrent senders because
    the SENDER is serialized: `deliver_outbox` keys on `tg:<binding_id>` and
    `uq_jobs_serialized_lease` is a partial unique index, so two live leases on
    one binding are impossible for every writer including psql.
    """

    def _claim_sender(self, conn, binding):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, lease_token FROM fn_claim_job('interactive', %s,"
                " '60 seconds'::interval, 5)",
                (f"w-{uuid.uuid4().hex[:6]}",),
            )
            return cur.fetchone()

    def test_two_senders_for_one_binding_cannot_both_hold_a_lease(self, outbox_db):
        binding = _new_binding(outbox_db)
        _seed_sender_job(outbox_db, binding)
        _seed_sender_job(outbox_db, binding)

        c1 = _worker_conn(outbox_db, autocommit=False)
        c2 = _worker_conn(outbox_db, autocommit=False)
        loser: dict = {}

        def contender():
            try:
                loser["row"] = self._claim_sender(c2, binding)
                c2.commit()
            except Exception as exc:  # noqa: BLE001 — the assertion target
                loser["error"] = exc
                c2.rollback()

        try:
            winner = self._claim_sender(c1, binding)
            assert winner is not None, "positive control: the first sender leased"
            t = threading.Thread(target=contender)
            t.start()
            time.sleep(0.6)
            assert t.is_alive(), (
                "the second sender should be BLOCKED on the winner's"
                " uncommitted index entry — if it finished already the race"
                " was not exercised and this proves nothing"
            )
            c1.commit()
            t.join(timeout=10)
        finally:
            c1.close()
            c2.close()

        assert "row" not in loser, (
            f"the second sender was handed a lease: {loser.get('row')} — two"
            " senders on one chat is the #883 phantom-win shape"
        )
        err = loser["error"]
        assert isinstance(err, pg_errors.UniqueViolation), err
        assert err.diag.constraint_name == "uq_jobs_serialized_lease", (
            err.diag.constraint_name
        )
        _owner_exec(
            outbox_db,
            "UPDATE jobs SET state = 'cancelled' WHERE serialization_key = %s",
            (f"tg:{binding}",),
        )

    def test_the_unique_index_is_what_refuses_the_second_sender(self, outbox_db):
        """Drop the guard, watch the refusal disappear, restore it.

        The claimers must still run CONCURRENTLY with the index gone. Serially
        they would not both lease even without it, because `fn_claim_job`
        carries its own `NOT EXISTS (… state = 'leased')` — but that is a
        SNAPSHOT read, so two claimers that never see each other's uncommitted
        lease both pass it and the index is what refuses. Running them one
        after the other would prove the door's filter, not the guard, and the
        test would pass for the wrong reason.
        """
        binding = _new_binding(outbox_db)
        _seed_sender_job(outbox_db, binding)
        _seed_sender_job(outbox_db, binding)
        _owner_exec(outbox_db, "DROP INDEX uq_jobs_serialized_lease")
        try:
            c1 = _worker_conn(outbox_db, autocommit=False)
            c2 = _worker_conn(outbox_db, autocommit=False)
            second: dict = {}

            def contender():
                second["row"] = self._claim_sender(c2, binding)
                c2.commit()

            try:
                first = self._claim_sender(c1, binding)
                assert first is not None, "positive control: the first leased"
                t = threading.Thread(target=contender)
                t.start()
                t.join(timeout=10)
                assert not t.is_alive(), (
                    "with no index there is no uncommitted entry to block on,"
                    " so the second claimer must finish at once"
                )
                c1.commit()
            finally:
                c1.close()
                c2.close()

            assert second["row"] is not None, (
                "with the index gone BOTH senders must lease — that is what"
                " proves the index was load-bearing"
            )
            leased = _owner_exec(
                outbox_db,
                "SELECT count(*) FROM jobs WHERE serialization_key = %s"
                " AND state = 'leased'",
                (f"tg:{binding}",),
                fetch=True,
            )[0][0]
            assert leased == 2, "two live senders on one chat, guard removed"
        finally:
            _owner_exec(
                outbox_db,
                "UPDATE jobs SET state = 'cancelled' WHERE serialization_key = %s",
                (f"tg:{binding}",),
            )
            _owner_exec(outbox_db, UQ_LEASE_SQL)


class TestTheClaimIsSingleWinner:
    """Mechanism 2: `pending → sending` is a CAS, so the loser matches zero
    rows rather than being handed the same row twice."""

    def test_two_claimers_on_one_row_and_only_one_takes_it(self, outbox_db):
        binding = _new_binding(outbox_db)
        outbox_id = _enqueue(outbox_db, binding=binding)
        c1 = _worker_conn(outbox_db, autocommit=False)
        c2 = _worker_conn(outbox_db, autocommit=False)
        loser: dict = {}

        def contender():
            with c2.cursor() as cur:
                cur.execute(CLAIM_SQL, (binding,))
                loser["rowcount"] = cur.rowcount
                loser["row"] = cur.fetchone()
            c2.commit()

        try:
            with c1.cursor() as cur:
                cur.execute(CLAIM_SQL, (binding,))
                assert cur.rowcount == 1, "positive control: the winner claimed"
                assert str(cur.fetchone()[0]) == str(outbox_id)
            t = threading.Thread(target=contender)
            t.start()
            t.join(timeout=10)
            assert not t.is_alive(), "the contender never returned"
            c1.commit()
        finally:
            c1.close()
            c2.close()

        # SKIP LOCKED means the loser does not block — it finds no unlocked
        # pending row and matches nothing. Zero rows here is an empty
        # candidate set, which is why the CAS test below is the one that
        # proves the state predicate rather than this one.
        assert loser["rowcount"] == 0 and loser["row"] is None
        assert _state(outbox_db, outbox_id)[0] == "sending"
        assert _state(outbox_db, outbox_id)[1] == 1, "claimed exactly once"

    def test_the_state_predicate_is_what_makes_the_claim_a_CAS(self, outbox_db):
        """The guard, isolated: a row already `sending` must not re-claim.

        Driven directly rather than through the race, because SKIP LOCKED
        hides the predicate — the interesting case is a claimer whose
        candidate row moved underneath it, which is #883's exact window.
        """
        outbox_id = _enqueue(outbox_db)
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'sending' WHERE id = %s",
            (outbox_id,),
        )
        conn = _worker_conn(outbox_db)
        try:
            with conn.cursor() as cur:
                # Positive control: the row exists and is visible here.
                cur.execute(
                    "SELECT count(*) FROM channel_outbox WHERE id = %s", (outbox_id,)
                )
                assert cur.fetchone()[0] == 1
                cur.execute(
                    "UPDATE channel_outbox SET state = 'sending'"
                    " WHERE id = %s AND state = 'pending'",
                    (outbox_id,),
                )
                assert cur.rowcount == 0, (
                    "a row already 'sending' was claimed again — the CAS did"
                    " not fence, and two senders would hold one row"
                )
                # And with the clause gone the same statement takes it.
                cur.execute(
                    "UPDATE channel_outbox SET state = 'sending' WHERE id = %s",
                    (outbox_id,),
                )
                assert cur.rowcount == 1, (
                    "without the state predicate the write lands — the clause"
                    " is the guard"
                )
        finally:
            conn.close()


class TestStoppedSenderStrandsNothing:
    """The gate's first half, driven through the service."""

    def _engine(self, outbox_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            outbox_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=2,
            max_overflow=0,
        )

    async def _tenant(self, conn, outbox_db):
        from sqlalchemy import text as _t

        await conn.execute(
            _t("SELECT set_config('app.tenant_id', :v, false)"),
            {"v": outbox_db["ws"]},
        )
        await conn.execute(_t("SELECT set_config('app.actor_kind', 'system', false)"))

    @pytest.mark.asyncio
    async def test_a_row_left_sending_by_a_dead_sender_is_recovered(self, outbox_db):
        from src.services.target.outbox import recover_stranded, resolve_ambiguous

        binding = _new_binding(outbox_db)
        outbox_id = _enqueue(outbox_db, kind="notification", binding=binding)
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'sending', attempts = 1 WHERE id = %s",
            (outbox_id,),
        )
        assert _state(outbox_db, outbox_id)[0] == "sending", "positive control"

        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                stranded = await recover_stranded(conn, binding_id=binding)
                assert str(outbox_id) in stranded, (
                    "the stranded row was not recovered — a stopped sender"
                    " left work nobody will ever pick up"
                )
                for oid in stranded:
                    await resolve_ambiguous(conn, outbox_id=oid)
                await conn.commit()
        finally:
            await engine.dispose()

        state, attempts, _ = _state(outbox_db, outbox_id)
        assert state == "pending", (
            "a notification stranded on its FIRST attempt gets the one retry"
            " the policy allows"
        )
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_recovery_never_blind_retries_it_goes_through_ambiguous(
        self, outbox_db
    ):
        """R8: Telegram has no read-back for a lost response, so a stranded
        row must not go straight back to `pending` — whether the send left is
        exactly what nobody knows."""
        from src.services.target.outbox import recover_stranded

        binding = _new_binding(outbox_db)
        outbox_id = _enqueue(outbox_db, kind="notification", binding=binding)
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'sending' WHERE id = %s",
            (outbox_id,),
        )
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                await recover_stranded(conn, binding_id=binding)
                await conn.commit()
        finally:
            await engine.dispose()
        assert _state(outbox_db, outbox_id)[0] == "ambiguous"


class TestTheLostAckPolicyIsBoundedPerKind:
    """The gate's second half: ≤1 extra notification, prompts converge."""

    def _engine(self, outbox_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            outbox_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=2,
            max_overflow=0,
        )

    async def _tenant(self, conn, outbox_db):
        from sqlalchemy import text as _t

        await conn.execute(
            _t("SELECT set_config('app.tenant_id', :v, false)"),
            {"v": outbox_db["ws"]},
        )
        await conn.execute(_t("SELECT set_config('app.actor_kind', 'system', false)"))

    @pytest.mark.asyncio
    async def test_a_notification_retries_exactly_once_then_fails(self, outbox_db):
        """The bound the gate names: at most ONE extra notification."""
        from src.services.target.outbox import resolve_ambiguous

        outbox_id = _enqueue(outbox_db, kind="notification")
        engine = self._engine(outbox_db)
        try:
            # First ambiguity, on attempt 1: the policy allows the retry.
            _owner_exec(
                outbox_db,
                "UPDATE channel_outbox SET state = 'ambiguous', attempts = 1"
                " WHERE id = %s",
                (outbox_id,),
            )
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                first = await resolve_ambiguous(conn, outbox_id=outbox_id)
                await conn.commit()
            assert first == "pending", "the one allowed retry"

            # Second ambiguity, on attempt 2: the retry is spent.
            _owner_exec(
                outbox_db,
                "UPDATE channel_outbox SET state = 'ambiguous', attempts = 2"
                " WHERE id = %s",
                (outbox_id,),
            )
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                second = await resolve_ambiguous(conn, outbox_id=outbox_id)
                await conn.commit()
            assert second == "failed", (
                "a second retry would make TWO extra notifications — the"
                " policy bounds the duplicate at one"
            )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_an_approval_prompt_resends_rather_than_failing(self, outbox_db):
        """Two live cards are tolerable; a silently dropped prompt is not."""
        from src.services.target.outbox import resolve_ambiguous

        outbox_id = _enqueue(outbox_db, kind="approval_prompt")
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'ambiguous', attempts = 5 WHERE id = %s",
            (outbox_id,),
        )
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                got = await resolve_ambiguous(conn, outbox_id=outbox_id)
                await conn.commit()
        finally:
            await engine.dispose()
        assert got == "pending", (
            "a prompt must resend at ANY attempt count — the retry-once bound"
            " is the notification rule and must not leak onto prompts"
        )

    @pytest.mark.asyncio
    async def test_supersede_all_targets_every_known_ref_and_converges(self, outbox_db):
        """Prompts converge on supersede: every live card is retired and a
        `prompt_supersede` row is queued per KNOWN ref; a card whose ref was
        lost is still retired and simply ages out (R6)."""
        from src.services.target.outbox import supersede_all

        binding = _new_binding(outbox_db)
        intent = str(uuid.uuid4())
        sent = _enqueue(
            outbox_db, kind="approval_prompt", intent_id=intent, binding=binding
        )
        lost = _enqueue(
            outbox_db, kind="approval_prompt", intent_id=intent, binding=binding
        )
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'sent', external_message_ref = 'ref-1'"
            " WHERE id = %s",
            (sent,),
        )
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'ambiguous' WHERE id = %s",
            (lost,),
        )

        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                n = await supersede_all(
                    conn,
                    workspace_id=outbox_db["ws"],
                    binding_id=binding,
                    intent_id=intent,
                )
                await conn.commit()
        finally:
            await engine.dispose()

        assert n == 2, "both live cards must be retired, ref known or not"
        assert _state(outbox_db, sent)[0] == "superseded"
        assert _state(outbox_db, lost)[0] == "superseded"

        supersedes = _owner_exec(
            outbox_db,
            "SELECT payload FROM channel_outbox WHERE intent_id = %s"
            " AND kind = 'prompt_supersede'",
            (intent,),
            fetch=True,
        )
        assert len(supersedes) == 1, (
            "one supersede row per KNOWN ref — the lost-ref card is retired"
            " but has nothing to target"
        )
        assert supersedes[0][0]["supersedes_ref"] == "ref-1"

        # Convergence: a second supersede finds nothing live and adds nothing.
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                again = await supersede_all(
                    conn,
                    workspace_id=outbox_db["ws"],
                    binding_id=binding,
                    intent_id=intent,
                )
                await conn.commit()
        finally:
            await engine.dispose()
        assert again == 0, "supersede-all must converge, not fan out"


class TestPacingDefersRatherThanFails:
    """`rate_counters` consumption, both `02` §6 scopes. Over budget leaves the
    row `pending` — H5 slip-a-slot, not a lost message.

    The budget is spent by a REAL first send rather than by seeding the counter
    or passing a limit of zero. Measured while writing this: the shipped
    increment-and-check idiom does not deny at ``limit=0``, because its
    ``WHERE rc.count < :limit`` guards only the ON CONFLICT branch and the
    first hit of a window is an INSERT. Every `05` limit is >= 1 so nothing
    depends on it, but a test that spent the budget that way would have been
    asserting an artifact of the fixture instead of the guard.
    """

    def _engine(self, outbox_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            outbox_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=2,
            max_overflow=0,
        )

    async def _tenant(self, conn, outbox_db):
        from sqlalchemy import text as _t

        await conn.execute(
            _t("SELECT set_config('app.tenant_id', :v, false)"),
            {"v": outbox_db["ws"]},
        )

    @pytest.mark.asyncio
    async def test_a_send_lands_then_the_next_defers_and_stays_pending(self, outbox_db):
        from src.services.target.outbox import OutboxPaced, deliver

        binding = _new_binding(outbox_db)
        first_id = _enqueue(outbox_db, kind="notification", binding=binding)
        second_id = _enqueue(outbox_db, kind="notification", binding=binding)
        seen: list = []

        async def transport(row):
            seen.append(row["id"])
            return "tg-msg-77"

        now = _now()
        engine = self._engine(outbox_db)
        try:
            # One message of chat budget: the first send spends it.
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                result = await deliver(
                    conn,
                    binding_id=binding,
                    transport=transport,
                    now=now,
                    chat_limit=1,
                    chat_window_seconds=CHAT_WINDOW_S,
                    global_limit=GLOBAL_LIMIT,
                    global_window_seconds=GLOBAL_WINDOW_S,
                )
                await conn.commit()
            assert result is not None and result["state"] == "sent"

            # The second, in the SAME window, is deferred rather than failed.
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                with pytest.raises(OutboxPaced):
                    await deliver(
                        conn,
                        binding_id=binding,
                        transport=transport,
                        now=now,
                        chat_limit=1,
                        chat_window_seconds=CHAT_WINDOW_S,
                        global_limit=GLOBAL_LIMIT,
                        global_window_seconds=GLOBAL_WINDOW_S,
                    )
                await conn.rollback()
        finally:
            await engine.dispose()

        assert seen == [str(first_id)], (
            "the transport must have been reached exactly once — a paced send"
            " that still called out is a message sent off-budget"
        )
        state, attempts, ref = _state(outbox_db, first_id)
        assert (state, attempts, ref) == ("sent", 1, "tg-msg-77")
        assert _state(outbox_db, second_id) == ("pending", 0, None), (
            "a paced send must leave its row untouched for the next poll —"
            " parking it in 'sending' behind a budget is how work strands"
        )

    @pytest.mark.asyncio
    async def test_the_global_budget_defers_independently_of_the_chat_one(
        self, outbox_db
    ):
        """Both scopes are consumed, so either can defer. A chat well inside
        its own budget must still yield to the fleet-wide one."""
        from src.services.target.outbox import OutboxPaced, deliver

        binding = _new_binding(outbox_db)
        outbox_id = _enqueue(outbox_db, kind="notification", binding=binding)
        now = _now()

        # Spend the GLOBAL window on another binding entirely.
        other = _new_binding(outbox_db)
        _enqueue(outbox_db, kind="notification", binding=other)

        async def transport(row):
            return "tg-msg-1"

        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                await deliver(
                    conn,
                    binding_id=other,
                    transport=transport,
                    now=now,
                    chat_limit=CHAT_LIMIT,
                    chat_window_seconds=CHAT_WINDOW_S,
                    global_limit=1,
                    global_window_seconds=GLOBAL_WINDOW_S,
                )
                await conn.commit()

            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                with pytest.raises(OutboxPaced):
                    await deliver(
                        conn,
                        binding_id=binding,
                        transport=transport,
                        now=now,
                        chat_limit=CHAT_LIMIT,  # this chat has spent nothing
                        chat_window_seconds=CHAT_WINDOW_S,
                        global_limit=1,
                        global_window_seconds=GLOBAL_WINDOW_S,
                    )
                await conn.rollback()
        finally:
            await engine.dispose()

        assert _state(outbox_db, outbox_id) == ("pending", 0, None)


class TestAStaleSenderCannotOverwriteALiveOne:
    """The co-location this module leans on, proven rather than asserted.

    `deliver` never commits: the outbox write and `finalize_job` commit
    together, so a stale owner's fenced finalization takes its outbox write
    with it. That is a SERVICE discipline, which is exactly the shape #883
    warns about — so it is exercised, not documented.
    """

    def _engine(self, outbox_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            outbox_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=3,
            max_overflow=0,
        )

    @pytest.mark.asyncio
    async def test_marking_sent_twice_is_fenced_and_does_not_overwrite(self, outbox_db):
        """Fact 2, isolated — and the test the mutation battery said was
        missing. Removing `AND state = 'sending'` from the write CAS left the
        whole gate green, because every other case reaches `mark_sent` with
        the row genuinely `sending`. A fence nothing exercises is a claim, not
        a guard.
        """
        from sqlalchemy import text as _t

        from src.services.target.outbox import OutboxFenced, mark_sent

        binding = _new_binding(outbox_db)
        outbox_id = _enqueue(outbox_db, kind="notification", binding=binding)
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'sending' WHERE id = %s",
            (outbox_id,),
        )
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    _t("SELECT set_config('app.tenant_id', :v, false)"),
                    {"v": outbox_db["ws"]},
                )
                await mark_sent(
                    conn, outbox_id=outbox_id, external_message_ref="ref-first"
                )
                # Positive control: the row exists and is `sent`, so the zero
                # rows below are a predicate miss rather than an empty set.
                got = (
                    await conn.execute(
                        _t(
                            "SELECT state FROM channel_outbox WHERE id ="
                            " CAST(:i AS uuid)"
                        ),
                        {"i": outbox_id},
                    )
                ).first()
                assert got[0] == "sent"
                with pytest.raises(OutboxFenced):
                    await mark_sent(
                        conn, outbox_id=outbox_id, external_message_ref="ref-second"
                    )
                await conn.commit()
        finally:
            await engine.dispose()
        assert _state(outbox_db, outbox_id)[2] == "ref-first", (
            "the second write overwrote the recorded ref — the outbox would"
            " name a message it did not send"
        )

    @pytest.mark.asyncio
    async def test_a_sender_whose_card_was_superseded_mid_send_is_fenced(
        self, outbox_db
    ):
        """The realistic instance of fact 2: `supersede_all` retires a card
        while its sender is mid-flight. The sender must not resurrect it."""
        from sqlalchemy import text as _t

        from src.services.target.outbox import OutboxFenced, mark_sent

        binding = _new_binding(outbox_db)
        intent = str(uuid.uuid4())
        outbox_id = _enqueue(
            outbox_db, kind="approval_prompt", intent_id=intent, binding=binding
        )
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'sending' WHERE id = %s",
            (outbox_id,),
        )
        # The intent moved on; supersede-all retired the card underneath us.
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'superseded' WHERE id = %s",
            (outbox_id,),
        )
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    _t("SELECT set_config('app.tenant_id', :v, false)"),
                    {"v": outbox_db["ws"]},
                )
                with pytest.raises(OutboxFenced):
                    await mark_sent(
                        conn, outbox_id=outbox_id, external_message_ref="late-ref"
                    )
                await conn.rollback()
        finally:
            await engine.dispose()
        state, _, ref = _state(outbox_db, outbox_id)
        assert (state, ref) == ("superseded", None), (
            "a superseded card was marked sent — the outbox would hold a live"
            " card the supersede pass believed it had retired"
        )

    @pytest.mark.asyncio
    async def test_a_fenced_finalize_rolls_back_the_outbox_write_with_it(
        self, outbox_db
    ):
        from sqlalchemy import text as _t

        from src.services.target.jobs import JobFenced, finalize_job
        from src.services.target.outbox import mark_sent

        binding = str(uuid.uuid4())
        # A second binding so this scenario cannot disturb the shared one.
        _owner_exec(
            outbox_db,
            "INSERT INTO channel_bindings (workspace_id, id, channel, external_ref)"
            " VALUES (%s, %s, 'telegram_group', %s)",
            (outbox_db["ws"], binding, f"tg-{uuid.uuid4().hex[:8]}"),
        )
        outbox_id = _enqueue(outbox_db, kind="notification", binding=binding)
        job_id = _seed_sender_job(outbox_db, binding)
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'sending' WHERE id = %s",
            (outbox_id,),
        )
        # The job is leased by SOMEONE ELSE — our sender's token is stale.
        _owner_exec(
            outbox_db,
            "UPDATE jobs SET state = 'leased', lease_token = gen_random_uuid(),"
            " locked_until = now() + interval '60 seconds' WHERE id = %s",
            (job_id,),
        )
        stale_token = str(uuid.uuid4())

        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    _t("SELECT set_config('app.tenant_id', :v, false)"),
                    {"v": outbox_db["ws"]},
                )
                # The stale sender writes the outbox and then finalizes.
                await mark_sent(
                    conn, outbox_id=outbox_id, external_message_ref="ghost-ref"
                )
                with pytest.raises(JobFenced):
                    await finalize_job(conn, str(job_id), stale_token, "succeeded")
                await conn.rollback()
        finally:
            await engine.dispose()

        state, _, ref = _state(outbox_db, outbox_id)
        assert (state, ref) == ("sending", None), (
            "the stale sender's 'sent' survived its fenced finalization — the"
            " outbox would record a send the live owner never made, which is"
            " #883 one table over"
        )


class TestThePollerReplacesTheRedisWakeUp:
    """`05` cadence, run for real. TT:P0-07's Redis half is struck, so this
    loop IS the wake-up mechanism and its liveness is a gate concern."""

    def _factory(self, outbox_db):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(
            outbox_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=2,
            max_overflow=0,
        )
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    @pytest.mark.asyncio
    async def test_the_loop_drains_a_queue_on_its_own_timer(self, outbox_db):
        """Three rows, a short interval, no manual ticks: the loop must find
        and send them itself. A poller that only works when driven by hand is
        not a replacement for a wake-up channel."""
        import asyncio

        from src.services.target.outbox import OutboxPoller

        binding = _new_binding(outbox_db)
        ids = [
            _enqueue(outbox_db, kind="notification", binding=binding) for _ in range(3)
        ]
        sent_refs: list = []

        async def transport(row):
            sent_refs.append(row["id"])
            return f"tg-{len(sent_refs)}"

        engine, factory = self._factory(outbox_db)

        poller = OutboxPoller(
            _tenant_session_factory(outbox_db, factory),
            binding_id=binding,
            transport=transport,
            clock=_now,
            interval_seconds=0.05,  # `05` says 2 s; the gate compresses it
            chat_limit=CHAT_LIMIT,
            chat_window_seconds=CHAT_WINDOW_S,
            global_limit=GLOBAL_LIMIT,
            global_window_seconds=GLOBAL_WINDOW_S,
        )
        try:
            await poller.start()
            deadline = time.monotonic() + 15
            while len(sent_refs) < 3 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            drained_at = poller.ticks
            # Keep it running on an EMPTY queue: a wake-up replacement must
            # poll, not merely react to work that was already there when it
            # started. Three ticks for three rows would satisfy a loop that
            # ran once per row and then stopped.
            await asyncio.sleep(0.4)
            kept_beating = poller.ticks
            await poller.stop()
        finally:
            await engine.dispose()

        assert kept_beating > drained_at, (
            f"the loop stopped beating once the queue emptied"
            f" ({drained_at} → {kept_beating}) — it is reacting, not polling"
        )
        assert sorted(sent_refs) == sorted(str(i) for i in ids), (
            f"the queue did not drain: {sent_refs}"
        )
        assert poller.sent == 3 and poller.consecutive_failures == 0
        for outbox_id in ids:
            assert _state(outbox_db, outbox_id)[0] == "sent"

    @pytest.mark.asyncio
    async def test_a_paced_tick_is_health_not_failure(self, outbox_db):
        """`consecutive_failures` is what a liveness check reads, so pacing
        must not move it — a chat at its budget is working correctly, and a
        supervisor that restarted the sender for it would be the defect."""
        from src.services.target.outbox import OutboxPoller

        binding = _new_binding(outbox_db)
        _enqueue(outbox_db, kind="notification", binding=binding)
        _enqueue(outbox_db, kind="notification", binding=binding)

        async def transport(row):
            return "tg-ok"

        engine, factory = self._factory(outbox_db)
        fixed = _now()
        poller = OutboxPoller(
            _tenant_session_factory(outbox_db, factory),
            binding_id=binding,
            transport=transport,
            clock=lambda: fixed,  # one window for both ticks
            interval_seconds=0.05,
            chat_limit=1,
            chat_window_seconds=CHAT_WINDOW_S,
            global_limit=GLOBAL_LIMIT,
            global_window_seconds=GLOBAL_WINDOW_S,
        )
        try:
            first = await poller.tick()
            second = await poller.tick()
        finally:
            await engine.dispose()

        assert first is not None and first["state"] == "sent"
        assert second is None
        assert poller.deferred == 1 and poller.sent == 1
        assert poller.consecutive_failures == 0, (
            "a paced tick moved the failure counter — a chat at its budget"
            " would read as a dying sender"
        )
