"""Gate — channel outbox + Telegram sender.

*"Stopped-sender and lost-ack injections strand nothing; duplicate sends
bounded exactly as the policy states (≤1 extra notification; prompts converge
on supersede)."*

THE HAZARD THE OUTBOX AND SENDER CLOSE. `channel_outbox` carries no unique index
and no lease token, so the argument that makes `jobs` safe does NOT
transfer: `jobs` has both and this table has neither. An outbox exists to
make a send happen exactly once, so a failure — two writers both told they
won — reads here as the machine believing it sent something it did not, or sending
twice while believing it sent once.

So the mechanism is proven, not argued. Two facts, each tested against the
real database as the real `svc_worker` login:

1. `uq_jobs_serialized_lease` on `tg:<binding_id>` admits ONE live sender per
   chat, writer-independently — `TestOneSenderPerBinding`.
2. Every outbox write is a CAS on the state it leaves, so a loser matches zero
   rows rather than being handed a phantom win — `TestTheClaimIsSingleWinner`,
   `TestAStaleSenderCannotOverwriteALiveOne`.

Concurrency rules, all exercised by the tests:

* Every race runs on REAL concurrent connections at **READ COMMITTED**,
  asserted in its own test — a race test can pass at REPEATABLE READ while
  production races at READ COMMITTED, so a concurrency test at the wrong
  level proves nothing about production.
* No refusal is inferred from a bare rowcount alone. Where rowcount IS the
  discriminator, the docstring says why it can be: a predicate miss on a
  column the winner changed, rather than a self-transition no-op.
* Every refusal carries a positive control — the row being refused exists and
  is visible to the refusing connection.
* Every guard is proven load-bearing by removing it and watching the refusal
  disappear.
"""

from __future__ import annotations

import contextlib
import itertools
import json
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
    async_url,
    build_world,
    in_tenant,
    seed_intent,
    seed_workspace_chain,
    sweep_as_worker,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: 20 msgs/min/group, 30/s global. Parameters, never hardcoded in src.
CHAT_LIMIT, CHAT_WINDOW_S = 20, 60
GLOBAL_LIMIT, GLOBAL_WINDOW_S = 30, 1

#: The claim CAS, as a template so the unguarded variant is the SAME statement
#: minus its clause rather than string surgery on it (the idiom).
_CLAIM_TEMPLATE = (
    "UPDATE channel_outbox SET state = 'sending', attempts = attempts + 1"
    " WHERE id = (SELECT id FROM channel_outbox"
    "             WHERE binding_id = %s AND state = 'pending'"
    "             ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED){guard}"
    " RETURNING id"
)
CLAIM_SQL = _CLAIM_TEMPLATE.format(guard=" AND state = 'pending'")
UNGUARDED_CLAIM_SQL = _CLAIM_TEMPLATE.format(guard="")

#: `uq_jobs_serialized_lease`, verbatim from `machinery.py` — re-added after its drop.
UQ_LEASE_SQL = (
    "CREATE UNIQUE INDEX uq_jobs_serialized_lease ON jobs (serialization_key)"
    " WHERE state = 'leased'"
)


@pytest.fixture(scope="module")
def outbox_db(admin_conn, owner_actor):
    """Replayed schema + one workspace + one telegram binding, once.

    The binding is seeded HERE and not taken from `seed_workspace_chain`,
    which creates no `channel_bindings` row — the exact vacuity where a refusal
    test matches zero rows and passes having refused nothing. Every test below
    asserts a rowcount, so a missing binding would
    report as "matched 0 rows" rather than hiding.
    """
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = build_world(db, owner_actor, admin_conn)
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
            "ingress": as_user(db, "svc_ingress"),
            "owner_stream": dsn,
            "ws": str(chain["ws"]),
            "ws_owner": str(chain["user"]),
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
    instance is silently ignored: the poller drains nothing while every
    assertion runs. A real context manager is the only shape that works, and it
    is also the contract `OutboxPoller` documents.
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


def _new_binding(outbox_db, external_ref=None, *, channel="telegram_group") -> str:
    """A binding nobody else is using — on *external_ref* when the scenario
    needs a real chat id. A `telegram_dm` one is seeded here because no code
    path writes one: the writer refuses a private chat unless its caller asks
    for one, and the bind door, its one caller, never asks.

    The fixture is module-scoped (a role-carrying template cannot be held),
    so every scenario mints its own binding rather than sharing one — the
    same rule `test_jobs_lease_gate.py` applies to serialization keys. Without
    it a test claims whichever pending row is oldest across the whole module,
    which is a cross-test dependency wearing a passing assertion.
    """
    return str(
        _owner_exec(
            outbox_db,
            "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
            " VALUES (%s, %s, %s) RETURNING id",
            (
                outbox_db["ws"],
                channel,
                external_ref or f"tg-{uuid.uuid4().hex[:10]}",
            ),
            fetch=True,
        )[0][0]
    )


def _enqueue(
    outbox_db,
    *,
    kind="notification",
    intent_id=None,
    binding=None,
    payload=None,
    attempts=0,
) -> str:
    """One `pending` row as the owner; returns its id."""
    return _owner_exec(
        outbox_db,
        "INSERT INTO channel_outbox"
        " (workspace_id, binding_id, kind, intent_id, payload, attempts)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            outbox_db["ws"],
            binding or outbox_db["binding"],
            kind,
            intent_id,
            json.dumps(payload if payload is not None else {"v": 1}),
            attempts,
        ),
        fetch=True,
    )[0][0]


def _engine(outbox_db, *, pool_size=2, login="worker"):
    """An async engine on the scratch database as *login*: the worker, or
    `owner_stream` (the schema owner, which bypasses RLS)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(
        async_url(outbox_db[login]),
        pool_size=pool_size,
        max_overflow=0,
    )


async def _tenant(conn, outbox_db):
    """The worker's tenant and actor GUCs on one connection."""
    from sqlalchemy import text as _t

    await conn.execute(
        _t("SELECT set_config('app.tenant_id', :v, false)"), {"v": outbox_db["ws"]}
    )
    await conn.execute(_t("SELECT set_config('app.actor_kind', 'system', false)"))


def _state(outbox_db, outbox_id):
    return _owner_exec(
        outbox_db,
        "SELECT state, attempts, external_message_ref FROM channel_outbox"
        " WHERE id = %s",
        (outbox_id,),
        fetch=True,
    )[0]


def _backdate(outbox_db, outbox_id, seconds):
    """Age a row's `updated_at` by *seconds*. The touch trigger stamps it on
    every UPDATE, so it is held off for the one statement."""
    _owner_exec(
        outbox_db,
        "ALTER TABLE channel_outbox DISABLE TRIGGER tg_touch_channel_outbox",
    )
    try:
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET updated_at = now() - make_interval(secs => %s)"
            " WHERE id = %s",
            (seconds, outbox_id),
        )
    finally:
        _owner_exec(
            outbox_db,
            "ALTER TABLE channel_outbox ENABLE TRIGGER tg_touch_channel_outbox",
        )


def _seed_sender_job(outbox_db, binding=None):
    """A `deliver_outbox` job keyed `tg:<binding_id>`."""
    return _owner_exec(
        outbox_db,
        "INSERT INTO jobs (workspace_id, kind, lane, serialization_key, run_at,"
        " payload, max_attempts) VALUES (%s, 'deliver_outbox', 'interactive',"
        " %s, now(), '{\"v\": 1}', 3) RETURNING id",
        (outbox_db["ws"], f"tg:{binding or outbox_db['binding']}"),
        fetch=True,
    )[0][0]


def _dm_chat() -> str:
    """A private chat's id: a positive Telegram user id nobody else holds.
    For a private chat the chat id IS the person's Telegram user id, which is
    what `bindings.deliverable_binding_where` joins on."""
    return str(10**9 + uuid.uuid4().int % 10**9)


def _join(outbox_db, ws, user):
    _owner_exec(
        outbox_db,
        "INSERT INTO workspace_members (workspace_id, user_id, role)"
        " VALUES (%s, %s, 'member')",
        (ws, user),
    )


def _link(outbox_db, user, chat):
    _owner_exec(
        outbox_db,
        "INSERT INTO user_identities (user_id, provider, external_id)"
        " VALUES (%s, 'telegram', %s)",
        (user, chat),
    )


def _person(outbox_db, *, chat=None, member_of=()) -> str:
    """A user, linked to Telegram as *chat* when given, a member of each
    workspace in *member_of*."""
    user = str(
        _owner_exec(
            outbox_db, "INSERT INTO users DEFAULT VALUES RETURNING id", fetch=True
        )[0][0]
    )
    if chat is not None:
        _link(outbox_db, user, chat)
    for ws in member_of:
        _join(outbox_db, ws, user)
    return user


def _other_workspace(outbox_db) -> str:
    """A second workspace with its own owner."""
    conn = psycopg2.connect(outbox_db["owner_stream"])
    try:
        return str(seed_workspace_chain(conn, f"l4-other-{uuid.uuid4().hex[:8]}")["ws"])
    finally:
        conn.close()


async def _claim(engine, outbox_db, binding):
    """`outbox.claim_next` on one connection of *engine*, with the
    workspace's tenant set."""
    from src.services.target.outbox import claim_next

    async with engine.connect() as conn:
        await _tenant(conn, outbox_db)
        row = await claim_next(conn, binding_id=binding)
        await conn.commit()
    return row


async def _run_worker(outbox_db, transport, done, *, after_sweeps=0):
    """The real worker's interactive lanes and sender sweep over *transport*,
    run until *done()* (a blocking read, run in a thread) holds and then for
    *after_sweeps* more completed sweeps; 30 s at most. The module's other
    scenarios leave rows and jobs of their own behind, and the lanes and the
    sweep serve those too, so a caller asserts only on its own rows."""
    import asyncio

    from src.services.target.work_loop import WorkerConfig as _Cfg
    from src.worker import SenderSweeper, compose

    engine = _engine(outbox_db, pool_size=10)
    stop = asyncio.Event()
    try:
        cfg = _Cfg(
            lane_concurrency={"interactive": 2, "bulk": 1},
            poller_interval_seconds=0.1,
            sender_hold_seconds=1.0,
            claim_idle_seconds=0.05,
            sender_sweep_seconds=0.2,
            chat_limit=CHAT_LIMIT,
            chat_window_seconds=CHAT_WINDOW_S,
            global_limit=GLOBAL_LIMIT,
            global_window_seconds=GLOBAL_WINDOW_S,
        )
        app = compose(engine=engine, config=cfg, env={}, transport=transport)
        loops = [wl for wl in app.loops if wl.lane == "interactive"]
        sweeper = SenderSweeper(app)
        tasks = [asyncio.create_task(wl.run()) for wl in loops]
        tasks.append(asyncio.create_task(sweeper.run(stop)))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not await asyncio.to_thread(done):
            await asyncio.sleep(0.1)
        target = sweeper.sweeps + after_sweeps
        while time.monotonic() < deadline and sweeper.sweeps < target:
            await asyncio.sleep(0.05)
        stop.set()
        for wl in loops:
            wl.stop()
        await asyncio.gather(*tasks)
    finally:
        await engine.dispose()


class TestTheLevelIsReadCommitted:
    """Pinned as a fact. Every claim below holds at the level production runs,
    and most of these proofs keep passing at stricter levels — so without
    this they could silently stop describing production."""

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
    """Mechanism 1, for a table with no unique index or lease token.

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
        candidate row changes between selection and the state update.
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


class TestARevokedBindingIsNeverClaimed:
    """`outbox.claim_next` takes a row only while its binding is active, so a
    sender job minted before an admin removed the group or the bot
    was kicked sends nothing into that chat. Driven through the service as
    `svc_worker`, with the positive control first: the same row IS claimed
    once the binding is active again."""

    @pytest.mark.asyncio
    async def test_a_pending_row_on_a_revoked_binding_stays_pending(self, outbox_db):
        from src.services.target.outbox import claim_next

        binding = _new_binding(outbox_db)
        outbox_id = _enqueue(outbox_db, binding=binding)
        _owner_exec(
            outbox_db,
            "UPDATE channel_bindings SET state = 'revoked' WHERE id = %s",
            (binding,),
        )
        engine = _engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await _tenant(conn, outbox_db)
                assert await claim_next(conn, binding_id=binding) is None
                await conn.commit()
            assert _state(outbox_db, outbox_id)[:2] == ("pending", 0), (
                "a revoked binding's row was claimed — its sender would post"
                " into a group the workspace removed"
            )

            _owner_exec(
                outbox_db,
                "UPDATE channel_bindings SET state = 'active' WHERE id = %s",
                (binding,),
            )
            async with engine.connect() as conn:
                await _tenant(conn, outbox_db)
                row = await claim_next(conn, binding_id=binding)
                await conn.commit()
        finally:
            await engine.dispose()
        assert row is not None and row["id"] == str(outbox_id), (
            "positive control: the active binding's row is claimed"
        )
        assert _state(outbox_db, outbox_id)[0] == "sending"

    @pytest.mark.asyncio
    async def test_a_revoked_bindings_leftover_queue_is_retired(self, outbox_db):
        """What `deliver_outbox` runs on finding its binding revoked: a row a
        429 put back to `pending`, one a dead sender left `sending` and one
        `ambiguous` are all superseded, so a re-bind posts no stale card. A
        `sent` row and another binding's row are untouched."""
        from src.services.target.bindings import retire_unsettled

        binding = _new_binding(outbox_db)
        other = _new_binding(outbox_db)
        rows = {
            state: _enqueue(outbox_db, binding=binding)
            for state in ("pending", "sending", "ambiguous", "sent")
        }
        for state in ("sending", "ambiguous", "sent"):
            _owner_exec(
                outbox_db,
                "UPDATE channel_outbox SET state = %s,"
                " external_message_ref = CASE WHEN %s = 'sent' THEN '1' END"
                " WHERE id = %s",
                (state, state, rows[state]),
            )
        bystander = _enqueue(outbox_db, binding=other)
        _owner_exec(
            outbox_db,
            "UPDATE channel_bindings SET state = 'revoked' WHERE id = %s",
            (binding,),
        )
        engine = _engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await _tenant(conn, outbox_db)
                moved = await retire_unsettled(conn, binding_id=binding)
                await conn.commit()
        finally:
            await engine.dispose()
        assert moved == 3
        for state in ("pending", "sending", "ambiguous"):
            assert _state(outbox_db, rows[state])[0] == "superseded", state
        assert _state(outbox_db, rows["sent"])[0] == "sent"
        assert _state(outbox_db, bystander)[0] == "pending", (
            "another binding's queue was touched"
        )


def _sent_card(outbox_db, binding, intent) -> str:
    """An `approval_prompt` already sent: a live card with a message to edit."""
    return _owner_exec(
        outbox_db,
        "INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id,"
        " payload, state, external_message_ref)"
        " VALUES (%s, %s, 'approval_prompt', %s, %s, 'sent', %s) RETURNING id",
        (
            outbox_db["ws"],
            binding,
            intent,
            json.dumps({"v": 1, "text": "card"}),
            f"m-{uuid.uuid4().hex[:8]}",
        ),
        fetch=True,
    )[0][0]


class TestAPrivateChatIsClaimedOnlyForItsMember:
    """A card goes to a private chat only while the person whose chat it is
    belongs to the binding's workspace (`bindings.deliverable_binding_where`).
    Every refusal has its positive control: the same row IS claimed once the
    person belongs. As `svc_worker` and as the schema owner: the owner
    bypasses RLS, so its refusal proves the predicate's own workspace
    correlation is the guard, not the tenant policy."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("login", ["worker", "owner_stream"])
    async def test_belonging_to_another_workspace_is_not_belonging_here(
        self, outbox_db, login
    ):
        chat = _dm_chat()
        binding = _new_binding(outbox_db, chat, channel="telegram_dm")
        person = _person(outbox_db, chat=chat, member_of=(_other_workspace(outbox_db),))
        outbox_id = _enqueue(outbox_db, binding=binding)
        engine = _engine(outbox_db, login=login)
        try:
            assert await _claim(engine, outbox_db, binding) is None
            assert _state(outbox_db, outbox_id)[:2] == ("pending", 0), (
                "a private chat's card was claimed for a person who belongs to"
                " another workspace only"
            )

            _join(outbox_db, outbox_db["ws"], person)
            row = await _claim(engine, outbox_db, binding)
        finally:
            await engine.dispose()
        assert row is not None and row["id"] == str(outbox_id), (
            "positive control: once the person belongs here, the row is claimed"
        )

    @pytest.mark.asyncio
    async def test_a_chat_no_linked_member_holds_is_not_claimed(self, outbox_db):
        """The person is a member but no Telegram identity of theirs is this
        chat (never linked, or unlinked). A linked member with another chat
        stands beside them, so the refusal is the chat's, not an empty
        membership."""
        chat = _dm_chat()
        binding = _new_binding(outbox_db, chat, channel="telegram_dm")
        person = _person(outbox_db, member_of=(outbox_db["ws"],))
        _person(outbox_db, chat=_dm_chat(), member_of=(outbox_db["ws"],))
        outbox_id = _enqueue(outbox_db, binding=binding)
        engine = _engine(outbox_db)
        try:
            assert await _claim(engine, outbox_db, binding) is None
            assert _state(outbox_db, outbox_id)[:2] == ("pending", 0)

            _link(outbox_db, person, chat)
            row = await _claim(engine, outbox_db, binding)
        finally:
            await engine.dispose()
        assert row is not None and row["id"] == str(outbox_id), (
            "positive control: linked as this chat, the member's row is claimed"
        )


class TestAnOutcomeQueuesNoEditForAChatNoCardMayGoTo:
    """An outcome reaches a card in every live binding but queues the edit
    only where a card may go: a private chat whose person does not belong
    gets its card superseded, or restated, and no edit. That holds for the
    tap's one-statement door, the settled sweep's per-binding one and the
    review resolutions' restate door alike. A group and a member's private
    chat beside it get both: the positive controls, run as `svc_ingress`
    like the refusal."""

    @staticmethod
    def _three_chats(outbox_db) -> tuple[str, str, str]:
        """Three live bindings: a private chat whose person is linked and a
        member of nothing here (refused), a member's private chat, and a
        group."""
        refused_chat, member_chat = _dm_chat(), _dm_chat()
        refused = _new_binding(outbox_db, refused_chat, channel="telegram_dm")
        _person(outbox_db, chat=refused_chat)
        member = _new_binding(outbox_db, member_chat, channel="telegram_dm")
        _person(outbox_db, chat=member_chat, member_of=(outbox_db["ws"],))
        group = _new_binding(outbox_db, f"-100{uuid.uuid4().int % 10**10:010d}")
        return refused, member, group

    @staticmethod
    def _edited(outbox_db, intent) -> set[str]:
        """The bindings an edit was queued for, for *intent*."""
        return {
            str(binding)
            for (binding,) in _owner_exec(
                outbox_db,
                "SELECT binding_id FROM channel_outbox"
                " WHERE kind = 'prompt_supersede' AND intent_id = %s",
                (intent,),
                fetch=True,
            )
        }

    @pytest.mark.asyncio
    async def test_the_card_is_superseded_and_no_edit_is_queued(self, outbox_db):
        from src.services.target import outbox

        refused, member, group = self._three_chats(outbox_db)
        tap, swept = (
            str(
                seed_intent(
                    outbox_db["owner_stream"],
                    outbox_db["ws"],
                    f"dm-edit-{leg}-{uuid.uuid4().hex[:6]}",
                )["intent"]
            )
            for leg in ("tap", "swept")
        )
        cards = {
            (intent, binding): _sent_card(outbox_db, binding, intent)
            for intent in (tap, swept)
            for binding in (refused, member, group)
        }

        async def outcome(session):
            await outbox.supersede_everywhere(
                session, workspace_id=outbox_db["ws"], intent_id=tap, outcome_text="x"
            )
            for binding in (refused, member, group):
                await outbox.supersede_all(
                    session,
                    workspace_id=outbox_db["ws"],
                    binding_id=binding,
                    intent_id=swept,
                    outcome_text="x",
                )

        await in_tenant(
            outbox_db["ingress"], outbox_db["ws"], outbox_db["ws_owner"], outcome
        )

        for key, card in cards.items():
            assert _state(outbox_db, card)[0] == "superseded", key
        for intent in (tap, swept):
            assert self._edited(outbox_db, intent) == {member, group}, intent

    @pytest.mark.asyncio
    async def test_a_restated_card_queues_no_edit_for_the_refused_chat(self, outbox_db):
        """`restate_everywhere_touched`, the review resolutions' door, through
        its count form. It writes the outcome onto the card in every live
        binding, changes no card's state, and queues the edit that shows it
        only where a card may go."""
        from src.services.target import outbox

        refused, member, group = self._three_chats(outbox_db)
        intent = str(
            seed_intent(
                outbox_db["owner_stream"],
                outbox_db["ws"],
                f"dm-restate-{uuid.uuid4().hex[:6]}",
            )["intent"]
        )
        for binding in (refused, member, group):
            _sent_card(outbox_db, binding, intent)

        queued = await in_tenant(
            outbox_db["ingress"],
            outbox_db["ws"],
            outbox_db["ws_owner"],
            lambda session: outbox.restate_everywhere(
                session,
                workspace_id=outbox_db["ws"],
                intent_id=intent,
                outcome_text="x",
            ),
        )

        assert queued == 2, "one edit each for the group and the member's chat"
        cards = {
            str(binding): (state, said)
            for binding, state, said in _owner_exec(
                outbox_db,
                "SELECT binding_id, state, payload->>'outcome_text'"
                " FROM channel_outbox"
                " WHERE kind = 'approval_prompt' AND intent_id = %s",
                (intent,),
                fetch=True,
            )
        }
        assert cards == dict.fromkeys((refused, member, group), ("sent", "x"))
        assert self._edited(outbox_db, intent) == {member, group}


class TestARemovedPersonsPrivateChatGetsNoQueuedCard:
    """A card queued for a person's private chat before they are removed is
    retired by the sender the sweep mints, and never sent; the sweep, which
    reads no membership, mints for that chat once and not again. On the real
    worker: the sweep, the lane and the handler, with the removal run through
    `workspaces.remove_member` as `svc_ingress`. A member who stays gets their
    card on the same run: the positive control."""

    @pytest.mark.asyncio
    async def test_the_queued_card_is_retired_at_send_and_never_sent(self, outbox_db):
        from src.services.target import workspaces

        gone_chat, kept_chat = _dm_chat(), _dm_chat()
        gone = _person(outbox_db, chat=gone_chat, member_of=(outbox_db["ws"],))
        _person(outbox_db, chat=kept_chat, member_of=(outbox_db["ws"],))
        gone_binding = _new_binding(outbox_db, gone_chat, channel="telegram_dm")
        kept_binding = _new_binding(outbox_db, kept_chat, channel="telegram_dm")
        queued = _enqueue(
            outbox_db, binding=gone_binding, payload={"v": 1, "text": "a"}
        )
        control = _enqueue(
            outbox_db, binding=kept_binding, payload={"v": 1, "text": "b"}
        )

        role = await in_tenant(
            outbox_db["ingress"],
            outbox_db["ws"],
            outbox_db["ws_owner"],
            lambda session: workspaces.remove_member(
                session,
                workspace_id=outbox_db["ws"],
                user_id=gone,
                by_user_id=outbox_db["ws_owner"],
            ),
        )
        assert role == "member"

        delivered: list[tuple[str, str]] = []

        class _Transport:
            def for_chat(self, external_ref):
                async def send(row):
                    delivered.append((external_ref, str(row["id"])))
                    return f"tg-{row['id']}"

                return send

        # Two more completed sweeps after the retire: a sender minted for the
        # retired chat again would show below as a second job.
        await _run_worker(
            outbox_db,
            _Transport(),
            lambda: (
                (_state(outbox_db, queued)[0], _state(outbox_db, control)[0])
                == ("superseded", "sent")
            ),
            after_sweeps=2,
        )

        assert _state(outbox_db, queued)[0] == "superseded", (
            "the removed person's queued card was not retired"
        )
        assert [d for d in delivered if d[0] == gone_chat] == [], (
            "a card reached the private chat of a person no longer in the workspace"
        )
        assert _state(outbox_db, control)[0] == "sent"
        assert (kept_chat, str(control)) in delivered, (
            "positive control: the member who stays gets their card"
        )
        minted = _owner_exec(
            outbox_db,
            "SELECT count(*) FROM jobs"
            " WHERE kind = 'deliver_outbox' AND serialization_key = %s",
            (f"tg:{gone_binding}",),
            fetch=True,
        )[0][0]
        assert minted == 1, (
            f"the sweep minted {minted} senders for the retired chat; the first"
            " retires its queue and none follows"
        )


class TestStoppedSenderStrandsNothing:
    """The gate's first half, driven through the service."""

    def _engine(self, outbox_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            async_url(outbox_db["worker"]),
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
        """Telegram has no read-back for a lost response, so a stranded
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
            async_url(outbox_db["worker"]),
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
    async def test_a_live_senders_lost_answer_is_resolved_after_the_backoff(
        self, outbox_db
    ):
        """`settle` marks a lost answer `ambiguous`. The binding's next
        claim resolves the rows that have waited the backoff — the aged card is
        resent (pending, then claimed), the fresh one is left for a later tick,
        and a settled row is never touched."""
        from src.services.target.outbox import (
            AMBIGUOUS_RESOLVE_AFTER_SECONDS,
            pace_and_claim,
        )

        binding = _new_binding(outbox_db)
        aged = _enqueue(outbox_db, kind="prompt_supersede", binding=binding)
        fresh = _enqueue(outbox_db, kind="prompt_supersede", binding=binding)
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'ambiguous', attempts = 1 WHERE id IN (%s, %s)",
            (aged, fresh),
        )
        _backdate(outbox_db, aged, AMBIGUOUS_RESOLVE_AFTER_SECONDS + 1)
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                claimed = await pace_and_claim(
                    conn,
                    binding_id=binding,
                    now=datetime.now(timezone.utc),
                    chat_limit=20,
                    chat_window_seconds=CHAT_WINDOW_S,
                    global_limit=GLOBAL_LIMIT,
                    global_window_seconds=GLOBAL_WINDOW_S,
                )
                await conn.commit()
        finally:
            await engine.dispose()
        assert claimed is not None and str(claimed["id"]) == aged, (
            "the aged row went back to pending and was the next claim"
        )
        assert _state(outbox_db, aged)[0] == "sending"
        assert _state(outbox_db, fresh)[0] == "ambiguous", "not yet its turn"

    @pytest.mark.asyncio
    async def test_a_lost_edit_is_retired_when_a_newer_edit_of_the_message_exists(
        self, outbox_db
    ):
        """Real rows: an aged ambiguous `prompt_supersede` whose message ref
        has a LATER `prompt_supersede` (any state) is `superseded`, never
        resent — the older line must not land over the newer one."""
        from src.services.target.outbox import resolve_ambiguous

        binding = _new_binding(outbox_db)
        # The newer-edit check joins on the intent (it rides `ix_outbox_intent`,
        # `ix_outbox_intent`): the rows carry the chain's intent, as every card edit does.
        ((intent,),) = _owner_exec(
            outbox_db,
            "SELECT id FROM post_intents WHERE workspace_id = %s LIMIT 1",
            (outbox_db["ws"],),
            fetch=True,
        )
        older = _owner_exec(
            outbox_db,
            "INSERT INTO channel_outbox"
            " (workspace_id, binding_id, kind, intent_id, payload, state, attempts)"
            " VALUES (%s, %s, 'prompt_supersede', %s,"
            ' \'{"v": 1, "supersedes_ref": "9001", "outcome_text": "Approved"}\','
            " 'ambiguous', 1) RETURNING id",
            (outbox_db["ws"], binding, intent),
            fetch=True,
        )[0][0]
        _owner_exec(
            outbox_db,
            "INSERT INTO channel_outbox"
            " (workspace_id, binding_id, kind, intent_id, payload, state)"
            " VALUES (%s, %s, 'prompt_supersede', %s,"
            ' \'{"v": 1, "supersedes_ref": "9001", "outcome_text": "Posted"}\','
            " 'sent')",
            (outbox_db["ws"], binding, intent),
        )
        lone = _owner_exec(
            outbox_db,
            "INSERT INTO channel_outbox"
            " (workspace_id, binding_id, kind, intent_id, payload, state, attempts)"
            " VALUES (%s, %s, 'prompt_supersede', %s,"
            ' \'{"v": 1, "supersedes_ref": "9002", "outcome_text": "Skipped"}\','
            " 'ambiguous', 1) RETURNING id",
            (outbox_db["ws"], binding, intent),
            fetch=True,
        )[0][0]
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                assert await resolve_ambiguous(conn, outbox_id=older) == "superseded"
                assert await resolve_ambiguous(conn, outbox_id=lone) == "pending"
                await conn.commit()
        finally:
            await engine.dispose()
        assert _state(outbox_db, older)[0] == "superseded"
        assert _state(outbox_db, lone)[0] == "pending"

    @pytest.mark.asyncio
    async def test_an_approval_prompt_resends_rather_than_failing(self, outbox_db):
        """A silently dropped prompt is not tolerable, and a resend leaves no
        second live card (a card leaves with no keyboard). The resend is
        bounded at MAX_PROMPT_RESENDS (a card that keeps losing its answer is
        not resent forever), and that bound is the prompt's own, not the
        notification's retry-once."""
        from src.services.target.outbox import MAX_PROMPT_RESENDS, resolve_ambiguous

        within = _enqueue(outbox_db, kind="approval_prompt")
        past = _enqueue(outbox_db, kind="approval_prompt")
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'ambiguous', attempts = %s WHERE id = %s",
            (MAX_PROMPT_RESENDS, within),
        )
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'ambiguous', attempts = %s WHERE id = %s",
            (MAX_PROMPT_RESENDS + 2, past),
        )
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                got_within = await resolve_ambiguous(conn, outbox_id=within)
                got_past = await resolve_ambiguous(conn, outbox_id=past)
                await conn.commit()
        finally:
            await engine.dispose()
        assert got_within == "pending", (
            "a prompt within its bound must resend — the retry-once bound"
            " is the notification rule and must not leak onto prompts"
        )
        assert MAX_PROMPT_RESENDS > 1, (
            "the prompt's bound is wider than a notification's"
        )
        assert got_past == "failed", "past its bound a prompt is not resent forever"

    @pytest.mark.asyncio
    async def test_supersede_all_targets_every_known_ref_and_converges(self, outbox_db):
        """Prompts converge on supersede: every live card is retired and a
        `prompt_supersede` row is queued per KNOWN ref; a card whose ref was
        lost is still retired, and a copy it left in the chat has no
        keyboard."""
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
    """`rate_counters` consumption, both scopes. Over budget leaves the
    row `pending` — a slipped slot, not a lost message.

    The budget is spent by a REAL first send rather than by seeding the counter
    or passing a limit of zero: the increment-and-check idiom does not deny at
    ``limit=0``, because its ``WHERE rc.count < :limit`` guards only the ON
    CONFLICT branch and the first hit of a window is an INSERT. Every limit is
    >= 1 so nothing depends on it, but a test that spent the budget that way
    would assert an artifact of the fixture instead of the guard.
    """

    def _engine(self, outbox_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            async_url(outbox_db["worker"]),
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
    with it. That is a SERVICE discipline, which is exactly the shape — so
    it is exercised, not documented.
    """

    def _engine(self, outbox_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            async_url(outbox_db["worker"]),
            pool_size=3,
            max_overflow=0,
        )

    @pytest.mark.asyncio
    async def test_marking_sent_twice_is_fenced_and_does_not_overwrite(self, outbox_db):
        """Fact 2, isolated: the case that kills the mutant. Removing
        `AND state = 'sending'` from the write CAS leaves every other case
        green, because each reaches `mark_sent` with the row genuinely
        `sending`. A fence nothing exercises is a claim, not a guard.
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
    """The poller's cadence, run for real. There is no Redis wake-up, so this
    loop IS the wake-up mechanism and its liveness is a gate concern.

    The loop production runs is `work_loop.deliver_outbox`'s hold: it builds a
    poller and drives `tick` on the cadence until `sender_hold_seconds`, and
    the sweep re-mints the job. The drain below is driven the way the executor
    drives it.
    """

    def _factory(self, outbox_db):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(
            async_url(outbox_db["worker"]),
            pool_size=2,
            max_overflow=0,
        )
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    @pytest.mark.asyncio
    async def test_the_hold_drains_a_queue_on_its_own_timer(self, outbox_db):
        """Three rows, a short interval, the executor's own hold loop: the
        poller must find and send them itself, one per tick, with nothing
        telling it a row arrived. A poller that only works when driven once
        per row by hand is not a replacement for a wake-up channel, so the
        loop below is bounded by TIME and by nothing else — it keeps ticking
        after the queue is empty and stops on the clock, exactly as
        `work_loop.deliver_outbox` does."""
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

        interval = 0.05  # the cadence is 2 s; the gate compresses it
        poller = OutboxPoller(
            _tenant_session_factory(outbox_db, factory),
            binding_id=binding,
            transport=transport,
            clock=_now,
            interval_seconds=interval,
            chat_limit=CHAT_LIMIT,
            chat_window_seconds=CHAT_WINDOW_S,
            global_limit=GLOBAL_LIMIT,
            global_window_seconds=GLOBAL_WINDOW_S,
        )
        drained_at = None
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                await poller.tick()
                if drained_at is None and len(sent_refs) == 3:
                    # Do not stop here: an empty queue must keep being polled.
                    drained_at = poller.ticks
                    deadline = min(deadline, time.monotonic() + 0.4)
                await asyncio.sleep(interval)
        finally:
            await engine.dispose()

        assert drained_at is not None, f"the queue did not drain in 15 s: {sent_refs}"
        assert poller.ticks > drained_at, (
            f"the loop stopped beating once the queue emptied"
            f" ({drained_at} → {poller.ticks}) — it is reacting, not polling"
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
        second_id = _enqueue(outbox_db, kind="notification", binding=binding)

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
        # The paced tick's claim was rolled back with its session: the row is
        # still `pending`, un-attempted, for the next window — never stranded
        # `sending` for `recover_stranded` to write off.
        assert _state(outbox_db, second_id) == ("pending", 0, None)
        assert poller.consecutive_failures == 0, (
            "a paced tick moved the failure counter — a chat at its budget"
            " would read as a dying sender"
        )


class TestTheSenderCommitsBeforeItSpeaks:
    """The poller's tick is transaction-per-checkpoint. The claim (and
    the pacing debits) are COMMITTED before the provider call, so while a send
    is in flight the `tg_global` rate row is free and the claimed row is a
    committed `sending` a tap's supersede can take; after the send the sender
    edits a card it sent for an intent that moved meanwhile."""

    def _factory(self, outbox_db):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(
            async_url(outbox_db["worker"]),
            pool_size=3,
            max_overflow=0,
        )
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    @pytest.mark.asyncio
    async def test_while_a_send_is_in_flight_the_claim_is_committed_and_the_rate_row_is_free(
        self, outbox_db
    ):
        import asyncio

        from src.services.target.outbox import OutboxPoller
        from src.services.target.rate_counters import increment, window_start

        binding = _new_binding(outbox_db)
        outbox_id = _enqueue(outbox_db, kind="notification", binding=binding)
        now = _now()
        in_flight = asyncio.Event()
        release = asyncio.Event()

        async def slow_transport(row):
            in_flight.set()
            await release.wait()
            return "tg-slow"

        engine, factory = self._factory(outbox_db)
        poller = OutboxPoller(
            _tenant_session_factory(outbox_db, factory),
            binding_id=binding,
            transport=slow_transport,
            clock=lambda: now,
            interval_seconds=0.05,
            chat_limit=CHAT_LIMIT,
            chat_window_seconds=CHAT_WINDOW_S,
            global_limit=GLOBAL_LIMIT,
            global_window_seconds=GLOBAL_WINDOW_S,
        )
        try:
            tick = asyncio.create_task(poller.tick())
            await asyncio.wait_for(in_flight.wait(), 10)
            # The claim is COMMITTED: another connection sees `sending`.
            assert _state(outbox_db, outbox_id)[0] == "sending"
            # The `tg_global` row is FREE: a debit from another transaction
            # returns at once (a row lock held across the send would block it
            # for the whole upload —).
            async with factory() as other:
                await other.execute(
                    __import__("sqlalchemy").text("SET LOCAL statement_timeout = '2s'")
                )
                allowed = await increment(
                    other,
                    scope="tg_global",
                    key="",
                    window_start=window_start(now, GLOBAL_WINDOW_S),
                    limit=GLOBAL_LIMIT,
                )
                await other.commit()
            assert allowed is not None
            release.set()
            result = await asyncio.wait_for(tick, 10)
        finally:
            release.set()
            await engine.dispose()
        assert result["state"] == "sent"
        assert _state(outbox_db, outbox_id) == ("sent", 1, "tg-slow")

    @pytest.mark.asyncio
    async def test_a_card_superseded_in_flight_is_edited_by_the_sender_itself(
        self, outbox_db
    ):
        """A tap retires the card between claim and send: the supersede takes
        the committed `sending` row, `mark_sent` is fenced, and the sender —
        the only one who knows the ref — records it on the card, so a later
        line reaches the message, and queues the edit for it."""
        import asyncio

        from src.services.target.outbox import OutboxPoller, supersede_all

        binding = _new_binding(outbox_db)
        (intent,) = _owner_exec(
            outbox_db,
            "SELECT id FROM post_intents WHERE workspace_id = %s LIMIT 1",
            (outbox_db["ws"],),
            fetch=True,
        )[0]
        outbox_id = _owner_exec(
            outbox_db,
            "INSERT INTO channel_outbox (workspace_id, binding_id, kind, intent_id, payload)"
            " VALUES (%s, %s, 'approval_prompt', %s,"
            '  \'{"v": 2, "text": "card"}\') RETURNING id',
            (outbox_db["ws"], binding, intent),
            fetch=True,
        )[0][0]
        now = _now()
        in_flight = asyncio.Event()
        release = asyncio.Event()

        async def slow_transport(row):
            in_flight.set()
            await release.wait()
            from src.channels.telegram_transport import SendReceipt

            return SendReceipt("tg-late", sent_as="media")

        engine, factory = self._factory(outbox_db)
        poller = OutboxPoller(
            _tenant_session_factory(outbox_db, factory),
            binding_id=binding,
            transport=slow_transport,
            clock=lambda: now,
            interval_seconds=0.05,
            chat_limit=CHAT_LIMIT,
            chat_window_seconds=CHAT_WINDOW_S,
            global_limit=GLOBAL_LIMIT,
            global_window_seconds=GLOBAL_WINDOW_S,
        )
        try:
            tick = asyncio.create_task(poller.tick())
            await asyncio.wait_for(in_flight.wait(), 10)
            async with factory() as other:
                await other.execute(
                    __import__("sqlalchemy").text(
                        "SELECT set_config('app.tenant_id', :v, false)"
                    ),
                    {"v": outbox_db["ws"]},
                )
                n = await supersede_all(
                    other,
                    workspace_id=outbox_db["ws"],
                    binding_id=binding,
                    intent_id=str(intent),
                    outcome_text="⏭️ Skipped by Ada · now",
                )
                await other.commit()
            assert n == 1, "the committed `sending` row is what the supersede takes"
            release.set()
            result = await asyncio.wait_for(tick, 10)
        finally:
            release.set()
            await engine.dispose()
        assert result["state"] == "superseded"
        state, _, ref = _state(outbox_db, outbox_id)
        assert (state, ref) == ("superseded", "tg-late"), (
            "the card takes the ref only the sender received"
        )
        ((sent_as,),) = _owner_exec(
            outbox_db,
            "SELECT payload->>'sent_as' FROM channel_outbox WHERE id = %s",
            (outbox_id,),
            fetch=True,
        )
        assert sent_as == "media", "and how it went out, for the next edit's method"
        rows = _owner_exec(
            outbox_db,
            "SELECT payload->>'supersedes_ref', payload->>'sent_as',"
            "       payload->>'outcome_text' FROM channel_outbox"
            " WHERE binding_id = %s AND kind = 'prompt_supersede'"
            "   AND payload->>'supersedes_ref' = 'tg-late'",
            (binding,),
            fetch=True,
        )
        assert rows == [("tg-late", "media", "⏭️ Skipped by Ada · now")], (
            "the sender must queue the edit for the ref only it received, with"
            " how the card went out and the line the supersede carried"
        )

    @pytest.mark.asyncio
    async def test_an_idle_poller_spends_no_pacing_budget(self, outbox_db):
        """Empty ticks debit nothing: a silent chat keeps its whole window for
        the card that arrives."""
        from src.services.target.outbox import OutboxPoller

        binding = _new_binding(outbox_db)
        now = _now()

        async def never(row):  # pragma: no cover — nothing to send
            raise AssertionError("nothing is pending")

        engine, factory = self._factory(outbox_db)
        poller = OutboxPoller(
            _tenant_session_factory(outbox_db, factory),
            binding_id=binding,
            transport=never,
            clock=lambda: now,
            interval_seconds=0.05,
            chat_limit=CHAT_LIMIT,
            chat_window_seconds=CHAT_WINDOW_S,
            global_limit=GLOBAL_LIMIT,
            global_window_seconds=GLOBAL_WINDOW_S,
        )
        try:
            for _ in range(20):
                assert await poller.tick() is None
        finally:
            await engine.dispose()
        spent = _owner_exec(
            outbox_db,
            "SELECT count(*) FROM rate_counters WHERE scope = 'tg_chat' AND key = %s",
            (binding,),
            fetch=True,
        )[0][0]
        assert spent == 0 and poller.consecutive_failures == 0


class TestAFloodLimitWritesADurableHold:
    """A 429 returns the row to `pending` and spends the
    pacing rows for Telegram's `retry_after` in ONE statement, so every
    replica's sender defers until it passes — no in-task sleep,
    no ambiguous row, no attempt lost to the provider's own limit."""

    def _engine(self, outbox_db):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(
            async_url(outbox_db["worker"]),
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
    async def test_the_hold_is_on_the_rows_and_the_next_send_defers(self, outbox_db):
        from src.services.target.outbox import (
            CHAT_SCOPED_GLOBAL_BRAKE_SECONDS,
            ChannelPaced,
            OutboxPaced,
            deliver,
        )

        binding = _new_binding(outbox_db)
        row_id = _enqueue(outbox_db, kind="notification", binding=binding)
        reached: list = []

        async def flooded(row):
            reached.append(row["id"])
            raise ChannelPaced("429", retry_after_s=3, scope="chat")

        now = _now()
        engine = self._engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                result = await deliver(
                    conn,
                    binding_id=binding,
                    transport=flooded,
                    now=now,
                    chat_limit=CHAT_LIMIT,
                    chat_window_seconds=CHAT_WINDOW_S,
                    global_limit=GLOBAL_LIMIT,
                    global_window_seconds=GLOBAL_WINDOW_S,
                )
                await conn.commit()
            assert result is not None and result["state"] == "paced"
            assert result["retry_after_s"] == 3.0
            assert result["held_windows"]["global"] >= 1
            assert result["held_windows"]["chat"] >= 1
            assert _state(outbox_db, row_id) == ("pending", 0, None), (
                "back to pending with the claim's attempt restored"
            )

            # A second sender — any replica — in the same instant is deferred
            # by the rows, before it reaches the provider.
            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                with pytest.raises(OutboxPaced):
                    await deliver(
                        conn,
                        binding_id=binding,
                        transport=flooded,
                        now=now,
                        chat_limit=CHAT_LIMIT,
                        chat_window_seconds=CHAT_WINDOW_S,
                        global_limit=GLOBAL_LIMIT,
                        global_window_seconds=GLOBAL_WINDOW_S,
                    )
                await conn.rollback()

            # ...and after the hold has passed, the row goes out.
            async def sends(row):
                reached.append(row["id"])
                return "tg-after-hold"

            async with engine.connect() as conn:
                await self._tenant(conn, outbox_db)
                later = await deliver(
                    conn,
                    binding_id=binding,
                    transport=sends,
                    now=now + timedelta(seconds=CHAT_WINDOW_S + 5),
                    chat_limit=CHAT_LIMIT,
                    chat_window_seconds=CHAT_WINDOW_S,
                    global_limit=GLOBAL_LIMIT,
                    global_window_seconds=GLOBAL_WINDOW_S,
                )
                await conn.commit()
        finally:
            await engine.dispose()

        assert reached == [str(row_id), str(row_id)], reached
        assert later is not None and later["state"] == "sent"
        assert _state(outbox_db, row_id)[0] == "sent"
        held = _owner_exec(
            outbox_db,
            "SELECT count(*) FROM rate_counters"
            " WHERE scope = 'tg_global' AND key = '' AND count >= %s"
            "   AND window_start >= %s AND window_start <= %s",
            (GLOBAL_LIMIT, now, now + timedelta(seconds=3)),
            fetch=True,
        )[0][0]
        # Chat-scoped: the fleet's row takes the 2 s brake (three inclusive 1 s
        # windows), the chat's row the whole retry_after — the deferral above
        # came from the chat row.
        assert held == CHAT_SCOPED_GLOBAL_BRAKE_SECONDS // GLOBAL_WINDOW_S + 1, (
            f"the global brake must be spent for every window, got {held}"
        )
        chat_held = _owner_exec(
            outbox_db,
            "SELECT count(*) FROM rate_counters"
            " WHERE scope = 'tg_chat' AND key = %s AND count >= %s",
            (binding, CHAT_LIMIT),
            fetch=True,
        )[0][0]
        assert chat_held >= 1

    @pytest.mark.asyncio
    async def test_the_poller_counts_a_flood_as_deferred_and_remembers_the_wait(
        self, outbox_db
    ):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from src.services.target.outbox import ChannelPaced, OutboxPoller

        binding = _new_binding(outbox_db)
        row_id = _enqueue(outbox_db, kind="notification", binding=binding)

        async def flooded(row):
            raise ChannelPaced("429", retry_after_s=9, scope="global")

        engine = self._engine(outbox_db)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        poller = OutboxPoller(
            _tenant_session_factory(outbox_db, factory),
            binding_id=binding,
            transport=flooded,
            clock=_now,
            interval_seconds=0.05,
            chat_limit=CHAT_LIMIT,
            chat_window_seconds=CHAT_WINDOW_S,
            global_limit=GLOBAL_LIMIT,
            global_window_seconds=GLOBAL_WINDOW_S,
        )
        try:
            result = await poller.tick()
        finally:
            await engine.dispose()

        assert result is not None and result["state"] == "paced"
        assert poller.deferred == 1 and poller.held_for_s == 9.0
        assert poller.consecutive_failures == 0 and poller.sent == 0
        state, attempts, ref = _state(outbox_db, row_id)
        assert (state, ref) == ("pending", None), (
            "a 429 leaves the row pending for the next window — never ambiguous"
        )


class TestASlowChatDoesNotDelayAnother:
    """On the worker: two senders on the
    interactive lane, two bindings; a transport that takes seconds for one
    chat must not delay the other binding's send by more than a poller
    cadence — the K tasks are what free the lane from a slow binding."""

    @pytest.mark.asyncio
    async def test_the_other_binding_sends_while_one_is_slow(self, outbox_db):
        import asyncio
        import time as _time

        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.work_loop import WorkerConfig as _Cfg
        from src.worker import compose

        slow_binding = _new_binding(outbox_db)
        fast_binding = _new_binding(outbox_db)
        refs = {
            b: _owner_exec(
                outbox_db,
                "SELECT external_ref FROM channel_bindings WHERE id = %s",
                (b,),
                fetch=True,
            )[0][0]
            for b in (slow_binding, fast_binding)
        }
        slow_row = _enqueue(outbox_db, kind="notification", binding=slow_binding)
        fast_row = _enqueue(outbox_db, kind="notification", binding=fast_binding)
        _seed_sender_job(outbox_db, binding=slow_binding)
        _seed_sender_job(outbox_db, binding=fast_binding)
        # Another test's worker in this module can leave a sender job due for its
        # own binding (`ensure_sender_jobs` mints one per binding with a pending
        # row, and a stopped worker does not drain it). The lanes claim the oldest
        # job first, so those would be the sends this test sees: retire them, so
        # only this test's two bindings are due.
        _owner_exec(
            outbox_db,
            "UPDATE jobs SET state = 'cancelled' WHERE kind = 'deliver_outbox'"
            " AND state IN ('ready', 'leased') AND serialization_key NOT IN (%s, %s)",
            (f"tg:{slow_binding}", f"tg:{fast_binding}"),
        )
        sent_at: dict = {}
        started = _time.monotonic()

        class _Transport:
            def for_chat(self, external_ref):
                async def send(row):
                    if external_ref == refs[slow_binding]:
                        await asyncio.sleep(3.0)
                    sent_at[str(row["id"])] = _time.monotonic() - started
                    return f"tg-{row['id']}"

                return send

        from src.services.target import unit_of_work as _uow

        engine = create_async_engine(
            async_url(outbox_db["worker"]),
            pool_size=10,
            max_overflow=0,
        )
        watch = _uow.PoolWatch(engine)
        try:
            cfg = _Cfg(
                lane_concurrency={"interactive": 2, "bulk": 1},
                poller_interval_seconds=0.1,
                sender_hold_seconds=1.0,
                claim_idle_seconds=0.05,
                chat_limit=CHAT_LIMIT,
                chat_window_seconds=CHAT_WINDOW_S,
                global_limit=GLOBAL_LIMIT,
                global_window_seconds=GLOBAL_WINDOW_S,
            )
            app = compose(engine=engine, config=cfg, env={}, transport=_Transport())
            loops = [wl for wl in app.loops if wl.lane == "interactive"]
            assert len(loops) == 2
            tasks = [asyncio.create_task(wl.run()) for wl in loops]
            deadline = _time.monotonic() + 10
            while len(sent_at) < 2 and _time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            for wl in loops:
                wl.stop()
            await asyncio.gather(*tasks)
        finally:
            await engine.dispose()

        assert set(sent_at) == {str(slow_row), str(fast_row)}, sent_at
        assert sent_at[str(fast_row)] < 1.5, (
            f"the fast binding waited {sent_at[str(fast_row)]:.2f}s behind the slow one"
        )
        assert sent_at[str(slow_row)] >= 3.0
        assert _state(outbox_db, fast_row)[0] == "sent"
        assert _state(outbox_db, slow_row)[0] == "sent"
        # The measurement for the sender kind: two tasks in the
        # hold, each with its short sessions and poller ticks one at a time,
        # never more than one connection each plus a claim in flight.
        assert watch.checked_out_peak <= 3, watch.checked_out_peak


class TestACardIsRenderedAgainAtClaimTime:
    """A card is rendered again when the sender claims it: a card whose slot
    moved on is retired without a send; a live one goes out as the fresh card
    with no keyboard (`TestACardsKeyboardArrivesOnlyByAConfirmedEdit`), and
    the row's record is the fresh card."""

    def _stale_card(self, outbox_db, binding, intent):
        """A stale card: manual buttons, one attempt spent."""
        return _enqueue(
            outbox_db,
            kind="approval_prompt",
            intent_id=intent,
            binding=binding,
            payload={
                "v": 2,
                "text": "📸 old",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Posted myself", "callback_data": "v1:posted:x"}]
                    ]
                },
            },
            attempts=1,
        )

    async def _deliver(self, outbox_db, binding, transport):
        from src.services.target.outbox import deliver

        engine = _engine(outbox_db)
        try:
            async with engine.connect() as conn:
                await _tenant(conn, outbox_db)
                result = await deliver(
                    conn,
                    binding_id=binding,
                    transport=transport,
                    now=_now(),
                    chat_limit=CHAT_LIMIT,
                    chat_window_seconds=CHAT_WINDOW_S,
                    global_limit=GLOBAL_LIMIT,
                    global_window_seconds=GLOBAL_WINDOW_S,
                )
                await conn.commit()
            return result
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_card_for_a_slot_that_moved_on_is_retired_not_sent(self, outbox_db):
        binding = _new_binding(outbox_db)
        intent = str(
            seed_intent(
                outbox_db["owner_stream"], outbox_db["ws"], "gone", state="skipped"
            )["intent"]
        )
        row_id = self._stale_card(outbox_db, binding, intent)
        sent = []

        async def transport(row):
            sent.append(row["id"])
            return "tg-never"

        result = await self._deliver(outbox_db, binding, transport)
        assert result is None and sent == []
        assert _state(outbox_db, row_id)[0] == "superseded"

    @pytest.mark.asyncio
    async def test_a_story_parked_before_its_card_went_out_gets_its_review_card(
        self, outbox_db
    ):
        binding = _new_binding(outbox_db)
        intent = str(
            seed_intent(
                outbox_db["owner_stream"],
                outbox_db["ws"],
                "parked",
                state="review_required",
            )["intent"]
        )
        row_id = self._stale_card(outbox_db, binding, intent)
        sent = []

        async def transport(row):
            sent.append(row["payload"])
            return "tg-review"

        result = await self._deliver(outbox_db, binding, transport)
        assert result is not None and result["state"] == "sent"
        assert len(sent) == 1, "the parked story's card goes out once"
        assert sent[0]["outcome_text"].startswith("👀 Needs review")
        # Its review keyboard (the three resolutions, then Configure) arrives
        # by the edit that follows the send, as every card's does.
        assert "reply_markup" not in sent[0]
        assert _state(outbox_db, row_id)[0] == "superseded", (
            "a card sent with its line is recorded settled, its ref kept"
        )

    @pytest.mark.asyncio
    async def test_a_card_for_a_live_slot_goes_out_fresh_and_without_a_keyboard(
        self, outbox_db
    ):
        binding = _new_binding(outbox_db)
        intent = str(
            seed_intent(
                outbox_db["owner_stream"],
                outbox_db["ws"],
                "live",
                state="awaiting_approval",
            )["intent"]
        )
        # A workspace whose story would show Post now sends its card bare too.
        _owner_exec(
            outbox_db,
            "UPDATE workspaces SET api_publishing_enabled = true WHERE id = %s",
            (outbox_db["ws"],),
        )
        row_id = self._stale_card(outbox_db, binding, intent)
        sent = []

        async def transport(row):
            sent.append(row["payload"])
            return "tg-fresh"

        result = await self._deliver(outbox_db, binding, transport)
        assert result is not None and result["state"] == "sent"
        assert len(sent) == 1
        assert "reply_markup" not in sent[0], "the card leaves with no keyboard"
        assert "📸 old" not in json.dumps(sent[0], ensure_ascii=False)
        stored = _owner_exec(
            outbox_db,
            "SELECT payload::text FROM channel_outbox WHERE id = %s",
            (row_id,),
            fetch=True,
        )[0][0]
        assert "reply_markup" not in stored and "📸 old" not in stored


class _ScriptedTransport:
    """The sender's transport, scripted. Each call is answered from *script* in
    turn: a receipt is returned, an exception raised. An edit past the script
    lands, as Telegram answers one, with the ref it names. Every row it was
    handed is kept in `rows`."""

    def __init__(self, *script):
        self._script = list(script)
        self.rows: list[dict] = []

    async def __call__(self, row):
        from src.channels.telegram_transport import SendReceipt, TelegramSendError

        self.rows.append(row)
        answer = self._script.pop(0) if self._script else None
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        if row["kind"] != "prompt_supersede":
            raise TelegramSendError("no answer was scripted for this send")
        return SendReceipt(row["payload"]["supersedes_ref"], sent_as="edit")

    def sent(self, kind):
        """The rows of *kind* it was handed, in order."""
        return [r for r in self.rows if r["kind"] == kind]


class TestACardsKeyboardArrivesOnlyByAConfirmedEdit:
    """A card is sent with no keyboard, and settling its send queues the edit
    that brings one, for the ref Telegram answered with. So the copy a lost
    answer leaves behind has no buttons; an edit Telegram refuses leaves an
    inert card on a story that stays decidable; and an edit that is lost or
    paced is sent again until it lands. Driven through `OutboxPoller.tick`
    over a transport double."""

    def _pending_card(self, outbox_db, *, chat=None):
        """A binding of its own, a story awaiting its decision, and its card
        still pending with a keyboard in the payload, which the sender leaves
        out. Returns the binding, the intent and the card."""
        binding = _new_binding(outbox_db, chat)
        intent = str(
            seed_intent(
                outbox_db["owner_stream"],
                outbox_db["ws"],
                f"arm-{uuid.uuid4().hex[:6]}",
                state="awaiting_approval",
            )["intent"]
        )
        card = _enqueue(
            outbox_db,
            kind="approval_prompt",
            intent_id=intent,
            binding=binding,
            payload={
                "v": 2,
                "text": "📸 old",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Posted myself", "callback_data": "v1:posted:x"}]
                    ]
                },
            },
        )
        return binding, intent, card

    def _edits(self, outbox_db, binding):
        """The binding's edits, oldest first, as (payload, state, attempts)."""
        return _owner_exec(
            outbox_db,
            "SELECT payload, state, attempts FROM channel_outbox"
            " WHERE binding_id = %s AND kind = 'prompt_supersede'"
            " ORDER BY created_at",
            (binding,),
            fetch=True,
        )

    @contextlib.asynccontextmanager
    async def _sender(self, outbox_db, binding, transport):
        """The binding's poller over *transport*, on the worker's login and a
        clock window of its own, with the tenant session factory it writes
        through."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from src.services.target.outbox import OutboxPoller

        engine = _engine(outbox_db)
        tenant = _tenant_session_factory(
            outbox_db, async_sessionmaker(engine, expire_on_commit=False)
        )
        now = _now()
        try:
            yield (
                OutboxPoller(
                    tenant,
                    binding_id=binding,
                    transport=transport,
                    clock=lambda: now,
                    interval_seconds=0.05,
                    chat_limit=CHAT_LIMIT,
                    chat_window_seconds=CHAT_WINDOW_S,
                    global_limit=GLOBAL_LIMIT,
                    global_window_seconds=GLOBAL_WINDOW_S,
                ),
                tenant,
            )
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_resent_card_leaves_no_second_card_with_buttons(self, outbox_db):
        """A card whose send lost its answer is sent again, and the copy the
        first send may have left in the chat has no buttons: no send carries a
        keyboard, and the one edit that brings it names the ref of the send
        that was answered."""
        from src.channels.telegram_transport import SendReceipt, TelegramSendError
        from src.services.target.outbox import AMBIGUOUS_RESOLVE_AFTER_SECONDS

        binding, intent, card = self._pending_card(outbox_db)
        telegram = _ScriptedTransport(
            TelegramSendError("sendMessage: transport failure"),
            SendReceipt("tg-2", sent_as="text"),
        )
        async with self._sender(outbox_db, binding, telegram) as (poller, _):
            lost = await poller.tick()
            assert lost is not None and lost["state"] == "ambiguous"
            attempts = _state(outbox_db, card)[1]
            _backdate(outbox_db, card, AMBIGUOUS_RESOLVE_AFTER_SECONDS * attempts + 1)
            resent = await poller.tick()
            armed = await poller.tick()
        assert resent is not None and resent["state"] == "sent"
        assert armed is not None and armed["state"] == "sent"

        cards = telegram.sent("approval_prompt")
        assert len(cards) == 2, "the send that lost its answer, and its resend"
        assert all("reply_markup" not in c["payload"] for c in cards)
        edits = self._edits(outbox_db, binding)
        assert [(p["supersedes_ref"], s) for p, s, _ in edits] == [("tg-2", "sent")]
        assert "outcome_text" not in edits[0][0], "the story waits: the keyboard alone"
        (edit,) = telegram.sent("prompt_supersede")
        tokens = [
            b.get("callback_data")
            for r in edit["payload"]["reply_markup"]["inline_keyboard"]
            for b in r
        ]
        assert f"v1:cfg:{intent}" in tokens, "the edit brings the story's keyboard"

    @pytest.mark.asyncio
    async def test_a_refused_edit_leaves_an_inert_card_and_the_next_change_repaints_it(
        self, outbox_db
    ):
        """Telegram refusing the edit that would bring a card its keyboard
        fails that edit once. The card stays in the chat with no buttons, the
        story stays `awaiting_approval` (the web queue lists it, so it can be
        decided there), and when the story changes, the edit that follows
        writes its line onto the message."""
        from src.channels.telegram_transport import SendReceipt, TelegramRefused
        from src.services.target.outbox import supersede_all

        binding, intent, card = self._pending_card(outbox_db)
        line = "⏭️ Skipped by Ada · now"
        telegram = _ScriptedTransport(
            SendReceipt("tg-1", sent_as="text"),
            TelegramRefused(
                "editMessageReplyMarkup: 400 Bad Request: can't parse reply"
                " keyboard markup",
                code=400,
            ),
        )
        async with self._sender(outbox_db, binding, telegram) as (poller, tenant):
            sent = await poller.tick()
            refused = await poller.tick()
            assert sent is not None and sent["state"] == "sent"
            assert refused is not None and refused["state"] == "failed"
            assert [s for _, s, _ in self._edits(outbox_db, binding)] == ["failed"]
            assert not any(
                "reply_markup" in c["payload"] for c in telegram.sent("approval_prompt")
            ), "no card was accepted with a keyboard"
            assert _owner_exec(
                outbox_db,
                "SELECT state FROM post_intents WHERE id = %s",
                (intent,),
                fetch=True,
            ) == [("awaiting_approval",)], "the story still waits for a decision"

            # The story is decided the way a skip decides it.
            _owner_exec(
                outbox_db,
                "UPDATE post_intents SET state = 'skipped' WHERE id = %s",
                (intent,),
            )
            async with tenant() as session:
                queued = await supersede_all(
                    session,
                    workspace_id=outbox_db["ws"],
                    binding_id=binding,
                    intent_id=intent,
                    outcome_text=line,
                )
                await session.commit()
            assert queued == 1, "the card is superseded, and its edit is queued"
            repainted = await poller.tick()
        assert repainted is not None and repainted["state"] == "sent"
        edit = telegram.rows[-1]
        assert edit["kind"] == "prompt_supersede"
        assert edit["payload"]["supersedes_ref"] == "tg-1"
        assert edit["payload"]["outcome_text"] == line
        assert [
            (p["supersedes_ref"], p.get("outcome_text"), s)
            for p, s, _ in self._edits(outbox_db, binding)
        ] == [("tg-1", None, "failed"), ("tg-1", line, "sent")]

    def test_the_backoff_grows_with_attempts_and_the_sweep_agrees(self, outbox_db):
        """A lost answer waits `AMBIGUOUS_RESOLVE_AFTER_SECONDS` times the
        row's attempts before it is resolved, and the sender sweep mints the
        binding's sender on the same schedule: a row still waiting is neither
        resolved nor given a sender."""
        import asyncio

        from src.services.target.outbox import (
            AMBIGUOUS_RESOLVE_AFTER_SECONDS,
            resolve_aged_ambiguous,
        )

        attempts = 3
        due = AMBIGUOUS_RESOLVE_AFTER_SECONDS * attempts
        binding = _new_binding(outbox_db)
        edit = _enqueue(
            outbox_db,
            kind="prompt_supersede",
            binding=binding,
            payload={"v": 1, "supersedes_ref": "tg-9"},
            attempts=attempts,
        )
        _owner_exec(
            outbox_db,
            "UPDATE channel_outbox SET state = 'ambiguous' WHERE id = %s",
            (edit,),
        )

        def senders():
            return _owner_exec(
                outbox_db,
                "SELECT count(*) FROM jobs"
                " WHERE kind = 'deliver_outbox' AND serialization_key = %s",
                (f"tg:{binding}",),
                fetch=True,
            )[0][0]

        async def resolve():
            engine = _engine(outbox_db)
            try:
                async with engine.connect() as conn:
                    await _tenant(conn, outbox_db)
                    resolved = await resolve_aged_ambiguous(conn, binding_id=binding)
                    await conn.commit()
                return resolved
            finally:
                await engine.dispose()

        # Past the backoff of one attempt, short of the one three have earned.
        _backdate(outbox_db, edit, due - AMBIGUOUS_RESOLVE_AFTER_SECONDS)
        sweep_as_worker(outbox_db["worker"])
        assert senders() == 0, "a row still waiting is given no sender"
        assert asyncio.run(resolve()) == []
        assert _state(outbox_db, edit)[0] == "ambiguous"

        _backdate(outbox_db, edit, due + 10)
        sweep_as_worker(outbox_db["worker"])
        assert senders() == 1, "a row that is due is given its binding's sender"
        assert asyncio.run(resolve()) == [str(edit)]
        assert _state(outbox_db, edit)[0] == "pending"

    @pytest.mark.asyncio
    async def test_not_modified_confirms_the_edit(self, outbox_db):
        """Telegram's "message is not modified" says the keyboard already
        stands: the edit is sent, and nothing is queued after it. The real
        transport, over a mock Bot API."""
        import httpx

        from src.channels.telegram_transport import TelegramTransport
        from tests.fixtures.fake_providers import OFFLINE

        chat = f"-100{uuid.uuid4().int % 10**10:010d}"
        binding, intent, card = self._pending_card(outbox_db, chat=chat)
        calls = []

        def handler(request):
            method = request.url.path.rsplit("/", 1)[-1]
            calls.append((method, json.loads(request.content)))
            if method == "sendMessage":
                return httpx.Response(
                    200, json={"ok": True, "result": {"message_id": 7}}
                )
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": (
                        "Bad Request: message is not modified: specified new"
                        " message content and reply markup are exactly the same"
                        " as a current content and reply markup of the message"
                    ),
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = TelegramTransport(
            "synthetic-bot-token", client=client, policy=OFFLINE
        )
        send = transport.for_chat(chat)
        try:
            async with self._sender(outbox_db, binding, send) as (poller, _):
                sent = await poller.tick()
                confirmed = await poller.tick()
                idle = await poller.tick()
        finally:
            await client.aclose()
        assert [m for m, _ in calls] == ["sendMessage", "editMessageReplyMarkup"]
        assert "reply_markup" not in calls[0][1], "the card went out with no keyboard"
        assert sent is not None and sent["state"] == "sent"
        assert confirmed is not None and confirmed["state"] == "sent"
        assert idle is None, "nothing is left to send"
        assert [
            (p["supersedes_ref"], s) for p, s, _ in self._edits(outbox_db, binding)
        ] == [("7", "sent")], "the one edit is done, and none follows it"

    @pytest.mark.asyncio
    async def test_a_429_on_the_edit_holds_the_chat_and_spends_no_attempt(
        self, outbox_db
    ):
        """Telegram's flood limit on the edit is not the edit's failure: it
        goes back to `pending` with the attempt its claim consumed given back,
        and the chat's pacing row is held for the wait Telegram named."""
        from src.channels.telegram_transport import SendReceipt, TelegramPaced

        binding, intent, card = self._pending_card(outbox_db)
        telegram = _ScriptedTransport(
            SendReceipt("tg-1", sent_as="text"),
            TelegramPaced(
                "editMessageReplyMarkup: 429",
                retry_after_s=30,
                scope="chat",
                code=429,
            ),
        )
        async with self._sender(outbox_db, binding, telegram) as (poller, _):
            sent = await poller.tick()
            paced = await poller.tick()
        assert sent is not None and sent["state"] == "sent"
        assert paced is not None and paced["state"] == "paced"
        assert poller.held_for_s == 30.0
        assert [(s, a) for _, s, a in self._edits(outbox_db, binding)] == [
            ("pending", 0)
        ]
        held = _owner_exec(
            outbox_db,
            "SELECT count(*) FROM rate_counters"
            " WHERE scope = 'tg_chat' AND key = %s AND count >= %s",
            (binding, CHAT_LIMIT),
            fetch=True,
        )[0][0]
        assert held >= 1, "the chat's row is spent for the wait"
        (sent_card,) = telegram.sent("approval_prompt")
        assert "reply_markup" not in sent_card["payload"]

    @pytest.mark.asyncio
    async def test_edits_are_claimed_before_cards(self, outbox_db):
        """A card's keyboard, which only an edit brings, follows the card
        before the next card is sent: with the oldest pending row a card and
        the newest an edit, the edit is claimed first."""
        binding = _new_binding(outbox_db)
        card = _enqueue(
            outbox_db,
            kind="approval_prompt",
            binding=binding,
            payload={"v": 2, "text": "card"},
        )
        edit = _enqueue(
            outbox_db,
            kind="prompt_supersede",
            binding=binding,
            payload={"v": 1, "supersedes_ref": "tg-9"},
        )
        engine = _engine(outbox_db)
        try:
            first = await _claim(engine, outbox_db, binding)
            second = await _claim(engine, outbox_db, binding)
        finally:
            await engine.dispose()
        assert first is not None and second is not None
        assert (first["kind"], str(first["id"])) == ("prompt_supersede", str(edit))
        assert (second["kind"], str(second["id"])) == ("approval_prompt", str(card)), (
            "the card follows its edit; it is not lost"
        )


class TestTheSenderFollowsAGroupThatMoved:
    """The sender discovers a group that becomes a supergroup.

    If its migration notice never reaches the ingress, the next delivery
    discovers the new id: Telegram refuses the old id and names the new one.
    The binding follows, and the chat keeps receiving: the worker's own sender
    sweep mints the binding's next sender, which delivers to the successor.
    (The row that discovered the move is the settle taxonomy's — a gone
    destination fails it outright.)"""

    @pytest.mark.asyncio
    async def test_the_next_delivery_reaches_the_new_chat(self, outbox_db):
        from src.channels.telegram_transport import TelegramChatGone

        old, new = (
            f"-4{uuid.uuid4().int % 10**9:09d}",
            "-100" + str(uuid.uuid4().int % 10**10).zfill(10),
        )
        binding = _new_binding(outbox_db, external_ref=old)
        _enqueue(outbox_db, binding=binding, payload={"v": 1, "text": "a"})
        follows = _enqueue(outbox_db, binding=binding, payload={"v": 1, "text": "b"})
        delivered: list[tuple[str, str]] = []

        class _Transport:
            def for_chat(self, external_ref):
                async def send(row):
                    if external_ref == old:
                        raise TelegramChatGone(
                            "sendMessage: 400 Bad Request: group chat was"
                            " upgraded to a supergroup chat",
                            migrate_to=new,
                        )
                    delivered.append((external_ref, str(row["id"])))
                    return f"tg-{row['id']}"

                return send

        await _run_worker(
            outbox_db, _Transport(), lambda: _state(outbox_db, follows)[0] == "sent"
        )

        ref = _owner_exec(
            outbox_db,
            "SELECT external_ref, state FROM channel_bindings WHERE id = %s",
            (binding,),
            fetch=True,
        )[0]
        assert ref == (new, "active"), "the binding follows the chat"
        assert _state(outbox_db, follows)[0] == "sent"
        assert (new, str(follows)) in delivered, "and the next card reaches it"


@pytest.mark.parametrize("blocked_count", [1, 4])
@pytest.mark.parametrize("delivery_path", ["poller", "deliver"])
async def test_media_admission_defer_is_fair_across_fresh_pollers(
    outbox_db, monkeypatch, blocked_count, delivery_path
):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.services.target import outbox, prompts, source_policy

    binding = _new_binding(outbox_db)
    denied = {str(uuid.uuid4()) for _ in range(blocked_count)}
    rows = [
        _enqueue(
            outbox_db,
            binding=binding,
            kind="approval_prompt",
            intent_id=i,
            payload={"v": 1, "text": "synthetic"},
        )
        for i in denied
    ]
    ready = _enqueue(outbox_db, binding=binding, payload={"v": 1, "text": "synthetic"})

    async def render(session, *, intent_id):
        if intent_id in denied:
            raise source_policy.MediaAdmissionPending("validation_pending")
        return {"v": 1, "text": "synthetic"}

    monkeypatch.setattr(prompts, "rerender_prompt", render)
    monkeypatch.setattr(outbox, "MEDIA_ADMISSION_PASS_LIMIT", 2, raising=False)
    engine = _engine(outbox_db)
    factory = _tenant_session_factory(
        outbox_db, async_sessionmaker(engine, expire_on_commit=False)
    )
    sent = []

    async def transport(row):
        sent.append(row["id"])
        return "synthetic-ref"

    def poller():
        return outbox.OutboxPoller(
            factory,
            binding_id=binding,
            transport=transport,
            clock=_now,
            interval_seconds=1,
            chat_limit=20,
            chat_window_seconds=60,
            global_limit=30,
            global_window_seconds=1,
        )

    async def attempt():
        if delivery_path == "poller":
            return await poller().tick()  # reconstruct each pass
        async with factory() as session:
            result = await outbox.deliver(
                session,
                binding_id=binding,
                transport=transport,
                now=_now(),
                chat_limit=20,
                chat_window_seconds=60,
                global_limit=30,
                global_window_seconds=1,
            )
            await session.commit()
            return result

    try:
        for _ in range(blocked_count // 2 + 1):
            await attempt()  # persisted fairness, including the direct caller
            if sent:
                break
            async with factory() as session:
                assert (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM rate_counters WHERE scope='tg_chat' AND key=:b"
                        ),
                        {"b": binding},
                    )
                ).scalar_one() == 0
        assert sent == [ready]
        async with factory() as session:
            actual = (
                await session.execute(
                    text(
                        "SELECT state,attempts,last_failure_class FROM channel_outbox WHERE id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": rows},
                )
            ).all()
            assert actual == [("pending", 0, None)] * blocked_count
        denied.clear()
        await attempt()
        assert len(sent) == 2 and sent[1] in rows
    finally:
        await engine.dispose()


async def test_media_admission_touch_uses_wallclock_in_old_transaction(outbox_db):
    from sqlalchemy import text

    binding = _new_binding(outbox_db)
    row = _enqueue(
        outbox_db, binding=binding, kind="approval_prompt", intent_id=str(uuid.uuid4())
    )
    engine = _engine(outbox_db)
    try:
        async with engine.connect() as conn:
            await _tenant(conn, outbox_db)
            before = (
                await conn.execute(
                    text("SELECT created_at FROM channel_outbox WHERE id=:id"),
                    {"id": row},
                )
            ).scalar_one()
            later = _enqueue(outbox_db, binding=binding)
            later_at = (
                await conn.execute(
                    text("SELECT created_at FROM channel_outbox WHERE id=:id"),
                    {"id": later},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "UPDATE channel_outbox SET updated_at=clock_timestamp() WHERE id=:id AND state='pending'"
                ),
                {"id": row},
            )
            touched = (
                await conn.execute(
                    text(
                        "SELECT created_at,updated_at FROM channel_outbox WHERE id=:id"
                    ),
                    {"id": row},
                )
            ).one()
            assert touched.created_at == before and touched.updated_at >= later_at
            await conn.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("unexpected", [False, True])
async def test_selected_past_posts_no_send_restores_attempt_but_keeps_admitted_rate_charge(
    outbox_db, unexpected
):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.services.target import outbox

    binding = _new_binding(outbox_db)
    row_id = _enqueue(outbox_db, binding=binding, payload={"v": 1, "text": "synthetic"})
    engine = _engine(outbox_db)
    factory = _tenant_session_factory(
        outbox_db, async_sessionmaker(engine, expire_on_commit=False)
    )

    async def no_send(row):
        error = outbox.MediaAdmissionNotSent("past_posts_revalidation_required")
        error.preparation_failed = unexpected
        raise error

    poller = outbox.OutboxPoller(
        factory,
        binding_id=binding,
        transport=no_send,
        clock=_now,
        interval_seconds=1,
        chat_limit=20,
        chat_window_seconds=60,
        global_limit=30,
        global_window_seconds=1,
    )
    try:
        result = await poller.tick()
        assert (
            result["state"] == "media_deferred"
            and poller.deferred == 1
            and poller.sent == 0
        )
        assert poller.consecutive_failures == int(unexpected)
        async with factory() as session:
            current = (
                await session.execute(
                    text(
                        "SELECT state,attempts,last_failure_class,last_failed_at FROM channel_outbox WHERE id=:id"
                    ),
                    {"id": row_id},
                )
            ).one()
            assert tuple(current) == ("pending", 0, None, None)
            assert (
                await session.execute(
                    text(
                        "SELECT count FROM rate_counters WHERE scope='tg_chat' AND key=:b"
                    ),
                    {"b": binding},
                )
            ).scalar_one() == 1
    finally:
        await engine.dispose()
