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

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.principal import Principal, current_principal
from src.api.routes import v1
from src.config.settings import settings
from src.services.target import google_oidc, tenant_resolution

PRINCIPAL = Principal(
    session_id="11111111-1111-1111-1111-111111111111",
    user_id="22222222-2222-2222-2222-222222222222",
)
WS = "33333333-3333-3333-3333-333333333333"
INTENT = "44444444-4444-4444-4444-444444444444"

#: The configured sign-in world: API host, front-end origin, client id.
API = "https://api.example.test"
FRONT = "https://app.example.test"
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
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", CLIENT_ID, raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec", raising=False)
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE_URL", API, raising=False)
    monkeypatch.setattr(settings, "WEB_APP_URL", FRONT, raising=False)


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
