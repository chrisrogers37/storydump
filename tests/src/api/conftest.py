"""Fixtures for the API layer — the factory, a fake engine, and the seams.

Route unit tests never reach SQL: the engine is a fake whose session refuses
`execute`, and each test patches the service function its route calls, so a
route that grows a query without a seam fails loudly here rather than passing
against nothing. The real database path — sign in, create a workspace, list,
approve, as the production role on the replayed target schema — is
`tests/scripts/test_web_router_x2_gate.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.principal import Principal, current_principal
from src.api.routes import v1
from src.services.target import tenant_resolution

PRINCIPAL = Principal(
    session_id="11111111-1111-1111-1111-111111111111",
    user_id="22222222-2222-2222-2222-222222222222",
)
WS = "33333333-3333-3333-3333-333333333333"
INTENT = "44444444-4444-4444-4444-444444444444"


class FakeSession:
    """Refuses SQL. A test that trips this needs a patched seam, not a query."""

    async def execute(self, *args, **kwargs):
        raise AssertionError(
            "a route unit test reached SQL — patch the service seam instead"
        )


class _Ctx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


class FakeEngine:
    """Only what the routes touch: `begin()` and `connect()`."""

    def __init__(self):
        self.session = FakeSession()

    def begin(self):
        return _Ctx(self.session)

    def connect(self):
        return _Ctx(self.session)


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
def tenant(monkeypatch, engine):
    """The tenant seam: the unit of work yields the fake session and the gate
    records what it was asked. Returns the call log."""
    asked = []

    @asynccontextmanager
    async def open_tenant(request, workspace_id, principal):
        asked.append(("uow", workspace_id, principal.user_id))
        yield engine.session

    async def gate(session, workspace_id, user_id, minimum_role="member"):
        asked.append(("gate", workspace_id, user_id, minimum_role))
        return "owner"

    monkeypatch.setattr(v1, "_open_tenant", open_tenant)
    monkeypatch.setattr(tenant_resolution, "authorize_member", gate)
    return asked
