"""The Storydump API — the target tier's web surface (#1028, #1015).

`create_app()` is a factory because the previous module-level singleton is why
every API test in the repo bound the legacy app: an engine, a settings snapshot
and a middleware stack assembled at import time cannot be handed a test
double. The Procfile line is unchanged — ``uvicorn src.api.app:app`` — and the
module still exports `app`, built once at the bottom.

What it mounts, and why each lives where it does:

- ``/auth`` — sign-in hosted on the API (`07` §1), see `routes/auth.py`.
- ``/api/v1`` — reads as resources, writes as the `01` vocabulary, see
  `routes/v1.py`.
- ``/webhooks/telegram`` — the W4 ingress route: the `/start` door (#1183)
  and the group join path (#1242). Chat-inbound COMMANDS remain #854 and are
  not dispatched
  (`TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` + `app.state.ingress`) rather than
  writing a new one.
- ``/health`` — Railway's probe (`railway.toml`), which now also says whether
  a target engine is configured, so a service that would 503 every data route
  is visible from the probe instead of only from the first request.

What deliberately does not exist any more: the legacy ``/auth`` OAuth router,
the ``/api/onboarding`` router and its Mini App (`/static`; the Mini App's
own URL, `/webapp/onboarding`, stays answered — as a redirect to the front
end, because buttons already sent still navigate there — see
`routes/retired.py`), all of which read the legacy schema that M.3 froze; the in-memory
SlowAPI limiter — a process-local mutable singleton (`01` §"what deliberately
does not exist") — whose only job now, pre-auth admission, is the durable
`rate_counters` ``preauth_ip`` scope (`02` §6, `05`) inside the auth routes.

The engine comes from `TARGET_DATABASE_URL` and from nothing else. Unset means
``app.state.engine is None`` and every data route answers 503 naming the
variable — never the settings-built URL, which on the deployed service points
at nothing and on a misconfigured one would point at a legacy-shaped database
(#1010's class of silent misdirection).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from contextlib import asynccontextmanager
import time
from typing import Mapping, Optional

from sqlalchemy.exc import TimeoutError as PoolTimeout
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.api.routes.auth import router as auth_router
from src.api.routes.retired import router as retired_router
from src.api.routes.v1 import IDEMPOTENCY_HEADER
from src.api.routes.v1 import router as v1_router
from src.api.routes import webhooks
from src.api.routes.meta import router as meta_router
from src.api.routes.webhooks import router as webhooks_router
from src.config.settings import settings
from src.exceptions.tenancy import TenantResolutionError
from src.services.target.commands import CommandNotBuilt, CommandRefused
from src.services.target.invitations import InvitationRefused
from src.services.target.category_mix import MixInvalid
from src.services.target.provisioning import ProvisioningRefused
from src.services.target import backpressure, posting_health, scheduling_health
from src.services.target.work_loop import WorkerConfig
from src.services.target.unit_of_work import (
    connection_role,
    create_engine,
    engine_url_from_env,
    INGRESS_POOL_TIMEOUT_SEAM,
    PoolWatch,
)
from src.services.target.telegram_dispatch import TelegramDispatcher
from src.services.target.webhook_ingress import AdmissionConflict, DeliveryReplayed
from src.utils.logger import logger

VERSION = "0.2.0"
_START_TIME = time.time()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Standard security headers on every response. One strict policy for
    every path: the Telegram Mini App exemption (frame-ancestors for
    telegram.org) left with the Mini App."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
        return response


class DropAmbiguousForwardedForMiddleware:
    """Drop X-Forwarded-For entirely when it arrives as more than one header.

    A well-behaved single reverse proxy (the topology TRUSTED_PROXY_HOSTS
    assumes) combines any inbound X-Forwarded-For into one string and appends
    its own observed peer, producing exactly one outbound header. More than
    one header instance reaching the origin means either the immediate proxy
    did not behave that way, or the request took a shape a single-hop trust
    model cannot interpret -- and there is no way to reconstruct, from the
    flattened wire representation alone, which instance is genuinely the
    trusted proxy's. Concatenating the instances is not a fix: whichever one
    ends up last after concatenation still wins ProxyHeadersMiddleware's
    right-to-left trust walk, so an attacker who controls ordering still wins
    (an earlier draft of this fix did exactly that, and its own test caught
    it -- see the PR for the failed attempt).

    So this drops the header instead of merging it, which makes
    ProxyHeadersMiddleware fall back to the raw connecting peer -- the same
    path it already takes when there is no X-Forwarded-For header at all.
    Under normal operation behind a single well-behaved proxy this never
    triggers; it exists for the anomalous case, and failing closed to the
    shared edge IP beats failing open to an attacker-chosen identity. See #765.
    """

    _XFF = b"x-forwarded-for"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers", []))
        count = sum(1 for k, _ in headers if k.lower() == self._XFF)

        if count > 1:
            peer = scope.get("client")
            logger.warning(
                "Dropping X-Forwarded-For: %d header instances from peer %s "
                "(ambiguous -- falling back to raw peer for trust attribution)",
                count,
                peer[0] if peer else "unknown",
            )
            scope["headers"] = [(k, v) for k, v in headers if k.lower() != self._XFF]

        await self.app(scope, receive, send)


#: `TenantResolutionError.reason` → status, for the reasons the web surface can
#: raise. Session reasons are 401 and the body does not say which (the
#: distinct reasons exist for the log); a workspace the caller cannot see is
#: 404, never 403 — the same 404 a workspace that does not exist gets (`07`
#: §5); below the floor is 403. The chat-side reasons are not in this table
#: because no web route resolves a chat; reaching the handler with one is a
#: programming error, and it is answered as one (500 + log), never as a
#: silently chosen client status.
_TENANT_STATUS = {
    "invalid_session": 401,
    "expired_session": 401,
    "revoked_session": 401,
    "disabled_user": 401,
    "not_a_member": 404,
    "insufficient_role": 403,
}
_TENANT_DETAIL = {401: "authentication required", 404: "not found", 403: "forbidden"}

#: `CommandRefused.reason` → status. Pinned TOTAL over `commands.REASONS` by
#: the factory test, so a new reason cannot ship without a row here.
_COMMAND_STATUS = {
    "unknown_command": 404,
    "not_built": 501,
    "workspace_required": 400,
    "invalid_args": 400,
    "not_found": 404,
    "illegal_transition": 409,
    "manual_mode": 409,
    "not_connected": 409,
    "cancelling": 409,
    "nothing_to_confirm": 409,
    "may_have_posted": 409,
}

#: `ProvisioningRefused.reason` → status (#1041).
#:
#: A caller-supplied value we will not store is 400 and NAMES which one, so a
#: front end can point at the field rather than say "invalid".
#:
#: `slot_not_seeded` is deliberately ABSENT. It is a postcondition on the
#: seeding invariant with no currently reachable path (measured — see
#: `provisioning.create_destination`), so reaching it is a programming error
#: and is answered as one (500 + log) rather than as a client status somebody
#: chose. Mapping it would turn a broken invariant into a number a front end
#: renders and nobody investigates.
_PROVISIONING_STATUS = {
    "account_ref_required": 400,
    "account_ref_too_long": 400,
    # The typed-handle path (#1089). Mapped for the same reason as the two
    # above: these are values a person typed into a field, so the answer has to
    # be a 400 naming which one. An unmapped reason falls through to `_unmapped`
    # and is answered 500 — correct for a broken invariant, wrong for a typo.
    "handle_required": 400,
    "handle_malformed": 400,
    "handle_too_long": 400,
    "folder_required": 400,
    "folder_not_a_drive_folder": 400,
    # A pick inside, or containing, a folder already connected (owner ruling
    # 2026-09-08: connected folders are disjoint). A conflict with what is
    # here, not a typo — 409, like `drive_not_connected`.
    "source_nested": 409,
    # The connected folders changed between the pick's check and its write
    # (two admins at once): a conflict to retry, not a typo.
    "sources_changed": 409,
}

#: `InvitationRefused.reason` → status, total over `invitations.REASONS`.
_INVITATION_STATUS = {
    "not_acceptable": 404,
    "identity_mismatch": 403,
    # The CREATE half's refusals (#1172). All three are the caller's input
    # being wrong rather than a state or an authorization fact, so 400 — and
    # `already_invited` is deliberately NOT 409: a pending invitation to that
    # address is not a conflicting write to fix by retrying, it is a thing
    # that already exists, and the remedy is to revoke or wait rather than to
    # send again.
    "already_invited": 400,
    "email_required": 400,
    "invalid_channel": 400,
    "invalid_role": 400,
}
_INVITATION_DETAIL = {
    "not_acceptable": "invitation not acceptable",
    "identity_mismatch": "identity proof mismatch",
    "already_invited": "that address already has a pending invitation",
    "email_required": "an email invitation needs an address",
    "invalid_role": "role must be admin or member",
    "invalid_channel": "delivery_channel must be email or telegram",
}


def _unmapped(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unmapped refusal on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


def _register_handlers(app: FastAPI) -> None:
    """Service refusals → HTTP, once. No route speaks a status for these."""

    @app.exception_handler(PoolTimeout)
    async def _pool_saturated(request: Request, exc: PoolTimeout):
        # The ingress pool's 1 s wait (phase 2 step 2) is met by every route,
        # not only the webhook: a web request that cannot get a connection is
        # told to retry rather than shown a 500 (the webhook route maps the
        # same wait itself, before admission, and never reaches this).
        logger.warning("pool saturated on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=503,
            content={"detail": "busy — try again", "reason": "pool_saturated"},
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(TenantResolutionError)
    async def _tenant(request: Request, exc: TenantResolutionError):
        status = _TENANT_STATUS.get(exc.reason)
        if status is None:
            return _unmapped(request, exc)
        logger.info("refused %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=status, content={"detail": _TENANT_DETAIL[status]}
        )

    @app.exception_handler(CommandRefused)
    async def _command(request: Request, exc: CommandRefused):
        status = _COMMAND_STATUS.get(exc.reason)
        if status is None:
            return _unmapped(request, exc)
        content = {"detail": str(exc), "reason": exc.reason}
        if isinstance(exc, CommandNotBuilt):
            content = {
                "command": exc.command,
                "detail": "not built",
                "reason": "not_built",
            }
        return JSONResponse(status_code=status, content=content)

    @app.exception_handler(ProvisioningRefused)
    async def _provisioning(request: Request, exc: ProvisioningRefused):
        status = _PROVISIONING_STATUS.get(exc.reason)
        if status is None:
            return _unmapped(request, exc)
        logger.info("refused %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=status, content={"detail": str(exc), "reason": exc.reason}
        )

    @app.exception_handler(MixInvalid)
    async def _mix(request: Request, exc: MixInvalid):
        # The web's one code carrier is `reason` (`target-api.ts::readError`),
        # matched as `[a-z0-9_]+` — so the refusal travels as
        # `invalid_mix_<reason>`, not in `detail`.
        logger.info("refused %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "reason": f"invalid_mix_{exc.reason}"},
        )

    @app.exception_handler(InvitationRefused)
    async def _invitation(request: Request, exc: InvitationRefused):
        status = _INVITATION_STATUS.get(exc.reason)
        if status is None:
            return _unmapped(request, exc)
        return JSONResponse(
            status_code=status,
            content={"detail": _INVITATION_DETAIL[exc.reason], "reason": exc.reason},
        )

    @app.exception_handler(DeliveryReplayed)
    async def _replayed(request: Request, exc: DeliveryReplayed):
        # Acknowledged WITHOUT re-execution — the same key and the same body.
        return JSONResponse(status_code=200, content={"outcome": "replayed"})

    @app.exception_handler(AdmissionConflict)
    async def _conflict(request: Request, exc: AdmissionConflict):
        # Channel-neutral wording: the exception already names the key that
        # was reused, and which header carried it is the adapter's business.
        return JSONResponse(
            status_code=409,
            content={
                "detail": "admission conflict: this key was already used for different content",
                "reason": "admission_conflict",
            },
        )


def _engine_from_env(env: Mapping[str, str]) -> Optional[AsyncEngine]:
    url = engine_url_from_env(env)
    if url is None:
        logger.warning(
            "TARGET_DATABASE_URL is unset: the API has no target engine and every "
            "data route answers 503 until it is configured"
        )
        return None
    # The API's pool waits `INGRESS_POOL_TIMEOUT_SEAM`, not the worker's 3 s:
    # the wait is the tap's user-visible latency (phase 2 step 2).
    return create_engine(url, pool_timeout=INGRESS_POOL_TIMEOUT_SEAM)


def _ingress_workers(env: Mapping[str, str]) -> int:
    """How many uvicorn processes serve this API — the Procfile's `--workers`
    is not visible from inside a process, so the deployment states it in
    `WEB_CONCURRENCY` (uvicorn's own variable); absent = one (F5 baseline)."""
    raw = (env.get("WEB_CONCURRENCY") or "").strip()
    try:
        return max(1, int(raw)) if raw else 1
    except ValueError:
        return 1


async def _register_webhook(app: FastAPI, env: Mapping[str, str]) -> None:
    """Register the bot's webhook on this API at startup — idempotent, on
    every deploy — and cache the report for `/health`. Never raises.

    The API holds the token and the secret already; a human step that must
    follow every deploy is a step that will be missed (the tap's first blocker
    was exactly that: a registration asking for `message` updates only).
    Skipped, with the reason in the report, when the token or the secret is
    absent, or when `TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` switches it off.
    """
    from src.channels import telegram_webhook_registration as reg

    token = env.get("TARGET_TELEGRAM_BOT_TOKEN")
    secret = env.get("TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN")
    if not reg.autoregister_enabled(
        env.get(reg.AUTOREGISTER_VAR), environment=env.get(reg.ENVIRONMENT_VAR)
    ):
        switched_off = (env.get(reg.AUTOREGISTER_VAR) or "").strip().lower() in (
            "0",
            "false",
            "no",
            "off",
        )
        app.state.webhook = {
            "ok": False,
            "skipped": (
                f"autoregister switched off ({reg.AUTOREGISTER_VAR})"
                if switched_off
                else "autoregister off (not the production environment)"
            ),
        }
        return
    if not token or not secret:
        app.state.webhook = {
            "ok": False,
            "skipped": "bot token or webhook secret not set",
        }
        return
    transport = None
    try:
        max_connections = reg.max_connections_from(env.get(reg.MAX_CONNECTIONS_VAR))
        transport = _telegram_transport(env)
        app.state.webhook = await reg.register(
            transport,
            url=env.get(reg.URL_VAR) or reg.DEFAULT_WEBHOOK_URL,
            secret=secret,
            expected_bot=env.get("TARGET_TELEGRAM_BOT_USERNAME"),
            max_connections=max_connections,
        )
    except reg.BadMaxConnections as exc:
        # Names the variable and the value, never a secret.
        app.state.webhook = {"ok": False, "error": str(exc)}
        logger.error("telegram webhook not registered: %s", exc)
    except Exception as exc:  # noqa: BLE001 — diagnostic; never fails startup
        app.state.webhook = {"ok": False, "error": type(exc).__name__}
        logger.warning(
            "telegram webhook not registered at startup: %s", type(exc).__name__
        )
    finally:
        if transport is not None:
            try:
                await transport.aclose()
            except Exception:  # noqa: BLE001
                pass


async def _sample_webhook_live(app: FastAPI, env: Mapping[str, str]) -> None:
    """Every minute: what Telegram holds for the bot's webhook RIGHT NOW —
    `getWebhookInfo`'s backlog and last delivery error — cached on
    `app.state.webhook_live` for `/health`. Read-only, so it runs wherever a
    token exists; a failure is a report, never a raise. This is the signal
    that tells "Telegram is not delivering" from "our route is failing": the
    former shows as a growing backlog with no error, the latter as
    `last_error_message` naming our response code."""
    if not env.get("TARGET_TELEGRAM_BOT_TOKEN"):
        return
    transport = _telegram_transport(env)
    try:
        while True:
            try:
                info = await transport.webhook_info()
                app.state.webhook_live = {
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "url": info.get("url"),
                    "pending_update_count": info.get("pending_update_count"),
                    "last_error_date": info.get("last_error_date"),
                    "last_error_message": info.get("last_error_message"),
                    "max_connections": info.get("max_connections"),
                    "allowed_updates": info.get("allowed_updates"),
                }
            except Exception as exc:  # noqa: BLE001 — a report, never a failed loop
                app.state.webhook_live = {
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "error": type(exc).__name__,
                }
            await asyncio.sleep(60)
    finally:
        try:
            await transport.aclose()
        except Exception:  # noqa: BLE001
            pass


async def _sample_db_role(app: FastAPI) -> None:
    """Fill `app.state.db_role` once, from the catalog; never raise.

    Diagnostic for the F.4 rollout (#751): production has connected as the
    owner role with BYPASSRLS, which makes every tenant policy inert, and this
    is how the switch to the runtime login is verified after a deploy. A
    failure leaves None — "not sampled" — never a guessed-safe value.
    """
    if app.state.engine is None:
        return
    try:
        async with app.state.engine.connect() as conn:
            app.state.db_role = await connection_role(conn)
    except Exception as exc:  # noqa: BLE001 — diagnostic; never fails startup
        logger.warning("database role not sampled at startup: %s", exc)


def _cors_origins() -> list[str]:
    """The ONE browser origin admitted (`settings.web_app_origin`); never "*",
    and with no origin configured no origin is admitted."""
    return [settings.web_app_origin] if settings.web_app_origin else []


def _telegram_transport(env: Mapping[str, str]):
    """The one bot transport the API speaks with — the `/start` door's
    acknowledgement and a tap's answer (the card's edit is the outbox's) — or None
    without the bot token (the same variable the worker sends with, so the API
    never holds a second credential for the one bot)."""
    token = env.get("TARGET_TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    from src.channels.telegram_transport import transport_from_env

    return transport_from_env(token, env)


def _telegram_reply(env: Mapping[str, str]):
    """The `/start` door's acknowledgement sender, or None without the token."""
    transport = _telegram_transport(env)
    return None if transport is None else transport.send_text


def create_app(
    *, engine: Optional[AsyncEngine] = None, env: Optional[Mapping[str, str]] = None
) -> FastAPI:
    """Assemble the app. *engine* injects the target engine (tests, or a
    composition root that owns the pool); otherwise it comes from *env*
    (default: the process environment) and from nothing else."""

    @asynccontextmanager
    async def _lifespan(app_: FastAPI):
        # The role sample and the webhook registration run as background
        # tasks so startup never waits on the database or on Telegram (see
        # `app.state.db_role` / `app.state.webhook` below); a task still
        # pending at shutdown is cancelled rather than left to die with the loop.
        tasks = [
            asyncio.create_task(_sample_db_role(app_)),
            asyncio.create_task(
                _register_webhook(app_, os.environ if env is None else env)
            ),
            asyncio.create_task(
                _sample_webhook_live(app_, os.environ if env is None else env)
            ),
        ]
        try:
            yield
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    app = FastAPI(
        title="Storydump API",
        description="Sign-in, reads and commands for the target tier",
        version=VERSION,
        lifespan=_lifespan,
    )
    app.state.engine = (
        engine
        if engine is not None
        else _engine_from_env(os.environ if env is None else env)
    )
    # Which database login this process holds, and whether it bypasses RLS
    # (#751, F.4). Sampled ONCE, in the background, after startup — `/health`
    # reports the cached answer and still opens no connection of its own, so a
    # database blip cannot fail the probe. None means "not sampled", never
    # "safe": production has connected as the owner role with BYPASSRLS, which
    # makes every tenant policy inert, and this field is how the switch to the
    # runtime login is verified after a deploy.
    app.state.db_role = None
    # The webhook registration report (`_register_webhook`): None until the
    # startup task has run; then `ok`, what Telegram holds, or why it was
    # skipped — never the token or the secret.
    app.state.webhook = None
    # What Telegram holds for the webhook now (`_sample_webhook_live`, every
    # minute): the backlog and the last delivery error — the signal that tells
    # "Telegram is not delivering" from "our route is failing".
    app.state.webhook_live = None

    # The W4 ingress seam: the `/start` door (#1183) and the group join path
    # (#1242, on #854's resolver door `fn_resolve_binding` — `07` §14).
    #
    # ⚠ THIS DOES NOT DISPATCH CHAT-INBOUND COMMANDS. A `/start` payload
    # carries its own resolution; a group message resolves its chat through
    # the door. An "approve" typed in a group is still not dispatched — that
    # is what #854 now tracks.
    #
    # Wired only when an engine exists: without one there is nothing to
    # `connect` to, and a runtime whose `connect` fails would convert the
    # route's honest 503 into a 500 mid-delivery.
    bot = _telegram_transport(os.environ if env is None else env)
    app.state.tap_metrics = webhooks.TapMetrics()
    app.state.ingress_workers = _ingress_workers(os.environ if env is None else env)
    app.state.pool_watch = (
        PoolWatch(app.state.engine) if app.state.engine is not None else None
    )
    if app.state.pool_watch is not None:
        snap = app.state.pool_watch.snapshot()
        logger.info(
            "ingress pool: size=%d overflow=%d timeout_s=%.1f workers=%d"
            " → Σ ingress connections = %d (the `05` inequality: 3×10 workers"
            " + 2×10 ingress = 50)",
            snap["size"],
            snap["overflow"],
            snap["timeout_s"],
            app.state.ingress_workers,
            (snap["size"] + snap["overflow"]) * app.state.ingress_workers,
        )
    app.state.ingress = (
        webhooks.IngressRuntime(
            connect=app.state.engine.connect,
            dispatch=TelegramDispatcher(),
            reply=None if bot is None else bot.send_text,
            answer_callback=None if bot is None else bot.answer_callback,
        )
        if app.state.engine is not None
        else None
    )

    _register_handlers(app)

    # Middleware. Starlette prepends, so the LAST added runs FIRST on the
    # request path: CORS outermost, then the ambiguous-XFF drop (#765) ahead
    # of the trusted-proxy walk (#726), then security headers innermost.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_hosts
    )
    app.add_middleware(DropAmbiguousForwardedForMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", IDEMPOTENCY_HEADER],
    )

    app.include_router(auth_router, prefix="/auth")
    app.include_router(v1_router, prefix="/api/v1")
    app.include_router(webhooks_router, prefix="/webhooks")
    # Meta's policy callbacks (#410). Under the same prefix as the other
    # provider-called doors; the URLs are not registered with Meta yet.
    app.include_router(meta_router, prefix="/webhooks/meta")
    # The Mini App's URL is baked into buttons real users still hold; it
    # redirects rather than 404s (`routes/retired.py`).
    app.include_router(retired_router)

    @app.get("/health")
    async def health_check():
        """Railway's probe. No auth. `target_database` is configuration
        presence, not liveness — a probe that opened a connection would take
        the service down for a database blip no restart repairs."""
        return {
            "status": "ok",
            "version": VERSION,
            "uptime_seconds": int(time.time() - _START_TIME),
            "target_database": app.state.engine is not None,
            "db_role": app.state.db_role,
            # The pool arithmetic and its high-water mark (phase 2 step 4):
            # with `ingress_workers` this is the Σ the `05` inequality reads.
            "pool": (
                app.state.pool_watch.snapshot()
                if getattr(app.state, "pool_watch", None) is not None
                else None
            ),
            "ingress_workers": app.state.ingress_workers,
            # The tap counters (phase 1 of the 2026-09-09 plan, step 12).
            "taps": app.state.tap_metrics.snapshot(),
            # The webhook this API registered on the bot at startup — a
            # snapshot from this process's start (`sampled: startup`).
            "webhook": app.state.webhook,
            "webhook_live": app.state.webhook_live,
        }

    @app.get("/health/scheduling")
    async def scheduling_health_check():
        """Is scheduling still advancing? (#1090 F1) — a SECOND health surface,
        deliberately not `/health` above.

        Railway gates deploys on `/health`, whose docstring is explicit that a
        probe opening a connection would take the service down for a database
        blip no restart repairs. That is right, and it is exactly why #1026 asked
        for a separate dependency-touching check: liveness and "is the work
        happening" are different questions and one endpoint cannot answer both
        without making one of them wrong.

        NOTHING IS RAISED HERE. This reports; the FLEET alert path polls it and
        decides. Two independent reasons, and the second is measured:

        1. An alert whose SENDING is performed by the system it monitors cannot
           fire when that system is down — the same law that kept this detector
           off the job table, applied to the output side.
        2. The app's own notification routing has NO WRITER: nothing anywhere
           writes `channel_bindings`, for any workspace (navi). An alert
           delivered into it would vanish silently, and we would have built a
           detector whose output goes nowhere.

        Unauthenticated, so it answers in AGGREGATES ONLY — counts and a lag,
        never a workspace, an account or a handle.

        503 when the engine is absent, matching every other data route: a
        monitor must be able to tell "scheduling is fine" from "I could not
        look", and collapsing those is the failure this whole issue is about.
        """
        engine = app.state.engine
        if engine is None:
            raise HTTPException(
                status_code=503, detail="target database not configured"
            )
        # A DIRECT CONNECTION, not a unit of work, and the empty tenant string
        # this replaced was not a near-miss — `UnitOfWork.__init__` refuses a
        # blank tenant at CONSTRUCTION, so the route raised before touching the
        # database and returned 500 to every caller it ever had.
        #
        # The guard is right and must not move. This aggregate is estate-wide
        # and has no tenant; naming one that does not exist is a lie the guard
        # correctly refused, and the remedy is the one its own message gives.
        #
        # That the estate-wide read ANSWERS rests on the owner bypassing RLS,
        # which is now measured rather than assumed: production connects as
        # `neondb_owner`, `058` sets no `FORCE ROW LEVEL SECURITY` and never
        # reassigns the owner, so `p_tenant` does not apply. Under a role the
        # policy DOES cover, a tenant-less read returns zero rows — and zero
        # rows here reads as a healthy estate. Whoever closes #751 must give
        # this a door; `accounts_active` is what tells the two apart.
        async with engine.connect() as conn:
            # TWO AXES, ONE PAYLOAD (#1120). The cursor axis is empty whenever
            # no destination is active, and `no-signal` is then the answer
            # whether the worker is healthy or DEAD — so the one monitored axis
            # covered nothing at all until the first tenant arrived. The worker
            # axis reads system jobs, whose population is tenant-independent.
            #
            # Same endpoint rather than a sibling, deliberately: a second URL
            # would need a second poller invocation enrolled on the fleet host,
            # a unit change, to close a hole the existing poller can already
            # reach. The cursor keys keep their names and meanings, so a poller
            # predating this change reads the payload exactly as before.
            lag = await scheduling_health.scheduling_lag(conn)
            worker = await scheduling_health.worker_freshness(conn)
            # The backpressure signal (phase 3a step 6): the same numbers the
            # worker's status line prints, for the poller that watches this —
            # without the waiting workspace's id (this route is public and
            # promises nothing identifying; `identify` stays False).
            pressure = await backpressure.snapshot(
                conn,
                now=datetime.now(timezone.utc),
                global_limit=WorkerConfig().global_limit,
                global_window_seconds=WorkerConfig().global_window_seconds,
            )
            return {**lag, "worker": worker, "backpressure": pressure}

    @app.get("/health/posting")
    async def posting_health_check():
        """Did a post actually LAND? (#1268) — a THIRD health surface.

        `/health/scheduling` above reads the clock and the worker. Both stayed
        true through a sixteen-day silence in which nothing posted: 1936
        consecutive `healthy` readings, every field of them correct. An intent
        awaiting approval is not overdue, it is waiting correctly, so the
        machinery gauge reads healthy because the machinery IS healthy.

        A SEPARATE URL — and the rule is stated here rather than re-argued,
        because the issue this closes names the next axes (stranded approvals,
        an empty media pool, an undelivered outbox) and each will ask again.

        **An axis JOINS an existing payload when it can share that poller's
        single verdict. It gets its OWN surface when it would have to be RANKED
        against an existing one.**

        Ranking is what masks. `scheduling_monitor.classify` returns
        `WORKER_DOWN` "FIRST, and above every cursor reading" — correct there,
        and it means a stalled cursor is unreportable while the worker is down.
        That is a fair trade for two axes that share a cause. It is not one
        here: "nothing posted" and "the clock stopped" are the pair that was
        *observed to disagree for sixteen days*, so whichever lost the ranking
        would be the one silenced, and this axis exists precisely because the
        other read healthy.

        The tempting discriminator — "it needs its own verdict, thresholds and
        state file" — does NOT hold, and is recorded as rejected so it is not
        reached for again: the worker axis has its own verdict states and two
        thresholds of its own, and was folded in anyway.

        NOTHING IS RAISED HERE, for the two reasons `/health/scheduling` gives
        verbatim: an alert whose sending is performed by the system it monitors
        cannot fire when that system is down, and the app's own notification
        routing has no writer, so an alert delivered there would vanish.

        Unauthenticated, so it answers in AGGREGATES ONLY — counts and ages,
        never a workspace, an account, a handle or a permalink.

        503 when the engine is absent: "posting is fine" and "I could not look"
        must never collapse, which is the whole subject of the issue this
        closes.
        """
        engine = app.state.engine
        if engine is None:
            raise HTTPException(
                status_code=503, detail="target database not configured"
            )
        # A DIRECT CONNECTION, not a unit of work, for the reason the route
        # above records: `UnitOfWork.__init__` refuses a blank tenant at
        # construction, and this aggregate is estate-wide and has no tenant.
        # Its cross-tenant reach rests on the owner bypassing RLS (#751), the
        # same footing and the same door to build when that is closed.
        async with engine.connect() as conn:
            posting = await posting_health.posting_freshness(conn)
            attempts = await posting_health.publish_attempts(conn)
            # `accounts_active` is CONTEXT for the alert text and never a gate:
            # a poller excused from speaking by a zero here would excuse an
            # empty tier forever, which is the first half of the outage this
            # endpoint exists for. The age beside it is the opposite — an
            # anchor that can only make the poller speak sooner.
            dests = await posting_health.destinations(conn)
            # Every key spelled in the service that computes it, so a rename
            # cannot leave the route publishing a name nothing produces.
            return {**posting, **attempts, **dests}

    return app


app = create_app()
