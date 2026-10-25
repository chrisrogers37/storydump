"""The worker's kind→executor registry and per-lane claim loop.

This is the tier half of the composition root: `src.worker` (the entrypoint)
builds engines, connections, `Clock`, `LeaseHeartbeat` and the seams, then
runs one :class:`WorkLoop` per lane over the registry built here. Nothing in
this module knows a channel — the transport arrives as an injected callable
(`outbox.deliver`'s contract: takes the row, returns the external ref).

The deployed worker uses `svc_worker`. System selections use bounded
SECURITY DEFINER functions; tenant writes run with the selected workspace.
Publish jobs are minted by approval, review resolution, automatic approval
and give-up handling through `jobs.mint_publish_job`.

**Parking is the registry's honest state, not an error path.** A claimed job
whose kind cannot run here is RESCHEDULED with its attempt restored: alive,
visible, counted, never
stranded on a lease and never finalized dead. A seam the deployment lacks
(no transport, no transit store, no media_fetch) parks its dependent kind the
same way, with the missing seam named in the reason.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeout

from src.services.target import (
    activation_nudge,
    credential_lifecycle,
    email_sender,
    media_sync,
    media_adapters,
)

from src.services.target import (
    past_posts_activation,
    past_posts_deletion,
    past_posts_reconciliation,
    bindings,
    content_runway,
    jobs,
    offboarding,
    outbox,
    prompts,
    publish_pipeline,
    reconciler,
    scheduler,
    unit_of_work,
)
from src.services.target.instagram_sources import is_activated
from src.utils.datetime_utils import utcnow

logger = logging.getLogger("target.work_loop")


@dataclass(frozen=True)
class Parked:
    """A kind this deployment cannot run, and why. Loud by construction."""

    reason: str


@dataclass(frozen=True)
class WorkerConfig:
    """The worker's numbers as parameters, env-overridable at the entrypoint.

    Some defaults are marked provisional; the composition root owns
    overriding them.
    """

    lease_seconds: float = 90.0  # 60–120 band
    # K concurrent claim-and-run tasks per
    # lane in ONE process, on pooled checkouts — K bounds tasks, not
    # connections. Start values under the ceiling `assert_concurrency_fits`
    # enforces; raising them past it needs the measurement the gate records.
    lane_concurrency: Mapping[str, int] = field(
        default_factory=lambda: {"interactive": 3, "bulk": 2}
    )
    # Per-workspace, per-lane: interactive 5, bulk 3 (one
    # publish + one sync + one misc). They bind
    # only across several replicas: `fn_claim_job` counts the deployment's
    # leases, and under the pinned pool (`POOL_SIZE_SEAM`) one process never
    # runs more tasks per lane than these — `assert_concurrency_fits` checks
    # the pool, not the caps — so on a single worker one workspace can hold
    # a whole lane.
    ws_lane_cap_interactive: int = 5
    ws_lane_cap_bulk: int = 3
    claim_idle_seconds: float = 1.0  # sleep when a lane has nothing runnable
    park_seconds: float = 900.0  # executor-less kinds retry this often
    # < lease_seconds: the poller's hold per claim, short so a
    # busy binding yields its lane sooner; the sweep re-mints while rows remain.
    sender_hold_seconds: float = 15.0
    sender_sweep_seconds: float = 3.0  # cadence of the sender-job mint sweep
    #: A sweep is bounded; the next one takes the rest.
    sender_mint_limit: int = 200
    prompt_sweep_seconds: float = 5.0  # cadence of the prompt sweep
    prompt_sweep_limit: int = 50  # sweep batch (`prompts.sweep_due_prompts`)
    # The late window: a planned story is still served up to
    # an hour after its time — the worker down, or a pause that ended inside
    # the hour; past it, it is missed and its bound chats are told. Passed to
    # both doors (`fn_prompts_due`, `fn_planned_misses`), so a new value needs
    # no migration.
    planned_late_seconds: int = 3600
    status_interval_seconds: float = 60.0  # cadence of the status line
    # The event-loop watchdog (`loop_watchdog`): the loop stamps a beat every
    # `loop_beat_seconds`, and one silent for `loop_stall_seconds` ends the
    # process so the platform restarts it. Five minutes, because a false exit
    # spends a restart and the loop can be held for minutes by steps that DO
    # return: the egress floor resolves names on the loop's thread
    # (`egress._resolve_addresses`), and several provider calls can queue
    # behind one slow resolver. `tests/src/test_worker.py` pins that the
    # restart still lands inside the monitor's worker-down threshold.
    loop_stall_seconds: float = 300.0
    loop_beat_seconds: float = 5.0
    lane_max_consecutive_errors: int = 10  # claim errors before the lane dies loudly
    poller_interval_seconds: float = 2.0  # outbox cadence
    chat_limit: int = 18  # per-chat sends per window
    chat_window_seconds: int = 60
    global_limit: int = 25  # global sends per window
    global_window_seconds: int = 1
    reap_limit: int = 500  # the reap's total per sweep; the card sweep reuses it
    # Retention: `rate_counters` keeps 7 d (every window is 24 h or less, so
    # a row that old only holds the key it counted); 5,000 per batch.
    rate_counters_keep_seconds: int = 7 * 24 * 3600
    retention_batch: int = 5000
    #: Batches repeat within one run until one comes back short or this is spent.
    retention_budget_seconds: float = 5.0
    # 1,440 min (24 h) when the workspace's approval_ttl_minutes is NULL.
    approval_ttl_seconds: int = 24 * 3600
    approved_ttl_seconds: int = 72 * 3600
    reconcile_limit: int = 50
    reconcile_notify_after_seconds: int = 6 * 3600
    transit_reap_older_than_seconds: int = 48 * 3600
    stranded_alert_after_seconds: int = 24 * 3600  # re-alert cadence
    stranded_alert_limit: int = 200  # rows re-alerted per beat
    # The "no media available" notice for a missed slot: once per account per 24 h.
    no_media_notice_after_seconds: int = 24 * 3600
    # The runway notice's level, in days of eligible content: told once
    # below it, re-armed a margin above it (`content_runway.REARM_MARGIN_DAYS`).
    low_runway_days: int = content_runway.LOW_RUNWAY_DAYS
    # The front end's origin (`settings.web_app_origin`), for the deep link in
    # the parked-intent notice. None = the notice still fires,
    # without a link: being told late beats not being told.
    web_app_origin: Optional[str] = None
    # The activation nudge, built off: `build_registry` parks its sweep
    # unless this is set AND an email provider and `web_app_origin` exist, and
    # the entrypoint sets it only from TARGET_ACTIVATION_NUDGE_ENABLED.
    activation_nudge_enabled: bool = False
    # Emails per daily run, TARGET_ACTIVATION_NUDGE_LIMIT when set. Small by
    # default because the latch is set as each email is queued, not as it is
    # sent: a provider mistake on an armed day uses up that day's nudges, so
    # the first armed run should risk few. Raise it once one has been
    # delivered. `email_sender`'s 90/day is shared with invitations, and a
    # person waiting on an invite comes first.
    activation_nudge_limit: int = 3
    # Only people who signed up this recently, so switching the nudge on does
    # not mail everyone who ever stopped part-way.
    activation_nudge_since_days: int = 30
    # Idle this long before a nudge: `fn_activation_funnel`'s stall window for the funnel.
    activation_nudge_stall_seconds: int = 72 * 3600

    def concurrency_for(self, lane: str) -> int:
        return max(1, int(self.lane_concurrency.get(lane, 1)))

    def ws_lane_cap_for(self, lane: str) -> int:
        return (
            self.ws_lane_cap_interactive
            if lane == "interactive"
            else self.ws_lane_cap_bulk
        )

    clock_interval_seconds: float = 15.0
    clock_max_inserts: int = 500
    refresh_cadence_seconds: int = 7 * 24 * 3600
    heartbeat_interval_seconds: float = 20.0
    offboard_grace_seconds: int = offboarding.GRACE_SECONDS_DEFAULT
    offboard_drain_timeout_seconds: int = 15 * 60  # publish-drain 15 min
    offboard_drain_recheck_seconds: int = 60  # provisional
    offboard_drain_limit: int = 500  # provisional


#: Connections the worker holds besides its claim-and-run tasks, counted as
#: the STEADY holders: the clock's pinned election connection, plus two
#: periodic holders that may coincide (a clock tick's session and a
#: heartbeat beat). The other periodic readers — the status reporter (60 s),
#: the sender sweeper (3 s), the prompt sweeper (5 s) — are
#: transient waiters: they hold a connection for milliseconds, and when the
#: pool is momentarily full they wait `pool_timeout`, which is not a fault
#: (`run_once` counts a claim's pool wait apart from errors). So the ceiling
#: bounds what is held at once in steady state, not every possible overlap.
RESERVED_CONNECTIONS = 3
#: What one task holds at its peak, per lane. An interactive task's kinds run
#: their own transactions (the sender) or one short job transaction: one
#: connection. A bulk task's plain kinds — a sync walk, a credential refresh —
#: open sessions of their own UNDER the loop's job transaction, so a bulk task
#: holds two at its peak. The ceiling weighs them so, rather than pretending.
TASK_CONNECTIONS = {"interactive": 1, "bulk": 2}


def task_connections(config: WorkerConfig) -> int:
    return sum(
        config.concurrency_for(lane) * TASK_CONNECTIONS[lane]
        for lane in ("interactive", "bulk")
    )


def assert_concurrency_fits(config: WorkerConfig, *, pool_size: int) -> str:
    """The ceiling: every task DB-active at once (weighed by what a
    task holds at its peak, `TASK_CONNECTIONS`) plus the reserved steady
    holders must fit the pool — `K_interactive × 1 + K_bulk × 2 + 3 ≤ pool`.
    Returns the startup line; raises `ValueError` naming the numbers when
    the configuration would oversubscribe the pool (a worker that starts
    and then times out on every checkout is the failure this refuses at
    boot). A momentary overlap of the transient readers past the pool is a
    bounded wait, counted, never a failure."""
    k_i = config.concurrency_for("interactive")
    k_b = config.concurrency_for("bulk")
    held = task_connections(config)
    line = (
        f"lanes: interactive×{k_i} bulk×{k_b} pool={pool_size}"
        f" (tasks hold up to {held} + {RESERVED_CONNECTIONS} reserved)"
    )
    if held + RESERVED_CONNECTIONS > pool_size:
        raise ValueError(
            f"{line}: {k_i}×{TASK_CONNECTIONS['interactive']} +"
            f" {k_b}×{TASK_CONNECTIONS['bulk']} + {RESERVED_CONNECTIONS} reserved"
            f" exceeds the pool of {pool_size} — lower TARGET_WORKER_*_CONCURRENCY"
            " or raise the pool (measure the load first)"
        )
    return line


def own_transactions(fn):
    """Mark an executor that opens its OWN transactions against the engine —
    the publish pipeline's checkpoints, the email sender's provider call,
    the outbox sender's hold — so the loop runs it with NO job session open
    and finalizes in a transaction of its own afterwards (a task
    waiting on a provider holds no pooled connection while it waits)."""
    fn.owns_transactions = True
    return fn


@dataclass
class WorkerDeps:
    """The injected seams. `None` parks the dependent kind rather than faking it."""

    meta: Any = None
    transit: Any = None
    #: One provider registry for listing, publishing and approval-card media.
    adapters: media_adapters.MediaAdapters = field(default_factory=dict)
    media_fetch: Optional[Callable[[dict], Any]] = None
    #: Explicit captured past-posts turns, composed by `past_posts_composition` and only
    #: when `past_posts_activation` arms the worker.
    past_posts_sync: Any = None
    #: The past-posts coordinator (`instagram_publish.PastPostsPipeline`), handed to
    #: the publish pipeline; None unless `past_posts_activation` armed this worker,
    #: and then a past-posts intent is refused `past_posts_pipeline_not_configured`.
    past_posts: Any = None
    #: The tombstone key ring (`past_posts_activation.load_keys`): it arms the
    #: past-posts source, and a deletion receipt is redacted with it either way.
    past_posts_keys: Any = None
    #: The armed past-posts source's release run (`past_posts_activation.release_evidence`),
    #: which keys its reconciliation jobs, or None.
    past_posts_evidence: Any = None
    #: The past-posts adapter's HTTP client, closed at shutdown, or None.
    past_posts_client: Any = None
    mix_conversion_evidence: Any = None
    #: The EmailSender port, or None when no provider is wired.
    email: Optional[Any] = None
    transport: Optional[Callable[[dict], Any]] = None
    poll: Optional[Callable[..., Any]] = None
    refresh: Optional[Callable[..., Any]] = None
    #: The advisory Meta usage pre-check, or None — off by default;
    #: `TARGET_USAGE_PRECHECK_ENABLED` composes it.
    precheck: Any = None
    engine: Any = None
    config: WorkerConfig = field(default_factory=WorkerConfig)
    #: Selected-only preparation, outside SQL; strict fresh DB evidence remains authoritative.
    selected_prepare: Any = None
    past_posts_capability: Any = None


_UNBUILT_REASON = (
    "no executor exists in the target tier; "
    "job stays alive and re-checks on the park cadence"
)

#: Kinds the tier has never carried an executor for. The registry parks them
#: unconditionally; the schema-derived completeness test keeps this honest.
#: (The three past-posts lifecycle kinds all have executors; the
#: existing-account reconciliation parks only while the past-posts source is not armed.)
UNBUILT_KINDS = ("reencrypt_credentials",)


def build_registry(deps: WorkerDeps) -> dict:
    """kind → adapter | Parked, for every kind in the registry."""

    cfg = deps.config
    sessions = unit_of_work.make_session_for(deps.engine)

    def short(session, job):
        # The loop hands a marked executor no session; a caller
        # that DOES pass one (the unit seam) keeps it for every write.
        return sessions(job) if session is None else nullcontext(session)

    async def plan_slot(session, job):
        session.info["past_posts_capability"] = deps.past_posts_capability
        payload = job.get("payload") or {}
        # The slot rides jsonb as a string Postgres rendered, so POSTGRES
        # parses it back: PG strips trailing fractional zeros when
        # rendering timestamptz into jsonb, and CPython's fromisoformat is
        # version-sensitive about fraction widths (3.10, the repo floor,
        # rejects most of them — a stranding clock, not an outage, because
        # the backoff swallows the ValueError). Producer parses its own
        # rendering; no interpreter rule is encoded anywhere.
        row = (
            (
                await session.execute(
                    text(
                        "SELECT a.provider_account_ref, a.state,"
                        # The inner CAST AS text pins $1's inferred type: a
                        # bare CAST(:slot AS timestamptz) makes asyncpg refuse
                        # the string.
                        "       CAST(CAST(:slot AS text) AS timestamptz) AS slot_at"
                        " FROM ig_accounts a"
                        " WHERE a.id = :acct"
                    ),
                    {
                        "acct": str(payload["ig_account_id"]),
                        "slot": str(payload["slot_at"]),
                    },
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeError(
                f"plan_slot {job['id']}: account {payload.get('ig_account_id')}"
                " has no ig_accounts row"
            )
        if row["state"] != "active":
            # Minted while the account was active, run after it was removed
            # (or parked for reauth): the clock reads `active` only, and so
            # does this — a stale mint is a no-op, not a post.
            return "account_inactive"
        slot_at = row["slot_at"]
        outcome = await scheduler.execute_plan_slot(
            session,
            workspace_id=str(job["workspace_id"]),
            ig_account_id=str(payload["ig_account_id"]),
            slot_at=slot_at,
            provider_account_ref=row["provider_account_ref"],
            no_media_notice_after_seconds=cfg.no_media_notice_after_seconds,
            low_runway_days=cfg.low_runway_days,
        )
        if outcome.notice is not None:
            # The library was empty AND there was no surface to say so on.
            # Passed through rather than re-derived: `notice` already IS the
            # verdict, so re-testing it here would be a second place to keep
            # in step with the sentinel.
            return outcome.notice
        if outcome.intent_id is not None:
            # The fast path of the prompt edge: mint and prompt on
            # the same beat, same transaction. The prompt sweep is the
            # correctness backstop for anything this misses (a crash between
            # mint and prompt).
            await prompts.sweep_due_prompts(
                session,
                workspace_id=str(job["workspace_id"]),
                limit=1,
                late_seconds=cfg.planned_late_seconds,
            )

    @own_transactions
    async def reap_expired(session, job):
        if session is not None:
            raise RuntimeError("reap_expired requires closed outer transactions")
        await scheduler.execute_reap_expired(
            deps.engine,
            limit=cfg.reap_limit,
            approval_ttl_seconds=cfg.approval_ttl_seconds,
            approved_ttl_seconds=cfg.approved_ttl_seconds,
            capability=deps.past_posts_capability,
        )

    @own_transactions
    async def reconcile_ambiguous(session, job):
        """The sweep. TWO reasons, and only one of them needs a poll.

        `fn_reconciler_sweep` tags every row `ladder_due` or `notify_window`.
        A ladder row is an ambiguous publish that must be resolved against the
        provider; a notify row is a parked `review_required` intent whose
        customer-notification window has passed, which is purely
        informational. Feeding the second into `reconcile_intent` — ignoring
        the tag — polls a provider about an intent whose ladder is already
        spent, and produces no notification at all. So a notify row, by the tag
        the `04_functions.sql` door sets, goes to `reconciler.notify_parked_customer`.

        **The poll seam parks the LADDER half, not the kind.** The module
        docstring's rule is that a missing seam parks its *dependent* kind; the
        notify half depends on nothing the deployment lacks, so parking it with
        the ladder over-parks: a deployment with no poll seam would lose the
        customer notification too.

        **A failed row fails alone**. The sweep reads in one short
        transaction and each row runs in a transaction of its own, so a row
        that raises — including a verdict whose flip matched no row —
        rolls back alone, and the rows behind it still land rather than wait
        behind it (an unresolved ambiguity holds its account's next publish,
        `uq_publish_exclusive`). Not a savepoint per row in one transaction: a
        batch larger than `offboarding.MAX_WRITING_SAVEPOINTS` overflows the
        backend's subtransaction cache. After the beat the first
        failure is re-raised, so the job still fails and retries. A notice
        nobody can hear is recorded only by a beat that returns
        (`reconciler.record_no_surface` says why), so a failing beat leaves
        none behind. A caller that passes its own session (the unit seam) gets
        a savepoint per row on it instead.

        **The provider is asked with no transaction open**. A ladder
        row asks the provider (`reconciler.observe`) before its transaction
        opens, then claims its workspace, reads how far its ladder has climbed
        and records the answer in it: the egress floor refuses a provider call
        made inside a transaction, so a poll inside one could never
        resolve an ambiguity. A verdict that changes the intent's state is a
        compare-and-set on the state it leaves, so an intent that moved while
        the provider was asked fails its row, loudly.
        """
        async with short(session, job) as reader:
            due = await reconciler.sweep_due(
                reader,
                limit=cfg.reconcile_limit,
                notify_after_seconds=cfg.reconcile_notify_after_seconds,
            )

        @asynccontextmanager
        async def row_transaction():
            # A transaction of the row's own; on the unit seam, a savepoint.
            async with (
                short(session, job) as row_session,
                nullcontext() if session is None else session.begin_nested(),
            ):
                yield row_session

        async def reconcile_row(op):
            if op["reason"] == "notify_window":
                async with row_transaction() as session:
                    return await reconciler.notify_parked_customer(
                        session,
                        intent_id=op["intent_id"],
                        workspace_id=op["workspace_id"],
                        web_app_origin=cfg.web_app_origin,
                    )
            # Asked before the row's transaction opens (the docstring).
            status_code = await reconciler.observe(
                deps.poll, intent_id=op["intent_id"], workspace_id=op["workspace_id"]
            )
            async with row_transaction() as session:
                # Scope the ladder row too. `04_functions.sql` says every write from this sweep
                # "runs tenant-scoped as svc_worker": the session carries
                # `app.tenant_id = ''` because this is a system singleton, so an
                # unscoped write here is invisible to `p_tenant`. On the unit
                # seam the rows share one session, and asserting the tenant per
                # row is what keeps a ladder row from inheriting the scope of
                # whichever row preceded it. The ladder's count is read AFTER
                # the claim for the same reason: `post_intents` is
                # policy-covered, and a read with no tenant answers 0 for every
                # row — a ladder that never exhausts.
                await unit_of_work.apply_gucs(
                    session,
                    tenant_id=str(op["workspace_id"]),
                    actor_kind="system",
                )
                await reconciler.reconcile_intent(
                    session,
                    intent_id=op["intent_id"],
                    workspace_id=op["workspace_id"],
                    status_code=status_code,
                    checks=await reconciler.checks_so_far(
                        session, intent_id=op["intent_id"]
                    ),
                )

        ladder_skipped = 0
        # The notify rows whose workspace has nowhere to receive the notice.
        unreachable = []
        failures = []
        for op in due:
            if op["reason"] == "ladder_due" and deps.poll is None:
                ladder_skipped += 1
                continue
            try:
                if await reconcile_row(op) == outbox.UNDELIVERABLE:
                    unreachable.append(op)
            except Exception as exc:  # noqa: BLE001 — re-raised after the beat
                logger.exception(
                    "reconcile_ambiguous: the %s row of intent %s failed; its"
                    " writes rolled back and NO verdict was recorded for it —"
                    " the beat goes on",
                    op["reason"],
                    op["intent_id"],
                )
                failures.append(exc)
        if ladder_skipped:
            # Loud, per the module docstring: the deployment cannot do this
            # half and says so every beat it has work for it.
            logger.warning(
                "reconcile_ambiguous: %d ladder-due intent(s) left unpolled —"
                " no provider poll seam configured (stub Meta adapter supplies"
                " it); the notify half ran",
                ladder_skipped,
            )
        if failures:
            # Loud: the job fails and retries. The rows that resolved
            # have committed, so only the failed rows come round again.
            logger.error(
                "reconcile_ambiguous: %d of %d row(s) failed this beat; the"
                " rest committed",
                len(failures),
                len(due),
            )
            raise failures[0]
        if unreachable:
            async with short(session, job) as writer:
                recorded = [
                    await reconciler.record_no_surface(
                        writer,
                        intent_id=op["intent_id"],
                        workspace_id=op["workspace_id"],
                        retry_after_seconds=cfg.reconcile_notify_after_seconds,
                    )
                    for op in unreachable
                ]
            if outbox.UNDELIVERABLE in recorded:
                # One workspace with nowhere to receive its notice makes the
                # WHOLE sweep not-a-delivery. The rows that did land have
                # committed; what must not happen is the run reading as clean
                # when somebody was owed a message and got none.
                return outbox.UNDELIVERABLE

    async def alert_stranded_sources(session, job):
        # Alert-only: nothing here re-arms a source or enqueues a sync. The
        # re-arm belongs to the connect flow.
        await media_sync.alert_stranded_sources(
            session,
            stale_after_seconds=cfg.stranded_alert_after_seconds,
            limit=cfg.stranded_alert_limit,
        )

    # `own_transactions`: no provider, but each batch commits on its own, so
    # the run must not sit inside one job session. The payload is not read.
    @own_transactions
    async def retention_sweep(session, job):
        await scheduler.execute_retention_sweep(
            lambda: sessions(job),
            keep_seconds=cfg.rate_counters_keep_seconds,
            batch=cfg.retention_batch,
            budget_seconds=cfg.retention_budget_seconds,
        )

    async def reap_transit(session, job):
        await scheduler.execute_reap_transit_assets(
            session,
            lister=deps.transit.list_stale,
            deleter=deps.transit.destroy_asset,
            older_than_seconds=cfg.transit_reap_older_than_seconds,
        )

    @own_transactions
    async def run_pipeline(session, job):
        # The pipeline owns its own transactions against the engine; the loop
        # opens no job session for it and it finalizes itself.
        outcome = await publish_pipeline.run_publish_pipeline(
            dict(job),
            engine=deps.engine,
            meta=deps.meta,
            transit=deps.transit,
            media_fetch=deps.media_fetch,
            precheck=deps.precheck,
            past_posts=deps.past_posts,
        )
        logger.info("publish_pipeline %s -> %s", job["id"], outcome)
        # The pipeline finalizes or reschedules its own job in its own
        # transactions; the loop must not finalize again.
        return jobs.SELF_FINALIZED

    @own_transactions
    async def deliver_outbox(session, job):
        # The bounded sender hold: while THIS lease serializes the binding's
        # sender, run poller ticks until the queue drains or the hold elapses,
        # then return — the loop finalizes the job and the sweep re-mints one
        # when new rows arrive, so the sender cycles rather than lives forever.
        # Own transactions: the binding is read in one short
        # transaction, the hold runs with NO job session open (the poller
        # ticks on its own), and the binding's retirement or the paced
        # reschedule each take a short transaction of their own.
        payload = job.get("payload") or {}
        binding_id = str(
            payload.get("binding_id") or job["serialization_key"].split(":", 1)[1]
        )

        async with short(session, job) as reader:
            row = (
                (
                    await reader.execute(
                        text(
                            "SELECT external_ref, workspace_id, state,"
                            f" ({bindings.DELIVERABLE_BINDING_WHERE}) AS deliverable"
                            " FROM channel_bindings WHERE id = :b"
                        ),
                        {"b": binding_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RuntimeError(
                f"deliver_outbox {job['id']}: binding {binding_id} has no row"
            )
        if not row["deliverable"]:
            # Revoked after this job was minted (an admin removed the group,
            # the bot was kicked), or a private chat whose person does not
            # belong (`bindings.deliverable_binding_where`). The claim refuses
            # it anyway (`outbox.claim_next`); ending here spends no hold, and
            # retiring its queue keeps a stale card from going out later, a
            # re-bind's included, and leaves the sweep nothing to mint for.
            async with short(session, job) as writer:
                retired = await bindings.retire_unsettled(writer, binding_id=binding_id)
            logger.info(
                "deliver_outbox %s: binding %s (%s) is not deliverable — nothing"
                " sent, %d retired",
                job["id"],
                binding_id,
                row["state"],
                retired,
            )
            return None
        poller = outbox.OutboxPoller(
            unit_of_work.poller_session_factory(deps.engine, str(row["workspace_id"])),
            binding_id=binding_id,
            past_posts_capability=deps.past_posts_capability,
            transport=deps.transport.for_chat(row["external_ref"]),
            clock=utcnow,
            interval_seconds=cfg.poller_interval_seconds,
            chat_limit=cfg.chat_limit,
            chat_window_seconds=cfg.chat_window_seconds,
            global_limit=cfg.global_limit,
            global_window_seconds=cfg.global_window_seconds,
        )
        deadline = time.monotonic() + cfg.sender_hold_seconds
        while time.monotonic() < deadline:
            before = (poller.deferred, poller.consecutive_failures)
            result = await poller.tick()
            if result is not None and result.get("destination_gone"):
                # The chat will not take messages. Follow it (a group that
                # became a supergroup) or retire the binding; either way this
                # hold ends — the sweep will not mint for a revoked binding.
                moved = result.get("migrate_to")
                async with short(session, job) as writer:
                    followed = await bindings.follow_or_retire(
                        writer, binding_id=binding_id, successor=moved
                    )
                logger.warning(
                    "deliver_outbox %s: binding %s %s (chat gone%s)",
                    job["id"],
                    binding_id,
                    "re-pointed" if followed else "revoked",
                    f", moved to {moved}" if moved else "",
                )
                break
            if result is not None and result.get("state") == "paced":
                # A 429: the hold is on the pacing rows for every replica; this
                # sender yields its lane now and comes back when Telegram said
                # to — its own reschedule, no attempt spent.
                wait = float(result.get("retry_after_s") or cfg.poller_interval_seconds)
                async with short(session, job) as writer:
                    await jobs.reschedule_job(
                        writer,
                        job["id"],
                        job["lease_token"],
                        run_at=utcnow() + timedelta(seconds=wait),
                        restore_attempt=True,
                    )
                logger.info(
                    "deliver_outbox %s: paced by the provider — back in %.0fs",
                    job["id"],
                    wait,
                )
                return jobs.SELF_FINALIZED
            if (
                result is None
                and (
                    poller.deferred,
                    poller.consecutive_failures,
                )
                == before
            ):
                break  # drained: not paced, not failed — nothing pending
            await asyncio.sleep(cfg.poller_interval_seconds)

    from src.services.target import mix_conversion

    async def convert_source_mix(session, job):
        return await mix_conversion.convert_job(
            session, job, evidence=deps.mix_conversion_evidence
        )

    # The existing-account reconciliation backfills grants made before the
    # past-posts source was live, so it exists only where the past-posts source is armed and its
    # client built. The sweep mints for it on the same condition, and only with
    # the release run its jobs are keyed to, so nothing here mints a job that
    # would only park; each deploy's run retries what an earlier one left.
    reconciling = (
        is_activated(deps.past_posts_capability) and deps.past_posts_client is not None
    )

    # The sweep mints the cleanup jobs, then, when the past-posts source is armed, the
    # reconciliation jobs. Own transactions: each door commits on its own,
    # outside any job session, so a failing leg never takes the other back.
    @own_transactions
    async def past_posts_lifecycle_sweep(session, job):
        async with short(session, job) as writer:
            minted = (
                await writer.execute(
                    text("SELECT fn_meta_receipt_cleanup_jobs(:n)"),
                    {"n": PAST_POSTS_SWEEP_LIMIT},
                )
            ).scalar()
        reconcile_minted = 0
        evidence = deps.past_posts_evidence
        if reconciling and isinstance(
            evidence, past_posts_activation.PastPostsActivationEvidence
        ):
            async with short(session, job) as writer:
                reconcile_minted = (
                    await writer.execute(
                        text(
                            "SELECT fn_past_posts_reconcile_jobs(:n, :run_id, :digest)"
                        ),
                        {
                            "n": PAST_POSTS_RECONCILE_LIMIT,
                            "run_id": evidence.run_id,
                            "digest": evidence.digest,
                        },
                    )
                ).scalar()
        # It runs every minute whether or not the past-posts source is on: say so only when
        # it minted something.
        if minted or reconcile_minted:
            logger.info(
                "past_posts_lifecycle_sweep %s: %s cleanup job(s), %s reconciliation job(s)",
                job["id"],
                minted,
                reconcile_minted,
            )

    # Own transactions: its transit drain asks the provider between its short
    # transactions, never inside one (`past_posts_deletion`'s docstring).
    @own_transactions
    async def meta_deletion_cleanup(session, job):
        outcome = await past_posts_deletion.execute(deps, job)
        logger.info("meta_deletion_cleanup %s -> %s", job["id"], outcome)

    # Own transactions: it asks the provider who a token is for between its
    # short transactions, never inside one. A turn that left accounts over put
    # its job back itself (`past_posts_reconciliation.CONTINUED`).
    @own_transactions
    async def reconcile_past_posts_accounts(session, job):
        outcome = await past_posts_reconciliation.execute(deps, job)
        logger.info("reconcile_past_posts_accounts %s -> %s", job["id"], outcome)
        if outcome == past_posts_reconciliation.CONTINUED:
            return jobs.SELF_FINALIZED

    registry: dict = {kind: Parked(_UNBUILT_REASON) for kind in UNBUILT_KINDS}
    registry["past_posts_lifecycle_sweep"] = past_posts_lifecycle_sweep
    # Needs no activation: a deletion closes things (every lifecycle entry point
    # does), so it runs with the keys alone. Without a transit store it asks the
    # provider nothing and counts any upload still owed as unresolved, so a scope
    # with none completes and one with some waits (then needs review).
    registry["meta_deletion_cleanup"] = (
        meta_deletion_cleanup
        if deps.past_posts_keys is not None
        else Parked("deletion cleanup needs PAST_POSTS_HMAC_KEYS")
    )
    if reconciling:
        registry["reconcile_past_posts_accounts"] = reconcile_past_posts_accounts
    elif not is_activated(deps.past_posts_capability):
        registry["reconcile_past_posts_accounts"] = Parked(
            "instagram past posts off (PAST_POSTS_HMAC_KEYS missing or invalid)"
        )
    else:
        registry["reconcile_past_posts_accounts"] = Parked(
            "past-posts reconciliation needs the past-posts adapter's HTTP client"
        )
    registry["convert_source_mix"] = (
        convert_source_mix
        if isinstance(
            deps.mix_conversion_evidence, mix_conversion.MixConversionEvidence
        )
        else Parked("mix conversion evidence unavailable")
    )
    registry["plan_slot"] = plan_slot
    registry["reap_expired"] = reap_expired
    registry["retention_sweep"] = retention_sweep
    # No `deps.adapters` gate: this path makes no provider call, and a fleet with
    # no adapter wired is exactly the one whose sources are stranded.
    registry["alert_stranded_sources"] = alert_stranded_sources
    # NOT parked on `deps.poll`: the executor's notify half needs no provider
    # seam and its ladder half skips loudly without one. See the handler.
    registry["reconcile_ambiguous"] = reconcile_ambiguous
    registry["reap_transit_assets"] = (
        reap_transit
        if deps.transit is not None
        else Parked("no transit store configured (CLOUDINARY_* absent)")
    )
    # The ladder calls all three — fetch, transit.upload, meta.create_container
    # — so it is live only when all three are wired, and parks naming the
    # first one missing.
    _missing = next(
        (
            name
            for name, dep in (
                ("media_fetch", deps.media_fetch),
                ("meta", deps.meta),
                ("transit", deps.transit),
            )
            if dep is None
        ),
        None,
    )
    registry["publish_pipeline"] = (
        run_pipeline
        if _missing is None
        else Parked(
            f"{_missing} is not wired: the publish leg needs media_fetch (composed"
            " from the media-source adapter in worker.compose), meta (the"
            " Instagram Graph adapter) and transit (CLOUDINARY_*) — #1220"
        )
    )
    registry["deliver_outbox"] = (
        deliver_outbox
        if deps.transport is not None
        else Parked("no channel transport configured")
    )

    # `own_transactions`: this opens its own `poller_session_factory`
    # and commits the credential claim before `deps.refresh` — "outside any transaction",
    # as its comment says. It never touches the session passed here.
    @own_transactions
    async def refresh_credential(session, job):
        return await credential_lifecycle.refresh_credential(deps, session, job)

    async def reauth_prompt(session, job):
        return await credential_lifecycle.reauth_prompt(deps, session, job)

    # `own_transactions`: its docstring states that a provider call
    # inside a unit of work violates this codebase's checkpoint discipline,
    # and lists "a floor refusal" among its retryable dispositions — so it
    # reaches the floor, and did so with the loop's session open.
    @own_transactions
    async def revoke_workspace_credentials(session, job):
        return await credential_lifecycle.revoke_workspace_credentials(
            deps, session, job
        )

    registry["refresh_credential"] = (
        refresh_credential
        if deps.refresh is not None
        else Parked("no refresh door provided (compose wires the real one)")
    )

    @own_transactions
    async def send_email(session, job):
        # Own-transactions, for `run_pipeline`'s reason above plus one of its
        # own: a provider call cannot run inside an open transaction.
        sent = await email_sender.execute_send_email(
            job, sender=deps.email, engine=deps.engine
        )
        if sent is None:
            # The executor rescheduled the job itself (over the daily budget);
            # the loop must not finalize it again.
            logger.info("send_email %s deferred by its own budget", job["id"])
            return jobs.SELF_FINALIZED

    registry["send_email"] = (
        send_email
        if deps.email is not None
        else Parked("no email provider configured — set RESEND_API_KEY and EMAIL_FROM")
    )

    async def activation_nudge_sweep(session, job):
        # Enqueues `send_email` jobs and sets each person's latch in the same
        # transaction; no provider call happens here.
        await activation_nudge.sweep_stalled(
            session,
            since_days=cfg.activation_nudge_since_days,
            stall_seconds=cfg.activation_nudge_stall_seconds,
            limit=cfg.activation_nudge_limit,
            web_app_origin=cfg.web_app_origin,
        )

    # Built off. Three gates, each named when it is the one missing: the
    # switch, a provider to send through, and an origin for the email's link.
    nudge_closed = [
        reason
        for is_open, reason in (
            (cfg.activation_nudge_enabled, "TARGET_ACTIVATION_NUDGE_ENABLED is off"),
            (deps.email is not None, "no email provider configured"),
            (bool(cfg.web_app_origin), "no web app origin for the email's link"),
        )
        if not is_open
    ]
    registry["activation_nudge_sweep"] = (
        Parked("; ".join(nudge_closed)) if nudge_closed else activation_nudge_sweep
    )

    # No external seam: the prompt writes outbox rows and nothing else, so it
    # is live in every deployment that has an engine at all.
    async def offboard_workspace(session, job):
        # The loop DISCARDS a handler's return value, so without this line the
        # outcome, the counts and the transit seam's "left to TTL" report reach
        # nobody in production — three docstrings would claim an observability
        # that only the gate had. Same shape as `run_pipeline` above.
        outcome = await offboarding.execute_offboard(deps, session, job)
        logger.info("offboard_workspace %s -> %s", job["id"], outcome)
        return outcome

    # No seam gate. Leg 3 is the only leg with a provider seam and it degrades
    # to the TTL backstop when `deps.transit` is None, so
    # parking the whole workflow for a missing transit store would strand an
    # offboard over the one leg that is allowed to skip.
    registry["offboard_workspace"] = offboard_workspace
    registry["reauth_prompt"] = reauth_prompt
    # Needs no `deps` seam: it talks to Google through the egress floor with a
    # per-call client, the way `ig_refresh` does. Nothing to wire, so nothing
    # to park behind.
    registry["revoke_workspace_credentials"] = revoke_workspace_credentials

    # `own_transactions`: `_run_sync` resolves the provider adapter
    # and owns its sessions around, never across, that adapter's calls;
    # the session passed here is unused.
    @own_transactions
    async def sync_media_source(session, job):
        return await media_sync.sync_media_source(deps, session, job)

    # `own_transactions`: the same `_run_sync`, one page at a time.
    @own_transactions
    async def first_ingest_chunk(session, job):
        return await media_sync.first_ingest_chunk(deps, session, job)

    # Seam-blocked, not unbuilt, and the difference is visible to whoever reads
    # the park reason: an executor that does not exist needs building, an absent
    # seam needs WIRING. A silent park would be indistinguishable from a
    # kind nobody has started.
    _NO_ADAPTERS = Parked("no media adapters configured (registry #1644)")
    registry["sync_media_source"] = sync_media_source if deps.adapters else _NO_ADAPTERS
    registry["first_ingest_chunk"] = (
        first_ingest_chunk if deps.adapters else _NO_ADAPTERS
    )
    return registry


#: Kinds a sweep re-mints on its own: their `failed` is a log line, never a
#: tenant notice ("will not retry" would be a lie). The publish pipeline's
#: producers call `jobs.mint_publish_job`; a sweep does not re-mint it. Its own
#: ceiling covers typed errors, while an escaped exception needs a workspace
#: notice so an intent stranded in `publishing` is visible.
_REMINTED_KINDS = frozenset(
    {
        "deliver_outbox",
        "plan_slot",
        "convert_source_mix",
        # The sweep re-mints cleanup for as long as a scope is due, and a
        # reconciliation is re-found by its own door.
        "meta_deletion_cleanup",
        "reconcile_past_posts_accounts",
    }
)

#: The most cleanup jobs one sweep mints (the door caps it at 25 itself).
PAST_POSTS_SWEEP_LIMIT = 25

#: The most reconciliation jobs one sweep mints (the door caps it at 5 itself).
PAST_POSTS_RECONCILE_LIMIT = 5

#: The user-language sentence for a tenant kind whose budget is spent — the
#: thing, not the kind.
FAILURE_NOTICES: dict[str, str] = {
    "sync_media_source": (
        "The sync of your Drive folder failed and will try again tomorrow;"
        " open Settings › Integrations on the web to check the folder, or pick"
        " it again to sync now."
    ),
    "first_ingest_chunk": (
        "Reading your Drive folder failed part-way and will try again tomorrow;"
        " open Settings › Integrations on the web to check the folder, or pick"
        " it again to sync now."
    ),
    "refresh_credential": (
        "Renewing a connection to Instagram or Google Drive failed and won't"
        " retry on its own; reconnect on the web — Instagram under Settings ›"
        " Accounts, Google Drive under Settings › Integrations."
    ),
    "reauth_prompt": (
        "We could not tell you about a connection that needs attention; open"
        " Settings on the web — Accounts for Instagram, Integrations for Drive."
    ),
    "publish_pipeline": (
        "Posting a story to Instagram hit an unexpected error and stopped;"
        " open the Queue on the web."
    ),
}

#: Kinds whose exhausted job leaves a Drive source with `next_sync_at` NULL
#: (leg 4 nulls it at mint; only a completed sync re-arms it). Without the
#: re-arm below the source would never be selected again — "will try again
#: tomorrow" has to be made true, not just said.
_SYNC_KINDS = frozenset({"sync_media_source", "first_ingest_chunk"})
REARM_AFTER_SECONDS = 24 * 3600


async def _rearm_source(session, job, *, capability=None) -> int:
    """Re-arm the source a spent sync job carried (`payload.source_id`) for
    tomorrow's baseline, if it is still active and still disarmed. Returns
    rows re-armed (0 when the payload names no source, names one that is not
    a uuid, or it moved on). A malformed id is refused here rather than by the
    cast: the re-arm runs in the finalize's own transaction, where a raise
    would abort the finalize and the spent job would be claimed again on
    every lease lapse. The reaper's re-arm tolerates it the same way."""
    source_id = (job.get("payload") or {}).get("source_id")
    workspace_id = job.get("workspace_id")
    if not source_id or workspace_id is None:
        return 0
    try:
        uuid.UUID(str(source_id))
    except ValueError:
        logger.warning(
            "job %s: payload source_id %r is not a uuid; no source re-armed",
            job.get("id"),
            source_id,
        )
        return 0
    from src.services.target import readers, source_read_admission

    source = await readers.row(
        session,
        "SELECT provider FROM media_sources WHERE workspace_id=:ws AND id=:sid",
        ws=str(workspace_id),
        sid=str(source_id),
    )
    if source is not None and source["provider"] == "instagram_past_posts":
        try:
            capture = source_read_admission.capture_from_payload(
                (job.get("payload") or {}).get("source_read_capture")
            )
            if capture.workspace_id != str(workspace_id) or capture.source_id != str(
                source_id
            ):
                return 0
            await source_read_admission.check_source_read(
                session, capture, capability=capability
            )
            await jobs.assert_lease(session, job["id"], job["lease_token"])
        except source_read_admission.SourceReadRefused:
            return 0
    result = await session.execute(
        text(
            "UPDATE media_sources"
            "   SET next_sync_at = now() + make_interval(secs => :secs)"
            " WHERE id = CAST(:s AS uuid) AND workspace_id = CAST(:ws AS uuid)"
            "   AND state = 'active' AND next_sync_at IS NULL"
        ),
        {"s": str(source_id), "ws": str(workspace_id), "secs": REARM_AFTER_SECONDS},
    )
    return int(result.rowcount or 0)


_DEFAULT_NOTICE = (
    "A background task for this workspace failed and won't retry on its own;"
    " open Settings on the web."
)


async def _settle_exhausted(session, job, *, capability=None) -> Optional[dict]:
    """The state a spent job leaves behind, written in the finalize's own
    transaction and ahead of the notice's savepoint, so that a failed notice
    cannot roll it back: a sync's source re-armed for tomorrow, a publish
    job's story flipped to review. Returns the parked story's `tell_review`
    arguments for the notice, or None. Nothing here may raise on a malformed
    payload: outside the savepoint a raise aborts the finalize, and the spent
    job would be claimed again on every lease lapse, so each branch refuses a
    malformed id with a log line instead."""
    kind = str(job.get("kind"))
    if kind in _SYNC_KINDS:
        await _rearm_source(session, job, capability=capability)
        return None
    if kind == "publish_pipeline":
        return await publish_pipeline.flip_exhausted(session, job)
    return None


async def _notify_exhausted(session, job, parked: Optional[dict]) -> None:
    """One `notification` outbox row per push binding when a tenant kind the
    sweeps do not re-mint has spent its budget. Nothing for system kinds and
    the re-minted kinds; a workspace with no binding gets nothing here (the
    log carries it)."""
    kind = str(job.get("kind"))
    workspace_id = job.get("workspace_id")
    if workspace_id is None or kind in _REMINTED_KINDS:
        return
    if kind == "publish_pipeline":
        # The story a dead publish job carried is parked for the
        # workspace's review — the card with its buttons and one honest
        # line — never left reading Approved behind a generic notice. The
        # flip is `_settle_exhausted`'s; this is its courtesy.
        if parked:
            await publish_pipeline.tell_review(session, **parked)
        return
    bindings = await prompts.push_bindings(session, str(workspace_id))
    await outbox.fanout_notification(
        session,
        workspace_id=str(workspace_id),
        bindings=bindings,
        text=FAILURE_NOTICES.get(kind, _DEFAULT_NOTICE),
    )


async def _finalize(session, job, *, undeliverable: bool) -> None:
    """The job's terminal state from its outcome — one spelling.

    Shared by both arms of `_run_job` (the executor that owns its own
    transaction and the one that does not), so the two cannot disagree about
    what `UNDELIVERABLE` means. The *session* is the caller's, because which
    transaction this commits in is the difference between the two arms and
    the one thing that must stay theirs."""
    await jobs.finalize_job(
        session,
        job["id"],
        job["lease_token"],
        terminal_state="review_required" if undeliverable else "succeeded",
    )


class WorkLoop:
    """One lane's claim → dispatch → finalize cycle.

    Observables rather than logs (`OutboxPoller` precedent): `processed`,
    `parked`, `failures`, `fenced`, `consecutive_errors` — a supervisor acts
    on these; escalation stays the entrypoint's.
    """

    def __init__(
        self,
        *,
        claim_conn=None,
        connect: Optional[Callable[[], Any]] = None,
        session_for: Callable[[dict], Any],
        lane: str,
        registry: dict,
        heartbeat,
        config: WorkerConfig,
        worker_name: str,
        past_posts_capability=None,
    ):
        self._claim_conn = claim_conn
        #: A claim checks out a pooled connection, claims (the door
        #: commits) and returns it at once — K loops bound tasks, not
        #: connections. *connect* is `engine.connect` (an async context
        #: manager factory); a bound `claim_conn` is the test seam.
        self._connect = connect
        self._session_for = session_for
        self.lane = lane
        self._registry = registry
        self._heartbeat = heartbeat
        self._config = config
        self._worker_name = worker_name
        #: The past-posts source's activation: the claim door and a spent sync's re-arm
        #: judge a past-posts source by it, and None is an inactive past-posts source.
        self._past_posts_capability = past_posts_capability
        self.processed = 0
        self.parked = 0
        self.failures = 0
        self.fenced = 0
        #: Jobs that spent their budget and ended `failed`.
        self.exhausted = 0
        #: Claims that waited out a momentarily full pool: pressure,
        #: reported on the status line, never an error.
        self.claim_waits = 0
        #: Jobs that ran cleanly and reached NOBODY. Its own counter rather
        #: than a share of `processed`, because the whole point is that the two
        #: are not the same outcome.
        self.undeliverable = 0
        self.consecutive_errors = 0
        self._stop = asyncio.Event()

    def bind_claim_conn(self, conn) -> None:
        """The test seam: pin one connection for this loop's claims. Production
        claims on pooled checkouts via *connect* instead."""
        self._claim_conn = conn

    async def _claim(self):
        claim = dict(
            lane=self.lane,
            worker=self._worker_name,
            lease_seconds=self._config.lease_seconds,
            ws_lane_cap=self._config.ws_lane_cap_for(self.lane),
            capability=self._past_posts_capability,
        )
        if self._claim_conn is not None:
            return await jobs.claim_job(self._claim_conn, **claim)
        assert self._connect is not None  # run_once checked
        async with self._connect() as conn:
            return await jobs.claim_job(conn, **claim)

    async def run_once(self) -> bool:
        """Claim and run at most one job. Returns True when one was claimed."""
        if self._claim_conn is None and self._connect is None:
            raise RuntimeError("WorkLoop.run before bind_claim_conn or connect")
        try:
            job = await self._claim()
        except PoolTimeout:
            # The pool was momentarily full (a transient reader overlapped
            # every task): a bounded wait, not a database fault — counted on
            # its own so the lane's error ceiling never reads pressure as
            # failure.
            self.claim_waits += 1
            logger.warning(
                "lane %s: claim waited out the pool (%d so far) — pressure, not a fault",
                self.lane,
                self.claim_waits,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — survive transient, die loud on persistent
            self.consecutive_errors += 1
            logger.error(
                "lane %s: claim failed (%r) — %d/%d consecutive; the lane dies"
                " loudly at the ceiling",
                self.lane,
                exc,
                self.consecutive_errors,
                self._config.lane_max_consecutive_errors,
            )
            if self.consecutive_errors >= self._config.lane_max_consecutive_errors:
                raise
            return False
        self.consecutive_errors = 0
        if job is None:
            return False
        token = job["lease_token"]
        self._heartbeat.register(token)
        try:
            await self._run_job(job)
        finally:
            self._heartbeat.unregister(token)
        return True

    async def _run_parked(self, job, entry: Parked) -> None:
        """A kind with no seam, or none the registry knows: nothing runs, the
        job is put down for `park_seconds` with its attempt restored, and the
        lane counts it. Its own concern — it never reaches an executor — and
        keeping it out keeps `_run_job` under the size at which its two
        finalize arms could drift."""
        logger.warning(
            "parked kind %s (job %s): %s", job["kind"], job["id"], entry.reason
        )
        async with self._session_for(job) as session:
            await jobs.reschedule_job(
                session,
                job["id"],
                job["lease_token"],
                run_at=utcnow() + timedelta(seconds=self._config.park_seconds),
                restore_attempt=True,
            )
        self.parked += 1

    async def _run_job(self, job) -> None:
        kind = job["kind"]
        entry = self._registry.get(kind)
        if entry is None:
            entry = Parked(
                f"kind {kind!r} is not in the registry — schema drift; parked"
            )
        if isinstance(entry, Parked):
            await self._run_parked(job, entry)
            return
        try:
            if getattr(entry, "owns_transactions", False):
                # An executor that waits — on a provider, on its own
                # checkpoints, on the sender's hold — runs with NO job session
                # open, so no pooled connection sits idle in a transaction for
                # the wait; its finalize takes a short transaction after.
                outcome = await entry(None, job)
                undeliverable = outcome == outbox.UNDELIVERABLE
                if outcome is not jobs.SELF_FINALIZED:
                    async with self._session_for(job) as session:
                        await _finalize(session, job, undeliverable=undeliverable)
            else:
                async with self._session_for(job) as session:
                    outcome = await entry(session, job)
                    # The executor's verdict decides the terminal state. It
                    # was discarded here and `succeeded` hardcoded, which is
                    # what let a producer that reached nobody report a clean
                    # run. Every other executor returns None and is
                    # unaffected.
                    undeliverable = outcome == outbox.UNDELIVERABLE
                    # An executor that finalized or rescheduled its OWN job
                    # says so with `jobs.SELF_FINALIZED` — a second finalize
                    # here would only be fenced and counted as an error.
                    if outcome is not jobs.SELF_FINALIZED:
                        await _finalize(session, job, undeliverable=undeliverable)
            if undeliverable:
                logger.warning(
                    "job %s (%s) reached no delivery surface — parked"
                    " review_required, NOT recorded as delivered",
                    job["id"],
                    kind,
                )
                self.undeliverable += 1
            else:
                self.processed += 1
            self.consecutive_errors = 0
        except jobs.JobFenced:
            logger.warning(
                "job %s fenced during finalize; another owner won", job["id"]
            )
            self.fenced += 1
        except Exception:
            self.failures += 1
            self.consecutive_errors += 1
            now = utcnow()
            if jobs.budget_exhausted(job, now=now):
                # The budget is spent — attempts or the
                # deadline. The job ends `failed`, and a tenant kind the
                # sweeps do not re-mint tells the workspace in its own words
                # (the machine detail stays in the log).
                logger.exception(
                    "job %s (%s) failed and its budget is spent (attempts=%s/%s,"
                    " deadline=%s) — ending failed",
                    job["id"],
                    kind,
                    job.get("attempts"),
                    job.get("max_attempts"),
                    job.get("deadline_at"),
                )
                try:
                    async with self._session_for(job) as session:
                        # Ahead of the notice's savepoint, so a failed notice
                        # cannot roll the spent job's state back.
                        parked = await _settle_exhausted(
                            session, job, capability=self._past_posts_capability
                        )
                        # The notice rides a savepoint: a failure writing it
                        # must not take the finalize down with it (the log
                        # already carries the failure; the notice is a
                        # courtesy, the terminal state is the record).
                        try:
                            async with session.begin_nested():
                                await _notify_exhausted(session, job, parked)
                        except Exception:  # noqa: BLE001 — logged, finalize proceeds
                            logger.exception(
                                "job %s (%s): the exhausted notice could not be"
                                " written; finalizing failed without it",
                                job["id"],
                                kind,
                            )
                        await jobs.finalize_job(
                            session, job["id"], job["lease_token"], "failed"
                        )
                    self.exhausted += 1
                except jobs.JobFenced:
                    self.fenced += 1
                except Exception:  # noqa: BLE001 — the lease expires; the lane lives
                    # A pool wait or a database fault while ending the job:
                    # the lease lapses and the reaper returns the row (the
                    # attempt stays consumed); a failure to record a failure
                    # must not take the lane — and its K−1 in-flight jobs —
                    # down with it.
                    logger.exception(
                        "job %s (%s): could not be finalized failed; its lease"
                        " will lapse",
                        job["id"],
                        kind,
                    )
                return
            backoff = jobs.backoff_seconds(
                str(job.get("lane") or "bulk"), int(job.get("attempts") or 1)
            )
            logger.exception(
                "job %s (%s) failed; rescheduling in %.0fs (attempt %s/%s)",
                job["id"],
                kind,
                backoff,
                job.get("attempts"),
                job.get("max_attempts"),
            )
            try:
                async with self._session_for(job) as session:
                    await jobs.reschedule_job(
                        session,
                        job["id"],
                        job["lease_token"],
                        run_at=now + timedelta(seconds=backoff),
                        restore_attempt=False,
                    )
            except jobs.JobFenced:
                self.fenced += 1
            except Exception:  # noqa: BLE001 — the lease expires; the lane lives
                logger.exception(
                    "job %s (%s): could not be rescheduled; its lease will lapse",
                    job["id"],
                    kind,
                )

    async def run(self) -> None:
        while not self._stop.is_set():
            worked = await self.run_once()
            if not worked:
                await jobs.wait_or_stop(self._stop, self._config.claim_idle_seconds)

    def stop(self) -> None:
        self._stop.set()


async def ensure_sender_jobs(
    session, *, limit: int = WorkerConfig.sender_mint_limit
) -> int:
    """Mint one `deliver_outbox` job per Telegram binding that has pending
    outbox rows and no live sender job. Idempotent by the live-job check on
    the `tg:<binding>` serialization key; returns rows minted.

    The sweep-driven cycle is the design: a sender hold drains and the job
    finalizes `succeeded`; the next sweep re-mints only while pending rows
    exist — or an `ambiguous` row has waited the resolution backoff, which
    grows with its attempts (a live sender's lost answer is resolved by the
    binding's sender, and a quiet binding has none) — so an empty outbox
    mints nothing and a busy one always has exactly one live sender per
    binding.
    """
    attempts, deadline_seconds = jobs.LANE_BUDGETS["interactive"]
    # The statement is `fn_sender_sweep`'s body (worker sweep functions): one INSERT … SELECT whose
    # NOT EXISTS live-job check is its idempotence, a SECURITY DEFINER door
    # because the sweep runs with no tenant and every table it reads is
    # policy-covered. The key prefix, the lane budget and the bound stay
    # spelled once here; the binding predicate is `bindings.push_binding_where`,
    # pinned to the door's body by a test — the live set, which
    # `bindings.deliverable_binding_where` narrows at the sender.
    result = await session.execute(
        text("SELECT fn_sender_sweep(:prefix, :attempts, :deadline, :age, :lim)"),
        {
            "prefix": jobs.SENDER_KEY_PREFIX,
            "attempts": attempts,
            "deadline": deadline_seconds,
            "age": outbox.AMBIGUOUS_RESOLVE_AFTER_SECONDS,
            "lim": int(limit),
        },
    )
    return int(result.scalar() or 0)
