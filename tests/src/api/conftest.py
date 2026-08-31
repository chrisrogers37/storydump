"""Fixtures and helpers for the API layer — the factory, a fake engine, the
seams, and the two builders every sign-in test needs.

Route unit tests never reach SQL: the engine is a fake whose session refuses
`execute`, and each test patches the service function its route calls, so a
route that grows a query without a seam fails loudly here rather than passing
against nothing. The real database path — sign in, create a workspace, list,
approve, as the production role on the replayed target schema — is
`tests/scripts/test_web_router_x2_gate.py`, which imports the same builders.
"""

from __future__ import annotations

import base64
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.app import create_app
from src.api.principal import COOKIE, Principal, current_principal
from src.api.routes import auth, v1
from src.config.settings import settings
from src.services.target import google_oidc, tenant_resolution
from src.services.target.unit_of_work import asyncpg_url

PRINCIPAL = Principal(
    session_id="11111111-1111-1111-1111-111111111111",
    user_id="22222222-2222-2222-2222-222222222222",
)
WS = "33333333-3333-3333-3333-333333333333"
INTENT = "44444444-4444-4444-4444-444444444444"

#: The configured sign-in world: API host, front-end origin, client id.
API = "https://api.example.test"
FRONT = "https://app.example.test"
#: The registrable domain both of the above sit under — what a cookie must be
#: scoped to for the front end to read a session the API minted.
COOKIE_DOMAIN = "example.test"
CLIENT_ID = "cid"


def unsigned_id_token(state: str, **over) -> str:
    """An ID token as Google would mint it for *state* — claims only, no real
    signature, which is all `verify_id_token` reads (OIDC Core §3.1.3.7 (6)).
    Keyword overrides replace or add claims; a `None` override becomes JSON
    null, so a missing claim is expressible."""
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": google_oidc.nonce_for(state),
        "sub": "sub-1",
        "email": "p@example.com",
        "email_verified": True,
        "name": "P",
    }
    claims.update(over)

    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


def cookie_header(resp: httpx.Response, name: str) -> str:
    """The raw Set-Cookie header for *name* — attributes included, which is
    what the cookie tests assert on."""
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(name + "="):
            return header
    raise AssertionError(f"no {name} cookie in {resp.headers.get_list('set-cookie')}")


def cookie_value(resp: httpx.Response, name: str) -> str:
    return cookie_header(resp, name).split(";", 1)[0].split("=", 1)[1]


class FakeSession:
    """Refuses SQL. A test that trips this needs a patched seam, not a query."""

    async def execute(self, *args, **kwargs):
        raise AssertionError(
            "a route unit test reached SQL — patch the service seam instead"
        )


class FakeEngine:
    """Only what the routes touch: `begin()` and `connect()`."""

    def __init__(self):
        self.session = FakeSession()

    @asynccontextmanager
    async def begin(self):
        yield self.session

    connect = begin


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def app(engine):
    return create_app(engine=engine)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def signed_in(app):
    """A resolved principal, bypassing the session lookup."""
    app.dependency_overrides[current_principal] = lambda: PRINCIPAL
    yield PRINCIPAL
    app.dependency_overrides.clear()


@pytest.fixture
def google_configured(monkeypatch):
    """A sign-in world that can actually DELIVER the session it mints.

    `SESSION_COOKIE_DOMAIN` covers both `API` and `FRONT`, which are sibling
    subdomains of `COOKIE_DOMAIN`. Before #1117 this fixture set every other
    variable and left the cookie host-only — modelling, and so normalising, the
    exact deployment that authenticates and then bounces the person back to
    `/login`.
    """
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", CLIENT_ID, raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec", raising=False)
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE_URL", API, raising=False)
    monkeypatch.setattr(settings, "WEB_APP_URL", FRONT, raising=False)
    monkeypatch.setattr(settings, "SESSION_COOKIE_DOMAIN", COOKIE_DOMAIN, raising=False)


@pytest.fixture
def tenant(monkeypatch, engine):
    """The tenant seam: the unit of work yields the fake session and the gate
    records what it was asked. ``tenant.refuse`` makes the gate raise."""
    asked = []

    class Seam(list):
        refuse = None

    log = Seam()

    @asynccontextmanager
    async def open_tenant(request, workspace_id, principal):
        log.append(("uow", workspace_id, principal.user_id))
        yield engine.session

    async def gate(session, workspace_id, user_id, minimum_role="member"):
        log.append(("gate", workspace_id, user_id, minimum_role))
        if log.refuse is not None:
            raise log.refuse
        return "owner"

    monkeypatch.setattr(v1, "_open_tenant", open_tenant)
    monkeypatch.setattr(tenant_resolution, "authorize_member", gate)
    del asked
    return log


# --- the real app, for the gates ---------------------------------------------


@asynccontextmanager
async def api_client(dsn: str):
    """The real app over a fresh NullPool engine on *dsn*, as an ASGI client;
    the engine is disposed with the client. Yields (client, engine). The
    real-database gates' driver — route unit tests use `client` above."""
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        app = create_app(engine=engine)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=API) as client:
            yield client, engine
    finally:
        await engine.dispose()


async def sign_in(
    client: httpx.AsyncClient, monkeypatch, *, sub: str, email: str
) -> dict:
    """Drive the real sign-in with only the provider stubbed, assert it lands
    on `/welcome`, and return the bearer header for the new session."""
    start = await client.get("/auth/google", follow_redirects=False)
    assert start.status_code == 302, start.text
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    nonce = cookie_value(start, auth.NONCE_COOKIE)

    async def exchange_code(client_, **kw):
        return unsigned_id_token(state, sub=sub, email=email, name=sub)

    monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
    done = await client.get(
        f"/auth/google/callback?state={state}&code=c0de",
        headers={"Cookie": f"{auth.NONCE_COOKIE}={nonce}"},
        follow_redirects=False,
    )
    assert done.status_code == 302, done.text
    assert done.headers["location"] == f"{FRONT}/welcome", done.headers["location"]
    return {"Authorization": f"Bearer {cookie_value(done, COOKIE)}"}
