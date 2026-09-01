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
from src.services.target import scheduling_health


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
        """Both dicts, because BOTH are indexed on the refusal path.

        This pin caught `invalid_channel` reaching `REASONS` without a status
        (#1172) — a coupling invisible at the point the reason is added, in a
        file that PR never touched. It only asserted `_INVITATION_STATUS`,
        though, and that is a hole in the same direction: the handler reads
        `_INVITATION_STATUS.get(...)` and falls back to a logged 500, but then
        indexes `_INVITATION_DETAIL[...]` BARE. So a reason mapped in the first
        and missing from the second passed this test and raised `KeyError` at
        runtime — the exact failure the pin exists to prevent, one dict over.
        """
        from src.api import app as module
        from src.services.target import invitations

        assert set(module._INVITATION_STATUS) == set(invitations.REASONS)
        assert set(module._INVITATION_DETAIL) == set(invitations.REASONS)

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

    def test_it_reaches_the_query_when_an_engine_is_present(self, client, monkeypatch):
        """THE TEST THIS ROUTE WAS MISSING — and the absence is the finding,
        not the 500 it let through.

        Both tests above build `create_app(env={})`, so `app.state.engine` is
        None and both return at the 503 branch — ONE LINE ABOVE the code that
        runs in production. The query underneath was mutation-tested against a
        real database and was never the problem. The route was, and every green
        signal it ever produced was true about a path that does not execute.

        So this one takes the other branch. It needs no real database, and that
        is exactly the point: the defect raised at `UnitOfWork` construction
        before any SQL, so ANY non-None engine would have caught it — and none
        of the coverage supplied one.
        """
        seen = []

        async def fake_lag(executor):
            seen.append(executor)
            return {"stalled": 0, "accounts_active": 7, "max_lag_seconds": None}

        async def fake_worker(executor):
            # #1120 added a SECOND seam on this route. A route unit test has to
            # stub both or it reaches SQL — which this class's own fixture
            # guard catches, and did.
            seen.append(executor)
            return {
                "succeeded_ever": 0,
                "last_success_age_seconds": None,
                "overdue_ready": 0,
                "max_overdue_seconds": None,
            }

        monkeypatch.setattr(scheduling_health, "scheduling_lag", fake_lag)
        monkeypatch.setattr(scheduling_health, "worker_freshness", fake_worker)
        resp = client.get("/health/scheduling")

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # The cursor axis keeps its exact shape — that is the poller contract a
        # deployment predating #1120 still reads. Asserted key-by-key rather
        # than by whole-dict equality so ADDING an axis is not a breakage while
        # CHANGING one of these still is.
        assert payload["stalled"] == 0
        assert payload["accounts_active"] == 7
        assert payload["max_lag_seconds"] is None
        assert seen, "the route answered without ever reaching its seam"
        # `scheduling_lag` names its parameter `executor`, not `session`, so a
        # connection is a legal argument. Pinned because that duck type is what
        # lets the route drop the unit of work at all.
        assert hasattr(seen[0], "execute")

    def test_scheduling_health_serves_the_worker_axis_too(self, client, monkeypatch):
        """#1120: both axes on ONE payload, deliberately.

        A second endpoint would need a second poller invocation enrolled on the
        fleet host — a unit change — to close a hole the existing poller can
        already reach. One payload keeps the fix inside the app.
        """

        async def fake_lag(executor):
            return {"stalled": 0, "accounts_active": 0, "max_lag_seconds": None}

        async def fake_worker(executor):
            return {
                "succeeded_ever": 78,
                "last_success_age_seconds": 3600,
                "overdue_ready": 0,
                "max_overdue_seconds": None,
            }

        monkeypatch.setattr(scheduling_health, "scheduling_lag", fake_lag)
        monkeypatch.setattr(scheduling_health, "worker_freshness", fake_worker)
        resp = client.get("/health/scheduling")

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # The cursor axis is unchanged — the poller's existing contract.
        assert payload["accounts_active"] == 0
        assert payload["worker"]["succeeded_ever"] == 78
        assert payload["worker"]["last_success_age_seconds"] == 3600
