"""The web principal — `07` §1's session, resolved once per request.

One FastAPI dependency answers "who is calling": the ``sd_session`` cookie, or
``Authorization: Bearer <the same opaque value>`` for a front end that forwards
the cookie from its server side. Those are ONE credential with two carriers,
not two credentials — the bearer form exists so SSR can call the API without
the browser, and it is verified by the same hash lookup. Verification is
`sessions.resolve`, which also slides the expiry; nothing here re-implements it.

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

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from src.config.settings import settings
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import sessions

#: The session cookie. One name, imported by the auth routes and the tests.
COOKIE = "sd_session"


@dataclass(frozen=True)
class Principal:
    """Who is calling: which session row, which user."""

    session_id: str
    user_id: str


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
            detail="target database not configured: set TARGET_DATABASE_URL",
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


def presented_token(request: Request) -> Optional[str]:
    """The opaque session value the request carries, bearer first."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        value = auth[7:].strip()
        if value:
            return value
    return request.cookies.get(COOKIE) or None


async def current_principal(request: Request) -> Principal:
    """FastAPI dependency: authenticate, slide, return the principal."""
    engine = require_engine(request)
    value = presented_token(request)
    if value is None:
        raise TenantResolutionError("invalid_session", "no session presented")
    async with engine.begin() as conn:
        session = await sessions.resolve(conn, token_hash=sessions.token_hash(value))
    return Principal(session_id=session.id, user_id=session.user_id)


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
