"""The worker's kind→executor registry and per-lane claim loop (#942, #903).

This is the tier half of the composition root: `src.worker` (the entrypoint)
builds engines, connections, `Clock`, `LeaseHeartbeat` and the seams, then
runs one :class:`WorkLoop` per lane over the registry built here. Nothing in
this module knows a channel — the transport arrives as an injected callable
(`outbox.deliver`'s contract: takes the row, returns the external ref).

**Parking is the registry's honest state, not an error path.** The `02` §5
registry names fifteen kinds; six have executors today, and `fn_clock_tick`
already mints two of the executor-less ones (`refresh_credential`,
`sync_media_source`) — measured on #790. A claimed job whose kind cannot run
here is RESCHEDULED with its attempt restored: alive, visible, counted, never
stranded on a lease and never finalized dead. A seam the deployment lacks
(no transport, no transit store, no media_fetch) parks its dependent kind the
same way, with the missing seam named in the reason.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.services.target import credential_lifecycle, email_sender, media_sync

from src.services.target import (
    bindings,
    jobs,
    offboarding,
    outbox,
    prompts,
    publish_pipeline,
    reconciler,
    scheduler,
    unit_of_work,
)

logger = logging.getLogger("target.work_loop")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Parked:
    """A kind this deployment cannot run, and why. Loud by construction."""

    reason: str


@dataclass(frozen=True)
class WorkerConfig:
    """The `05` numbers as parameters, env-overridable at the entrypoint.

    Defaults follow `05-operations` where it states a number and are marked
    provisional where it does not; the composition root owns overriding them.
    """

    lease_seconds: float = 90.0  # 05: 60–120 band
    # `05:31` row 1, phase 3b (F7 (a)): K concurrent claim-and-run tasks per
    # lane in ONE process, on pooled checkouts — K bounds tasks, not
    # connections. Start values under the ceiling `assert_concurrency_fits`
    # enforces; raising them past it needs the measurement the gate records.
    lane_concurrency: Mapping[str, int] = field(
        default_factory=lambda: {"interactive": 3, "bulk": 2}
    )
    # `05:33` row 3 — per-workspace, per-lane: a workspace can never own a
    # lane. Interactive 5 (half one replica's interactive pool), bulk 3
    # (one publish + one sync + one misc). Raised from 2/2 in phase 3a.
    ws_lane_cap_interactive: int = 5
    ws_lane_cap_bulk: int = 3
    claim_idle_seconds: float = 1.0  # sleep when a lane has nothing runnable
    retry_backoff_seconds: float = 60.0  # R8 retryable-failure backoff
    park_seconds: float = 900.0  # executor-less kinds retry this often
    # < lease_seconds: the poller's hold per claim. 45 → 15 in phase 3a so a
    # busy binding yields its lane sooner; the sweep re-mints while rows remain.
    sender_hold_seconds: float = 15.0
    sender_sweep_seconds: float = 3.0  # cadence of the sender-job mint sweep
    prompt_sweep_seconds: float = 5.0  # cadence of the prompt sweep (W3)
    lane_max_consecutive_errors: int = 10  # claim errors before the lane dies loudly
    poller_interval_seconds: float = 2.0  # 05: outbox cadence
    chat_limit: int = 18  # 05: per-chat sends per window
    chat_window_seconds: int = 60
    global_limit: int = 25  # 05: global sends per window
    global_window_seconds: int = 1
    reap_limit: int = 200
    approval_ttl_seconds: int = 72 * 3600
    approved_ttl_seconds: int = 72 * 3600
    reconcile_limit: int = 50
    reconcile_notify_after_seconds: int = 6 * 3600
    transit_reap_older_than_seconds: int = 48 * 3600
    stranded_alert_after_seconds: int = 24 * 3600  # re-alert cadence (#1061)
    stranded_alert_limit: int = 200  # rows re-alerted per beat
    # 05: "no media available" notice dedup 24 h (06 section 5, slot missed).
    no_media_notice_after_seconds: int = 24 * 3600
    # The front end's origin (`settings.web_app_origin`), for the deep link in
    # the parked-intent notice (06 section 5). None = the notice still fires,
    # without a link: being told late beats not being told.
    web_app_origin: Optional[str] = None

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
    offboard_drain_timeout_seconds: int = 15 * 60  # 05: publish-drain 15 min
    offboard_drain_recheck_seconds: int = 60  # provisional: 05 states no cadence
    offboard_drain_limit: int = 500  # provisional: 05 states no bound


#: Connections the worker holds besides its claim-and-run tasks, counted as
#: the STEADY holders: the clock's pinned election connection, plus two
#: periodic holders that may coincide (a clock tick's session and a
#: heartbeat beat). The other periodic readers — the status reporter (60 s),
#: the sender sweeper (3 s), the prompt sweeper (5 s) — and a kind that opens
#: a second session inside its job (`reconcile_ambiguous`'s poll) are
#: transient waiters: they hold a connection for milliseconds, and when the
#: pool is momentarily full they wait `pool_timeout`, which is not a fault
#: (`run_once` counts a claim's pool wait apart from errors). So the ceiling
#: bounds what is held at once in steady state, not every possible overlap.
RESERVED_CONNECTIONS = 3
#: What one task holds at its peak, per lane. An interactive task's kinds run
#: their own transactions (the sender) or one short job transaction: one
#: connection. A bulk task's plain kinds — a sync walk, a credential refresh,
#: the ambiguous reconciler's poll — open sessions of their own UNDER the
#: loop's job transaction, so a bulk task holds two at its peak (adversarial
#: review of the 3b PR). The ceiling weighs them so, rather than pretending.
TASK_CONNECTIONS = {"interactive": 1, "bulk": 2}


def task_connections(config: WorkerConfig) -> int:
    return sum(
        config.concurrency_for(lane) * TASK_CONNECTIONS[lane]
        for lane in ("interactive", "bulk")
    )


def assert_concurrency_fits(config: WorkerConfig, *, pool_size: int) -> str:
    """The phase 3b ceiling: every task DB-active at once (weighed by what a
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
            " or raise the pool with the measurement 03_worker-throughput.md"
            " step 10 asks for"
        )
    return line


def own_transactions(fn):
    """Mark an executor that opens its OWN transactions against the engine —
    the publish pipeline's checkpoints, the email sender's provider call,
    the outbox sender's hold — so the loop runs it with NO job session open
    and finalizes in a transaction of its own afterwards (phase 3b: a task
    waiting on a provider holds no pooled connection while it waits)."""
    fn.owns_transactions = True
    return fn


@dataclass
class WorkerDeps:
    """The injected seams. `None` parks the dependent kind rather than faking it."""

    meta: Any = None
    transit: Any = None
    #: The Drive read leg (#982) — ONE adapter for W6's listing and W5b's
    #: bytes, so the two workstreams cannot build divergent copies. Duck-typed
    #: (`list_files` / `fetch_bytes`); `StubDriveAdapter` until M.3 (#862).
    drive: Any = None
    media_fetch: Optional[Callable[[dict], Any]] = None
    #: The `07` §1 EmailSender port, or None when no provider is wired (#1092).
    email: Optional[Any] = None
    transport: Optional[Callable[[dict], Any]] = None
    poll: Optional[Callable[..., Any]] = None
    refresh: Optional[Callable[..., Any]] = None
    engine: Any = None
    config: WorkerConfig = field(default_factory=WorkerConfig)


_UNBUILT_REASON = (
    "no executor exists in the target tier (build-path W6/X.3/S.4); "
    "job stays alive and re-checks on the park cadence"
)

#: Kinds the tier has never carried an executor for. The registry parks them
#: unconditionally; the schema-derived completeness test keeps this honest.
UNBUILT_KINDS = (
    "retention_sweep",
    "reencrypt_credentials",
)


def build_registry(deps: WorkerDeps) -> dict:
    """kind → adapter | Parked, for every kind in the `02` §5 registry."""

    cfg = deps.config

    async def plan_slot(session, job):
        payload = job.get("payload") or {}
        # The slot rides jsonb as a string Postgres rendered, so POSTGRES
        # parses it back (#969): PG strips trailing fractional zeros when
        # rendering timestamptz into jsonb, and CPython's fromisoformat is
        # version-sensitive about fraction widths (3.10, the repo floor,
        # rejects most of them — a stranding clock, not an outage, because
        # the R8 backoff swallowed the ValueError). Producer parses its own
        # rendering; no interpreter rule is encoded anywhere.
        row = (
            (
                await session.execute(
                    text(
                        "SELECT a.provider_account_ref, a.state, w.approval_mode,"
                        # The inner CAST AS text pins $1's inferred type —
                        # `.claude/rules/database.md` › bound parameters.
                        "       CAST(CAST(:slot AS text) AS timestamptz) AS slot_at"
                        " FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
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
            approval_mode=row["approval_mode"],
            no_media_notice_after_seconds=cfg.no_media_notice_after_seconds,
        )
        if outcome.notice is not None:
            # The library was empty AND there was no surface to say so on.
            # Passed through rather than re-derived: `notice` already IS the
            # verdict, so re-testing it here would be a second place to keep
            # in step with the sentinel.
            return outcome.notice
        if outcome.intent_id is not None:
            # The fast path of the `02` §4 prompt edge: mint and prompt on
            # the same beat, same transaction. The prompt sweep is the
            # correctness backstop for anything this misses (a crash between
            # mint and prompt, or intents minted before W3 existed).
            await prompts.sweep_due_prompts(session, limit=1)

    async def reap_expired(session, job):
        await scheduler.execute_reap_expired(
            session,
            limit=cfg.reap_limit,
            approval_ttl_seconds=cfg.approval_ttl_seconds,
            approved_ttl_seconds=cfg.approved_ttl_seconds,
        )
        # The cards of what the reaper (or anyone) ended lose their buttons
        # and gain the terminal line — phase 1 of the 2026-09-09 tap plan.
        await prompts.sweep_settled_cards(session, limit=cfg.reap_limit)

    async def reconcile_ambiguous(session, job):
        """The `02` §6 sweep. TWO reasons, and only one of them needs a poll.

        `fn_reconciler_sweep` tags every row `ladder_due` or `notify_window`.
        A ladder row is an ambiguous publish that must be resolved against the
        provider; a notify row is a parked `review_required` intent whose
        customer-notification window has passed (`06` §5), which is purely
        informational. Feeding the second into `reconcile_intent` — what this
        did before the tag was read — polls a provider about an intent whose
        ladder is already spent, and produces no notification at all. That is
        why #1090 D4 had no producer despite the door shipping in 059.

        **The poll seam parks the LADDER half, not the kind.** The module
        docstring's rule is that a missing seam parks its *dependent* kind; the
        notify half depends on nothing the deployment lacks, so parking it with
        the ladder over-parks. This matters concretely rather than in
        principle: production runs `poll=None` (`worker.py`), so under the old
        registration the whole kind was `Parked` and the customer notification
        could not fire even once the producer existed.
        """
        due = await reconciler.sweep_due(
            session,
            limit=cfg.reconcile_limit,
            notify_after_seconds=cfg.reconcile_notify_after_seconds,
        )
        ladder_skipped = unreachable = 0
        for op in due:
            if op["reason"] == "notify_window":
                sent = await reconciler.notify_parked_customer(
                    session,
                    intent_id=op["intent_id"],
                    workspace_id=op["workspace_id"],
                    web_app_origin=cfg.web_app_origin,
                    retry_after_seconds=cfg.reconcile_notify_after_seconds,
                )
                if sent == outbox.UNDELIVERABLE:
                    unreachable += 1
                continue
            if deps.poll is None:
                ladder_skipped += 1
                continue
            # Scope the ladder row too. 059 says every write from this sweep
            # "runs tenant-scoped as svc_worker" and nothing did — the session
            # carries `app.tenant_id = ''` because this is a system singleton,
            # so these writes were invisible to `p_tenant` already. Reading the
            # reason tag also makes the tenant VARY across one sweep, so
            # asserting it per row is what keeps a ladder row from inheriting
            # the scope of whichever notify row preceded it.
            await unit_of_work.apply_gucs(
                session,
                tenant_id=str(op["workspace_id"]),
                actor_kind="system",
            )
            await reconciler.reconcile_intent(
                session,
                intent_id=op["intent_id"],
                workspace_id=op["workspace_id"],
                poll=deps.poll,
                checks=op.get("checks", 0),
            )
        if ladder_skipped:
            # Loud, per the module docstring: the deployment cannot do this
            # half and says so every beat it has work for it.
            logger.warning(
                "reconcile_ambiguous: %d ladder-due intent(s) left unpolled —"
                " no provider poll seam configured (stub Meta adapter supplies"
                " it); the notify half ran",
                ladder_skipped,
            )
        if unreachable:
            # One workspace with nowhere to receive its notice makes the WHOLE
            # sweep not-a-delivery. The rows that did land are already written
            # in this transaction; what must not happen is the run reading as
            # clean when somebody was owed a message and got none.
            return outbox.UNDELIVERABLE

    async def alert_stranded_sources(session, job):
        # Alert-only: nothing here re-arms a source or enqueues a sync. The
        # re-arm is fork F4 (a) and belongs to the connect flow (#1061).
        await media_sync.alert_stranded_sources(
            session,
            stale_after_seconds=cfg.stranded_alert_after_seconds,
            limit=cfg.stranded_alert_limit,
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
        # opens no job session for it (phase 3b) and it finalizes itself.
        outcome = await publish_pipeline.run_publish_pipeline(
            dict(job),
            engine=deps.engine,
            meta=deps.meta,
            transit=deps.transit,
            media_fetch=deps.media_fetch,
        )
        logger.info("publish_pipeline %s -> %s", job["id"], outcome)
        # The pipeline finalizes or reschedules its own job in its own
        # transactions; the loop must not finalize again.
        return jobs.SELF_FINALIZED

    sessions = make_session_for(deps.engine)

    @own_transactions
    async def deliver_outbox(session, job):
        # The bounded sender hold: while THIS lease serializes the binding's
        # sender, run poller ticks until the queue drains or the hold elapses,
        # then return — the loop finalizes the job and the sweep re-mints one
        # when new rows arrive, so the sender cycles rather than lives forever.
        # Own transactions (phase 3b): the binding is read in one short
        # transaction, the hold runs with NO job session open (the poller
        # ticks on its own), and the binding's retirement or the paced
        # reschedule each take a short transaction of their own.
        payload = job.get("payload") or {}
        binding_id = str(
            payload.get("binding_id") or job["serialization_key"].split(":", 1)[1]
        )

        def short():
            # The loop hands a marked executor no session (phase 3b); a caller
            # that DOES pass one (the unit seam) keeps it for every write.
            return sessions(job) if session is None else nullcontext(session)

        async with short() as reader:
            row = (
                (
                    await reader.execute(
                        text(
                            "SELECT external_ref, workspace_id FROM channel_bindings"
                            " WHERE id = :b"
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
        poller = outbox.OutboxPoller(
            poller_session_factory(deps.engine, str(row["workspace_id"])),
            binding_id=binding_id,
            transport=deps.transport.for_chat(row["external_ref"]),
            clock=_utcnow,
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
                async with short() as writer:
                    followed = bool(moved) and await bindings.repoint(
                        writer, binding_id=binding_id, external_ref=str(moved)
                    )
                    if not followed:
                        await bindings.revoke_by_id(writer, binding_id=binding_id)
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
                # to — its own reschedule, no attempt spent (phase 3a step 2).
                wait = float(result.get("retry_after_s") or cfg.poller_interval_seconds)
                async with short() as writer:
                    await jobs.reschedule_job(
                        writer,
                        job["id"],
                        job["lease_token"],
                        run_at=_utcnow() + timedelta(seconds=wait),
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

    registry: dict = {kind: Parked(_UNBUILT_REASON) for kind in UNBUILT_KINDS}
    # W6's two kinds are seam-blocked rather than unbuilt, and the difference is
    # visible to whoever reads the park reason: an executor that does not exist
    # needs building, an absent seam needs WIRING. Naming the seam is the
    # contract W6 parks behind (#982) — a silent park would be indistinguishable
    # from a kind nobody has started.
    if deps.drive is None:
        for _kind in ("sync_media_source", "first_ingest_chunk"):
            registry[_kind] = Parked(
                "no Drive read seam configured (WorkerDeps.drive is None;"
                " build-path #982) — the executor is blocked on the seam, not"
                " unwritten"
            )
    registry["plan_slot"] = plan_slot
    registry["reap_expired"] = reap_expired
    # No `deps.drive` gate: this path makes no provider call, and a fleet with
    # no adapter wired is exactly the one whose sources are stranded (#1061).
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
    # first one missing (#1220 step 3; the seam astrid named on #982).
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
            " Instagram Graph adapter) and transit (CLOUDINARY_*) — build-path"
            " W5b / #1220 step 3"
        )
    )
    registry["deliver_outbox"] = (
        deliver_outbox
        if deps.transport is not None
        else Parked("no channel transport configured (build-path W2)")
    )

    async def refresh_credential(session, job):
        return await credential_lifecycle.refresh_credential(deps, session, job)

    async def reauth_prompt(session, job):
        return await credential_lifecycle.reauth_prompt(deps, session, job)

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
        # own: a provider call cannot run inside an open transaction (`02` §5).
        sent = await email_sender.execute_send_email(
            job, sender=deps.email, engine=deps.engine
        )
        if sent is None:
            # The executor rescheduled the job itself (over the daily budget);
            # the loop must not finalize it again (phase 3a: the sentinel
            # replaces the fence warning this path used to log).
            logger.info("send_email %s deferred by its own budget", job["id"])
            return jobs.SELF_FINALIZED

    registry["send_email"] = (
        send_email
        if deps.email is not None
        else Parked(
            "no email provider configured — set RESEND_API_KEY and EMAIL_FROM"
            " (`07` §1's owner ack on adding Resend is OPEN, #1092)"
        )
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
    # to `06` §1's documented TTL backstop when `deps.transit` is None, so
    # parking the whole workflow for a missing transit store would strand an
    # offboard over the one leg that is allowed to skip.
    registry["offboard_workspace"] = offboard_workspace
    registry["reauth_prompt"] = reauth_prompt
    # Needs no `deps` seam: it talks to Google through the egress floor with a
    # per-call client, the way `ig_refresh` does. Nothing to wire, so nothing
    # to park behind (#1083).
    registry["revoke_workspace_credentials"] = revoke_workspace_credentials

    async def sync_media_source(session, job):
        return await media_sync.sync_media_source(deps, session, job)

    async def first_ingest_chunk(session, job):
        return await media_sync.first_ingest_chunk(deps, session, job)

    _NO_DRIVE = Parked(
        "no drive door configured (build-path #982); wiring a test fake into"
        " production is not composition"
    )
    registry["sync_media_source"] = (
        sync_media_source if deps.drive is not None else _NO_DRIVE
    )
    registry["first_ingest_chunk"] = (
        first_ingest_chunk if deps.drive is not None else _NO_DRIVE
    )
    return registry


#: Kinds a sweep re-mints on its own: their `failed` is a log line, never a
#: tenant notice ("will not retry" would be a lie). `publish_pipeline` is NOT
#: here: only `approve` mints it, and its own ceiling covers the errors it
#: types — an exception that ESCAPES it is exactly the case the workspace must
#: hear about, or the intent strands in `publishing` with nobody told.
_REMINTED_KINDS = frozenset({"deliver_outbox", "plan_slot"})

#: The user-language sentence for a tenant kind whose budget is spent — the
#: thing, not the kind (`05:38`; phase 3a step 3).
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
        " retry on its own; open Settings › Integrations on the web to reconnect."
    ),
    "reauth_prompt": (
        "We could not tell you about a connection that needs attention; open"
        " Settings › Integrations on the web."
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


async def _rearm_source(session, job) -> int:
    """Re-arm the source a spent sync job carried (`payload.source_id`) for
    tomorrow's baseline, if it is still active and still disarmed. Returns
    rows re-armed (0 when the payload names no source or it moved on)."""
    source_id = (job.get("payload") or {}).get("source_id")
    workspace_id = job.get("workspace_id")
    if not source_id or workspace_id is None:
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


async def _notify_exhausted(session, job) -> None:
    """One `notification` outbox row per push binding when a tenant kind the
    sweeps do not re-mint has spent its budget. Nothing for system kinds and
    the re-minted kinds; a workspace with no binding gets nothing here (the
    log carries it)."""
    kind = str(job.get("kind"))
    workspace_id = job.get("workspace_id")
    if workspace_id is None or kind in _REMINTED_KINDS:
        return
    if kind in _SYNC_KINDS:
        await _rearm_source(session, job)
    from src.services.target import prompts  # noqa: PLC0415 — cycle

    bindings = await prompts.push_bindings(session, str(workspace_id))
    await outbox.fanout_notification(
        session,
        workspace_id=str(workspace_id),
        bindings=bindings,
        text=FAILURE_NOTICES.get(kind, _DEFAULT_NOTICE),
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
    ):
        self._claim_conn = claim_conn
        #: Phase 3b: a claim checks out a pooled connection, claims (the door
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
        self.processed = 0
        self.parked = 0
        self.failures = 0
        self.fenced = 0
        #: Jobs that spent their `05:38` budget and ended `failed` (phase 3a).
        self.exhausted = 0
        #: Claims that waited out a momentarily full pool (phase 3b): pressure,
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
        (phase 3b) claims on pooled checkouts via *connect* instead."""
        self._claim_conn = conn

    async def _claim(self):
        claim = dict(
            lane=self.lane,
            worker=self._worker_name,
            lease_seconds=self._config.lease_seconds,
            ws_lane_cap=self._config.ws_lane_cap_for(self.lane),
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
            # failure (phase 3b review).
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

    async def _run_job(self, job) -> None:
        kind = job["kind"]
        entry = self._registry.get(kind)
        if entry is None:
            entry = Parked(
                f"kind {kind!r} is not in the registry — schema drift; parked"
            )
        if isinstance(entry, Parked):
            logger.warning("parked kind %s (job %s): %s", kind, job["id"], entry.reason)
            async with self._session_for(job) as session:
                await jobs.reschedule_job(
                    session,
                    job["id"],
                    job["lease_token"],
                    run_at=_utcnow() + timedelta(seconds=self._config.park_seconds),
                    restore_attempt=True,
                )
            self.parked += 1
            return
        try:
            if getattr(entry, "owns_transactions", False):
                # Phase 3b: an executor that waits — on a provider, on its own
                # checkpoints, on the sender's hold — runs with NO job session
                # open, so no pooled connection sits idle in a transaction for
                # the wait; its finalize takes a short transaction after.
                outcome = await entry(None, job)
                undeliverable = outcome == outbox.UNDELIVERABLE
                if outcome is not jobs.SELF_FINALIZED:
                    async with self._session_for(job) as session:
                        await jobs.finalize_job(
                            session,
                            job["id"],
                            job["lease_token"],
                            terminal_state=(
                                "review_required" if undeliverable else "succeeded"
                            ),
                        )
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
                    # here would only be fenced and counted as an error
                    # (phase 3a).
                    if outcome is not jobs.SELF_FINALIZED:
                        await jobs.finalize_job(
                            session,
                            job["id"],
                            job["lease_token"],
                            terminal_state=(
                                "review_required" if undeliverable else "succeeded"
                            ),
                        )
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
            now = _utcnow()
            if jobs.budget_exhausted(job, now=now):
                # F6 (a): the `05:38` budget is spent — attempts or the
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
                        # The notice rides a savepoint: a failure writing it
                        # must not take the finalize down with it (the log
                        # already carries the failure; the notice is a
                        # courtesy, the terminal state is the record).
                        try:
                            async with session.begin_nested():
                                await _notify_exhausted(session, job)
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
                    # down with it (adversarial review of the 3b PR).
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


def make_session_for(engine):
    """Per-job transaction contexts with the GUC invariant applied once.

    Tenant scope comes from the claimed row (system singletons carry none and
    get an empty tenant id — fail-closed under any tenant policy); the actor
    is `system`, the `02` §4 worker actor. Lives here (phase 3b) because the
    sender executor takes its own short transactions through it.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)

    def session_for(job: dict):
        @asynccontextmanager
        async def ctx():
            async with maker() as session:
                async with session.begin():
                    await unit_of_work.apply_gucs(
                        session,
                        tenant_id=str(job.get("workspace_id") or ""),
                        actor_kind="system",
                    )
                    yield session

        return ctx()

    return session_for


def poller_session_factory(engine, tenant_id: str):
    """Sessions for the outbox poller with the GUC invariant pre-applied.

    The poller opens its own transaction per tick and commits it; SET LOCAL
    inside that transaction is what keeps pool reuse safe (`apply_gucs`).
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as session:
            await unit_of_work.apply_gucs(
                session, tenant_id=tenant_id, actor_kind="system"
            )
            yield session

    return factory


async def ensure_sender_jobs(session) -> int:
    """Mint one `deliver_outbox` job per Telegram binding that has pending
    outbox rows and no live sender job. Idempotent by the live-job check on
    the `tg:<binding>` serialization key; returns rows minted.

    The sweep-driven cycle is the design: a sender hold drains and the job
    finalizes `succeeded`; the next sweep re-mints only while pending rows
    exist, so an empty outbox mints nothing and a busy one always has exactly
    one live sender per binding.
    """
    result = await session.execute(
        text(
            "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
            " run_at, max_attempts, deadline_at, payload)"
            " SELECT 'deliver_outbox', b.workspace_id, 'interactive',"
            "        'tg:' || b.id, now(), 3, now() + interval '10 minutes',"
            "        jsonb_build_object('v', 1, 'binding_id', b.id)"
            "   FROM channel_bindings b"
            "  WHERE b.state = 'active' AND b.channel LIKE 'telegram%'"
            "    AND EXISTS (SELECT 1 FROM channel_outbox o"
            "                 WHERE o.binding_id = b.id AND o.state = 'pending')"
            "    AND NOT EXISTS (SELECT 1 FROM jobs j"
            "                     WHERE j.serialization_key = 'tg:' || b.id"
            "                       AND j.state IN ('ready', 'leased'))"
            # H5: a sweep is bounded; the next one takes the rest.
            "  LIMIT 200"
        )
    )
    return result.rowcount
