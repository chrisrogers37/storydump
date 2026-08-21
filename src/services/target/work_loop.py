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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text

from src.services.target import jobs, publish_pipeline, reconciler, scheduler

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
    ws_lane_cap: int = 2  # per-workspace per-lane concurrency
    claim_idle_seconds: float = 1.0  # sleep when a lane has nothing runnable
    retry_backoff_seconds: float = 60.0  # R8 retryable-failure backoff
    park_seconds: float = 900.0  # executor-less kinds retry this often
    sender_hold_seconds: float = 45.0  # < lease_seconds: poller hold per claim
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
    clock_interval_seconds: float = 15.0
    clock_max_inserts: int = 500
    refresh_cadence_seconds: int = 7 * 24 * 3600
    heartbeat_interval_seconds: float = 20.0


@dataclass
class WorkerDeps:
    """The injected seams. `None` parks the dependent kind rather than faking it."""

    meta: Any = None
    transit: Any = None
    media_fetch: Optional[Callable[[dict], Any]] = None
    transport: Optional[Callable[[dict], Any]] = None
    poll: Optional[Callable[..., Any]] = None
    engine: Any = None
    config: WorkerConfig = field(default_factory=WorkerConfig)


_UNBUILT_REASON = (
    "no executor exists in the target tier (build-path W5d/W6/X.3/S.4); "
    "job stays alive and re-checks on the park cadence"
)

#: Kinds the tier has never carried an executor for. The registry parks them
#: unconditionally; the schema-derived completeness test keeps this honest.
UNBUILT_KINDS = (
    "sync_media_source",
    "first_ingest_chunk",
    "refresh_credential",
    "offboard_workspace",
    "revoke_workspace_credentials",
    "reauth_prompt",
    "retention_sweep",
    "reencrypt_credentials",
    "send_email",
)


def build_registry(deps: WorkerDeps) -> dict:
    """kind → adapter | Parked, for every kind in the `02` §5 registry."""

    cfg = deps.config

    async def plan_slot(session, job):
        payload = job.get("payload") or {}
        row = (
            (
                await session.execute(
                    text(
                        "SELECT a.provider_account_ref, w.approval_mode"
                        " FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
                        " WHERE a.id = :acct"
                    ),
                    {"acct": str(payload["ig_account_id"])},
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
        await scheduler.execute_plan_slot(
            session,
            workspace_id=str(job["workspace_id"]),
            ig_account_id=str(payload["ig_account_id"]),
            slot_at=payload["slot_at"],
            provider_account_ref=row["provider_account_ref"],
            approval_mode=row["approval_mode"],
        )

    async def reap_expired(session, job):
        await scheduler.execute_reap_expired(
            session,
            limit=cfg.reap_limit,
            approval_ttl_seconds=cfg.approval_ttl_seconds,
            approved_ttl_seconds=cfg.approved_ttl_seconds,
        )

    async def reconcile_ambiguous(session, job):
        due = await reconciler.sweep_due(
            session,
            limit=cfg.reconcile_limit,
            notify_after_seconds=cfg.reconcile_notify_after_seconds,
        )
        for op in due:
            await reconciler.reconcile_intent(
                session,
                intent_id=op["intent_id"],
                workspace_id=op["workspace_id"],
                poll=deps.poll,
                checks=op.get("checks", 0),
            )

    async def reap_transit(session, job):
        await scheduler.execute_reap_transit_assets(
            session,
            lister=deps.transit.list_stale,
            deleter=deps.transit.destroy_asset,
            older_than_seconds=cfg.transit_reap_older_than_seconds,
        )

    async def run_pipeline(session, job):
        # The pipeline owns its own transactions against the engine; the
        # session here is only the finalization context the loop holds.
        outcome = await publish_pipeline.run_publish_pipeline(
            dict(job),
            engine=deps.engine,
            meta=deps.meta,
            transit=deps.transit,
            media_fetch=deps.media_fetch,
        )
        logger.info("publish_pipeline %s -> %s", job["id"], outcome)

    async def deliver_outbox(session, job):
        # W2 replaces this body with the bounded sender hold: run an
        # OutboxPoller for < lease_seconds while THIS lease serializes the
        # binding's sender, then reschedule so the job cycles, not finalizes.
        raise NotImplementedError("the W2 increment wires the sender hold")

    registry: dict = {kind: Parked(_UNBUILT_REASON) for kind in UNBUILT_KINDS}
    registry["plan_slot"] = plan_slot
    registry["reap_expired"] = reap_expired
    registry["reconcile_ambiguous"] = (
        reconcile_ambiguous
        if deps.poll is not None
        else Parked("no provider poll seam configured (stub Meta adapter supplies it)")
    )
    registry["reap_transit_assets"] = (
        reap_transit
        if deps.transit is not None
        else Parked("no transit store configured (CLOUDINARY_* absent)")
    )
    registry["publish_pipeline"] = (
        run_pipeline
        if deps.media_fetch is not None
        else Parked(
            "media_fetch has no production implementation (build-path W5b);"
            " wiring a test fake into production is not composition"
        )
    )
    registry["deliver_outbox"] = (
        deliver_outbox
        if deps.transport is not None
        else Parked("no channel transport configured (build-path W2)")
    )
    return registry


class WorkLoop:
    """One lane's claim → dispatch → finalize cycle.

    Observables rather than logs (`OutboxPoller` precedent): `processed`,
    `parked`, `failures`, `fenced`, `consecutive_errors` — a supervisor acts
    on these; escalation stays the entrypoint's.
    """

    def __init__(
        self,
        *,
        claim_conn,
        session_factory,
        lane: str,
        registry: dict,
        heartbeat,
        config: WorkerConfig,
        worker_name: str,
    ):
        self._claim_conn = claim_conn
        self._session_factory = session_factory
        self._lane = lane
        self._registry = registry
        self._heartbeat = heartbeat
        self._config = config
        self._worker_name = worker_name
        self.processed = 0
        self.parked = 0
        self.failures = 0
        self.fenced = 0
        self.consecutive_errors = 0
        self._stop = asyncio.Event()

    async def run_once(self) -> bool:
        """Claim and run at most one job. Returns True when one was claimed."""
        job = await jobs.claim_job(
            self._claim_conn,
            lane=self._lane,
            worker=self._worker_name,
            lease_seconds=self._config.lease_seconds,
            ws_lane_cap=self._config.ws_lane_cap,
        )
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
            async with self._session_factory.begin() as session:
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
            async with self._session_factory.begin() as session:
                await entry(session, job)
                await jobs.finalize_job(
                    session,
                    job["id"],
                    job["lease_token"],
                    terminal_state="succeeded",
                )
            self.processed += 1
            self.consecutive_errors = 0
        except jobs.JobFenced:
            logger.warning(
                "job %s fenced during finalize; another owner won", job["id"]
            )
            self.fenced += 1
        except Exception:
            logger.exception(
                "job %s (%s) failed; rescheduling with backoff", job["id"], kind
            )
            self.failures += 1
            self.consecutive_errors += 1
            try:
                async with self._session_factory.begin() as session:
                    await jobs.reschedule_job(
                        session,
                        job["id"],
                        job["lease_token"],
                        run_at=_utcnow()
                        + timedelta(seconds=self._config.retry_backoff_seconds),
                        restore_attempt=False,
                    )
            except jobs.JobFenced:
                self.fenced += 1

    async def run(self) -> None:
        while not self._stop.is_set():
            worked = await self.run_once()
            if not worked:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._config.claim_idle_seconds
                    )
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()
