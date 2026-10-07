"""Sign-in, hosted on the API (`07` §1; #1015 §4 of the router design), and
the Drive connect leg's callback — the same shape with a different purpose.

`GET /auth/google` mints an anonymous `oauth_states` row and sends the browser
to Google; `GET /auth/google/callback` consumes that state one-shot, exchanges
the code server-side, verifies the ID token, upserts the identity keyed on the
subject, mints the opaque session and sets the cookie; `POST /auth/signout`
revokes it (``?everywhere=true``: every live session of that user). One
verifier, one credential, and no secret anywhere that could mint a session
for an arbitrary user — the reason this lives here and not on the front end.

Two transactions bracket the provider call, never one around it (`02` §5):
the state is consumed and COMMITTED before Google is contacted, so a failed
exchange costs the person a fresh click and nothing else, and the write opens
afterwards. Both pre-auth endpoints debit the durable `preauth_ip` counter
(`05`: 30/min per client IP) in the same transaction as the state work, so a
refused request leaves no debit behind. That first transaction is
`_consume_callback`, written once for both callbacks.

`GET /auth/google-drive/callback` is the other half of
`POST /api/v1/workspaces/{ws}/sources/{id}/connect` (the gdrive epic, P3).
The state was minted for a signed-in admin and pins the workspace and the
user. A state minted for another leg is refused by name at consume; the
returning browser must carry the session of the state's user, and that user
must still be an admin, checked again inside the write; the credential is
written inside a unit of work for THAT workspace as THAT user, so the audit
trigger names the actor and `p_tenant` binds the row. Both legs' redirect URIs come from `google_client`.

Failures redirect to the front end's `/auth/error` with a closed ``reason``
(virgil's P3 already renders it) when `WEB_APP_URL` is set, and answer JSON
400 otherwise. Reasons: ``denied`` (the person or Google declined) ·
``missing_params`` · ``state_refused`` (unknown, expired, consumed, minted
for another leg, or the nonce cookie did not match) · ``exchange_failed`` ·
``identity_collision`` (sign-in: the verified email belongs to another
account — D35, never merged) · ``not_admitted`` (sign-in: a new account whose
email nobody admitted or invited, 092 — the one refusal that lands on
`/login?error=not_admitted`, beside the waitlist, rather than on this page) ·
``grant_incomplete`` (Drive: Google answered
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

from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from src.api import google_client, instagram_client
from src.api import principal as principal_mod
from src.api.principal import (
    clear_session_cookie,
    preauth_guard,
    presented_token,
    require_deliverable_session,
    require_engine,
    require_same_origin,
    set_session_cookie,
)
from src.config.settings import settings
from src.exceptions.base import StorydumpError
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    commands,
    google_drive_oauth,
    google_oidc,
    identity,
    ig_login_oauth,
    media_sync,
    provisioning,
    sessions,
    tenant_resolution,
)
from src.services.target.oauth_states import (
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

#: The pre-auth guard's 429 (`principal.preauth_guard`).
SIGNIN_LIMITED = "too many sign-in attempts"

#: The Drive leg's name on the error page (`flow=`); sign-in carries none.
DRIVE_FLOW = "drive"
#: The Instagram connect leg's (#1220 step 2).
INSTAGRAM_FLOW = "instagram"


def _refuse(path: str, key: str, reason: str, **extra: str) -> Response:
    """A refusal on the front end's *path* as `?<key>=<reason>` — or, without
    a front end, JSON 400 with the reason as `detail`."""
    origin = settings.web_app_origin
    if origin:
        query = urlencode({key: reason, **extra})
        return RedirectResponse(f"{origin}{path}?{query}", status_code=302)
    return JSONResponse(status_code=400, content={"detail": reason, **extra})


def _fail(reason: str, *, flow: Optional[str] = None) -> Response:
    """The error page — or JSON 400 without a front end — with the leg named
    when it is not sign-in's."""
    return _refuse("/auth/error", "reason", reason, **({"flow": flow} if flow else {}))


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
    require_presenter: bool = False,
) -> dict | Response:
    """The callback preamble every leg shares: the provider's own error, the
    two required params, then the first transaction — the pre-auth debit and
    the one-shot consume, refusing BY NAME a state minted for another leg.
    Returns the consumed state row, or the failure response to send as-is.

    *require_presenter* is the connect legs' rule: **the state row is
    necessary and not sufficient.** It pins the user who started the flow, and
    the returning browser must carry the session cookie the API set at sign-in
    (it rides the top-level return navigation under SameSite=Lax), resolving
    to that same user — refused before the code is spent."""
    engine = require_engine(request)
    if error:
        return _fail("denied", flow=flow)
    if not state or not code:
        return _fail("missing_params", flow=flow)
    label = {DRIVE_FLOW: "drive connect", INSTAGRAM_FLOW: "instagram connect"}.get(
        flow, "google sign-in"
    )
    async with engine.begin() as conn:
        await preauth_guard(conn, request, detail=SIGNIN_LIMITED)
        try:
            row = await consume_state(
                conn,
                state=state,
                cookie_nonce=cookie_nonce,
                expected_provider=expected_provider,
                expected_purpose=expected_purpose,
            )
        except OAuthStateRefused as exc:
            logger.warning("%s: state refused: %s", label, exc)
            return _fail("state_refused", flow=flow)
    if require_presenter:
        presenter = await _presenting_user(request)
        if presenter is None or presenter != str(row["user_id"]):
            logger.warning(
                "%s: the returning browser's session is not the state's user"
                " (presented=%s)",
                label,
                "none" if presenter is None else "other",
            )
            return _fail("state_refused", flow=flow)
    return row


@router.get("/google")
async def google_signin(request: Request) -> Response:
    client_id, _, redirect_uri = google_client.configured(
        google_client.SIGNIN_CALLBACK_PATH
    )
    require_deliverable_session()
    engine = require_engine(request)
    cookie_nonce = new_state()
    async with engine.begin() as conn:
        await preauth_guard(conn, request, detail=SIGNIN_LIMITED)
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
                conn,
                sub=who.sub,
                email=who.email,
                display_name=who.display_name,
                signup_open=settings.TARGET_SIGNUP_OPEN,
            )
        except identity.SignupNotAdmitted:
            logger.info("google sign-in: a new account was not admitted")
            # Sign-up is gated (092): /login says so and points at the waitlist.
            return _refuse("/login", "error", "not_admitted")
        except identity.IdentityCollision:
            return _fail("identity_collision")
        value = await sessions.issue(conn, user_id=user_id)

    response = RedirectResponse(_landing(), status_code=302)
    set_session_cookie(response, value)
    response.delete_cookie(NONCE_COOKIE, path=NONCE_COOKIE_PATH)
    return response


@router.post("/signout")
async def signout(request: Request, everywhere: bool = False) -> Response:
    """Revocation is the logout (`session_tokens.revoked_at`); clearing the
    cookie is a courtesy. No principal required: an already-dead session is
    signed out the same way, and nothing is disclosed either way.

    ``?everywhere=true`` revokes every live session of the presenting user —
    every browser and device they are signed in on, this one included
    (`sessions.revoke_all_for_user`). A dead presented session revokes
    nothing, so a stale cookie cannot reach its siblings, and the answer's
    ``revoked`` count says so: the front end shows 0 as not done.

    A session carried by the COOKIE must come from an admitted origin
    (`require_same_origin`): a forged post from a sibling host would
    otherwise sign a person out of everything with one hidden form.
    """
    engine = require_engine(request)
    value = presented_token(request)
    revoked = 0
    if value is not None:
        require_same_origin(request)
        digest = sessions.token_hash(value)
        async with engine.begin() as conn:
            if everywhere:
                revoked = await sessions.revoke_all_for_user(conn, token_hash=digest)
            else:
                await sessions.revoke(conn, token_hash=digest)
    body = {"signed_out": True}
    if everywhere:
        body["revoked"] = revoked
    response = JSONResponse(body)
    clear_session_cookie(response)
    return response


@router.get("/google-drive/callback")
async def google_drive_callback(
    request: Request,
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    """The Drive connect leg's return: consume the state, check the returning
    browser is the one that started the flow, exchange the code, write the
    credential. The Instagram leg's checks: the returning session must be the
    state's user, and that user must still be an admin inside the write."""
    client_id, client_secret, redirect_uri = google_client.configured(
        google_client.DRIVE_CALLBACK_PATH
    )
    row = await _consume_callback(
        request,
        state=state,
        code=code,
        error=error,
        expected_provider=google_drive_oauth.PROVIDER,
        expected_purpose=set(commands.CONNECT_PURPOSE_KIND),
        flow=DRIVE_FLOW,
        require_presenter=True,
    )
    if isinstance(row, Response):
        return row
    if row["reconnect_target"] is None or str(row["reconnect_target"]) != str(
        row["workspace_id"]
    ):
        # The grant is the WORKSPACE's (069, `07` §15): the issue leg pins the
        # workspace as its own target. A state that names anything else — a
        # source id from the folder-first leg this replaced, or nothing — is
        # not one this leg can act on.
        logger.warning(
            "drive connect: state does not pin its workspace as the grant's owner"
        )
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
        channel=principal_mod.WEB_CHANNEL,
    )
    try:
        async with uow.begin() as session:
            # The floor of the command the state's purpose stands for, at issue
            # AND at callback, as the Instagram leg: a demoted admin's pending
            # state must not land a grant.
            await tenant_resolution.authorize_member(
                session,
                str(row["workspace_id"]),
                str(row["user_id"]),
                minimum_role=commands.connect_floor(row["purpose"]),
            )
            # The state's user is the granter (091, `07` §34): the presenter
            # check above proved the returning browser is theirs, so the
            # Google account just consented is theirs, and only they browse it.
            await google_drive_oauth.store_credential(
                session,
                workspace_id=row["workspace_id"],
                grant=grant,
                granted_by=str(row["user_id"]),
            )
            # F4 (a), in THIS transaction — `store_credential`'s contract, now
            # workspace-wide: every gdrive folder becomes eligible again beside
            # the write that makes it so.
            await media_sync.rearm_after_connect(
                session, workspace_id=row["workspace_id"]
            )
    except TenantResolutionError as exc:
        logger.warning("drive connect: callback authorization refused: %s", exc)
        return _fail("state_refused", flow=DRIVE_FLOW)

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
    here or a new one — and write the credential. The presenting-session
    check is `_consume_callback`'s; the `07` §2 callback-time checks follow."""
    app_id, app_secret, redirect_uri = instagram_client.configured()
    row = await _consume_callback(
        request,
        state=state,
        code=code,
        error=error,
        expected_provider=ig_login_oauth.PROVIDER,
        expected_purpose=set(commands.CONNECT_PURPOSE_KIND),
        flow=INSTAGRAM_FLOW,
        require_presenter=True,
    )
    if isinstance(row, Response):
        return row

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
    # Pinned: the state named the destination to connect or reconnect.
    # Unpinned: the workspace-level ADD (owner ruling 2026-09-04). One landing
    # function decides; the credential write below is the same either way.
    target = row["reconnect_target"]
    uow = unit_of_work(
        require_engine(request),
        workspace_id,
        actor_kind="user",
        actor_user_id=str(row["user_id"]),
        channel=principal_mod.WEB_CHANNEL,
    )
    try:
        async with uow.begin() as session:
            # `07` §2: the floor of the command the state's purpose stands for,
            # checked at issue AND at callback. The row pins the workspace and
            # the user; what can change between the two is the membership,
            # and a demoted admin's pending state must not land a credential.
            await tenant_resolution.authorize_member(
                session,
                workspace_id,
                str(row["user_id"]),
                minimum_role=commands.connect_floor(row["purpose"]),
            )
            account_id, _ = await provisioning.connect_destination(
                session,
                workspace_id=workspace_id,
                ig_account_id=None if target is None else str(target),
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
        if exc.reason not in _ATTACH_REASON:
            raise
        logger.warning("instagram connect: landing refused: %s", exc)
        return _fail(_ATTACH_REASON[exc.reason], flow=INSTAGRAM_FLOW)

    return RedirectResponse(
        _landing("/dashboard/settings?connected=instagram"), status_code=302
    )


#: `provisioning.connect_destination`'s refusals on the error page. Each is a
#: DIFFERENT remedy, which is why they are not folded into `state_refused`
#: ("start again and it should work" is false for every one of them). A
#: reason outside this table is not a grant outcome at all — `slot_not_seeded`
#: is a programming error — and is answered as one, never as "start again".
_ATTACH_REASON = {
    "duplicate_destination": "already_connected",
    "wrong_account": "wrong_account",
    "not_found": "destination_gone",
    "workspace_inactive": "workspace_closing",
}


async def _presenting_user(request: Request) -> Optional[str]:
    """The user id of the session the returning browser carries, or None.

    Resolved as `current_principal` resolves a SESSION (the same cookie, the
    same `sessions.resolve`), on the engine directly: this runs before any
    tenant is known. An API token (`sdt_…`) is not a browser and never
    reaches an OAuth callback; here it hashes as a session and is None. A refusal of any kind is None — the caller's answer is
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
