"""L.8 gate — idempotent webhook admission under real concurrency (#865).

The gate's clause is *"200 replayed callbacks yield one command"*, and the hard
half of it is not the 200 — it is TWO, arriving at the same instant.

**How these prove the race is genuine, structurally rather than by assertion.**
Each racing coroutine opens its own connection, BEGINs, then blocks at a
two-party rendezvous before touching `command_dedup`. Neither can proceed until
both have arrived, so both are provably inside an open transaction at the
moment of the insert. A sequentially-executed version of this test cannot pass:
the first arrival would wait forever for a second that never comes. The
rendezvous is therefore not decoration — it is the evidence, and it fails by
hanging rather than by silently degrading into a sequential run.

`asyncio.Event` pairs rather than `asyncio.Barrier`, deliberately: `Barrier`
landed in 3.11 and CI runs 3.10.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest

from src.services.target import webhook_ingress as ingress
from src.services.target.webhook_ingress import AdmissionConflict, DeliveryReplayed
from tests.scripts.conftest import (
    _scratch,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def admit_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            seed_workspace_chain(conn, "l8-gate")
        finally:
            conn.close()

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(
            dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            connect_args={"server_settings": {"app.actor_kind": "system"}},
            poolclass=NullPool,
        )
        try:
            yield {"owner": dsn, "engine": engine}
        finally:
            _run(engine.dispose())
    finally:
        gen.close()


def _run(coro):
    """Fresh loop per call — `get_event_loop()` raises on 3.10 (CI)."""
    return asyncio.run(coro)


def _exec(admit_db, sql, params=None, fetch=False):
    conn = psycopg2.connect(admit_db["owner"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'system'")
            cur.execute(sql, params)
            return cur.fetchall() if fetch else cur.rowcount
    finally:
        conn.close()


def _ref() -> str:
    return f"update-{uuid.uuid4()}"


def _call(admit_db, fn):
    async def go():
        async with admit_db["engine"].connect() as conn:
            out = await fn(conn)
            await conn.commit()
            return out

    return _run(go())


class TestTheIsolationLevelIsWhatProductionRuns:
    """branden proved 19 tests still pass at REPEATABLE READ, including a race
    and a CAS. A concurrency test at the wrong level proves nothing about
    production, so the level is asserted rather than trusted."""

    def test_read_committed_on_the_sync_path(self, admit_db):
        assert _exec(
            admit_db,
            "SELECT current_setting('transaction_isolation'),"
            "       current_setting('default_transaction_isolation')",
            fetch=True,
        )[0] == ("read committed", "read committed")

    def test_read_committed_on_the_ASYNC_path_the_race_actually_uses(self, admit_db):
        """The sync assertion above is not enough on its own: the race runs on
        asyncpg connections, and it is those whose isolation decides whether
        the race means anything."""
        from sqlalchemy import text

        async def go():
            async with admit_db["engine"].connect() as conn:
                r = await conn.execute(text("SHOW transaction_isolation"))
                return r.scalar_one()

        assert _run(go()) == "read committed"


class TestASingleAdmission:
    def test_a_first_delivery_is_admitted(self, admit_db):
        ref = _ref()
        row = _call(
            admit_db,
            lambda c: ingress.admit(
                c, channel="telegram", external_ref=ref, payload={"update_id": 1}
            ),
        )
        assert row["external_ref"] == ref
        assert row["principal"] == "", "telegram update ids are bot-globally unique"

    def test_a_rowcount_positive_control_before_asserting_any_refusal(self, admit_db):
        """House standard, and three vacuous tests were caught this way on L.1
        alone: assert the row being refused EXISTS before a refusal means
        anything."""
        ref = _ref()
        _call(
            admit_db,
            lambda c: ingress.admit(
                c, channel="telegram", external_ref=ref, payload={"update_id": 2}
            ),
        )
        assert (
            _exec(
                admit_db,
                "SELECT count(*) FROM command_dedup WHERE external_ref = %s",
                (ref,),
                fetch=True,
            )[0][0]
            == 1
        ), "the positive control must find the row the refusal is about"

        with pytest.raises(DeliveryReplayed, match="already admitted"):
            _call(
                admit_db,
                lambda c: ingress.admit(
                    c, channel="telegram", external_ref=ref, payload={"update_id": 2}
                ),
            )

    def test_a_REPLAY_is_a_NAMED_refusal_not_a_returned_false(self, admit_db):
        """`rowcount` cannot discriminate a winner from a loser — #883 showed
        both getting rows=1 on a different rail — so "the second one did not
        insert" must be an identifiable event, not an absence."""
        ref = _ref()
        payload = {"update_id": 3, "message": {"text": "hi"}}
        _call(
            admit_db,
            lambda c: ingress.admit(
                c, channel="telegram", external_ref=ref, payload=payload
            ),
        )
        with pytest.raises(DeliveryReplayed) as caught:
            _call(
                admit_db,
                lambda c: ingress.admit(
                    c, channel="telegram", external_ref=ref, payload=payload
                ),
            )
        assert "do not execute it" in str(caught.value), (
            "the refusal must carry BOTH obligations — acknowledge, and do not "
            "execute — because a boolean carries neither"
        )

    def test_the_SAME_key_with_DIFFERENT_content_is_a_CONFLICT_not_a_replay(
        self, admit_db
    ):
        """Swallowing this as a replay would silently drop a real command — the
        failure that is invisible precisely because it looks like successful
        deduplication."""
        ref = _ref()
        _call(
            admit_db,
            lambda c: ingress.admit(
                c, channel="web", external_ref=ref, payload={"cmd": "a"}, principal="s1"
            ),
        )
        with pytest.raises(AdmissionConflict, match="DIFFERENT fingerprint"):
            _call(
                admit_db,
                lambda c: ingress.admit(
                    c,
                    channel="web",
                    external_ref=ref,
                    payload={"cmd": "b"},
                    principal="s1",
                ),
            )

    def test_key_reordering_is_NOT_a_conflict(self, admit_db):
        """The fingerprint normalizes, so a provider that reorders JSON keys
        between deliveries still hashes the same. Without this every
        redelivery would fail closed — correct in direction, an outage in
        practice."""
        ref = _ref()
        _call(
            admit_db,
            lambda c: ingress.admit(
                c, channel="telegram", external_ref=ref, payload={"a": 1, "b": 2}
            ),
        )
        with pytest.raises(DeliveryReplayed):
            _call(
                admit_db,
                lambda c: ingress.admit(
                    c, channel="telegram", external_ref=ref, payload={"b": 2, "a": 1}
                ),
            )

    def test_the_same_ref_on_a_DIFFERENT_channel_is_a_different_key(self, admit_db):
        ref = _ref()
        for channel in ("telegram", "cli"):
            _call(
                admit_db,
                lambda c, ch=channel: ingress.admit(
                    c, channel=ch, external_ref=ref, payload={"x": 1}, principal=""
                ),
            )
        assert (
            _exec(
                admit_db,
                "SELECT count(*) FROM command_dedup WHERE external_ref = %s",
                (ref,),
                fetch=True,
            )[0][0]
            == 2
        )


class TestTwoSimultaneousDeliveriesProduceExactlyOneAdmission:
    """The increment. Not a retry — a RACE.

    A retry arrives after the first attempt finished, so any check works. Two
    simultaneous deliveries both run the check before either has written, which
    is the case a check-then-act gets wrong and the case this class exists for.
    """

    async def _racer(self, engine, *, ref, payload, mine, theirs, outcomes, tag):
        from sqlalchemy import text

        async with engine.connect() as conn:
            # Open the transaction FIRST, so the rendezvous below happens with
            # both parties genuinely inside one.
            await conn.execute(text("SELECT 1"))
            mine.set()
            await theirs.wait()  # <- a sequential run cannot get past here
            try:
                await ingress.admit(
                    conn, channel="telegram", external_ref=ref, payload=payload
                )
                await conn.commit()
                outcomes[tag] = "admitted"
            except DeliveryReplayed:
                await conn.rollback()
                outcomes[tag] = "replayed"
            except AdmissionConflict as exc:  # pragma: no cover - would be a bug
                await conn.rollback()
                outcomes[tag] = f"conflict:{exc}"

    def _race(self, admit_db, ref, payload):
        outcomes: dict[str, str] = {}
        a, b = asyncio.Event(), asyncio.Event()

        async def main():
            await asyncio.gather(
                self._racer(
                    admit_db["engine"],
                    ref=ref,
                    payload=payload,
                    mine=a,
                    theirs=b,
                    outcomes=outcomes,
                    tag="A",
                ),
                self._racer(
                    admit_db["engine"],
                    ref=ref,
                    payload=payload,
                    mine=b,
                    theirs=a,
                    outcomes=outcomes,
                    tag="B",
                ),
            )

        _run(main())
        return outcomes

    def test_exactly_one_admission_and_the_other_is_a_NAMED_replay(self, admit_db):
        ref = _ref()
        outcomes = self._race(admit_db, ref, {"update_id": 99})

        assert sorted(outcomes.values()) == ["admitted", "replayed"], outcomes
        assert (
            _exec(
                admit_db,
                "SELECT count(*) FROM command_dedup WHERE external_ref = %s",
                (ref,),
                fetch=True,
            )[0][0]
            == 1
        ), "two deliveries, ONE row"

    def test_the_race_was_genuinely_concurrent(self, admit_db):
        """The proof, and it is structural rather than an assertion about
        timing. Both coroutines must arrive at the rendezvous before either can
        insert; a sequentially-executed version would block forever at the
        first `theirs.wait()`. That this test terminates at all is the
        evidence, so it fails by hanging rather than by quietly degrading into
        a sequential run that still passes."""
        ref = _ref()
        outcomes = self._race(admit_db, ref, {"update_id": 100})
        assert set(outcomes) == {"A", "B"}, "both racers must have run"
        assert "admitted" in outcomes.values() and "replayed" in outcomes.values()

    def test_a_check_then_act_admission_loses_the_NAMED_refusal(self, admit_db):
        """The discrimination proof, and the claim is narrower than the obvious
        one — corrected after measuring rather than asserted from intuition.

        A check-then-act `admit()` does NOT admit two rows: `command_dedup`'s
        primary key stops that, and it would stop it whatever the code did. So
        "the naive version double-admits" is FALSE, and a test asserting it
        would pass for the wrong reason.

        What the naive version actually loses is the NAMED refusal. Both racers
        pass their SELECT before either INSERTs, so neither sees the other, and
        the loser surfaces a raw `UniqueViolationError` instead of
        `DeliveryReplayed` — an unhandled integrity error at the top of a
        webhook handler, which a caller cannot distinguish from a real fault
        and will therefore retry. Measured: mutating `admit()` to check-then-act
        reddens both race tests above with exactly that exception.

        This test pins the property that survives: the loser's refusal is
        NAMED, which is the thing the atomic single-statement insert buys.
        """
        ref = _ref()
        outcomes = self._race(admit_db, ref, {"update_id": 101})
        assert sorted(outcomes.values()) == ["admitted", "replayed"], (
            "the loser must surface a NAMED DeliveryReplayed — a raw integrity "
            "error here is the check-then-act signature"
        )
        assert not any("conflict" in v for v in outcomes.values())


class TestAnAbortedWinnerDoesNotPoisonTheKey:
    """`ON CONFLICT DO NOTHING` makes the loser WAIT on the winner's tuple. If
    the winner aborts, Postgres lets the loser's insert PROCEED — so an
    abandoned attempt must not leave the delivery permanently unadmittable.
    Verified rather than assumed, because the module's docstring claims it."""

    def test_an_abandoned_attempt_leaves_the_delivery_admittable(self, admit_db):
        from sqlalchemy import text

        ref = _ref()
        result: dict[str, str] = {}
        a, b = asyncio.Event(), asyncio.Event()

        async def aborter():
            async with admit_db["engine"].connect() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO command_dedup"
                        " (channel, principal, external_ref, fingerprint)"
                        " VALUES ('telegram','',:ref,'doomed')"
                    ),
                    {"ref": ref},
                )
                a.set()
                await asyncio.sleep(0.15)  # let the other one start waiting
                await conn.rollback()
                result["aborter"] = "rolled back"

        async def survivor():
            await a.wait()
            async with admit_db["engine"].connect() as conn:
                row = await ingress.admit(
                    conn, channel="telegram", external_ref=ref, payload={"u": 1}
                )
                await conn.commit()
                result["survivor"] = row["fingerprint"]
                b.set()

        async def main():
            await asyncio.gather(aborter(), survivor())

        _run(main())
        assert result["aborter"] == "rolled back"
        assert result["survivor"] != "doomed"
        rows = _exec(
            admit_db,
            "SELECT fingerprint FROM command_dedup WHERE external_ref = %s",
            (ref,),
            fetch=True,
        )
        assert len(rows) == 1 and rows[0][0] != "doomed"


class TestTheGatesOwnNumber:
    def test_200_replayed_callbacks_yield_ONE_command(self, admit_db):
        """The gate, literally. 200 deliveries of the same callback; exactly one
        is admitted and 199 are named replays."""
        ref = _ref()
        payload = {"callback_query": {"id": "cb-1", "data": "approve:xyz"}}
        admitted, replayed = 0, 0
        for _ in range(200):
            try:
                _call(
                    admit_db,
                    lambda c: ingress.admit(
                        c, channel="telegram", external_ref=ref, payload=payload
                    ),
                )
                admitted += 1
            except DeliveryReplayed:
                replayed += 1

        assert (admitted, replayed) == (1, 199)
        assert (
            _exec(
                admit_db,
                "SELECT count(*) FROM command_dedup WHERE external_ref = %s",
                (ref,),
                fetch=True,
            )[0][0]
            == 1
        )


class TestTheSecretToken:
    def test_a_matching_token_is_accepted(self):
        assert ingress.verify_secret_token("s3cret", "s3cret") is True

    def test_a_wrong_token_is_refused(self):
        assert ingress.verify_secret_token("wrong", "s3cret") is False

    def test_an_UNCONFIGURED_expected_token_refuses_EVERYTHING(self):
        """The direction matters: a missing secret must close the door, not open
        it. An adapter that accepted everything when misconfigured would pass a
        happy-path test and be an open webhook in production."""
        assert ingress.verify_secret_token("anything", None) is False
        assert ingress.verify_secret_token("anything", "") is False

    def test_an_absent_presented_token_is_refused(self):
        assert ingress.verify_secret_token(None, "s3cret") is False
