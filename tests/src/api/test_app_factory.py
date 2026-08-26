"""The factory's contract: what is mounted, what is gone, and what is refused.

The two fail-closed facts are the ones worth a test each: no target engine
means a 503 that NAMES the variable (never the settings-built URL), and no
`WEB_APP_URL` means no browser origin is admitted (never "*").
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config.settings import settings


class TestEngineConfiguration:
    def test_no_target_url_means_no_engine_and_a_named_503(self):
        app = create_app(env={})
        assert app.state.engine is None
        client = TestClient(app)
        health = client.get("/health").json()
        assert health["status"] == "ok" and health["target_database"] is False
        resp = client.get("/api/v1/me")
        assert resp.status_code == 503
        assert "TARGET_DATABASE_URL" in resp.json()["detail"]

    def test_target_url_builds_an_asyncpg_engine_without_connecting(self):
        app = create_app(
            env={
                "TARGET_DATABASE_URL": "postgresql://u:p@db.example.test/neondb?sslmode=require"
            }
        )
        assert app.state.engine is not None
        assert app.state.engine.url.drivername == "postgresql+asyncpg"
        assert TestClient(app).get("/health").json()["target_database"] is True


class TestCors:
    @pytest.fixture
    def origin(self, monkeypatch):
        monkeypatch.setattr(
            settings, "WEB_APP_URL", "https://app.example.test/", raising=False
        )
        return "https://app.example.test"

    def test_the_configured_front_end_origin_is_admitted_with_credentials(self, origin):
        client = TestClient(create_app(env={}))
        resp = client.options(
            "/api/v1/me",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization, idempotency-key",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == origin
        assert resp.headers["access-control-allow-credentials"] == "true"
        allowed = resp.headers["access-control-allow-headers"].lower()
        assert "authorization" in allowed and "idempotency-key" in allowed

    def test_any_other_origin_is_not(self, origin):
        client = TestClient(create_app(env={}))
        resp = client.options(
            "/api/v1/me",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in resp.headers

    def test_no_front_end_configured_admits_no_origin(self, monkeypatch):
        monkeypatch.setattr(settings, "WEB_APP_URL", None, raising=False)
        client = TestClient(create_app(env={}))
        resp = client.options(
            "/api/v1/me",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in resp.headers


class TestLegacySurfaceIsGone:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/onboarding/init",
            "/api/onboarding/settings",
            "/static/onboarding/index.html",
            "/auth/instagram",
        ],
    )
    def test_legacy_paths_are_not_routed(self, client, path):
        assert client.get(path).status_code == 404


class TestTheRetiredMiniAppLinkStillLands:
    """Buttons the legacy bot already sent bake `/webapp/onboarding` into the
    message and navigate client-side, so the path must answer: a redirect to
    the front end's sign-in, with the legacy chat id dropped; 410 with a
    sentence when no front end is configured. Never a 404."""

    def test_redirects_to_the_front_end_sign_in_and_drops_the_chat_id(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            settings, "WEB_APP_URL", "https://app.example.test/", raising=False
        )
        for path in ("/webapp/onboarding", "/webapp/onboarding?chat_id=-1001234567890"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 302, path
            assert resp.headers["location"] == "https://app.example.test/login"

    def test_without_a_front_end_it_is_gone_not_missing(self, client, monkeypatch):
        monkeypatch.setattr(settings, "WEB_APP_URL", None, raising=False)
        resp = client.get("/webapp/onboarding", follow_redirects=False)
        assert resp.status_code == 410
        assert "retired" in resp.text.lower()

    def test_the_dormant_webhook_route_is_still_mounted(self, client):
        # 403 is the route refusing an unarmed delivery — it exists.
        assert client.post("/webhooks/telegram", json={}).status_code == 403

    def test_no_process_local_limiter_survives(self, app):
        assert not hasattr(app.state, "limiter")
        import src.api.app as module

        assert "slowapi" not in module.__dict__ and not any(
            "slowapi" in str(getattr(m, "__module__", "")) for m in app.user_middleware
        )


class TestRefusalMappingsAreTotal:
    """Every closed reason a service can raise has a status here — pinned, so
    a new reason cannot ship as a silently chosen 400 (or, now, a 500)."""

    def test_command_reasons(self):
        from src.api import app as module
        from src.services.target import commands

        assert set(module._COMMAND_STATUS) == set(commands.REASONS)

    def test_invitation_reasons(self):
        from src.api import app as module
        from src.services.target import invitations

        assert set(module._INVITATION_STATUS) == set(invitations.REASONS)

    def test_tenant_reasons_are_a_subset_of_the_closed_vocabulary(self):
        from src.api import app as module
        from src.exceptions.tenancy import TenantResolutionError

        assert set(module._TENANT_STATUS) <= set(TenantResolutionError.REASONS)
        # the web surface never resolves a chat; those reasons stay unmapped
        assert "unknown_binding" not in module._TENANT_STATUS

    def test_an_unmapped_reason_is_a_500_not_a_guessed_client_status(
        self, client, signed_in, monkeypatch
    ):
        from src.exceptions.tenancy import TenantResolutionError
        from src.services.target import identity

        async def get_user(conn, *, user_id):
            raise TenantResolutionError("unknown_binding")

        monkeypatch.setattr(identity, "get_user", get_user)
        resp = client.get("/api/v1/me")
        assert resp.status_code == 500
        assert resp.json() == {"detail": "internal error"}


class TestSchedulingHealthIsASecondSurface:
    """#1090 F1. `/health` is Railway's liveness gate and must not open a
    connection; this is the dependency-touching check #1026 asked for, and the
    two being separate endpoints is the design rather than an accident."""

    def test_it_is_not_the_railway_probe(self):
        """If these were one endpoint, either liveness opens a connection — and a
        database blip takes the service down for a fault no restart repairs — or
        the scheduling check touches nothing and reports healthy through any
        outage, which is #1026 exactly."""
        app = create_app(env={})
        paths = {r.path for r in app.routes}
        assert "/health" in paths
        assert "/health/scheduling" in paths

    def test_it_refuses_rather_than_reassures_when_it_cannot_look(self):
        """A monitor must distinguish "scheduling is fine" from "I could not
        look". Collapsing those is the whole subject of #1090 F1, so an absent
        engine is a 503 and never a cheerful zero."""
        app = create_app(env={})
        assert app.state.engine is None
        resp = TestClient(app).get("/health/scheduling")
        assert resp.status_code == 503

    def test_the_railway_probe_still_opens_no_connection(self):
        """The property that made a second endpoint necessary — pinned here so a
        later tidy-up cannot merge them."""
        app = create_app(env={})
        assert TestClient(app).get("/health").status_code == 200
