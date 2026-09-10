"""The target worker — the composition root `04` §Deployment names (#942, #903).

`python -m src.worker` is the process the deployed `worker` entrypoint runs
when armed: `src.main` dispatches here on `WORKER_IMPL=target` (#942), so the
M.3 window flips a service variable, never the Procfile. Until armed it runs
only against a database branch. It builds
the engine, the per-lane claim connections, the clock election, the lease
heartbeat and the injected seams, and dispatches over the `work_loop` registry.

Composition is split from connection on purpose: :func:`compose` assembles the
whole object graph without touching the network, so the graph's properties —
which kinds are live, that the clock's recurring set stays inside them, that
the lease numbers agree — are unit-testable facts rather than deploy-time
surprises. :func:`run` binds connections and supervises.

Seam posture for the W1 slice (build-path `2026-08-21`): no transport (W2), no
media_fetch (W5b), no provider poll (W5a) — their kinds PARK loudly rather
than run against fakes. The transit store goes live iff `CLOUDINARY_*` is
configured. `fn_clock_tick`'s account/credential/source legs are the door's
own; this process only chooses the recurring singletons it can actually run.
"""

from __future__ import annotations

import asyncio
from typing import Optional
import logging

from src.config.settings import settings
import os
import signal
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.channels.telegram_webhook_registration import bot_matches
from src.services.target import (
    credential_lifecycle,
    drive_credentials,
    email_sender,
    google_drive_adapter,
    jobs,
    scheduler,
    unit_of_work,
)
from src.services.target import health as health_endpoint
from src.services.target import prompts as prompts_mod
from src.services.target.work_loop import (
    Parked,
    WorkerConfig,
    WorkerDeps,
    WorkLoop,
    build_registry,
    ensure_sender_jobs,
)

logger = logging.getLogger("target.worker")


class WorkerTaskDied(RuntimeError):
    """A supervised background task exited before stop was requested."""


def _transit_from_env(env):
    name = env.get("CLOUDINARY_CLOUD_NAME")
    key = env.get("CLOUDINARY_API_KEY")
    secret = env.get("CLOUDINARY_API_SECRET")
    if name and key and secret:
        from src.services.target.transit import TransitStore

        return TransitStore(cloud_name=name, api_key=key, api_secret=secret)
    return None


#: What a story may weigh on the way to Meta (Meta's own limits: 8 MB for a
#: story image, 100 MB for a story video pulled by URL). Distinct from the
#: Telegram card's caps (`MEDIA_CARD_MAX_BYTES`): a file too large for a
#: Telegram preview may still be a fine story.
PUBLISH_MAX_BYTES = {"image": 8 * 1024 * 1024, "video": 100 * 1024 * 1024}


def _publish_media_fetch(drive):
    """`publish_pipeline`'s ``media_fetch``: the intent row → the file's bytes,
    read from Drive under the workspace's grant (#1220 step 3). The row
    carries `source_id`, `workspace_id`, `provider_file_ref` and `media_kind`
    since the pipeline's `_load` gained them."""

    async def fetch(intent: dict) -> bytes:
        kind = str(intent.get("media_kind") or "image")
        content, _name, _mime = await drive.fetch_bytes(
            source_id=str(intent["source_id"]),
            workspace_id=str(intent["workspace_id"]),
            file_ref=str(intent["provider_file_ref"]),
            max_bytes=PUBLISH_MAX_BYTES.get(kind, PUBLISH_MAX_BYTES["image"]),
        )
        return content

    return fetch


def _meta_from_env(engine, env):
    """The real Instagram Graph adapter, always constructible: it needs no
    secret of its own — each call reads the account's token from
    `oauth_credentials` through `ig_credentials.token_for_account`.
    `META_GRAPH_VERSION` overrides the Graph version."""
    from src.services.target import ig_credentials
    from src.services.target.instagram_graph import (
        DEFAULT_GRAPH_VERSION,
        InstagramGraphAdapter,
    )

    async def token_for_account(ref: str, *, workspace_id=None) -> str:
        return await ig_credentials.token_for_account(
            engine, ref, workspace_id=workspace_id
        )

    return InstagramGraphAdapter(
        token_for_account=token_for_account,
        version=env.get("META_GRAPH_VERSION") or DEFAULT_GRAPH_VERSION,
    )


def _poll_from(engine, meta, *, session_factory=None):
    """`reconcile_ambiguous`'s ladder half (`06` §5): the container's status
    for an ambiguous intent, asked of Meta (#1220 step 3).

    Returns the `status_code` the reconciler classifies (PUBLISHED → posted,
    ERROR/EXPIRED → failed, anything else inconclusive), or None — inconclusive
    under every mode — when the intent has no container or Meta's answer is a
    typed error: the ladder records the sighting and, spent, parks the intent
    for a human. A typed error is logged by type and returned as None rather
    than raised so one account's dead token cannot abort the whole sweep.
    Reads as the owner role (BYPASSRLS, #751) like `ig_credentials`."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.services.target.meta_adapter import MetaError, MetaLostResponse

    maker = session_factory or async_sessionmaker(engine, expire_on_commit=False)

    async def poll(*, intent_id) -> Optional[str]:
        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT i.ig_container_id, a.provider_account_ref,"
                            "       i.workspace_id"
                            "  FROM post_intents i"
                            "  JOIN ig_accounts a ON a.id = i.ig_account_id"
                            " WHERE i.id = :id"
                        ),
                        {"id": str(intent_id)},
                    )
                )
                .mappings()
                .first()
            )
        if row is None or not row["ig_container_id"]:
            return None
        try:
            return await meta.container_status(
                str(row["ig_container_id"]),
                provider_account_ref=row["provider_account_ref"],
                workspace_id=str(row["workspace_id"]),
            )
        except (MetaError, MetaLostResponse) as exc:
            logger.warning(
                "reconcile poll for intent %s: %s — recorded as inconclusive",
                intent_id,
                type(exc).__name__,
            )
            return None

    return poll


def make_session_for(engine):
    """Per-job transaction contexts with the GUC invariant applied once.

    Tenant scope comes from the claimed row (system singletons carry none and
    get an empty tenant id — fail-closed under any tenant policy); the actor
    is `system`, the `02` §4 worker actor.
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


@dataclass
class WorkerApp:
    """The composed, not-yet-connected worker."""

    registry: dict
    loops: list
    recurring: dict
    heartbeat: jobs.LeaseHeartbeat
    heartbeat_lease_seconds: float
    heartbeat_interval_seconds: float
    config: WorkerConfig
    deps: WorkerDeps
    engine: object
    clock: object = None  # set by run() so a supervisor can read its observables
    sweeper: object = None  # set by run(); carries sweeps/mints observables
    prompt_sweeper: object = None  # set by run(); sweeps/prompted/advanced
    #: The bot this worker is CONFIGURED to speak as (`TARGET_TELEGRAM_BOT_USERNAME`,
    #: the API's webhook bot) and the bot its token actually is (from the
    #: startup probe). A mismatch means every card's buttons belong to a bot
    #: nobody listens on — the 2026-09-10 crosswire — so it parks the sender.
    expected_bot: object = None
    bot_username: object = None


def compose(
    *, engine, config: WorkerConfig, env: dict, transport=None, refresh=None, drive=None
) -> WorkerApp:
    """Assemble the object graph. Touches no network — the transport arrives
    built (main() constructs it from TARGET_TELEGRAM_BOT_TOKEN) and its
    credential is probed by run(), not here. *refresh* defaults to the real
    IG refresh door — always constructible (no config), egress-floored; tests
    inject a scripted one."""
    # The publish leg (#1220 step 3): the Graph adapter is always built (it
    # reads each account's token at call time); the media fetch exists only
    # with a Drive adapter; the registry parks the kind naming whichever of
    # the three (fetch, meta, transit) is missing. The same adapter answers the
    # ambiguous-publish reconciler's poll.
    meta = _meta_from_env(engine, env)
    deps = WorkerDeps(
        meta=meta,
        transit=_transit_from_env(env),
        media_fetch=None if drive is None else _publish_media_fetch(drive),
        # None until a provider is wired, and that is the honest state rather
        # than a stub: `07` §1's owner ack on adding Resend is OPEN, so the
        # kind parks with a reason naming what is missing (#1092).
        email=email_sender.sender_from_env(env),
        transport=transport,
        poll=_poll_from(engine, meta),
        refresh=refresh if refresh is not None else credential_lifecycle.ig_refresh,
        drive=drive,
        engine=engine,
        config=config,
    )
    registry = build_registry(deps)
    live = {k for k, e in registry.items() if not isinstance(e, Parked)}
    recurring = {
        "v": 1,
        "reap_expired": 6 * 3600.0,
        # #1061: a source stranded in `error` is never re-scheduled, so the
        # branch that alerts never runs again. This beat is the only thing
        # that re-opens its mouth. Cadence is the clock's; the per-source
        # bound is `cfg.stranded_alert_after_seconds`.
        "alert_stranded_sources": 6 * 3600.0,
        # 05: "Reconciler cadence + budget | sweep every 60 s, LIMIT 50".
        # Nothing minted this kind before, so the door shipped in 059 was never
        # walked and #1090 D4's customer notification had no beat to ride. The
        # kind is now live regardless of the poll seam (its notify half needs
        # none), so it satisfies the `recurring <= live` assert below.
        "reconcile_ambiguous": 60.0,
    }
    if "reap_transit_assets" in live:
        recurring["reap_transit_assets"] = 6 * 3600.0
    assert set(recurring) - {"v"} <= live, "recurring kinds must be runnable here"

    heartbeat = jobs.LeaseHeartbeat(
        connect=lambda: engine.connect(),
        interval_seconds=config.heartbeat_interval_seconds,
        lease_seconds=config.lease_seconds,
    )
    session_for = make_session_for(engine)
    name = f"{socket.gethostname()}-{os.getpid()}"
    loops = [
        WorkLoop(
            session_for=session_for,
            lane=lane,
            registry=registry,
            heartbeat=heartbeat,
            config=config,
            worker_name=f"{name}-{lane}",
        )
        for lane in ("interactive", "bulk")
    ]
    return WorkerApp(
        registry=registry,
        loops=loops,
        recurring=recurring,
        expected_bot=(env.get("TARGET_TELEGRAM_BOT_USERNAME") or "").lstrip("@")
        or None,
        heartbeat=heartbeat,
        heartbeat_lease_seconds=config.lease_seconds,
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        config=config,
        deps=deps,
        engine=engine,
    )


DEAD_CREDENTIAL_REASON = (
    "credential rejected by Telegram at startup (getMe 401/403) — DEAD TOKEN;"
    " the channel is down until it is replaced, and every park of this job is"
    " this reminder"
)


WRONG_BOT_REASON = (
    "WRONG BOT: the token is @{actual}, the configured bot is @{expected} — cards"
    " from it carry buttons nobody listens on; fix TARGET_TELEGRAM_BOT_TOKEN"
)


async def apply_transport_probe(app: WorkerApp) -> None:
    """The composition-time credential check — the shitpost-alpha lesson.

    A live probe logs the bot identity once. A dead one parks the channel IN
    THE SHARED REGISTRY with the dead-token reason (the loops hold the same
    dict, so every sender claim from here on warns with it) and logs at ERROR.
    A transport that would send nothing must never be indistinguishable from
    one with nothing to send.
    """
    transport = app.deps.transport
    if transport is None:
        return
    from src.channels.telegram_transport import TelegramAuthDead

    try:
        username = await transport.probe()
    except TelegramAuthDead as exc:
        logger.error(
            "Telegram credential is DEAD at startup (%s) — parking the"
            " deliver_outbox channel; the worker runs without it",
            exc,
        )
        app.registry["deliver_outbox"] = Parked(DEAD_CREDENTIAL_REASON)
        return
    app.bot_username = username
    if app.expected_bot and not bot_matches(username, app.expected_bot):
        # The 2026-09-10 crosswire: the worker sent cards as the old bot while
        # the API's webhook listened on the new one, so every tap went where
        # nothing listened. A card from the wrong bot is a dead card — refuse
        # to send any, loudly, rather than mint buttons that cannot answer.
        logger.error(
            "Telegram token belongs to @%s but the configured bot is @%s —"
            " parking the deliver_outbox channel; set the worker's"
            " TARGET_TELEGRAM_BOT_TOKEN to the API's bot",
            username,
            app.expected_bot,
        )
        app.registry["deliver_outbox"] = Parked(
            WRONG_BOT_REASON.format(actual=username, expected=app.expected_bot)
        )
        return
    logger.info("telegram channel live as @%s", username)


def status_line(
    *,
    loops,
    clock,
    heartbeat,
    transport=None,
    sweeper=None,
    prompt_sweeper=None,
    bot_username=None,
) -> str:
    """One human-readable line from the observables — the soak's visibility."""
    lanes = " ".join(
        f"{wl.lane}[processed={wl.processed} parked={wl.parked}"
        f" failures={wl.failures} fenced={wl.fenced}]"
        for wl in loops
    )
    clock_part = (
        f"clock[elected={clock.elected} ticks={clock.ticks}"
        f" inserts={clock.inserts} errs={clock.consecutive_failures}]"
        if clock is not None
        else "clock[unstarted]"
    )
    hb_part = (
        f"heartbeat[beats={heartbeat.beats} short={heartbeat.short_beats}"
        f" errs={heartbeat.consecutive_failures}]"
    )
    line = f"{lanes} {clock_part} {hb_part}"
    if transport is not None:
        line += (
            f" transport[bot=@{bot_username or '?'} auth_failures={transport.auth_failures},"
            f" media_fetch_failures={getattr(transport, 'media_fetch_failures', 0)}]"
        )
    if sweeper is not None:
        line += f" sweeper[sweeps={sweeper.sweeps} mints={sweeper.mints}]"
    if prompt_sweeper is not None:
        line += (
            f" prompts[sweeps={prompt_sweeper.sweeps}"
            f" prompted={prompt_sweeper.prompted}"
            f" advanced={prompt_sweeper.advanced}]"
        )
    return line


class SenderSweeper:
    """The sender-job mint sweep, with the observables its silent death cost
    us (#958 review, navi): `sweeps` counts iterations, `mints` counts jobs
    minted — both ride the status line, so a sweeper that stops sweeping is
    visible as a counter that stops moving, and a sweeper that DIES is caught
    by run()'s task supervision."""

    def __init__(self, app: "WorkerApp"):
        self._app = app
        self.sweeps = 0
        self.mints = 0

    async def run(self, stop: asyncio.Event) -> None:
        maker = async_sessionmaker(self._app.engine, expire_on_commit=False)
        while not stop.is_set():
            self.sweeps += 1
            if not isinstance(self._app.registry.get("deliver_outbox"), Parked):
                try:
                    async with maker() as session:
                        async with session.begin():
                            await unit_of_work.apply_gucs(
                                session, tenant_id="", actor_kind="system"
                            )
                            self.mints += await ensure_sender_jobs(session)
                except Exception:  # noqa: BLE001 — outlive a blip, loudly
                    logger.exception("sender-job sweep failed; retrying on cadence")
            await jobs.wait_or_stop(stop, self._app.config.sender_sweep_seconds)


class PromptSweeper:
    """The W3 prompt sweep on its own cadence: due intents gain their cards
    and advance to `awaiting_approval` in the same pass — the web queue is a
    surface every workspace has (#1033), so delivery is not what gates the
    edge. Counters ride the status line — the same absence-is-failure
    control the sender sweeper carries. Runs regardless of channel state: cards enqueued while
    the transport is parked simply wait as `pending` rows."""

    def __init__(self, app: "WorkerApp"):
        self._app = app
        self.sweeps = 0
        self.prompted = 0
        self.advanced = 0

    async def run(self, stop: asyncio.Event) -> None:
        maker = async_sessionmaker(self._app.engine, expire_on_commit=False)
        while not stop.is_set():
            self.sweeps += 1
            try:
                async with maker() as session:
                    async with session.begin():
                        await unit_of_work.apply_gucs(
                            session, tenant_id="", actor_kind="system"
                        )
                        counts = await prompts_mod.sweep_due_prompts(session, limit=50)
                self.prompted += counts["prompted"]
                self.advanced += counts["advanced"]
            except Exception:  # noqa: BLE001 — outlive a blip, loudly
                logger.exception("prompt sweep failed; retrying on cadence")
            await jobs.wait_or_stop(stop, self._app.config.prompt_sweep_seconds)


async def supervise(stop: asyncio.Event, tasks) -> asyncio.Task | None:
    """Wait until stop is requested OR any supervised task exits.

    Returns the dead task: one that raised (regardless of stop), or one that
    exited cleanly before stop was requested. None on legitimate shutdown —
    a clean or cancelled exit after stop fired. The caller owns
    what loud means; this only guarantees a death can never pass unnoticed —
    the class finding from #958: two background tasks could die while the
    worker kept printing healthy status lines.
    """
    stop_waiter = asyncio.create_task(stop.wait(), name="stop-waiter")
    try:
        done, _ = await asyncio.wait(
            {stop_waiter, *tasks}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        if not stop_waiter.done():
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)
    # Death is a PER-TASK verdict, never a global-flag one (navi, #958 cycle
    # 2 — a blanket stop.is_set() exclusion masked a real crash that raced an
    # external stop). A task is dead when it RAISED (always, stop or no stop),
    # or when it returned cleanly while stop was unset (every supervised body
    # is a while-not-stop loop, so a clean early return is only reachable
    # through a bug). A clean exit after stop fired is legitimate shutdown —
    # the soak's own status reporter, measured — and cancellation is shutdown
    # machinery, not death.
    dead = [
        t
        for t in done
        if t is not stop_waiter
        and not t.cancelled()
        and (t.exception() is not None or not stop.is_set())
    ]
    return dead[0] if dead else None


async def _status_reporter(app: WorkerApp, stop: asyncio.Event, every: float) -> None:
    while not stop.is_set():
        if not await jobs.wait_or_stop(stop, every):
            logger.info(
                "status: %s",
                status_line(
                    loops=app.loops,
                    clock=app.clock,
                    heartbeat=app.heartbeat,
                    transport=app.deps.transport,
                    sweeper=app.sweeper,
                    prompt_sweeper=app.prompt_sweeper,
                    bot_username=app.bot_username,
                ),
            )


async def run(app: WorkerApp, *, stop: asyncio.Event | None = None) -> None:
    """Bind connections, start the clock/heartbeat/loops, run until stopped.

    *stop* is the test seam: signals set the same event, so a harness can end
    a soak the way SIGTERM would without owning a process.
    """
    engine = app.engine
    cfg = app.config
    stop = stop or asyncio.Event()

    def _request_stop(signame: str):
        logger.info("received %s — stopping", signame)
        stop.set()

    loop_ = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop_.add_signal_handler(sig, _request_stop, sig.name)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    # BEFORE the first database connection, deliberately. Railway times the
    # SOCKET, not the program: `main.py` records the previous occurrence of this
    # exact trap, where slow startup steps ran ahead of the listener and healthy
    # deploys were marked FAILED. Binding here means the endpoint answers for the
    # whole of startup however slow the connections and the election turn out to
    # be, which is also why this root needs no startup-grace constant.
    health_server = app.health_server = await health_endpoint.serve_health(app)

    election_conn = await engine.connect()
    # Who the worker connects as, and whether that login bypasses RLS (#751,
    # F.4): the runtime posture is verified from this line after a deploy.
    role = await unit_of_work.connection_role(election_conn)
    logger.info("worker database role: %s", role if role is not None else "unknown")
    claim_conns = [await engine.connect() for _ in app.loops]
    for wl, conn in zip(app.loops, claim_conns):
        wl.bind_claim_conn(conn)

    clock = app.clock = scheduler.Clock(
        election_conn,
        async_sessionmaker(engine, expire_on_commit=False),
        interval_seconds=cfg.clock_interval_seconds,
        max_inserts=cfg.clock_max_inserts,
        refresh_cadence_seconds=cfg.refresh_cadence_seconds,
        recurring=app.recurring,
    )

    hb_task = asyncio.create_task(app.heartbeat.run(), name="lease-heartbeat")
    await clock.start()
    loop_tasks = [
        asyncio.create_task(wl.run(), name=f"lane-{wl.lane}") for wl in app.loops
    ]
    logger.info(
        "worker up: lanes=%s live_kinds=%s recurring=%s",
        [wl.lane for wl in app.loops],
        sorted(k for k, e in app.registry.items() if not hasattr(e, "reason")),
        sorted(set(app.recurring) - {"v"}),
    )
    await apply_transport_probe(app)
    status_task = asyncio.create_task(
        _status_reporter(app, stop, 60.0), name="status-reporter"
    )
    app.sweeper = SenderSweeper(app)
    sweep_task = asyncio.create_task(app.sweeper.run(stop), name="sender-job-sweeper")
    app.prompt_sweeper = PromptSweeper(app)
    prompt_task = asyncio.create_task(
        app.prompt_sweeper.run(stop), name="prompt-sweeper"
    )
    supervised = [
        t
        for t in (
            hb_task,
            status_task,
            sweep_task,
            prompt_task,
            getattr(app.clock, "_task", None),
        )
        if t is not None
    ] + loop_tasks
    died = None
    try:
        died = await supervise(stop, supervised)
        if died is not None:
            exc = died.exception() if not died.cancelled() else None
            logger.error(
                "background task %s DIED (%r) before stop was requested —"
                " stopping the worker LOUDLY; a limping worker is the silent"
                " failure this supervision exists to prevent",
                died.get_name(),
                exc,
            )
            stop.set()
    finally:
        status_task.cancel()
        sweep_task.cancel()
        prompt_task.cancel()
        for result in await asyncio.gather(
            status_task, sweep_task, prompt_task, return_exceptions=True
        ):
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error("task raised during shutdown: %r", result)
        for wl in app.loops:
            wl.stop()
        for result in await asyncio.gather(*loop_tasks, return_exceptions=True):
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error("lane raised during shutdown: %r", result)
        await clock.stop()
        health_server.close()
        await health_server.wait_closed()
        await app.heartbeat.stop()
        hb_task.cancel()
        await asyncio.gather(hb_task, return_exceptions=True)
        for conn in claim_conns:
            await conn.close()
        await election_conn.close()
        if app.deps.transport is not None:
            await app.deps.transport.aclose()
        await engine.dispose()
        logger.info(
            "worker stopped %s; final %s",
            "after a task death" if died is not None else "cleanly",
            status_line(
                loops=app.loops,
                clock=app.clock,
                heartbeat=app.heartbeat,
                transport=app.deps.transport,
                sweeper=app.sweeper,
                prompt_sweeper=app.prompt_sweeper,
            ),
        )
        if died is not None:
            raise WorkerTaskDied(f"background task {died.get_name()} died") from (
                died.exception() if not died.cancelled() else None
            )


def _card_media_fetch(drive):
    """`TelegramTransport.media_fetch`: a card's media block → (bytes, name,
    mime) under the workspace grant, capped at Telegram's upload limit for
    the kind so an oversize file is refused from metadata, never downloaded."""

    from src.channels.telegram_transport import MediaTransient, MediaUnavailable
    from src.services.target.drive_adapter import (
        DriveLostResponse,
        DriveRetryableError,
        DriveTerminalError,
    )

    async def fetch(media: dict) -> tuple[bytes, str, Optional[str]]:
        cap = google_drive_adapter.MEDIA_CARD_MAX_BYTES.get(
            str(media.get("kind")), google_drive_adapter.MEDIA_CARD_MAX_BYTES["image"]
        )
        try:
            return await drive.fetch_bytes(
                source_id=str(media["source_id"]),
                workspace_id=str(media["workspace_id"]),
                file_ref=str(media["ref"]),
                max_bytes=cap,
            )
        except DriveTerminalError as exc:
            # The FILE's fault, for good (too large, gone, refused): the text
            # card, at WARNING.
            raise MediaUnavailable(str(exc)) from exc
        except (DriveRetryableError, DriveLostResponse) as exc:
            # A blip: nothing reached Telegram, so the send propagates as
            # ambiguous and is resent with the photo intact. A dead grant
            # (DriveCredentialDead) is in neither list on purpose — it is
            # loud, and counted.
            raise MediaTransient(str(exc)) from exc

    return fetch


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # `05` numbers come from the dataclass defaults; the web origin is
    # deployment config, so it is read from settings at the composition root
    # rather than duplicated as a worker env var.
    from src.config.settings import settings as _settings

    config = WorkerConfig(web_app_origin=_settings.web_app_origin)
    env = dict(os.environ)
    engine = unit_of_work.create_engine(unit_of_work.engine_url_from_env(env))
    transport = None
    token = env.get("TARGET_TELEGRAM_BOT_TOKEN")
    # The Drive read leg (#982). Armed unconditionally: it needs no env of its
    # own (the engine carries the credential lookup, the token is the workspace's grant since 069),
    # so an env gate here would be a switch with nothing to switch on.
    #
    # Safe to arm before a `gdrive` credential writer exists. A source with no
    # credential raises DriveCredentialDead, which `media_sync` classifies
    # persistent — the source flips to `error`, its disconnect alert fires once,
    # and THE JOB SUCCEEDS. So the failure mode is a visible source in `error`,
    # not a poisoned lane.
    #
    # `media_fetch` IS derived from this since #1220 step 3 (`compose`), and
    # the registry gates `publish_pipeline` on media_fetch AND meta AND transit
    # — the triple astrid named on #982 — so a missing Cloudinary trio parks
    # the kind by name rather than crashing the ladder.
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        # Said at startup, once, where an operator looks: without the client
        # the hourly refresh (P5, #1247) cannot run here, and every folder's
        # sync will refuse — retryably, in the log, never to a tenant — an
        # hour after the workspace connects.
        logger.warning(
            "Drive read leg armed without GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET:"
            " this worker cannot refresh a Drive grant; set both on this service"
        )
    drive = google_drive_adapter.GoogleDriveAdapter(
        token_provider=drive_credentials.provider_from_engine(engine),
    )
    if token:
        from src.channels.telegram_transport import transport_from_env

        # The approval card is the photo (owner, 2026-09-08): the transport
        # fetches a card's media through the same Drive read leg. This is NOT
        # the publish pipeline's `media_fetch` seam (`_publish_media_fetch`,
        # composed above), which reads the file for Meta at its own cap.
        transport = transport_from_env(token, env, media_fetch=_card_media_fetch(drive))
    app = compose(
        engine=engine, config=config, env=env, transport=transport, drive=drive
    )
    try:
        asyncio.run(run(app))
    except WorkerTaskDied:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
