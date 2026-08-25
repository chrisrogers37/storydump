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
        assert client_health(app)["target_database"] is True


def client_health(app):
    return TestClient(app).get("/health").json()


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
            "/webapp/onboarding",
            "/api/onboarding/init",
            "/api/onboarding/settings",
            "/static/onboarding/index.html",
            "/auth/instagram",
        ],
    )
    def test_legacy_paths_are_not_routed(self, client, path):
        assert client.get(path).status_code == 404

    def test_the_dormant_webhook_route_is_still_mounted(self, client):
        # 403 is the route refusing an unarmed delivery — it exists.
        assert client.post("/webhooks/telegram", json={}).status_code == 403

    def test_no_process_local_limiter_survives(self, app):
        assert not hasattr(app.state, "limiter")
        import src.api.app as module

        assert "slowapi" not in module.__dict__ and not any(
            "slowapi" in str(getattr(m, "__module__", "")) for m in app.user_middleware
        )
