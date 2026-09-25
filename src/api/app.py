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
  is visible from the probe instead of only from the first request; with
  ``/health/scheduling`` and ``/health/posting``, see `routes/health.py`.

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
import os
from contextlib import asynccontextmanager
from typing import Mapping, Optional

from sqlalchemy.exc import TimeoutError as PoolTimeout
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.api.routes.auth import router as auth_router
from src.api.routes.health import VERSION
from src.api.routes.health import router as health_router
from src.api.routes.retired import router as retired_router
from src.api.routes.v1 import IDEMPOTENCY_HEADER
from src.api.routes.v1 import router as v1_router
from src.api.routes.tokens import router as tokens_router
from src.api.routes.ops import router as ops_router
from src.api.routes import webhooks
from src.api.routes.meta import router as meta_router
from src.config.settings import settings
from src.exceptions.tenancy import TenantResolutionError, TokenRefused
from src.services.target import oauth_states
from src.services.target.commands import CommandNotBuilt, CommandRefused
from src.services.target.invitations import InvitationRefused
from src.services.target.category_mix import MixInvalid
from src.services.target.provisioning import ProvisioningRefused
from src.services.target.service_tokens import TokenArgsInvalid
from src.services.target.unit_of_work import (
    connection_role,
    create_engine,
    engine_url_from_env,
    INGRESS_POOL_TIMEOUT_SEAM,
    PoolWatch,
)
from src.services.target.vocabulary import DATABASE_URL_VAR
from src.services.target.telegram_dispatch import TelegramDispatcher
from src.services.target.webhook_ingress import AdmissionConflict, DeliveryReplayed
from src.utils.logger import logger


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


#: How often `_sample_webhook_live` re-reads what Telegram holds — the cadence
#: is the API's to choose, so it is stated here and handed to the channel's
#: `live_samples`. The CLI's `storydump health` reads the cached sample and
#: states this bound in its help; the number should be findable from both ends.
WEBHOOK_LIVE_SAMPLE_SECONDS = 60

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
    # The token resolver's three answers (phase 01 of the v2 CLI): like their
    # session twins, 401 and the response never says which.
    "invalid_token": 401,
    "expired_token": 401,
    "revoked_token": 401,
}
_TENANT_DETAIL = {401: "authentication required", 404: "not found", 403: "forbidden"}

#: `TokenRefused.reason` → 403 WITH the reason: a live token asked for
#: something it may not have, and the CLI says the right sentence only if the
#: answer names it. Pinned total over `TokenRefused.REASONS` by the factory
#: test.
_TOKEN_STATUS = {
    "session_required": 403,
    "readonly_token": 403,
    "wrong_workspace": 403,
}

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


def _reason_detail(exc, status: int) -> dict:
    """The ordinary refusal body: the message, and the machine-routable
    reason the web's `target-api.ts::readError` matches on."""
    return {"detail": str(exc), "reason": exc.reason}


def _tenant_detail(exc, status: int) -> dict:
    """No `reason` on the wire: the web surface must not learn which of
    "not a member", "no such workspace" and "disabled" it met (`07` §5, no
    existence oracle). The detail is keyed by the STATUS, not the reason."""
    return {"detail": _TENANT_DETAIL[status]}


def _invitation_detail(exc, status: int) -> dict:
    """The invitation's own sentence, keyed by reason — `str(exc)` is the
    service's wording and this table is the surface's."""
    return {"detail": _INVITATION_DETAIL[exc.reason], "reason": exc.reason}


def _command_body(exc, status: int) -> dict:
    # `CommandNotBuilt` is the vocabulary's "known, not built yet": the body
    # names the command so the caller can tell it from a typo, and its reason
    # is the fixed `not_built` rather than the exception's message.
    if isinstance(exc, CommandNotBuilt):
        return {"command": exc.command, "detail": "not built", "reason": "not_built"}
    return _reason_detail(exc, status)


def _mapped(table: dict, content=_reason_detail, *, log: bool = True):
    """A handler for a refusal whose `reason` this table maps to a status.

    The five handlers below had each written out the same decision — look
    the reason up, fall through to `_unmapped` when it is not there (which
    is the pin `TestRefusalMappingsAreTotal` exists to keep honest), then
    answer. What actually varies is the body and whether the refusal is
    logged, so those are the arguments; everything else is this function.
    """

    async def handler(request: Request, exc):
        status = table.get(exc.reason)
        if status is None:
            return _unmapped(request, exc)
        if log:
            logger.info("refused %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=status, content=content(exc, status))

    return handler


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

    app.add_exception_handler(
        TenantResolutionError, _mapped(_TENANT_STATUS, _tenant_detail)
    )

    app.add_exception_handler(TokenRefused, _mapped(_TOKEN_STATUS, _reason_detail))

    @app.exception_handler(TokenArgsInvalid)
    async def _token_args(request: Request, exc: TokenArgsInvalid):
        return JSONResponse(
            status_code=400, content={"detail": str(exc), "reason": "invalid_args"}
        )

    app.add_exception_handler(
        CommandRefused, _mapped(_COMMAND_STATUS, _command_body, log=False)
    )

    app.add_exception_handler(
        ProvisioningRefused, _mapped(_PROVISIONING_STATUS, _reason_detail)
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

    app.add_exception_handler(
        InvitationRefused, _mapped(_INVITATION_STATUS, _invitation_detail, log=False)
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
            f"{DATABASE_URL_VAR} is unset or blank: the API has no target engine "
            "and every data route answers 503 until it is configured"
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
    """Cache the startup registration's report on `app.state.webhook` for
    `/health` — `reg.register_at_startup` decides it and never raises, so this
    is the whole of the API's part: hand it the bot transport this process
    speaks with, and keep what it says."""
    from src.channels import telegram_webhook_registration as reg

    app.state.webhook = await reg.register_at_startup(
        env, transport_factory=_telegram_transport
    )


async def _sample_webhook_live(app: FastAPI, env: Mapping[str, str]) -> None:
    """Cache each live webhook sample on `app.state.webhook_live` for `/health`.
    The loop, its cadence and its never-raise rule are `reg.live_samples`; this
    task exists to hold the latest one where the probe can read it, and is
    cancelled at shutdown like the other two."""
    from src.channels import telegram_webhook_registration as reg

    async for sample in reg.live_samples(
        env,
        transport_factory=_telegram_transport,
        interval=WEBHOOK_LIVE_SAMPLE_SECONDS,
    ):
        app.state.webhook_live = sample


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


def _require_key_ring() -> None:
    """Build the credential key ring, or refuse to start.

    The connect callbacks encrypt every Instagram token and Drive grant
    through it (`oauth_states.ring`). Built lazily, a missing or malformed key
    passed Railway's health check and failed the first connect — after the
    person had already granted access at Meta or Google. A missing engine is
    answered differently (503 per data route, `/health` saying so) because it
    is visible from the probe; a missing key is visible nowhere until someone
    connects. Raised from startup, uvicorn exits, the deploy fails its check,
    and the previous deploy keeps serving.
    """
    try:
        oauth_states.ring()
    except oauth_states.RingUnavailable as exc:
        logger.error(
            "the credential key ring cannot load (%s): the connect callbacks"
            " encrypt every token through it; set a valid ENCRYPTION_KEY on this"
            " service. Refusing to start.",
            exc,
        )
        raise


def _cors_origins() -> list[str]:
    """The ONE browser origin admitted (`settings.web_app_origin`); never "*",
    and with no origin configured no origin is admitted."""
    return [settings.web_app_origin] if settings.web_app_origin else []


def _telegram_transport(env: Mapping[str, str]):
    """The one bot transport the API speaks with — the `/start` door's
    acknowledgement and a tap's answer (the card's edit is the outbox's) — or None
    without the bot token (the same variable the worker sends with, so the API
    never holds a second credential for the one bot)."""
    from src.channels import telegram_webhook_registration as reg

    token = env.get(reg.TOKEN_VAR)
    if not token:
        return None
    from src.channels.telegram_transport import transport_from_env

    return transport_from_env(token, env)


def create_app(
    *, engine: Optional[AsyncEngine] = None, env: Optional[Mapping[str, str]] = None
) -> FastAPI:
    """Assemble the app. *engine* injects the target engine (tests, or a
    composition root that owns the pool); otherwise it comes from *env*
    (default: the process environment) and from nothing else."""
    # Resolved once: `os.environ` is a live mapping, so binding it here reads
    # exactly what reading it per call site read — five spellings of one
    # decision, and `create_app(env=…)`'s seam is now a single place. The
    # `_lifespan` closure below captures the resolved mapping, not the
    # decision.
    env = os.environ if env is None else env

    @asynccontextmanager
    async def _lifespan(app_: FastAPI):
        # First, so a process that cannot encrypt starts no task — in
        # particular it never re-registers the production bot's webhook.
        _require_key_ring()
        # The role sample and the webhook registration run as background
        # tasks so startup never waits on the database or on Telegram (see
        # `app.state.db_role` / `app.state.webhook` below); a task still
        # pending at shutdown is cancelled rather than left to die with the loop.
        tasks = [
            asyncio.create_task(_sample_db_role(app_)),
            asyncio.create_task(_register_webhook(app_, env)),
            asyncio.create_task(_sample_webhook_live(app_, env)),
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
    app.state.engine = engine if engine is not None else _engine_from_env(env)
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
    bot = _telegram_transport(env)
    app.state.tap_metrics = webhooks.TapMetrics()
    app.state.ingress_workers = _ingress_workers(env)
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
    # The token routes and `/me/principal` (phase 01 of the v2 CLI) share the
    # v1 prefix and the v1 seams; their own file keeps the allowlist readable.
    app.include_router(tokens_router, prefix="/api/v1")
    # The read views (phase 02 of the v2 CLI, fork F6): one file for the
    # operator surface, tenant-scoped, admitted to tokens.
    app.include_router(ops_router, prefix="/api/v1")
    app.include_router(webhooks.router, prefix="/webhooks")
    # Meta's policy callbacks (#410). Under the same prefix as the other
    # provider-called doors; the URLs are not registered with Meta yet.
    app.include_router(meta_router, prefix="/webhooks/meta")
    # The Mini App's URL is baked into buttons real users still hold; it
    # redirects rather than 404s (`routes/retired.py`).
    app.include_router(retired_router)
    # Railway's probe and the two dependency-touching axes (`routes/health.py`).
    # Included LAST, where the three used to be written out inline, so the
    # route table keeps the order it has always had.
    app.include_router(health_router)

    return app


app = create_app()
