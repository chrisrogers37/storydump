"""The Instagram connect leg — what the exchange makes of Meta's answers, and
which state the connect route mints (#1220 step 2; #1041).

Driven through the egress seam the way `test_google_drive_oauth.py` drives the
Drive exchange, except that this leg makes THREE provider calls in sequence
(code → short-lived token, short-lived → long-lived, token → profile), so the
fake answers a queue rather than one body. Nothing here touches a database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from src.services.target import egress
from src.services.target import ig_login_oauth as ig

REDIRECT = "https://api.test/auth/instagram-login/callback"
APP_ID = "1234567890"
APP_SECRET = "shh"

SHORT = {"data": [{"access_token": "IGQVJ-short", "user_id": 17841400000000001}]}
LONG = {"access_token": "IGQVJ-long", "token_type": "bearer", "expires_in": 5184000}
PROFILE = {"user_id": "17841400000000001", "username": "gatortails", "id": "9"}


def queued_egress(monkeypatch, answers):
    """Patch `egress.request` to answer *answers* in order — each a
    ``(status, body)`` — and record every call's shape."""
    calls = []
    queue = list(answers)

    async def fake_request(client, method, url, *, policy=None, **kwargs):
        calls.append({"method": method, "url": url, "policy": policy, **kwargs})
        status, body = queue.pop(0)
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(
            status, content=content, headers={"content-type": "application/json"}
        )

    monkeypatch.setattr(egress, "request", fake_request)
    return calls


async def _exchange(code="c0de#_"):
    async with httpx.AsyncClient() as client:
        return await ig.exchange_code(
            client,
            code=code,
            redirect_uri=REDIRECT,
            client_id=APP_ID,
            client_secret=APP_SECRET,
        )


class TestExchangeCode:
    async def test_three_calls_in_order_and_the_grant_carries_who_it_is_for(
        self, monkeypatch
    ):
        calls = queued_egress(monkeypatch, [(200, SHORT), (200, LONG), (200, PROFILE)])
        before = datetime.now(timezone.utc)
        grant = await _exchange()

        assert [c["method"] for c in calls] == ["POST", "GET", "GET"]
        assert calls[0]["url"] == ig.TOKEN_URL
        # The `#_` suffix Instagram appends to codes is stripped before the
        # exchange — the legacy flow's lesson, carried over.
        assert calls[0]["data"] == {
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT,
            "code": "c0de",
        }
        assert calls[1]["url"] == ig.LONG_LIVED_URL
        assert calls[1]["params"] == {
            "grant_type": "ig_exchange_token",
            "client_secret": APP_SECRET,
            "access_token": "IGQVJ-short",
        }
        assert calls[2]["url"] == ig.PROFILE_URL
        assert calls[2]["params"] == {
            "fields": "user_id,username",
            "access_token": "IGQVJ-long",
        }
        assert grant.access_token == "IGQVJ-long"
        assert grant.ig_user_id == "17841400000000001"
        assert grant.username == "gatortails"
        assert grant.expires_at is not None and grant.expires_at > before

    async def test_every_call_goes_through_the_floor_with_a_timeout_class(
        self, monkeypatch
    ):
        calls = queued_egress(monkeypatch, [(200, SHORT), (200, LONG), (200, PROFILE)])
        await _exchange()
        assert all(c["policy"] is not None for c in calls)

    async def test_a_refused_code_is_named_exchange_failed(self, monkeypatch):
        queued_egress(monkeypatch, [(400, {"error_message": "Invalid code"})])
        with pytest.raises(ig.IgOAuthRefused) as info:
            await _exchange()
        assert info.value.reason == "exchange_failed"
        assert "IGQVJ" not in str(info.value)

    async def test_a_short_lived_answer_without_a_token_is_malformed(self, monkeypatch):
        queued_egress(monkeypatch, [(200, {"data": [{"user_id": 1}]})])
        with pytest.raises(ig.IgOAuthRefused) as info:
            await _exchange()
        assert info.value.reason == "malformed_response"

    async def test_a_refused_long_lived_exchange_is_named(self, monkeypatch):
        queued_egress(monkeypatch, [(200, SHORT), (400, {"error": {"message": "no"}})])
        with pytest.raises(ig.IgOAuthRefused) as info:
            await _exchange()
        assert info.value.reason == "long_lived_failed"

    async def test_a_refused_profile_read_is_named(self, monkeypatch):
        queued_egress(monkeypatch, [(200, SHORT), (200, LONG), (401, {})])
        with pytest.raises(ig.IgOAuthRefused) as info:
            await _exchange()
        assert info.value.reason == "profile_failed"

    async def test_a_profile_without_a_user_id_is_malformed(self, monkeypatch):
        queued_egress(monkeypatch, [(200, SHORT), (200, LONG), (200, {"id": "9"})])
        with pytest.raises(ig.IgOAuthRefused) as info:
            await _exchange()
        assert info.value.reason == "malformed_response"

    async def test_a_body_that_is_not_json_is_malformed_not_a_crash(self, monkeypatch):
        queued_egress(monkeypatch, [(200, b"<html>")])
        with pytest.raises(ig.IgOAuthRefused) as info:
            await _exchange()
        assert info.value.reason == "malformed_response"

    def test_every_reason_the_leg_can_raise_has_a_redirect(self):
        for reason in (
            "exchange_failed",
            "malformed_response",
            "long_lived_failed",
            "profile_failed",
        ):
            assert reason in ig.REDIRECT_REASON

    def test_an_unknown_reason_cannot_be_raised(self):
        with pytest.raises(ValueError):
            ig.IgOAuthRefused("something_else")


class TestAuthorizationUrl:
    def test_asks_instagram_for_the_two_scopes_with_the_state(self):
        url = ig.authorization_url("st4te", redirect_uri=REDIRECT, client_id=APP_ID)
        parts = urlsplit(url)
        q = parse_qs(parts.query)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == ig.AUTHORIZE_URL
        assert parts.netloc == "api.instagram.com"
        assert q["state"] == ["st4te"]
        assert q["client_id"] == [APP_ID]
        assert q["redirect_uri"] == [REDIRECT]
        assert set(q["scope"][0].split(",")) == set(ig.REQUIRED_SCOPES)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _Conn:
    def __init__(self, value):
        self._value = value
        self.params = None

    async def execute(self, statement, params=None):
        self.params = params
        return _ScalarResult(self._value)


class TestConnectPurpose:
    """Which state the connect route mints — the same three-way answer the Drive
    route gets from `google_drive_oauth.connect_purpose`."""

    async def test_an_account_that_is_not_this_workspaces_is_none(self):
        assert (
            await ig.connect_purpose(_Conn(None), workspace_id="ws", ig_account_id="a")
            is None
        )

    async def test_a_never_credentialed_account_connects(self):
        assert (
            await ig.connect_purpose(_Conn(False), workspace_id="ws", ig_account_id="a")
            == "connect"
        )

    async def test_a_credentialed_account_reconnects(self):
        assert (
            await ig.connect_purpose(_Conn(True), workspace_id="ws", ig_account_id="a")
            == "reconnect"
        )

    async def test_the_query_binds_both_keys(self):
        conn = _Conn(False)
        await ig.connect_purpose(conn, workspace_id="ws-1", ig_account_id="acct-1")
        assert conn.params["ws"] == "ws-1" and conn.params["acct"] == "acct-1"
        assert conn.params["provider"] == ig.PROVIDER
