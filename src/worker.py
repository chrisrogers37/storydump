"""The target worker — the composition root `04` §Deployment names (#942, #903).

`python -m src.worker` is the process the Procfile's `worker` entry swaps to at
the M.3 window; until then it runs only against a database branch. It builds
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
import logging
import os
import signal
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.services.target import jobs, scheduler, unit_of_work
from src.services.target.work_loop import (
    Parked,
    WorkerConfig,
    WorkerDeps,
    WorkLoop,
    build_registry,
    ensure_sender_jobs,
)

logger = logging.getLogger("target.worker")


def engine_url_from_env(env: dict):
    """The branch-soak/deploy door: TARGET_DATABASE_URL, made asyncpg-safe.

    Accepts the plain `postgresql://` URL a platform hands out and rewrites
    what the asyncpg driver refuses: the dialect suffix, and the libpq-only
    `sslmode`/`channel_binding` query params (Neon appends both; asyncpg
    takes `ssl=`). Returns None when unset so `unit_of_work.create_engine`
    falls back to the settings-built URL.
    """
    url = env.get("TARGET_DATABASE_URL")
    if not url:
        return None
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    sslmode = params.pop("sslmode", None)
    params.pop("channel_binding", None)
    if sslmode and "ssl" not in params:
        params["ssl"] = sslmode
    return urlunsplit(parts._replace(query=urlencode(params)))


def _transit_from_env(env):
    name = env.get("CLOUDINARY_CLOUD_NAME")
    key = env.get("CLOUDINARY_API_KEY")
    secret = env.get("CLOUDINARY_API_SECRET")
    if name and key and secret:
        from src.services.target.transit import TransitStore

        return TransitStore(cloud_name=name, api_key=key, api_secret=secret)
    return None


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


def compose(*, engine, config: WorkerConfig, env: dict, transport=None) -> WorkerApp:
    """Assemble the object graph. Touches no network — the transport arrives
    built (main() constructs it from TARGET_TELEGRAM_BOT_TOKEN) and its
    credential is probed by run(), not here."""
    deps = WorkerDeps(
        meta=None,
        transit=_transit_from_env(env),
        media_fetch=None,
        transport=transport,
        poll=None,
        engine=engine,
        config=config,
    )
    registry = build_registry(deps)
    live = {k for k, e in registry.items() if not isinstance(e, Parked)}
    recurring = {"v": 1, "reap_expired": 6 * 3600.0}
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
    logger.info("telegram channel live as @%s", username)


def status_line(*, loops, clock, heartbeat, transport=None) -> str:
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
        line += f" transport[auth_failures={transport.auth_failures}]"
    return line


async def _sender_job_sweeper(app: WorkerApp, stop: asyncio.Event) -> None:
    """Mint sender jobs on cadence while the channel is live. Never mints for
    a parked channel — that would manufacture parked work on a timer."""
    maker = async_sessionmaker(app.engine, expire_on_commit=False)
    while not stop.is_set():
        if not isinstance(app.registry.get("deliver_outbox"), Parked):
            try:
                async with maker() as session:
                    async with session.begin():
                        await unit_of_work.apply_gucs(
                            session, tenant_id="", actor_kind="system"
                        )
                        await ensure_sender_jobs(session)
            except Exception:  # noqa: BLE001 — the sweep must outlive a blip
                logger.exception("sender-job sweep failed; retrying on cadence")
        try:
            async with asyncio.timeout(app.config.sender_sweep_seconds):
                await stop.wait()
        except TimeoutError:
            pass


async def _status_reporter(app: WorkerApp, stop: asyncio.Event, every: float) -> None:
    while not stop.is_set():
        try:
            async with asyncio.timeout(every):
                await stop.wait()
        except TimeoutError:
            logger.info(
                "status: %s",
                status_line(
                    loops=app.loops,
                    clock=app.clock,
                    heartbeat=app.heartbeat,
                    transport=app.deps.transport,
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

    election_conn = await engine.connect()
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
    sweep_task = asyncio.create_task(
        _sender_job_sweeper(app, stop), name="sender-job-sweeper"
    )
    try:
        await stop.wait()
    finally:
        status_task.cancel()
        sweep_task.cancel()
        await asyncio.gather(status_task, sweep_task, return_exceptions=True)
        for wl in app.loops:
            wl.stop()
        await asyncio.gather(*loop_tasks, return_exceptions=True)
        await clock.stop()
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
            "worker stopped cleanly; final %s",
            status_line(loops=app.loops, clock=app.clock, heartbeat=app.heartbeat),
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = WorkerConfig()
    env = dict(os.environ)
    engine = unit_of_work.create_engine(engine_url_from_env(env))
    transport = None
    token = env.get("TARGET_TELEGRAM_BOT_TOKEN")
    if token:
        from src.channels.telegram_transport import TelegramTransport

        transport = TelegramTransport(token)
    app = compose(engine=engine, config=config, env=env, transport=transport)
    asyncio.run(run(app))


if __name__ == "__main__":
    main()
