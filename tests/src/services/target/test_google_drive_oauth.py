"""The Drive connect leg — the pure half and the floor-driven half (P3 of the
gdrive credential epic, F2 (b): a target-tier sibling of `google_oidc`).

Driven through the egress seam exactly as `test_google_oidc.py` drives the
sign-in exchange (`capture_egress`, shared). The token-endpoint REQUEST shape
is pinned once, there, because both legs post it through one function
(`google_oidc.code_grant`); what is pinned here is what THIS leg makes of the
answer. Nothing here touches a database; the row the grant becomes is the
gate's business (`tests/scripts/test_gdrive_oauth_gate.py`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from src.services.target import google_drive_oauth as drive
from tests.src.services.target.conftest import capture_egress, drive_grant

CLIENT_ID = "cid.apps.googleusercontent.com"
REDIRECT = "https://api.test/auth/google-drive/callback"

#: The token endpoint's answer to an offline `drive.readonly` grant.
GRANT = {
    "access_token": "ya29.access",
    "refresh_token": "1//refresh",
    "expires_in": 3599,
    "scope": "https://www.googleapis.com/auth/drive.readonly",
    "token_type": "Bearer",
}


class TestAuthorizationUrl:
    def test_asks_for_offline_readonly_access_on_a_consent_screen(self):
        url = drive.authorization_url(
            client_id=CLIENT_ID, redirect_uri=REDIRECT, state="st4te"
        )
        parts = urlsplit(url)
        q = parse_qs(parts.query)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == drive.AUTHORIZE_URL
        assert q["response_type"] == ["code"]
        assert q["client_id"] == [CLIENT_ID]
        assert q["redirect_uri"] == [REDIRECT]
        assert q["state"] == ["st4te"]
        # drive.readonly and nothing else: the narrowest scope that lists and
        # downloads a pre-existing folder (legacy `google_drive_oauth.py:40-59`
        # evaluated drive.file and drive.metadata.readonly and rejected both;
        # the narrower path is the Picker, tracked as #327).
        assert q["scope"] == ["https://www.googleapis.com/auth/drive.readonly"]
        # offline + consent is what makes Google issue a refresh token, and
        # re-issue it on a repeat grant (the legacy flow's `:99-108` shape).
        assert q["access_type"] == ["offline"]
        assert q["prompt"] == ["consent"]
        # Not an OIDC flow: no nonce. The state row pins user and workspace.
        assert "nonce" not in q


class TestExchangeCode:
    """`egress.request` patched; what this leg makes of the token endpoint's
    answer is the contract (the request shape is `test_google_oidc`'s pin)."""

    @staticmethod
    def _answer(monkeypatch, **over):
        """The token endpoint's answer: GRANT unless overridden."""
        return capture_egress(monkeypatch, **({"body": GRANT} | over))

    @staticmethod
    async def _exchange():
        return await drive.exchange_code(
            None,
            code="c0de",
            redirect_uri=REDIRECT,
            client_id=CLIENT_ID,
            client_secret="s3cret",
        )

    async def test_posts_the_code_grant_and_keeps_both_tokens(self, monkeypatch):
        seen = self._answer(monkeypatch)
        before = datetime.now(timezone.utc)
        grant = await self._exchange()
        after = datetime.now(timezone.utc)
        # The request rides `google_oidc.code_grant`, whose shape is pinned
        # there; this leg's own claim is that it is the code grant at all.
        assert seen["data"]["grant_type"] == "authorization_code"
        assert grant.access_token == "ya29.access"
        assert grant.refresh_token == "1//refresh"
        # `expires_in` is relative to the exchange, so the expiry is bounded
        # by the clock on either side of it rather than pinned to an instant.
        ttl = timedelta(seconds=3599)
        assert before + ttl <= grant.expires_at <= after + ttl

    async def test_a_token_endpoint_error_is_refused_by_name(self, monkeypatch):
        self._answer(monkeypatch, status=400, body={"error": "invalid_grant"})
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "exchange_failed"

    async def test_a_grant_without_a_refresh_token_is_refused_not_stored(
        self, monkeypatch
    ):
        """F3 (b) stores the refresh token. A grant without one would work for
        an hour and then strand the source; the URL asked for one with
        `prompt=consent`, so its absence is refused by name rather than kept."""
        self._answer(monkeypatch, body={**GRANT, "refresh_token": None})
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "no_refresh_token"

    async def test_a_grant_that_narrowed_the_scope_is_refused(self, monkeypatch):
        """Google's consent screen can grant a subset of what was asked."""
        self._answer(
            monkeypatch,
            body={**GRANT, "scope": "https://www.googleapis.com/auth/userinfo.email"},
        )
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "scope_not_granted"

    async def test_a_body_that_is_not_json_or_lacks_the_access_token_is_malformed(
        self, monkeypatch
    ):
        self._answer(monkeypatch, raw=b"<html>")
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "malformed_response"
        self._answer(monkeypatch, body={**GRANT, "access_token": ""})
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "malformed_response"

    async def test_a_missing_expires_in_leaves_the_expiry_unknown(self, monkeypatch):
        body = dict(GRANT)
        del body["expires_in"]
        self._answer(monkeypatch, body=body)
        grant = await self._exchange()
        assert grant.expires_at is None

    def test_every_reason_the_leg_can_raise_has_a_redirect(self):
        """The map is TOTAL over the vocabulary because the vocabulary IS its
        key set: the constructor admits nothing else, so the callback cannot
        meet a reason it has no redirect for. Two redirects, no more."""
        assert set(drive.REDIRECT_REASON) == {
            "exchange_failed",
            "malformed_response",
            "no_refresh_token",
            "scope_not_granted",
        }
        assert set(drive.REDIRECT_REASON.values()) == {
            "exchange_failed",
            "grant_incomplete",
        }
        for reason in drive.REDIRECT_REASON:
            assert drive.DriveOAuthRefused(reason).reason == reason
        with pytest.raises(ValueError):
            drive.DriveOAuthRefused("not_a_reason", "x")


class TestPayloadEnvelope:
    """ONE encrypted column carries both tokens. The envelope is the contract
    between this writer and `drive_credentials` (the only reader), versioned
    so a later shape can be told apart from a malformed one."""

    def test_round_trips_both_tokens_under_a_version(self):
        plaintext = drive.encode_payload(drive_grant())
        assert json.loads(plaintext)["v"] == drive.PAYLOAD_VERSION
        assert drive.decode_payload(plaintext) == drive.DrivePayload(
            access_token="ya29.access", refresh_token="1//refresh"
        )

    @pytest.mark.parametrize(
        "plaintext",
        [
            "ya29.bare-token",  # the shape #1054's read door assumed nothing wrote
            json.dumps({"v": 2, "access_token": "a", "refresh_token": "r"}),
            json.dumps({"v": 1, "access_token": "a"}),
            json.dumps({"v": 1, "access_token": "", "refresh_token": "r"}),
            json.dumps([1, 2]),
        ],
    )
    def test_anything_but_a_v1_envelope_is_malformed(self, plaintext):
        with pytest.raises(drive.DrivePayloadMalformed):
            drive.decode_payload(plaintext)
