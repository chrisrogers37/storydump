"""The target worker — the composition root.

`python -m src.worker` is the process the deployed `worker` entrypoint runs:
`src.main` dispatches here unconditionally. It builds the engine, the per-lane
claim connections, the clock election, the lease heartbeat and the injected
seams, and dispatches over the `work_loop` registry.

Composition is split from connection on purpose: :func:`compose` assembles the
whole object graph without touching the network, so the graph's properties —
which kinds are live, that the clock's recurring set stays inside them, that
the lease numbers agree — are unit-testable facts rather than deploy-time
surprises. :func:`run` binds connections and supervises.

Seam posture: a seam this process cannot build PARKS its kinds loudly rather
than running them against a fake — no `TARGET_TELEGRAM_BOT_TOKEN` parks the
sender, no `CLOUDINARY_*` trio parks the publish kind (the transit store goes
live iff all three are set), a dead or wrong-bot token parks the channel at the
startup probe. `fn_clock_tick`'s account/credential/source legs are the door's
own; this process only chooses the recurring singletons it can actually run.

The deployed connection is `svc_worker`. Tenant work sets `app.tenant_id`;
SECURITY DEFINER functions provide bounded system reads before a tenant is
known. The owner login is reserved for schema changes.

Configuration the worker cannot run on at all refuses to BOOT instead
(:func:`main`): no database URL, a credential key ring that cannot load, a lane
concurrency the pool cannot hold. Parking the ring's kinds would be possible and
worse — a parked worker passes the health check and replaces a working deploy
with one that cannot read a single credential; refusing keeps the old one.
"""

from __future__ import annotations

import asyncio
from typing import Optional
import logging

from src.config.settings import settings
import os
import signal
import socket
import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.channels import telegram_webhook_registration as reg
from src.channels.telegram_webhook_registration import bot_matches
from src.services.target import (
    past_posts_activation,
    past_posts_composition,
    credential_lifecycle,
    drive_credentials,
    email_sender,
    google_drive_adapter,
    media_adapters,
    jobs,
    loop_watchdog,
    oauth_states,
    scheduler,
    unit_of_work,
    worker_health,
)
from src.services.target import backpressure as _backpressure
from src.services.target import prompts as prompts_mod
from src.services.target.vocabulary import PROVIDER_GDRIVE, PUBLISH_MAX_BYTES
from src.services.target.work_loop import (
    assert_concurrency_fits,
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


#: The default-off flag for the advisory Meta usage pre-check. Truthy spellings
#: only; anything else leaves the seam None
#: and no usage read is ever made.
USAGE_PRECHECK_ENV = "TARGET_USAGE_PRECHECK_ENABLED"
_TRUTHY = ("1", "true", "yes", "on")


#: The activation nudge's switch, default off like the pre-check above.
#: It is one of three gates: `build_registry` also wants an email provider and
#: a web origin, and parks the sweep naming whichever is missing.
ACTIVATION_NUDGE_ENV = "TARGET_ACTIVATION_NUDGE_ENABLED"

#: The nudge's daily cap when set; `WorkerConfig.activation_nudge_limit`, a
#: small first-run value, when not. Refused by name as the lane sizes
#: are.
ACTIVATION_NUDGE_LIMIT_ENV = "TARGET_ACTIVATION_NUDGE_LIMIT"


def _env_flag(env, name: str) -> bool:
    """A default-off switch: on for a truthy spelling, off for anything else."""
    return str(env.get(name) or "").strip().lower() in _TRUTHY


def _precheck_from_env(env):
    if not _env_flag(env, USAGE_PRECHECK_ENV):
        return None
    from src.services.target.usage_precheck import DEFAULT_TTL_SECONDS, UsagePrecheck

    # Said at compose time: the `worker up` line is identical either way,
    # and a canary that is armed must be visible in the deploy log.
    logger.info("usage pre-check armed (ttl=%ds)", int(DEFAULT_TTL_SECONDS))
    return UsagePrecheck()


def _transit_from_env(env):
    name = env.get("CLOUDINARY_CLOUD_NAME")
    key = env.get("CLOUDINARY_API_KEY")
    secret = env.get("CLOUDINARY_API_SECRET")
    if name and key and secret:
        from src.services.target.transit import TransitStore

        return TransitStore(cloud_name=name, api_key=key, api_secret=secret)
    return None


def _conversion_evidence(env):
    """This worker's release's source-mix conversion run
    (`mix_conversion.release_evidence`). `main` reads it once and hands it to
    `compose`, which reads it itself when it is handed none."""
    from src.services.target import mix_conversion

    return mix_conversion.release_evidence(env.get(past_posts_composition.RELEASE_VAR))


def _publish_media_fetch(adapters: media_adapters.MediaAdapters):
    """`publish_pipeline`'s ``media_fetch``: the intent row → the file's bytes,
    read through its source's provider. The row
    carries `source_id`, `workspace_id`, `provider_file_ref` and `media_kind`
    from the pipeline's `_load`."""

    async def fetch(intent: dict) -> bytes:
        kind = str(intent.get("media_kind") or "image")
        state, provider = intent.get("source_state"), intent.get("provider")
        # A missing source is gone for good. A Drive folder that is not active
        # still reaches its credential door: a
        # disconnected folder's approved story retries and parks for review
        # rather than failing. Any other provider is refused here
        # while its source is inactive.
        if state is None or (state != "active" and provider != "gdrive"):
            raise media_adapters.MediaTerminalError(
                "media source is missing or inactive"
            )
        try:
            adapter = media_adapters.get_adapter(adapters, intent["provider"])
        except media_adapters.ProviderNotWired:
            raise media_adapters.MediaTerminalError(
                "media provider is not wired"
            ) from None
        try:
            content, _name, _mime = await adapter.fetch_bytes(
                intent["source_config"],
                str(intent["provider_file_ref"]),
                source_id=str(intent["source_id"]),
                workspace_id=str(intent["workspace_id"]),
                max_bytes=PUBLISH_MAX_BYTES.get(kind, PUBLISH_MAX_BYTES["image"]),
            )
        except media_adapters.MEDIA_FAILURES as exc:
            raise media_adapters.redacted_error(exc) from None
        return content

    return fetch


def _meta_from_env(engine, env):
    """The real Instagram Graph adapter, always constructible: it needs no
    secret of its own — each call reads the account's token from
    `oauth_credentials` through `ig_credentials.token_for_account`.
    `META_GRAPH_VERSION` overrides the Graph version."""
    from src.services.target import ig_credentials
    from src.services.target.instagram_graph import InstagramGraphAdapter, graph_version

    async def token_for_account(ref: str, *, workspace_id=None) -> str:
        return await ig_credentials.token_for_account(
            engine, ref, workspace_id=workspace_id
        )

    return InstagramGraphAdapter(
        token_for_account=token_for_account, version=graph_version(env)
    )


def _poll_from(engine, meta, *, session_factory=None):
    """`reconcile_ambiguous`'s ladder half: the container's status
    for an ambiguous intent, asked of Meta.

    Returns the `status_code` the reconciler classifies (PUBLISHED → posted,
    ERROR/EXPIRED → failed, anything else inconclusive), or None — inconclusive
    under every mode — when the intent has no container or Meta's answer is a
    typed error: the ladder records the sighting and, spent, parks the intent
    for a human. A typed error is logged by type and returned as None rather
    than raised so one account's dead token cannot abort the whole sweep.
    Reads under the workspace the reconciler's sweep row names, claimed in the
    poll's own session (worker sweep functions): a tenant-less read would see nothing once the
    worker runs as `svc_worker`."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.services.target.meta_adapter import MetaError, MetaLostResponse

    maker = session_factory or async_sessionmaker(engine, expire_on_commit=False)

    async def poll(*, intent_id, workspace_id) -> Optional[str]:
        async with maker() as session:
            # The reconciler's sweep row names the workspace; the read runs
            # under it (worker sweep functions) — a fresh session with no tenant sees nothing
            # under the policies once the worker runs as svc_worker.
            await unit_of_work.apply_gucs(
                session, tenant_id=str(workspace_id), actor_kind="system"
            )
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
    #: Set by run(): the raw-socket liveness listener (`worker_health`), bound
    #: before the first connection and closed in the finally. Declared here
    #: with its three siblings rather than appearing out of nowhere as an
    #: attribute assignment, which would make each reader use
    #: `getattr(..., None)`.
    mix_conversion_evidence: object = None
    source_mix_capability: dict = None
    health_server: object = None
    #: Set by run(): the event-loop watchdog (`loop_watchdog`), armed for the
    #: whole of the run.
    watchdog: object = None
    #: The bot this worker is CONFIGURED to speak as (`TARGET_TELEGRAM_BOT_USERNAME`,
    #: the API's webhook bot) and the bot its token actually is (from the
    #: startup probe). A mismatch means every card's buttons belong to a bot
    #: nobody listens on, so it parks the sender.
    expected_bot: object = None
    bot_username: object = None


#: Env names for K, the claim-and-run tasks per lane; the defaults are the
#: dataclass's. A non-integer or a value below 1 is refused by name.
LANE_CONCURRENCY_ENV = {
    "interactive": "TARGET_WORKER_INTERACTIVE_CONCURRENCY",
    "bulk": "TARGET_WORKER_BULK_CONCURRENCY",
}


def _env_count(env: dict, name: str) -> Optional[int]:
    """A count from *env*, or None when it is unset. A non-integer or a value
    below 1 is refused by name."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def lane_concurrency_from_env(env: dict) -> dict:
    out = dict(WorkerConfig().lane_concurrency)
    for lane, name in LANE_CONCURRENCY_ENV.items():
        value = _env_count(env, name)
        if value is not None:
            out[lane] = value
    return out


def worker_config_from_env(env: dict, *, web_app_origin: Optional[str]) -> WorkerConfig:
    """The config `main` runs with. The dataclass carries the default
    numbers and the deployment's own come from *env*. The web origin is
    deployment config read from settings at the composition root, rather
    than duplicated as a worker env var."""
    return WorkerConfig(
        web_app_origin=web_app_origin,
        lane_concurrency=lane_concurrency_from_env(env),
        activation_nudge_enabled=_env_flag(env, ACTIVATION_NUDGE_ENV),
        activation_nudge_limit=(
            _env_count(env, ACTIVATION_NUDGE_LIMIT_ENV)
            or WorkerConfig().activation_nudge_limit
        ),
    )


def compose(
    *,
    engine,
    config: WorkerConfig,
    env: dict,
    transport=None,
    refresh=None,
    adapters: Optional[media_adapters.MediaAdapters] = None,
    past_posts: Optional[past_posts_composition.WorkerPastPosts] = None,
    meta=None,
    transit=None,
    conversion_evidence=None,
) -> WorkerApp:
    """Assemble the object graph. Touches no network — the transport arrives
    built (main() constructs it from TARGET_TELEGRAM_BOT_TOKEN) and its
    credential is probed by run(), not here. *refresh* defaults to the real
    IG refresh door — always constructible (no config), egress-floored; tests
    inject a scripted one.

    *past-posts source* is `past_posts_composition.build_worker`'s answer, which `main`
    builds first because the card media fetch needs its coordinator. None, the
    default, wires no past-posts seam at all, as a deploy without the past-posts source's key
    ring does. *conversion_evidence* is the release's conversion run `main`
    already read; None, the default, reads it from *env*. *meta*
    and *transit* are test overrides of the seams built from *env*; `main`
    passes neither."""
    # The publish leg: the Graph adapter is always built (it
    # reads each account's token at call time); the media fetch exists only
    # with media adapters; the registry parks the kind naming whichever of
    # the three (fetch, meta, transit) is missing. The same adapter answers the
    # ambiguous-publish reconciler's poll.
    from src.services.target import mix_conversion

    meta = meta if meta is not None else _meta_from_env(engine, env)
    if conversion_evidence is None:
        conversion_evidence = _conversion_evidence(env)
    mix_capability = mix_conversion.capability(env, "worker", conversion_evidence)
    deps = WorkerDeps(
        meta=meta,
        transit=transit if transit is not None else _transit_from_env(env),
        media_fetch=_publish_media_fetch(adapters) if adapters else None,
        # None until the worker has RESEND_API_KEY and EMAIL_FROM, and that is
        # the honest state rather than a stub: the kind parks with a reason
        # naming what is missing.
        email=email_sender.sender_from_env(env),
        transport=transport,
        poll=_poll_from(engine, meta),
        refresh=refresh if refresh is not None else credential_lifecycle.ig_refresh,
        precheck=_precheck_from_env(env),
        adapters=adapters or {},
        mix_conversion_evidence=conversion_evidence,
        engine=engine,
        config=config,
        # The past-posts source. The key ring arms it, and redacts a deletion receipt
        # either way; everything else is the armed past-posts source's, and None without
        # the ring.
        past_posts_keys=past_posts_activation.load_keys(env),
        past_posts=past_posts.coordinator if past_posts else None,
        past_posts_evidence=past_posts.evidence if past_posts else None,
        past_posts_client=past_posts.client if past_posts else None,
        past_posts_sync=past_posts.sync_ports if past_posts else None,
        # The coordinator's wrapper, which owns the bound-content review route,
        # never the bare `SelectedPreparation` under it.
        selected_prepare=past_posts.coordinator.selected_prepare
        if past_posts
        else None,
        past_posts_capability=past_posts.capability if past_posts else None,
    )
    registry = build_registry(deps)
    live = {k for k, e in registry.items() if not isinstance(e, Parked)}
    recurring = {
        "v": 1,
        "reap_expired": 60.0,  # every lapsed lease waits for this beat
        # a source stranded in `error` is never re-scheduled, so the
        # branch that alerts never runs again. This beat is the only thing
        # that re-opens its mouth. Cadence is the clock's; the per-source
        # bound is `cfg.stranded_alert_after_seconds`.
        "alert_stranded_sources": 6 * 3600.0,
        "retention_sweep": 3600.0,
        # Reconciler cadence + budget: sweep every 60 s, LIMIT 50. The kind is
        # live regardless of the poll seam (its notify half needs none),
        # so it satisfies the `recurring <= live` assert below.
        #
        # With `reap_expired` it is the fastest unconditional beat, which the
        # worker-down threshold (`DEFAULT_WORKER_STALE_S` in
        # `storydump_cli/health_classify/scheduling.py`) rests on: slow both and that
        # threshold must rise with them. `tests/src/test_worker.py` fails
        # until it does.
        "reconcile_ambiguous": 60.0,
        # Mints the deletion-cleanup jobs a Meta deletion receipt is owed. Not
        # gated on the activation: a deletion closes things (`past_posts_lifecycle`).
        "past_posts_lifecycle_sweep": 60.0,
    }
    if "reap_transit_assets" in live:
        recurring["reap_transit_assets"] = 6 * 3600.0
    if "activation_nudge_sweep" in live:
        # Daily. Live only when the switch, a provider and an origin are all
        # there; said here because an armed nudge emails real people,
        # and that must be visible in the deploy log.
        recurring["activation_nudge_sweep"] = 24 * 3600.0
        logger.info(
            "activation nudge armed (limit=%d/day)", config.activation_nudge_limit
        )
    assert set(recurring) - {"v"} <= live, "recurring kinds must be runnable here"

    heartbeat = jobs.LeaseHeartbeat(
        connect=lambda: engine.connect(),
        interval_seconds=config.heartbeat_interval_seconds,
        lease_seconds=config.lease_seconds,
    )
    session_for = unit_of_work.make_session_for(engine)
    name = f"{socket.gethostname()}-{os.getpid()}"
    # K claim-and-run tasks per lane, each claiming on a
    # pooled checkout it returns at once — K bounds tasks, not connections.
    # The ceiling is asserted HERE, at composition, so an oversubscribed
    # configuration refuses to start rather than timing out on checkouts.
    logger.info(
        "worker %s",
        assert_concurrency_fits(config, pool_size=unit_of_work.POOL_SIZE_SEAM),
    )
    loops = [
        WorkLoop(
            # A unit composition may pass an engine with no `connect`; such a
            # loop never claims (it raises at run, as an unbound one does).
            connect=getattr(engine, "connect", None),
            session_for=session_for,
            lane=lane,
            registry=registry,
            heartbeat=heartbeat,
            config=config,
            worker_name=f"{name}-{lane}-{k}",
            past_posts_capability=deps.past_posts_capability,
        )
        for lane in ("interactive", "bulk")
        for k in range(config.concurrency_for(lane))
    ]
    return WorkerApp(
        registry=registry,
        mix_conversion_evidence=conversion_evidence,
        source_mix_capability=mix_capability,
        loops=loops,
        recurring=recurring,
        expected_bot=(env.get(reg.BOT_VAR) or "").lstrip("@") or None,
        heartbeat=heartbeat,
        heartbeat_lease_seconds=config.lease_seconds,
        heartbeat_interval_seconds=config.heartbeat_interval_seconds,
        config=config,
        deps=deps,
        engine=engine,
    )


DEAD_CREDENTIAL_REASON = (
    "credential rejected by Telegram at startup (getMe 401) — DEAD TOKEN;"
    " the channel is down until it is replaced, and every park of this job is"
    " this reminder"
)


WRONG_BOT_REASON = (
    "WRONG BOT: the token is @{actual}, the configured bot is @{expected} — cards"
    " from it carry buttons nobody listens on; fix TARGET_TELEGRAM_BOT_TOKEN"
)


async def apply_transport_probe(app: WorkerApp) -> None:
    """The composition-time credential check.

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
        # A card from the wrong bot is a dead card — refuse
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
    backpressure=None,
    watchdog=None,
) -> str:
    """One human-readable line from the observables — the soak's visibility.
    *backpressure* is `backpressure.snapshot`'s dict: the
    queue's depth and age per lane, the outbox backlog, the `tg_global`
    pacing state and the workspace waiting longest."""
    # K loops per lane report as ONE lane: the counters summed, the
    # task count shown, so the line reads the same at K=1 and K=4.
    by_lane: dict = {}
    for wl in loops:
        agg = by_lane.setdefault(
            wl.lane,
            {
                "tasks": 0,
                "processed": 0,
                "parked": 0,
                "failures": 0,
                "exhausted": 0,
                "fenced": 0,
                "waits": 0,
            },
        )
        agg["tasks"] += 1
        for key in ("processed", "parked", "failures", "exhausted", "fenced"):
            agg[key] += int(getattr(wl, key, 0) or 0)
        agg["waits"] += int(getattr(wl, "claim_waits", 0) or 0)
    lanes = " ".join(
        f"{lane}[tasks={a['tasks']} processed={a['processed']} parked={a['parked']}"
        f" failures={a['failures']} exhausted={a['exhausted']} fenced={a['fenced']}"
        f" waits={a['waits']}]"
        for lane, a in by_lane.items()
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
            f" advanced={prompt_sweeper.advanced}"
            f" approved={prompt_sweeper.approved}"
            f" missed={prompt_sweeper.missed}"
            f" unheard={prompt_sweeper.unheard}]"
        )
    if watchdog is not None:
        line += f" watchdog[armed={watchdog.armed}]"
    if backpressure is not None:
        line += " " + _backpressure.render(backpressure)
    return line


class SenderSweeper:
    """The sender-job mint sweep, with the observables its silent death cost
    us: `sweeps` counts iterations, `mints` counts jobs
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
                            self.mints += await ensure_sender_jobs(
                                session, limit=self._app.config.sender_mint_limit
                            )
                except Exception:  # noqa: BLE001 — outlive a blip, loudly
                    logger.exception("sender-job sweep failed; retrying on cadence")
            await jobs.wait_or_stop(stop, self._app.config.sender_sweep_seconds)


class PromptSweeper:
    """The prompt sweep on its own cadence: due intents gain their cards
    and advance to `awaiting_approval` in the same pass — the web queue is a
    surface every workspace has, so delivery is not what gates the
    edge. Then the miss leg (`fn_planned_serve` / `fn_planned_misses`): planned stories that will not be served
    end `expired` and their bound chats are told. The two legs run in two
    transactions, so a fault in one never holds the other back. Counters ride
    the status line — the same absence-is-failure control the sender sweeper
    carries; `unheard` counts misses nobody could be told of. Runs regardless
    of channel state: cards enqueued while the transport is parked simply wait
    as `pending` rows."""

    def __init__(self, app: "WorkerApp"):
        self._app = app
        self.sweeps = 0
        self.prompted = 0
        self.advanced = 0
        self.approved = 0  # stories the serve step approved
        self.missed = 0
        self.unheard = 0

    async def prepare_selected(self, sessions):
        deps = getattr(self._app, "deps", None)
        prepare = getattr(deps, "selected_prepare", None)
        if prepare is None:
            return
        # The bounded door discovers already selected subjects, never a past-posts catalog.
        async with sessions({}) as session:
            subjects = (
                (
                    await session.execute(
                        text("SELECT * FROM fn_selected_preparation_due(:lim)"),
                        {"lim": self._app.config.prompt_sweep_limit},
                    )
                )
                .mappings()
                .all()
            )
        for subject in subjects:
            try:
                await prepare(
                    engine=self._app.engine,
                    workspace_id=str(subject["workspace_id"]),
                    intent_id=str(subject["intent_id"]),
                )
            except Exception:  # noqa: BLE001 — one selected failure cannot starve its siblings
                logger.warning("selected media preparation deferred", exc_info=False)

    async def run(self, stop: asyncio.Event) -> None:
        # Each leg is one transaction with no tenant and the system actor —
        # the shape `make_session_for` gives a system job — so the doors and
        # the per-workspace claims inside the sweep scope what it touches.
        sessions = unit_of_work.make_session_for(self._app.engine)
        cfg = self._app.config
        while not stop.is_set():
            self.sweeps += 1
            try:
                await self.prepare_selected(sessions)
                async with sessions({}) as session:
                    session.info["past_posts_capability"] = getattr(
                        getattr(self._app, "deps", None), "past_posts_capability", None
                    )
                    counts = await prompts_mod.sweep_due_prompts(
                        session,
                        limit=cfg.prompt_sweep_limit,
                        late_seconds=cfg.planned_late_seconds,
                    )
                self.prompted += counts["prompted"]
                self.advanced += counts["advanced"]
                self.approved += counts.get("approved", 0)
            except Exception:  # noqa: BLE001 — outlive a blip, loudly
                logger.exception("prompt sweep failed; retrying on cadence")
            try:
                async with sessions({}) as session:
                    misses = await prompts_mod.sweep_planned_misses(
                        session,
                        limit=cfg.prompt_sweep_limit,
                        late_seconds=cfg.planned_late_seconds,
                    )
                self.missed += misses["missed"]
                self.unheard += misses["unheard"]
            except Exception:  # noqa: BLE001 — outlive a blip, loudly
                logger.exception("planned-miss sweep failed; retrying on cadence")
            await jobs.wait_or_stop(stop, cfg.prompt_sweep_seconds)


async def supervise(stop: asyncio.Event, tasks) -> asyncio.Task | None:
    """Wait until stop is requested OR any supervised task exits.

    Returns the dead task: one that raised (regardless of stop), or one that
    exited cleanly before stop was requested. None on legitimate shutdown —
    a clean or cancelled exit after stop fired. The caller owns
    what loud means; this guarantees that a background task that dies while
    the worker keeps printing healthy status lines is still reported.
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
    # Death is a PER-TASK verdict, never a global-flag one: a blanket
    # stop.is_set() exclusion would mask a real crash that races an external
    # stop. A task is dead when it RAISED (always, stop or no stop), or when
    # it returned cleanly while stop was unset (every supervised body is a
    # while-not-stop loop, so a clean early return is only reachable through a
    # bug). A clean exit after stop fired is legitimate shutdown — the soak's
    # own status reporter exits that way — and cancellation is shutdown
    # machinery, not death.
    dead = [
        t
        for t in done
        if t is not stop_waiter
        and not t.cancelled()
        and (t.exception() is not None or not stop.is_set())
    ]
    return dead[0] if dead else None


async def _backpressure_snapshot(app: WorkerApp):
    """One short read for the status line; a failure is a None, never a
    reporter that stops reporting."""
    try:
        # identify: the worker's own log, never the operating details
        return await _backpressure.read(app.engine, app.config, identify=True)
    except Exception as exc:  # noqa: BLE001 — the line still prints
        logger.warning("status: backpressure snapshot failed: %r", exc)
        return None


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
                    backpressure=await _backpressure_snapshot(app),
                    watchdog=app.watchdog,
                ),
            )


async def run(app: WorkerApp, *, stop: asyncio.Event | None = None) -> None:
    """Run the worker under its event-loop watchdog (`loop_watchdog`).

    The watchdog is armed before the first startup step and disarmed after the
    last step of the stop. Its condition is the loop not turning, which is as
    true of a startup or a teardown that blocks as of a tick that does, and
    nothing else times either: the health listener answers from before the
    first connection, so the deploy's check says nothing about the rest of
    startup, and a stop the worker begins by itself, after a task death, has no
    drain window behind it. The `finally` holds nothing else, so the thread
    cannot outlive the run whichever way the run ends.

    *stop* is the test seam: signals set the same event, so a harness can end
    a soak the way SIGTERM would without owning a process.
    """
    cfg = app.config
    watchdog = app.watchdog = loop_watchdog.LoopWatchdog(
        stall_seconds=cfg.loop_stall_seconds, beat_seconds=cfg.loop_beat_seconds
    )
    watchdog.start(asyncio.get_running_loop())
    try:
        logger.info(
            "loop watchdog armed: exits %d when the event loop is silent for %ds",
            loop_watchdog.STALL_EXIT_CODE,
            cfg.loop_stall_seconds,
        )
        await _run(app, stop=stop)
    finally:
        watchdog.stop()


def _clock_for(app: WorkerApp, election_conn) -> scheduler.Clock:
    """The clock this worker runs: its cadence, its recurring set, and the
    past-posts source's activation, so a tick judges a past-posts source as the claim does."""
    cfg = app.config
    return scheduler.Clock(
        election_conn,
        async_sessionmaker(app.engine, expire_on_commit=False),
        interval_seconds=cfg.clock_interval_seconds,
        max_inserts=cfg.clock_max_inserts,
        refresh_cadence_seconds=cfg.refresh_cadence_seconds,
        recurring=app.recurring,
        conversion_evidence=app.mix_conversion_evidence,
        capability=app.deps.past_posts_capability,
    )


async def _run(app: WorkerApp, *, stop: asyncio.Event | None) -> None:
    """Bind connections, start the clock/heartbeat/loops, run until stopped."""
    engine = app.engine
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
    # SOCKET, not the program: a listener bound after the slow startup steps
    # cannot answer during them, and Railway can mark a healthy deploy FAILED.
    # Binding here means the endpoint answers for the whole of
    # startup however slow the connections and the election turn out to be,
    # which is also why this root needs no startup-grace constant.
    health_server = app.health_server = await worker_health.serve_health(app)

    election_conn = await engine.connect()
    # Who the worker connects as, and whether that login bypasses RLS:
    # the runtime posture is verified from this line after a deploy.
    role = await unit_of_work.connection_role(election_conn)
    logger.info("worker database role: %s", role if role is not None else "unknown")
    # No pinned claim connections: each loop checks one out per
    # claim through `connect` and returns it at once.

    clock = app.clock = _clock_for(app, election_conn)

    hb_task = asyncio.create_task(app.heartbeat.run(), name="lease-heartbeat")
    await clock.start()
    loop_tasks = [
        asyncio.create_task(wl.run(), name=f"lane-{wl._worker_name}")
        for wl in app.loops
    ]
    logger.info(
        "worker up: lanes=%s live_kinds=%s recurring=%s",
        [wl.lane for wl in app.loops],
        sorted(k for k, e in app.registry.items() if not hasattr(e, "reason")),
        sorted(set(app.recurring) - {"v"}),
    )
    await apply_transport_probe(app)
    status_task = asyncio.create_task(
        _status_reporter(app, stop, app.config.status_interval_seconds),
        name="status-reporter",
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
        await election_conn.close()
        if app.deps.transport is not None:
            await app.deps.transport.aclose()
        if app.deps.past_posts_client is not None:
            await app.deps.past_posts_client.aclose()
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


def _card_media_fetch(
    engine, adapters: media_adapters.MediaAdapters, *, fetch_selected=None
):
    """Resolve a queued card's source under its tenant, then fetch outside SQL.

    Drive cards use the source/reference contract. Past-posts cards additionally
    name the server-selected intent and use its bound preparation port; an
    absent port is a no-send refusal. Current provider configuration is read
    here, for every queued card.
    """
    from src.channels.telegram_transport import MediaTransient, MediaUnavailable

    async def read(media: dict) -> tuple[bytes, str, Optional[str]]:
        source_id, workspace_id = str(media["source_id"]), str(media["workspace_id"])
        factory = unit_of_work.poller_session_factory(engine, workspace_id)
        async with factory() as session:
            source = (
                (
                    await session.execute(
                        text(
                            "SELECT provider, config FROM media_sources"
                            " WHERE id = :source AND workspace_id = :workspace"
                            " AND state = 'active'"
                        ),
                        {"source": source_id, "workspace": workspace_id},
                    )
                )
                .mappings()
                .first()
            )
        if source is None:
            if media.get("intent_id"):
                from src.services.target.outbox import MediaAdmissionNotSent

                raise MediaAdmissionNotSent("asset_unavailable")
            raise MediaUnavailable("media source is missing or inactive")
        if source["provider"] == "instagram_past_posts":
            from src.services.target import source_policy, source_read_admission
            from src.services.target.outbox import MediaAdmissionNotSent

            intent_id = media.get("intent_id")
            if (
                not isinstance(intent_id, str)
                or not intent_id
                or fetch_selected is None
            ):
                raise MediaAdmissionNotSent("past_posts_revalidation_required")
            try:
                selected = await fetch_selected(
                    engine=engine, workspace_id=workspace_id, intent_id=intent_id
                )
            except source_policy.MediaAdmissionPending as exc:
                raise MediaAdmissionNotSent(exc.reason) from None
            except (
                source_read_admission.SourceReadRefused,
                *media_adapters.MEDIA_FAILURES,
            ):
                raise MediaAdmissionNotSent(
                    "past_posts_revalidation_required"
                ) from None
            mime = "image/jpeg" if selected.media_kind == "image" else "video/mp4"
            return selected.content, media.get("file_name") or "story.jpg", mime
        cap = media_adapters.MEDIA_CARD_MAX_BYTES.get(
            str(media.get("kind")), media_adapters.MEDIA_CARD_MAX_BYTES["image"]
        )
        try:
            adapter = media_adapters.get_adapter(adapters, source["provider"])
            return await adapter.fetch_bytes(
                source["config"],
                str(media["ref"]),
                source_id=source_id,
                workspace_id=workspace_id,
                max_bytes=cap,
            )
        except media_adapters.MediaCredentialDead:
            # Keep the transport's counted failure arm, without provider text.
            raise media_adapters.MediaCredentialDead(
                "media connection needs to be renewed"
            ) from None
        except media_adapters.MediaSourceGone:
            raise media_adapters.MediaSourceGone(
                "media source is unavailable"
            ) from None
        except (
            media_adapters.MediaTerminalError,
            media_adapters.ProviderNotWired,
        ):
            # Provider messages may contain credentials or signed URLs.
            raise MediaUnavailable("media source is unavailable") from None
        except (
            media_adapters.MediaRetryableError,
            media_adapters.MediaLostResponse,
        ):
            # Dead credentials and ring failures remain loud and counted.
            raise MediaTransient("media provider temporarily unavailable") from None
        except media_adapters.MediaError:
            raise media_adapters.MediaError("media provider read failed") from None

    async def fetch(media: dict):
        from src.services.target.outbox import MediaAdmissionNotSent

        try:
            return await read(media)
        except MediaAdmissionNotSent:
            raise
        except Exception:  # noqa: BLE001 — past-posts cards never degrade to actionable text
            if media.get("intent_id") is not None:
                raise MediaAdmissionNotSent(
                    "validation_pending", preparation_failed=True
                ) from None
            raise  # Drive's existing transport taxonomy and telemetry are unchanged

    return fetch


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env = dict(os.environ)
    url = unit_of_work.engine_url_from_env(env)
    if url is None:
        # No fallback: a root that ran against a database nobody named is the
        # plausible-wrong-value casualty this refusal exists to prevent. By
        # name, loudly — the API's data routes refuse the same absence (503).
        print(
            f"FATAL: {unit_of_work.DATABASE_URL_VAR} is unset. The worker runs the"
            " target tier only and has no database to run it against; set"
            f" {unit_of_work.DATABASE_URL_VAR} on this service. Refusing to boot.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    # The key ring, before anything connects or binds. Built lazily, a missing
    # or malformed key booted a worker that answered its health check and then
    # failed credential reads one at a time — and before `RingUnavailable`
    # existed, the refresh leg recorded each as a dead Instagram account and
    # messaged its owner to reconnect. Refused here, ahead of `run`'s health
    # listener, the deploy fails Railway's check and the previous one serves on.
    try:
        oauth_states.ring()
    except oauth_states.RingUnavailable as exc:
        # The remedy is named because the tempting one is wrong: a newly
        # generated key boots, and then cannot read a stored credential.
        print(
            f"FATAL: the credential key ring cannot load. {exc}\nThe worker decrypts"
            " every token it posts and syncs with, so it needs the key the stored"
            " credentials were encrypted with — the API service holds the same one."
            " Refusing to boot.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    config = worker_config_from_env(env, web_app_origin=settings.web_app_origin)
    engine = unit_of_work.create_engine(url)
    transport = None
    token = env.get(reg.TOKEN_VAR)
    # The Drive read leg is armed unconditionally. The engine resolves each
    # workspace's stored Drive grant when a source is read, so this leg needs
    # no credential environment variable of its own.
    #
    # Safe to arm with no `gdrive` credential stored. A source with no
    # credential raises DriveCredentialDead, which `media_sync` classifies
    # persistent — the source flips to `error`, its disconnect alert fires once,
    # and THE JOB SUCCEEDS. So the failure mode is a visible source in `error`,
    # not a poisoned lane.
    #
    # `media_fetch` IS derived from this (`compose`), and the registry
    # gates `publish_pipeline` on media_fetch AND meta AND transit, so a
    # missing Cloudinary trio parks the kind by name rather than crashing the
    # ladder.
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        # Said at startup, once, where an operator looks: without the client
        # the hourly refresh cannot run here, and every folder's
        # sync will refuse — retryably, in the log, never to a tenant — an
        # hour after the workspace connects.
        logger.warning(
            "Drive read leg armed without GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET:"
            " this worker cannot refresh a Drive grant; set both on this service"
        )
    drive = google_drive_adapter.GoogleDriveAdapter(
        token_provider=drive_credentials.provider_from_engine(engine),
    )
    adapters = {PROVIDER_GDRIVE: drive}
    # The past-posts source first: the approval card's media fetch needs its coordinator.
    # None (and one `off (<reason>)` line) unless the key ring loads.
    conversion_evidence = _conversion_evidence(env)
    past_posts = past_posts_composition.build_worker(engine=engine, env=env)
    if token:
        from src.channels.telegram_transport import transport_from_env

        # The approval card is the photo: the transport
        # fetches a card's media through the same Drive read leg. This is NOT
        # the publish pipeline's `media_fetch` seam (`_publish_media_fetch`,
        # composed above), which reads the file for Meta at its own cap.
        transport = transport_from_env(
            token,
            env,
            media_fetch=_card_media_fetch(
                engine,
                adapters,
                fetch_selected=past_posts.coordinator.fetch_selected
                if past_posts
                else None,
            ),
        )
    app = compose(
        engine=engine,
        config=config,
        env=env,
        transport=transport,
        adapters=adapters,
        past_posts=past_posts,
        conversion_evidence=conversion_evidence,
    )
    try:
        asyncio.run(run(app))
    except WorkerTaskDied:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
