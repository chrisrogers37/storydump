"""The web principal — `07` §1's session, resolved once per request.

One FastAPI dependency answers "who is calling": the ``sd_session`` cookie, or
``Authorization: Bearer <the same opaque value>`` for a front end that forwards
the cookie from its server side. Those are ONE credential with two carriers,
not two credentials — the bearer form exists so SSR can call the API without
the browser, and it is verified by the same hash lookup. Verification is
`sessions.resolve`, which also slides the expiry; nothing here re-implements it.

The second credential is the API token (`07` §6; plan `2026-09-15-cli-v2`):
a bearer value that starts with ``sdt_`` is routed to `service_tokens.resolve`
and yields a token principal — a person-bound token acts as its person over
the ``cli`` channel, a workspace service identity has no person and reads.
The prefix routes BEARER values only; the cookie is always a session. Tokens
are admitted to :data:`TOKEN_ROUTES` and nowhere else: every other route
depends on `require_session`, which refuses a token with a reason the CLI can
act on. That allowlist is the contract the gate enumerates.

A user with no workspace is a valid principal. Tenancy is decided per route by
the central gate (`tenant_resolution.authorize_member`), never here — on the
greenfield every user starts tenant-less, so refusing them at the door would
refuse every new sign-up.

The lookup runs on a raw engine connection rather than a unit of work: the UoW
is unconstructible without a tenant, and authentication precedes tenancy by
definition (`session_tokens` and `users` are user-plane — `058` class 3,
`060` — readable before any ``app.tenant_id`` exists).

Refusals are the `TenantResolutionError` the resolver raises; the app maps that
type once (invalid/expired/revoked session → 401), so this module speaks no
HTTP except for the one condition that is the deployment's rather than the
caller's: no target database configured, which is a 503 named after the
variable, never a silent fallback to the settings-built URL (#1010's class).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from src.config.settings import settings
from src.exceptions.tenancy import TenantResolutionError, TokenRefused
from src.services.target import service_tokens, sessions, tenant_resolution
from src.services.target.unit_of_work import unit_of_work
from src.services.target.vocabulary import DATABASE_URL_VAR

#: The session cookie. One name, imported by the auth routes and the tests.
COOKIE = "sd_session"

#: The `command_dedup.channel` and `app.channel` GUC value for the web
#: surface (`webhook_ingress.CHANNELS`). The browser's sessions, the two
#: OAuth callbacks and the v1 command adapter are one channel; four
#: spellings of it is how a dedup row lands under a name nothing reads.
WEB_CHANNEL = "web"

#: The routes a token may reach, as ``(method, path)`` after the router
#: prefix. Everything else is session-only by dependency. No minting route
#: is here, whatever the token's role — a token never mints a token.
TOKEN_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/me/principal"),
        ("GET", "/api/v1/me/tokens"),
        ("DELETE", "/api/v1/me/tokens/{token_id}"),
        ("GET", "/api/v1/workspaces/{ws}/tokens"),
        ("DELETE", "/api/v1/workspaces/{ws}/tokens/{token_id}"),
        ("POST", "/api/v1/workspaces/{ws}/commands/{command}"),
        # phase 02: the read views (`src/api/routes/ops.py`)
        ("GET", "/api/v1/ops/workspaces/{ws}/story/{intent_id}"),
        ("GET", "/api/v1/ops/workspaces/{ws}/cards/{intent_id}"),
        ("GET", "/api/v1/ops/workspaces/{ws}/floating"),
        ("GET", "/api/v1/ops/workspaces/{ws}/account/{key}"),
        ("GET", "/api/v1/ops/workspaces/{ws}/jobs"),
        ("GET", "/api/v1/ops/workspaces/{ws}/outbox"),
        ("GET", "/api/v1/ops/workspaces/{ws}/burst"),
        ("GET", "/api/v1/ops/posture"),
    }
)


@dataclass(frozen=True)
class Principal:
    """Who is calling: a session (which row, which user) or a token.

    The defaults keep a session principal's shape exactly what it was —
    ``Principal(session_id=…, user_id=…)`` — so nothing that builds one
    changes. A token principal has ``kind="token"``, ``channel="cli"`` and
    the token's facts; a service identity is the one with no ``user_id``.
    """

    session_id: Optional[str]
    user_id: Optional[str]
    kind: str = "session"
    channel: str = WEB_CHANNEL
    token_id: Optional[str] = None
    token_name: Optional[str] = None
    token_role: Optional[str] = None
    token_workspace_id: Optional[str] = None
    token_expires_at: Optional[datetime] = None

    @property
    def is_token(self) -> bool:
        return self.kind == "token"

    @property
    def is_service_identity(self) -> bool:
        return self.kind == "token" and self.user_id is None

    @property
    def actor_kind(self) -> str:
        """The `app.actor_kind` GUC: a person is a user whichever carrier
        they arrive on; a service identity is the operator kind."""
        return "operator" if self.is_service_identity else "user"

    @property
    def dedup_principal(self) -> str:
        """The `command_dedup.principal` slot: the session id, or the token
        id under a prefix so the two namespaces can never collide."""
        if self.is_token:
            return f"token:{self.token_id}"
        return self.session_id or ""


def require_engine(request: Request) -> AsyncEngine:
    """The target engine, or a 503 that names the missing variable.

    `TARGET_DATABASE_URL` unset must never quietly become the settings-built
    URL: on the deployed service that URL points at nothing (production sets
    no `DB_*`), and on a misconfigured one it would point at a legacy-shaped
    database — a wrong answer that reads as a right one.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail=f"target database not configured: set {DATABASE_URL_VAR}",
        )
    return engine


def _covers(domain: str, host: str) -> bool:
    """Whether a cookie scoped to *domain* is sent to *host* — RFC 6265 §5.1.3
    domain-matching, which is exact-or-subdomain and nothing cleverer."""
    return host == domain or host.endswith("." + domain)


def session_delivery_gap() -> Optional[str]:
    """Why a session minted here could not be read by the configured front end,
    or ``None`` when it can.

    The sign-in leg mints a session on the API host and the front end reads it
    from the browser's cookie jar. Those are only the same jar when the cookie's
    scope covers BOTH hosts, and nothing in the request tells you it does not —
    the sign-in succeeds, the cookie is set, and the front end simply never sees
    it, so the person is bounced back to `/login` having just signed in.

    That state is not hypothetical: it is what production shipped (#1117), and
    the reason it survived is that every individual part of it works.

    ``WEB_APP_URL`` unset is NOT a gap here. It is the documented fail-closed
    reading — no browser origin is admitted and sign-in lands on the API's own
    root — and changing that is a deployment decision, not this gate's.

    KNOWN BOUND, because a gate that implies more than it checks is worse than
    none: this compares host suffixes and does NOT consult the Public Suffix
    List. ``SESSION_COOKIE_DOMAIN`` set to a public suffix (``up.railway.app``
    is one) passes this check and is still rejected by every browser. The PSL
    is the browser's to enforce and we do not carry a copy.
    """
    front = settings.web_app_origin
    if not front:
        return None
    base = settings.OAUTH_REDIRECT_BASE_URL
    if not base:
        return None
    api_host = urlsplit(base).hostname
    web_host = urlsplit(front).hostname
    if not api_host or not web_host:
        return None
    if api_host == web_host:
        return None
    domain = (settings.SESSION_COOKIE_DOMAIN or "").lstrip(".")
    if not domain:
        return (
            f"the session cookie is host-only on {api_host} and the front end "
            f"is {web_host}, which will never receive it: set "
            f"SESSION_COOKIE_DOMAIN to a domain covering both hosts"
        )
    if not _covers(domain, api_host) or not _covers(domain, web_host):
        return (
            f"SESSION_COOKIE_DOMAIN={domain} does not cover both {api_host} "
            f"and {web_host}, so the front end will never receive the session: "
            f"serve the API and the front end from one registrable domain"
        )
    return None


def require_deliverable_session() -> None:
    """A 503 that names the variable when sign-in would mint a session the
    front end cannot read — the same posture as `require_engine`, for the same
    reason: the condition is the deployment's, not the caller's.

    This refuses at the START of the leg. Sending someone to Google for a
    sign-in we already know we cannot deliver spends their consent on a round
    trip that ends at the login page it began on.
    """
    gap = session_delivery_gap()
    if gap is not None:
        raise HTTPException(
            status_code=503, detail=f"sign-in cannot deliver a session: {gap}"
        )


def presented_bearer(request: Request) -> Optional[str]:
    """The ``Authorization: Bearer`` value alone, or None."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        value = auth[7:].strip()
        if value:
            return value
    return None


def presented_token(request: Request) -> Optional[str]:
    """The opaque session value the request carries, bearer first."""
    return presented_bearer(request) or request.cookies.get(COOKIE) or None


async def current_principal(request: Request) -> Principal:
    """FastAPI dependency: authenticate, slide or stamp, return the principal.

    A bearer value with the token prefix is a token and resolves through the
    token resolver; every other value — bearer or cookie — is a session and
    takes the path it always took.
    """
    engine = require_engine(request)
    bearer = presented_bearer(request)
    if bearer is not None and service_tokens.is_token(bearer):
        async with engine.begin() as conn:
            token = await service_tokens.resolve(
                conn, token_hash=service_tokens.token_hash(bearer)
            )
        return Principal(
            session_id=None,
            user_id=token.user_id,
            kind="token",
            channel="cli",
            token_id=token.token_id,
            token_name=token.name,
            token_role=token.role,
            token_workspace_id=token.workspace_id,
            token_expires_at=token.expires_at,
        )
    value = bearer or request.cookies.get(COOKIE) or None
    if value is None:
        raise TenantResolutionError("invalid_session", "no session presented")
    async with engine.begin() as conn:
        session = await sessions.resolve(conn, token_hash=sessions.token_hash(value))
    return Principal(session_id=session.id, user_id=session.user_id)


def require_own_workspace(principal: Principal, workspace_id: str) -> None:
    """A workspace service identity addresses its own workspace and no other
    — the one check both routers make, with the one sentence."""
    if principal.token_workspace_id != workspace_id:
        raise TokenRefused("wrong_workspace", "this token belongs to another workspace")


def not_found() -> HTTPException:
    """The house 404: a row the caller may not see and a row that is not
    there answer identically (`07` §5 — no existence oracle).

    A FUNCTION rather than a module constant, deliberately: a raised
    exception instance carries `__traceback__` and `__context__`, so a
    shared one would pin a request's frames until the next raise replaced
    them. The detail string is the thing worth having in one place.
    """
    return HTTPException(status_code=404, detail="not found")


async def require_session(
    principal: Principal = Depends(current_principal),
) -> Principal:
    """FastAPI dependency for every route outside :data:`TOKEN_ROUTES`: a
    session passes through; a token is refused with ``session_required``."""
    if principal.is_token:
        raise TokenRefused(
            "session_required", "this route needs a signed-in web session"
        )
    return principal


def set_session_cookie(response: Response, value: str) -> None:
    """`07` §1: HttpOnly, Secure, SameSite=Lax; Domain from settings so a
    same-site front end's server side can read it (None = host-only)."""
    response.set_cookie(
        COOKIE,
        value,
        max_age=sessions.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        domain=settings.SESSION_COOKIE_DOMAIN,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Same attributes as the set, or the browser keeps the old cookie."""
    response.delete_cookie(
        COOKIE,
        httponly=True,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite="lax",
        domain=settings.SESSION_COOKIE_DOMAIN,
        path="/",
    )


# --- the tenant seams, shared by every router --------------------------------
#
# These four were `v1._open_tenant`, `v1._member`, `v1._admin` and
# `v1._json_object`: private names in the biggest router that `tokens.py`,
# `ops.py` and the shared test conftest all reached across for. They are
# cross-router gates like the ones above, so they live here, as public names.
#
# `member_session` and `admin_session` call `open_tenant` as a BARE MODULE
# GLOBAL, and every caller outside this module reaches these through the
# module attribute (`principal_mod.open_tenant(...)`). A from-import would
# bind the original function at import time and the conftest's monkeypatch
# would not reach it — a unit test that quietly opens a real unit of work.


def open_tenant(request: Request, workspace_id: str, principal: Principal):
    """The request's tenant-scoped unit of work: tenant + actor GUCs applied,
    one transaction. The one seam the unit gate replaces.

    The GUCs come from the principal: a person is ``user`` on ``web`` or, via
    a person-bound token, on ``cli``; a service identity is ``operator`` with
    no user, which the audit triggers read as such. The triggers never see a
    token's name — that rides the direct `cli_command` row (F4).
    """
    return unit_of_work(
        require_engine(request),
        workspace_id,
        actor_kind=principal.actor_kind,
        actor_user_id=principal.user_id,
        channel=principal.channel,
    ).begin()


@asynccontextmanager
async def member_session(request: Request, workspace_id: str, principal: Principal):
    """Open the tenant's unit of work and run the ONE gate — every read."""
    async with open_tenant(request, workspace_id, principal) as session:
        await tenant_resolution.authorize_member(
            session, workspace_id, principal.user_id, minimum_role="member"
        )
        yield session


@asynccontextmanager
async def admin_session(request: Request, workspace_id: str, principal: Principal):
    """`member_session`, at the admin floor."""
    async with open_tenant(request, workspace_id, principal) as session:
        await tenant_resolution.authorize_member(
            session, workspace_id, principal.user_id, minimum_role="admin"
        )
        yield session


async def json_object(request: Request) -> dict[str, Any]:
    """The body as a JSON object; an empty body is an empty object."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body
