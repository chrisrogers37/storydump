"""The OAuth history choice on the web adapter (#1644), every service
seam patched: the start body's refusals and what the state records, the
connect preview and the applied-result resource at their floors, and the
callback's past-posts source landing — its order, its refusal and its absence
without a capability. The database path, every matrix row under the real
roles, is `tests/scripts/test_oauth_past_posts_choice_gate.py`.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.api import app as app_module
from src.api import principal as principal_mod
from src.api.principal import COOKIE
from src.api.routes import auth, v1
from src.config.settings import settings
from src.services.target import (
    connect_choice,
    identity_mappings,
    ig_login_oauth,
    provisioning,
    rate_counters,
    sessions,
    tenant_resolution,
)
from src.services.target.instagram_sources import PastPostsCapability, PastPostsRefused
from tests.src.api.conftest import FRONT, PRINCIPAL, WS

ACCOUNT = "55555555-5555-4555-8555-555555555555"
USER = "22222222-2222-2222-2222-222222222222"
RESULT = "88888888-8888-4888-8888-888888888888"
LIVE = PastPostsCapability(enabled=True)
#: `live_revision` as shipped, captured before any fixture replaces it: the
#: no-capability callback test runs the real one.
REAL_LIVE_REVISION = connect_choice.live_revision
#: "The capability was never injected", which is the default state.
ABSENT = object()

#: The app-scoped id the exchange proved: not the account's own id.
APP_SCOPED = "990000000000007"
CHOICE = {
    "past_posts_choice": {
        "use_history": True,
        "expected_mix_revision": 8,
        "preview_version": 1,
    }
}
ADD = f"/api/v1/workspaces/{WS}/accounts/connect"
ROW = f"/api/v1/workspaces/{WS}/accounts/{ACCOUNT}/connect"
PREVIEW = f"/api/v1/workspaces/{WS}/accounts/connect-preview"


def test_every_refusal_reason_has_a_status():
    assert set(app_module._PAST_POSTS_CHOICE_STATUS) == set(connect_choice.REASONS)


@pytest.fixture
def instagram_configured(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "app-1", raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)
    monkeypatch.setattr(
        settings, "OAUTH_REDIRECT_BASE_URL", "https://api.example.test", raising=False
    )


@pytest.fixture
def issued(monkeypatch):
    """What the start asked `issue_state` to mint."""
    seen = {}

    async def issue_state(session, **kw):
        seen.update(kw)
        return "st4te"

    monkeypatch.setattr(v1, "issue_state", issue_state)
    return seen


@pytest.fixture
def past_posts(app, monkeypatch):
    """The capability injected, the mix's live revision and a named account's
    facts behind their seams; `purpose` is the row start's own."""
    world = {
        "revision": 8,
        "facts": {"handle": "example_handle", "has_past_posts": False},
        "purpose": "connect",
    }
    app.state.past_posts_capability = LIVE

    async def live_revision(executor, *, workspace_id, capability):
        assert capability is LIVE
        return world["revision"]

    async def account(executor, ws, account_id):
        return world["facts"]

    async def connect_purpose(session, *, workspace_id, ig_account_id):
        return world["purpose"]

    monkeypatch.setattr(connect_choice, "live_revision", live_revision)
    monkeypatch.setattr(connect_choice, "_account", account)
    monkeypatch.setattr(ig_login_oauth, "connect_purpose", connect_purpose)
    return world


PAST_POSTS_KEYS = {
    "past_posts_entry",
    "past_posts_use_history",
    "past_posts_expected_mix_revision",
    "past_posts_preview_version",
}


class TestStart:
    @pytest.mark.parametrize("payload", [None, {}, {"past_posts_choice": None}])
    def test_no_choice_without_the_past_posts_records_no_past_posts_keys(
        self, client, signed_in, tenant, instagram_configured, issued, payload
    ):
        resp = (
            client.post(ADD, json=payload) if payload is not None else client.post(ADD)
        )
        assert resp.status_code == 200, resp.text
        assert "state=st4te" in resp.json()["authorization_url"]
        assert not PAST_POSTS_KEYS & set(issued)
        assert issued["reconnect_target"] is None

    @pytest.mark.parametrize(
        "content",
        [
            b'{"past_posts": null}',
            b'{"past_posts_choice": {"use_history": true, "expected_mix_revision": 8,'
            b' "preview_version": 2}}',
            b'{"past_posts_choice": {"use_history": "yes", "expected_mix_revision": 8,'
            b' "preview_version": 1}}',
            b"[1]",
            b"null",
            b"{not json",
        ],
        ids=[
            "unknown-key",
            "another-preview",
            "wrong-type",
            "an-array",
            "json-null",
            "not-json",
        ],
    )
    def test_a_malformed_body_is_refused_by_name_before_any_state(
        self, client, signed_in, tenant, instagram_configured, issued, content
    ):
        resp = client.post(
            ADD, content=content, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["reason"] == "invalid_past_posts_choice"
        assert issued == {}
        # Refused before a unit of work opened, so before a connection was taken.
        assert tenant == []

    @pytest.mark.parametrize("url", [ADD, ROW], ids=["header-add", "row-start"])
    def test_the_body_is_read_before_the_session_opens(
        self,
        client,
        signed_in,
        tenant,
        instagram_configured,
        issued,
        past_posts,
        monkeypatch,
        url,
    ):
        """A slow client must not hold a pooled connection while it sends: the
        body is in hand, and judged, before the unit of work opens."""
        read = principal_mod.json_object

        async def spy(request):
            tenant.append(("body",))
            return await read(request)

        monkeypatch.setattr(principal_mod, "json_object", spy)
        resp = client.post(url, json=CHOICE)
        assert resp.status_code == 200, resp.text
        steps = [step[0] for step in tenant]
        assert steps.count("body") == 1
        assert steps[:2] == ["body", "uow"]

    def test_a_choice_where_the_past_posts_is_not_live_is_503(
        self, client, signed_in, tenant, instagram_configured, issued
    ):
        resp = client.post(ADD, json=CHOICE)
        assert resp.status_code == 503
        assert resp.json()["reason"] == "past_posts_not_activated"
        assert issued == {}

    def test_a_stale_on_preview_is_a_mix_conflict_before_any_state(
        self, client, signed_in, tenant, instagram_configured, issued, past_posts
    ):
        past_posts["revision"] = 9
        resp = client.post(ADD, json=CHOICE)
        assert resp.status_code == 409
        assert resp.json() == {"reason": "mix_conflict", "current_revision": 9}
        assert issued == {}

    def test_a_live_add_records_its_choice_in_the_state(
        self, client, signed_in, tenant, instagram_configured, issued, past_posts
    ):
        resp = client.post(ADD, json=CHOICE)
        assert resp.status_code == 200, resp.text
        assert {key: issued[key] for key in PAST_POSTS_KEYS} == {
            "past_posts_entry": "add",
            "past_posts_use_history": True,
            "past_posts_expected_mix_revision": 8,
            "past_posts_preview_version": 1,
        }
        assert issued["reconnect_target"] is None and issued["purpose"] == "connect"

    def test_a_first_connect_records_its_entry_and_target(
        self, client, signed_in, tenant, instagram_configured, issued, past_posts
    ):
        resp = client.post(ROW, json=CHOICE)
        assert resp.status_code == 200, resp.text
        assert issued["past_posts_entry"] == "first_connect"
        assert issued["reconnect_target"] == ACCOUNT

    def test_a_reconnect_refuses_a_choice_and_records_its_entry_without_one(
        self, client, signed_in, tenant, instagram_configured, issued, past_posts
    ):
        past_posts["purpose"] = "reconnect"
        refused = client.post(ROW, json=CHOICE)
        assert refused.status_code == 400
        assert refused.json()["reason"] == "invalid_past_posts_choice"
        assert issued == {}
        resp = client.post(ROW, json={"past_posts_choice": None})
        assert resp.status_code == 200, resp.text
        assert issued["purpose"] == "reconnect"
        assert {key: issued.get(key) for key in PAST_POSTS_KEYS} == {
            "past_posts_entry": "reconnect",
            "past_posts_use_history": None,
            "past_posts_expected_mix_revision": None,
            "past_posts_preview_version": None,
        }


class TestPreviewRoute:
    @pytest.fixture
    def previewed(self, monkeypatch):
        seen = {}

        async def preview(session, **kw):
            seen.update(kw)
            return {"entry": "seen"}

        monkeypatch.setattr(connect_choice, "preview", preview)
        return seen

    def test_requires_a_session(self, client):
        assert client.get(PREVIEW).status_code == 401

    def test_the_header_add_previews_at_its_floor(
        self, app, client, signed_in, tenant, previewed
    ):
        app.state.past_posts_capability = LIVE
        resp = client.get(PREVIEW)
        assert resp.status_code == 200 and resp.json() == {"entry": "seen"}
        assert previewed == {
            "workspace_id": WS,
            "account_id": None,
            "purpose": "connect",
            "capability": LIVE,
        }
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "admin"),
        ]

    def test_a_named_account_previews_at_its_start_purpose(
        self, client, signed_in, tenant, previewed, monkeypatch
    ):
        asked = []

        async def connect_purpose(session, *, workspace_id, ig_account_id):
            asked.append((workspace_id, ig_account_id))
            return "reconnect"

        monkeypatch.setattr(ig_login_oauth, "connect_purpose", connect_purpose)
        resp = client.get(PREVIEW, params={"account_id": ACCOUNT})
        assert resp.status_code == 200
        assert asked == [(WS, ACCOUNT)]
        assert previewed["account_id"] == ACCOUNT
        assert previewed["purpose"] == "reconnect"
        assert previewed["capability"] is None

    def test_an_account_that_is_not_this_workspaces_is_404(
        self, client, signed_in, tenant, previewed, monkeypatch
    ):
        async def connect_purpose(session, **kw):
            return None

        monkeypatch.setattr(ig_login_oauth, "connect_purpose", connect_purpose)
        assert client.get(PREVIEW, params={"account_id": ACCOUNT}).status_code == 404
        assert previewed == {}

    def test_a_non_uuid_account_is_refused_before_any_seam(
        self, client, signed_in, tenant, previewed
    ):
        assert client.get(PREVIEW, params={"account_id": "x"}).status_code == 422
        assert tenant == [] and previewed == {}


class TestResultRoute:
    URL = f"/api/v1/workspaces/{WS}/connection-results/{RESULT}"

    @pytest.fixture
    def stored(self, monkeypatch):
        holder = {"value": None, "asked": None}

        async def result(session, **kw):
            holder["asked"] = kw
            return holder["value"]

        monkeypatch.setattr(connect_choice, "result", result)
        return holder

    def test_it_reads_as_the_session_principal_at_the_member_floor(
        self, client, signed_in, tenant, stored
    ):
        stored["value"] = {
            "account_id": ACCOUNT,
            "source_id": RESULT,
            "outcome": "connected_history_off_mix_conflict",
            "history_enabled": False,
            "mix_revision": 10,
            "mix_reset_applied": False,
            "next_action": "review_mix",
            "current_mix_revision": 10,
            "created_at": datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc),
        }
        resp = client.get(self.URL)
        assert resp.status_code == 200
        assert resp.json()["next_action"] == "review_mix"
        # A reader's datetime rides FastAPI's `jsonable_encoder`, as it does on
        # every route here (`+00:00`, never `Z`): the wire form the Drive status
        # and token routes' tests pin too.
        assert resp.json()["created_at"] == "2026-10-12T12:00:00+00:00"
        assert stored["asked"] == {
            "workspace_id": WS,
            "result_id": RESULT,
            "user_id": PRINCIPAL.user_id,
            "session_id": PRINCIPAL.session_id,
        }
        assert tenant[-1] == ("gate", WS, PRINCIPAL.user_id, "member")

    def test_no_result_for_this_person_and_session_is_the_house_404(
        self, client, signed_in, tenant, stored
    ):
        resp = client.get(self.URL)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}

    def test_a_non_uuid_result_is_refused_before_any_seam(
        self, client, signed_in, tenant, stored
    ):
        resp = client.get(f"/api/v1/workspaces/{WS}/connection-results/x")
        assert resp.status_code == 422
        assert tenant == [] and stored["asked"] is None


class TestCallbackLanding:
    """`GET /auth/instagram-login/callback` where the past-posts source is live: the
    choice lands after the destination and its credential, with the prior
    grant read before the credential, and the browser returns to Settings
    with the applied result's id. Everything behind a seam, in order."""

    URL = "/auth/instagram-login/callback?state=st-ig&code=c0de"

    @pytest.fixture
    def flow(self, app, client, google_configured, monkeypatch):
        monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "4242", raising=False)
        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)
        log = []
        world = {
            "row": {
                "state": "st-ig",
                "purpose": "connect",
                "provider": "ig_login",
                "user_id": USER,
                "workspace_id": WS,
                "reconnect_target": None,
                "past_posts_entry": "add",
                "past_posts_use_history": True,
                "past_posts_expected_mix_revision": 8,
                "past_posts_preview_version": 1,
            },
            "revision": 8,
            "purpose": "connect",
            "land": None,
            "landed": None,
            "mapped": None,
            "app_scoped_id": APP_SCOPED,
        }

        async def increment(conn, **kw):
            return 1

        async def consume_state(conn, *, state, **kw):
            return dict(world["row"])

        async def resolve(conn, *, token_hash):
            return SimpleNamespace(id="sess-1", user_id=USER)

        async def exchange_code(client_, **kw):
            log.append(("exchange",))
            return ig_login_oauth.IgGrant(
                access_token="IGQVJ-long",
                expires_at=None,
                ig_user_id="17841400000000001",
                username="example_handle",
                app_scoped_id=world["app_scoped_id"],
            )

        class _Uow:
            def __init__(self, engine, tenant_id, **kw):
                log.append(("uow", tenant_id))

            def begin(self):
                @asynccontextmanager
                async def _cm():
                    yield SimpleNamespace()

                return _cm()

        async def authorize_member(session, workspace_id, user_id, minimum_role):
            log.append(("gate", workspace_id, user_id, minimum_role))

        async def live_revision(session, *, workspace_id, capability):
            log.append(("live", workspace_id, capability))
            return world["revision"]

        async def connect(session, *, workspace_id, provider_account_ref, handle, **kw):
            log.append(("connect", workspace_id, kw.get("ig_account_id")))
            return ACCOUNT, True

        async def connect_purpose(session, *, workspace_id, ig_account_id):
            log.append(("prior", workspace_id, ig_account_id))
            return world["purpose"]

        async def store_credential(session, *, workspace_id, ig_account_id, **kw):
            log.append(("store", workspace_id, ig_account_id))
            return "cred-1"

        async def record(session, **kw):
            log.append(("map",))
            world["mapped"] = kw
            return True

        async def land_choice(session, **kw):
            log.append(("land",))
            world["landed"] = kw
            if world["land"] is not None:
                raise world["land"]
            return RESULT

        monkeypatch.setattr(rate_counters, "increment", increment)
        monkeypatch.setattr(auth, "consume_state", consume_state)
        monkeypatch.setattr(sessions, "resolve", resolve)
        monkeypatch.setattr(ig_login_oauth, "exchange_code", exchange_code)
        monkeypatch.setattr(auth, "unit_of_work", _Uow)
        monkeypatch.setattr(tenant_resolution, "authorize_member", authorize_member)
        monkeypatch.setattr(connect_choice, "live_revision", live_revision)
        monkeypatch.setattr(provisioning, "connect_destination", connect)
        monkeypatch.setattr(ig_login_oauth, "connect_purpose", connect_purpose)
        monkeypatch.setattr(ig_login_oauth, "store_credential", store_credential)
        monkeypatch.setattr(connect_choice, "land_choice", land_choice)
        # The writer's own seam: `record_oauth_exchange`, which decides whether
        # there is anything to map, runs for real.
        monkeypatch.setattr(identity_mappings, "record", record)
        client.cookies.set(COOKIE, "opaque-session-value")
        app.state.past_posts_capability = LIVE
        world["log"] = log
        return world

    def test_the_choice_lands_after_the_credential_and_returns_its_result(
        self, client, flow
    ):
        resp = client.get(self.URL, follow_redirects=False)
        assert resp.status_code == 302, resp.text
        assert resp.headers["location"] == (
            f"{FRONT}/dashboard/settings?tab=accounts&connection={RESULT}"
        )
        assert flow["log"] == [
            ("exchange",),
            ("uow", WS),
            ("gate", WS, USER, "admin"),
            ("live", WS, LIVE),
            ("connect", WS, None),
            ("prior", WS, ACCOUNT),
            ("store", WS, ACCOUNT),
            ("map",),
            ("land",),
        ]
        landed = flow["landed"]
        assert landed["workspace_id"] == WS and landed["ig_account_id"] == ACCOUNT
        assert landed["had_prior_grant"] is False
        assert landed["session_id"] == "sess-1"
        assert landed["capability"] is LIVE
        assert landed["state"]["past_posts_use_history"] is True
        assert landed["state"]["user_id"] == USER

    def test_one_authorization_is_shared_by_the_mapping_and_the_past_posts(
        self, client, flow
    ):
        """One id and one time for the mapping row and for the past-posts source's
        authorization: the generation and the identity it was verified under are
        joined on `authorization_id`. The generation keeps the account's own id
        as its subject; the mapping carries the app-scoped id beside it."""
        resp = client.get(self.URL, follow_redirects=False)
        assert resp.status_code == 302, resp.text
        authorization = flow["landed"]["authorization"]
        assert uuid.UUID(authorization.authorization_id).version == 4
        assert authorization.app_id == "4242"
        assert authorization.subject_id == "17841400000000001"
        assert authorization.authorized_at.utcoffset() == timedelta(0)
        assert flow["mapped"] == {
            "workspace_id": WS,
            "ig_account_id": ACCOUNT,
            "credential_id": "cred-1",
            "authorization_id": authorization.authorization_id,
            "app_identity": "instagram:4242",
            "subject_ref": APP_SCOPED,
            "account_ref": "17841400000000001",
            "method": "oauth_exchange",
            "authorized_at": authorization.authorized_at,
            "verified_at": authorization.authorized_at,
        }

    def test_two_callbacks_never_share_an_authorization(self, client, flow):
        ids = []
        for _ in range(2):
            assert client.get(self.URL, follow_redirects=False).status_code == 302
            ids.append(flow["mapped"]["authorization_id"])
        assert ids[0] != ids[1]

    @pytest.mark.parametrize("proven", [None, ""], ids=["no-id", "empty"])
    def test_an_exchange_that_proved_no_app_scoped_id_refuses_the_landing(
        self, client, flow, proven
    ):
        """Nothing can be mapped, so the past-posts source landing is refused whole: the
        error page names `history_setup_failed`, and neither the mapping nor
        the past-posts source was written (the unit of work rolls the rest back)."""
        flow["app_scoped_id"] = proven
        resp = client.get(self.URL, follow_redirects=False)
        assert resp.headers["location"] == (
            f"{FRONT}/auth/error?reason=history_setup_failed&flow=instagram"
        )
        assert [e[0] for e in flow["log"]] == [
            "exchange",
            "uow",
            "gate",
            "live",
            "connect",
            "prior",
            "store",
        ]
        assert flow["mapped"] is None and flow["landed"] is None

    @pytest.mark.parametrize("app_id", [None, "", "app-1", "42a"])
    def test_a_deployment_naming_no_instagram_app_refuses_the_landing(
        self, client, flow, monkeypatch, app_id
    ):
        """No app identity to bind the mapping to. The unconfigured id (unset
        is a 503 before any of this) is no app: it is refused, never mapped."""
        monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", app_id, raising=False)
        resp = client.get(self.URL, follow_redirects=False)
        if app_id in (None, ""):
            assert resp.status_code == 503, "unset is refused before the exchange"
            assert flow["mapped"] is None
            return
        assert resp.headers["location"] == (
            f"{FRONT}/auth/error?reason=history_setup_failed&flow=instagram"
        )
        assert flow["mapped"] is None and flow["landed"] is None

    def test_a_prior_grant_is_the_accounts_never_the_states_purpose(self, client, flow):
        flow["purpose"] = "reconnect"
        assert flow["row"]["purpose"] == "connect"
        client.get(self.URL, follow_redirects=False)
        assert flow["landed"]["had_prior_grant"] is True

    def test_an_unconverted_mix_makes_no_past_posts_write(self, client, flow):
        flow["revision"] = None
        resp = client.get(self.URL, follow_redirects=False)
        assert resp.headers["location"] == (
            f"{FRONT}/dashboard/settings?connected=instagram"
        )
        assert [e[0] for e in flow["log"]] == [
            "exchange",
            "uow",
            "gate",
            "live",
            "connect",
            "store",
        ]

    def test_a_refused_history_setup_lands_on_the_error_page(self, client, flow):
        flow["land"] = PastPostsRefused("past_posts_credential_unavailable")
        resp = client.get(self.URL, follow_redirects=False)
        assert resp.headers["location"] == (
            f"{FRONT}/auth/error?reason=history_setup_failed&flow=instagram"
        )

    @pytest.mark.parametrize(
        "injected",
        [ABSENT, None, PastPostsCapability(enabled=False)],
        ids=["never-injected", "none", "disabled"],
    )
    def test_without_an_enabled_capability_the_callback_makes_no_past_posts_write(
        self, app, client, flow, monkeypatch, injected
    ):
        """No enabled capability, though the state recorded an On choice: the
        REAL `live_revision` answers before any statement (the unit of work's
        session here has no `execute`, so a statement would raise), and the
        landing makes only the connect writes: the exchange, the unit of work,
        the admin gate, the destination, the credential. No prior-grant read,
        no past-posts source landing, no result, and the redirect is
        `?connected=instagram`."""
        if injected is ABSENT:
            del app.state.past_posts_capability
        else:
            app.state.past_posts_capability = injected
        monkeypatch.setattr(connect_choice, "live_revision", REAL_LIVE_REVISION)
        assert flow["row"]["past_posts_use_history"] is True

        resp = client.get(self.URL, follow_redirects=False)

        assert resp.status_code == 302, resp.text
        assert resp.headers["location"] == (
            f"{FRONT}/dashboard/settings?connected=instagram"
        )
        assert flow["log"] == [
            ("exchange",),
            ("uow", WS),
            ("gate", WS, USER, "admin"),
            ("connect", WS, None),
            ("store", WS, ACCOUNT),
        ]
        assert flow["landed"] is None
