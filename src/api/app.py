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
- ``/webhooks/telegram`` — the W4 ingress route, wired for the `/start` door
  only (#1183). Chat-inbound resolution remains #854 and is NOT enabled
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

import os
import time
from typing import Mapping, Optional

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
from src.api.routes.webhooks import router as webhooks_router
from src.config.settings import settings
from src.exceptions.tenancy import TenantResolutionError
from src.services.target.commands import CommandNotBuilt, CommandRefused
from src.services.target.invitations import InvitationRefused
from src.services.target.provisioning import ProvisioningRefused
from src.services.target import scheduling_health
from src.services.target.unit_of_work import create_engine, engine_url_from_env
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
    return create_engine(url)


def _cors_origins() -> list[str]:
    """The ONE browser origin admitted (`settings.web_app_origin`); never "*",
    and with no origin configured no origin is admitted."""
    return [settings.web_app_origin] if settings.web_app_origin else []


def create_app(
    *, engine: Optional[AsyncEngine] = None, env: Optional[Mapping[str, str]] = None
) -> FastAPI:
    """Assemble the app. *engine* injects the target engine (tests, or a
    composition root that owns the pool); otherwise it comes from *env*
    (default: the process environment) and from nothing else."""
    app = FastAPI(
        title="Storydump API",
        description="Sign-in, reads and commands for the target tier",
        version=VERSION,
    )
    app.state.engine = (
        engine
        if engine is not None
        else _engine_from_env(os.environ if env is None else env)
    )
    # The W4 ingress seam, WIRED for the `/start` door only (#1183).
    #
    # ⚠ THIS DOES NOT MAKE CHAT-INBOUND WORK, and the distinction is the whole
    # reason #1183 was filed separately from #854. A `/start` payload carries
    # its own resolution — `link-<state>` names its user, `inv-<token>` names
    # its workspace — so both resolve against tables `svc_ingress` already
    # reaches pre-context (`oauth_states`, `user_identities`, `users` are all
    # role-scoped `USING (true)`; the `fn_invitation_accept` door is already
    # granted). An ORDINARY chat message carries neither and still needs
    # #854's resolver, because `channel_bindings` is GUC-filtered for
    # `svc_ingress` and there is no pre-context path to a workspace.
    #
    # Wired only when an engine exists: without one there is nothing to
    # `connect` to, and a runtime whose `connect` fails would convert the
    # route's honest 503 into a 500 mid-delivery.
    app.state.ingress = (
        webhooks.IngressRuntime(
            connect=app.state.engine.connect,
            dispatch=TelegramDispatcher(),
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
            return {**lag, "worker": await scheduling_health.worker_freshness(conn)}

    return app


app = create_app()
