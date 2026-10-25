"""The factory's contract: what is mounted, what is not, and what is refused.

The two fail-closed facts are the ones worth a test each: no target engine
means a 503 that NAMES the variable (never the settings-built URL), and no
`WEB_APP_URL` means no browser origin is admitted (never "*").
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src import __version__
from src.api.app import create_app
from src.api.principal import Principal, current_principal
from src.api.routes import health as health_routes
from src.api.routes.health import AnswerCache
from src.config.settings import settings
from src.services.target import (
    backpressure,
    delivery_health,
    health_reads,
    posting_health,
    scheduling_health,
)
from tests.src.api.conftest import FakeEngine
from tests.src.services.target.test_delivery_health import _Doors
from tests.unreadable_urls import (
    ENGINE_READABLE,
    ENGINE_UNREADABLE,
    PASSWORDS,
    REFUSAL,
    assert_refused_by_name,
    assert_withheld,
    cases,
)


#: An operator, for the operating details (`/api/v1/ops/health`);
#: who else is refused is `test_ops_routes.py`'s to pin.
OPERATOR = Principal(session_id="s-op", user_id="00000000-0000-4000-8000-0000000000aa")

#: The `PastPostsRefused` reasons a mix write can meet (a past-posts row's On/Off,
#: `versioned_mix.mutate` and `preview`) and the status each answers
#: (`app._PAST_POSTS_STATUS`): these four, and no other reason.
PAST_POSTS_REFUSALS = {
    "past_posts_not_activated": 503,
    "past_posts_source_unavailable": 409,
    "invalid_past_posts_choice": 400,
    "mix_conflict": 409,
}


def details(client: TestClient) -> dict:
    """The API's operating details, read as an operator, the queue stubbed out:
    the engines these tests pass (`_RoleEngine`) cannot answer its SQL."""
    client.app.dependency_overrides[current_principal] = lambda: OPERATOR

    async def no_queue(state):
        return None

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "OPS_USER_IDS", OPERATOR.user_id)
            mp.setattr(health_routes, "queue_pressure", no_queue)
            return client.get("/api/v1/ops/health").json()
    finally:
        client.app.dependency_overrides.pop(current_principal, None)


class TestEngineConfiguration:
    def test_no_target_url_means_no_engine_and_a_named_503(self):
        app = create_app(env={})
        assert app.state.engine is None
        client = TestClient(app)
        assert client.get("/health").json() == {
            "status": "ok",
            "version": __version__,
            "commit": None,
        }
        assert details(client)["target_database"] is False
        resp = client.get("/api/v1/me")
        assert resp.status_code == 503
        assert "TARGET_DATABASE_URL" in resp.json()["detail"]

    def test_target_url_builds_an_asyncpg_engine_without_connecting(self):
        app = create_app(
            env={
                "TARGET_DATABASE_URL": "postgresql://u:not-a-real-password@db.example.test/neondb?sslmode=require"
            }
        )
        assert app.state.engine is not None
        assert app.state.engine.url.drivername == "postgresql+asyncpg"
        assert details(TestClient(app))["target_database"] is True


class TestAnUnreadableTargetUrlFailsStartup:
    """A URL that carries a raw `@` besides the one before the host, part of
    whose password SQLAlchemy reads as the rest of the URL, or one SQLAlchemy
    cannot read as written. Its own error, or asyncpg's on the first
    connection, would quote that part. The API still fails to start, so a bad
    deploy fails its health check and the previous one keeps serving, but its
    error names the variable and the cause, quotes none of the URL and carries
    no error that does. Only this case changes: an unset URL still means no
    engine and a 503 on every data route, and a URL SQLAlchemy reads builds an
    engine on the password its writer meant."""

    @pytest.mark.parametrize("url", **cases(ENGINE_UNREADABLE))
    def test_it_names_the_variable_and_nothing_of_its_value(self, url):
        with pytest.raises(ValueError) as caught:
            create_app(env={"TARGET_DATABASE_URL": url})
        assert_refused_by_name(caught.value, "TARGET_DATABASE_URL")

    @pytest.mark.parametrize("form", list(ENGINE_READABLE))
    def test_control_a_url_sqlalchemy_reads_builds_an_engine_on_its_password(
        self, form
    ):
        app = create_app(env={"TARGET_DATABASE_URL": ENGINE_READABLE[form]})
        assert app.state.engine is not None
        assert app.state.engine.url.password == PASSWORDS[form]

    def test_the_process_that_imports_the_app_exits_without_quoting_it(self):
        """The start command imports `src.api.app`, which builds the app at
        import: the traceback that process prints is the one an operator
        reads in the deploy's log."""
        repo = Path(__file__).resolve().parents[3]
        proc = subprocess.run(
            [sys.executable, "-c", "import src.api.app"],
            cwd=repo,
            env={
                **os.environ,
                "PYTHONPATH": str(repo),
                "TARGET_DATABASE_URL": ENGINE_UNREADABLE["raw-at-sign-then-colon"],
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        assert f"TARGET_DATABASE_URL {REFUSAL}" in proc.stderr, proc.stderr
        assert "During handling of the above exception" not in proc.stderr
        assert "was the direct cause" not in proc.stderr
        assert_withheld(proc.stdout, proc.stderr)


class TestThePublicProbeSaysLittle:
    """`/health` is unauthenticated: it says ok and which build answers, the
    one thing a deploy is verified by, and nothing an attacker or a
    competitor could use (the rest is `details`, the operators')."""

    def test_it_names_the_deployed_commit(self):
        app = create_app(env={"RAILWAY_GIT_COMMIT_SHA": "abc1234aaaabbbbccccdddd"})
        assert TestClient(app).get("/health").json() == {
            "status": "ok",
            "version": __version__,
            "commit": "abc1234",
        }
        assert details(TestClient(app))["commit"] == "abc1234"

    DOCS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"RAILWAY_ENVIRONMENT_NAME": "prod"},
            {"RAILWAY_ENVIRONMENT_NAME": "production"},
            {"RAILWAY_ENVIRONMENT_NAME": " Production ", "API_DOCS": "1"},
            {"API_DOCS": "true"},
        ],
    )
    def test_no_docs_and_no_schema_unless_asked_for_outside_production(self, env):
        """Off unless `API_DOCS=1`, so a renamed environment or another host
        fails closed; production refuses even the switch."""
        client = TestClient(create_app(env=env))
        for path in self.DOCS:
            assert client.get(path).status_code == 404, path
        assert client.get("/health").status_code == 200

    @pytest.mark.parametrize(
        "value, says",
        [
            ("", "OPS_USER_IDS is empty"),
            ("00000000-0000-4000-8000-0000000000aa, tok_123", "entry 2 of 2"),
            ("{00000000-0000-4000-8000-0000000000aa},tok_123,", "entry 2 of 2"),
        ],
    )
    def test_startup_says_why_ops_will_refuse_everyone(self, monkeypatch, value, says):
        from src.api import app as app_module

        said = []
        monkeypatch.setattr(
            app_module.logger, "warning", lambda msg, *a: said.append(msg % a)
        )
        app_module._warn_about_ops_user_ids(value)
        text = " ".join(said)
        assert says in text
        assert "tok_123" not in text, "an entry is never echoed"

    def test_a_pasted_id_in_another_spelling_still_names_the_person(self):
        """Braced, hyphen-less and `urn:uuid:` pastes are the same id the
        database returns; only a non-UUID admits nobody, and is the one the
        startup warning names."""
        from src.config.settings import parse_ops_user_ids

        canonical = "00000000-0000-4000-8000-0000000000aa"
        for spelling in (
            canonical.upper(),
            "{" + canonical + "}",
            canonical.replace("-", ""),
            "urn:uuid:" + canonical,
        ):
            assert parse_ops_user_ids(f" {spelling} , tok_123") == (
                frozenset({canonical}),
                [2],
            ), spelling
        assert parse_ops_user_ids("") == (frozenset(), [])

    @pytest.mark.parametrize(
        "env",
        [{"API_DOCS": "1"}, {"API_DOCS": "1", "RAILWAY_ENVIRONMENT_NAME": "staging"}],
    )
    def test_a_development_server_asks_for_the_docs(self, env):
        client = TestClient(create_app(env=env))
        for path in self.DOCS:
            assert client.get(path).status_code == 200, path


class TestTheVersionIsThePackages:
    """`/health.version` and the OpenAPI document both report the package.

    Neither is hand-typed: a typed string does not move when `src.__version__`
    does, and `/health` and `storydump doctor`, which prints it, would
    then tell whoever is debugging a deploy the wrong number.
    """

    def test_health_reports_the_package_version(self):
        app = create_app(env={})
        assert details(TestClient(app))["version"] == __version__

    def test_the_openapi_document_reports_the_same_one(self):
        """The two cannot disagree.

        `app.py` imports `VERSION` for `FastAPI(version=…)`, so this is one
        object in two places — and this pins that, not just the value.
        """
        app = create_app(env={"API_DOCS": "1"})
        client = TestClient(app)
        assert (
            client.get("/openapi.json").json()["info"]["version"]
            == details(client)["version"]
            == __version__
        )


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
    a new reason cannot ship as a silently chosen 400 (or a 500)."""

    def test_command_reasons(self):
        from src.api import app as module
        from src.services.target import commands

        assert set(module._COMMAND_STATUS) == set(commands.REASONS)

    def test_media_unsuitable_uses_the_command_refusal_handler(self, app, client):
        from src.services.target.commands import CommandRefused

        async def unsuitable():
            raise CommandRefused(
                "media_unsuitable",
                "selected media is unavailable",
                facts={"reason": "unsupported"},
            )

        app.add_api_route("/_test/media-refusal", unsuitable)
        response = client.get("/_test/media-refusal")
        assert response.status_code == 409
        assert response.json() == {
            "detail": "command refused: media_unsuitable — selected media is unavailable",
            "reason": "media_unsuitable",
            "facts": {"reason": "unsupported"},
        }

    def test_invitation_reasons(self):
        """Both dicts, because BOTH are indexed on the refusal path.

        A reason reaching `REASONS` without a status is a coupling
        invisible at the point the reason is added, in a file the change does
        not touch. The handler reads `_INVITATION_STATUS.get(...)` and falls
        back to a logged 500, but then indexes `_INVITATION_DETAIL[...]` BARE,
        so a reason mapped in the first and missing from the second would raise
        `KeyError` at runtime — the exact failure the pin exists to prevent,
        one dict over.
        """
        from src.api import app as module
        from src.services.target import invitations

        assert set(module._INVITATION_STATUS) == set(invitations.REASONS)
        assert set(module._INVITATION_DETAIL) == set(invitations.REASONS)

    def test_token_reasons(self):
        """A live token's refusals answer WITH a reason; a fourth reason
        without a status would reach the unmapped 500."""
        from src.api import app as module
        from src.exceptions.tenancy import TokenRefused

        assert set(module._TOKEN_STATUS) == set(TokenRefused.REASONS)

    def test_cross_site_reasons(self):
        """The cookie origin check's one reason answers 403 with its name."""
        from src.api import app as module
        from src.exceptions.tenancy import CrossSiteRefused

        assert set(module._CROSS_SITE_STATUS) == set(CrossSiteRefused.REASONS)

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

    def test_a_mapped_tenant_refusal_carries_no_reason_on_the_wire(
        self, client, signed_in, monkeypatch
    ):
        """The no-existence-oracle rule, as a body: the detail is
        keyed by the STATUS and the reason never leaves the log. A shared
        refusal-body helper that "normalised" this would tell the web surface
        which of "not a member", "no such workspace" and "disabled" it met."""
        from src.api import app as module
        from src.exceptions.tenancy import TenantResolutionError
        from src.services.target import identity

        async def get_user(conn, *, user_id):
            raise TenantResolutionError("not_a_member")

        monkeypatch.setattr(identity, "get_user", get_user)
        resp = client.get("/api/v1/me")
        assert resp.status_code == 404
        assert resp.json() == {"detail": module._TENANT_DETAIL[404]}
        assert "reason" not in resp.json()

    def test_an_invitation_refusal_carries_its_own_sentence_not_str_exc(
        self, client, signed_in, monkeypatch
    ):
        """`InvitationRefused` answers with `_INVITATION_DETAIL[reason]` — the
        surface's wording — and NOT with `str(exc)`, which is the service's."""
        from src.api import app as module
        from src.services.target import invitations

        async def accept(*args, **kwargs):
            raise invitations.InvitationRefused(
                "identity_mismatch", "the service's own wording"
            )

        monkeypatch.setattr(invitations, "accept", accept)
        resp = client.post("/api/v1/invitations/tok/accept")
        assert resp.status_code == module._INVITATION_STATUS["identity_mismatch"]
        assert resp.json() == {
            "detail": module._INVITATION_DETAIL["identity_mismatch"],
            "reason": "identity_mismatch",
        }
        assert resp.json()["detail"] != "the service's own wording"

    def test_the_past_posts_reasons_a_mix_write_can_meet_are_the_mapped_ones(self):
        """Deliberately NOT total over `PastPostsRefused`: it also carries the
        allocation, sync and fence machinery's reasons, which no request can
        cause and which stay a logged 500. So the table is pinned to the four
        a mix write can meet, and mapping a fifth is decided here too."""
        from src.api import app as module

        assert module._PAST_POSTS_STATUS == PAST_POSTS_REFUSALS

    @pytest.mark.parametrize("reason, status", list(PAST_POSTS_REFUSALS.items()))
    def test_a_mapped_past_posts_refusal_answers_its_status_and_names_its_reason(
        self, app, client, reason, status
    ):
        """The rollback case among them: past-posts rows still present after the
        capability was withdrawn answer `past_posts_not_activated` as a 503, not
        the bare 500 an unhandled `PastPostsRefused` would be."""
        from src.services.target.instagram_sources import PastPostsRefused

        async def refuse():
            raise PastPostsRefused(reason)

        app.add_api_route("/_test/past-posts-refusal", refuse)
        response = client.get("/_test/past-posts-refusal")
        assert response.status_code == status
        assert response.json() == {"detail": reason, "reason": reason}

    @pytest.mark.parametrize(
        "reason", ["past_posts_allocation_contract_failed", "past_posts_stale_capture"]
    )
    def test_any_other_past_posts_reason_is_a_500_not_a_guessed_client_status(
        self, app, client, reason
    ):
        """An internal invariant of the allocation or the fence is the
        server's fault: the logged 500 and its own sentence, never a status
        somebody chose for it and never the reason on the wire."""
        from src.services.target.instagram_sources import PastPostsRefused

        assert reason not in PAST_POSTS_REFUSALS

        async def refuse():
            raise PastPostsRefused(reason)

        app.add_api_route("/_test/past-posts-invariant", refuse)
        response = client.get("/_test/past-posts-invariant")
        assert response.status_code == 500
        assert response.json() == {"detail": "internal error"}

    @pytest.mark.usefixtures("stubbed_bound")
    def test_a_cancelled_health_statement_is_the_busy_answer(self, client, monkeypatch):
        """`health_reads.StatementTimedOut` is load, not a fault: the 503 a
        pool wait gets, under its own reason, retried after a second."""

        async def cancelled(executor):
            raise health_reads.StatementTimedOut(
                "canceling statement due to statement timeout"
            )

        monkeypatch.setattr(posting_health, "posting_freshness", cancelled)
        resp = client.get("/health/posting")
        assert resp.status_code == 503
        assert resp.json() == {
            "detail": "busy — try again",
            "reason": "statement_timeout",
        }
        assert resp.headers["retry-after"] == "1"


async def _fake_snapshot(executor, **kwargs):
    """The queue's backpressure seam, which the operating details read:
    stubbed so a unit test never reaches SQL."""
    return {
        "lanes": {
            "interactive": {"ready": 0, "oldest_age_s": 0.0},
            "bulk": {"ready": 2, "oldest_age_s": 12.5},
        },
        "outbox_pending": 4,
        "tg_global": {"paced_windows_last_minute": 0, "hold_active": False},
        "ws_oldest_wait": None,
    }


@pytest.mark.usefixtures("stubbed_bound")
class TestSchedulingHealthIsASecondSurface:
    """`/health` is Railway's liveness gate and must not open a
    connection; this is the dependency-touching check, and the two
    being separate endpoints is the design rather than an accident."""

    def test_it_is_not_the_railway_probe(self):
        """If these were one endpoint, either liveness opens a connection — and a
        database blip takes the service down for a fault no restart repairs — or
        the scheduling check touches nothing and reports healthy through any
        outage, which is exactly."""
        app = create_app(env={})
        paths = {r.path for r in app.routes}
        assert "/health" in paths
        assert "/health/scheduling" in paths

    def test_it_refuses_rather_than_reassures_when_it_cannot_look(self):
        """A monitor must distinguish "scheduling is fine" from "I could not
        look", so an absent engine is a 503 and never a cheerful
        zero."""
        app = create_app(env={})
        assert app.state.engine is None
        resp = TestClient(app).get("/health/scheduling")
        assert resp.status_code == 503

    def test_the_railway_probe_still_opens_no_connection(self):
        """The property that makes a second endpoint necessary — pinned here so
        no tidy-up can merge them."""
        app = create_app(env={})
        assert TestClient(app).get("/health").status_code == 200

    def test_it_reaches_the_query_when_an_engine_is_present(self, client, monkeypatch):
        """The branch production runs: an engine is present.

        Both tests above build `create_app(env={})`, so `app.state.engine` is
        None and both return at the 503 branch — ONE LINE ABOVE the code that
        runs in production. So this one takes the other branch. It needs no
        real database, and that is exactly the point: a fault at `UnitOfWork`
        construction comes before any SQL, so ANY non-None engine reaches it.
        """
        seen = []

        async def fake_lag(executor):
            seen.append(executor)
            return {"stalled": 0, "accounts_active": 7, "max_lag_seconds": None}

        async def fake_worker(executor):
            # This route has a SECOND seam. A route unit test has to
            # stub both or it reaches SQL, which this class's own fixture guard
            # catches.
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
        # The cursor axis keeps its exact shape, so a reader of the cursor axis
        # alone is unaffected by the worker axis. Asserted key-by-key rather
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
        """Both axes on ONE payload, deliberately.

        An external poller of this endpoint reads both axes from one URL, and
        the cursor axis keeps its keys beside the worker axis.
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
        # The cursor axis keeps its keys beside the worker axis.
        assert payload["accounts_active"] == 0
        assert payload["worker"]["succeeded_ever"] == 78
        assert payload["worker"]["last_success_age_seconds"] == 3600

    def test_the_public_payloads_are_exactly_what_the_classifiers_read(
        self, client, monkeypatch
    ):
        """Both axes are public because they are polled, so they carry what the
        classifiers in `storydump_cli/health_classify/` read and nothing more.
        This is both directions: a key added here is published to anyone, and a
        key dropped here is lost to the classifier. Most missing keys read as
        `unreachable` and page someone, but not all: the scheduling classifier
        reads a missing `max_lag_seconds` as null, so for that one this test is
        the only guard."""
        from storydump_cli.health_classify import posting as pm, scheduling as sm

        async def fake_lag(executor):
            return {"stalled": 0, "accounts_active": 1, "max_lag_seconds": 3}

        async def fake_worker(executor):
            return {
                "succeeded_ever": 1,
                "last_success_age_seconds": 30,
                "overdue_ready": 0,
                "max_overdue_seconds": None,
            }

        async def fake_freshness(executor):
            return {
                "posted_ever": 1,
                "last_post_age_seconds": 60,
                "intents_ever": 2,
                "oldest_intent_age_seconds": 600,
            }

        async def fake_attempts(executor):
            return {"debited_total": 1, "ledger_days": 1}

        async def fake_destinations(executor):
            return {"accounts_active": 1, "oldest_active_destination_age_seconds": 9}

        async def no_queue(executor, **kwargs):
            raise AssertionError("the public axes do not read the queue")

        monkeypatch.setattr(scheduling_health, "scheduling_lag", fake_lag)
        monkeypatch.setattr(scheduling_health, "worker_freshness", fake_worker)
        monkeypatch.setattr(backpressure, "snapshot", no_queue)
        monkeypatch.setattr(posting_health, "posting_freshness", fake_freshness)
        monkeypatch.setattr(posting_health, "publish_attempts", fake_attempts)
        monkeypatch.setattr(posting_health, "destinations", fake_destinations)

        scheduling = client.get("/health/scheduling").json()
        assert set(scheduling) == {*sm._COUNTS, sm._LAG, sm._WORKER}
        assert set(scheduling[sm._WORKER]) == {*sm._WORKER_COUNTS, *sm._WORKER_AGES}
        posting = client.get("/health/posting").json()
        assert set(posting) == set(pm._COUNTS + pm._AGES)


class TestTheQueueMovedToTheDetails:
    """The backpressure signal (the ready lanes, the pending outbox, the
    Telegram pacing) is the operating details', not an axis of public
    `/health/scheduling`."""

    @staticmethod
    def state(engine):
        return SimpleNamespace(engine=engine, health_cache=AnswerCache())

    def test_the_details_carry_it_asked_without_identification(self, monkeypatch):
        seen = {}

        async def fake_snapshot(executor, **kwargs):
            seen.update(kwargs)
            return await _fake_snapshot(executor, **kwargs)

        monkeypatch.setattr(backpressure, "snapshot", fake_snapshot)
        body = asyncio.run(health_routes.queue_pressure(self.state(FakeEngine())))
        assert body["outbox_pending"] == 4
        assert not seen.get("identify"), seen
        assert "workspace_id" not in str(body)

    def test_no_engine_is_none_and_a_failed_read_is_named(self, monkeypatch):
        async def broken(executor, **kwargs):
            raise TimeoutError("pool")

        monkeypatch.setattr(backpressure, "snapshot", broken)
        assert asyncio.run(health_routes.queue_pressure(self.state(None))) is None
        failed = asyncio.run(health_routes.queue_pressure(self.state(FakeEngine())))
        assert failed == {"error": "TimeoutError"}

    def test_a_database_that_stops_answering_is_named_not_waited_on(self, monkeypatch):
        async def hangs(executor, **kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(backpressure, "snapshot", hangs)
        monkeypatch.setattr(health_routes, "QUEUE_READ_TIMEOUT_S", 0.01)
        slow = asyncio.run(health_routes.queue_pressure(self.state(FakeEngine())))
        assert slow == {"error": "TimeoutError"}


@pytest.mark.usefixtures("stubbed_bound")
class TestPostingHealthIsATHIRDSurface:
    """`/health/scheduling` reads the clock and the worker, and both
    can stay true while nothing posts. This route answers the one question
    they cannot: did a post LAND."""

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
                # refuses a payload where they disagree. A fixed age here would
                # make the `accounts_active=0` case an estate that cannot
                # exist, which the pair guard refuses.
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

        A test that builds `create_app(env={})` returns at the 503 branch one
        line above the production path, and every green signal it produces is
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
        """The route and the poller's classifier are one repo and one
        test suite on purpose, so the wire contract cannot drift. `classify`
        rejects a missing or mistyped key as `unreachable`; this drives the
        REAL classifier over the REAL route body rather than asserting a
        hand-written copy of it."""
        from storydump_cli.health_classify.posting import (
            NEVER_POSTED_OVERDUE,
            classify,
        )

        self._stub_seams(monkeypatch, accounts_active=0)
        resp = client.get("/health/posting")

        verdict = classify(
            resp.status_code,
            resp.text,
            silence_s=48 * 3600,
            grace_s=72 * 3600,
            watched_s=16 * 24 * 3600,
        )
        # Not merely "parsed": a never-posted payload past its grace is overdue.
        assert verdict.state == NEVER_POSTED_OVERDUE

    def test_the_railway_probe_still_opens_no_connection(self):
        """Three health surfaces, and one reason for the split: liveness must
        not open a connection or a database blip takes the service down for a
        fault no restart repairs."""
        app = create_app(env={})
        assert TestClient(app).get("/health").status_code == 200


@pytest.mark.usefixtures("stubbed_bound")
class TestDeliveryHealthIsAFOURTHSurface:
    """Deliveries failing and posts not landing are independent causes,
    so the outbox's failures get their own surface rather than a key in either
    of the other two payloads, which would rank one cause against the other."""

    def test_it_is_its_own_route_beside_the_other_three(self):
        paths = {r.path for r in create_app(env={}).routes}
        assert {
            "/health",
            "/health/scheduling",
            "/health/posting",
            "/health/delivery",
        } <= paths

    def test_it_refuses_rather_than_reassures_when_it_cannot_look(self):
        """No engine is a 503, never an hour of zero failures."""
        app = create_app(env={})
        assert app.state.engine is None
        assert TestClient(app).get("/health/delivery").status_code == 503

    def test_it_reaches_its_seam(self, client, monkeypatch):
        seen = []

        async def fake(executor):
            seen.append(executor)
            return {
                "window_seconds": 3600,
                "sent_in_window": 3,
                "failed_or_ambiguous": 0,
                "by_class": {},
            }

        monkeypatch.setattr(delivery_health, "outbox_failures", fake)
        resp = client.get("/health/delivery")
        assert resp.status_code == 200, resp.text
        assert resp.json()["sent_in_window"] == 3
        (executor,) = seen
        assert hasattr(executor, "execute")

    @staticmethod
    def _get(engine, client, failures, sent=40):
        """The REAL `delivery_health` behind the route, over outbox failure records' doors
        scripted on the conftest engine's connection."""
        engine.session = doors = _Doors(failures, sent)
        resp = client.get("/health/delivery")
        assert resp.status_code == 200, resp.text
        return resp, doors

    def test_the_body_is_aggregates_only(self, engine, client):
        """The route is unauthenticated: counts and codes, and nothing that
        names a workspace, a chat or a message."""
        resp, doors = self._get(
            engine,
            client,
            [("destination_gone", 403, 5, 5), ("rate_limited", 429, 9, 0)],
        )
        body = resp.json()
        assert body["window_seconds"] == 3600 and body["sent_in_window"] == 40
        assert body["failed_or_ambiguous"] == 5
        assert body["by_class"]["destination_gone"] == {
            "rows": 5,
            "alerting": 5,
            "codes": {"403": 5},
        }
        assert body["by_class"]["rate_limited"]["alerting"] == 0
        for word in ("workspace", "chat", "binding", "payload", "external"):
            assert word not in resp.text
        assert len(doors.statements) == 2


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
        role = details(client)["db_role"]
        if role is not None:
            return role
        time.sleep(0.02)
    return details(client)["db_role"]


class TestHealthReportsTheDatabaseRole:
    """The operating details say which database login the API holds, and
    whether that login bypasses row-level security.

    The deployed API connects as `svc_ingress`. The owner role applies schema
    changes. Reporting the login and its RLS attributes exposes an accidental
    privileged runtime connection.

    The value is sampled ONCE, at startup, in the background. The probe itself
    still opens no connection: that property is what keeps a database blip from
    taking the service down for a fault no restart repairs, and it is pinned
    below by counting connections.
    """

    def test_without_startup_the_role_is_unknown_not_invented(self):
        app = create_app(env={})
        assert details(TestClient(app))["db_role"] is None

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
            role = details(client)["db_role"]
        assert resp.status_code == 200
        assert role is None


class TestTheApiRegistersItsOwnWebhook:
    """With the bot token and the webhook secret set, startup registers the
    door on the bot and `/health` reports what Telegram holds — never a
    secret."""

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
            body = details(client)
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
                live = details(client).get("webhook_live")
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
    """The operating details name the pool's numbers and how many processes
    serve the ingress, so the connection inequality in
    `src/services/target/unit_of_work.py` can be read off them."""

    def test_without_an_engine_the_pool_is_none_and_workers_default_to_one(self):
        from fastapi.testclient import TestClient

        body = details(TestClient(create_app(env={})))
        assert body["pool"] is None and body["ingress_workers"] == 1

    def test_web_concurrency_names_the_process_count(self):
        from fastapi.testclient import TestClient

        body = details(TestClient(create_app(env={"WEB_CONCURRENCY": "2"})))
        assert body["ingress_workers"] == 2

    def test_with_an_engine_the_pool_snapshot_is_reported(self, monkeypatch):
        from fastapi.testclient import TestClient

        from src.api import app as app_module
        from src.services.target import unit_of_work as uow

        engine = uow.create_engine(
            "postgresql+asyncpg://u:not-a-real-password@localhost:1/db",
            pool_timeout=uow.INGRESS_POOL_TIMEOUT_SEAM,
        )
        monkeypatch.setattr(app_module, "_engine_from_env", lambda env: engine)
        try:
            body = details(TestClient(create_app(env={})))
            assert body["pool"] == {
                "size": 10,
                "overflow": 0,
                "timeout_s": 1.0,
                "checked_out": 0,
                "checked_out_peak": 0,
            }
        finally:
            engine.sync_engine.dispose()


class TestTheInvitationEmailSender:
    """The API composes the invitation email's sender: it sends after the
    invitation commits, so its link never rests in a job."""

    ENV = {"RESEND_API_KEY": "re_secret_value", "EMAIL_FROM": "Storydump <a@b.com>"}

    def test_both_variables_compose_a_sender_that_sends_once(self):
        from src.services.target import email_sender

        app = create_app(engine=FakeEngine(), env=self.ENV)
        assert isinstance(app.state.email, email_sender.ResendSender)
        assert app.state.email.policy is email_sender.INTERACTIVE_POLICY

    @pytest.mark.parametrize(
        "env", [{}, {"RESEND_API_KEY": "re_k"}, {"EMAIL_FROM": "a@b.com"}]
    )
    def test_anything_less_composes_none(self, env):
        assert create_app(engine=FakeEngine(), env=env).state.email is None

    def test_the_startup_line_says_whether_and_names_no_value(self, monkeypatch):
        from src.api import app as app_module

        said = []
        monkeypatch.setattr(
            app_module.logger, "info", lambda msg, *a: said.append(msg % a)
        )
        create_app(engine=FakeEngine(), env=self.ENV)
        create_app(engine=FakeEngine(), env={})
        text = " | ".join(said)
        assert "invitation email is configured" in text
        assert "invitation email is not configured" in text
        assert "re_secret_value" not in text
        assert "a@b.com" not in text
