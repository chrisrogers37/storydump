"""Sign-in, hosted on the API (`07` §1; #1015 §4 of the router design).

`GET /auth/google` mints an anonymous `oauth_states` row and sends the browser
to Google; `GET /auth/google/callback` consumes that state one-shot, exchanges
the code server-side, verifies the ID token, upserts the identity keyed on the
subject, mints the opaque session and sets the cookie; `POST /auth/signout`
revokes it. One verifier, one credential, and no secret anywhere that could
mint a session for an arbitrary user — the reason this lives here and not on
the front end.

Two transactions bracket the provider call, never one around it (`02` §5):
the state is consumed and COMMITTED before Google is contacted, so a failed
exchange costs the person a fresh click and nothing else, and the identity +
session write opens afterwards. Both pre-auth endpoints debit the durable
`preauth_ip` counter (`05`: 30/min per client IP) in the same transaction as
the state work, so a refused request leaves no debit behind.

Failures redirect to the front end's `/auth/error` with a closed ``reason``
(virgil's P3 already renders it) when `WEB_APP_URL` is set, and answer JSON
400 otherwise. Reasons: ``denied`` (the person or Google declined) ·
``missing_params`` · ``state_refused`` (unknown, expired, consumed, or the
nonce cookie did not match) · ``exchange_failed`` · ``identity_collision``
(the verified email belongs to another account — D35, never merged).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from src.api.principal import (
    clear_session_cookie,
    presented_token,
    require_engine,
    set_session_cookie,
)
from src.config.settings import settings
from src.services.target import google_oidc, identity, rate_counters, sessions
from src.services.target.egress import EgressRefused, StorydumpError
from src.services.target.ig_login_oauth import (
    STATE_TTL_SECONDS,
    OAuthStateRefused,
    consume_state,
    issue_state,
    new_state,
)
from src.utils.logger import logger

router = APIRouter(tags=["auth"])

#: The CSRF double-submit cookie for the anonymous sign-in state (`07` §2).
#: Scoped to the sign-in path so it rides along to the callback and nowhere
#: else; Lax is what lets a top-level navigation back from Google carry it.
NONCE_COOKIE = "sd_oauth_nonce"
NONCE_COOKIE_PATH = "/auth/google"

#: `05`: pre-auth admission, 30/min per client IP, scope `preauth_ip`.
PREAUTH_LIMIT = 30
PREAUTH_WINDOW_SECONDS = 60
PREAUTH_SCOPE = "preauth_ip"


def _configured() -> tuple[str, str, str]:
    """(client_id, client_secret, redirect_uri), or a 503 that names what is
    missing. Sign-in that is not configured refuses; it never half-works."""
    missing = [
        name
        for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OAUTH_REDIRECT_BASE_URL")
        if not getattr(settings, name, None)
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"google sign-in not configured: set {', '.join(missing)}",
        )
    base = settings.OAUTH_REDIRECT_BASE_URL.rstrip("/")
    return (
        settings.GOOGLE_CLIENT_ID,
        settings.GOOGLE_CLIENT_SECRET,
        f"{base}/auth/google/callback",
    )


def _client_ip(request: Request) -> str:
    """The attributed peer — `request.client.host` AFTER ProxyHeadersMiddleware
    has applied the trusted-proxy walk (#726/#765), which is the `02` §6
    client-IP source rule. Never a header read here."""
    return request.client.host if request.client else "unknown"


async def _preauth_guard(conn, request: Request) -> None:
    now = datetime.now(timezone.utc)
    count = await rate_counters.increment(
        conn,
        scope=PREAUTH_SCOPE,
        key=_client_ip(request),
        window_start=rate_counters.window_start(now, PREAUTH_WINDOW_SECONDS),
        limit=PREAUTH_LIMIT,
    )
    if count is None:
        raise HTTPException(status_code=429, detail="too many sign-in attempts")


def _fail(reason: str) -> Response:
    if settings.WEB_APP_URL:
        query = urlencode({"reason": reason})
        return RedirectResponse(
            f"{settings.WEB_APP_URL.rstrip('/')}/auth/error?{query}", status_code=302
        )
    return JSONResponse(status_code=400, content={"detail": reason})


def _landing() -> str:
    if settings.WEB_APP_URL:
        return f"{settings.WEB_APP_URL.rstrip('/')}/welcome"
    return "/"


@router.get("/google")
async def google_signin(request: Request) -> Response:
    client_id, _, redirect_uri = _configured()
    engine = require_engine(request)
    cookie_nonce = new_state()
    async with engine.begin() as conn:
        await _preauth_guard(conn, request)
        state = await issue_state(
            conn,
            purpose="signin",
            provider=google_oidc.PROVIDER,
            cookie_nonce=cookie_nonce,
        )
    response = RedirectResponse(
        google_oidc.authorization_url(
            client_id=client_id, redirect_uri=redirect_uri, state=state
        ),
        status_code=302,
    )
    response.set_cookie(
        NONCE_COOKIE,
        cookie_nonce,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        path=NONCE_COOKIE_PATH,
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    client_id, client_secret, redirect_uri = _configured()
    engine = require_engine(request)
    if error:
        return _fail("denied")
    if not state or not code:
        return _fail("missing_params")

    cookie_nonce = request.cookies.get(NONCE_COOKIE)
    async with engine.begin() as conn:
        await _preauth_guard(conn, request)
        try:
            await consume_state(conn, state=state, cookie_nonce=cookie_nonce)
        except OAuthStateRefused as exc:
            logger.warning("google sign-in: state refused: %s", exc)
            return _fail("state_refused")

    # The provider call sits between the two transactions, never inside one.
    try:
        async with httpx.AsyncClient() as client:
            id_token = await google_oidc.exchange_code(
                client,
                code=code,
                redirect_uri=redirect_uri,
                client_id=client_id,
                client_secret=client_secret,
            )
        who = google_oidc.verify_id_token(
            google_oidc.decode_id_token(id_token),
            client_id=client_id,
            nonce=google_oidc.nonce_for(state),
        )
    except (google_oidc.OidcRefused, EgressRefused, StorydumpError) as exc:
        # The message names the refusal; the token itself is never logged.
        logger.warning("google sign-in: exchange refused: %s", exc)
        return _fail("exchange_failed")

    async with engine.begin() as conn:
        try:
            user_id = await identity.upsert_google_identity(
                conn,
                sub=who.sub,
                email=who.email,
                email_verified=who.email is not None,
                display_name=who.display_name,
            )
        except identity.IdentityCollision:
            return _fail("identity_collision")
        value = await sessions.issue(conn, user_id=user_id)

    response = RedirectResponse(_landing(), status_code=302)
    set_session_cookie(response, value)
    response.delete_cookie(NONCE_COOKIE, path=NONCE_COOKIE_PATH)
    return response


@router.post("/signout")
async def signout(request: Request) -> Response:
    """Revocation is the logout (`session_tokens.revoked_at`); clearing the
    cookie is a courtesy. No principal required: an already-dead session is
    signed out the same way, and nothing is disclosed either way."""
    engine = require_engine(request)
    value = presented_token(request)
    if value is not None:
        async with engine.begin() as conn:
            await sessions.revoke(conn, token_hash=sessions.token_hash(value))
    response = JSONResponse({"signed_out": True})
    clear_session_cookie(response)
    return response
