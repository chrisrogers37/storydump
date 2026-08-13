"""FastAPI application for Storydump OAuth flows and Mini App."""

import time
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.api.rate_limit import limiter
from src.api.routes.oauth import router as oauth_router
from src.api.routes.onboarding import router as onboarding_router
from src.config.settings import settings
from src.utils.logger import logger


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to every response.

    Telegram Mini Apps load inside Telegram's webview/iframe, so /webapp/,
    /api/onboarding/, and /static/onboarding/ paths need relaxed CSP:
    - script-src must allow https://telegram.org for the WebApp SDK
    - frame-ancestors must allow Telegram's web client domains
    Without the SDK loading, Telegram.WebApp.ready() never fires and
    the native loading overlay stays on top — freezing all interaction.
    """

    _MINI_APP_PREFIXES = ("/webapp/", "/api/onboarding/", "/static/onboarding/")

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        if request.url.path.startswith(self._MINI_APP_PREFIXES):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://telegram.org; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
            )
        else:
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
    shared edge IP (pooled, still rate-limited) beats failing open to an
    attacker-chosen identity. See #765.
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
            scope["headers"] = [
                (k, v) for k, v in headers if k.lower() != self._XFF
            ]

        await self.app(scope, receive, send)


_START_TIME = time.time()

app = FastAPI(
    title="Storydump API",
    description="OAuth and API endpoints for Storydump",
    version="0.1.0",
)

# Security headers — HSTS, CSP, X-Frame-Options, X-Content-Type-Options
app.add_middleware(SecurityHeadersMiddleware)

# Proxy headers — trust X-Forwarded-For/Proto from the edge that fronts us, so
# request.client.host returns the real client IP rather than the proxy's.
#
# Named hosts, never "*": under the wildcard uvicorn honours the header from any
# peer AND takes its leftmost entry, which the caller writes. Every IP-keyed
# control downstream — the limiter's key_func, auth_monitor's failure buckets —
# then partitions on a value the caller chooses. See TRUSTED_PROXY_HOSTS (#726).
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_hosts)

# Ambiguous X-Forwarded-For — drop it rather than guess which instance is real.
#
# ProxyHeadersMiddleware reads headers via dict(scope["headers"]), which keeps
# only the LAST of a repeated header name. A caller who sends X-Forwarded-For
# as two headers instead of one comma-joined value can make dict() discard
# the edge's real value and keep their own — bypassing the fix above. Must
# run before ProxyHeadersMiddleware on the request path, so it is added AFTER
# it here: Starlette's add_middleware prepends, so the middleware added last
# runs first. See #765.
app.add_middleware(DropAmbiguousForwardedForMiddleware)

# Rate limiting — 30 req/min per IP global default (see src/api/rate_limit.py)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS middleware — restrict to our own domain in production
_cors_origins = (
    [settings.OAUTH_REDIRECT_BASE_URL]
    if settings.OAUTH_REDIRECT_BASE_URL
    else ["http://localhost:8000"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

# Static files for Mini App
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Register routes
app.include_router(oauth_router, prefix="/auth")
app.include_router(onboarding_router, prefix="/api/onboarding")


@app.get("/health")
async def health_check():
    """Health check endpoint for Railway. No auth required."""
    return {
        "status": "ok",
        "version": app.version,
        "uptime_seconds": int(time.time() - _START_TIME),
    }


@app.get("/webapp/onboarding")
async def serve_onboarding_webapp():
    """Serve the onboarding Mini App HTML."""
    html_path = STATIC_DIR / "onboarding" / "index.html"
    if not html_path.exists():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Onboarding app not found")
    return FileResponse(str(html_path), media_type="text/html")
