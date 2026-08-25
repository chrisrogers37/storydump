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

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from src.api.principal import COOKIE
from src.api.routes import auth
from src.config.settings import settings
from src.services.target import google_oidc, identity, rate_counters, sessions
from src.services.target.ig_login_oauth import OAuthStateRefused

API = "https://api.example.test"
FRONT = "https://app.example.test"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec", raising=False)
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE_URL", API, raising=False)
    monkeypatch.setattr(settings, "WEB_APP_URL", FRONT, raising=False)


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


def _id_token(state, **over):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "cid",
        "exp": now + 300,
        "iat": now,
        "nonce": google_oidc.nonce_for(state),
        "sub": "sub-1",
        "email": "p@example.com",
        "email_verified": True,
        "name": "P",
    }
    claims.update(over)
    seg = lambda o: (
        base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    )  # noqa: E731
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


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
        cookie = next(
            c
            for c in resp.headers.get_list("set-cookie")
            if c.startswith(auth.NONCE_COOKIE)
        )
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
        nonce = (
            next(
                c
                for c in resp.headers.get_list("set-cookie")
                if c.startswith(auth.NONCE_COOKIE)
            )
            .split(";")[0]
            .split("=", 1)[1]
        )
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
            return _id_token(state)

        async def upsert(conn, *, sub, email, email_verified, display_name):
            seen["upsert"] = (sub, email, email_verified, display_name)
            return "user-uuid"

        async def issue(conn, *, user_id, ttl_seconds=None):
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
        assert seen["upsert"] == ("sub-1", "p@example.com", True, "P")
        assert seen["issue"] == "user-uuid"
        cookie = next(
            c for c in resp.headers.get_list("set-cookie") if c.startswith(COOKIE)
        )
        assert "opaque-value" in cookie
        for attr in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
            assert attr in cookie, attr
        assert state_store["consumed"] is True

    def test_a_nonce_mismatch_in_the_token_is_refused(
        self, client, configured, counter, state_store, monkeypatch
    ):
        state, nonce = self._signin(client)

        async def exchange_code(client_, **kw):
            return _id_token(state, nonce="somebody-elses")

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
            return _id_token(state)

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
        cookie = next(
            c for c in resp.headers.get_list("set-cookie") if c.startswith(COOKIE)
        )
        assert "Max-Age=0" in cookie or "expires=" in cookie.lower()

    def test_without_a_session_it_still_answers_and_touches_nothing(
        self, client, monkeypatch
    ):
        async def revoke(conn, *, token_hash):
            raise AssertionError("nothing to revoke")

        monkeypatch.setattr(sessions, "revoke", revoke)
        assert client.post("/auth/signout").status_code == 200
