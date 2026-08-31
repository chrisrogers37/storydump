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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.services.target import credential_lifecycle, email_sender, media_sync

from src.services.target import (
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
    ws_lane_cap: int = 2  # per-workspace per-lane concurrency
    claim_idle_seconds: float = 1.0  # sleep when a lane has nothing runnable
    retry_backoff_seconds: float = 60.0  # R8 retryable-failure backoff
    park_seconds: float = 900.0  # executor-less kinds retry this often
    sender_hold_seconds: float = 45.0  # < lease_seconds: poller hold per claim
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
    clock_interval_seconds: float = 15.0
    clock_max_inserts: int = 500
    refresh_cadence_seconds: int = 7 * 24 * 3600
    heartbeat_interval_seconds: float = 20.0
    offboard_grace_seconds: int = 30 * 24 * 3600  # 05: grace window 30 days
    offboard_drain_timeout_seconds: int = 15 * 60  # 05: publish-drain 15 min
    offboard_drain_recheck_seconds: int = 60  # provisional: 05 states no cadence


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
                        "SELECT a.provider_account_ref, w.approval_mode,"
                        # The inner CAST AS text pins $1's inferred type: bare
                        # CAST(:slot AS timestamptz) makes PG infer the param
                        # as timestamptz and asyncpg then refuses the string
                        # (measured — the gate went red on exactly that).
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
        slot_at = row["slot_at"]
        intent_id = await scheduler.execute_plan_slot(
            session,
            workspace_id=str(job["workspace_id"]),
            ig_account_id=str(payload["ig_account_id"]),
            slot_at=slot_at,
            provider_account_ref=row["provider_account_ref"],
            approval_mode=row["approval_mode"],
        )
        if intent_id is not None:
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
        # The bounded sender hold: while THIS lease serializes the binding's
        # sender, run poller ticks until the queue drains or the hold elapses,
        # then return — the loop finalizes the job and the sweep re-mints one
        # when new rows arrive, so the sender cycles rather than lives forever.
        payload = job.get("payload") or {}
        binding_id = str(
            payload.get("binding_id") or job["serialization_key"].split(":", 1)[1]
        )
        row = (
            (
                await session.execute(
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

    async def send_email(session, job):
        # Own-transactions, for `run_pipeline`'s reason above plus one of its
        # own: a provider call cannot run inside an open transaction (`02` §5).
        sent = await email_sender.execute_send_email(
            job, sender=deps.email, engine=deps.engine
        )
        if sent is None:
            # The executor rescheduled the job itself, so the loop's finalize
            # below will not match a leased row and will log a fence warning
            # ("another owner won") that is not true here. Said plainly on the
            # line above it, because that warning is what an operator reads to
            # answer "is the reaper racing my workers". The accounting itself
            # is the loop's to fix and is filed.
            logger.info(
                "send_email %s deferred; the fence warning that follows is the"
                " deferral, not a lost lease",
                job["id"],
            )

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
        return await offboarding.execute_offboard(deps, session, job)

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
        session_for: Callable[[dict], Any],
        lane: str,
        registry: dict,
        heartbeat,
        config: WorkerConfig,
        worker_name: str,
    ):
        self._claim_conn = claim_conn
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
        self.consecutive_errors = 0
        self._stop = asyncio.Event()

    def bind_claim_conn(self, conn) -> None:
        """The run-time half of construction: compose() wires everything that
        needs no connection; the entrypoint binds the live claim connection."""
        self._claim_conn = conn

    async def run_once(self) -> bool:
        """Claim and run at most one job. Returns True when one was claimed."""
        if self._claim_conn is None:
            raise RuntimeError("WorkLoop.run before bind_claim_conn")
        try:
            job = await jobs.claim_job(
                self._claim_conn,
                lane=self.lane,
                worker=self._worker_name,
                lease_seconds=self._config.lease_seconds,
                ws_lane_cap=self._config.ws_lane_cap,
            )
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
            async with self._session_for(job) as session:
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
                async with self._session_for(job) as session:
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
                await jobs.wait_or_stop(self._stop, self._config.claim_idle_seconds)

    def stop(self) -> None:
        self._stop.set()


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
            " run_at, max_attempts, payload)"
            " SELECT 'deliver_outbox', b.workspace_id, 'interactive',"
            "        'tg:' || b.id, now(), 3,"
            "        jsonb_build_object('v', 1, 'binding_id', b.id)"
            "   FROM channel_bindings b"
            "  WHERE b.state = 'active' AND b.channel LIKE 'telegram%'"
            "    AND EXISTS (SELECT 1 FROM channel_outbox o"
            "                 WHERE o.binding_id = b.id AND o.state = 'pending')"
            "    AND NOT EXISTS (SELECT 1 FROM jobs j"
            "                     WHERE j.serialization_key = 'tg:' || b.id"
            "                       AND j.state IN ('ready', 'leased'))"
        )
    )
    return result.rowcount
