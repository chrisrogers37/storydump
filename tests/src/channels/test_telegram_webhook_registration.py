"""The API registers its own webhook at startup (phase 1 of the 2026-09-09 tap
plan, deploy step made automatic): what it asks Telegram for, what it refuses,
and what it reports — never a secret."""

from __future__ import annotations

import pytest

from src.channels import telegram_webhook_registration as reg

TOKEN = "8675309:AAtestSECRETtokenVALUExyz"
SECRET = "0123456789abcdef0123456789abcdef"
URL = "https://api.storydump.app/webhooks/telegram"


class _Transport:
    """A scripted bot: records every call; answers `getMe` and `getWebhookInfo`."""

    def __init__(self, username="storydump_app_bot", *, fail_set=None, info_url=URL):
        self.username = username
        self.fail_set = fail_set
        self.info_url = info_url
        self.calls = []

    def redact(self, text):
        return text.replace(TOKEN, "<TOKEN>")

    async def probe(self):
        self.calls.append(("getMe", None))
        return self.username

    async def set_webhook(self, **kw):
        self.calls.append(("setWebhook", kw))
        if self.fail_set is not None:
            raise self.fail_set
        return True

    async def webhook_info(self):
        self.calls.append(("getWebhookInfo", None))
        return {
            "url": self.info_url,
            "allowed_updates": ["message", "callback_query"],
            "pending_update_count": 3,
            "max_connections": 10,
        }


class TestWhatIsAskedFor:
    def test_taps_are_asked_for(self):
        assert "callback_query" in reg.ALLOWED_UPDATES
        assert "message" in reg.ALLOWED_UPDATES

    @pytest.mark.asyncio
    async def test_register_sets_the_url_secret_kinds_and_cap_then_reads_back(self):
        t = _Transport()
        report = await reg.register(
            t,
            url=URL,
            secret=SECRET,
            expected_bot="storydump_app_bot",
            max_connections=10,
        )
        assert [c[0] for c in t.calls] == ["getMe", "setWebhook", "getWebhookInfo"]
        assert t.calls[1][1] == {
            "url": URL,
            "secret_token": SECRET,
            "allowed_updates": ["message", "callback_query"],
            "max_connections": 10,
        }
        assert report["ok"] is True
        assert report["bot"] == "storydump_app_bot"
        assert report["allowed_updates"] == ["message", "callback_query"]
        assert report["pending_update_count"] == 3
        assert SECRET not in str(report) and TOKEN not in str(report)

    @pytest.mark.asyncio
    async def test_the_wrong_bot_is_refused_before_set_webhook(self):
        t = _Transport(username="someone_elses_bot")
        report = await reg.register(
            t,
            url=URL,
            secret=SECRET,
            expected_bot="storydump_app_bot",
            max_connections=10,
        )
        assert report["ok"] is False and "someone_elses_bot" in report["error"]
        assert [c[0] for c in t.calls] == ["getMe"]

    @pytest.mark.asyncio
    async def test_a_failed_set_webhook_is_a_report_never_a_raise(self):
        t = _Transport(fail_set=RuntimeError(f"boom {TOKEN}"))
        report = await reg.register(
            t, url=URL, secret=SECRET, expected_bot=None, max_connections=10
        )
        assert report["ok"] is False
        assert report["error"].startswith("RuntimeError:")
        assert TOKEN not in report["error"] and "<TOKEN>" in report["error"]

    @pytest.mark.asyncio
    async def test_a_different_registered_url_is_not_ok(self):
        t = _Transport(info_url="https://elsewhere.example/hook")
        report = await reg.register(
            t, url=URL, secret=SECRET, expected_bot=None, max_connections=10
        )
        assert report["ok"] is False and "different URL" in report["error"]


class TestTheKnobs:
    def test_the_cap_defaults_and_bounds(self):
        assert reg.max_connections_from(None) == reg.DEFAULT_MAX_CONNECTIONS
        assert reg.max_connections_from(" 20 ") == 20
        for bad in ("0", "101", "many"):
            with pytest.raises(reg.BadMaxConnections):
                reg.max_connections_from(bad)

    def test_autoregister_is_on_unless_switched_off(self):
        assert reg.autoregister_enabled(None) is True
        assert reg.autoregister_enabled("1") is True
        for off in ("0", "false", "no", "OFF"):
            assert reg.autoregister_enabled(off) is False

    def test_bot_matching_ignores_the_at_sign_and_case(self):
        assert reg.bot_matches("Storydump_App_Bot", "@storydump_app_bot")
        assert reg.bot_matches("x", None)
        assert not reg.bot_matches("x", "y")
