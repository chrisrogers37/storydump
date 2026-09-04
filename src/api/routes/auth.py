"""Sign-in, hosted on the API (`07` §1; #1015 §4 of the router design), and
the Drive connect leg's callback — the same shape with a different purpose.

`GET /auth/google` mints an anonymous `oauth_states` row and sends the browser
to Google; `GET /auth/google/callback` consumes that state one-shot, exchanges
the code server-side, verifies the ID token, upserts the identity keyed on the
subject, mints the opaque session and sets the cookie; `POST /auth/signout`
revokes it. One verifier, one credential, and no secret anywhere that could
mint a session for an arbitrary user — the reason this lives here and not on
the front end.

Two transactions bracket the provider call, never one around it (`02` §5):
the state is consumed and COMMITTED before Google is contacted, so a failed
exchange costs the person a fresh click and nothing else, and the write opens
afterwards. Both pre-auth endpoints debit the durable `preauth_ip` counter
(`05`: 30/min per client IP) in the same transaction as the state work, so a
refused request leaves no debit behind. That first transaction is
`_consume_callback`, written once for both callbacks.

`GET /auth/google-drive/callback` is the other half of
`POST /api/v1/workspaces/{ws}/sources/{id}/connect` (the gdrive epic, P3).
The state was minted for a signed-in admin and pins the workspace, the user
and the source, and **the state row is the only thing the callback trusts**:
it carries no session, a state minted for another leg is refused by name at
consume, and the credential is written inside a unit of work for THAT
workspace as THAT user, so the audit trigger names the actor and `p_tenant`
binds the row. Both legs' redirect URIs come from `google_client`.

Failures redirect to the front end's `/auth/error` with a closed ``reason``
(virgil's P3 already renders it) when `WEB_APP_URL` is set, and answer JSON
400 otherwise. Reasons: ``denied`` (the person or Google declined) ·
``missing_params`` · ``state_refused`` (unknown, expired, consumed, minted
for another leg, or the nonce cookie did not match) · ``exchange_failed`` ·
``identity_collision`` (sign-in: the verified email belongs to another
account — D35, never merged) · ``grant_incomplete`` (Drive: Google answered
with a grant the leg will not keep; `google_drive_oauth.REDIRECT_REASON` maps
each refusal) · ``already_connected`` (Instagram: the real account is already
another destination in this workspace). A Drive failure also carries
``flow=drive`` and an Instagram one ``flow=instagram``: the page is
sign-in-shaped by default and needs to know which leg it renders for.

`GET /auth/instagram-login/callback` is the other half of
`POST /api/v1/workspaces/{ws}/accounts/{id}/connect` (#1220 step 2): the same
shape as the Drive leg on the LEGACY flow's registered path, so the Meta app
needs no console change. The `07` §2 admin check runs again at the callback,
inside the write transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from src.api import google_client, instagram_client
from src.api.principal import (
    clear_session_cookie,
    presented_token,
    require_deliverable_session,
    require_engine,
    set_session_cookie,
)
from src.config.settings import settings
from src.exceptions.base import StorydumpError
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    google_drive_oauth,
    google_oidc,
    identity,
    ig_login_oauth,
    media_sync,
    provisioning,
    rate_counters,
    sessions,
    tenant_resolution,
)
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

#: The Drive leg's name on the error page (`flow=`); sign-in carries none.
DRIVE_FLOW = "drive"
#: The Instagram connect leg's (#1220 step 2).
INSTAGRAM_FLOW = "instagram"


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


def _fail(reason: str, *, flow: Optional[str] = None) -> Response:
    """The error page — or JSON 400 without a front end — with the leg named
    when it is not sign-in's."""
    params = {"reason": reason}
    if flow:
        params["flow"] = flow
    origin = settings.web_app_origin
    if origin:
        return RedirectResponse(
            f"{origin}/auth/error?{urlencode(params)}", status_code=302
        )
    content = {"detail": reason}
    if flow:
        content["flow"] = flow
    return JSONResponse(status_code=400, content=content)


def _landing(path: str = "/welcome") -> str:
    """Where a finished leg lands on the front end: sign-in on `/welcome`, the
    Drive connect on the settings screen the button that started it lives on.
    `/` without a front end."""
    origin = settings.web_app_origin
    return f"{origin}{path}" if origin else "/"


async def _consume_callback(
    request: Request,
    *,
    state: Optional[str],
    code: Optional[str],
    error: Optional[str],
    expected_provider: str,
    expected_purpose,
    cookie_nonce: Optional[str] = None,
    flow: Optional[str] = None,
) -> dict | Response:
    """The callback preamble both legs share: the provider's own error, the
    two required params, then the first transaction — the pre-auth debit and
    the one-shot consume, refusing BY NAME a state minted for another leg.
    Returns the consumed state row, or the failure response to send as-is."""
    engine = require_engine(request)
    if error:
        return _fail("denied", flow=flow)
    if not state or not code:
        return _fail("missing_params", flow=flow)
    label = {DRIVE_FLOW: "drive connect", INSTAGRAM_FLOW: "instagram connect"}.get(
        flow, "google sign-in"
    )
    async with engine.begin() as conn:
        await _preauth_guard(conn, request)
        try:
            return await consume_state(
                conn,
                state=state,
                cookie_nonce=cookie_nonce,
                expected_provider=expected_provider,
                expected_purpose=expected_purpose,
            )
        except OAuthStateRefused as exc:
            logger.warning("%s: state refused: %s", label, exc)
            return _fail("state_refused", flow=flow)


@router.get("/google")
async def google_signin(request: Request) -> Response:
    client_id, _, redirect_uri = google_client.configured(
        google_client.SIGNIN_CALLBACK_PATH
    )
    require_deliverable_session()
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
    client_id, client_secret, redirect_uri = google_client.configured(
        google_client.SIGNIN_CALLBACK_PATH
    )
    row = await _consume_callback(
        request,
        state=state,
        code=code,
        error=error,
        expected_provider=identity.PROVIDER_GOOGLE,
        expected_purpose="signin",
        cookie_nonce=request.cookies.get(NONCE_COOKIE),
    )
    if isinstance(row, Response):
        return row

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
    except StorydumpError as exc:
        # The message names the refusal; the token itself is never logged.
        logger.warning("google sign-in: exchange refused: %s", exc)
        return _fail("exchange_failed")

    engine = require_engine(request)
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
    write the credential — the sign-in callback's shape, trusting the state
    row alone (the module docstring has what that buys)."""
    client_id, client_secret, redirect_uri = google_client.configured(
        google_client.DRIVE_CALLBACK_PATH
    )
    row = await _consume_callback(
        request,
        state=state,
        code=code,
        error=error,
        expected_provider=google_drive_oauth.PROVIDER,
        expected_purpose={"connect", "reconnect"},
        flow=DRIVE_FLOW,
    )
    if isinstance(row, Response):
        return row
    if row["reconnect_target"] is None:
        # The schema still admits a target-less connect state (closing that
        # is a follow-up); this leg cannot act on one — the source IS what
        # the credential is for.
        logger.warning("drive connect: state names no source")
        return _fail("state_refused", flow=DRIVE_FLOW)

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
    except StorydumpError as exc:
        # The message names the refusal; no token rides in it. A Drive
        # refusal maps through the leg's own table; the floor's (host,
        # budget) carry no Drive reason and fall to `exchange_failed`.
        logger.warning("drive connect: exchange refused: %s", exc)
        return _fail(
            google_drive_oauth.REDIRECT_REASON.get(
                getattr(exc, "reason", None), "exchange_failed"
            ),
            flow=DRIVE_FLOW,
        )

    # The credential lands inside the state's own workspace, as the state's
    # user: the audit trigger names the actor, and `p_tenant` binds the row.
    uow = unit_of_work(
        require_engine(request),
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
        # F4 (a), in THIS transaction — `store_credential`'s contract. The
        # target is the state row's, never a client-supplied id: the callback
        # acts for the workspace and source the issue leg pinned.
        if row["reconnect_target"] is not None:
            await media_sync.rearm_after_connect(
                session,
                workspace_id=row["workspace_id"],
                source_id=row["reconnect_target"],
            )

    return RedirectResponse(
        _landing("/dashboard/settings?connected=gdrive"), status_code=302
    )


@router.get("/instagram-login/callback")
async def instagram_login_callback(
    request: Request,
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    """The Instagram connect leg's return (#1220 step 2): consume the state,
    check the returning browser is the one that started the flow, exchange
    the code for a long-lived token and its owner, land that identity on a
    destination — the one the state pinned, or (untargeted: the workspace-
    level ADD, owner ruling 2026-09-04) the row this account already has
    here or a new one — and write the credential. The Drive callback's shape
    plus the `07` §2 callback-time checks.

    **The state row is necessary and not sufficient.** It pins the user who
    started the flow; it does not prove the browser that returned is theirs.
    Without the session check below, an admin could mint a state, hand the
    authorization URL to someone else, and end up holding THAT person's
    Instagram token on their own destination. So the callback also requires
    the session cookie the API set at sign-in (it rides the top-level return
    navigation under SameSite=Lax) and refuses unless it resolves to the
    state's user — the same one-line rule as the admin re-check: what the
    issue leg established, the callback re-establishes."""
    app_id, app_secret, redirect_uri = instagram_client.configured()
    row = await _consume_callback(
        request,
        state=state,
        code=code,
        error=error,
        expected_provider=ig_login_oauth.PROVIDER,
        expected_purpose={"connect", "reconnect"},
        flow=INSTAGRAM_FLOW,
    )
    if isinstance(row, Response):
        return row

    presenter = await _presenting_user(request)
    if presenter is None or presenter != str(row["user_id"]):
        logger.warning(
            "instagram connect: the returning browser's session is not the"
            " state's user (presented=%s)",
            "none" if presenter is None else "other",
        )
        return _fail("state_refused", flow=INSTAGRAM_FLOW)

    # The provider calls sit between the two transactions, never inside one.
    try:
        async with httpx.AsyncClient() as client:
            grant = await ig_login_oauth.exchange_code(
                client,
                code=code,
                redirect_uri=redirect_uri,
                client_id=app_id,
                client_secret=app_secret,
            )
    except StorydumpError as exc:
        # Which of the three provider calls failed is in the log line; to the
        # person every one of them is "the last step did not complete".
        logger.warning("instagram connect: exchange refused: %s", exc)
        return _fail("exchange_failed", flow=INSTAGRAM_FLOW)

    workspace_id = str(row["workspace_id"])
    # Targeted: the state pinned the destination to connect or reconnect.
    # Untargeted: the workspace-level ADD (owner ruling 2026-09-04) — the
    # destination is whichever row this account already has here, or a new
    # scheduled one; either way the credential write below is the same.
    target = row["reconnect_target"]
    uow = unit_of_work(
        require_engine(request),
        workspace_id,
        actor_kind="user",
        actor_user_id=str(row["user_id"]),
        channel="web",
    )
    try:
        async with uow.begin() as session:
            # `07` §2: admin+ checked at issue AND at callback. The row pins
            # the workspace and the user; what can change between the two is
            # the membership, and a demoted admin's pending state must not
            # land a credential.
            await tenant_resolution.authorize_member(
                session, workspace_id, str(row["user_id"]), minimum_role="admin"
            )
            if target is None:
                account_id, _ = await provisioning.connect_destination(
                    session,
                    workspace_id=workspace_id,
                    provider_account_ref=grant.ig_user_id,
                    handle=grant.username,
                )
            else:
                account_id = str(target)
                await provisioning.attach_connected_identity(
                    session,
                    workspace_id=workspace_id,
                    ig_account_id=account_id,
                    provider_account_ref=grant.ig_user_id,
                    handle=grant.username,
                )
            # One write for connect AND reconnect: the upsert replaces an
            # existing credential in place (`07` §2 — same row id, no gap).
            await ig_login_oauth.store_credential(
                session,
                workspace_id=workspace_id,
                ig_account_id=account_id,
                token=grant.access_token,
                expires_at=grant.expires_at,
            )
    except TenantResolutionError as exc:
        logger.warning("instagram connect: callback authorization refused: %s", exc)
        return _fail("state_refused", flow=INSTAGRAM_FLOW)
    except provisioning.ProvisioningRefused as exc:
        logger.warning("instagram connect: attach refused: %s", exc)
        return _fail(
            _ATTACH_REASON.get(exc.reason, "state_refused"), flow=INSTAGRAM_FLOW
        )

    return RedirectResponse(
        _landing("/dashboard/settings?connected=instagram"), status_code=302
    )


#: `provisioning.attach_connected_identity`'s refusals (also raised through
#: `connect_destination`'s adopt path) on the error page. Each
#: is a DIFFERENT remedy, which is why they are not folded into `state_refused`
#: ("start again and it should work" is false for all three).
_ATTACH_REASON = {
    "duplicate_destination": "already_connected",
    "wrong_account": "wrong_account",
    "not_found": "destination_gone",
}


async def _presenting_user(request: Request) -> Optional[str]:
    """The user id of the session the returning browser carries, or None.

    Resolved exactly as `current_principal` resolves it (the same cookie, the
    same `sessions.resolve`), on the engine directly: this runs before any
    tenant is known. A refusal of any kind is None — the caller's answer is
    the same closed `state_refused` either way, so a prober learns nothing.
    """
    value = presented_token(request)
    if value is None:
        return None
    try:
        async with require_engine(request).begin() as conn:
            session = await sessions.resolve(
                conn, token_hash=sessions.token_hash(value)
            )
    except TenantResolutionError:
        return None
    return str(session.user_id)
