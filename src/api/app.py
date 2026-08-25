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
- ``/webhooks/telegram`` — the dormant W4 ingress route, unchanged; W4 arms it
  (`TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` + `app.state.ingress`) rather than
  writing a new one.
- ``/health`` — Railway's probe (`railway.toml`), which now also says whether
  a target engine is configured, so a service that would 503 every data route
  is visible from the probe instead of only from the first request.

What deliberately does not exist any more: the legacy ``/auth`` OAuth router,
the ``/api/onboarding`` router and its Mini App (`/webapp/onboarding`,
`/static`), all of which read the legacy schema that M.3 froze; the in-memory
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

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.api.routes.auth import router as auth_router
from src.api.routes.v1 import IDEMPOTENCY_HEADER
from src.api.routes.v1 import router as v1_router
from src.api.routes.webhooks import router as webhooks_router
from src.config.settings import settings
from src.exceptions.tenancy import TenantResolutionError
from src.services.target.commands import CommandNotBuilt, CommandRefused
from src.services.target.unit_of_work import create_engine, engine_url_from_env
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


#: `TenantResolutionError.reason` → status. Session reasons are 401 and the
#: body does not say which (the distinct reasons exist for the log); a
#: workspace the caller cannot see is 404, never 403 — the same 404 a
#: workspace that does not exist gets (`07` §5); below the floor is 403.
_TENANT_STATUS = {
    "invalid_session": 401,
    "expired_session": 401,
    "revoked_session": 401,
    "disabled_user": 401,
    "not_a_member": 404,
    "insufficient_role": 403,
}
_TENANT_DETAIL = {401: "authentication required", 404: "not found", 403: "forbidden"}

#: `CommandRefused.reason` → status.
_COMMAND_STATUS = {
    "unknown_command": 404,
    "not_built": 501,
    "workspace_required": 400,
    "invalid_args": 400,
    "not_found": 404,
    "illegal_transition": 409,
    "manual_mode": 409,
}


def _register_handlers(app: FastAPI) -> None:
    """Service refusals → HTTP, once. No route speaks a status for these."""

    @app.exception_handler(TenantResolutionError)
    async def _tenant(request: Request, exc: TenantResolutionError):
        status = _TENANT_STATUS.get(exc.reason, 400)
        logger.info("refused %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=status,
            content={"detail": _TENANT_DETAIL.get(status, str(exc))},
        )

    @app.exception_handler(CommandRefused)
    async def _command(request: Request, exc: CommandRefused):
        status = _COMMAND_STATUS.get(exc.reason, 400)
        content = {"detail": str(exc), "reason": exc.reason}
        if isinstance(exc, CommandNotBuilt):
            content = {"command": exc.command, "detail": "not built", "reason": "not_built"}
        return JSONResponse(status_code=status, content=content)

    @app.exception_handler(DeliveryReplayed)
    async def _replayed(request: Request, exc: DeliveryReplayed):
        # Acknowledged WITHOUT re-execution — the same key and the same body.
        return JSONResponse(status_code=200, content={"outcome": "replayed"})

    @app.exception_handler(AdmissionConflict)
    async def _conflict(request: Request, exc: AdmissionConflict):
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"{IDEMPOTENCY_HEADER} reused with a different body",
                "reason": "idempotency_conflict",
            },
        )

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError):
        # The database's CHECKs are the authority on values (`workspaces.py`):
        # a check violation is the caller's 400, a unique/FK violation a 409.
        cause = getattr(exc.orig, "__cause__", None)
        constraint = getattr(cause, "constraint_name", None) or ""
        is_check = type(cause).__name__ == "CheckViolationError"
        return JSONResponse(
            status_code=400 if is_check else 409,
            content={
                "detail": (
                    f"invalid value ({constraint})" if is_check else f"conflict ({constraint})"
                ),
                "reason": "invalid_args" if is_check else "conflict",
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
    """The ONE browser origin admitted, from `WEB_APP_URL`; never "*", and
    with no origin configured no origin is admitted."""
    return [settings.WEB_APP_URL.rstrip("/")] if settings.WEB_APP_URL else []


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
    # The W4 ingress resolution seam (#854), unchanged: None means the webhook
    # route refuses every delivery BEFORE admitting it.
    app.state.ingress = None

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

    return app


app = create_app()
