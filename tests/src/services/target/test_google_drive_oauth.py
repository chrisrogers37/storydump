"""The Drive connect leg — the pure half and the floor-driven half (P3 of the
gdrive credential epic, F2 (b): a target-tier sibling of `google_oidc`).

Driven through the egress seam exactly as `test_google_oidc.py` drives the
sign-in exchange: `egress.request` is patched and the REQUEST SHAPE is the
contract. Nothing here touches a database; the row the grant becomes is the
gate's business (`tests/scripts/test_gdrive_oauth_gate.py`).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from src.services.target import egress
from src.services.target import google_drive_oauth as drive

CLIENT_ID = "cid.apps.googleusercontent.com"
REDIRECT = "https://api.test/auth/google-drive/callback"
NOW = datetime(2026, 8, 25, 20, 0, tzinfo=timezone.utc)


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

    def test_the_scope_constant_is_the_single_readonly_scope(self):
        assert drive.SCOPE == "https://www.googleapis.com/auth/drive.readonly"


class TestExchangeCode:
    """`egress.request` patched; the request shape is the contract."""

    GRANT = {
        "access_token": "ya29.access",
        "refresh_token": "1//refresh",
        "expires_in": 3599,
        "scope": "https://www.googleapis.com/auth/drive.readonly",
        "token_type": "Bearer",
    }

    @staticmethod
    def _capture(monkeypatch, status=200, body=None, raw=None):
        seen = {}

        async def fake_request(client, method, url, *, policy=None, **kwargs):
            seen.update(method=method, url=url, policy=policy, **kwargs)
            content = (
                raw
                if raw is not None
                else json.dumps(
                    TestExchangeCode.GRANT if body is None else body
                ).encode()
            )
            return httpx.Response(
                status, content=content, headers={"content-type": "application/json"}
            )

        monkeypatch.setattr(drive.egress, "request", fake_request)
        return seen

    async def _exchange(self, **over):
        kw = dict(
            code="c0de",
            redirect_uri=REDIRECT,
            client_id=CLIENT_ID,
            client_secret="s3cret",
            now=NOW,
        )
        kw.update(over)
        return await drive.exchange_code(None, **kw)

    async def test_posts_the_code_grant_and_keeps_both_tokens(self, monkeypatch):
        seen = self._capture(monkeypatch)
        grant = await self._exchange()
        assert (seen["method"], seen["url"]) == ("POST", drive.TOKEN_URL)
        assert seen["data"] == {
            "code": "c0de",
            "client_id": CLIENT_ID,
            "client_secret": "s3cret",
            "redirect_uri": REDIRECT,
            "grant_type": "authorization_code",
        }
        assert seen["policy"].timeout_class == "standard"
        assert grant.access_token == "ya29.access"
        assert grant.refresh_token == "1//refresh"
        assert grant.expires_at == NOW + timedelta(seconds=3599)
        assert grant.scope == drive.SCOPE

    async def test_the_token_host_needs_no_allowlist_widening(self):
        assert urlsplit(drive.TOKEN_URL).hostname in egress.DEFAULT_ALLOWED_HOSTS

    async def test_a_token_endpoint_error_is_refused_by_name(self, monkeypatch):
        self._capture(monkeypatch, status=400, body={"error": "invalid_grant"})
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "exchange_failed"

    async def test_a_grant_without_a_refresh_token_is_refused_not_stored(
        self, monkeypatch
    ):
        """F3 (b) stores the refresh token. A grant without one would work for
        an hour and then strand the source; the URL asked for one with
        `prompt=consent`, so its absence is refused by name rather than kept."""
        self._capture(monkeypatch, body={**self.GRANT, "refresh_token": None})
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "no_refresh_token"

    async def test_a_grant_that_narrowed_the_scope_is_refused(self, monkeypatch):
        """Google's consent screen can grant a subset of what was asked."""
        self._capture(
            monkeypatch,
            body={
                **self.GRANT,
                "scope": "https://www.googleapis.com/auth/userinfo.email",
            },
        )
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "scope_not_granted"

    async def test_a_body_that_is_not_json_or_lacks_the_access_token_is_malformed(
        self, monkeypatch
    ):
        self._capture(monkeypatch, raw=b"<html>")
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "malformed_response"
        self._capture(monkeypatch, body={**self.GRANT, "access_token": ""})
        with pytest.raises(drive.DriveOAuthRefused) as exc:
            await self._exchange()
        assert exc.value.reason == "malformed_response"

    async def test_a_missing_expires_in_leaves_the_expiry_unknown(self, monkeypatch):
        body = dict(self.GRANT)
        del body["expires_in"]
        self._capture(monkeypatch, body=body)
        grant = await self._exchange()
        assert grant.expires_at is None

    def test_every_refusal_reason_is_in_the_closed_set(self):
        for reason in (
            "exchange_failed",
            "no_refresh_token",
            "scope_not_granted",
            "malformed_response",
        ):
            assert reason in drive.REASONS
        with pytest.raises(ValueError):
            drive.DriveOAuthRefused("not_a_reason", "x")


class TestPayloadEnvelope:
    """ONE encrypted column carries both tokens. The envelope is the contract
    between this writer and `drive_credentials` (the only reader), versioned
    so a later shape can be told apart from a malformed one."""

    def _grant(self, **over):
        kw = dict(
            access_token="ya29.access",
            refresh_token="1//refresh",
            expires_at=NOW,
            scope=drive.SCOPE,
        )
        kw.update(over)
        return drive.DriveGrant(**kw)

    def test_round_trips_both_tokens_under_a_version(self):
        plaintext = drive.encode_payload(self._grant())
        decoded = drive.decode_payload(plaintext)
        assert decoded["v"] == drive.PAYLOAD_VERSION == 1
        assert decoded["access_token"] == "ya29.access"
        assert decoded["refresh_token"] == "1//refresh"
        assert decoded["scope"] == drive.SCOPE

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
