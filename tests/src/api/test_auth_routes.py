"""`/auth` — sign-in hosted on the API, with the provider and the database
behind seams.

The properties that matter: sign-in refuses (503) rather than half-works when
unconfigured; the redirect carries a state the database issued and a nonce
derived from it, and the CSRF cookie the callback will demand; the pre-auth
counter keys on the ATTRIBUTED client (the #776 property, on the control that
replaced the limiter); every callback failure lands on the front end's error
page with a closed reason; success sets the session cookie with `07` §1's
attributes; sign-out revokes.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from src.api.principal import COOKIE, session_delivery_gap
from src.api.routes import auth
from src.config.settings import settings
from src.services.target import (
    google_oidc,
    identity,
    ig_login_oauth,
    provisioning,
    rate_counters,
    sessions,
    tenant_resolution,
)
from src.services.target.ig_login_oauth import OAuthStateRefused
from tests.src.api.conftest import (
    API,
    COOKIE_DOMAIN,
    FRONT,
    cookie_header,
    unsigned_id_token,
)


@pytest.fixture
def configured(google_configured):
    """The sign-in world from conftest, under the name these tests read."""


@pytest.fixture
def counter(monkeypatch):
    """The pre-auth counter: records keys, answers `value`."""
    log = {"keys": [], "value": 1}

    async def increment(conn, *, scope, key, window_start, limit):
        assert scope == auth.PREAUTH_SCOPE and limit == auth.PREAUTH_LIMIT
        log["keys"].append(key)
        return log["value"]

    monkeypatch.setattr(rate_counters, "increment", increment)
    return log


@pytest.fixture
def state_store(monkeypatch):
    """`oauth_states` behind a seam: issue records the nonce it was given,
    consume checks the presented nonce against it."""
    store = {}

    async def issue_state(conn, *, purpose, provider, cookie_nonce, **kw):
        assert (purpose, provider) == ("signin", "google")
        store["state"] = "st-1"
        store["nonce"] = cookie_nonce
        return "st-1"

    async def consume_state(conn, *, state, cookie_nonce=None, **kw):
        if state != store.get("state") or cookie_nonce != store.get("nonce"):
            raise OAuthStateRefused("no")
        store["consumed"] = True
        return {"state": state, "purpose": "signin"}

    monkeypatch.setattr(auth, "issue_state", issue_state)
    monkeypatch.setattr(auth, "consume_state", consume_state)
    return store


class TestSignin:
    def test_unconfigured_is_a_503_naming_what_is_missing(self, client, monkeypatch):
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None, raising=False)
        resp = client.get("/auth/google", follow_redirects=False)
        assert resp.status_code == 503
        assert "GOOGLE_CLIENT_ID" in resp.json()["detail"]

    def test_redirects_to_google_with_an_issued_state_and_the_csrf_cookie(
        self, client, configured, counter, state_store
    ):
        resp = client.get("/auth/google", follow_redirects=False)
        assert resp.status_code == 302
        loc = urlsplit(resp.headers["location"])
        assert loc.netloc == "accounts.google.com"
        q = parse_qs(loc.query)
        assert q["state"] == ["st-1"]
        assert q["nonce"] == [google_oidc.nonce_for("st-1")]
        assert q["redirect_uri"] == [f"{API}/auth/google/callback"]
        cookie = cookie_header(resp, auth.NONCE_COOKIE)
        assert "HttpOnly" in cookie and "Path=/auth/google" in cookie
        assert state_store["nonce"] and state_store["nonce"] in cookie

    def test_over_the_preauth_limit_is_429(
        self, client, configured, counter, state_store
    ):
        counter["value"] = None
        assert client.get("/auth/google", follow_redirects=False).status_code == 429

    def test_the_preauth_counter_keys_on_the_attributed_peer(
        self, app, configured, counter, state_store
    ):
        """A caller who is NOT a trusted proxy cannot choose their own bucket
        by writing X-Forwarded-For: the key is the raw peer (#726/#776)."""
        client = TestClient(app, client=("203.0.113.9", 4321))
        client.get(
            "/auth/google",
            headers={"X-Forwarded-For": "198.51.100.7"},
            follow_redirects=False,
        )
        assert counter["keys"] == ["203.0.113.9"]


class TestCallback:
    def _signin(self, client):
        resp = client.get("/auth/google", follow_redirects=False)
        state = parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]
        nonce = cookie_header(resp, auth.NONCE_COOKIE).split(";")[0].split("=", 1)[1]
        return state, nonce

    @pytest.mark.parametrize(
        "query, reason",
        [
            ("error=access_denied", "denied"),
            ("state=st-1", "missing_params"),
            ("code=c", "missing_params"),
        ],
    )
    def test_denied_or_incomplete_lands_on_the_error_page(
        self, client, configured, counter, query, reason
    ):
        resp = client.get(f"/auth/google/callback?{query}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == f"{FRONT}/auth/error?reason={reason}"

    def test_a_state_without_its_cookie_is_refused(
        self, client, configured, counter, state_store
    ):
        state, _ = self._signin(client)
        client.cookies.clear()
        resp = client.get(
            f"/auth/google/callback?state={state}&code=c", follow_redirects=False
        )
        assert resp.headers["location"] == f"{FRONT}/auth/error?reason=state_refused"

    def test_a_refused_exchange_lands_on_the_error_page(
        self, client, configured, counter, state_store, monkeypatch
    ):
        state, nonce = self._signin(client)

        async def exchange_code(client_, **kw):
            raise google_oidc.OidcRefused("exchange_failed")

        monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
        resp = client.get(
            f"/auth/google/callback?state={state}&code=c",
            cookies={auth.NONCE_COOKIE: nonce},
            follow_redirects=False,
        )
        assert resp.headers["location"] == f"{FRONT}/auth/error?reason=exchange_failed"

    def test_signs_in_upserts_on_sub_and_sets_the_session_cookie(
        self, client, configured, counter, state_store, monkeypatch
    ):
        state, nonce = self._signin(client)
        seen = {}

        async def exchange_code(
            client_, *, code, redirect_uri, client_id, client_secret
        ):
            seen["exchange"] = (code, redirect_uri, client_id)
            return unsigned_id_token(state)

        async def upsert(conn, *, sub, email, display_name):
            seen["upsert"] = (sub, email, display_name)
            return "user-uuid"

        async def issue(conn, *, user_id):
            seen["issue"] = user_id
            return "opaque-value"

        monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
        monkeypatch.setattr(identity, "upsert_google_identity", upsert)
        monkeypatch.setattr(sessions, "issue", issue)

        resp = client.get(
            f"/auth/google/callback?state={state}&code=c0de",
            cookies={auth.NONCE_COOKIE: nonce},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == f"{FRONT}/welcome"
        assert seen["exchange"] == ("c0de", f"{API}/auth/google/callback", "cid")
        assert seen["upsert"] == ("sub-1", "p@example.com", "P")
        assert seen["issue"] == "user-uuid"
        cookie = cookie_header(resp, COOKIE)
        assert "opaque-value" in cookie
        for attr in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
            assert attr in cookie, attr
        assert state_store["consumed"] is True

    def test_a_nonce_mismatch_in_the_token_is_refused(
        self, client, configured, counter, state_store, monkeypatch
    ):
        state, nonce = self._signin(client)

        async def exchange_code(client_, **kw):
            return unsigned_id_token(state, nonce="somebody-elses")

        monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
        resp = client.get(
            f"/auth/google/callback?state={state}&code=c",
            cookies={auth.NONCE_COOKIE: nonce},
            follow_redirects=False,
        )
        assert resp.headers["location"] == f"{FRONT}/auth/error?reason=exchange_failed"

    def test_an_email_held_by_another_account_is_never_merged(
        self, client, configured, counter, state_store, monkeypatch
    ):
        state, nonce = self._signin(client)

        async def exchange_code(client_, **kw):
            return unsigned_id_token(state)

        async def upsert(conn, **kw):
            raise identity.IdentityCollision("held elsewhere")

        monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
        monkeypatch.setattr(identity, "upsert_google_identity", upsert)
        resp = client.get(
            f"/auth/google/callback?state={state}&code=c",
            cookies={auth.NONCE_COOKIE: nonce},
            follow_redirects=False,
        )
        assert (
            resp.headers["location"] == f"{FRONT}/auth/error?reason=identity_collision"
        )


class TestSignout:
    def test_revokes_the_presented_session_and_clears_the_cookie(
        self, client, monkeypatch
    ):
        revoked = []

        async def revoke(conn, *, token_hash):
            revoked.append(token_hash)
            return True

        monkeypatch.setattr(sessions, "revoke", revoke)
        resp = client.post("/auth/signout", headers={"Authorization": "Bearer opaque"})
        assert resp.status_code == 200
        assert revoked == [hashlib.sha256(b"opaque").hexdigest()]
        cookie = cookie_header(resp, COOKIE)
        assert "Max-Age=0" in cookie or "expires=" in cookie.lower()

    def test_without_a_session_it_still_answers_and_touches_nothing(
        self, client, monkeypatch
    ):
        async def revoke(conn, *, token_hash):
            raise AssertionError("nothing to revoke")

        monkeypatch.setattr(sessions, "revoke", revoke)
        assert client.post("/auth/signout").status_code == 200


class TestSessionDelivery:
    """#1117 — sign-in refuses rather than minting a session the front end can
    never read.

    Production authenticated correctly and bounced the person back to `/login`,
    because the cookie was host-only on the API host while the front end sat on
    a different registrable domain. Every part worked; the composition did not.
    """

    def test_a_host_only_cookie_with_a_separate_front_end_refuses(
        self, client, configured, monkeypatch
    ):
        """The #1117 shape exactly: everything else configured, cookie host-only."""
        monkeypatch.setattr(settings, "SESSION_COOKIE_DOMAIN", None, raising=False)
        resp = client.get("/auth/google", follow_redirects=False)
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "SESSION_COOKIE_DOMAIN" in detail
        assert "api.example.test" in detail and "app.example.test" in detail

    def test_a_cookie_domain_covering_neither_host_refuses(
        self, client, configured, monkeypatch
    ):
        """Production's real shape — the API on one registrable domain, the front
        end on another, so no cookie scope can span them."""
        monkeypatch.setattr(
            settings, "SESSION_COOKIE_DOMAIN", "elsewhere.test", raising=False
        )
        resp = client.get("/auth/google", follow_redirects=False)
        assert resp.status_code == 503
        assert "does not cover both" in resp.json()["detail"]

    def test_a_covering_cookie_domain_is_let_through(
        self, client, configured, counter, state_store
    ):
        """The positive control. Without it the two refusals above are also
        satisfied by a gate that refuses everything."""
        resp = client.get("/auth/google", follow_redirects=False)
        assert resp.status_code == 302
        assert urlsplit(resp.headers["location"]).netloc == "accounts.google.com"

    def test_no_front_end_configured_is_not_a_gap(
        self, client, configured, counter, state_store, monkeypatch
    ):
        """`WEB_APP_URL` unset is the documented fail-closed reading, not a
        misconfiguration this gate owns. It is also what production runs today,
        and this gate must not be the thing that takes sign-in down.
        """
        monkeypatch.setattr(settings, "WEB_APP_URL", None, raising=False)
        monkeypatch.setattr(settings, "SESSION_COOKIE_DOMAIN", None, raising=False)
        resp = client.get("/auth/google", follow_redirects=False)
        assert resp.status_code == 302

    def test_a_same_origin_deployment_needs_no_cookie_domain(
        self, client, configured, counter, state_store, monkeypatch
    ):
        """Host-only is correct when the API and the front end are one host."""
        monkeypatch.setattr(settings, "WEB_APP_URL", API, raising=False)
        monkeypatch.setattr(settings, "SESSION_COOKIE_DOMAIN", None, raising=False)
        resp = client.get("/auth/google", follow_redirects=False)
        assert resp.status_code == 302

    def test_the_gate_does_not_consult_the_public_suffix_list(self, monkeypatch):
        """The stated bound, pinned so it is not mistaken for coverage.

        `up.railway.app` IS a public suffix, so a cookie scoped to it is rejected
        by every browser — measured in Chromium against the real host. This gate
        compares host suffixes only and PASSES that configuration. If a later
        change makes it PSL-aware, this test should fail and be deleted; until
        then it records what the gate does not check.
        """
        monkeypatch.setattr(
            settings,
            "OAUTH_REDIRECT_BASE_URL",
            "https://a.up.railway.app",
            raising=False,
        )
        monkeypatch.setattr(
            settings, "WEB_APP_URL", "https://b.up.railway.app", raising=False
        )
        monkeypatch.setattr(
            settings, "SESSION_COOKIE_DOMAIN", "up.railway.app", raising=False
        )
        assert session_delivery_gap() is None

    def test_a_leading_dot_on_the_cookie_domain_is_accepted(self, monkeypatch):
        """`.example.test` and `example.test` are the same scope (RFC 6265 §5.2.3
        strips the dot); the gate must not read the legacy spelling as a gap."""
        monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE_URL", API, raising=False)
        monkeypatch.setattr(settings, "WEB_APP_URL", FRONT, raising=False)
        monkeypatch.setattr(
            settings, "SESSION_COOKIE_DOMAIN", "." + COOKIE_DOMAIN, raising=False
        )
        assert session_delivery_gap() is None


WS = "33333333-3333-3333-3333-333333333333"
USER = "22222222-2222-2222-2222-222222222222"
ACCOUNT = "55555555-5555-4555-8555-555555555555"


class TestInstagramCallback:
    """`GET /auth/instagram-login/callback` — the return half of the destination
    connect (#1220 step 2). The Drive callback's shape: the state row is the
    only thing trusted, the provider call sits between two transactions, and
    every failure lands on the error page with `flow=instagram`.

    Everything behind a seam: the state consume, the exchange, the unit of
    work (a fake session — the conftest engine refuses SQL), the admin
    re-check `07` §2 asks for at the callback, and the three writes.
    """

    @pytest.fixture
    def instagram(self, configured, counter, monkeypatch):
        monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "app-1", raising=False)
        monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)

    @pytest.fixture
    def state_row(self, monkeypatch):
        row = {
            "state": "st-ig",
            "purpose": "connect",
            "provider": "ig_login",
            "user_id": USER,
            "workspace_id": WS,
            "reconnect_target": ACCOUNT,
        }
        seen = {}

        async def consume_state(
            conn, *, state, expected_provider=None, expected_purpose=None, **kw
        ):
            seen.update(
                state=state, provider=expected_provider, purpose=expected_purpose
            )
            if state != "st-ig":
                raise OAuthStateRefused("no")
            return dict(row)

        monkeypatch.setattr(auth, "consume_state", consume_state)
        row["seen"] = seen
        return row

    @pytest.fixture
    def writes(self, monkeypatch):
        """The unit of work and the three writes, recorded in order."""
        log = []

        class _Session:
            pass

        class _Uow:
            def __init__(self, engine, tenant_id, **kw):
                log.append(
                    ("uow", tenant_id, kw.get("actor_user_id"), kw.get("channel"))
                )

            def begin(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def _cm():
                    yield _Session()

                return _cm()

        async def authorize_member(
            session, workspace_id, user_id, minimum_role="member"
        ):
            log.append(("gate", workspace_id, user_id, minimum_role))
            return "owner"

        async def attach(
            session, *, workspace_id, ig_account_id, provider_account_ref, handle
        ):
            log.append(
                ("attach", workspace_id, ig_account_id, provider_account_ref, handle)
            )

        async def store_credential(
            session, *, workspace_id, ig_account_id, token, expires_at=None
        ):
            log.append(("store", workspace_id, ig_account_id, token))
            return "cred-new"

        log_state = {}
        monkeypatch.setattr(auth, "unit_of_work", _Uow)
        monkeypatch.setattr(tenant_resolution, "authorize_member", authorize_member)
        monkeypatch.setattr(provisioning, "attach_connected_identity", attach)
        monkeypatch.setattr(ig_login_oauth, "store_credential", store_credential)
        log_state["log"] = log
        return log_state

    @pytest.fixture
    def grant(self, monkeypatch):
        async def exchange_code(
            client_, *, code, redirect_uri, client_id, client_secret
        ):
            return ig_login_oauth.IgGrant(
                access_token="IGQVJ-long",
                expires_at=None,
                ig_user_id="17841400000000001",
                username="gatortails",
            )

        monkeypatch.setattr(ig_login_oauth, "exchange_code", exchange_code)

    @pytest.mark.parametrize(
        "query, reason",
        [
            ("error=access_denied", "denied"),
            ("state=st-ig", "missing_params"),
            ("code=c", "missing_params"),
        ],
    )
    def test_denied_or_incomplete_lands_on_the_error_page_for_this_flow(
        self, client, instagram, query, reason
    ):
        resp = client.get(
            f"/auth/instagram-login/callback?{query}", follow_redirects=False
        )
        assert resp.status_code == 302
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason={reason}&flow=instagram"
        )

    def test_a_state_minted_for_another_leg_is_refused_by_name(
        self, client, instagram, state_row
    ):
        resp = client.get(
            "/auth/instagram-login/callback?state=st-other&code=c",
            follow_redirects=False,
        )
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=instagram"
        )
        assert state_row["seen"]["provider"] == "ig_login"
        assert set(state_row["seen"]["purpose"]) == {"connect", "reconnect"}

    def test_a_state_naming_no_account_cannot_act(
        self, client, instagram, state_row, writes
    ):
        state_row["reconnect_target"] = None
        resp = client.get(
            "/auth/instagram-login/callback?state=st-ig&code=c", follow_redirects=False
        )
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=instagram"
        )
        assert writes["log"] == []

    def test_a_refused_exchange_lands_on_the_error_page(
        self, client, instagram, state_row, writes, monkeypatch
    ):
        async def exchange_code(client_, **kw):
            raise ig_login_oauth.IgOAuthRefused("long_lived_failed")

        monkeypatch.setattr(ig_login_oauth, "exchange_code", exchange_code)
        resp = client.get(
            "/auth/instagram-login/callback?state=st-ig&code=c", follow_redirects=False
        )
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=exchange_failed&flow=instagram"
        )
        assert writes["log"] == []

    def test_connect_attaches_the_real_identity_stores_the_credential_and_returns_to_settings(
        self, client, instagram, state_row, writes, grant
    ):
        resp = client.get(
            "/auth/instagram-login/callback?state=st-ig&code=c0de",
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.text
        assert (
            resp.headers["location"]
            == f"{FRONT}/dashboard/settings?connected=instagram"
        )
        assert writes["log"] == [
            ("uow", WS, USER, "web"),
            ("gate", WS, USER, "admin"),
            ("attach", WS, ACCOUNT, "17841400000000001", "gatortails"),
            ("store", WS, ACCOUNT, "IGQVJ-long"),
        ]

    def test_reconnect_takes_the_same_single_write_as_connect(
        self, client, instagram, state_row, writes, grant
    ):
        """The upsert replaces the payload in place (`07` §2); the route has
        no branch to get wrong, which is the point of it being one write."""
        state_row["purpose"] = "reconnect"
        client.get(
            "/auth/instagram-login/callback?state=st-ig&code=c0de",
            follow_redirects=False,
        )
        assert ("store", WS, ACCOUNT, "IGQVJ-long") in writes["log"]

    def test_a_user_no_longer_admin_at_callback_time_is_refused(
        self, client, instagram, state_row, writes, grant, monkeypatch
    ):
        from src.exceptions.tenancy import TenantResolutionError

        async def authorize_member(
            session, workspace_id, user_id, minimum_role="member"
        ):
            raise TenantResolutionError("insufficient_role")

        monkeypatch.setattr(tenant_resolution, "authorize_member", authorize_member)
        resp = client.get(
            "/auth/instagram-login/callback?state=st-ig&code=c0de",
            follow_redirects=False,
        )
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=instagram"
        )
        assert not any(w[0] in ("attach", "store") for w in writes["log"])

    def test_an_account_already_connected_elsewhere_in_the_workspace_is_named(
        self, client, instagram, state_row, writes, grant, monkeypatch
    ):
        async def attach(session, **kw):
            raise provisioning.ProvisioningRefused("duplicate_destination")

        monkeypatch.setattr(provisioning, "attach_connected_identity", attach)
        resp = client.get(
            "/auth/instagram-login/callback?state=st-ig&code=c0de",
            follow_redirects=False,
        )
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=already_connected&flow=instagram"
        )
