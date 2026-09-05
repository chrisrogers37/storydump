"""W1 — registry + claim-loop logic for the worker composition root (#942, #903).

The registry is the 15-entry kind→executor table the scoping doc names, and the
denominator is DERIVED from the schema's own ck_jobs_kind rather than restated
(#951's anti-pinning discipline): a kind added by a future migration fails here
until the registry says whether it runs or parks.

Parking is the load-bearing negative: a claimed job whose kind has no runnable
executor must be RESCHEDULED (visible, alive, counted) — never finalized dead,
never stranded on a lease, and never silently dropped. fn_clock_tick already
mints kinds nothing can run (refresh_credential, sync_media_source), so this is
a measured hazard, not a hypothetical (#790 verification record).

Unit tier: executors and session doors are monkeypatched at the module seam —
the DB-backed halves are exercised by tests/scripts/test_w1_worker_gate.py.
"""

import pytest
import re


from src.models.target.machinery import Job
from src.services.target import work_loop
from src.services.target.jobs import JobFenced
from src.services.target.work_loop import (
    Parked,
    WorkerConfig,
    WorkerDeps,
    build_registry,
)


def schema_kinds() -> set:
    ck = next(c for c in Job.__table__.constraints if c.name == "ck_jobs_kind")
    return set(re.findall(r"'([a-z_]+)'", str(ck.sqltext)))


def full_deps(**over):
    """Every seam supplied — the maximal registry."""

    async def _fake_seam(*a, **k):  # pragma: no cover - never invoked here
        raise AssertionError("seam invoked in a registry-shape test")

    base = dict(
        meta=object(),
        transit=object(),
        media_fetch=_fake_seam,
        transport=_fake_seam,
        poll=_fake_seam,
        refresh=_fake_seam,
        drive=object(),
        email=object(),
        config=WorkerConfig(),
    )
    base.update(over)
    return WorkerDeps(**base)


class TestRegistryCoversTheSchema:
    def test_registry_keys_equal_the_schema_kind_check(self):
        registry = build_registry(full_deps())
        assert set(registry) == schema_kinds()

    def test_every_entry_is_an_adapter_or_a_parked_reason(self):
        registry = build_registry(full_deps())
        for kind, entry in registry.items():
            if isinstance(entry, Parked):
                assert entry.reason.strip(), f"{kind} parked with no reason"
            else:
                assert callable(entry), f"{kind} entry is neither Parked nor callable"

    def test_with_every_seam_supplied_the_live_set_is_exactly_the_built_executors(self):
        registry = build_registry(full_deps())
        live = {k for k, e in registry.items() if not isinstance(e, Parked)}
        assert live == {
            "plan_slot",
            "publish_pipeline",
            "deliver_outbox",
            "reconcile_ambiguous",
            "reap_expired",
            "reap_transit_assets",
            "refresh_credential",
            "reauth_prompt",
            "sync_media_source",
            "first_ingest_chunk",
            "alert_stranded_sources",
            "revoke_workspace_credentials",
            # #1092: live once a provider is configured, and `full_deps` now
            # supplies that seam like every other.
            "send_email",
            # #1090 H1: live unconditionally. Its one provider seam is leg 3,
            # which `06` §1 already backstops with the FC-3.6 TTL sweep, so a
            # missing transit store must not park the whole workflow.
            "offboard_workspace",
        }

    def test_the_unbuilt_kinds_park_even_with_every_seam_supplied(self):
        registry = build_registry(full_deps())
        unbuilt = schema_kinds() - {
            "plan_slot",
            "publish_pipeline",
            "deliver_outbox",
            "reconcile_ambiguous",
            "reap_expired",
            "reap_transit_assets",
            "refresh_credential",
            "reauth_prompt",
            "sync_media_source",
            "first_ingest_chunk",
            "alert_stranded_sources",
            "revoke_workspace_credentials",
            # #1092: live once a provider is configured, and `full_deps` now
            # supplies that seam like every other.
            "send_email",
            "offboard_workspace",  # #1090 H1
        }
        assert unbuilt, "denominator went empty — the schema kinds parse broke"
        for kind in unbuilt:
            assert isinstance(registry[kind], Parked), f"{kind} should have no executor"


class TestSeamAbsenceParksTheDependentKind:
    def test_no_transport_parks_deliver_outbox(self):
        registry = build_registry(full_deps(transport=None))
        assert isinstance(registry["deliver_outbox"], Parked)
        assert "transport" in registry["deliver_outbox"].reason

    def test_no_email_provider_parks_send_email(self):
        """#1092. The reason must NAME the config, because this seam is absent
        for a reason nobody can act on from the code — `07` §1's owner ack on
        adding Resend is open — and "no email provider configured" without the
        variable names sends the reader to the wrong place."""
        registry = build_registry(full_deps(email=None))
        assert isinstance(registry["send_email"], Parked)
        reason = registry["send_email"].reason
        assert "RESEND_API_KEY" in reason and "EMAIL_FROM" in reason

    def test_an_email_provider_makes_send_email_live(self):
        """The positive control the parked assertion needs: a seam test that
        never sees the kind become live cannot tell "parks correctly" from
        "parks always"."""
        registry = build_registry(full_deps())
        assert not isinstance(registry["send_email"], Parked)

    def test_no_transit_parks_the_transit_reaper(self):
        registry = build_registry(full_deps(transit=None))
        assert isinstance(registry["reap_transit_assets"], Parked)

    def test_no_media_fetch_parks_publish_pipeline(self):
        registry = build_registry(full_deps(media_fetch=None))
        assert isinstance(registry["publish_pipeline"], Parked)
        assert "media_fetch" in registry["publish_pipeline"].reason

    def test_no_poll_does_NOT_park_the_reconciler(self):
        """Re-pointed, not deleted (#1090 D4). The property this class pins —
        a missing seam parks its DEPENDENT kind — is unchanged and still has
        four instances above. The reconciler stopped being one of them: the
        `02` §6 sweep returns `ladder_due` rows (which need the poll) AND
        `notify_window` rows (which need nothing), so parking the kind parked
        a half that does not depend on the absent seam. Production runs
        `poll=None`, so the old parking is exactly why `06` §5's customer
        notification could never fire."""
        registry = build_registry(full_deps(poll=None))
        assert not isinstance(registry["reconcile_ambiguous"], Parked)


class TestReconcilerSweepBranchesOnItsReason:
    """The `notify_window` half of the `02` §6 sweep (#1090 D4).

    `fn_reconciler_sweep` has tagged its rows since 059; nothing read the tag,
    so every notify row was polled as if the ladder were due and no
    notification was ever produced.
    """

    def _job(self):
        return {"id": "j-rec", "kind": "reconcile_ambiguous", "workspace_id": None}

    async def _drive(self, monkeypatch, *, rows, deps):
        notified, reconciled = [], []

        async def fake_sweep(session, *, limit, notify_after_seconds):
            return rows

        async def fake_notify(
            session, *, intent_id, workspace_id, web_app_origin, retry_after_seconds
        ):
            notified.append((intent_id, web_app_origin))
            return 1

        async def fake_reconcile(session, *, intent_id, **kw):
            reconciled.append(intent_id)
            return "pending"

        monkeypatch.setattr(work_loop.reconciler, "sweep_due", fake_sweep)
        monkeypatch.setattr(work_loop.reconciler, "notify_parked_customer", fake_notify)
        monkeypatch.setattr(work_loop.reconciler, "reconcile_intent", fake_reconcile)
        registry = build_registry(deps)
        await registry["reconcile_ambiguous"](_FakeSession(), self._job())
        return notified, reconciled

    async def test_a_notify_row_notifies_and_is_never_polled(self, monkeypatch):
        """The defect in one assertion. `full_deps` supplies a poll seam that
        RAISES when invoked, so a notify row routed into the ladder fails here
        rather than passing quietly."""
        rows = [{"intent_id": "i-1", "workspace_id": "ws-1", "reason": "notify_window"}]
        notified, reconciled = await self._drive(
            monkeypatch,
            rows=rows,
            deps=full_deps(config=WorkerConfig(web_app_origin="https://app.example")),
        )
        assert notified == [("i-1", "https://app.example")]
        assert reconciled == [], "a parked intent must not be re-polled"

    async def test_a_ladder_row_is_reconciled_not_notified(self, monkeypatch):
        """The positive control: a branch test that never sees the other side
        cannot tell "routes correctly" from "routes everything one way"."""
        rows = [{"intent_id": "i-2", "workspace_id": "ws-1", "reason": "ladder_due"}]
        notified, reconciled = await self._drive(
            monkeypatch, rows=rows, deps=full_deps()
        )
        assert reconciled == ["i-2"]
        assert notified == []

    async def test_without_a_poll_seam_the_notify_half_still_runs(self, monkeypatch):
        """Production's exact configuration (`worker.py` passes `poll=None`).
        The ladder row is skipped, the notify row is served."""
        rows = [
            {"intent_id": "i-3", "workspace_id": "ws-1", "reason": "ladder_due"},
            {"intent_id": "i-4", "workspace_id": "ws-1", "reason": "notify_window"},
        ]
        notified, reconciled = await self._drive(
            monkeypatch, rows=rows, deps=full_deps(poll=None)
        )
        assert [n[0] for n in notified] == ["i-4"]
        assert reconciled == [], "no seam to poll with"

    async def test_a_workspace_with_no_surface_makes_the_sweep_undeliverable(
        self, monkeypatch
    ):
        """The executor must not report a delivery it could not make."""
        from src.services.target import outbox

        rows = [{"intent_id": "i-5", "workspace_id": "ws-1", "reason": "notify_window"}]

        async def fake_sweep(session, *, limit, notify_after_seconds):
            return rows

        async def fake_notify(session, **kw):
            return outbox.UNDELIVERABLE

        monkeypatch.setattr(work_loop.reconciler, "sweep_due", fake_sweep)
        monkeypatch.setattr(work_loop.reconciler, "notify_parked_customer", fake_notify)
        registry = build_registry(full_deps())
        got = await registry["reconcile_ambiguous"](_FakeSession(), self._job())
        assert got == outbox.UNDELIVERABLE

    async def test_the_sweep_keys_match_what_the_door_returns(self):
        """The `KeyError` that could not surface while the kind was parked.

        The door returns `o_intent_id`/`o_workspace_id`; the consumer reads
        `intent_id`/`workspace_id`. `sweep_due` now aliases in the SELECT, so
        this asserts the two spellings agree at the one place that chooses
        them — reading the shipped SQL rather than restating it.
        """
        import inspect

        src = inspect.getsource(work_loop.reconciler.sweep_due)
        for alias in ("AS intent_id", "AS workspace_id", "AS reason"):
            assert alias in src, f"sweep_due must alias {alias}"


class TestAJobThatReachedNobodyIsNotASuccess:
    """#1090, ari's mid-sprint constraint. Two producers already log-and-succeed
    on an empty binding list, so the ledger records a clean run for a message
    nobody received and the cadence repeats into the void forever. The verdict
    now decides the terminal state."""

    def _loop(self, executor):
        from src.services.target.work_loop import WorkLoop

        finalized = {}

        class _Jobs:
            JobFenced = work_loop.jobs.JobFenced

            @staticmethod
            async def finalize_job(session, job_id, token, terminal_state):
                finalized["state"] = terminal_state

            @staticmethod
            async def reschedule_job(*a, **k):  # pragma: no cover
                raise AssertionError("must not reschedule")

        loop = WorkLoop.__new__(WorkLoop)
        loop._registry = {"deliver_outbox": executor}
        loop._config = WorkerConfig()
        loop.processed = loop.parked = loop.failures = 0
        loop.fenced = loop.undeliverable = loop.consecutive_errors = 0

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx(job):
            yield _FakeSession()

        loop._session_for = _ctx
        return loop, finalized, _Jobs

    async def test_undeliverable_parks_review_required_and_is_counted_apart(
        self, monkeypatch
    ):
        from src.services.target import outbox

        async def executor(session, job):
            return outbox.UNDELIVERABLE

        loop, finalized, fake_jobs = self._loop(executor)
        monkeypatch.setattr(work_loop, "jobs", fake_jobs)
        await loop._run_job({"id": "j1", "kind": "deliver_outbox", "lease_token": "t"})

        assert finalized["state"] == "review_required", (
            "not `succeeded`, and not `failed` either: retrying cannot"
            " conjure a binding, so `failed` would trade a silent success"
            " for a poison loop"
        )
        assert (loop.undeliverable, loop.processed) == (1, 0), (
            "counted apart from real deliveries — that separation IS the fix"
        )

    async def test_an_ordinary_executor_still_succeeds(self, monkeypatch):
        """The positive control. Every existing executor returns None, and a
        change that made THEM stop succeeding would be worse than the bug."""

        async def executor(session, job):
            return None

        loop, finalized, fake_jobs = self._loop(executor)
        monkeypatch.setattr(work_loop, "jobs", fake_jobs)
        await loop._run_job({"id": "j2", "kind": "deliver_outbox", "lease_token": "t"})
        assert finalized["state"] == "succeeded"
        assert (loop.undeliverable, loop.processed) == (0, 1)


class _FakeSession:
    """Session double for the adapter seam: records executed statements."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.statements = []

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), params))
        rows = self.rows

        class _R:
            def mappings(self_inner):
                class _M:
                    def first(self_m):
                        return rows[0] if rows else None

                return _M()

            def first(self_inner):
                return rows[0] if rows else None

        return _R()


class TestPlanSlotAdapterMapsThePayload:
    async def test_adapter_resolves_ref_and_mode_then_calls_the_executor(
        self, monkeypatch
    ):
        seen = {}

        async def fake_execute_plan_slot(session, **kwargs):
            seen.update(kwargs)
            return work_loop.scheduler.SlotOutcome(intent_id="intent-1")

        monkeypatch.setattr(
            work_loop.scheduler, "execute_plan_slot", fake_execute_plan_slot
        )
        from datetime import datetime, timezone

        resolved_slot = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)

        swept = []

        async def fake_sweep(session, *, limit):
            swept.append(limit)
            return {"prompted": 0, "advanced": 0}

        monkeypatch.setattr(work_loop.prompts, "sweep_due_prompts", fake_sweep)
        session = _FakeSession(
            rows=[
                {
                    "provider_account_ref": "ig-acct-9",
                    "state": "active",
                    "approval_mode": "manual",
                    "slot_at": resolved_slot,
                }
            ]
        )
        job = {
            "id": "j1",
            "kind": "plan_slot",
            "workspace_id": "ws-1",
            "payload": {
                "v": 1,
                "ig_account_id": "acct-1",
                "slot_at": "2026-08-21T10:00:00+00:00",
            },
        }
        registry = build_registry(full_deps())
        await registry["plan_slot"](session, job)

        assert seen["workspace_id"] == "ws-1"
        assert seen["ig_account_id"] == "acct-1"
        assert seen["provider_account_ref"] == "ig-acct-9"
        assert seen["approval_mode"] == "manual"
        # The payload crosses jsonb as a string POSTGRES rendered, so the
        # resolve query casts it server-side (#969) — the executor receives
        # the row's datetime and no Python parser ever sees the string.
        assert seen["slot_at"] is resolved_slot
        bound = session.statements[0][1]
        assert bound["slot"] == "2026-08-21T10:00:00+00:00", (
            "the raw payload string must ride to Postgres unmodified"
        )
        assert swept == [1], "a minted intent triggers the same-beat prompt fast path"

    async def test_a_slot_minted_for_an_account_since_removed_is_a_no_op(
        self, monkeypatch
    ):
        """The clock reads `active` only; a `plan_slot` it minted a tick before
        the destination was removed must not become a post afterwards."""
        called = []

        async def fake_execute_plan_slot(session, **kwargs):
            called.append(kwargs)

        monkeypatch.setattr(
            work_loop.scheduler, "execute_plan_slot", fake_execute_plan_slot
        )
        from datetime import datetime, timezone

        session = _FakeSession(
            rows=[
                {
                    "provider_account_ref": "ig-acct-9",
                    "state": "disabled",
                    "approval_mode": "manual",
                    "slot_at": datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc),
                }
            ]
        )
        job = {
            "id": "j2",
            "kind": "plan_slot",
            "workspace_id": "ws-1",
            "payload": {
                "v": 1,
                "ig_account_id": "acct-1",
                "slot_at": "2026-08-21T10:00:00+00:00",
            },
        }
        registry = build_registry(full_deps())
        assert await registry["plan_slot"](session, job) == "account_inactive"
        assert called == []


class _Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def _rec(*a, **k):
            self.calls.append((name, a, k))

        return _rec


class _FakeHeartbeat:
    def __init__(self):
        self.registered = []
        self.unregistered = []

    def register(self, token):
        self.registered.append(token)

    def unregister(self, token):
        self.unregistered.append(token)


def _loop_with(monkeypatch, *, registry, claims):
    """A WorkLoop wired to fakes: `claims` yields job dicts then None."""
    hb = _FakeHeartbeat()
    events = _Recorder()

    claim_iter = iter(claims)

    async def fake_claim_job(conn, **kwargs):
        try:
            return next(claim_iter)
        except StopIteration:
            return None

    monkeypatch.setattr(work_loop.jobs, "claim_job", fake_claim_job)
    monkeypatch.setattr(work_loop.jobs, "finalize_job", events.finalize_job)
    monkeypatch.setattr(work_loop.jobs, "reschedule_job", events.reschedule_job)

    class _SessionCtx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *exc):
            return False

    seen_session_jobs = []

    def session_for(job):
        seen_session_jobs.append(job["id"])
        return _SessionCtx()

    loop = work_loop.WorkLoop(
        claim_conn=object(),
        session_for=session_for,
        lane="bulk",
        registry=registry,
        heartbeat=hb,
        config=WorkerConfig(),
        worker_name="w-test",
    )
    return loop, hb, events


class TestOneCycle:
    async def test_success_finalizes_succeeded_and_pairs_heartbeat_registration(
        self, monkeypatch
    ):
        ran = []

        async def ok_adapter(session, job):
            ran.append(job["id"])

        job = {"id": "j1", "kind": "plan_slot", "lease_token": "t1", "payload": {}}
        loop, hb, events = _loop_with(
            monkeypatch, registry={"plan_slot": ok_adapter}, claims=[job]
        )
        processed = await loop.run_once()

        assert processed is True and ran == ["j1"]
        assert [c[0] for c in events.calls] == ["finalize_job"]
        assert events.calls[0][2]["terminal_state"] == "succeeded"
        assert hb.registered == ["t1"] and hb.unregistered == ["t1"]

    async def test_a_parked_kind_is_rescheduled_alive_with_its_attempt_restored(
        self, monkeypatch, caplog
    ):
        job = {
            "id": "j2",
            "kind": "refresh_credential",
            "lease_token": "t2",
            "payload": {},
        }
        loop, hb, events = _loop_with(
            monkeypatch,
            registry={"refresh_credential": Parked("no executor exists (W5d)")},
            claims=[job],
        )
        with caplog.at_level("WARNING"):
            await loop.run_once()

        assert [c[0] for c in events.calls] == ["reschedule_job"]
        kwargs = events.calls[0][2]
        assert kwargs["restore_attempt"] is True
        assert loop.parked == 1
        assert any("refresh_credential" in r.message for r in caplog.records)
        assert hb.registered == ["t2"] and hb.unregistered == ["t2"]

    async def test_an_executor_failure_reschedules_without_restoring_the_attempt(
        self, monkeypatch
    ):
        async def boom(session, job):
            raise RuntimeError("provider fell over")

        job = {"id": "j3", "kind": "plan_slot", "lease_token": "t3", "payload": {}}
        loop, hb, events = _loop_with(
            monkeypatch, registry={"plan_slot": boom}, claims=[job]
        )
        await loop.run_once()

        assert [c[0] for c in events.calls] == ["reschedule_job"]
        assert events.calls[0][2]["restore_attempt"] is False
        assert loop.failures == 1
        assert hb.unregistered == ["t3"], (
            "a failed run must still release its lease registration"
        )

    async def test_a_fenced_finalize_counts_and_never_raises_out_of_the_cycle(
        self, monkeypatch
    ):
        async def ok_adapter(session, job):
            pass

        async def fenced_finalize(session, job_id, lease_token, terminal_state):
            raise JobFenced(f"job {job_id} fenced")

        job = {"id": "j4", "kind": "plan_slot", "lease_token": "t4", "payload": {}}
        loop, hb, events = _loop_with(
            monkeypatch, registry={"plan_slot": ok_adapter}, claims=[job]
        )
        monkeypatch.setattr(work_loop.jobs, "finalize_job", fenced_finalize)
        await loop.run_once()

        assert loop.fenced == 1
        assert hb.unregistered == ["t4"]

    async def test_an_empty_claim_reports_idle(self, monkeypatch):
        loop, hb, events = _loop_with(monkeypatch, registry={}, claims=[])
        processed = await loop.run_once()
        assert processed is False and events.calls == []


class TestLaneSurvivesTransientClaimErrors:
    """navi's second instance: an unguarded claim error killed the lane task
    silently. The lane now survives transient errors loudly (logged, counted)
    and dies LOUDLY — by raising into the supervisor — only when they persist
    past the configured ceiling."""

    async def test_transient_claim_errors_are_survived_and_counted(
        self, monkeypatch, caplog
    ):
        calls = {"n": 0}

        async def flaky_claim(conn, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ConnectionError("db blip")
            return None

        monkeypatch.setattr(work_loop.jobs, "claim_job", flaky_claim)
        loop = work_loop.WorkLoop(
            claim_conn=object(),
            session_for=lambda job: None,
            lane="bulk",
            registry={},
            heartbeat=_FakeHeartbeat(),
            config=WorkerConfig(claim_idle_seconds=0.01),
            worker_name="w-t",
        )
        with caplog.at_level("ERROR"):
            assert await loop.run_once() is False
            assert await loop.run_once() is False
        assert loop.consecutive_errors == 2
        assert sum("claim failed" in r.message for r in caplog.records) == 2
        assert await loop.run_once() is False  # healthy claim resets
        assert loop.consecutive_errors == 0

    async def test_persistent_claim_errors_raise_past_the_ceiling(self, monkeypatch):
        async def dead_claim(conn, **kwargs):
            raise ConnectionError("db down")

        monkeypatch.setattr(work_loop.jobs, "claim_job", dead_claim)
        cfg = WorkerConfig(claim_idle_seconds=0.01, lane_max_consecutive_errors=3)
        loop = work_loop.WorkLoop(
            claim_conn=object(),
            session_for=lambda job: None,
            lane="bulk",
            registry={},
            heartbeat=_FakeHeartbeat(),
            config=cfg,
            worker_name="w-t",
        )
        import pytest as _pytest

        with _pytest.raises(ConnectionError):
            for _ in range(10):
                await loop.run_once()
        assert loop.consecutive_errors == 3


class TestDeliverOutboxRetiresAGoneChat:
    """The deliverer's definitive "chat gone" ends the hold and retires the
    binding — or follows a group that became a supergroup (#1240 review)."""

    def _job(self):
        return {
            "id": "j-d",
            "kind": "deliver_outbox",
            "workspace_id": "ws-1",
            "serialization_key": "binding:b-1",
            "payload": {"binding_id": "b-1"},
        }

    def _registry(self):
        from types import SimpleNamespace

        transport = SimpleNamespace(for_chat=lambda ref: lambda row: None)
        return build_registry(full_deps(transport=transport))

    @pytest.fixture
    def gone(self, monkeypatch):
        seen = {"revoked": [], "repointed": [], "migrate_to": None, "repoint_ok": True}

        class _Poller:
            def __init__(self, *a, **kw):
                self.deferred = 0
                self.consecutive_failures = 0

            async def tick(self):
                return {
                    "state": "failed",
                    "destination_gone": True,
                    "migrate_to": seen["migrate_to"],
                }

        async def revoke_by_id(session, *, binding_id):
            seen["revoked"].append(binding_id)
            return True

        async def repoint(session, *, binding_id, external_ref):
            seen["repointed"].append((binding_id, external_ref))
            return seen["repoint_ok"]

        monkeypatch.setattr(work_loop.outbox, "OutboxPoller", _Poller)
        monkeypatch.setattr(work_loop.bindings, "revoke_by_id", revoke_by_id)
        monkeypatch.setattr(work_loop.bindings, "repoint", repoint)
        return seen

    async def test_a_kicked_bot_revokes_the_binding(self, gone):
        session = _FakeSession(
            rows=[{"external_ref": "-100777", "workspace_id": "ws-1"}]
        )
        await self._registry()["deliver_outbox"](session, self._job())
        assert gone["revoked"] == ["b-1"] and gone["repointed"] == []

    async def test_a_supergroup_upgrade_follows_the_chat(self, gone):
        gone["migrate_to"] = "-1009999"
        session = _FakeSession(rows=[{"external_ref": "-777", "workspace_id": "ws-1"}])
        await self._registry()["deliver_outbox"](session, self._job())
        assert gone["repointed"] == [("b-1", "-1009999")] and gone["revoked"] == []

    async def test_a_successor_another_workspace_holds_revokes_instead(self, gone):
        gone["migrate_to"] = "-1009999"
        gone["repoint_ok"] = False
        session = _FakeSession(rows=[{"external_ref": "-777", "workspace_id": "ws-1"}])
        await self._registry()["deliver_outbox"](session, self._job())
        assert gone["revoked"] == ["b-1"]
