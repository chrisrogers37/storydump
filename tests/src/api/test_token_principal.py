"""A bearer token as a principal (phase 01 of the v2 CLI).

Two carriers, two credentials: the ``sdt_`` prefix routes a value to the token
resolver and every other bearer value stays on the session path byte for byte
(`TestAuthentication` in test_v1_routes pins the session side). Tokens are
admitted to an explicit route allowlist; every other route is session-only and
says so with a reason the CLI can act on.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from fastapi.routing import APIRoute

from src.api import principal
from src.api.principal import (
    TOKEN_ROUTES,
    Principal,
    current_principal,
    require_session,
)
from src.exceptions.tenancy import TenantResolutionError, TokenRefused
from src.services.target import service_tokens, sessions, vocabulary, workspaces
from tests.src.api.conftest import PRINCIPAL, WS

TOKEN_ID = "33333333-3333-4333-8333-333333333333"
SERVICE_ID = "44444444-4444-4444-8444-444444444444"

PERSON_TOKEN = Principal(
    session_id=None,
    user_id=PRINCIPAL.user_id,
    kind="token",
    channel="cli",
    token_id=TOKEN_ID,
    token_name="claude-code",
    token_role="operator",
    token_workspace_id=None,
)
SERVICE_TOKEN = Principal(
    session_id=None,
    user_id=None,
    kind="token",
    channel="cli",
    token_id=SERVICE_ID,
    token_name="ops-bot",
    token_role="readonly",
    token_workspace_id=WS,
)


@pytest.fixture
def as_principal(app):
    """Override the resolved principal with any `Principal`."""

    def install(principal):
        app.dependency_overrides[current_principal] = lambda: principal
        return principal

    yield install
    app.dependency_overrides.clear()


class TestThePrincipalShape:
    def test_a_session_principal_is_unchanged_in_shape_and_defaults(self):
        assert PRINCIPAL.kind == "session"
        assert PRINCIPAL.channel == "web"
        assert PRINCIPAL.token_id is None
        assert PRINCIPAL.is_token is False
        assert PRINCIPAL.dedup_principal == PRINCIPAL.session_id
        assert PRINCIPAL.actor_kind == "user"

    def test_a_person_bound_token_acts_as_the_person_over_cli(self):
        assert PERSON_TOKEN.is_token and not PERSON_TOKEN.is_service_identity
        assert PERSON_TOKEN.actor_kind == "user"
        assert PERSON_TOKEN.dedup_principal == f"token:{TOKEN_ID}"

    def test_a_service_identity_has_no_user_and_audits_as_operator(self):
        assert SERVICE_TOKEN.is_service_identity
        assert SERVICE_TOKEN.actor_kind == "operator"
        assert SERVICE_TOKEN.user_id is None


class TestRoutingOnThePrefix:
    def test_a_prefixed_bearer_resolves_through_the_token_resolver(
        self, client, monkeypatch
    ):
        seen = {}

        async def resolve(conn, *, token_hash):
            seen["hash"] = token_hash
            return service_tokens.TokenPrincipal(
                token_id=TOKEN_ID,
                name="claude-code",
                role="operator",
                user_id=PRINCIPAL.user_id,
                workspace_id=None,
                expires_at=None,
            )

        async def never(conn, *, token_hash):
            raise AssertionError("a token value must not reach sessions.resolve")

        async def memberships(conn, *, user_id):
            return [{"id": WS, "name": "Mine", "state": "active", "role": "owner"}]

        monkeypatch.setattr(service_tokens, "resolve", resolve)
        monkeypatch.setattr(sessions, "resolve", never)
        monkeypatch.setattr(workspaces, "list_for_user", memberships)
        resp = client.get(
            "/api/v1/me/principal", headers={"Authorization": "Bearer sdt_abc"}
        )
        assert resp.status_code == 200, resp.text
        assert seen["hash"] == service_tokens.token_hash("sdt_abc")
        body = resp.json()
        assert body["kind"] == "token"
        assert body["user_id"] == PRINCIPAL.user_id
        assert body["token"]["id"] == TOKEN_ID
        assert body["token"]["name"] == "claude-code"
        assert body["token"]["role"] == "operator"
        assert body["token"]["workspace_id"] is None
        assert body["workspaces"] == [{"id": WS, "name": "Mine", "role": "owner"}]

    def test_an_unprefixed_bearer_never_reaches_the_token_resolver(
        self, client, monkeypatch
    ):
        async def never(conn, *, token_hash):
            raise AssertionError("a session value must not reach the token resolver")

        async def resolve(conn, *, token_hash):
            return sessions.Session(id=PRINCIPAL.session_id, user_id=PRINCIPAL.user_id)

        async def memberships(conn, *, user_id):
            return []

        monkeypatch.setattr(service_tokens, "resolve", never)
        monkeypatch.setattr(sessions, "resolve", resolve)
        monkeypatch.setattr(workspaces, "list_for_user", memberships)
        resp = client.get(
            "/api/v1/me/principal", headers={"Authorization": "Bearer opaque"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["kind"] == "session"
        assert resp.json()["token"] is None

    def test_a_cookie_that_happens_to_start_with_the_prefix_is_a_session(
        self, client, monkeypatch
    ):
        """The prefix routes BEARER values only: the cookie is always a
        session, so a token pasted into the cookie jar never authenticates."""
        seen = []

        async def resolve(conn, *, token_hash):
            seen.append(token_hash)
            raise TenantResolutionError("invalid_session")

        async def never(conn, *, token_hash):
            raise AssertionError("the cookie must stay on the session path")

        monkeypatch.setattr(sessions, "resolve", resolve)
        monkeypatch.setattr(service_tokens, "resolve", never)
        resp = client.get("/api/v1/me", cookies={"sd_session": "sdt_abc"})
        assert resp.status_code == 401
        assert seen == [sessions.token_hash("sdt_abc")]

    @pytest.mark.parametrize("reason", vocabulary.TOKEN_RESOLUTION_REASONS)
    def test_a_dead_token_is_401_without_saying_why(self, client, monkeypatch, reason):
        async def resolve(conn, *, token_hash):
            raise TenantResolutionError(reason)

        monkeypatch.setattr(service_tokens, "resolve", resolve)
        resp = client.get(
            "/api/v1/me/principal", headers={"Authorization": "Bearer sdt_dead"}
        )
        assert resp.status_code == 401
        assert resp.json() == {"detail": "authentication required"}


class TestTheAllowlist:
    """Every route outside `TOKEN_ROUTES` refuses a token with a reason."""

    @staticmethod
    def _fill(path: str) -> str:
        return (
            path.replace("{ws}", WS)
            .replace("{token_id}", TOKEN_ID)
            .replace("{command}", "approve")
            .replace("{token}", "inv-token")
            .replace("{intent_id}", str(uuid.uuid4()))
            .replace("{media_id}", str(uuid.uuid4()))
            .replace("{source_id}", str(uuid.uuid4()))
            .replace("{account_id}", str(uuid.uuid4()))
        )

    def _routes(self, app):
        for route in app.routes:
            if isinstance(route, APIRoute) and route.path.startswith("/api/v1/"):
                for method in route.methods - {"HEAD", "OPTIONS"}:
                    yield method, route.path

    def test_the_allowlist_is_small_and_names_no_minting_route(self):
        assert ("POST", "/api/v1/me/tokens") not in TOKEN_ROUTES
        assert ("POST", "/api/v1/workspaces/{ws}/tokens") not in TOKEN_ROUTES
        assert ("POST", "/api/v1/workspaces/{ws}/commands/{command}") in TOKEN_ROUTES
        assert ("GET", "/api/v1/me/principal") in TOKEN_ROUTES

    def test_every_route_outside_the_allowlist_refuses_a_token(
        self, app, client, as_principal
    ):
        """The refusal is the dependency's, before any handler runs: the
        fake engine refuses SQL, so a route that reached its handler under
        a token would surface as an AssertionError, not a 403."""
        as_principal(PERSON_TOKEN)
        refused = []
        for method, path in self._routes(app):
            if (method, path) in TOKEN_ROUTES:
                continue
            resp = client.request(method, self._fill(path), json={})
            assert resp.status_code == 403, (method, path, resp.text)
            assert resp.json()["reason"] == "session_required", (method, path)
            refused.append((method, path))
        assert len(refused) >= 28, refused
        assert ("POST", "/api/v1/invitations/{token}/accept") in refused
        assert ("POST", "/api/v1/workspaces/{ws}/drive/connect") in refused
        assert ("POST", "/api/v1/workspaces/{ws}/accounts/connect") in refused
        assert ("GET", "/api/v1/me") in refused

    def test_every_allowlisted_route_exists(self, app):
        present = set(self._routes(app))
        assert set(TOKEN_ROUTES) <= present, set(TOKEN_ROUTES) - present

    def test_require_session_is_the_dependency_on_every_session_only_route(self, app):
        for route in app.routes:
            if not (isinstance(route, APIRoute) and route.path.startswith("/api/v1/")):
                continue
            for method in route.methods - {"HEAD", "OPTIONS"}:
                deps = {d.call for d in route.dependant.dependencies}
                if (method, route.path) in TOKEN_ROUTES:
                    assert current_principal in deps, (method, route.path)
                    assert require_session not in deps, (method, route.path)
                else:
                    assert require_session in deps, (method, route.path)


class TestRequireSession:
    @pytest.mark.asyncio
    async def test_a_session_passes_and_a_token_is_refused_with_the_reason(self):
        assert await require_session(PRINCIPAL) is PRINCIPAL
        with pytest.raises(TokenRefused) as exc:
            await require_session(PERSON_TOKEN)
        assert exc.value.reason == "session_required"
        with pytest.raises(TokenRefused):
            await require_session(SERVICE_TOKEN)

    def test_token_refusal_reasons_are_the_vocabulary(self):
        # Pinned to a LITERAL, deliberately, and not to `vocabulary.TOKEN_REFUSALS`.
        # Until #1336 `TokenRefused.REASONS` was its own hand-written copy, so
        # comparing the two caught a drift between them. #1336 made REASONS read
        # the vocabulary directly — which is the point of that PR — and at that
        # moment this assertion became `tuple(x) == x`: the same object on both
        # sides, true whatever the vocabulary says. The `cli_v2_01.sh` mutation
        # that deletes "wrong_workspace" from the vocabulary stopped being killed
        # by it.
        #
        # There is no second copy left to compare against, so the only thing that
        # can still fail is a statement of what the set actually IS. A reason
        # added or removed here is a wire-visible change to what `/v1` refuses
        # with, and it should cost a deliberate edit of this line.
        assert tuple(TokenRefused.REASONS) == (
            "session_required",
            "readonly_token",
            "wrong_workspace",
        )
        assert tuple(vocabulary.TOKEN_REFUSALS) == tuple(TokenRefused.REASONS)

    def test_replace_keeps_a_token_principal_frozen_and_comparable(self):
        assert replace(PERSON_TOKEN, token_role="readonly").token_role == "readonly"
        assert PERSON_TOKEN == replace(PERSON_TOKEN)


class TestTheHouse404:
    """`principal.not_found()` — the one 404 eight routes had spelled out.

    A row the caller may not see and a row that is not there answer
    identically (`07` §5, no existence oracle), so the status and the detail
    are the wire contract and belong in one place.
    """

    def test_the_status_and_the_detail_are_the_house_answer(self):
        exc = principal.not_found()
        assert exc.status_code == 404
        assert exc.detail == "not found"

    def test_two_calls_are_distinct_objects(self):
        """The reason it is a function and not a module constant: a raised
        instance carries `__traceback__` and `__context__`, so a shared one
        would pin one request's frames until the next raise replaced them."""
        assert principal.not_found() is not principal.not_found()


class TestTheTenantSeamsResolveThroughTheModule:
    """The four seams that moved here from `v1` (`open_tenant`,
    `member_session`, `admin_session`, `json_object`).

    The shared conftest patches `principal.open_tenant` BY NAME, so the two
    gates must reach it as a module global and every router must reach all
    four through the module attribute. A from-import binds the real function
    at import time: the patch would not land, and a route unit test would
    quietly open a real unit of work against the test engine and pass for
    the wrong reason.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "gate,floor",
        [("member_session", "member"), ("admin_session", "admin")],
    )
    async def test_both_gates_call_the_patched_open_tenant(
        self, monkeypatch, gate, floor
    ):
        from contextlib import asynccontextmanager

        from src.services.target import tenant_resolution

        seen = []

        @asynccontextmanager
        async def fake_open_tenant(request, workspace_id, who):
            seen.append(("uow", workspace_id, who.user_id))
            yield "session"

        async def fake_gate(session, workspace_id, user_id, minimum_role="member"):
            seen.append(("gate", workspace_id, user_id, minimum_role))

        monkeypatch.setattr(principal, "open_tenant", fake_open_tenant)
        monkeypatch.setattr(tenant_resolution, "authorize_member", fake_gate)

        async with getattr(principal, gate)(None, WS, PRINCIPAL) as session:
            assert session == "session"
        assert seen == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, floor),
        ]

    @pytest.mark.parametrize("router", ["v1", "tokens", "ops"])
    def test_no_router_from_imports_a_seam(self, router):
        import importlib

        module = importlib.import_module(f"src.api.routes.{router}")
        for name in ("open_tenant", "member_session", "admin_session", "json_object"):
            assert not hasattr(module, name), (
                f"src.api.routes.{router} binds `{name}` at import time; reach it"
                " as `principal_mod.{name}` so the conftest's patch lands"
            )
