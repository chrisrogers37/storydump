"""The factory's contract: what is mounted, what is gone, and what is refused.

The two fail-closed facts are the ones worth a test each: no target engine
means a 503 that NAMES the variable (never the settings-built URL), and no
`WEB_APP_URL` means no browser origin is admitted (never "*").
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config.settings import settings
from src.services.target import backpressure, posting_health, scheduling_health


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


async def _fake_snapshot(executor, **kwargs):
    """The third seam on `/health/scheduling` (phase 3a): stubbed so a route
    unit test never reaches SQL."""
    return {
        "lanes": {
            "interactive": {"ready": 0, "oldest_age_s": 0.0},
            "bulk": {"ready": 2, "oldest_age_s": 12.5},
        },
        "outbox_pending": 4,
        "tg_global": {"paced_windows_last_minute": 0, "hold_active": False},
        "ws_oldest_wait": None,
    }


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
        monkeypatch.setattr(backpressure, "snapshot", _fake_snapshot)
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
        monkeypatch.setattr(backpressure, "snapshot", _fake_snapshot)
        resp = client.get("/health/scheduling")

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # The cursor axis is unchanged — the poller's existing contract.
        assert payload["accounts_active"] == 0
        # Phase 3a: the backpressure signal rides the same payload — a third
        # seam, stubbed like the other two, and asserted present.
        assert payload["backpressure"]["outbox_pending"] == 4
        assert payload["worker"]["succeeded_ever"] == 78
        assert payload["worker"]["last_success_age_seconds"] == 3600


class TestPostingHealthIsATHIRDSurface:
    """#1268. `/health/scheduling` reads the clock and the worker, and both
    stayed true through a sixteen-day silence in which nothing posted — 1936
    consecutive `healthy` readings. This route answers the one question false
    across both halves of that outage: did a post LAND."""

    def test_it_is_its_own_route_beside_the_other_two(self):
        app = create_app(env={})
        paths = {r.path for r in app.routes}
        assert {"/health", "/health/scheduling", "/health/posting"} <= paths

    def test_it_refuses_rather_than_reassures_when_it_cannot_look(self):
        """ "posting is fine" and "I could not look" must never collapse — an
        absent engine is a 503 and never a cheerful zero, which would read as
        *nothing has posted* and page for the wrong reason."""
        app = create_app(env={})
        assert app.state.engine is None
        assert TestClient(app).get("/health/posting").status_code == 503

    @staticmethod
    def _stub_seams(monkeypatch, *, accounts_active=2):
        """Patch all THREE route seams and return what each was called with.

        One helper rather than two verbatim copies: the conftest's `FakeSession`
        hard-fails on unstubbed SQL, so a fourth seam would otherwise be three
        edits across two tests, one of which fails with an assertion about SQL
        rather than about the missing stub.
        """
        seen = []

        async def fake_posting(executor):
            seen.append(("posting", executor))
            return {
                "posted_ever": 0,
                "last_post_age_seconds": None,
                "intents_ever": 6,
                "oldest_intent_age_seconds": 172800,
            }

        async def fake_attempts(executor):
            seen.append(("attempts", executor))
            return {"debited_total": 0, "ledger_days": 0}

        async def fake_destinations(executor):
            seen.append(("destinations", executor))
            return {
                "accounts_active": accounts_active,
                # DERIVED, never a constant. The count and the age are one fact
                # told twice — `max()` over no rows is NULL — and `classify`
                # refuses a payload where they disagree. A fixed age here made
                # the `accounts_active=0` case an estate that cannot exist, and
                # the pair guard caught this stub the moment it was added.
                "oldest_active_destination_age_seconds": (
                    604800 if accounts_active else None
                ),
            }

        monkeypatch.setattr(posting_health, "posting_freshness", fake_posting)
        monkeypatch.setattr(posting_health, "publish_attempts", fake_attempts)
        monkeypatch.setattr(posting_health, "destinations", fake_destinations)
        return seen

    def test_it_reaches_its_seams_when_an_engine_is_present(self, client, monkeypatch):
        """The branch the two tests above never take.

        This class's sibling records the finding the hard way: both of ITS
        first tests built `create_app(env={})`, returned at the 503 branch one
        line above the production path, and every green signal they produced was
        true about code that does not execute. So this one supplies an engine
        and stubs all three seams — the conftest's fake session refuses
        `execute`, so a route that grows a query without a seam fails here.
        """
        seen = self._stub_seams(monkeypatch)
        resp = client.get("/health/posting")

        assert resp.status_code == 200, resp.text
        # Every key the poller is strict about, asserted individually: a
        # whole-dict equality would make ADDING an axis a breakage, while
        # dropping one of these must stay one.
        payload = resp.json()
        assert payload["posted_ever"] == 0
        assert payload["last_post_age_seconds"] is None
        assert payload["intents_ever"] == 6
        assert payload["oldest_intent_age_seconds"] == 172800
        assert payload["debited_total"] == 0
        assert payload["ledger_days"] == 0
        assert payload["accounts_active"] == 2
        assert payload["oldest_active_destination_age_seconds"] == 604800
        # Every count/age pair the poller cross-checks must arrive agreeing, or
        # the real `classify` below would answer `unreachable` on a live estate.
        assert (payload["posted_ever"] > 0) is (
            payload["last_post_age_seconds"] is not None
        )
        assert (payload["intents_ever"] > 0) is (
            payload["oldest_intent_age_seconds"] is not None
        )
        assert (payload["accounts_active"] > 0) is (
            payload["oldest_active_destination_age_seconds"] is not None
        )
        assert {kind for kind, _ in seen} == {"posting", "attempts", "destinations"}
        # The services name their parameter `executor`, so a connection is a
        # legal argument — the duck type that lets the route drop the unit of
        # work, which refuses a blank tenant at construction.
        assert all(hasattr(ex, "execute") for _, ex in seen)

    def test_the_payload_satisfies_the_pollers_strictness(self, client, monkeypatch):
        """The two halves of #1268 are one repo and one test suite on purpose,
        so the wire contract cannot drift. `classify` rejects a missing or
        mistyped key as `unreachable`; this drives the REAL classifier over the
        REAL route body rather than asserting a hand-written copy of it."""
        from scripts.posting_monitor import NEVER_POSTED_OVERDUE, classify

        self._stub_seams(monkeypatch, accounts_active=0)
        resp = client.get("/health/posting")

        verdict = classify(
            resp.status_code,
            resp.text,
            silence_s=48 * 3600,
            grace_s=72 * 3600,
            watched_s=16 * 24 * 3600,
        )
        # Not merely "parsed": the 2026-09 payload classifies as the outage.
        assert verdict.state == NEVER_POSTED_OVERDUE

    def test_the_railway_probe_still_opens_no_connection(self):
        """Three health surfaces now, and the reason for the split is unchanged:
        liveness must not open a connection or a database blip takes the service
        down for a fault no restart repairs."""
        app = create_app(env={})
        assert TestClient(app).get("/health").status_code == 200


class _RoleResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _RoleConn:
    def __init__(self, row):
        self._row = row

    async def execute(self, *args, **kwargs):
        return _RoleResult(self._row)


class _RoleEngine:
    """An engine whose connections know who they are, and count themselves."""

    def __init__(self, user="svc_ingress", bypassrls=False):
        self.connects = 0
        self._row = (user, bypassrls)

    @asynccontextmanager
    async def connect(self):
        self.connects += 1
        yield _RoleConn(self._row)

    begin = connect


def _wait_for_role(client, timeout=2.0):
    """The sample runs as a background task after startup; wait for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        role = client.get("/health").json()["db_role"]
        if role is not None:
            return role
        time.sleep(0.02)
    return client.get("/health").json()["db_role"]


class TestHealthReportsTheDatabaseRole:
    """`/health` says which database login the API holds, and whether that login
    bypasses row-level security (#751, F.4).

    Production connects as `neondb_owner` with `BYPASSRLS`, so every `p_tenant`
    policy is inert on the deployed path — and nothing could say so. The switch
    to `svc_ingress` is verified by reading this field back after the deploy.

    The value is sampled ONCE, at startup, in the background. The probe itself
    still opens no connection: that property is what keeps a database blip from
    taking the service down for a fault no restart repairs, and it is pinned
    below by counting connections.
    """

    def test_without_startup_the_role_is_unknown_not_invented(self):
        app = create_app(env={})
        assert TestClient(app).get("/health").json()["db_role"] is None

    def test_it_reports_the_login_and_bypassrls_after_startup(self):
        engine = _RoleEngine(user="svc_ingress", bypassrls=False)
        app = create_app(engine=engine)
        with TestClient(app) as client:
            assert _wait_for_role(client) == {"user": "svc_ingress", "bypassrls": False}

    def test_an_owner_login_is_reported_as_bypassing_rls(self):
        engine = _RoleEngine(user="neondb_owner", bypassrls=True)
        app = create_app(engine=engine)
        with TestClient(app) as client:
            assert _wait_for_role(client) == {"user": "neondb_owner", "bypassrls": True}

    def test_the_probe_opens_no_connection_of_its_own(self):
        engine = _RoleEngine()
        app = create_app(engine=engine)
        with TestClient(app) as client:
            _wait_for_role(client)
            for _ in range(3):
                assert client.get("/health").status_code == 200
        assert engine.connects == 1

    def test_a_failing_sample_leaves_the_probe_answering(self, engine):
        """The conftest engine refuses SQL. Startup must survive that and the
        probe must still answer 200 with an honest unknown."""
        app = create_app(engine=engine)
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["db_role"] is None


class TestTheApiRegistersItsOwnWebhook:
    """Phase 1 of the 2026-09-09 tap plan, deploy step made automatic: with
    the bot token and the webhook secret set, startup registers the door on
    the bot and `/health` reports what Telegram holds — never a secret."""

    def _app_with_bot(self, monkeypatch, env_extra=None):
        import json as _json

        import httpx

        from src.api import app as app_module
        from src.channels.telegram_transport import TelegramTransport

        calls = []

        def bot(request):
            name = str(request.url).rsplit("/", 1)[-1]
            body = _json.loads(request.content) if request.content else {}
            calls.append((name, body))
            if name == "getMe":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"id": 1, "username": "storydump_app_bot"},
                    },
                )
            if name == "setWebhook":
                return httpx.Response(200, json={"ok": True, "result": True})
            if name == "getWebhookInfo":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "url": "https://api.storydump.app/webhooks/telegram",
                            "allowed_updates": ["message", "callback_query"],
                            "pending_update_count": 0,
                            "max_connections": 10,
                        },
                    },
                )
            return httpx.Response(
                404, json={"ok": False, "error_code": 404, "description": "nope"}
            )

        def transport(env):
            token = env.get("TARGET_TELEGRAM_BOT_TOKEN")
            if not token:
                return None
            return TelegramTransport(
                token, client=httpx.AsyncClient(transport=httpx.MockTransport(bot))
            )

        monkeypatch.setattr(app_module, "_telegram_transport", transport)
        env = {
            "TARGET_TELEGRAM_BOT_TOKEN": "8675309:AAtest",
            "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN": "0123456789abcdef0123456789abcdef",
            "TARGET_TELEGRAM_BOT_USERNAME": "storydump_app_bot",
            "RAILWAY_ENVIRONMENT_NAME": "production",
            **(env_extra or {}),
        }
        return create_app(env=env), calls

    def _wait_for_webhook(self, client, timeout=3.0):
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            body = client.get("/health").json()
            if body.get("webhook") is not None:
                return body["webhook"]
            _time.sleep(0.05)
        raise AssertionError("the webhook registration never reported")

    def test_startup_registers_taps_and_the_cap_and_health_reports_it(
        self, monkeypatch
    ):
        from fastapi.testclient import TestClient

        app, calls = self._app_with_bot(monkeypatch)
        with TestClient(app) as client:
            report = self._wait_for_webhook(client)
        # The live sampler adds `getWebhookInfo` calls of its own; the
        # registration's sequence is the prefix.
        assert [c[0] for c in calls][:3] == ["getMe", "setWebhook", "getWebhookInfo"]
        assert calls[1][1]["allowed_updates"] == ["message", "callback_query"]
        assert calls[1][1]["max_connections"] == 10
        assert calls[1][1]["secret_token"] == "0123456789abcdef0123456789abcdef"
        assert calls[1][1]["drop_pending_updates"] is False
        assert report["ok"] is True and report["bot"] == "storydump_app_bot"
        assert report["allowed_updates"] == ["message", "callback_query"]
        assert "0123456789abcdef" not in str(report) and "AAtest" not in str(report)

    def test_the_live_webhook_sample_lands_on_health(self, monkeypatch):
        from fastapi.testclient import TestClient

        app, calls = self._app_with_bot(monkeypatch)
        with TestClient(app) as client:
            import time as _time

            deadline = _time.monotonic() + 3.0
            live = None
            while _time.monotonic() < deadline:
                live = client.get("/health").json().get("webhook_live")
                if live is not None:
                    break
                _time.sleep(0.05)
        assert live is not None and live["pending_update_count"] == 0
        assert live["allowed_updates"] == ["message", "callback_query"]
        assert "AAtest" not in str(live)

    def test_without_a_token_it_is_skipped_and_says_so(self):
        from fastapi.testclient import TestClient

        with TestClient(
            create_app(env={"RAILWAY_ENVIRONMENT_NAME": "production"})
        ) as client:
            report = self._wait_for_webhook(client)
        assert report["ok"] is False and "not set" in report["skipped"]

    def test_outside_production_it_is_off_by_default(self, monkeypatch):
        """A laptop or a preview holding the production token must never
        re-point production's webhook at itself."""
        from fastapi.testclient import TestClient

        app, calls = self._app_with_bot(
            monkeypatch, {"RAILWAY_ENVIRONMENT_NAME": "pr-42"}
        )
        with TestClient(app) as client:
            report = self._wait_for_webhook(client)
        registered = [c for c in calls if c[0] != "getWebhookInfo"]
        assert (
            registered == [] and "not the production environment" in report["skipped"]
        )

    def test_the_switch_turns_it_off(self, monkeypatch):
        from fastapi.testclient import TestClient

        app, calls = self._app_with_bot(
            monkeypatch, {"TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER": "0"}
        )
        with TestClient(app) as client:
            report = self._wait_for_webhook(client)
        registered = [c for c in calls if c[0] != "getWebhookInfo"]
        assert registered == [] and report["skipped"].startswith(
            "autoregister switched off"
        )


class TestHealthReportsThePoolArithmetic:
    """Phase 2 step 4: `/health` names the pool's numbers and how many
    processes serve the ingress, so the `05` inequality can be read off it."""

    def test_without_an_engine_the_pool_is_none_and_workers_default_to_one(self):
        from fastapi.testclient import TestClient

        body = TestClient(create_app(env={})).get("/health").json()
        assert body["pool"] is None and body["ingress_workers"] == 1

    def test_web_concurrency_names_the_process_count(self):
        from fastapi.testclient import TestClient

        body = (
            TestClient(create_app(env={"WEB_CONCURRENCY": "2"})).get("/health").json()
        )
        assert body["ingress_workers"] == 2

    def test_with_an_engine_the_pool_snapshot_is_reported(self, monkeypatch):
        from fastapi.testclient import TestClient

        from src.api import app as app_module
        from src.services.target import unit_of_work as uow

        engine = uow.create_engine(
            "postgresql+asyncpg://u:p@localhost:1/db",
            pool_timeout=uow.INGRESS_POOL_TIMEOUT_SEAM,
        )
        monkeypatch.setattr(app_module, "_engine_from_env", lambda env: engine)
        try:
            body = TestClient(create_app(env={})).get("/health").json()
            assert body["pool"] == {
                "size": 10,
                "overflow": 0,
                "timeout_s": 1.0,
                "checked_out": 0,
                "checked_out_peak": 0,
            }
        finally:
            engine.sync_engine.dispose()
