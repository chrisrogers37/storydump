"""L.3 gate — the permit rail and the reconciler, against a real database (#860).

The gate, verbatim: *"kill/drop tests at every Meta boundary issue at most one
publish call — including kill-between-permit-and-call, which must land in
`publishing_ambiguous` with ZERO retries and then be resolved by the reconciler
from stubbed evidence in both modes."*

**Scope stated rather than implied.** These run as the schema OWNER, so RLS is
bypassed. That is deliberate: the property under test is the permit protocol and
at-most-once, and F.4's runtime harness already exercises every policy on this
table as the declared logins. Mixing the two here would make a permit failure
and a policy failure indistinguishable.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest

from src.services.target import provider_ops
from src.services.target.provider_ops import PermitRefused
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ops_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "l3-gate")
        finally:
            conn.close()
        from sqlalchemy.pool import NullPool
        from sqlalchemy.ext.asyncio import create_async_engine

        # ONE engine for the module, disposed at teardown. A per-test engine
        # leaks its asyncpg connections, and pytest raises the resulting
        # ResourceWarning as an error — which reads as a logic failure in
        # whichever test happens to run when the garbage collector notices.
        engine = create_async_engine(
            dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            # NullPool is PRECAUTIONARY, and saying so matters because I
            # briefly believed otherwise. The kill-mid-transaction test
            # abandons a connection while it holds FOR SHARE on a jobs row;
            # pooled, such a connection can return to the pool with its
            # transaction open and block the next test on the lock. That
            # hazard is real and NullPool removes it by construction.
            #
            # It is NOT what fixed the hang I chased here. That was
            # environmental: two backends left idle-in-transaction for 36
            # minutes by test runs I had killed, blocking a DROP. With those
            # terminated, both files pass together in 8.6s — with and without
            # this. Recorded rather than quietly claimed as the fix, because a
            # comment asserting an unmeasured cause is the same defect as a
            # docstring asserting an unmeasured behaviour.
            poolclass=NullPool,
            # Actor context is established at CONNECT, the way a worker's unit
            # of work establishes it. The rail deliberately does not invent one:
            # a service that invented an actor to get past a governance guard
            # would have defeated the guard.
            connect_args={"server_settings": {"app.actor_kind": "system"}},
        )
        try:
            yield {
                "owner": dsn,
                "worker": as_user(db, "svc_worker"),
                "ws": chain["ws"],
                "iga": chain["iga"],
                "media": chain["media"],
                "engine": engine,
            }
        finally:
            _run(engine.dispose())
    finally:
        gen.close()


def _exec(ops_db, sql, params=None, fetch=False):
    conn = psycopg2.connect(ops_db["owner"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        conn.close()


def _engine(ops_db):
    return ops_db["engine"]


def _new_intent(ops_db, state="publishing", publish_step=None):
    """An intent born directly in *state*, on its own fresh media item.

    Three things shape this and all three were found by running it:

    * ``trg_intent_insert_guard`` requires intents to be born ``scheduled``.
      Its documented exemption is ``app.actor_kind = 'migration'``, which must
      be set in the SAME session as the insert — the same route L.1's gate uses,
      and for the same reason: these assertions are about the permit rail, not
      about how the row reached its state.
    * ``uq_intent_live_subject`` is unique on (workspace, media, account) for
      non-terminal rows, so every intent gets a NEW media item.
    * ``ck_publishing_debited`` refuses a publishing state without a cap debit,
      and ``ck_posted_complete`` later refuses an `api` post without an
      ``ig_container_id``. A publishing intent carries one because the real
      pipeline created a container before it ever tried to publish.
    """
    ws, iga = str(ops_db["ws"]), str(ops_db["iga"])
    debited = state in ("publishing", "publishing_ambiguous")
    conn = psycopg2.connect(ops_db["owner"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO media_items (workspace_id, source_id, content_hash,"
                " file_name, media_kind, provider_file_ref)"
                " SELECT %s, id, %s, 'f.jpg', 'image', %s FROM media_sources"
                " WHERE workspace_id = %s LIMIT 1 RETURNING id",
                (ws, f"h-{uuid.uuid4()}", f"r-{uuid.uuid4()}", ws),
            )
            media = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
                " provider_account_ref, approval_mode, schedule_slot_at, state,"
                " publish_step"
                + (", cap_consumed_on, ig_container_id" if debited else "")
                + ") VALUES (%s, %s, %s, %s, 'manual', now(), %s, %s"
                + (f", '2026-01-01', 'ctr-{uuid.uuid4()}'" if debited else "")
                + ") RETURNING id",
                (
                    ws,
                    iga,
                    media,
                    f"acct-{uuid.uuid4()}",
                    state,
                    publish_step
                    or (
                        "publish_called" if state == "publishing_ambiguous" else "none"
                    ),
                ),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _leased_job(ops_db, *, live=True):
    """A job holding a live (or expired) lease, and its token."""
    token = str(uuid.uuid4())
    offset = 300 if live else -300
    rows = _exec(
        ops_db,
        "INSERT INTO jobs (workspace_id, kind, lane, serialization_key, payload,"
        " max_attempts, state, lease_token, locked_by, locked_until)"
        " VALUES (%s, 'publish_pipeline', 'interactive', %s, '{\"v\": 1}', 3,"
        " 'leased', %s, 'w1', now() + make_interval(secs => %s)) RETURNING id",
        (str(ops_db["ws"]), f"acct:{uuid.uuid4()}", token, offset),
        fetch=True,
    )
    return rows[0][0], token


def _run(coro):
    """A fresh loop per call.

    NOT ``asyncio.get_event_loop()``: on Python 3.10 that RAISES
    ``RuntimeError: There is no current event loop`` when none is set, while on
    3.11 it still returns one with a DeprecationWarning. Local is 3.11 and CI is
    3.10, so the first version passed 43/43 here and failed 13 tests there —
    the case where local-green is structurally incapable of being evidence.

    Safe with NullPool: every checkout opens its own asyncpg connection and
    closes it on release, so no connection outlives the loop that created it.
    """
    return asyncio.run(coro)


class TestTheIsolationLevelIsWhatProductionRuns:
    """House standard. A concurrency test at REPEATABLE READ proves nothing
    about production — branden showed 19 tests still pass at the wrong level,
    including the race — so the level is asserted rather than assumed."""

    def test_read_committed_on_both_the_session_and_the_default(self, ops_db):
        rows = _exec(
            ops_db,
            "SELECT current_setting('transaction_isolation'),"
            "       current_setting('default_transaction_isolation')",
            fetch=True,
        )
        assert rows[0] == ("read committed", "read committed")


class TestThePermitProtocol:
    def test_a_live_lease_gets_a_permit_and_publish_marks_the_step(self, ops_db):
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                return await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )

        row = _run(go())
        assert row["state"] == "permitted"
        assert row["business_key"] == f"ig:publish:{intent}:1"
        step = _exec(
            ops_db,
            "SELECT publish_step FROM post_intents WHERE id = %s",
            (intent,),
            fetch=True,
        )[0][0]
        assert step == "publish_called", (
            "the permit IS the durable record that a publish may have been "
            "attempted; without this `ck_ambiguous_called` is not exact"
        )

    def test_an_EXPIRED_lease_is_refused_BY_NAME_with_no_permit_written(self, ops_db):
        """The pass-2 form checked id + token only and passed a lease that had
        expired without being reassigned. State AND expiry both matter."""
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db, live=False)
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )

        with pytest.raises(PermitRefused, match="fenced"):
            _run(go())
        assert (
            _exec(
                ops_db,
                "SELECT count(*) FROM provider_operations WHERE intent_id = %s",
                (intent,),
                fetch=True,
            )[0][0]
            == 0
        ), "a refused permit must leave NO row — and therefore no call"

    def test_a_FINALIZED_lease_is_refused_even_though_the_token_still_matches(
        self, ops_db
    ):
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        _exec(ops_db, "UPDATE jobs SET state = 'succeeded' WHERE id = %s", (job,))
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )

        with pytest.raises(PermitRefused, match="fenced"):
            _run(go())


class TestAtMostOnePublishCallAtEveryKillBoundary:
    """The gate's headline. Each test kills at a different Meta boundary and
    asserts the resumed pipeline issues AT MOST ONE publish call — counted, not
    argued, by a stub that records every invocation."""

    def _permit(self, ops_db, intent, kind="publish", generation=1):
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                return await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind=kind,
                    generation=generation,
                )

        return _run(go())

    def test_a_second_permit_for_the_same_generation_is_refused_BY_NAME(self, ops_db):
        """`uq_ops_business_key` is the object that enforces "one business key =
        at most one intended provider effect". A duplicate must surface as a
        named refusal, not as a silently reused row."""
        intent = _new_intent(ops_db)
        self._permit(ops_db, intent)
        with pytest.raises(PermitRefused, match="duplicate permit"):
            self._permit(ops_db, intent)
        assert (
            _exec(
                ops_db,
                "SELECT count(*) FROM provider_operations"
                " WHERE intent_id = %s AND op_kind = 'publish'",
                (intent,),
                fetch=True,
            )[0][0]
            == 1
        )

    def test_kill_BEFORE_the_permit_commits_leaves_no_permit_and_no_call(self, ops_db):
        """Crash before commit: no permit, no call, clean resume. Simulated by
        raising inside the permit transaction, which is the only way the
        pipeline can be interrupted at that boundary."""
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                await provider_ops._lease_is_live(conn, job, token)
                try:
                    raise RuntimeError("killed mid-transaction")
                finally:
                    await conn.rollback()

        with pytest.raises(RuntimeError):
            _run(go())
        assert (
            _exec(
                ops_db,
                "SELECT count(*) FROM provider_operations WHERE intent_id = %s",
                (intent,),
                fetch=True,
            )[0][0]
            == 0
        )

    def test_container_create_resumes_by_bumping_generation_never_parking(self, ops_db):
        """An orphaned container is INERT — nothing is published, containers
        expire unused, and Meta's caps count the publish step only. So the
        resumed pipeline retries with a fresh generation, and the intent is
        never parked. Recoverable effects must not stop the pipeline."""
        intent = _new_intent(ops_db)
        op = self._permit(ops_db, intent, kind="container_create")
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                verdict = await provider_ops.resume_unresolved(
                    conn,
                    op={"id": op["id"], "op_kind": "container_create"},
                    intent_id=intent,
                )
                await conn.commit()
                return verdict

        assert _run(go()) == "repermit"
        state, ref = _exec(
            ops_db,
            "SELECT state, response_ref FROM provider_operations WHERE id = %s",
            (op["id"],),
            fetch=True,
        )[0]
        assert state == "failed" and ref["error"] == "lost_response"
        assert (
            _exec(
                ops_db,
                "SELECT state FROM post_intents WHERE id = %s",
                (intent,),
                fetch=True,
            )[0][0]
            == "publishing"
        ), "a recoverable effect must NOT park the intent"

        # generation 2 is permittable — a fresh business key, so a fresh create.
        second = self._permit(ops_db, intent, kind="container_create", generation=2)
        assert second["business_key"] == f"ig:container:{intent}:2"

    def test_the_call_is_never_made_when_the_permit_is_refused(self, ops_db):
        """The class docstring for TestAtMostOnePublishCallAtEveryKillBoundary
        promises 'counted, not argued, by a stub that records every
        invocation' -- but no test in this file actually wires one up (grep
        confirms 'calls' appears only as an unpopulated list). This wires a
        REAL stub around a caller pattern: acquire the permit, and only if it
        returns successfully, invoke the (stubbed) provider call."""
        intent_ok = _new_intent(ops_db)
        job_ok, token_ok = _leased_job(ops_db)
        intent_fenced = _new_intent(ops_db)
        job_fenced, token_fenced = _leased_job(ops_db, live=False)
        engine = _engine(ops_db)
        provider_calls = []

        async def caller(intent, job, token):
            """The contract from acquire_permit's own docstring: the caller
            may issue the provider call only after acquire_permit returns --
            i.e. only after its transaction has committed."""
            async with engine.connect() as conn:
                try:
                    permit = await provider_ops.acquire_permit(
                        conn,
                        workspace_id=ops_db["ws"],
                        intent_id=intent,
                        job_id=job,
                        lease_token=token,
                        op_kind="publish",
                    )
                except PermitRefused:
                    return "refused"
                # Only reached if acquire_permit returned -- i.e. committed.
                assert permit is not None
                provider_calls.append(intent)
                return "called"

        result_ok = _run(caller(intent_ok, job_ok, token_ok))
        result_fenced = _run(caller(intent_fenced, job_fenced, token_fenced))

        assert result_ok == "called"
        assert result_fenced == "refused"
        assert provider_calls == [intent_ok], (
            "the stub must fire exactly once, for the permit that actually "
            "committed, and never for the fenced/refused one"
        )


class TestKillBetweenPermitAndCall:
    """The named case in the gate, and the sharpest one: the permit committed,
    the network call may or may not have happened, and nobody knows."""

    def _permitted_publish(self, ops_db):
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                return await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )

        return intent, _run(go())

    def test_it_lands_in_publishing_ambiguous_with_ZERO_retries(self, ops_db):
        intent, op = self._permitted_publish(ops_db)
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                verdict = await provider_ops.resume_unresolved(
                    conn, op={"id": op["id"], "op_kind": "publish"}, intent_id=intent
                )
                await conn.commit()
                return verdict

        assert _run(go()) == "parked"
        assert (
            _exec(
                ops_db,
                "SELECT state FROM post_intents WHERE id = %s",
                (intent,),
                fetch=True,
            )[0][0]
            == "publishing_ambiguous"
        )
        assert (
            _exec(
                ops_db,
                "SELECT state FROM provider_operations WHERE id = %s",
                (op["id"],),
                fetch=True,
            )[0][0]
            == "ambiguous"
        )

    def test_generation_NEVER_bumps_out_of_ambiguity(self, ops_db):
        """This is the structural half of at-most-once. `generation` is what
        mints a new business key, and a new business key is what would make a
        second publish permittable at all. Refusing to bump is therefore not a
        policy choice layered on top — it is the mechanism."""
        intent, op = self._permitted_publish(ops_db)
        engine = _engine(ops_db)

        async def park():
            async with engine.connect() as conn:
                await provider_ops.resume_unresolved(
                    conn, op={"id": op["id"], "op_kind": "publish"}, intent_id=intent
                )
                await conn.commit()

        _run(park())
        gens = _exec(
            ops_db,
            "SELECT generation FROM provider_operations"
            " WHERE intent_id = %s AND op_kind = 'publish'",
            (intent,),
            fetch=True,
        )
        assert [g[0] for g in gens] == [1], (
            "a second generation would mint ig:publish:<intent>:2 and make a "
            "second publish call permittable — which is the thing this rail exists "
            "to make impossible"
        )


class TestTheReconcilerResolvesTheAmbiguityInBothModes:
    """The gate's second half. Container-verdict terminalizes from the
    authoritative value; evidence-capture proves the machine parks
    `review_required` with the captured trail INSTEAD of self-terminalizing."""

    def _ambiguous(self, ops_db):
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                op = await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )
                await provider_ops.resume_unresolved(
                    conn, op={"id": op["id"], "op_kind": "publish"}, intent_id=intent
                )
                await conn.commit()
                return op

        return intent, _run(go())

    def _reconcile(self, ops_db, intent, *, status, mode, checks=0):
        from src.services.target import reconciler

        engine = _engine(ops_db)
        seen = []

        async def go():
            async with engine.connect() as conn:
                out = await reconciler.reconcile_intent(
                    conn,
                    intent_id=intent,
                    workspace_id=ops_db["ws"],
                    poll=lambda intent_id: seen.append(intent_id) or status,
                    stories_check=lambda intent_id: {"stories": []},
                    mode=mode,
                    checks=checks,
                )
                await conn.commit()
                return out

        return _run(go()), seen

    def test_container_verdict_terminalizes_posted_from_the_authoritative_value(
        self, ops_db
    ):
        intent, op = self._ambiguous(ops_db)
        outcome, seen = self._reconcile(
            ops_db, intent, status="PUBLISHED", mode="container_verdict"
        )
        assert outcome == "posted" and len(seen) == 1
        assert (
            _exec(
                ops_db,
                "SELECT state FROM post_intents WHERE id = %s",
                (intent,),
                fetch=True,
            )[0][0]
            == "posted"
        )
        assert (
            _exec(
                ops_db,
                "SELECT state FROM provider_operations WHERE id = %s",
                (op["id"],),
                fetch=True,
            )[0][0]
            == "succeeded"
        ), (
            "every verdict must terminalize the op row it judged, or the "
            "ix_ops_retire retention class never drains"
        )

    def test_container_verdict_terminalizes_failed_and_the_op_follows(self, ops_db):
        intent, op = self._ambiguous(ops_db)
        outcome, _ = self._reconcile(
            ops_db, intent, status="EXPIRED", mode="container_verdict"
        )
        assert outcome == "failed"
        assert (
            _exec(
                ops_db,
                "SELECT state FROM provider_operations WHERE id = %s",
                (op["id"],),
                fetch=True,
            )[0][0]
            == "failed"
        )

    def test_FINISHED_does_not_terminalize_anything(self, ops_db):
        """The `02` §6 trap, driven end to end rather than only unit-tested:
        after `publish_called`, FINISHED means *ready to publish*, so acting on
        it would terminalize a still-live publish as a definite non-event."""
        intent, _ = self._ambiguous(ops_db)
        outcome, _ = self._reconcile(
            ops_db, intent, status="FINISHED", mode="container_verdict"
        )
        assert outcome == "pending"
        assert (
            _exec(
                ops_db,
                "SELECT state FROM post_intents WHERE id = %s",
                (intent,),
                fetch=True,
            )[0][0]
            == "publishing_ambiguous"
        )

    def test_evidence_capture_parks_review_required_WITH_the_trail(self, ops_db):
        """Same authoritative-positive value, opposite outcome — which is the
        whole content of the mode. Driven at the exhausted rung so the park is
        reachable."""
        intent, _ = self._ambiguous(ops_db)
        outcome, _ = self._reconcile(
            ops_db, intent, status="PUBLISHED", mode="evidence_capture", checks=99
        )
        assert outcome == "review_required"
        state, last_error = _exec(
            ops_db,
            "SELECT state, last_error FROM post_intents WHERE id = %s",
            (intent,),
            fetch=True,
        )[0]
        assert state == "review_required", (
            "evidence-capture mode must NEVER self-terminalize posted, even on "
            "the value container-verdict mode calls authoritative-positive"
        )
        trail = last_error["evidence"]["trail"]
        assert any(e.get("status_code") == "PUBLISHED" for e in trail), (
            "parking without the captured trail would lose the only evidence an "
            "operator has to resolve it with"
        )
        assert any("stories" in e for e in trail), "the exhaustion tail must run"


class TestTheGuardsAreLoadBearing:
    """House standard: drop each DB-enforced guard and confirm the refusal
    disappears. A refusal that survives its guard's removal was never coming
    from the guard."""

    def test_dropping_uq_ops_business_key_lets_a_SECOND_permit_through(self, ops_db):
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def permit():
            async with engine.connect() as conn:
                return await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )

        _run(permit())
        with pytest.raises(PermitRefused, match="duplicate permit"):
            _run(permit())

        _exec(
            ops_db,
            "ALTER TABLE provider_operations DROP CONSTRAINT uq_ops_business_key",
        )
        try:
            _run(permit())  # no longer refused — the guard was doing the work
            assert (
                _exec(
                    ops_db,
                    "SELECT count(*) FROM provider_operations"
                    " WHERE intent_id = %s AND op_kind = 'publish'",
                    (intent,),
                    fetch=True,
                )[0][0]
                == 2
            )
        finally:
            _exec(
                ops_db,
                "DELETE FROM provider_operations a USING provider_operations b"
                " WHERE a.business_key = b.business_key AND a.ctid > b.ctid",
            )
            _exec(
                ops_db,
                "ALTER TABLE provider_operations ADD CONSTRAINT uq_ops_business_key"
                " UNIQUE (business_key)",
            )


class TestTheCASIsTheOnlyGuardOnResolutionAndItDiscriminates:
    """The #860 warning, answered by measurement and kept as a standing test.

    `provider_operations` has NO transition guard — measured, not read off the
    DDL — so the CAS predicate is the entire protection on `permitted →
    terminal`. #883's same-state failure does not reproduce, because a CAS keyed
    on the SOURCE state matches nothing once the row has moved, unlike a trigger
    keyed on "did the value change".
    """

    def test_the_table_really_has_no_transition_guard(self, ops_db):
        """Measured. If a guard is ever added, this reddens and the reasoning
        above has to be re-run rather than silently inherited."""
        triggers = _exec(
            ops_db,
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            " WHERE c.relname = 'provider_operations' AND NOT t.tgisinternal",
            fetch=True,
        )
        assert [t[0] for t in triggers] == ["tg_touch_provider_operations"], (
            "only the touch trigger; there is no transition guard, which is why "
            "the CAS below is load-bearing"
        )

    def test_a_rowcount_positive_control_before_asserting_any_refusal(self, ops_db):
        """House standard. Without this, a zero rowcount below could mean the
        UPDATE never matched anything for an unrelated reason."""
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def go():
            async with engine.connect() as conn:
                op = await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )
                await provider_ops.resolve_permit(
                    conn, op_id=op["id"], outcome="succeeded", response_ref={"v": 1}
                )
                await conn.commit()
                return op

        op = _run(go())
        assert (
            _exec(
                ops_db,
                "SELECT state FROM provider_operations WHERE id = %s",
                (op["id"],),
                fetch=True,
            )[0][0]
            == "succeeded"
        ), "the positive control must actually move the row"

        # ...and only now is a refusal meaningful.
        async def again():
            async with engine.connect() as conn:
                await provider_ops.resolve_permit(
                    conn, op_id=op["id"], outcome="failed"
                )

        with pytest.raises(PermitRefused, match="was not in state"):
            _run(again())

    def test_a_second_resolver_is_refused_BY_NAME_not_by_a_rowcount(self, ops_db):
        """`rowcount` cannot discriminate a winner from a loser — that is #883's
        entire lesson — so the loser must get a named refusal it cannot mistake
        for success."""
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def permit_then(outcome):
            async with engine.connect() as conn:
                op = await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )
                await provider_ops.resolve_permit(conn, op_id=op["id"], outcome=outcome)
                await conn.commit()
                return op["id"]

        op_id = _run(permit_then("ambiguous"))

        async def same_destination_again():
            async with engine.connect() as conn:
                await provider_ops.resolve_permit(
                    conn, op_id=op_id, outcome="ambiguous"
                )

        with pytest.raises(PermitRefused, match="was not in state"):
            _run(same_destination_again())

    def test_GENUINE_concurrent_resolvers_via_asyncio_gather(self, ops_db):
        """The committed 'second resolver' test is SEQUENTIAL (resolve, then
        resolve again) -- a real but weaker property than a true race. This
        drives two resolvers via asyncio.gather, separate connections, both
        targeting the SAME op from the SAME source state, genuinely
        concurrent rather than one-after-another."""
        intent = _new_intent(ops_db)
        job, token = _leased_job(ops_db)
        engine = _engine(ops_db)

        async def setup_permit():
            async with engine.connect() as conn:
                op = await provider_ops.acquire_permit(
                    conn,
                    workspace_id=ops_db["ws"],
                    intent_id=intent,
                    job_id=job,
                    lease_token=token,
                    op_kind="publish",
                )
                await conn.commit()
                return op["id"]

        op_id = _run(setup_permit())

        results = {"a": None, "b": None}

        async def resolver(name, outcome):
            async with engine.connect() as conn:
                try:
                    await provider_ops.resolve_permit(
                        conn,
                        op_id=op_id,
                        outcome=outcome,
                        response_ref={"v": 1, "who": name},
                    )
                    await conn.commit()
                    results[name] = "won"
                except PermitRefused:
                    results[name] = "refused"

        async def race():
            await asyncio.gather(
                resolver("a", "succeeded"),
                resolver("b", "failed"),
            )

        _run(race())
        winners = [v for v in results.values() if v == "won"]
        refused = [v for v in results.values() if v == "refused"]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        assert len(refused) == 1, f"expected exactly one refusal, got {results}"
