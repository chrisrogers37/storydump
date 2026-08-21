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
        }

    def test_the_nine_unbuilt_kinds_park_even_with_every_seam_supplied(self):
        registry = build_registry(full_deps())
        unbuilt = schema_kinds() - {
            "plan_slot",
            "publish_pipeline",
            "deliver_outbox",
            "reconcile_ambiguous",
            "reap_expired",
            "reap_transit_assets",
        }
        assert unbuilt, "denominator went empty — the schema kinds parse broke"
        for kind in unbuilt:
            assert isinstance(registry[kind], Parked), f"{kind} should have no executor"


class TestSeamAbsenceParksTheDependentKind:
    def test_no_transport_parks_deliver_outbox(self):
        registry = build_registry(full_deps(transport=None))
        assert isinstance(registry["deliver_outbox"], Parked)
        assert "transport" in registry["deliver_outbox"].reason

    def test_no_transit_parks_the_transit_reaper(self):
        registry = build_registry(full_deps(transit=None))
        assert isinstance(registry["reap_transit_assets"], Parked)

    def test_no_media_fetch_parks_publish_pipeline(self):
        registry = build_registry(full_deps(media_fetch=None))
        assert isinstance(registry["publish_pipeline"], Parked)
        assert "media_fetch" in registry["publish_pipeline"].reason

    def test_no_poll_parks_the_reconciler(self):
        registry = build_registry(full_deps(poll=None))
        assert isinstance(registry["reconcile_ambiguous"], Parked)


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
            return "intent-1"

        monkeypatch.setattr(
            work_loop.scheduler, "execute_plan_slot", fake_execute_plan_slot
        )
        session = _FakeSession(
            rows=[{"provider_account_ref": "ig-acct-9", "approval_mode": "manual"}]
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
