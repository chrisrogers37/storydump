"""Registry + claim-loop logic for the worker composition root.

The registry is the kind→executor table, and the denominator is DERIVED from
the schema's own ck_jobs_kind rather than restated: a kind a migration
adds fails here until the registry says whether it runs or parks.

Parking is the load-bearing negative: a claimed job whose kind has no runnable
executor must be RESCHEDULED (visible, alive, counted) — never finalized dead,
never stranded on a lease, and never silently dropped.

Unit tier: executors and session doors are monkeypatched at the module seam —
the DB-backed halves are exercised by tests/scripts/test_w1_worker_gate.py.
"""

import copy
import logging
import pytest
import re
from types import SimpleNamespace


from src.models.target.machinery import Job
from src.services.target import (
    past_posts_reconciliation,
    email_sender,
    sessions,
    signup_admissions,
    unit_of_work,
    work_loop,
)
from src.services.target.past_posts_activation import PastPostsActivationEvidence
from src.services.target.instagram_sources import PastPostsCapability
from src.services.target.jobs import SELF_FINALIZED, JobFenced
from src.services.target.mix_conversion import MixConversionEvidence
from src.services.target.work_loop import (
    UNBUILT_KINDS,
    Parked,
    WorkerConfig,
    WorkerDeps,
    build_registry,
)
from tests.src.services.target.conftest import RecordingSender
from tests.src.services.target.schema_code import function_body


def schema_kinds() -> set:
    ck = next(c for c in Job.__table__.constraints if c.name == "ck_jobs_kind")
    return set(re.findall(r"'([a-z_]+)'", str(ck.sqltext)))


#: The config half of "everything supplied": the activation nudge is
#: gated on its switch and a web origin as well as on the email seam.
FULL_CONFIG = dict(activation_nudge_enabled=True, web_app_origin="https://app.example")


def full_deps(**over):
    """Every seam supplied, and every config gate open — the maximal registry."""

    async def _fake_seam(*a, **k):  # pragma: no cover - never invoked here
        raise AssertionError("seam invoked in a registry-shape test")

    base = dict(
        mix_conversion_evidence=MixConversionEvidence("synthetic-run", "d" * 64),
        meta=object(),
        transit=object(),
        media_fetch=_fake_seam,
        transport=_fake_seam,
        poll=_fake_seam,
        refresh=_fake_seam,
        adapters={"gdrive": object()},
        email=object(),
        past_posts_keys=object(),
        # the past-posts source armed, with the client its adapter reads
        # through, which is what makes the reconciliation executor live.
        past_posts_capability=PastPostsCapability(enabled=True),
        past_posts_client=object(),
        # the past-posts coordinator and its preparation, which make the file
        # lane's prepare executor live.
        past_posts=object(),
        selected_prepare=_fake_seam,
        config=WorkerConfig(**FULL_CONFIG),
    )
    base.update(over)
    return WorkerDeps(**base)


def _reconcile_job():
    return {"id": "j-rec", "kind": "reconcile_ambiguous", "workspace_id": None}


async def _in_progress(*, intent_id, workspace_id):
    """The reconciler's provider poll, answering a container still in flight."""
    return "IN_PROGRESS"


class TestEveryProviderFacingExecutorOwnsItsTransactions:
    """An executor that reaches the egress floor runs with NO job session.

    `own_transactions` exists so "a task waiting on a provider holds no
    pooled connection while it waits" (its own docstring). Without the mark
    the loop would hold a transaction open across a Drive call, a token
    refresh or a revoke, and hand the executor a session it never touches.

    It is also the precondition for arming `_IN_TRANSACTION` in
    `make_session_for`. The flag is set for an executor's whole body, so an
    unmarked executor that reaches the floor would raise
    `TransactionDisciplineError` on a production path. The suite cannot
    see that on its own: the gates inject provider stubs (`refresh=_fake_seam`
    here, `refresh=refresh_stub` in `test_w5de_credential_lifecycle.py`), so
    nothing in the test tree reaches `egress.request` from a worker session.

    The executor list is explicit: each new provider-facing executor must
    declare that it owns its transactions.
    """

    #: Every registry kind whose executor can reach `egress.request`.
    PROVIDER_FACING = {
        "publish_pipeline",  # Meta: container create, publish, status
        "deliver_outbox",  # Telegram: the sender's bounded hold
        "send_email",  # the email provider
        "refresh_credential",  # ig_refresh -> egress.request
        "revoke_workspace_credentials",  # "through the egress floor with a
        # per-call client, the way ig_refresh does"
        "sync_media_source",  # deps.adapters provider list_changes
        "first_ingest_chunk",  # the same _run_sync, one page at a time
        "reconcile_ambiguous",  # deps.poll, the provider's verdict on an
        # ambiguous publish; each row commits alone
        "prepare_past_post",  # the past-posts adapter: the story's file
    }

    #: Marked for another reason: each of its batches commits on its own.
    OWNS_ITS_BATCHES = {"retention_sweep", "reap_expired"}

    #: Marked because a door or a step commits on its own: the lifecycle sweep's
    #: doors, the deletion cleanup's steps, whose transit drain asks the
    #: provider between them (the transit SDK does not ride the egress floor),
    #: and the reconciliation's, which asks Meta who a token is for between its
    #: short transactions (through the floor, one request per account).
    OWNS_ITS_STEPS = {
        "past_posts_lifecycle_sweep",
        "meta_deletion_cleanup",
        "reconcile_past_posts_accounts",
    }

    def test_the_provider_facing_kinds_are_exactly_the_marked_ones(self):
        registry = build_registry(full_deps())
        marked = {
            kind
            for kind, entry in registry.items()
            if getattr(entry, "owns_transactions", False)
        }
        assert (
            marked == self.PROVIDER_FACING | self.OWNS_ITS_BATCHES | self.OWNS_ITS_STEPS
        ), (
            "an executor that reaches the egress floor must own its"
            " transactions, or the loop holds a pooled connection across the"
            " provider call — and arming the `_IN_TRANSACTION` tripwire would"
            " raise on it (#1368)"
        )

    def test_reap_transit_is_deliberately_not_in_that_set(self):
        """The one provider-shaped kind that is NOT floor-bound.

        `transit.py:53`: its calls "do not ride the egress floor (the SDK owns
        its own HTTP)". Named here so the exclusion reads as a decision rather
        than an omission — it is the obvious candidate for someone to "fix".

        `retention_sweep` is absent because it only deletes rows in the
        database; `reencrypt_credentials` because it is `UNBUILT_KINDS`, parked
        with no executor at all, so there is nothing to reach a provider with.
        """
        registry = build_registry(full_deps())
        assert "reap_transit_assets" in registry
        assert not getattr(registry["reap_transit_assets"], "owns_transactions", False)


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
            "convert_source_mix",
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
            # live once a provider is configured, and `full_deps`
            # supplies that seam like every other.
            "send_email",
            # live unconditionally. Its one provider seam is leg 3,
            # which the transit TTL sweep already backstops, so a
            # missing transit store must not park the whole workflow.
            "offboard_workspace",
            # The `rate_counters` retention class only.
            "retention_sweep",
            # built, and live only with its switch and an origin as
            # well as the email seam, which `FULL_CONFIG` supplies.
            "activation_nudge_sweep",
            # the sweep needs nothing; the cleanup needs the
            # key ring alone, which `full_deps` supplies.
            "past_posts_lifecycle_sweep",
            "meta_deletion_cleanup",
            # live once the past-posts source is armed with its client,
            # which `full_deps` supplies.
            "reconcile_past_posts_accounts",
            # live once the past-posts coordinator is composed, which
            # `full_deps` supplies.
            "prepare_past_post",
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
            # live once a provider is configured, and `full_deps`
            # supplies that seam like every other.
            "send_email",
            "offboard_workspace",  #
            "retention_sweep",
            "activation_nudge_sweep",  # , with `FULL_CONFIG`
            "convert_source_mix",  # typed operator record in full_deps
            "past_posts_lifecycle_sweep",  #
            "meta_deletion_cleanup",  # , with the key ring
            "reconcile_past_posts_accounts",  # , with the past-posts source armed
            "prepare_past_post",  # , with the past-posts coordinator
        }
        assert unbuilt, "denominator went empty — the schema kinds parse broke"
        assert unbuilt == set(UNBUILT_KINDS)
        for kind in unbuilt:
            assert isinstance(registry[kind], Parked), f"{kind} should have no executor"


class TestDeletionCleanupNeedsOnlyTheKeyRing:
    """A deletion closes things, so the cleanup runs without a transit store: it
        asks the provider nothing, and counts any upload it holds as unresolved.
    Only the key ring, which its tombstones are hashed with,
        can keep it parked."""

    def test_with_the_ring_and_no_transit_store_it_is_live_and_owns_its_transactions(
        self,
    ):
        entry = build_registry(full_deps(transit=None))["meta_deletion_cleanup"]
        assert not isinstance(entry, Parked)
        assert entry.owns_transactions is True

    def test_without_the_ring_it_parks_naming_it(self):
        entry = build_registry(full_deps(past_posts_keys=None))["meta_deletion_cleanup"]
        assert isinstance(entry, Parked)
        assert "PAST_POSTS_HMAC_KEYS" in entry.reason


SWEEP_EVIDENCE = PastPostsActivationEvidence("synthetic-run", "e" * 64)


class TestReconciliationIsLiveOnlyWhereThePastPostsIsArmed:
    """The existing-account reconciliation backfills grants made
    before the past-posts source was live, so it needs the capability AND the past-posts
    adapter's client; the sweep mints its jobs only on the same terms, plus the
    release run the door keys them to."""

    def test_armed_with_its_client_it_is_live_and_owns_its_transactions(self):
        entry = build_registry(full_deps())["reconcile_past_posts_accounts"]
        assert not isinstance(entry, Parked)
        assert entry.owns_transactions is True

    @pytest.mark.parametrize(
        "capability", [None, PastPostsCapability(enabled=False)], ids=["none", "off"]
    )
    def test_without_an_active_capability_it_parks_naming_the_key_ring(
        self, capability
    ):
        entry = build_registry(full_deps(past_posts_capability=capability))[
            "reconcile_past_posts_accounts"
        ]
        assert isinstance(entry, Parked)
        assert "PAST_POSTS_HMAC_KEYS" in entry.reason

    def test_without_the_adapters_client_it_parks_naming_the_client(self):
        entry = build_registry(full_deps(past_posts_client=None))[
            "reconcile_past_posts_accounts"
        ]
        assert isinstance(entry, Parked)
        assert "HTTP client" in entry.reason

    @pytest.mark.parametrize(
        "outcome, result",
        [
            (past_posts_reconciliation.CONTINUED, SELF_FINALIZED),
            (past_posts_reconciliation.DONE, None),
        ],
    )
    async def test_a_continuing_turn_has_put_its_job_back_and_a_finished_one_has_not(
        self, monkeypatch, outcome, result
    ):
        """The loop finalizes a job unless its executor says it already put it
        back: a second finalize of a rescheduled job would only be fenced."""
        seen = []

        async def turn(deps, job):
            seen.append((deps, job))
            return outcome

        monkeypatch.setattr(past_posts_reconciliation, "execute", turn)
        deps = full_deps()
        job = {"id": "j-rec", "kind": "reconcile_past_posts_accounts"}
        entry = build_registry(deps)["reconcile_past_posts_accounts"]
        assert await entry(None, job) is result
        assert seen == [(deps, job)]

    async def _sweep(self, *, minted=(2, 2), **over):
        """The sweep run over a writer that records each statement it is sent.
        *minted* is what the cleanup door and the reconciliation door answer."""
        sent = []
        cleanup, reconcile = minted

        class Writer:
            async def execute(self, statement, params=None):
                sent.append((str(statement), params))
                answer = (
                    reconcile
                    if "fn_past_posts_reconcile_jobs" in str(statement)
                    else cleanup
                )
                return SimpleNamespace(scalar=lambda: answer)

        registry = build_registry(full_deps(**over))
        await registry["past_posts_lifecycle_sweep"](Writer(), {"id": "j-sweep"})
        return sent

    async def test_the_sweep_mints_reconciliation_jobs_with_the_evidence(self):
        sent = await self._sweep(past_posts_evidence=SWEEP_EVIDENCE)
        assert sent == [
            ("SELECT fn_meta_receipt_cleanup_jobs(:n)", {"n": 25}),
            (
                "SELECT fn_past_posts_reconcile_jobs(:n, :run_id, :digest)",
                {"n": 5, "run_id": "synthetic-run", "digest": "e" * 64},
            ),
        ]

    @pytest.mark.parametrize(
        "over",
        [
            {},
            {"past_posts_evidence": object()},
            {"past_posts_evidence": SWEEP_EVIDENCE, "past_posts_capability": None},
            {"past_posts_evidence": SWEEP_EVIDENCE, "past_posts_client": None},
        ],
        ids=["no evidence", "untyped evidence", "no capability", "no client"],
    )
    async def test_the_sweep_mints_none_without_the_evidence_or_the_armed_past_posts(
        self, over
    ):
        """Cleanup is minted regardless; a reconciliation job that would only
        park, or that nothing proved the activation for, is never minted."""
        sent = await self._sweep(**over)
        assert sent == [("SELECT fn_meta_receipt_cleanup_jobs(:n)", {"n": 25})]

    @pytest.mark.parametrize(
        "minted, line",
        [
            ((2, 0), "2 cleanup job(s), 0 reconciliation job(s)"),
            ((0, 3), "0 cleanup job(s), 3 reconciliation job(s)"),
            ((1, 1), "1 cleanup job(s), 1 reconciliation job(s)"),
            ((0, 0), None),
        ],
        ids=["cleanup", "reconciliation", "both", "nothing"],
    )
    async def test_the_sweep_says_so_only_when_it_minted_something(
        self, caplog, minted, line
    ):
        """It runs every minute, armed or not, and a past-posts source that is off mints
        nothing: a line a minute reading `0... 0` is noise a log reader has to
        learn to skip. A leg that minted is reported whichever leg it was."""
        with caplog.at_level("INFO", logger=work_loop.logger.name):
            await self._sweep(minted=minted, past_posts_evidence=SWEEP_EVIDENCE)
        said = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("past_posts_lifecycle_sweep")
        ]
        assert said == (
            [] if line is None else [f"past_posts_lifecycle_sweep j-sweep: {line}"]
        )


class TestSeamAbsenceParksTheDependentKind:
    def test_no_transport_parks_deliver_outbox(self):
        registry = build_registry(full_deps(transport=None))
        assert isinstance(registry["deliver_outbox"], Parked)
        assert "transport" in registry["deliver_outbox"].reason

    def test_no_email_provider_parks_send_email(self):
        """The reason must NAME the config: "no email provider
        configured" without the variable names sends the reader to the wrong
        place. Resend is the provider, so the reason does not say the ack is
        open: the variables are the whole of what is missing."""
        registry = build_registry(full_deps(email=None))
        assert isinstance(registry["send_email"], Parked)
        reason = registry["send_email"].reason
        assert reason == (
            "no email provider configured — set RESEND_API_KEY and EMAIL_FROM"
        )

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

    def test_no_meta_or_no_transit_parks_publish_pipeline_by_name(self):
        """The ladder calls fetch, transit and meta; each absence parks the
        kind naming itself."""
        registry = build_registry(full_deps(meta=None))
        assert isinstance(registry["publish_pipeline"], Parked)
        assert registry["publish_pipeline"].reason.startswith("meta is not wired")
        registry = build_registry(full_deps(transit=None))
        assert isinstance(registry["publish_pipeline"], Parked)
        assert registry["publish_pipeline"].reason.startswith("transit is not wired")

    def test_no_poll_does_NOT_park_the_reconciler(self):
        """A missing poll does not park the reconciler. The property
        this class pins — a missing seam parks its DEPENDENT kind — has four
        instances above, and the reconciler is not one of them: its sweep
        returns `ladder_due` rows (which need the poll) AND `notify_window`
        rows (which need nothing), so parking the kind would park a half that
        does not depend on the absent seam, the customer notification with
        it."""
        registry = build_registry(full_deps(poll=None))
        assert not isinstance(registry["reconcile_ambiguous"], Parked)


class TestTheActivationNudgeIsBuiltOff:
    """The sweep emails real people, so it is live only when its switch,
    an email provider and a web origin are ALL there, and a parked one names
    every gate that is closed and none that is open."""

    ORIGIN = "https://app.example"

    def _nudge(self, **over):
        return build_registry(full_deps(**over))["activation_nudge_sweep"]

    def test_the_default_build_parks_it_naming_the_switch(self):
        """Every seam supplied but the REAL default config, which has neither
        the switch nor an origin: both are named, the provider is not."""
        entry = self._nudge(config=WorkerConfig())
        assert isinstance(entry, Parked)
        assert (
            entry.reason
            == "TARGET_ACTIVATION_NUDGE_ENABLED is off; no web app origin for the email's link"
        )

    def test_switched_on_without_an_email_provider_it_stays_parked(self):
        config = WorkerConfig(activation_nudge_enabled=True, web_app_origin=self.ORIGIN)
        entry = self._nudge(email=None, config=config)
        assert isinstance(entry, Parked)
        assert entry.reason == "no email provider configured"

    def test_switched_on_without_a_web_origin_it_stays_parked(self):
        entry = self._nudge(config=WorkerConfig(activation_nudge_enabled=True))
        assert isinstance(entry, Parked)
        assert entry.reason == "no web app origin for the email's link"

    def test_a_provider_and_an_origin_do_not_arm_it_without_the_switch(self):
        entry = self._nudge(config=WorkerConfig(web_app_origin=self.ORIGIN))
        assert isinstance(entry, Parked)
        assert entry.reason == "TARGET_ACTIVATION_NUDGE_ENABLED is off"

    def test_all_three_gates_open_make_it_live(self):
        """The positive control: without it, "parks correctly" cannot be told
        from "parks always"."""
        config = WorkerConfig(activation_nudge_enabled=True, web_app_origin=self.ORIGIN)
        assert not isinstance(self._nudge(config=config), Parked)

    async def test_the_live_sweep_runs_with_the_configs_bounds(self, monkeypatch):
        """The entry hands the sweep the config's cap, window and idle
        time, and the origin for its links, rather than defaults of its own."""
        seen = {}

        async def sweep_stalled(session, **kwargs):
            seen.update(session=session, **kwargs)
            return 0

        monkeypatch.setattr(work_loop.activation_nudge, "sweep_stalled", sweep_stalled)
        config = WorkerConfig(
            activation_nudge_enabled=True,
            web_app_origin=self.ORIGIN,
            activation_nudge_limit=7,
            activation_nudge_since_days=11,
            activation_nudge_stall_seconds=1234,
        )
        session = object()
        await self._nudge(config=config)(session, {"id": "job-1"})
        assert seen == {
            "session": session,
            "since_days": 11,
            "stall_seconds": 1234,
            "limit": 7,
            "web_app_origin": self.ORIGIN,
        }


class TestReconcilerSweepBranchesOnItsReason:
    """The `notify_window` half of the reconciler sweep.

    `fn_reconciler_sweep` tags its rows (`04_functions.sql`), and the executor branches on
    the tag: a notify row is notified, not polled as if the ladder were due.
    """

    async def _drive(self, monkeypatch, *, rows, deps):
        notified, reconciled = [], []

        async def fake_sweep(session, *, limit, notify_after_seconds):
            return rows

        async def fake_notify(session, *, intent_id, workspace_id, web_app_origin):
            notified.append((intent_id, web_app_origin))
            return 1

        async def fake_reconcile(session, *, intent_id, **kw):
            reconciled.append(intent_id)
            return "pending"

        monkeypatch.setattr(work_loop.reconciler, "sweep_due", fake_sweep)
        monkeypatch.setattr(work_loop.reconciler, "notify_parked_customer", fake_notify)
        monkeypatch.setattr(work_loop.reconciler, "reconcile_intent", fake_reconcile)
        registry = build_registry(deps)
        await registry["reconcile_ambiguous"](_FakeSession(), _reconcile_job())
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
            monkeypatch, rows=rows, deps=full_deps(poll=_in_progress)
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

    async def test_the_ladder_count_is_read_after_the_rows_workspace_is_claimed(
        self, monkeypatch
    ):
        """`post_intents` is policy-covered and the sweep's session carries no
        tenant: a count read before the claim is 0 for every row, so every step
        is the first and the ladder never exhausts. The claim
        precedes the read, and what the row says is what the ladder gets."""
        rows = [{"intent_id": "i-6", "workspace_id": "ws-1", "reason": "ladder_due"}]
        seen = []

        async def fake_sweep(session, *, limit, notify_after_seconds):
            return rows

        async def fake_reconcile(session, *, intent_id, checks, **kw):
            seen.append(checks)
            return "pending"

        monkeypatch.setattr(work_loop.reconciler, "sweep_due", fake_sweep)
        monkeypatch.setattr(work_loop.reconciler, "reconcile_intent", fake_reconcile)
        session = _FakeSession(rows=[7])  # the evidence on the row: seven checks
        registry = build_registry(full_deps(poll=_in_progress))
        await registry["reconcile_ambiguous"](session, _reconcile_job())
        assert seen == [7]
        sql = [s for s, _ in session.statements]
        claim = next(i for i, s in enumerate(sql) if "app.tenant_id" in s)
        count = next(i for i, s in enumerate(sql) if "'checks'" in s)
        assert claim < count, (
            "the row's workspace is claimed before its ladder is counted"
        )

    async def _unheard(self, monkeypatch, session):
        """One notify row whose workspace has nowhere to receive the notice,
        swept on *session*: the beat's answer."""
        from src.services.target import outbox

        rows = [{"intent_id": "i-5", "workspace_id": "ws-1", "reason": "notify_window"}]

        async def fake_sweep(session, *, limit, notify_after_seconds):
            return rows

        async def fake_notify(session, **kw):
            return outbox.UNDELIVERABLE

        monkeypatch.setattr(work_loop.reconciler, "sweep_due", fake_sweep)
        monkeypatch.setattr(work_loop.reconciler, "notify_parked_customer", fake_notify)
        registry = build_registry(full_deps())
        return await registry["reconcile_ambiguous"](session, _reconcile_job())

    async def test_a_workspace_with_no_surface_makes_the_sweep_undeliverable(
        self, monkeypatch
    ):
        """The executor must not report a delivery it could not make. The
        attempt is recorded in the row's workspace, with the window the config
        names."""
        from src.services.target import outbox

        session = _FakeSession(rows=[{"id": "i-5"}])  # the stamp landed: a fresh record
        assert await self._unheard(monkeypatch, session) == outbox.UNDELIVERABLE
        claim, stamp = session.statements
        assert "app.tenant_id" in claim[0] and claim[1]["v0"] == "ws-1"
        assert "notify_attempted_at" in stamp[0]
        assert stamp[1] == {
            "intent": "i-5",
            "age": float(WorkerConfig().reconcile_notify_after_seconds),
        }

    async def test_inside_the_window_an_unheard_notice_is_a_clean_run(
        self, monkeypatch
    ):
        """Bounded, not silenced: once recorded, the condition stands on the
        ledger, and re-reporting it every 60 s would park a job per beat."""
        session = _FakeSession()  # the stamp is inside the window: nothing recorded
        assert await self._unheard(monkeypatch, session) is None

    async def test_the_sweep_keys_match_what_the_door_returns(self):
        """The `KeyError` a parked kind would hide.

        The door returns `o_intent_id`/`o_workspace_id`; the consumer reads
        `intent_id`/`workspace_id`. `sweep_due` aliases in the SELECT, so
        this asserts the two spellings agree at the one place that chooses
        them — reading the shipped SQL rather than restating it.
        """
        import inspect

        src = inspect.getsource(work_loop.reconciler.sweep_due)
        for alias in ("AS intent_id", "AS workspace_id", "AS reason"):
            assert alias in src, f"sweep_due must alias {alias}"


class TestReconcilerRowsFailAlone:
    """One row of the reconciler sweep fails alone (the executor's
    docstring has the why)."""

    def _rows(self, n):
        return [
            {"intent_id": f"i-{k}", "workspace_id": "ws-1", "reason": "ladder_due"}
            for k in range(n)
        ]

    def _own_transactions(self, monkeypatch):
        """`make_session_for` replaced by a factory that records how each of
        its transactions ended and, like the real one, marks the task as in a
        transaction while it is open. A savepoint inside one fails the test:
        in the loop's mode nothing should nest."""
        from contextlib import asynccontextmanager

        ended = []

        class _NoSavepoints(_FakeSession):
            def begin_nested(self):
                raise AssertionError("a row nested a savepoint in its own transaction")

        def make_session_for(engine):
            def session_for(job):
                @asynccontextmanager
                async def ctx():
                    token = work_loop.unit_of_work._IN_TRANSACTION.set(True)
                    try:
                        yield _NoSavepoints()
                    except BaseException:
                        ended.append("rolled_back")
                        raise
                    finally:
                        work_loop.unit_of_work._IN_TRANSACTION.reset(token)
                    ended.append("committed")

                return ctx()

            return session_for

        monkeypatch.setattr(
            work_loop.unit_of_work, "make_session_for", make_session_for
        )
        return ended

    def _sweep(self, monkeypatch, rows, reconcile):
        async def fake_sweep(session, *, limit, notify_after_seconds):
            return rows[:limit]

        monkeypatch.setattr(work_loop.reconciler, "sweep_due", fake_sweep)
        monkeypatch.setattr(work_loop.reconciler, "reconcile_intent", reconcile)

    async def test_no_transaction_carries_more_than_one_row(self, monkeypatch):
        """The drain's bound, pinned with more due rows than it
        allows: one transaction per row, so none can collect the writing
        savepoints a batch of `MAX_WRITING_SAVEPOINTS + 1` rows would."""
        from src.services.target.offboarding import MAX_WRITING_SAVEPOINTS

        ended = self._own_transactions(monkeypatch)
        rows = self._rows(MAX_WRITING_SAVEPOINTS + 1)
        reconciled = []

        async def fake_reconcile(session, *, intent_id, **kw):
            reconciled.append(intent_id)
            return "pending"

        self._sweep(monkeypatch, rows, fake_reconcile)
        registry = build_registry(
            full_deps(poll=_in_progress, config=WorkerConfig(reconcile_limit=len(rows)))
        )
        assert await registry["reconcile_ambiguous"](None, _reconcile_job()) is None
        assert reconciled == [r["intent_id"] for r in rows]
        # The sweep's one short read, then one transaction per row.
        assert ended == ["committed"] * (1 + len(rows))

    async def test_the_provider_is_asked_with_no_transaction_open(self, monkeypatch):
        """The egress floor refuses a provider call made inside a
        transaction (`unit_of_work.in_transaction()` is what it reads), so a
        ladder row asks before its transaction opens."""
        self._own_transactions(monkeypatch)
        asked = []

        async def poll(*, intent_id, workspace_id):
            asked.append((intent_id, work_loop.unit_of_work.in_transaction()))
            return "IN_PROGRESS"

        async def fake_reconcile(session, *, intent_id, status_code, **kw):
            assert status_code == "IN_PROGRESS", "the verdict gets what was observed"
            return "pending"

        self._sweep(monkeypatch, self._rows(2), fake_reconcile)
        registry = build_registry(full_deps(poll=poll))
        await registry["reconcile_ambiguous"](None, _reconcile_job())
        assert asked == [("i-0", False), ("i-1", False)]

    async def test_a_failed_row_rolls_back_alone_and_the_first_failure_is_raised(
        self, monkeypatch, caplog
    ):
        """Two rows fail and one resolves between them: the resolved row
        commits, each failed row rolls back only itself, and the beat still
        fails, with the FIRST failure, so the alarm is as loud as it was."""
        ended = self._own_transactions(monkeypatch)
        reconciled = []

        async def fake_reconcile(session, *, intent_id, **kw):
            if intent_id == "i-0":
                raise ValueError(f"intent {intent_id} matched no row")
            if intent_id == "i-2":
                raise RuntimeError("the poll seam broke")
            reconciled.append(intent_id)
            return "posted"

        self._sweep(monkeypatch, self._rows(3), fake_reconcile)
        registry = build_registry(full_deps(poll=_in_progress))
        with caplog.at_level("ERROR", logger=work_loop.__name__):
            with pytest.raises(ValueError, match="intent i-0 matched no row"):
                await registry["reconcile_ambiguous"](None, _reconcile_job())
        assert reconciled == ["i-1"], "the row behind a failure still ran"
        assert ended == ["committed", "rolled_back", "committed", "rolled_back"]
        failed = [r for r in caplog.records if "NO verdict was recorded" in r.message]
        assert [r.args[1] for r in failed] == ["i-0", "i-2"], "every failure is logged"
        assert any("2 of 3 row(s) failed" in r.message for r in caplog.records)

    def _mixed(self, monkeypatch, *, ladder_fails):
        """A ladder row, a notice nobody can hear (n-1) and a delivered one
        (n-2); returns the (intent, workspace) pairs the beat recorded as
        unheard."""
        from src.services.target import outbox

        self._own_transactions(monkeypatch)
        rows = [
            {"intent_id": "i-0", "workspace_id": "ws-1", "reason": "ladder_due"},
            {"intent_id": "n-1", "workspace_id": "ws-2", "reason": "notify_window"},
            {"intent_id": "n-2", "workspace_id": "ws-3", "reason": "notify_window"},
        ]
        recorded = []

        async def fake_reconcile(session, *, intent_id, **kw):
            if ladder_fails:
                raise ValueError(f"intent {intent_id} matched no row")
            return "pending"

        async def fake_notify(session, *, intent_id, **kw):
            return outbox.UNDELIVERABLE if intent_id == "n-1" else 1

        async def fake_record(session, *, intent_id, workspace_id, **kw):
            recorded.append((intent_id, workspace_id))
            return outbox.UNDELIVERABLE

        self._sweep(monkeypatch, rows, fake_reconcile)
        monkeypatch.setattr(work_loop.reconciler, "notify_parked_customer", fake_notify)
        monkeypatch.setattr(work_loop.reconciler, "record_no_surface", fake_record)
        return recorded

    async def test_a_failed_beat_stays_loud_and_records_no_unheard_notice(
        self, monkeypatch
    ):
        """A beat with a failed ladder row AND a notice nobody can hear fails
                with the row's error: `UNDELIVERABLE` never stands in for the alarm.
        Nor is the notice recorded, so the first beat that returns
                signals it."""
        recorded = self._mixed(monkeypatch, ladder_fails=True)
        registry = build_registry(full_deps(poll=_in_progress))
        with pytest.raises(ValueError, match="intent i-0 matched no row"):
            await registry["reconcile_ambiguous"](None, _reconcile_job())
        assert recorded == []

    async def test_a_returning_beat_records_only_its_unheard_notices(self, monkeypatch):
        from src.services.target import outbox

        recorded = self._mixed(monkeypatch, ladder_fails=False)
        registry = build_registry(full_deps(poll=_in_progress))
        got = await registry["reconcile_ambiguous"](None, _reconcile_job())
        assert got == outbox.UNDELIVERABLE
        assert recorded == [("n-1", "ws-2")], "a delivered notice is not an attempt"

    async def test_a_passed_session_gets_a_savepoint_per_row(self, monkeypatch):
        """The unit seam: a caller that passes its own session keeps it, and
        each row nests a savepoint on it rather than opening a transaction."""
        ended = self._own_transactions(monkeypatch)
        nested = []

        class _Counting(_FakeSession):
            def begin_nested(self):
                nested.append(1)
                return super().begin_nested()

        async def fake_reconcile(session, *, intent_id, **kw):
            return "pending"

        self._sweep(monkeypatch, self._rows(2), fake_reconcile)
        registry = build_registry(full_deps(poll=_in_progress))
        await registry["reconcile_ambiguous"](_Counting(), _reconcile_job())
        assert len(nested) == 2
        assert ended == [], "the seam's session is used, never a new one"


class TestAJobThatReachedNobodyIsNotASuccess:
    """A producer that reaches nobody on an empty binding list says so
    in its verdict, and the verdict decides the terminal state: the ledger
    records no clean run for a message nobody received, and the cadence does
    not repeat into the void forever."""

    def _loop(self, executor):
        from src.services.target.work_loop import WorkLoop

        finalized = {}

        class _Jobs:
            JobFenced = work_loop.jobs.JobFenced
            # The loop reads the sentinel and the budget helpers
            # from the module it was handed; the double mirrors them.
            SELF_FINALIZED = work_loop.jobs.SELF_FINALIZED
            budget_exhausted = staticmethod(work_loop.jobs.budget_exhausted)
            backoff_seconds = staticmethod(work_loop.jobs.backoff_seconds)

            @staticmethod
            async def finalize_job(session, job_id, token, terminal_state):
                finalized["state"] = terminal_state

            @staticmethod
            async def reschedule_job(*a, **k):  # pragma: no cover
                raise AssertionError("must not reschedule")

        loop = WorkLoop.__new__(WorkLoop)
        loop._registry = {"deliver_outbox": executor}
        loop._config = WorkerConfig()
        loop._past_posts_capability = None
        loop.processed = loop.parked = loop.failures = 0
        loop.fenced = loop.undeliverable = loop.consecutive_errors = 0
        loop.exhausted = 0

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
            "counted apart from real deliveries — that separation IS the property"
        )

    async def test_an_ordinary_executor_still_succeeds(self, monkeypatch):
        """The positive control. Every existing executor returns None, and a
        change that made THEM stop succeeding would be worse than the defect
        this class pins."""

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
        self.info = {}
        self.rows = rows or []
        self.statements = []

    def begin_nested(self):
        # The exhausted notice rides a savepoint. When its body
        # raises, the double drops what ran inside it, as ROLLBACK TO
        # SAVEPOINT does, and passes the error on.
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _sp():
            mark = len(self.statements)
            try:
                yield self
            except BaseException:
                del self.statements[mark:]
                raise

        return _sp()

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), params))
        rows = self.rows

        class _R:
            rowcount = 1  # an UPDATE's report, for the re-arm

            def mappings(self_inner):
                class _M:
                    def first(self_m):
                        return rows[0] if rows else None

                return _M()

            def first(self_inner):
                return rows[0] if rows else None

            def scalar(self_inner):
                return rows[0] if rows else None

            def fetchone(self_inner):
                return rows[0] if rows else None

        return _R()


class TestPlanSlotAdapterMapsThePayload:
    async def test_adapter_resolves_the_ref_then_calls_the_executor(self, monkeypatch):
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

        async def fake_sweep(session, *, limit, late_seconds, workspace_id):
            swept.append((limit, late_seconds))
            return {"prompted": 0, "advanced": 0}

        monkeypatch.setattr(work_loop.prompts, "sweep_due_prompts", fake_sweep)
        session = _FakeSession(
            rows=[
                {
                    "provider_account_ref": "ig-acct-9",
                    "state": "active",
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
        # The workspace's mode is not copied onto the story (`ck_intent_approval`):
        # a story is born deciding nothing, and its serve decides.
        assert "approval_mode" not in seen
        # The payload crosses jsonb as a string POSTGRES rendered, so the
        # resolve query casts it server-side — the executor receives
        # the row's datetime and no Python parser ever sees the string.
        assert seen["slot_at"] is resolved_slot
        bound = session.statements[0][1]
        assert bound["slot"] == "2026-08-21T10:00:00+00:00", (
            "the raw payload string must ride to Postgres unmodified"
        )
        assert swept == [(1, WorkerConfig().planned_late_seconds)], (
            "a minted intent triggers the same-beat prompt fast path, at the"
            " worker's own late window"
        )

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
            registry={"refresh_credential": Parked("no executor exists")},
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
    """An unguarded claim error would kill the lane task silently. The lane
    survives transient errors loudly (logged, counted) and dies LOUDLY — by
    raising into the supervisor — only when they persist past the configured
    ceiling."""

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


ACTIVE_ROW = {
    "external_ref": "-777",
    "workspace_id": "ws-1",
    "state": "active",
    "deliverable": True,
}


class TestDeliverOutboxRetiresAGoneChat:
    """The deliverer's definitive "chat gone" ends the hold and retires the
    binding — or follows a group that became a supergroup."""

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
        monkeypatch.setattr(work_loop.bindings, "_repoint", repoint)
        return seen

    async def test_a_kicked_bot_revokes_the_binding(self, gone):
        session = _FakeSession(rows=[{**ACTIVE_ROW, "external_ref": "-100777"}])
        await self._registry()["deliver_outbox"](session, self._job())
        assert gone["revoked"] == ["b-1"] and gone["repointed"] == []

    async def test_a_supergroup_upgrade_follows_the_chat(self, gone):
        gone["migrate_to"] = "-1009999"
        session = _FakeSession(rows=[ACTIVE_ROW])
        await self._registry()["deliver_outbox"](session, self._job())
        assert gone["repointed"] == [("b-1", "-1009999")] and gone["revoked"] == []

    async def test_a_successor_another_workspace_holds_revokes_instead(self, gone):
        gone["migrate_to"] = "-1009999"
        gone["repoint_ok"] = False
        session = _FakeSession(rows=[ACTIVE_ROW])
        await self._registry()["deliver_outbox"](session, self._job())
        assert gone["revoked"] == ["b-1"]


class TestDeliverOutboxSkipsARevokedBinding:
    """A job whose binding no card may go to sends nothing — one minted before
    an admin removed the group (or the bot was kicked), or one for a private
    chat whose person does not belong: the hold ends before a poller is built,
    and what is left of the binding's queue is retired. The answer
    is the binding read's own (`bindings.deliverable_binding_where`), so an
    ACTIVE binding it refuses is the case that tells a check of it from a
    check of `state`."""

    async def test_no_poller_runs_for_a_binding_no_card_may_go_to(self, monkeypatch):
        from types import SimpleNamespace

        from src.services.target import bindings

        built = []

        class _Poller:
            def __init__(self, *a, **kw):
                built.append(kw)

        monkeypatch.setattr(work_loop.outbox, "OutboxPoller", _Poller)
        transport = SimpleNamespace(for_chat=lambda ref: lambda row: None)
        registry = build_registry(full_deps(transport=transport))
        session = _FakeSession(rows=[{**ACTIVE_ROW, "deliverable": False}])
        job = {
            "id": "j-r",
            "kind": "deliver_outbox",
            "workspace_id": "ws-1",
            "serialization_key": "binding:b-1",
            "payload": {"binding_id": "b-1"},
        }
        assert await registry["deliver_outbox"](session, job) is None
        assert built == [], "a binding no card may go to got a sender"
        retire = [sql for sql, _ in session.statements if "superseded" in sql]
        assert len(retire) == 1 and "'sending'" in retire[0], (
            "the binding's leftover queue was not retired"
        )
        read = [sql for sql, _ in session.statements if "AS deliverable" in sql]
        assert len(read) == 1 and bindings.DELIVERABLE_BINDING_WHERE in read[0]


class TestWeightedCategorySelection:
    """Selection draws by the workspace's mix, keyed on the CONNECTED
    FOLDER. A seeded generator makes the draw
    deterministic; the statements the executor sends are pinned by shape,
    since the port's own SQL is the contract."""

    def _session(self, *, rows, counts, pick_id="media-1", intent_id="intent-1"):
        # `pick_id=None`: no draw is expected, and the no-media notice's
        # UPDATE … RETURNING finds nothing due.
        answers = (
            [rows, counts, [{"id": pick_id}], [{"id": intent_id}]]
            if pick_id is not None
            else [rows, counts, []]
        )

        class _S:
            def __init__(self):
                self.statements = []
                #: How each savepoint ended: "released", or "rolled back" by
                #: a raise inside it. Kept apart from `statements`, whose
                #: order the tests below pin.
                self.savepoints = []

            def begin_nested(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def savepoint():
                    try:
                        yield self
                    except BaseException:
                        self.savepoints.append("rolled back")
                        raise
                    self.savepoints.append("released")

                return savepoint()

            async def execute(self, stmt, params=None):
                self.statements.append((str(stmt), params))
                rows_ = answers.pop(0) if answers else []

                class _M:
                    def all(self_inner):
                        return rows_

                    def first(self_inner):
                        return rows_[0] if rows_ else None

                    def __iter__(self_inner):
                        return iter(rows_)

                class _R:
                    def mappings(self_inner):
                        return _M()

                    def first(self_inner):
                        r = rows_[0] if rows_ else None
                        if r is None:
                            return None
                        return tuple(r.values())

                    def all(self_inner):
                        return [tuple(r.values()) for r in rows_]

                return _R()

        return _S()

    async def _plan(self, session, rng):
        import random

        from src.services.target import content_runway
        from src.services.target.scheduler import execute_plan_slot

        return await execute_plan_slot(
            session,
            workspace_id="ws-1",
            ig_account_id="acct-1",
            slot_at="2026-09-06T22:00:00+00:00",
            provider_account_ref="ref",
            no_media_notice_after_seconds=86400,
            low_runway_days=content_runway.LOW_RUNWAY_DAYS,
            rng=random.Random(rng),
        )

    ANNOUNCEMENTS, BEHIND_THE_SCENES, EVENTS, OLD = (
        "s-announcements",
        "s-behind-the-scenes",
        "s-events",
        "s-old",
    )

    def _rows(self, **ratios):
        return [{"source_id": sid, "ratio": r} for sid, r in ratios.items()]

    async def test_the_draw_follows_the_weights_over_folders_that_have_media(self):
        picks = {}
        for seed in range(40):
            s = self._session(
                rows=self._rows(
                    **{self.ANNOUNCEMENTS: 0.7, self.BEHIND_THE_SCENES: 0.3}
                ),
                counts=[
                    {"source_id": self.ANNOUNCEMENTS, "n": 5},
                    {"source_id": self.BEHIND_THE_SCENES, "n": 5},
                ],
            )
            out = await self._plan(s, seed)
            assert out.intent_id == "intent-1"
            rows_sql = s.statements[0][0]
            assert "FROM media_sources" in rows_sql and "removed" in rows_sql
            pick_sql, pick_params = s.statements[2]
            assert (
                "m.source_id = CAST(:source_id AS uuid)" in pick_sql
                and "state = 'available'" in pick_sql
            )
            assert "m.category" not in pick_sql, "the name is a label, never a key"
            picks[pick_params["source_id"]] = picks.get(pick_params["source_id"], 0) + 1
        assert set(picks) == {self.ANNOUNCEMENTS, self.BEHIND_THE_SCENES}
        assert picks[self.ANNOUNCEMENTS] > picks[self.BEHIND_THE_SCENES], picks

    async def test_never_posted_files_go_in_the_folders_shuffled_order(self):
        """A batch exported together indexes together, and oldest-first would
        serve it in a row. The pick orders never-posted files by the row's
        random id — a stable per-file shuffle — and posted ones
        least-recently-posted first."""
        s = self._session(
            rows=self._rows(**{self.ANNOUNCEMENTS: 1.0}),
            counts=[{"source_id": self.ANNOUNCEMENTS, "n": 9}],
        )
        await self._plan(s, 3)
        pick_sql, pick_params = s.statements[2]
        assert "ORDER BY m.last_posted_at NULLS FIRST, m.id LIMIT 1" in pick_sql
        assert "created_at" not in pick_sql and "OFFSET" not in pick_sql
        assert pick_params["source_id"] == self.ANNOUNCEMENTS

    async def test_a_weighted_folder_with_no_media_is_never_drawn(self):
        for seed in range(20):
            s = self._session(
                rows=self._rows(
                    **{self.ANNOUNCEMENTS: 0.7, self.BEHIND_THE_SCENES: 0.3}
                ),
                counts=[{"source_id": self.ANNOUNCEMENTS, "n": 5}],
            )
            await self._plan(s, seed)
            assert s.statements[2][1]["source_id"] == self.ANNOUNCEMENTS

    async def test_an_automatic_folder_shares_by_its_media(self):
        picks = {}
        for seed in range(60):
            s = self._session(
                rows=self._rows(
                    **{
                        self.ANNOUNCEMENTS: 0.7,
                        self.BEHIND_THE_SCENES: 0.3,
                        self.EVENTS: None,
                    }
                ),
                counts=[
                    {"source_id": self.ANNOUNCEMENTS, "n": 50},
                    {"source_id": self.BEHIND_THE_SCENES, "n": 50},
                    {"source_id": self.EVENTS, "n": 50},
                ],
            )
            await self._plan(s, seed)
            sid = s.statements[2][1]["source_id"]
            picks[sid] = picks.get(sid, 0) + 1
        assert set(picks) == {self.ANNOUNCEMENTS, self.BEHIND_THE_SCENES, self.EVENTS}
        assert picks[self.ANNOUNCEMENTS] > picks[self.EVENTS] > 0, picks

    async def test_an_off_folder_is_never_drawn(self):
        for seed in range(10):
            s = self._session(
                rows=self._rows(**{self.ANNOUNCEMENTS: 1.0, self.OLD: 0.0}),
                counts=[
                    {"source_id": self.ANNOUNCEMENTS, "n": 5},
                    {"source_id": self.OLD, "n": 5},
                ],
            )
            await self._plan(s, seed)
            assert s.statements[2][1]["source_id"] == self.ANNOUNCEMENTS
        # Every weighted folder empty: nothing is drawn — an Off folder, or a
        # removed one (never in `rows`), is not a pool to fall back on.
        s = self._session(
            rows=self._rows(**{self.ANNOUNCEMENTS: 1.0, self.OLD: 0.0}),
            counts=[
                {"source_id": self.OLD, "n": 5},
                {"source_id": self.EVENTS, "n": 2},
            ],
            pick_id=None,
        )
        out = await self._plan(s, 3)
        assert out.intent_id is None
        assert not any("SELECT m.id FROM media_items" in st[0] for st in s.statements)

    async def test_without_a_mix_every_folder_is_automatic_and_shares_by_media(self):
        picks = {}
        for seed in range(30):
            s = self._session(
                rows=self._rows(
                    **{self.ANNOUNCEMENTS: None, self.BEHIND_THE_SCENES: None}
                ),
                counts=[
                    {"source_id": self.ANNOUNCEMENTS, "n": 90},
                    {"source_id": self.BEHIND_THE_SCENES, "n": 10},
                ],
            )
            await self._plan(s, seed)
            sid = s.statements[2][1]["source_id"]
            picks[sid] = picks.get(sid, 0) + 1
        assert picks.get(self.ANNOUNCEMENTS, 0) > picks.get(self.BEHIND_THE_SCENES, 0)

    async def test_when_no_connected_folder_has_media_nothing_is_drawn(self):
        s = self._session(
            rows=self._rows(**{self.ANNOUNCEMENTS: 1.0}), counts=[], pick_id=None
        )
        out = await self._plan(s, 1)
        assert out.intent_id is None
        assert not any("SELECT m.id FROM media_items" in st[0] for st in s.statements)
        rows_sql = s.statements[0][0]
        assert "s.state <> 'error'" in rows_sql, "a folder gone in Drive is never drawn"
        # The full selection rule still holds on the pick: the workspace-wide
        # locks and this account's recent ones ride the eligibility clause.
        counts_sql = s.statements[1][0]
        assert (
            "FROM post_locks l" in counts_sql
            and "l.ig_account_id = :acct" in counts_sql
        )

    async def test_a_runway_notice_nobody_receives_does_not_ride_the_mint(
        self, monkeypatch
    ):
        """The runway notice never decides the slot: owed and heard by
        nobody, its verdict is not carried back, so the job the mint belongs
        to succeeds. It is settled after the mint, in a savepoint."""
        from src.services.target import content_runway, outbox
        from src.services.target.scheduler import SlotOutcome

        settled = []

        async def after_mint(session, **kwargs):
            # What the session last sent when the notice was asked for.
            settled.append((session.statements[-1][0], kwargs))
            return outbox.UNDELIVERABLE

        monkeypatch.setattr(content_runway, "after_mint", after_mint)
        s = self._session(
            rows=self._rows(**{self.ANNOUNCEMENTS: 1.0}),
            counts=[{"source_id": self.ANNOUNCEMENTS, "n": 3}],
        )
        assert await self._plan(s, 1) == SlotOutcome(intent_id="intent-1")
        # Positive control: the notice was asked for, after the mint, with the
        # pool less the minted file.
        ((last_sent, asked),) = settled
        assert "INSERT INTO post_intents" in last_sent
        assert asked == {
            "workspace_id": "ws-1",
            "ig_account_id": "acct-1",
            "eligible": 2,
            "below_days": content_runway.LOW_RUNWAY_DAYS,
        }
        assert s.savepoints == ["released"]

    async def test_a_runway_notice_that_raises_never_costs_the_mint(
        self, monkeypatch, caplog
    ):
        """A failure writing the notice rolls back its savepoint alone and is
        logged: the minted intent stands and is returned, with no notice."""
        from src.services.target import content_runway
        from src.services.target.scheduler import SlotOutcome

        async def after_mint(session, **kwargs):
            raise RuntimeError("the latch row could not be written")

        monkeypatch.setattr(content_runway, "after_mint", after_mint)
        s = self._session(
            rows=self._rows(**{self.ANNOUNCEMENTS: 1.0}),
            counts=[{"source_id": self.ANNOUNCEMENTS, "n": 3}],
        )
        with caplog.at_level("ERROR", logger="src.services.target.scheduler"):
            out = await self._plan(s, 1)

        assert out == SlotOutcome(intent_id="intent-1")
        assert "INSERT INTO post_intents" in s.statements[-1][0], "the mint was sent"
        assert s.savepoints == ["rolled back"], "and the notice failed in its own"
        (logged,) = [r for r in caplog.records if "NOT written" in r.getMessage()]
        assert logged.levelname == "ERROR" and logged.exc_info is not None


class TestTheBudgetCeiling:
    """A failing job is rescheduled with the lane's
    backoff until its budget — attempts or deadline — is spent; then it ends
    `failed`, and a tenant kind the sweeps do not re-mint tells the workspace
    in its own words."""

    def _loop(self, monkeypatch, *, executor, bindings=("b-1",), rows=None):
        from datetime import datetime, timezone

        from src.services.target import outbox, prompts
        from src.services.target.work_loop import WorkLoop, WorkerConfig

        calls = {"finalized": [], "rescheduled": [], "notices": []}

        class _Jobs:
            JobFenced = work_loop.jobs.JobFenced
            SELF_FINALIZED = work_loop.jobs.SELF_FINALIZED
            budget_exhausted = staticmethod(work_loop.jobs.budget_exhausted)
            backoff_seconds = staticmethod(work_loop.jobs.backoff_seconds)

            @staticmethod
            async def finalize_job(session, job_id, token, terminal_state):
                calls["finalized"].append(terminal_state)

            @staticmethod
            async def reschedule_job(
                session, job_id, token, *, run_at, restore_attempt
            ):
                calls["rescheduled"].append((run_at, restore_attempt))

        async def push_bindings(session, workspace_id):
            return list(bindings)

        async def fanout_notification(
            session, *, workspace_id, bindings, text, intent_id=None
        ):
            calls["notices"].append((workspace_id, tuple(bindings), text))
            return len(bindings)

        monkeypatch.setattr(work_loop, "jobs", _Jobs)
        monkeypatch.setattr(prompts, "push_bindings", push_bindings)
        monkeypatch.setattr(outbox, "fanout_notification", fanout_notification)
        monkeypatch.setattr(
            work_loop, "utcnow", lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
        )

        loop = WorkLoop.__new__(WorkLoop)
        loop._registry = {"sync_media_source": executor, "deliver_outbox": executor}
        loop._config = WorkerConfig()
        loop._past_posts_capability = None
        loop.processed = loop.parked = loop.failures = 0
        loop.fenced = loop.undeliverable = loop.consecutive_errors = loop.exhausted = 0

        from contextlib import asynccontextmanager

        calls["sessions"] = []

        @asynccontextmanager
        async def _ctx(job):
            session = _FakeSession(rows)
            calls["sessions"].append(session)
            yield session

        loop._session_for = _ctx
        return loop, calls

    @staticmethod
    def _job(**over):
        from datetime import datetime, timedelta, timezone

        base = {
            "id": "j-1",
            "kind": "sync_media_source",
            "workspace_id": "ws-1",
            "lane": "bulk",
            "attempts": 1,
            "max_attempts": 5,
            "deadline_at": datetime(2030, 1, 1, tzinfo=timezone.utc)
            + timedelta(hours=1),
            "lease_token": "tok",
            "payload": {"v": 1},
        }
        base.update(over)
        return base

    async def test_under_budget_the_job_backs_off_on_the_lane_s_ladder(
        self, monkeypatch
    ):
        from datetime import datetime, timezone

        async def executor(session, job):
            raise RuntimeError("boom")

        loop, calls = self._loop(monkeypatch, executor=executor)
        await loop._run_job(self._job(attempts=2))

        assert loop.failures == 1 and loop.exhausted == 0
        assert calls["finalized"] == [] and calls["notices"] == []
        ((run_at, restored),) = calls["rescheduled"]
        assert restored is False, "a retryable failure keeps its consumed attempt"
        eta = (run_at - datetime(2030, 1, 1, tzinfo=timezone.utc)).total_seconds()
        assert 240 <= eta <= 360, f"bulk rung 2 is 300 s ± 20 %, got {eta}"

    async def test_at_the_attempt_ceiling_the_job_fails_and_the_workspace_hears(
        self, monkeypatch
    ):
        async def executor(session, job):
            raise RuntimeError("boom")

        loop, calls = self._loop(monkeypatch, executor=executor)
        await loop._run_job(self._job(attempts=5, max_attempts=5))

        assert calls["finalized"] == ["failed"] and calls["rescheduled"] == []
        assert loop.exhausted == 1 and loop.failures == 1
        ((ws, bindings, text),) = calls["notices"]
        assert ws == "ws-1" and bindings == ("b-1",)
        assert text == work_loop.FAILURE_NOTICES["sync_media_source"]
        assert "boom" not in text, "the machine detail stays in the log"

    async def test_a_passed_deadline_fails_the_job_whatever_the_attempts(
        self, monkeypatch
    ):
        from datetime import datetime, timedelta, timezone

        async def executor(session, job):
            raise RuntimeError("boom")

        loop, calls = self._loop(monkeypatch, executor=executor)
        await loop._run_job(
            self._job(
                attempts=1,
                deadline_at=datetime(2030, 1, 1, tzinfo=timezone.utc)
                - timedelta(seconds=1),
            )
        )
        assert calls["finalized"] == ["failed"] and len(calls["notices"]) == 1

    async def test_a_re_minted_kind_fails_quietly(self, monkeypatch):
        async def executor(session, job):
            raise RuntimeError("boom")

        loop, calls = self._loop(monkeypatch, executor=executor)
        await loop._run_job(
            self._job(kind="deliver_outbox", attempts=3, max_attempts=3)
        )

        assert calls["finalized"] == ["failed"]
        assert calls["notices"] == [], (
            "the sweep re-mints a sender; 'will not retry' would be a lie"
        )

    SRC = "5c0f2e8a-0b1d-4c3e-9f00-00000000c001"
    INTENT = "5c0f2e8a-0b1d-4c3e-9f00-00000000c0a1"

    @staticmethod
    def _statements(calls, needle):
        return [
            (sql, params)
            for session in calls["sessions"]
            for sql, params in session.statements
            if needle in sql
        ]

    async def test_a_spent_sync_re_arms_its_source_for_tomorrow(self, monkeypatch):
        """Leg 4 nulls `next_sync_at` at mint and only a completed sync re-arms
        it; a sync the loop ends `failed` would leave the source never selected
        again. The exhausted path re-arms it, so "will try again tomorrow" is
        true."""

        async def executor(session, job):
            raise RuntimeError("drive down")

        loop, calls = self._loop(monkeypatch, executor=executor)
        await loop._run_job(
            self._job(attempts=5, payload={"v": 1, "source_id": self.SRC})
        )
        assert calls["finalized"] == ["failed"] and len(calls["notices"]) == 1
        ((sql, params),) = self._statements(calls, "UPDATE media_sources")
        assert "next_sync_at IS NULL" in sql and "state = 'active'" in sql
        assert params["s"] == self.SRC and params["ws"] == "ws-1"
        assert params["secs"] == work_loop.REARM_AFTER_SECONDS

    @pytest.mark.parametrize(
        "payload",
        [{"v": 1}, {"v": 1, "source_id": "not-a-uuid"}],
        ids=["no source", "a source that is not a uuid"],
    )
    async def test_a_spent_sync_without_a_usable_source_re_arms_nothing(
        self, monkeypatch, payload
    ):
        """A malformed id is refused before the cast: the re-arm runs in the
        finalize's own transaction, and a raise there would abort it."""

        async def executor(session, job):
            raise RuntimeError("drive down")

        loop, calls = self._loop(monkeypatch, executor=executor)
        await loop._run_job(self._job(attempts=5, payload=payload))
        assert calls["finalized"] == ["failed"]
        assert self._statements(calls, "UPDATE media_sources") == []

    async def test_a_publish_job_the_loop_fails_parks_its_story_for_review(
        self, monkeypatch
    ):
        """Only `approve` mints `publish_pipeline`; an exception that ESCAPES the
        pipeline past the ceiling would strand the intent with nobody told.
        The story is parked for the workspace's review through the
        pipeline's own door (the card with its buttons, one honest line) —
        the generic exhausted notice is not the answer for a story."""
        from src.services.target import publish_pipeline

        flipped, told = [], []

        async def flip_exhausted(session, job):
            flipped.append(job["id"])
            return {
                "workspace_id": "ws-1",
                "intent_id": "it-1",
                "tz": "UTC",
                "notice": "n",
            }

        async def tell_review(session, **parked):
            told.append(parked["intent_id"])

        async def executor(session, job):
            raise RuntimeError("pool timeout")

        monkeypatch.setattr(publish_pipeline, "flip_exhausted", flip_exhausted)
        monkeypatch.setattr(publish_pipeline, "tell_review", tell_review)
        loop, calls = self._loop(monkeypatch, executor=executor)
        loop._registry["publish_pipeline"] = executor
        job = self._job(kind="publish_pipeline", attempts=5)
        await loop._run_job(job)
        assert calls["finalized"] == ["failed"]
        assert flipped == [job["id"]] and told == ["it-1"] and calls["notices"] == []

    async def test_a_prepare_job_the_loop_fails_parks_its_story_for_review(
        self, monkeypatch
    ):
        """A past-posts story's prepare job whose faults spend its budget parks
        the story it carried for review and tells, as a dead publish job's
        story is: never a story left waiting for a file nobody fetches."""
        from src.services.target import past_posts_preparation_job, publish_pipeline

        flipped, told = [], []

        async def flip_exhausted(session, job):
            flipped.append(job["id"])
            return {
                "workspace_id": "ws-1",
                "intent_id": "it-1",
                "tz": "UTC",
                "notice": "n",
            }

        async def tell_review(session, **parked):
            told.append(parked["intent_id"])

        async def executor(session, job):
            raise RuntimeError("pool timeout")

        monkeypatch.setattr(
            past_posts_preparation_job, "flip_exhausted", flip_exhausted
        )
        monkeypatch.setattr(publish_pipeline, "tell_review", tell_review)
        loop, calls = self._loop(monkeypatch, executor=executor)
        loop._registry["prepare_past_post"] = executor
        job = self._job(kind="prepare_past_post", lane="media", attempts=5)
        await loop._run_job(job)
        assert calls["finalized"] == ["failed"]
        assert flipped == [job["id"]] and told == ["it-1"] and calls["notices"] == []

    async def test_a_failed_courtesy_does_not_undo_a_dead_publish_job_s_park(
        self, monkeypatch
    ):
        """A publish job that spends its budget parks its story for review. The
        flip is the state change; the card restated and the notice are the
        courtesy, which rides the savepoint. A courtesy that fails must not take
        the flip with it: a story left `publishing` keeps its account's publish
        slot, and nothing moves a plain `publishing` row once its job has ended."""
        from src.services.target import publish_pipeline

        async def executor(session, job):
            raise RuntimeError("the fifth untyped crash")

        refused = []

        async def courtesy_fails(session, **kwargs):
            refused.append(kwargs["intent_id"])
            raise RuntimeError("the outbox refused the card")

        loop, calls = self._loop(
            monkeypatch,
            executor=executor,
            rows=[{"state": "publishing", "workspace_id": "ws-1", "tz": "UTC"}],
        )
        loop._registry["publish_pipeline"] = executor
        monkeypatch.setattr(publish_pipeline, "_restate_and_notify", courtesy_fails)
        await loop._run_job(
            self._job(
                kind="publish_pipeline",
                attempts=5,
                payload={"v": 1, "intent_id": self.INTENT},
            )
        )

        assert calls["finalized"] == ["failed"] and loop.exhausted == 1
        assert refused == [self.INTENT], "the courtesy fault never fired"
        flips = [
            params
            for _, params in self._statements(calls, "SET state = 'review_required'")
        ]
        assert flips == [{"intent": self.INTENT, "from_state": "publishing"}], (
            "the courtesy's rollback took the park's flip with it"
        )

    async def test_a_dead_publish_job_with_a_malformed_intent_parks_nothing(
        self, monkeypatch
    ):
        """A malformed `intent_id` is refused before the cast: the flip runs in
        the finalize's own transaction, where a raise would abort the finalize."""

        async def executor(session, job):
            raise RuntimeError("the fifth untyped crash")

        loop, calls = self._loop(monkeypatch, executor=executor)
        loop._registry["publish_pipeline"] = executor
        await loop._run_job(
            self._job(
                kind="publish_pipeline",
                attempts=5,
                payload={"v": 1, "intent_id": "not-a-uuid"},
            )
        )
        assert calls["finalized"] == ["failed"] and loop.exhausted == 1
        assert self._statements(calls, "post_intents") == []

    async def test_a_failed_notice_stops_neither_the_finalize_nor_the_re_arm(
        self, monkeypatch
    ):
        """The notice is a courtesy and rides a savepoint. The re-arm is what
        makes "will try again tomorrow" true, so the notice's rollback must not
        take it: the source would stay active with nothing to select it."""
        from src.services.target import prompts

        async def executor(session, job):
            raise RuntimeError("boom")

        async def broken_bindings(session, workspace_id):
            raise RuntimeError("the bindings query failed")

        loop, calls = self._loop(monkeypatch, executor=executor)
        monkeypatch.setattr(prompts, "push_bindings", broken_bindings)
        await loop._run_job(
            self._job(attempts=5, payload={"v": 1, "source_id": self.SRC})
        )
        assert calls["finalized"] == ["failed"] and calls["notices"] == []
        assert loop.exhausted == 1
        assert [
            params["s"] for _, params in self._statements(calls, "UPDATE media_sources")
        ] == [self.SRC], "the re-arm went down with the notice"

    async def test_a_workspace_with_no_binding_gets_the_log_line_only(
        self, monkeypatch
    ):
        async def executor(session, job):
            raise RuntimeError("boom")

        loop, calls = self._loop(monkeypatch, executor=executor, bindings=())
        await loop._run_job(self._job(attempts=5))
        assert calls["finalized"] == ["failed"]
        assert calls["notices"] == [
            ("ws-1", (), work_loop.FAILURE_NOTICES["sync_media_source"])
        ]

    async def test_a_kind_without_its_own_sentence_gets_the_default(self, monkeypatch):
        async def executor(session, job):
            raise RuntimeError("boom")

        loop, calls = self._loop(monkeypatch, executor=executor)
        loop._registry["some_tenant_kind"] = executor
        await loop._run_job(self._job(kind="some_tenant_kind", attempts=5))
        assert calls["notices"][0][2] == work_loop._DEFAULT_NOTICE


#: An admission's `send_email` payload as `signup_admissions.admit` queues it
#:the address and the claim link's base, never a token or a link.
ADMITTED = "admitted-person@example.com"
CLAIM_BASE = "https://app.example.test/claim/"
ADMISSION_PAYLOAD = {
    "v": 1,
    "to": ADMITTED,
    "template": "admission",
    "params": {"address": ADMITTED, "claim_base": CLAIM_BASE},
}

#: The engine `WorkerDeps` hands the adapter: passed on to the executor, and
#: never connected, since the storage below is in memory.
_NO_ENGINE = object()


class _SyntheticFault(Exception):
    """A fault after the send. Its message names no address, token or hash."""


class _AdmissionStorage:
    """The admission executor's own transactions and the link's store, in
    memory, with the loop's sessions counted beside them.

    A `system_session` stages what is written in it and commits that to
    `committed` only when it ends cleanly; a fault drops the stage. The store
    records what it was given (here, and nowhere else), stages the token's
    hash and marks its session, and the commit fault fires only at the end of
    a marked session: the budget's commit, the loop's reschedule and its
    finalize are other checkpoints, and none of them can take it. The fault
    named fires once. `committed` is this harness's model of the database,
    not evidence about it: the real rollback and the real refused commit are
    `tests/scripts/test_admission_email_fault_gate.py`'s.
    """

    def __init__(self, monkeypatch, *, fault):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        self.events, self.drawn, self.stored_with = [], [], []
        self.committed = {}
        #: Contexts open now: the executor's own sessions, and the loop's.
        self.open = self.loop_open = 0
        pending = {fault}
        draw = sessions.new_token

        def new_token():
            self.events.append("draw")
            self.drawn.append(draw())
            return self.drawn[-1]

        @asynccontextmanager
        async def system_session(engine):
            assert engine is _NO_ENGINE, "the adapter hands its executor deps.engine"
            session = SimpleNamespace(staged={}, stored=False)
            self.events.append("open")
            self.open += 1
            try:
                # Armed as the real one is, so a send inside it would read as
                # one made in a transaction.
                with unit_of_work.transaction_discipline():
                    try:
                        yield session
                    except BaseException:
                        self.events.append("rollback")
                        raise
                    if session.stored and "storage_commit" in pending:
                        pending.discard("storage_commit")
                        self.events.append("commit refused")
                        raise _SyntheticFault("the commit was refused")
                self.committed.update(session.staged)
                self.events.append("commit")
            finally:
                self.open -= 1

        async def debit_budget(session, **kwargs):
            self.events.append("debit")
            return None  # spent: the day's budget holds

        async def store_link(session, *, to, token):
            self.events.append("store")
            self.stored_with.append((to, token))
            session.staged[to] = sessions.token_hash(token)
            session.stored = True
            if "store" in pending:
                pending.discard("store")
                raise _SyntheticFault("the store failed")
            return True

        monkeypatch.setattr(sessions, "new_token", new_token)
        monkeypatch.setattr(email_sender, "system_session", system_session)
        monkeypatch.setattr(email_sender, "debit_budget", debit_budget)
        monkeypatch.setattr(signup_admissions, "store_link", store_link)

    @property
    def secrets(self):
        """What no log line may carry: the address, each token and its hash."""
        return [ADMITTED, *self.drawn, *map(sessions.token_hash, self.drawn)]


class _ProviderTakesIt(RecordingSender):
    """The recording sender, noting as it takes each email what is open in
    the sending task: the executor's sessions, the loop's, and the flag both
    arm (`unit_of_work.in_transaction`)."""

    def __init__(self, storage):
        super().__init__()
        self.storage, self.open_at_send = storage, []

    async def send(self, *, to, subject, body):
        self.storage.events.append("send")
        self.open_at_send.append(
            (self.storage.open, self.storage.loop_open, unit_of_work.in_transaction())
        )
        return await super().send(to=to, subject=subject, body=body)


class TestAnAdmissionEmailWhoseLinkFailsToStore:
    """The provider took an admission's email, and then storing its
    link failed: the store raised, or its transaction's commit was refused.
    Run through the REAL `send_email` adapter (`build_registry`) and
    `_run_job`, on `TestTheBudgetCeiling`'s recorded loop.

    Send first, store second. Before the last attempt the job is
    rescheduled with its attempt kept, and the retry, a new claim with a new
    lease, emails a fresh link whose hash alone is stored: two emails, the
    accepted cost of sending first. On the last attempt the job ends
    `failed`, with no retry, and a system job tells no workspace; the email
    it sent carries a link that was never stored. Nothing is open in the
    sending task at any send, and no log line carries the address, a token
    or a hash."""

    _loop = TestTheBudgetCeiling._loop
    _job = staticmethod(TestTheBudgetCeiling._job)

    #: How the storage session ends, by fault: the store's raise rolls it
    #: back; the refused commit is its end.
    FAULT_ENDS = {"store": "rollback", "storage_commit": "commit refused"}

    @staticmethod
    def _attempt(end):
        """One attempt as the executor and the loop take it: the token drawn,
        the budget spent and committed, the email sent, the link stored in a
        session of its own that ends in *end*, then the loop's one session
        (its reschedule, its finalize, or its failed finalize)."""
        return [
            "draw",
            "open",
            "debit",
            "commit",
            "send",
            "open",
            "store",
            end,
            "loop session",
        ]

    def _composed(self, monkeypatch, *, fault):
        from contextlib import asynccontextmanager

        storage = _AdmissionStorage(monkeypatch, fault=fault)
        provider = _ProviderTakesIt(storage)
        adapter = build_registry(
            WorkerDeps(email=provider, engine=_NO_ENGINE, config=WorkerConfig())
        )["send_email"]
        assert getattr(adapter, "owns_transactions", False)
        loop, calls = self._loop(monkeypatch, executor=adapter)
        loop._registry = {"send_email": adapter}

        @asynccontextmanager
        async def session_for(job):
            # `_loop`'s session, counted, and armed as `make_session_for`
            # arms the real one.
            storage.events.append("loop session")
            storage.loop_open += 1
            try:
                with unit_of_work.transaction_discipline():
                    session = _FakeSession()
                    calls["sessions"].append(session)
                    yield session
            finally:
                storage.loop_open -= 1

        loop._session_for = session_for
        return storage, provider, loop, calls

    def _claimed(self, *, attempts, lease_token):
        """The job as a claim hands it to the loop: its attempts counted, a
        lease of its own, and the payload as it was queued."""
        return self._job(
            id="j-admission",
            kind="send_email",
            workspace_id=None,
            attempts=attempts,
            max_attempts=5,
            lease_token=lease_token,
            payload=copy.deepcopy(ADMISSION_PAYLOAD),
        )

    @pytest.mark.parametrize("fault", ["store", "storage_commit"])
    async def test_a_fault_after_the_send_retries_and_the_retry_mails_a_fresh_link(
        self, monkeypatch, caplog, fault
    ):
        from datetime import datetime, timezone

        caplog.set_level(logging.DEBUG)
        storage, provider, loop, calls = self._composed(monkeypatch, fault=fault)
        first = self._claimed(attempts=1, lease_token="lease-1")

        await loop._run_job(first)

        assert storage.events == self._attempt(self.FAULT_ENDS[fault])
        assert calls["finalized"] == [] and loop.processed == 0, "nothing succeeded"
        assert storage.committed == {}, "the link the email carried was not stored"
        ((run_at, restored),) = calls["rescheduled"]
        assert restored is False, "a retryable failure keeps its consumed attempt"
        eta = (run_at - datetime(2030, 1, 1, tzinfo=timezone.utc)).total_seconds()
        assert 48 <= eta <= 72, f"bulk rung 1 is 60 s ± 20 %, got {eta}"

        # The retry is a new claim: one more attempt consumed, and a new lease.
        second = self._claimed(attempts=2, lease_token="lease-2")
        await loop._run_job(second)

        assert storage.events == (
            self._attempt(self.FAULT_ENDS[fault]) + self._attempt("commit")
        )
        assert calls["finalized"] == ["succeeded"] and len(calls["rescheduled"]) == 1
        assert (loop.failures, loop.processed, loop.exhausted) == (1, 1, 0)
        assert calls["notices"] == []
        first_token, second_token = storage.drawn
        assert first_token != second_token, "the retry draws a fresh token"
        assert [(m["to"], m["token"]) for m in provider.sent] == [
            (ADMITTED, first_token),
            (ADMITTED, second_token),
        ], "both emails went: the duplicate is the cost of sending first"
        assert CLAIM_BASE + second_token in provider.sent[1]["body"]
        assert storage.stored_with == [
            (ADMITTED, first_token),
            (ADMITTED, second_token),
        ]
        assert storage.committed == {ADMITTED: sessions.token_hash(second_token)}
        assert provider.open_at_send == [(0, 0, False)] * 2, (
            "no transaction is open in the sending task at either send"
        )
        assert first["payload"] == second["payload"] == ADMISSION_PAYLOAD
        assert "_SyntheticFault" in caplog.text, "the read covers the fault's traceback"
        for secret in storage.secrets:
            assert secret not in caplog.text

    @pytest.mark.parametrize("fault", ["store", "storage_commit"])
    async def test_on_the_last_attempt_it_ends_failed_with_no_retry_and_no_notice(
        self, monkeypatch, caplog, fault
    ):
        caplog.set_level(logging.DEBUG)
        storage, provider, loop, calls = self._composed(monkeypatch, fault=fault)
        last = self._claimed(attempts=5, lease_token="lease-5")

        await loop._run_job(last)

        assert storage.events == self._attempt(self.FAULT_ENDS[fault])
        assert calls["finalized"] == ["failed"] and calls["rescheduled"] == []
        assert calls["notices"] == [], "a system job tells no workspace"
        assert (loop.failures, loop.processed, loop.exhausted) == (1, 0, 1)
        assert [(m["to"], m["token"]) for m in provider.sent] == [
            (ADMITTED, storage.drawn[0])
        ], "the email went, with a link that was never stored"
        assert storage.committed == {}
        assert provider.open_at_send == [(0, 0, False)]
        assert last["payload"] == ADMISSION_PAYLOAD
        assert "_SyntheticFault" in caplog.text, "the read covers the fault's traceback"
        for secret in storage.secrets:
            assert secret not in caplog.text


class TestAFailureToRecordAFailureDoesNotTakeTheLane:
    """The failure path opens its own checkout; a pool wait or a database
    fault there must leave the lease to lapse, not raise out of the lane
    task (which would take the K−1 other in-flight jobs down with it)."""

    def _loop(self, monkeypatch, *, session_error):
        from datetime import datetime, timezone

        from src.services.target.work_loop import WorkLoop, WorkerConfig

        class _Jobs:
            JobFenced = work_loop.jobs.JobFenced
            SELF_FINALIZED = work_loop.jobs.SELF_FINALIZED
            budget_exhausted = staticmethod(work_loop.jobs.budget_exhausted)
            backoff_seconds = staticmethod(work_loop.jobs.backoff_seconds)

            @staticmethod
            async def finalize_job(*a, **k):  # pragma: no cover
                raise AssertionError("no session, no finalize")

            @staticmethod
            async def reschedule_job(*a, **k):  # pragma: no cover
                raise AssertionError("no session, no reschedule")

        monkeypatch.setattr(work_loop, "jobs", _Jobs)
        monkeypatch.setattr(
            work_loop, "utcnow", lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
        )

        async def executor(session, job):
            raise RuntimeError("boom")

        loop = WorkLoop.__new__(WorkLoop)
        loop._registry = {"k": executor}
        loop._config = WorkerConfig()
        loop._past_posts_capability = None
        loop.processed = loop.parked = loop.failures = 0
        loop.fenced = loop.undeliverable = loop.consecutive_errors = loop.exhausted = 0
        loop.claim_waits = 0

        from contextlib import asynccontextmanager

        opened = {"n": 0}

        @asynccontextmanager
        async def _ctx(job):
            opened["n"] += 1
            if (
                opened["n"] >= 2
            ):  # the executor's session opens; the failure path's fails
                raise session_error
            yield _FakeSession()

        loop._session_for = _ctx
        return loop

    async def test_a_pool_wait_on_the_backoff_path_is_logged_not_raised(
        self, monkeypatch
    ):
        from sqlalchemy.exc import TimeoutError as PoolTimeout

        loop = self._loop(monkeypatch, session_error=PoolTimeout("pool full"))
        await loop._run_job(
            {
                "id": "j",
                "kind": "k",
                "lane": "bulk",
                "attempts": 1,
                "max_attempts": 5,
                "lease_token": "t",
                "payload": {},
            }
        )
        assert loop.failures == 1 and loop.consecutive_errors == 1

    async def test_a_fault_on_the_exhausted_path_is_logged_not_raised(
        self, monkeypatch
    ):
        loop = self._loop(monkeypatch, session_error=RuntimeError("db gone"))
        await loop._run_job(
            {
                "id": "j",
                "kind": "k",
                "lane": "bulk",
                "attempts": 5,
                "max_attempts": 5,
                "lease_token": "t",
                "payload": {},
            }
        )
        assert loop.failures == 1 and loop.exhausted == 0


class TestAnExecutorThatFinalizesItself:
    """`jobs.SELF_FINALIZED`: the publish pipeline and a paced
    sender settle their own job; a second finalize by the loop would be fenced
    and counted as an error on every such run."""

    async def test_the_loop_does_not_finalize_a_self_finalized_job(self, monkeypatch):
        from src.services.target.work_loop import WorkLoop, WorkerConfig

        finalized = []

        class _Jobs:
            JobFenced = work_loop.jobs.JobFenced
            SELF_FINALIZED = work_loop.jobs.SELF_FINALIZED
            budget_exhausted = staticmethod(work_loop.jobs.budget_exhausted)
            backoff_seconds = staticmethod(work_loop.jobs.backoff_seconds)

            @staticmethod
            async def finalize_job(session, job_id, token, terminal_state):
                finalized.append(terminal_state)

        async def executor(session, job):
            return work_loop.jobs.SELF_FINALIZED

        monkeypatch.setattr(work_loop, "jobs", _Jobs)
        loop = WorkLoop.__new__(WorkLoop)
        loop._registry = {"publish_pipeline": executor}
        loop._config = WorkerConfig()
        loop._past_posts_capability = None
        loop.processed = loop.parked = loop.failures = 0
        loop.fenced = loop.undeliverable = loop.consecutive_errors = loop.exhausted = 0

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx(job):
            yield _FakeSession()

        loop._session_for = _ctx
        await loop._run_job(
            {"id": "j", "kind": "publish_pipeline", "lease_token": "t", "payload": {}}
        )
        assert finalized == [] and loop.processed == 1 and loop.fenced == 0


class TestPerLaneWorkspaceCaps:
    def test_the_caps_are_05_s_numbers_per_lane(self):
        from src.services.target.work_loop import WorkerConfig

        cfg = WorkerConfig()
        assert cfg.ws_lane_cap_for("interactive") == 5
        assert cfg.ws_lane_cap_for("bulk") == 3
        assert cfg.ws_lane_cap_for("media") == 1
        assert cfg.sender_hold_seconds == 15.0


class TestTheConcurrencyCeiling:
    """K per lane is bounded at composition
    by the pool — every task DB-active at once plus the three reserved
    connections must fit, or the worker refuses to start."""

    def test_the_start_values_fit_and_the_line_names_them(self):
        from src.services.target.work_loop import WorkerConfig, assert_concurrency_fits

        line = assert_concurrency_fits(WorkerConfig(), pool_size=10)
        assert line == (
            "lanes: interactive×2 bulk×2 media×1 pool=10"
            " (tasks hold up to 7 + 3 reserved)"
        )

    def test_a_task_weighs_what_it_holds_at_its_peak(self):
        """A bulk kind's plain executor opens its own sessions UNDER the
        loop's job transaction (a sync walk, a refresh, the reconciler's
        poll) — the ceiling counts what a task holds, not the task. A media
        task's executor owns its transactions and opens one short unit of work
        at a time: one connection."""
        from src.services.target.work_loop import (
            TASK_CONNECTIONS,
            WorkerConfig,
            task_connections,
        )

        assert TASK_CONNECTIONS == {"interactive": 1, "bulk": 2, "media": 1}
        assert task_connections(WorkerConfig()) == 2 * 1 + 2 * 2 + 1 * 1

    def test_past_the_ceiling_it_refuses_by_the_numbers(self):
        from src.services.target.work_loop import WorkerConfig, assert_concurrency_fits

        cfg = WorkerConfig(lane_concurrency={"interactive": 4, "bulk": 2, "media": 1})
        with pytest.raises(ValueError) as info:
            assert_concurrency_fits(cfg, pool_size=10)
        assert "4×1 + 2×2 + 1×1 + 3 reserved exceeds the pool of 10" in str(info.value)

    def test_the_media_lane_counts_against_the_ceiling(self):
        """Interactive and bulk alone fit (2×1 + 2×2 + 3 = 9 of 10), so the media
        tasks are all that can tip this over, and the refusal names all three."""
        from src.services.target.work_loop import WorkerConfig, assert_concurrency_fits

        cfg = WorkerConfig(lane_concurrency={"interactive": 2, "bulk": 2, "media": 2})
        with pytest.raises(ValueError) as info:
            assert_concurrency_fits(cfg, pool_size=10)
        assert str(info.value) == (
            "lanes: interactive×2 bulk×2 media×2 pool=10"
            " (tasks hold up to 8 + 3 reserved): 2×1 + 2×2 + 2×1 + 3 reserved"
            " exceeds the pool of 10 — lower TARGET_WORKER_*_CONCURRENCY"
            " or raise the pool (measure the load first)"
        )

    def test_the_start_values_fill_the_pinned_pool_exactly(self):
        """Exactly at the ceiling is allowed, and there is no connection to
        spare: one fewer in the pool refuses the start values, so raising one
        lane's K without lowering another's is refused at boot."""
        from src.services.target.work_loop import (
            RESERVED_CONNECTIONS,
            WorkerConfig,
            assert_concurrency_fits,
            task_connections,
        )

        cfg = WorkerConfig()
        pool = unit_of_work.POOL_SIZE_SEAM
        assert task_connections(cfg) + RESERVED_CONNECTIONS == pool
        assert assert_concurrency_fits(cfg, pool_size=pool).startswith("lanes:")
        with pytest.raises(ValueError):
            assert_concurrency_fits(cfg, pool_size=pool - 1)

    def test_an_unknown_lane_runs_one_task(self):
        from src.services.target.work_loop import WorkerConfig

        assert WorkerConfig().concurrency_for("weird") == 1


class TestClaimsOnPooledCheckouts:
    """A loop with `connect` checks a connection out per claim and
    returns it at once; a bound claim connection (the test seam) still wins."""

    def _loop(self, monkeypatch, **kw):
        from src.services.target.work_loop import WorkLoop, WorkerConfig

        claims = []

        async def claim_job(conn, **claim):
            claims.append((conn, claim))
            return None  # nothing runnable — the claim itself is the point

        monkeypatch.setattr(work_loop.jobs, "claim_job", claim_job)
        loop = WorkLoop(
            session_for=lambda job: None,
            lane="bulk",
            registry={},
            heartbeat=None,
            config=WorkerConfig(),
            worker_name="w",
            **kw,
        )
        return loop, claims

    async def test_each_claim_checks_out_and_returns_a_connection(self, monkeypatch):
        from contextlib import asynccontextmanager

        events = []

        @asynccontextmanager
        async def connect():
            events.append("out")
            yield "conn-1"
            events.append("back")

        loop, claims = self._loop(monkeypatch, connect=connect)
        assert await loop.run_once() is False
        assert await loop.run_once() is False
        assert events == ["out", "back", "out", "back"]
        assert [c for c, _ in claims] == ["conn-1", "conn-1"]
        assert claims[0][1]["lane"] == "bulk" and claims[0][1]["ws_lane_cap"] == 3

    async def test_a_bound_connection_is_used_instead(self, monkeypatch):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def connect():  # pragma: no cover — must not be used
            raise AssertionError("bound connection wins")
            yield

        loop, claims = self._loop(monkeypatch, connect=connect)
        loop.bind_claim_conn("pinned")
        await loop.run_once()
        assert [c for c, _ in claims] == ["pinned"]

    async def test_a_full_pool_is_a_counted_wait_not_an_error(self, monkeypatch):
        from contextlib import asynccontextmanager

        from sqlalchemy.exc import TimeoutError as PoolTimeout

        @asynccontextmanager
        async def connect():
            raise PoolTimeout("QueuePool limit reached")
            yield  # pragma: no cover

        loop, _ = self._loop(monkeypatch, connect=connect)
        assert await loop.run_once() is False
        assert loop.claim_waits == 1 and loop.consecutive_errors == 0, (
            "pressure must not walk the lane toward its error ceiling"
        )

    async def test_without_either_the_loop_says_so(self, monkeypatch):
        loop, _ = self._loop(monkeypatch)
        with pytest.raises(RuntimeError):
            await loop.run_once()


class TestAnExecutorThatOwnsItsTransactions:
    """An executor marked `own_transactions` runs with NO job
    session open and is finalized in a short transaction afterwards; a
    plain executor keeps the one-transaction shape."""

    def _loop(self, monkeypatch, executor):
        from src.services.target.work_loop import WorkLoop, WorkerConfig

        calls = {"finalized": [], "opened": 0, "open_during_run": []}

        class _Jobs:
            JobFenced = work_loop.jobs.JobFenced
            SELF_FINALIZED = work_loop.jobs.SELF_FINALIZED
            budget_exhausted = staticmethod(work_loop.jobs.budget_exhausted)
            backoff_seconds = staticmethod(work_loop.jobs.backoff_seconds)

            @staticmethod
            async def finalize_job(session, job_id, token, terminal_state):
                calls["finalized"].append((session, terminal_state))

        monkeypatch.setattr(work_loop, "jobs", _Jobs)
        loop = WorkLoop.__new__(WorkLoop)
        loop._registry = {"k": executor}
        loop._config = WorkerConfig()
        loop._past_posts_capability = None
        loop.processed = loop.parked = loop.failures = 0
        loop.fenced = loop.undeliverable = loop.consecutive_errors = loop.exhausted = 0

        from contextlib import asynccontextmanager

        state = {"open": 0}

        @asynccontextmanager
        async def _ctx(job):
            calls["opened"] += 1
            state["open"] += 1
            try:
                yield _FakeSession()
            finally:
                state["open"] -= 1

        loop._session_for = _ctx
        calls["state"] = state
        return loop, calls

    async def test_a_marked_executor_sees_no_session_and_is_finalized_after(
        self, monkeypatch
    ):
        from src.services.target.work_loop import own_transactions

        seen = {}

        @own_transactions
        async def executor(session, job):
            seen["session"] = session
            seen["open_during"] = calls["state"]["open"]
            return None

        loop, calls = self._loop(monkeypatch, executor)
        await loop._run_job({"id": "j", "kind": "k", "lease_token": "t", "payload": {}})
        assert seen["session"] is None
        assert seen["open_during"] == 0, "no job transaction is open while it waits"
        assert calls["opened"] == 1 and calls["finalized"][0][1] == "succeeded"
        assert loop.processed == 1

    async def test_a_marked_executor_that_finalizes_itself_opens_nothing(
        self, monkeypatch
    ):
        from src.services.target.work_loop import own_transactions

        @own_transactions
        async def executor(session, job):
            return work_loop.jobs.SELF_FINALIZED

        loop, calls = self._loop(monkeypatch, executor)
        await loop._run_job({"id": "j", "kind": "k", "lease_token": "t", "payload": {}})
        assert calls["opened"] == 0 and calls["finalized"] == []

    async def test_an_undeliverable_verdict_still_parks_review_required(
        self, monkeypatch
    ):
        from src.services.target import outbox
        from src.services.target.work_loop import own_transactions

        @own_transactions
        async def executor(session, job):
            return outbox.UNDELIVERABLE

        loop, calls = self._loop(monkeypatch, executor)
        await loop._run_job({"id": "j", "kind": "k", "lease_token": "t", "payload": {}})
        assert calls["finalized"][0][1] == "review_required" and loop.undeliverable == 1

    async def test_a_plain_executor_keeps_the_one_transaction_shape(self, monkeypatch):
        seen = {}

        async def executor(session, job):
            seen["session"] = session
            seen["open_during"] = calls["state"]["open"]

        loop, calls = self._loop(monkeypatch, executor)
        await loop._run_job({"id": "j", "kind": "k", "lease_token": "t", "payload": {}})
        assert seen["session"] is not None and seen["open_during"] == 1
        assert calls["opened"] == 1 and calls["finalized"][0][0] is seen["session"]

    def test_the_three_waiting_kinds_are_marked(self):
        from src.services.target.work_loop import WorkerDeps, build_registry

        registry = build_registry(
            WorkerDeps(
                engine=None,
                transport=object(),
                email=object(),
                meta=object(),
                transit=object(),
                media_fetch=lambda job: None,
            )
        )
        for kind in ("deliver_outbox", "send_email", "publish_pipeline"):
            entry = registry[kind]
            assert not hasattr(entry, "reason"), f"{kind} must be live here"
            assert getattr(entry, "owns_transactions", False), kind


class TestADeadPublishJobParksItsStory:
    """A `publish_pipeline` job whose budget is spent parks its story
    for the workspace's review through the pipeline's own door — never the
    generic exhausted notice alone, which would leave the card reading
    Approved forever."""

    async def test_the_exhausted_notice_routes_a_publish_job_to_the_park(
        self, monkeypatch
    ):
        from src.services.target import publish_pipeline

        told, fanned = [], []

        async def tell_review(session, **parked):
            told.append(parked)

        async def fanout_notification(*a, **k):  # pragma: no cover — must not run
            fanned.append(k)

        monkeypatch.setattr(publish_pipeline, "tell_review", tell_review)
        monkeypatch.setattr(
            work_loop.outbox, "fanout_notification", fanout_notification
        )
        parked = {"workspace_id": "ws", "intent_id": "it", "tz": "UTC", "notice": "n"}
        job = {"id": "j1", "kind": "publish_pipeline", "workspace_id": "ws"}
        await work_loop._notify_exhausted(_FakeSession(), job, parked)
        await work_loop._notify_exhausted(_FakeSession(), job, None)
        assert told == [parked] and fanned == [], (
            "a parked story gets its courtesy; nothing parked, no generic notice either"
        )


class TestTheSenderMintReadsItsOwners:
    """The sweep's INSERT reads the interactive lane's budget, the
    `tg:` key prefix and the push-binding predicate from the module that
    declares each, so retuning the lane or adding a second push channel cannot
    miss this statement."""

    async def test_the_mint_binds_the_interactive_lane_budget(self):
        from src.services.target import jobs

        class _Result:
            def scalar(self):

                return 0  # the door returns the minted count as a scalar (worker sweep functions)

        class _Session:
            def __init__(self):
                self.calls = []

            async def execute(self, statement, params=None):
                self.calls.append((str(statement), params))
                return _Result()

        session = _Session()
        assert await work_loop.ensure_sender_jobs(session) == 0
        sql, params = session.calls[0]
        attempts, deadline = jobs.LANE_BUDGETS["interactive"]
        assert params["attempts"] == attempts
        assert params["deadline"] == deadline
        assert params["prefix"] == jobs.SENDER_KEY_PREFIX
        assert params["lim"] == WorkerConfig.sender_mint_limit
        # The statement carries none of these literals.
        assert "'tg:'" not in sql and "LIMIT 200" not in sql
        assert "interval '10 minutes'" not in sql

    async def test_the_caller_may_bound_the_sweep(self):
        class _Result:
            def scalar(self):
                return 0  # the door returns the minted count as a scalar (worker sweep functions)

        class _Session:
            def __init__(self):
                self.calls = []

            async def execute(self, statement, params=None):
                self.calls.append((str(statement), params))
                return _Result()

        session = _Session()
        await work_loop.ensure_sender_jobs(session, limit=7)
        assert session.calls[0][1]["lim"] == 7

    def test_the_four_push_statements_read_the_one_predicate(self):
        """ "Where can we say this" has one owner, `bindings`: the sender sweep's
        door and the outcome doors' supersede read the live set, and every
        statement that queues or claims a card reads the deliverable one."""
        import inspect

        from src.services.target import bindings, outbox, prompts

        # The sender sweep's statement is `fn_sender_sweep`'s body — a door
        # cannot call the Python fragment, so the predicate is spelled in the
        # door and pinned here to the one owner: the door's body in the schema
        # code must carry `push_binding_where('b')` verbatim, and the Python does
        # not spell it.
        body = function_body("fn_sender_sweep")
        assert body.count(bindings.push_binding_where("b")) == 1
        assert "push_binding_where(" not in inspect.getsource(
            work_loop.ensure_sender_jobs
        )

        def uses(fn, fragment):
            return inspect.getsource(fn).count(fragment)

        assert uses(prompts.push_bindings, "bindings.DELIVERABLE_BINDING_WHERE") == 1
        assert uses(outbox.claim_next, "bindings.deliverable_binding_where('b')") == 1
        assert (
            uses(outbox.supersede_all, "bindings.deliverable_binding_where('cb')") == 1
        )
        for door in (
            outbox.supersede_everywhere_touched,
            outbox.restate_everywhere_touched,
        ):
            # Supersede in every live binding; queue the edit only where a
            # card may go.
            assert uses(door, "bindings.push_binding_where()") == 1, door.__name__
            assert uses(door, "bindings.DELIVERABLE_BINDING_WHERE") == 1, door.__name__
            assert uses(door, "b.deliverable") == 1, door.__name__
        # And the fragments are what they say.
        assert bindings.push_binding_where("b") == (
            "b.state = 'active' AND b.channel LIKE 'telegram%'"
        )
        assert bindings.deliverable_binding_where("b") == (
            "b.state = 'active' AND b.channel LIKE 'telegram%'"
            " AND (b.channel <> 'telegram_dm' OR EXISTS ("
            "SELECT 1 FROM user_identities dm_ui"
            " JOIN workspace_members dm_wm ON dm_wm.user_id = dm_ui.user_id"
            " WHERE dm_ui.provider = 'telegram'"
            " AND dm_ui.external_id = b.external_ref"
            " AND dm_wm.workspace_id = b.workspace_id))"
        )


@pytest.fixture(autouse=True)
def planner_context_seam(monkeypatch):
    """The fake planner SQL tests retain their legacy pool, not a fake runtime authority."""
    from src.services.target import source_policy, versioned_mix, source_state
    from unittest.mock import AsyncMock, Mock

    monkeypatch.setattr(source_policy, "require_context", Mock())
    monkeypatch.setattr(
        source_policy, "suitability_context", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        versioned_mix, "state", AsyncMock(return_value={"converted": False})
    )
    monkeypatch.setattr(source_state, "lock_all_workspace_sources", AsyncMock())


def test_reaper_owns_cleanup_transactions():
    executor = build_registry(full_deps())["reap_expired"]
    assert getattr(executor, "owns_transactions", False), (
        "reaper cleanup must release job/door locks before acquiring source prefixes"
    )
