"""`/auth` — sign-in hosted on the API, with the provider and the database
behind seams.

The properties that matter: sign-in refuses (503) rather than half-works when
unconfigured; the redirect carries a state the database issued and a nonce
derived from it, and the CSRF cookie the callback will demand; the pre-auth
counter keys on the ATTRIBUTED client; every callback failure lands on
the front end's error page with a closed reason; success sets the session
cookie with `HttpOnly`, `Secure`, `SameSite=lax` and `Path=/`; sign-out
revokes.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.api.principal import (
    COOKIE,
    PREAUTH_LIMIT,
    PREAUTH_SCOPE,
    session_delivery_gap,
)
from src.api.routes import auth
from src.config.settings import settings
from src.services.target import (
    commands,
    google_drive_oauth,
    google_oidc,
    identity,
    ig_login_oauth,
    media_sync,
    provisioning,
    rate_counters,
    sessions,
    signup_admissions,
    tenant_resolution,
)
from src.services.target.oauth_states import STATE_TTL_SECONDS, OAuthStateRefused, ring
from tests.src.services.target.conftest import APPENDIX_B_VERIFIER, drive_grant
from tests.src.api.conftest import (
    API,
    COOKIE_DOMAIN,
    FRONT,
    FakeSession,
    cookie_header,
    cookie_value,
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
        assert scope == PREAUTH_SCOPE and limit == PREAUTH_LIMIT
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


@pytest.fixture
def browser(client, monkeypatch):
    """The returning browser carries the session cookie of `holder` (the
    connect legs' callbacks require it to be the state's user)."""
    holder = {"user_id": USER}

    async def resolve(conn, *, token_hash):
        if holder["user_id"] is None:
            from src.exceptions.tenancy import TenantResolutionError

            raise TenantResolutionError("invalid_session", "no such session")
        from types import SimpleNamespace

        return SimpleNamespace(id="sess-1", user_id=holder["user_id"])

    monkeypatch.setattr(sessions, "resolve", resolve)
    client.cookies.set(COOKIE, "opaque-session-value")
    return holder


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
        by writing X-Forwarded-For: the key is the raw peer."""
        client = TestClient(app, client=("203.0.113.9", 4321))
        client.get(
            "/auth/google",
            headers={"X-Forwarded-For": "198.51.100.7"},
            follow_redirects=False,
        )
        assert counter["keys"] == ["203.0.113.9"]

    def test_behind_railways_edge_the_counter_keys_on_each_visitors_64(
        self, app, configured, counter, state_store
    ):
        """Behind Railway's edge (100.64.0.0/10) the key is the visitor, and
        an IPv6 visitor's whole /64 is one key, so walking the addresses of
        one subscriber's /64 is not a fresh limit per request."""
        client = TestClient(app, client=("100.64.0.13", 4321))
        for xff in ("192.0.2.50", "2001:db8:1:2::1", "2001:db8:1:2::ffff"):
            client.get(
                "/auth/google", headers={"X-Forwarded-For": xff}, follow_redirects=False
            )
        assert counter["keys"] == [
            "192.0.2.50",
            "2001:db8:1:2::/64",
            "2001:db8:1:2::/64",
        ]


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

    def test_only_rfc_6749s_error_codes_are_logged_by_name(self):
        """RFC 6749 §4.1.2.1's closed list: any other `error` value is the
        caller's own text, and is logged as `other`."""
        assert auth.OAUTH_ERRORS == {
            "access_denied",
            "invalid_request",
            "invalid_scope",
            "server_error",
            "temporarily_unavailable",
            "unauthorized_client",
            "unsupported_response_type",
        }

    @pytest.mark.parametrize(
        "query, says",
        [
            (
                "error=access_denied&state=fake-state-0000",
                "google sign-in: the provider returned error=access_denied;"
                " nothing was consumed",
            ),
            (
                "error=fake-error-0000&state=fake-state-0000",
                "google sign-in: the provider returned error=other;"
                " nothing was consumed",
            ),
            (
                "state=fake-state-0000",
                "google sign-in: the callback carried no state or no code",
            ),
            (
                "code=fake-code-0000",
                "google sign-in: the callback carried no state or no code",
            ),
        ],
    )
    def test_a_callback_that_consumes_nothing_says_so_without_the_query(
        self, client, configured, counter, app_lines, query, says
    ):
        """The access log prints no query string (`src/utils/logger.py`), so
        these exits name themselves; what the provider sent stays out."""
        resp = client.get(f"/auth/google/callback?{query}", follow_redirects=False)
        assert resp.status_code == 302
        assert says in app_lines, app_lines
        assert [line for line in app_lines if "fake-" in line] == [], app_lines

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

        async def upsert(conn, *, sub, email, display_name, signup_open):
            seen["upsert"] = (sub, email, display_name)
            seen["signup_open"] = signup_open
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
        assert seen["signup_open"] is False, "sign-up is gated by default (092)"
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

    def _refused_signup(self, client, monkeypatch):
        state, nonce = self._signin(client)
        seen = {}

        async def exchange_code(client_, **kw):
            return unsigned_id_token(state)

        async def upsert(conn, **kw):
            seen["signup_open"] = kw["signup_open"]
            raise identity.SignupNotAdmitted("not admitted")

        async def issue(conn, *, user_id):
            raise AssertionError("a refused sign-up mints no session")

        monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
        monkeypatch.setattr(identity, "upsert_google_identity", upsert)
        monkeypatch.setattr(sessions, "issue", issue)
        resp = client.get(
            f"/auth/google/callback?state={state}&code=c",
            cookies={auth.NONCE_COOKIE: nonce},
            follow_redirects=False,
        )
        return resp, seen

    def test_a_new_account_nobody_admitted_lands_on_the_sign_in_page(
        self, client, configured, counter, state_store, monkeypatch
    ):
        """`fn_signup_admitted`: the refusal is named, lands where the waitlist is, and sets no
        session cookie."""
        resp, _ = self._refused_signup(client, monkeypatch)
        assert resp.status_code == 302
        assert resp.headers["location"] == f"{FRONT}/login?error=not_admitted"
        assert COOKIE not in resp.headers.get("set-cookie", "")

    def test_without_a_front_end_the_refusal_is_a_named_400(
        self, client, configured, counter, state_store, monkeypatch
    ):
        monkeypatch.setattr(settings, "WEB_APP_URL", None, raising=False)
        monkeypatch.setattr(settings, "SESSION_COOKIE_DOMAIN", None, raising=False)
        resp, _ = self._refused_signup(client, monkeypatch)
        assert resp.status_code == 400
        assert resp.json() == {"detail": "not_admitted"}

    @pytest.mark.parametrize("value", [False, True])
    def test_the_open_switch_reaches_the_upsert(
        self, client, configured, counter, state_store, monkeypatch, value
    ):
        monkeypatch.setattr(settings, "TARGET_SIGNUP_OPEN", value, raising=False)
        _, seen = self._refused_signup(client, monkeypatch)
        assert seen["signup_open"] is value


#: A claim link's token, the length the worker draws (`sessions.new_token`).
CLAIM = "Cl41m-t0ken_" + "x" * 31


def _claim_start(client, token=CLAIM):
    """The claim page's form post: ``claim=<token>``."""
    return client.post(
        "/auth/google/claim", data={"claim": token}, follow_redirects=False
    )


def _assert_claim_cookie(resp, value: str, *, max_age: int) -> None:
    """`sd_claim` set to *value* for *max_age* seconds, with its attributes: a
    deletion (``'""'`` for 0 seconds) carries the same ones as the set."""
    cookie = cookie_header(resp, auth.CLAIM_COOKIE)
    assert cookie.split(";")[0] == f"{auth.CLAIM_COOKIE}={value}"
    for attr in (
        "HttpOnly",
        "Secure",
        "SameSite=lax",
        "Path=/auth/google",
        f"Max-Age={max_age}",
    ):
        assert attr in cookie, attr


class TestTheClaimStart:
    """`POST /auth/google/claim` (`signup_admissions`): the claim page's form posts the
    link's token. The API starts the sign-in as `GET /auth/google` does and
    keeps the token in `sd_claim` for the callback. It reads nothing about the
    claim, so this route has no claim seam to patch."""

    def test_it_starts_like_the_get_and_keeps_the_token_in_a_cookie(
        self, client, configured, counter, state_store
    ):
        resp = _claim_start(client)
        assert resp.status_code == 303
        loc = urlsplit(resp.headers["location"])
        assert loc.netloc == "accounts.google.com"
        q = parse_qs(loc.query)
        assert q["state"] == ["st-1"]
        assert q["nonce"] == [google_oidc.nonce_for("st-1")]
        assert q["redirect_uri"] == [f"{API}/auth/google/callback"]
        assert CLAIM not in resp.headers["location"], "the token rides no URL"
        nonce = cookie_header(resp, auth.NONCE_COOKIE)
        assert "HttpOnly" in nonce and "Path=/auth/google" in nonce
        assert state_store["nonce"] and state_store["nonce"] in nonce
        assert len(counter["keys"]) == 1, "the pre-auth counter is spent once"
        _assert_claim_cookie(resp, CLAIM, max_age=STATE_TTL_SECONDS)

    def test_the_cookie_is_secure_as_the_session_cookie_is(
        self, client, configured, counter, state_store, monkeypatch
    ):
        monkeypatch.setattr(settings, "SESSION_COOKIE_SECURE", False, raising=False)
        assert "Secure" not in cookie_header(_claim_start(client), auth.CLAIM_COOKIE)

    def test_the_longest_token_is_kept(self, client, configured, counter, state_store):
        token = "x" * 256
        resp = _claim_start(client, token)
        assert resp.status_code == 303
        assert cookie_value(resp, auth.CLAIM_COOKIE) == token

    @pytest.mark.parametrize(
        "token", [None, "", "x" * 257, "has space", "slash/ed", "pad=ded"]
    )
    def test_a_token_of_no_claims_shape_is_refused_before_any_state(
        self, client, configured, counter, state_store, token
    ):
        resp = client.post(
            "/auth/google/claim",
            data={} if token is None else {"claim": token},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == f"{FRONT}/login?error=claim_refused"
        assert resp.headers.get_list("set-cookie") == []
        assert "state" not in state_store and counter["keys"] == []

    @pytest.mark.parametrize(
        "data",
        [
            {f"f{i}": "x" for i in range(auth.CLAIM_MAX_FIELDS + 1)},
            {"claim": "x" * (auth.CLAIM_MAX_PART_BYTES + 1)},
        ],
        ids=["too many fields", "a part too large"],
    )
    def test_the_form_is_bounded_before_anything_else(
        self, client, configured, counter, state_store, data
    ):
        """An unauthenticated body is parsed before any gate, so the parse is
        bounded: past the bound the answer is the parser's 400, with no state,
        no debit and no cookie."""
        resp = client.post("/auth/google/claim", data=data, follow_redirects=False)
        assert resp.status_code == 400
        assert resp.headers.get_list("set-cookie") == []
        assert "state" not in state_store and counter["keys"] == []

    def test_a_body_that_is_not_the_form_is_refused_the_same_way(
        self, client, configured, counter, state_store
    ):
        """The route reads its own body (`test_security_hardening.py`): a
        JSON body carries no form field, so it is no claim."""
        resp = client.post(
            "/auth/google/claim", json={"claim": CLAIM}, follow_redirects=False
        )
        assert resp.headers["location"] == f"{FRONT}/login?error=claim_refused"
        assert "state" not in state_store and counter["keys"] == []

    def test_a_file_is_never_a_claim(self, client, configured, counter, state_store):
        resp = client.post(
            "/auth/google/claim",
            files={"claim": ("claim.txt", CLAIM.encode())},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert resp.headers.get_list("set-cookie") == []
        assert "state" not in state_store and counter["keys"] == []

    def test_without_a_front_end_the_refusal_is_a_named_400(
        self, client, configured, counter, state_store, monkeypatch
    ):
        monkeypatch.setattr(settings, "WEB_APP_URL", None, raising=False)
        resp = _claim_start(client, "has space")
        assert resp.status_code == 400
        assert resp.json() == {"detail": "claim_refused"}


class TestTheClaimAtTheCallback:
    """The callback with a claim (`signup_admissions`). A claim is asked only
    when the upsert refuses a new account with a verified email, inside one
    savepoint with the second upsert, which skips the gate. Everything is
    behind a seam: the upsert, `signup_admissions.claim`, the session mint,
    and the savepoint, whose fake records whether it was released or rolled
    back. One log records them all, in order."""

    @pytest.fixture
    def world(self, configured, counter, state_store, engine, monkeypatch):
        log = []
        seams = {"log": log, "upsert": "new", "claim": True}

        class _Session(FakeSession):
            @asynccontextmanager
            async def begin_nested(self):
                log.append("savepoint")
                try:
                    yield self
                except BaseException:
                    log.append("rolled back")
                    raise
                log.append("released")

        engine.session = _Session()

        async def upsert(conn, *, sub, email, display_name, signup_open):
            log.append(("upsert", email, signup_open))
            kind = seams["upsert"]
            if kind == "admitted" or (kind == "new" and signup_open):
                return "user-uuid"
            if kind == "held" or (kind == "collision" and signup_open):
                raise identity.IdentityCollision("held elsewhere")
            raise identity.SignupNotAdmitted("not admitted")

        async def claim(conn, *, token):
            log.append(("claim", token))
            return seams["claim"]

        async def issue(conn, *, user_id):
            log.append(("issue", user_id))
            return "opaque-value"

        monkeypatch.setattr(identity, "upsert_google_identity", upsert)
        monkeypatch.setattr(signup_admissions, "claim", claim)
        monkeypatch.setattr(sessions, "issue", issue)
        return seams

    def _callback(self, client, monkeypatch, *, claim=CLAIM, **claims):
        """Start (the claim page's POST, or the plain GET without a claim),
        then return to the callback with what the start set, Google stubbed
        to answer an ID token with *claims*."""
        start = (
            client.get("/auth/google", follow_redirects=False)
            if claim is None
            else _claim_start(client, claim)
        )
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        cookies = {auth.NONCE_COOKIE: cookie_value(start, auth.NONCE_COOKIE)}
        if claim is not None:
            cookies[auth.CLAIM_COOKIE] = cookie_value(start, auth.CLAIM_COOKIE)
        client.cookies.clear()

        async def exchange_code(client_, **kw):
            return unsigned_id_token(state, **claims)

        monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
        return client.get(
            f"/auth/google/callback?state={state}&code=c0de",
            cookies=cookies,
            follow_redirects=False,
        )

    def test_an_admitted_address_signs_up_without_calling_the_claim_fallback(
        self, client, world, monkeypatch
    ):
        """The gate admits the address, so the callback never asks the claim:
        the order of the calls is all this pins. Whether the claim's link
        survives is the database's to answer, and a mock cannot: an account
        created at its admission's own address uses that admission up, link
        and all (`trg_signup_admission_consume`), while an unrelated claim is left unspent. The gates
        hold both (`tests/scripts/test_signup_gate.py`)."""
        world["upsert"] = "admitted"
        resp = self._callback(client, monkeypatch)
        assert resp.headers["location"] == f"{FRONT}/welcome"
        assert world["log"] == [
            ("upsert", "p@example.com", False),
            ("issue", "user-uuid"),
        ]

    def test_an_unadmitted_address_spends_the_claim_in_a_savepoint_and_signs_up(
        self, client, world, monkeypatch
    ):
        resp = self._callback(client, monkeypatch)
        assert resp.headers["location"] == f"{FRONT}/welcome"
        assert world["log"] == [
            ("upsert", "p@example.com", False),
            "savepoint",
            ("claim", CLAIM),
            ("upsert", "p@example.com", True),
            "released",
            ("issue", "user-uuid"),
        ]
        assert "opaque-value" in cookie_header(resp, COOKIE)

    def test_a_refused_claim_lands_on_claim_refused_with_no_session(
        self, client, world, monkeypatch
    ):
        world["claim"] = False
        resp = self._callback(client, monkeypatch)
        assert resp.headers["location"] == f"{FRONT}/login?error=claim_refused"
        assert world["log"] == [
            ("upsert", "p@example.com", False),
            "savepoint",
            ("claim", CLAIM),
            "released",
        ]
        assert COOKIE not in resp.headers.get("set-cookie", "")

    def test_without_a_claim_a_new_account_is_not_admitted(
        self, client, world, monkeypatch
    ):
        resp = self._callback(client, monkeypatch, claim=None)
        assert resp.headers["location"] == f"{FRONT}/login?error=not_admitted"
        assert world["log"] == [("upsert", "p@example.com", False)]

    def test_with_no_verified_email_the_claim_is_never_asked(
        self, client, world, monkeypatch
    ):
        resp = self._callback(client, monkeypatch, email_verified=False)
        assert resp.headers["location"] == f"{FRONT}/login?error=not_admitted"
        assert world["log"] == [("upsert", None, False)]

    def test_a_collision_after_the_claim_rolls_the_savepoint_back(
        self, client, world, monkeypatch
    ):
        world["upsert"] = "collision"
        resp = self._callback(client, monkeypatch)
        assert (
            resp.headers["location"] == f"{FRONT}/auth/error?reason=identity_collision"
        )
        assert world["log"] == [
            ("upsert", "p@example.com", False),
            "savepoint",
            ("claim", CLAIM),
            ("upsert", "p@example.com", True),
            "rolled back",
        ]

    @pytest.mark.parametrize(
        "outcome, landing",
        [
            ("denied", "/auth/error?reason=denied"),
            ("missing_params", "/auth/error?reason=missing_params"),
            ("state_refused", "/auth/error?reason=state_refused"),
            ("exchange_failed", "/auth/error?reason=exchange_failed"),
            ("held", "/auth/error?reason=identity_collision"),
            ("no_email", "/login?error=not_admitted"),
            ("claim_refused", "/login?error=claim_refused"),
            ("collision", "/auth/error?reason=identity_collision"),
            ("signed_up", "/welcome"),
        ],
    )
    def test_every_answer_deletes_the_claim_cookie(
        self, client, world, monkeypatch, outcome, landing
    ):
        early = {
            "denied": "error=access_denied",
            "missing_params": "state=st-1",
            "state_refused": "state=not-the-state&code=c",
        }
        if outcome in early:
            resp = client.get(
                f"/auth/google/callback?{early[outcome]}",
                cookies={auth.CLAIM_COOKIE: CLAIM},
                follow_redirects=False,
            )
        else:
            claims = {}
            if outcome == "exchange_failed":
                claims["nonce"] = "somebody-elses"
            if outcome == "no_email":
                claims["email_verified"] = False
            if outcome == "claim_refused":
                world["claim"] = False
            if outcome in ("held", "collision"):
                world["upsert"] = outcome
            resp = self._callback(client, monkeypatch, **claims)
        assert resp.headers["location"] == f"{FRONT}{landing}"
        _assert_claim_cookie(resp, '""', max_age=0)


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

    def test_everywhere_revokes_every_session_of_the_presenting_user(
        self, client, monkeypatch
    ):
        """`?everywhere=true` goes to `revoke_all_for_user` with the presented
        hash, never to the one-row `revoke`; the cookie is cleared either way."""
        seen = []

        async def revoke(conn, *, token_hash):
            raise AssertionError("everywhere must not revoke only this session")

        async def revoke_all_for_user(conn, *, token_hash):
            seen.append(token_hash)
            return 3

        monkeypatch.setattr(sessions, "revoke", revoke)
        monkeypatch.setattr(sessions, "revoke_all_for_user", revoke_all_for_user)
        resp = client.post(
            "/auth/signout?everywhere=true",
            headers={"Authorization": "Bearer opaque"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"signed_out": True, "revoked": 3}
        assert seen == [hashlib.sha256(b"opaque").hexdigest()]
        cookie = cookie_header(resp, COOKIE)
        assert "Max-Age=0" in cookie or "expires=" in cookie.lower()

    def test_everywhere_without_a_session_touches_nothing(self, client, monkeypatch):
        async def revoke_all_for_user(conn, *, token_hash):
            raise AssertionError("nothing to revoke")

        monkeypatch.setattr(sessions, "revoke_all_for_user", revoke_all_for_user)
        resp = client.post("/auth/signout?everywhere=true")
        assert resp.status_code == 200
        assert resp.json() == {"signed_out": True, "revoked": 0}


class TestSessionDelivery:
    """— sign-in refuses rather than minting a session the front end can
    never read.

    With the session cookie host-only on the API host and the front end on a
    different registrable domain, a sign-in let through would authenticate
    correctly and bounce the person back to `/login`: every part works, and
    the composition does not.
    """

    def test_a_host_only_cookie_with_a_separate_front_end_refuses(
        self, client, configured, monkeypatch
    ):
        """The shape exactly: everything else configured, cookie host-only."""
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
        misconfiguration this gate owns. This gate must not be the thing that
        takes sign-in down.
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

        `up.railway.app` IS a public suffix, so every browser rejects a cookie
        scoped to it. This gate compares host suffixes only and PASSES that
        configuration: the test records what the gate does not check, and a
        PSL-aware gate fails it.
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

#: A callback's gate: (the purpose its state carries, the entry moved to owner,
#: the floor it checks). Only the carried purpose's own entry moves it.
CALLBACK_FLOOR_CASES = [
    ("connect", "connect_account", "owner"),
    ("reconnect", "reconnect_account", "owner"),
    ("connect", "reconnect_account", "admin"),
    ("reconnect", "connect_account", "admin"),
]


class TestInstagramCallback:
    """`GET /auth/instagram-login/callback` — the return half of the destination
    connect. The Drive callback's shape: the state row is
    trusted for what it pins, the provider call sits between two
    transactions, and every failure lands on the error page with
    `flow=instagram` — plus the two callback-time checks this leg adds:
    the returning browser's session must be the state's user, and that user
    must still be admin.

    Everything behind a seam: the state consume, the session resolve, the
    exchange, the unit of work (a fake session — the conftest engine refuses
    SQL), the admin re-check, and the two writes.
    """

    URL = "/auth/instagram-login/callback"

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
        """The unit of work and the two writes, recorded in order."""
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

        async def store_credential(
            session, *, workspace_id, ig_account_id, token, expires_at=None
        ):
            log.append(("store", workspace_id, ig_account_id, token))
            return "cred-new"

        seams = {"log": log, "connect_refusal": None}

        async def connect(
            session, *, workspace_id, provider_account_ref, handle, ig_account_id=None
        ):
            log.append(
                ("connect", workspace_id, ig_account_id, provider_account_ref, handle)
            )
            if seams["connect_refusal"] is not None:
                raise seams["connect_refusal"]
            return (ig_account_id or "acct-adopted", ig_account_id is None)

        monkeypatch.setattr(auth, "unit_of_work", _Uow)
        monkeypatch.setattr(tenant_resolution, "authorize_member", authorize_member)
        monkeypatch.setattr(provisioning, "connect_destination", connect)
        monkeypatch.setattr(ig_login_oauth, "store_credential", store_credential)
        return seams

    @pytest.fixture
    def grant(self, monkeypatch):
        async def exchange_code(
            client_, *, code, redirect_uri, client_id, client_secret
        ):
            return ig_login_oauth.IgGrant(
                access_token="IGQVJ-long",
                expires_at=None,
                ig_user_id="17841400000000001",
                username="example.brand",
            )

        monkeypatch.setattr(ig_login_oauth, "exchange_code", exchange_code)

    def _return(self, client, state="st-ig", code="c0de"):
        return client.get(
            f"{self.URL}?state={state}&code={code}", follow_redirects=False
        )

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
        resp = client.get(f"{self.URL}?{query}", follow_redirects=False)
        assert resp.status_code == 302
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason={reason}&flow=instagram"
        )

    def test_a_refused_return_says_so_without_the_query(
        self, client, instagram, app_lines
    ):
        """The access log prints no query string (`src/utils/logger.py`), so
        this exit names itself; what the provider sent stays out."""
        resp = client.get(
            f"{self.URL}?error=access_denied&state=fake-state-0000",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        says = (
            "instagram connect: the provider returned error=access_denied;"
            " nothing was consumed"
        )
        assert says in app_lines, app_lines
        assert [line for line in app_lines if "fake-" in line] == [], app_lines

    def test_a_state_minted_for_another_leg_is_refused_by_name(
        self, client, instagram, state_row, browser
    ):
        resp = self._return(client, state="st-other")
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=instagram"
        )
        assert state_row["seen"]["provider"] == "ig_login"
        assert set(state_row["seen"]["purpose"]) == {"connect", "reconnect"}

    def test_a_state_naming_no_account_adopts_or_creates_the_destination(
        self, client, instagram, state_row, browser, writes, grant
    ):
        """Destinations are added by connecting. An untargeted connect state
        lands the credential on the row the signed-in account already has
        here, or on a new one — one adopt-or-create seam, then the same single
        credential write."""
        state_row["reconnect_target"] = None
        resp = self._return(client)
        assert resp.status_code == 302, resp.text
        assert (
            resp.headers["location"]
            == f"{FRONT}/dashboard/settings?connected=instagram"
        )
        assert writes["log"] == [
            ("uow", WS, USER, "web"),
            ("gate", WS, USER, "admin"),
            ("connect", WS, None, "17841400000000001", "example.brand"),
            ("store", WS, "acct-adopted", "IGQVJ-long"),
        ]

    def test_a_closing_workspace_refuses_the_add_by_name(
        self, client, instagram, state_row, browser, writes, grant
    ):
        state_row["reconnect_target"] = None
        writes["connect_refusal"] = provisioning.ProvisioningRefused(
            "workspace_inactive", "offboarding"
        )
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=workspace_closing&flow=instagram"
        )
        assert not any(entry[0] == "store" for entry in writes["log"])

    def test_a_reason_that_is_not_a_grant_outcome_is_not_dressed_as_one(
        self, client, instagram, state_row, browser, writes, grant
    ):
        """`slot_not_seeded` is a programming error; "start again and it
        should work" would be a lie, so it is answered as an error."""
        writes["connect_refusal"] = provisioning.ProvisioningRefused(
            "slot_not_seeded", "the cursor could not be computed"
        )
        try:
            resp = self._return(client)
        except provisioning.ProvisioningRefused:
            return
        assert resp.status_code == 500
        assert "auth/error" not in resp.headers.get("location", "")

    def test_a_refused_adoption_lands_on_the_error_page_by_name(
        self, client, instagram, state_row, browser, writes, grant
    ):
        state_row["reconnect_target"] = None
        writes["connect_refusal"] = provisioning.ProvisioningRefused(
            "duplicate_destination", "twice"
        )
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=already_connected&flow=instagram"
        )
        assert not any(entry[0] == "store" for entry in writes["log"])

    def test_a_browser_without_a_session_is_refused_before_the_provider_is_called(
        self, client, instagram, state_row, writes, grant, monkeypatch
    ):
        """The state pins the user who STARTED the flow; a returning browser
        carrying no session could be anyone the authorization URL was handed
        to. Refused before the code is spent, and before any write."""
        called = []

        async def exchange_code(client_, **kw):
            called.append(True)

        monkeypatch.setattr(ig_login_oauth, "exchange_code", exchange_code)
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=instagram"
        )
        assert called == [] and writes["log"] == []

    def test_a_browser_signed_in_as_someone_else_is_refused(
        self, client, instagram, state_row, browser, writes, grant
    ):
        browser["user_id"] = "99999999-9999-4999-8999-999999999999"
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=instagram"
        )
        assert writes["log"] == []

    def test_a_session_that_does_not_resolve_is_refused_the_same_way(
        self, client, instagram, state_row, browser, writes, grant
    ):
        browser["user_id"] = None
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=instagram"
        )
        assert writes["log"] == []

    def test_a_refused_exchange_lands_on_the_error_page(
        self, client, instagram, state_row, browser, writes, monkeypatch
    ):
        async def exchange_code(client_, **kw):
            raise ig_login_oauth.IgOAuthRefused("long_lived_failed")

        monkeypatch.setattr(ig_login_oauth, "exchange_code", exchange_code)
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=exchange_failed&flow=instagram"
        )
        assert writes["log"] == []

    def test_connect_attaches_the_real_identity_stores_the_credential_and_returns_to_settings(
        self, client, instagram, state_row, browser, writes, grant
    ):
        resp = self._return(client)
        assert resp.status_code == 302, resp.text
        assert (
            resp.headers["location"]
            == f"{FRONT}/dashboard/settings?connected=instagram"
        )
        assert writes["log"] == [
            ("uow", WS, USER, "web"),
            ("gate", WS, USER, "admin"),
            ("connect", WS, ACCOUNT, "17841400000000001", "example.brand"),
            ("store", WS, ACCOUNT, "IGQVJ-long"),
        ]

    @pytest.mark.parametrize("carried,moved,floor", CALLBACK_FLOOR_CASES)
    def test_the_floor_is_the_carried_purposes(
        self,
        client,
        instagram,
        state_row,
        browser,
        writes,
        grant,
        monkeypatch,
        carried,
        moved,
        floor,
    ):
        state_row["purpose"] = carried
        monkeypatch.setitem(commands.ROLE_FLOOR, moved, "owner")
        self._return(client)
        assert [e for e in writes["log"] if e[0] == "gate"] == [
            ("gate", WS, USER, floor)
        ]

    def test_reconnect_takes_the_same_single_write_as_connect(
        self, client, instagram, state_row, browser, writes, grant
    ):
        """The upsert replaces the payload in place; the route has
        no branch to get wrong, which is the point of it being one write."""
        state_row["purpose"] = "reconnect"
        self._return(client)
        assert ("store", WS, ACCOUNT, "IGQVJ-long") in writes["log"]

    def test_a_user_no_longer_admin_at_callback_time_is_refused(
        self, client, instagram, state_row, browser, writes, grant, monkeypatch
    ):
        from src.exceptions.tenancy import TenantResolutionError

        async def authorize_member(
            session, workspace_id, user_id, minimum_role="member"
        ):
            raise TenantResolutionError("insufficient_role")

        monkeypatch.setattr(tenant_resolution, "authorize_member", authorize_member)
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=instagram"
        )
        assert not any(w[0] == "store" for w in writes["log"])

    @pytest.mark.parametrize(
        "refusal, reason",
        [
            ("duplicate_destination", "already_connected"),
            ("wrong_account", "wrong_account"),
            ("not_found", "destination_gone"),
        ],
    )
    def test_each_attach_refusal_has_its_own_remedy_on_the_error_page(
        self,
        client,
        instagram,
        state_row,
        browser,
        writes,
        grant,
        refusal,
        reason,
    ):
        writes["connect_refusal"] = provisioning.ProvisioningRefused(refusal)
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason={reason}&flow=instagram"
        )
        assert not any(w[0] == "store" for w in writes["log"])


class TestDriveCallback:
    """`GET /auth/google-drive/callback` — the return half of the WORKSPACE's
    Drive connect (workspace Drive grant). The state row is trusted for what it pins,
    which is the workspace itself; the returning browser must carry the
    state user's session and that user must still be admin (the Instagram
    leg's two checks); the provider call sits between the two transactions;
    the grant lands as the workspace's and every folder is re-armed beside
    it."""

    URL = "/auth/google-drive/callback"

    @pytest.fixture
    def drive_row(self, monkeypatch, ring_env):
        ring_env(key=Fernet.generate_key().decode())
        row = {
            "state": "st-drive",
            "purpose": "connect",
            "provider": "gdrive",
            "user_id": USER,
            "workspace_id": WS,
            "reconnect_target": WS,
            "encrypted_code_verifier": ring().encrypt(APPENDIX_B_VERIFIER),
        }

        async def consume_state(
            conn, *, state, expected_provider=None, expected_purpose=None, **kw
        ):
            if state != "st-drive":
                raise OAuthStateRefused("no")
            return dict(row)

        monkeypatch.setattr(auth, "consume_state", consume_state)
        return row

    @pytest.fixture
    def exchanged(self, monkeypatch):
        """The Drive exchange, stubbed; records the verifier each call sent."""
        verifiers = []

        async def exchange_code(
            client, *, code, redirect_uri, client_id, client_secret, code_verifier
        ):
            verifiers.append(code_verifier)
            return drive_grant()

        monkeypatch.setattr(google_drive_oauth, "exchange_code", exchange_code)
        return verifiers

    @pytest.fixture
    def writes(self, monkeypatch):
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

        async def store_credential(session, *, workspace_id, grant, granted_by):
            log.append(("store", workspace_id, grant.access_token, granted_by))
            return "cred-1"

        async def rearm(session, *, workspace_id, source_id=None):
            log.append(("rearm", workspace_id, source_id))
            return 2

        monkeypatch.setattr(auth, "unit_of_work", _Uow)
        monkeypatch.setattr(tenant_resolution, "authorize_member", authorize_member)
        monkeypatch.setattr(google_drive_oauth, "store_credential", store_credential)
        monkeypatch.setattr(media_sync, "rearm_after_connect", rearm)
        return log

    def test_the_grant_lands_on_the_workspace_and_every_folder_is_rearmed(
        self, client, configured, counter, drive_row, browser, exchanged, writes
    ):
        resp = client.get(
            self.URL,
            params={"state": "st-drive", "code": "c0de"},
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.text
        assert resp.headers["location"].endswith("/dashboard/settings?connected=gdrive")
        # PKCE (RFC 7636): the code is redeemed with the verifier
        # minted beside the state, read from the row the consume returned.
        assert exchanged == [APPENDIX_B_VERIFIER]
        assert writes == [
            ("uow", WS, USER, "web"),
            ("gate", WS, USER, "admin"),
            # `oauth_credentials.granted_by_user_id`: the state's user — the returning browser, checked — is
            # the granter, the one person who may browse this Drive.
            ("store", WS, "ya29.access", USER),
            ("rearm", WS, None),
        ]

    @pytest.mark.parametrize("carried,moved,floor", CALLBACK_FLOOR_CASES)
    def test_the_floor_is_the_carried_purposes(
        self,
        client,
        configured,
        counter,
        drive_row,
        browser,
        exchanged,
        writes,
        monkeypatch,
        carried,
        moved,
        floor,
    ):
        drive_row["purpose"] = carried
        monkeypatch.setitem(commands.ROLE_FLOOR, moved, "owner")
        self._return(client)
        assert [e for e in writes if e[0] == "gate"] == [("gate", WS, USER, floor)]

    @pytest.mark.parametrize(
        "field, value",
        [
            ("reconnect_target", ACCOUNT),
            ("reconnect_target", None),
            ("encrypted_code_verifier", None),
            ("encrypted_code_verifier", "not-a-fernet-token"),
        ],
        ids=["another-target", "no-target", "no-verifier", "undecryptable"],
    )
    def test_a_state_that_cannot_redeem_the_code_is_refused_before_the_exchange(
        self,
        client,
        configured,
        counter,
        drive_row,
        browser,
        exchanged,
        writes,
        field,
        value,
    ):
        """A state that pins anything but the workspace, carries no verifier
        (minted without one, or before the column) or carries one the ring
        cannot decrypt: refused as `state_refused` for this flow, the code
        never sent to Google, and nothing written."""
        drive_row[field] = value
        resp = client.get(
            self.URL,
            params={"state": "st-drive", "code": "c0de"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=drive"
        )
        assert exchanged == []
        assert writes == []

    def _return(self, client):
        return client.get(
            self.URL,
            params={"state": "st-drive", "code": "c0de"},
            follow_redirects=False,
        )

    def test_a_browser_without_a_session_is_refused_before_the_provider_is_called(
        self, client, configured, counter, drive_row, writes, monkeypatch
    ):
        """The callback requires the state user's session; a return without
        one is refused before the code is spent, and nothing is written."""
        called = []

        async def exchange_code(client_, **kw):
            called.append(True)

        monkeypatch.setattr(google_drive_oauth, "exchange_code", exchange_code)
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=drive"
        )
        assert called == [] and writes == []

    @pytest.mark.parametrize(
        "presented", ["99999999-9999-4999-8999-999999999999", None]
    )
    def test_a_browser_signed_in_as_someone_else_is_refused(
        self,
        client,
        configured,
        counter,
        drive_row,
        browser,
        exchanged,
        writes,
        presented,
    ):
        browser["user_id"] = presented
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=drive"
        )
        assert writes == []

    def test_a_user_no_longer_admin_at_callback_time_is_refused(
        self,
        client,
        configured,
        counter,
        drive_row,
        browser,
        exchanged,
        writes,
        monkeypatch,
    ):
        from src.exceptions.tenancy import TenantResolutionError

        async def authorize_member(
            session, workspace_id, user_id, minimum_role="member"
        ):
            raise TenantResolutionError("insufficient_role")

        monkeypatch.setattr(tenant_resolution, "authorize_member", authorize_member)
        resp = self._return(client)
        assert (
            resp.headers["location"]
            == f"{FRONT}/auth/error?reason=state_refused&flow=drive"
        )
        assert not any(w[0] == "store" for w in writes)


def test_an_open_sign_up_is_said_at_startup(monkeypatch):
    """TARGET_SIGNUP_OPEN is a local stack's switch (`fn_signup_admitted`); a process that runs
    with it says so, so production never has it on unnoticed. The app logger
    does not propagate, so a handler on it records the lines."""
    import logging

    from src.api.app import create_app
    from src.utils.logger import logger as app_logger
    from tests.src.api.conftest import FakeEngine

    lines: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = _Grab(level=logging.WARNING)
    app_logger.addHandler(handler)
    try:
        create_app(engine=FakeEngine())
        assert not [m for m in lines if "TARGET_SIGNUP_OPEN" in m]
        monkeypatch.setattr(settings, "TARGET_SIGNUP_OPEN", True)
        create_app(engine=FakeEngine())
        assert [m for m in lines if "TARGET_SIGNUP_OPEN is on" in m]
    finally:
        app_logger.removeHandler(handler)
