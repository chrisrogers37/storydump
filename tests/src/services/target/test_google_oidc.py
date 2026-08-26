"""Google sign-in — the three legs, unit-gated at zero network cost.

The claim checks are the load-bearing half: OIDC Core §3.1.3.7's `iss`,
`aud`, `exp`, `nonce` and `sub` each refused by name, and the email only
carried when Google marked it verified (D32: identity keys on `sub`; the
email is metadata). The exchange is driven through the egress seam so the
request shape — endpoint, grant type, redirect URI — is pinned without a
provider call.
"""

from __future__ import annotations

import base64
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from src.services.target import google_oidc as oidc
from tests.src.api.conftest import unsigned_id_token
from tests.src.services.target.conftest import capture_egress

CLIENT_ID = "cid.apps.googleusercontent.com"
STATE = "state-abc"


def _claims(**over) -> dict:
    """The default claim set as a dict — the shared builder's token, decoded."""
    defaults = {
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": "Person@Example.com",
        "name": "Person",
    }
    return oidc.decode_id_token(unsigned_id_token(STATE, **{**defaults, **over}))


class TestAuthorizationUrl:
    def test_carries_state_and_a_derived_nonce(self):
        url = oidc.authorization_url(
            client_id=CLIENT_ID,
            redirect_uri="https://api.test/auth/google/callback",
            state=STATE,
        )
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.netloc == "accounts.google.com"
        q = parse_qs(parts.query)
        assert q["response_type"] == ["code"]
        assert q["client_id"] == [CLIENT_ID]
        assert q["redirect_uri"] == ["https://api.test/auth/google/callback"]
        assert q["state"] == [STATE]
        assert q["nonce"] == [oidc.nonce_for(STATE)]
        assert set(q["scope"][0].split()) == {"openid", "email", "profile"}

    def test_nonce_is_a_pure_function_of_the_state(self):
        assert oidc.nonce_for(STATE) == oidc.nonce_for(STATE)
        assert oidc.nonce_for(STATE) != oidc.nonce_for(STATE + "x")


class TestVerifyIdToken:
    def test_accepts_a_good_token_and_lowercases_the_verified_email(self):
        who = oidc.verify_id_token(
            _claims(), client_id=CLIENT_ID, nonce=oidc.nonce_for(STATE)
        )
        assert who.sub == "1234567890"
        assert who.email == "person@example.com"
        assert who.display_name == "Person"

    def test_both_issuer_spellings_are_google(self):
        for iss in ("https://accounts.google.com", "accounts.google.com"):
            oidc.verify_id_token(
                _claims(iss=iss), client_id=CLIENT_ID, nonce=oidc.nonce_for(STATE)
            )

    def test_unverified_email_is_dropped_not_trusted(self):
        who = oidc.verify_id_token(
            _claims(email_verified=False),
            client_id=CLIENT_ID,
            nonce=oidc.nonce_for(STATE),
        )
        assert who.email is None
        assert who.sub == "1234567890"

    def test_audience_may_be_a_list(self):
        who = oidc.verify_id_token(
            _claims(aud=["other", CLIENT_ID]),
            client_id=CLIENT_ID,
            nonce=oidc.nonce_for(STATE),
        )
        assert who.sub

    @pytest.mark.parametrize(
        "over, reason",
        [
            ({"iss": "https://evil.example"}, "issuer"),
            ({"aud": "someone-else"}, "audience"),
            ({"exp": int(time.time()) - 3600}, "expired"),
            ({"iat": int(time.time()) + 3600}, "future"),
            ({"nonce": "not-ours"}, "nonce"),
            ({"nonce": None}, "nonce"),
            ({"sub": ""}, "subject"),
            ({"sub": None}, "subject"),
        ],
    )
    def test_each_claim_is_refused_by_name(self, over, reason):
        with pytest.raises(oidc.OidcRefused) as exc:
            oidc.verify_id_token(
                _claims(**over), client_id=CLIENT_ID, nonce=oidc.nonce_for(STATE)
            )
        assert exc.value.reason == reason

    def test_skew_tolerance_is_bounded(self):
        now = 1_000_000
        oidc.verify_id_token(
            _claims(exp=now - oidc.CLOCK_SKEW_SECONDS + 1, iat=now - 10),
            client_id=CLIENT_ID,
            nonce=oidc.nonce_for(STATE),
            now=now,
        )
        with pytest.raises(oidc.OidcRefused):
            oidc.verify_id_token(
                _claims(exp=now - oidc.CLOCK_SKEW_SECONDS - 1, iat=now - 10),
                client_id=CLIENT_ID,
                nonce=oidc.nonce_for(STATE),
                now=now,
            )


class TestDecodeIdToken:
    def test_round_trips_the_payload(self):
        assert oidc.decode_id_token(unsigned_id_token(STATE, sub="x"))["sub"] == "x"

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "a.b",
            "a.b.c.d",
            "a.!!!.c",
            "a." + base64.urlsafe_b64encode(b"[1]").decode() + ".c",
        ],
    )
    def test_malformed_is_refused(self, token):
        with pytest.raises(oidc.OidcRefused) as exc:
            oidc.decode_id_token(token)
        assert exc.value.reason == "malformed_id_token"


class TestExchangeCode:
    """Driven through the egress seam (`capture_egress`): the request shape is
    the contract, and it is pinned HERE for both Google legs — the Drive
    exchange posts through the same `code_grant`."""

    async def test_posts_the_code_grant_to_the_token_endpoint(self, monkeypatch):
        seen = capture_egress(monkeypatch, body={"id_token": "tok"})
        got = await oidc.exchange_code(
            None,
            code="c0de",
            redirect_uri="https://api.test/auth/google/callback",
            client_id=CLIENT_ID,
            client_secret="s3cret",
        )
        assert got == "tok"
        assert (seen["method"], seen["url"]) == ("POST", oidc.TOKEN_URL)
        assert seen["data"]["grant_type"] == "authorization_code"
        assert seen["data"]["code"] == "c0de"
        assert seen["data"]["redirect_uri"] == "https://api.test/auth/google/callback"
        assert seen["policy"].timeout_class == "standard"

    async def test_token_endpoint_error_is_refused_by_name(self, monkeypatch):
        capture_egress(monkeypatch, status=400, body={"error": "invalid_grant"})
        with pytest.raises(oidc.OidcRefused) as exc:
            await oidc.exchange_code(
                None, code="c", redirect_uri="r", client_id="i", client_secret="s"
            )
        assert exc.value.reason == "exchange_failed"

    async def test_a_response_without_an_id_token_is_refused(self, monkeypatch):
        capture_egress(monkeypatch, body={"access_token": "only"})
        with pytest.raises(oidc.OidcRefused) as exc:
            await oidc.exchange_code(
                None, code="c", redirect_uri="r", client_id="i", client_secret="s"
            )
        assert exc.value.reason == "no_id_token"

    def test_the_token_host_needs_no_allowlist_widening(self):
        from src.services.target.egress import DEFAULT_ALLOWED_HOSTS

        assert urlsplit(oidc.TOKEN_URL).hostname in DEFAULT_ALLOWED_HOSTS
