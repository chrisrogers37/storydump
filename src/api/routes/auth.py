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
(the verified email belongs to another account — D35, never merged) ·
``grant_incomplete`` (Drive only: the grant came back without a refresh token
or without `drive.readonly`, and was refused rather than stored).

`GET /auth/google-drive/callback` is the Drive connect leg's other half (the
gdrive epic, P3): the same two-transaction bracket around the provider call,
but the state was minted by `POST /api/v1/workspaces/{ws}/sources/{id}/connect`
for a signed-in admin, so it pins the workspace, the user and the source, and
the credential is written inside a unit of work for THAT workspace as THAT
user — the callback itself carries no session and trusts only the row. Its
redirect URI is the legacy flow's, byte for byte, so the Google client already
registers it.
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
from src.services.target import (
    google_drive_oauth,
    google_oidc,
    identity,
    rate_counters,
    sessions,
)
from src.services.target.egress import EgressRefused, StorydumpError
from src.services.target.ig_login_oauth import (
    STATE_TTL_SECONDS,
    OAuthStateRefused,
    consume_state,
    issue_state,
    new_state,
)
from src.services.target.unit_of_work import unit_of_work
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


#: Where Google sends the browser back for each leg. ONE Google client serves
#: both (`GOOGLE_CLIENT_ID`), so both URIs are registered on it; the Drive one
#: is the legacy flow's path unchanged.
SIGNIN_CALLBACK_PATH = "/auth/google/callback"
DRIVE_CALLBACK_PATH = "/auth/google-drive/callback"


def _configured(callback_path: str = SIGNIN_CALLBACK_PATH) -> tuple[str, str, str]:
    """(client_id, client_secret, redirect_uri), or a 503 that names what is
    missing. A leg that is not configured refuses; it never half-works."""
    missing = [
        name
        for name in (
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "OAUTH_REDIRECT_BASE_URL",
        )
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
        f"{base}{callback_path}",
    )


def drive_configured() -> tuple[str, str, str]:
    """The Drive connect leg's (client_id, client_secret, redirect_uri) — the
    same client as sign-in, the Drive callback. Read by the connect route in
    `v1.py` and by the callback below, so the two cannot disagree on the URI."""
    return _configured(DRIVE_CALLBACK_PATH)


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
    origin = settings.web_app_origin
    if origin:
        return RedirectResponse(
            f"{origin}/auth/error?{urlencode({'reason': reason})}", status_code=302
        )
    return JSONResponse(status_code=400, content={"detail": reason})


def _landing() -> str:
    origin = settings.web_app_origin
    return f"{origin}/welcome" if origin else "/"


def _connected_landing() -> str:
    """Where a finished Drive connect lands: the settings screen, which is
    where the button that started it lives."""
    origin = settings.web_app_origin
    return f"{origin}/dashboard/settings?connected=gdrive" if origin else "/"


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
            provider=identity.PROVIDER_GOOGLE,
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
                conn, sub=who.sub, email=who.email, display_name=who.display_name
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


@router.get("/google-drive/callback")
async def google_drive_callback(
    request: Request,
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    """The Drive connect leg's return: consume the state, exchange the code,
    write the credential — the gdrive epic's P3, in the sign-in callback's
    shape. The state row is the ONLY thing trusted: it names the workspace,
    the user and the source, and a state minted for any other purpose or
    provider (a sign-in state replayed here, say) is refused as such."""
    client_id, client_secret, redirect_uri = drive_configured()
    engine = require_engine(request)
    if error:
        return _fail("denied")
    if not state or not code:
        return _fail("missing_params")

    async with engine.begin() as conn:
        await _preauth_guard(conn, request)
        try:
            row = await consume_state(conn, state=state)
        except OAuthStateRefused as exc:
            logger.warning("drive connect: state refused: %s", exc)
            return _fail("state_refused")
    if (
        row["provider"] != google_drive_oauth.PROVIDER
        or row["purpose"] not in ("connect", "reconnect")
        or row["reconnect_target"] is None
    ):
        logger.warning("drive connect: state is not a drive connect state")
        return _fail("state_refused")

    # The provider call sits between the two transactions, never inside one.
    try:
        async with httpx.AsyncClient() as client:
            grant = await google_drive_oauth.exchange_code(
                client,
                code=code,
                redirect_uri=redirect_uri,
                client_id=client_id,
                client_secret=client_secret,
            )
    except google_drive_oauth.DriveOAuthRefused as exc:
        logger.warning("drive connect: grant refused: %s", exc)
        incomplete = exc.reason in ("no_refresh_token", "scope_not_granted")
        return _fail("grant_incomplete" if incomplete else "exchange_failed")
    except (EgressRefused, StorydumpError) as exc:
        logger.warning("drive connect: exchange refused: %s", exc)
        return _fail("exchange_failed")

    # The credential lands inside the state's own workspace, as the state's
    # user: the audit trigger names the actor, and `p_tenant` binds the row.
    uow = unit_of_work(
        engine,
        str(row["workspace_id"]),
        actor_kind="user",
        actor_user_id=str(row["user_id"]),
        channel="web",
    )
    async with uow.begin() as session:
        await google_drive_oauth.store_credential(
            session,
            workspace_id=row["workspace_id"],
            media_source_id=row["reconnect_target"],
            grant=grant,
        )
        # P4 (epic F4 (a)) re-arms the source HERE, in this same transaction:
        # state='active', alerted_at=NULL, next_sync_at=now().

    return RedirectResponse(_connected_landing(), status_code=302)
