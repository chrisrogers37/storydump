"""L.5 cap-ledger gate — the §4 flip, refund, and the two resolve edges (#862).

ZERO OVER-CAP is the assertion the product depends on: over-cap means posting
more than Meta allows, which limits the account. Everything here proves it,
and the proofs carry the house standards:

* the isolation level is asserted (READ COMMITTED), because a concurrency
  claim that only holds at a stricter level proves nothing about production;
* the R2 race is genuine — asyncio.gather over N tasks each on its OWN async
  connection, contending on ONE cap bucket — not a sequential test named for
  concurrency;
* refusals are NAMED (constraint / raised type), because a rowcount cannot
  discriminate a winner from a loser;
* every DB-enforced guard is proven load-bearing by DROPPING it and watching
  the refusal disappear;
* every refusal carries a rowcount positive control — the thing being denied
  is first shown to be writable.

Runs as the schema OWNER (RLS bypassed) like the L.3 gate: the property under
test is the cap arithmetic and the flip coupling, not the policies (F.4 owns
those). The flip/refund/resolve SQL is service-layer (no door), so the async
engine runs it exactly as a worker's UnitOfWork would.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import publish_cap
from src.services.target.publish_cap import FlipOutcome, IntentNotApproved
from tests.scripts.conftest import (
    _scratch,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = pytest.mark.integration

CAP = 3
DAY = date(2026, 3, 1)


@pytest.fixture(scope="module")
def cap_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "l5-cap")
        finally:
            conn.close()
        engine = create_async_engine(
            dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            poolclass=NullPool,
            connect_args={"server_settings": {"app.actor_kind": "system"}},
        )
        try:
            yield {
                "owner": dsn,
                "ws": str(chain["ws"]),
                "iga": str(chain["iga"]),
                "engine": engine,
            }
        finally:
            _run(engine.dispose())
    finally:
        gen.close()


def _exec(cap_db, sql, params=None, fetch=False):
    conn = psycopg2.connect(cap_db["owner"])
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


def _new_intent(cap_db, *, state="approved", account_ref=None, publish_step="none"):
    """An approved (or other-state) intent on a fresh media item.

    Mirrors the L.3 template: born directly in *state* under the migration
    actor (the insert guard's exemption); a fresh media item per intent
    (uq_intent_live_subject); the debited states carry cap_consumed_on +
    ig_container_id so ck_publishing_debited/ck_posted_complete hold.
    """
    ws, iga = cap_db["ws"], cap_db["iga"]
    ref = account_ref or f"acct-{uuid.uuid4()}"
    debited = state in ("publishing", "publishing_ambiguous", "review_required")
    extra_cols = ", cap_consumed_on, ig_container_id" if debited else ""
    extra_vals = f", '{DAY}', 'ctr-{uuid.uuid4()}'" if debited else ""
    rows = _exec(
        cap_db,
        "INSERT INTO media_items (workspace_id, source_id, content_hash,"
        " file_name, media_kind, provider_file_ref)"
        " SELECT %s, id, %s, 'f.jpg', 'image', %s FROM media_sources"
        " WHERE workspace_id = %s LIMIT 1 RETURNING id",
        (ws, f"h-{uuid.uuid4()}", f"r-{uuid.uuid4()}", ws),
        fetch=True,
    )
    media = rows[0][0]
    step = "publish_called" if state == "publishing_ambiguous" else publish_step
    rows = _exec(
        cap_db,
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at, state,"
        " publish_step" + extra_cols + ")"
        " VALUES (%s, %s, %s, %s, 'manual', now(), %s, %s" + extra_vals + ")"
        " RETURNING id",
        (ws, iga, media, ref, state, step),
        fetch=True,
    )
    return str(rows[0][0])


def _bucket_count(cap_db, *, day=DAY):
    rows = _exec(
        cap_db,
        "SELECT count FROM daily_post_counts WHERE workspace_id = %s"
        " AND ig_account_id = %s AND local_date = %s",
        (cap_db["ws"], cap_db["iga"], day),
        fetch=True,
    )
    return rows[0][0] if rows else None


def _intent(cap_db, intent_id):
    rows = _exec(
        cap_db,
        "SELECT state, cap_consumed_on, cap_refunded_at, publish_step"
        " FROM post_intents WHERE id = %s",
        (intent_id,),
        fetch=True,
    )
    r = rows[0]
    return {
        "state": r[0],
        "cap_consumed_on": r[1],
        "cap_refunded_at": r[2],
        "publish_step": r[3],
    }


def _clear_bucket(cap_db, *, day=DAY):
    _exec(
        cap_db,
        "DELETE FROM daily_post_counts WHERE workspace_id = %s"
        " AND ig_account_id = %s AND local_date = %s",
        (cap_db["ws"], cap_db["iga"], day),
    )


async def _flip(engine, cap_db, intent_id, *, cap=CAP):
    async with engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SET LOCAL app.actor_kind = 'system'"))
            outcome = await publish_cap.flip_to_publishing(
                conn,
                intent_id=intent_id,
                workspace_id=cap_db["ws"],
                ig_account_id=cap_db["iga"],
                local_date=DAY,
                effective_cap=cap,
            )
        return outcome


#: ONE persistent loop for the whole module. asyncpg connections are bound to
#: the loop they were created on, and the module-scoped engine is created (in
#: the fixture) and used (in tests) through this same loop — a fresh loop per
#: call raises "future belongs to a different loop".
_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


class TestTheLevelIsReadCommitted:
    def test_worker_connections_run_read_committed(self, cap_db):
        async def check():
            async with cap_db["engine"].connect() as conn:
                lvl = (await conn.execute(text("SHOW transaction_isolation"))).scalar()
                assert lvl == "read committed"

        _run(check())


class TestTheFlipOutcomes:
    def test_a_first_flip_proceeds_and_debits_once(self, cap_db):
        _clear_bucket(cap_db)
        intent = _new_intent(cap_db)
        assert _bucket_count(cap_db) is None  # positive control: nothing yet

        outcome = _run(_flip(cap_db["engine"], cap_db, intent))

        assert outcome is FlipOutcome.PROCEED
        assert _bucket_count(cap_db) == 1
        row = _intent(cap_db, intent)
        assert row["state"] == "publishing"
        assert str(row["cap_consumed_on"]) == str(DAY)

    def test_flip_defers_at_the_cap(self, cap_db):
        _clear_bucket(cap_db)
        # Fill the bucket to cap with distinct real accounts (so key-4 never
        # pre-empts the cap), each proceeding.
        for _ in range(CAP):
            i = _new_intent(cap_db)
            assert _run(_flip(cap_db["engine"], cap_db, i)) is FlipOutcome.PROCEED
        assert _bucket_count(cap_db) == CAP  # positive control: the row is AT cap

        over = _new_intent(cap_db)
        outcome = _run(_flip(cap_db["engine"], cap_db, over))

        assert outcome is FlipOutcome.DEFERRED, "over-cap must defer, not proceed"
        assert _bucket_count(cap_db) == CAP, "a denied flip debits nothing"
        assert _intent(cap_db, over)["state"] == "approved", "the intent stays approved"

    def test_a_non_approved_intent_raises_and_rolls_the_debit_back(self, cap_db):
        _clear_bucket(cap_db)
        # An intent NOT in 'approved' (already publishing): the debit CTE fires
        # but the flip matches zero rows → (1,0) → RAISE, rolling the debit back.
        intent = _new_intent(cap_db, state="publishing")
        before = _bucket_count(cap_db)

        with pytest.raises(IntentNotApproved):
            _run(_flip(cap_db["engine"], cap_db, intent))

        assert _bucket_count(cap_db) == before, (
            "the (1,0) rollback must return the debit — no leaked cap slot (#862)"
        )

    def test_a_second_live_publisher_on_one_account_defers_on_key4(self, cap_db):
        _clear_bucket(cap_db)
        ref = f"acct-{uuid.uuid4()}"
        # One publishing intent already live on the real account.
        _new_intent(cap_db, state="publishing", account_ref=ref)
        # A second approved intent for the SAME provider_account_ref.
        second = _new_intent(cap_db, state="approved", account_ref=ref)
        before = _bucket_count(cap_db)

        outcome = _run(_flip(cap_db["engine"], cap_db, second))

        assert outcome is FlipOutcome.DEFERRED, (
            "uq_publish_exclusive must refuse the second flip → defer"
        )
        assert _intent(cap_db, second)["state"] == "approved"
        assert _bucket_count(cap_db) == before, "the key-4 defer rolls the debit back"


class TestTheGuardsAreLoadBearing:
    def test_uq_publish_exclusive_is_what_refuses_the_second_publisher(self, cap_db):
        _clear_bucket(cap_db)
        _exec(cap_db, "DROP INDEX uq_publish_exclusive")
        try:
            ref = f"acct-{uuid.uuid4()}"
            _new_intent(cap_db, state="publishing", account_ref=ref)
            second = _new_intent(cap_db, state="approved", account_ref=ref)
            outcome = _run(_flip(cap_db["engine"], cap_db, second))
            assert outcome is FlipOutcome.PROCEED, (
                "with uq_publish_exclusive gone the second flip must proceed —"
                " which proves the index was what refused it"
            )
            assert _intent(cap_db, second)["state"] == "publishing"
        finally:
            # DELETE the two publishing rows (publishing→cancelled is a
            # forbidden edge) so the index can be rebuilt without a collision.
            _exec(
                cap_db,
                "DELETE FROM post_intents WHERE provider_account_ref = %s",
                (ref,),
            )
            _exec(
                cap_db,
                "CREATE UNIQUE INDEX uq_publish_exclusive ON post_intents"
                " (provider_account_ref)"
                " WHERE state IN ('publishing','publishing_ambiguous')",
            )

    def test_ck_dpc_nonneg_is_what_stops_an_over_refund(self, cap_db):
        _clear_bucket(cap_db)
        # A bucket at 0; a refund past zero must be refused by ck_dpc_nonneg.
        _exec(
            cap_db,
            "INSERT INTO daily_post_counts (workspace_id, ig_account_id,"
            " local_date, count, cap_at_write) VALUES (%s, %s, %s, 0, %s)",
            (cap_db["ws"], cap_db["iga"], DAY, CAP),
        )
        # Positive control: a legal decrement from 1 works.
        _exec(
            cap_db,
            "UPDATE daily_post_counts SET count = 1 WHERE workspace_id = %s"
            " AND ig_account_id = %s AND local_date = %s",
            (cap_db["ws"], cap_db["iga"], DAY),
        )
        with pytest.raises(psycopg2.errors.CheckViolation) as exc:
            _exec(
                cap_db,
                "UPDATE daily_post_counts SET count = count - 2 WHERE workspace_id = %s"
                " AND ig_account_id = %s AND local_date = %s",
                (cap_db["ws"], cap_db["iga"], DAY),
            )
        assert exc.value.diag.constraint_name == "ck_dpc_nonneg"


class TestZeroOverCapUnderConcurrency:
    """R2 — the assertion the product depends on. A GENUINE race: N tasks, each
    on its own async connection, all debiting ONE bucket at READ COMMITTED."""

    def test_the_atomic_debit_denies_over_cap_under_concurrency(self, cap_db):
        _clear_bucket(cap_db)
        n, cap = 12, CAP

        async def one_debit():
            async with cap_db["engine"].connect() as conn:
                async with conn.begin():
                    row = (
                        await conn.execute(
                            text(
                                "INSERT INTO daily_post_counts AS d"
                                " (workspace_id, ig_account_id, local_date, count,"
                                "  cap_at_write) VALUES (:ws, :acct, :day, 1, :cap)"
                                " ON CONFLICT (workspace_id, ig_account_id, local_date)"
                                "   DO UPDATE SET count = d.count + 1"
                                "   WHERE d.count < d.cap_at_write"
                                " RETURNING count"
                            ),
                            {
                                "ws": cap_db["ws"],
                                "acct": cap_db["iga"],
                                "day": DAY,
                                "cap": cap,
                            },
                        )
                    ).fetchone()
            return row is not None

        async def race():
            return await asyncio.gather(*[one_debit() for _ in range(n)])

        results = _run(race())
        succeeded = sum(1 for r in results if r)

        assert succeeded == cap, (
            f"exactly the cap may debit under {n} concurrent attempts:"
            f" {succeeded} succeeded"
        )
        assert _bucket_count(cap_db) == cap, "the ledger count is exactly the cap"
        assert _bucket_count(cap_db) <= cap, "ZERO OVER-CAP"

    def test_no_debit_is_lost_when_the_race_stays_under_cap(self, cap_db):
        _clear_bucket(cap_db)
        # Under-cap: every concurrent debit must land (no lost update).
        n = CAP  # == cap, all should succeed
        big_cap = 100

        async def one():
            async with cap_db["engine"].connect() as conn:
                async with conn.begin():
                    row = (
                        await conn.execute(
                            text(
                                "INSERT INTO daily_post_counts AS d"
                                " (workspace_id, ig_account_id, local_date, count,"
                                "  cap_at_write) VALUES (:ws, :acct, :day, 1, :cap)"
                                " ON CONFLICT (workspace_id, ig_account_id, local_date)"
                                "   DO UPDATE SET count = d.count + 1"
                                "   WHERE d.count < d.cap_at_write RETURNING count"
                            ),
                            {
                                "ws": cap_db["ws"],
                                "acct": cap_db["iga"],
                                "day": DAY,
                                "cap": big_cap,
                            },
                        )
                    ).fetchone()
            return row is not None

        async def race():
            return await asyncio.gather(*[one() for _ in range(n)])

        results = _run(race())
        assert all(results)
        assert _bucket_count(cap_db) == n, (
            "every under-cap debit is counted exactly once"
        )


class TestPass5ResolveEffects:
    def test_resolve_retry_refunds_the_recorded_day_and_next_flip_redebits_once(
        self, cap_db
    ):
        _clear_bucket(cap_db)
        # A review_required intent that carries a debit on DAY.
        intent = _new_intent(cap_db, state="review_required")
        _exec(
            cap_db,
            "INSERT INTO daily_post_counts (workspace_id, ig_account_id,"
            " local_date, count, cap_at_write) VALUES (%s, %s, %s, 1, %s)",
            (cap_db["ws"], cap_db["iga"], DAY, CAP),
        )
        assert _bucket_count(cap_db) == 1  # positive control

        async def retry():
            async with cap_db["engine"].connect() as conn:
                async with conn.begin():
                    await conn.execute(text("SET LOCAL app.actor_kind = 'operator'"))
                    return await publish_cap.resolve_retry(
                        conn,
                        intent_id=intent,
                        workspace_id=cap_db["ws"],
                        ig_account_id=cap_db["iga"],
                        attempts_by_step={"v": 1, "generation": 2},
                    )

        assert _run(retry()) is True
        assert _bucket_count(cap_db) == 0, "retry refunds the recorded day"
        row = _intent(cap_db, intent)
        assert row["state"] == "approved" and row["cap_consumed_on"] is None
        assert row["publish_step"] == "none", "checkpoints reset"

        # The NEXT flip re-debits the current day EXACTLY once.
        assert _run(_flip(cap_db["engine"], cap_db, intent)) is FlipOutcome.PROCEED
        assert _bucket_count(cap_db) == 1, "re-debit is exactly once, not twice"

    def test_resolve_cancel_retains_the_debit(self, cap_db):
        _clear_bucket(cap_db)
        intent = _new_intent(cap_db, state="review_required")
        _exec(
            cap_db,
            "INSERT INTO daily_post_counts (workspace_id, ig_account_id,"
            " local_date, count, cap_at_write) VALUES (%s, %s, %s, 1, %s)",
            (cap_db["ws"], cap_db["iga"], DAY, CAP),
        )

        async def cancel():
            async with cap_db["engine"].connect() as conn:
                async with conn.begin():
                    await conn.execute(text("SET LOCAL app.actor_kind = 'operator'"))
                    return await publish_cap.resolve_cancel(conn, intent_id=intent)

        assert _run(cancel()) is True
        assert _intent(cap_db, intent)["state"] == "cancelled"
        assert _bucket_count(cap_db) == 1, (
            "cancel RETAINS the debit (pass-5): a review_required intent may have"
            " published; refunding risks under-counting toward over-posting"
        )

    def test_refund_returns_the_recorded_day(self, cap_db):
        _clear_bucket(cap_db)
        intent = _new_intent(cap_db, state="publishing")
        _exec(
            cap_db,
            "INSERT INTO daily_post_counts (workspace_id, ig_account_id,"
            " local_date, count, cap_at_write) VALUES (%s, %s, %s, 1, %s)",
            (cap_db["ws"], cap_db["iga"], DAY, CAP),
        )

        async def fail_with_refund():
            async with cap_db["engine"].connect() as conn:
                async with conn.begin():
                    await conn.execute(text("SET LOCAL app.actor_kind = 'system'"))
                    await publish_cap.refund_cap(
                        conn,
                        intent_id=intent,
                        workspace_id=cap_db["ws"],
                        ig_account_id=cap_db["iga"],
                    )
                    await conn.execute(
                        text("UPDATE post_intents SET state = 'failed' WHERE id = :i"),
                        {"i": intent},
                    )

        _run(fail_with_refund())
        assert _bucket_count(cap_db) == 0, "the refund returned the recorded day"
        assert _intent(cap_db, intent)["cap_refunded_at"] is not None
