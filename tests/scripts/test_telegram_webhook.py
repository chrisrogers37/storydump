"""`scripts/telegram_webhook.py` — register and check the target bot's webhook
without a secret ever reaching a terminal paste (#1224, #1157).

The Bot API and the API's webhook door are behind one seam (`_http`), so what
is pinned is the tool's own behaviour: which calls it makes with which
parameters, how it classifies the answers, that a redirect is never followed,
that the wrong bot is refused, and that NO output line ever carries the bot
token or the webhook secret.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from scripts import telegram_webhook as tool

TOKEN = "123456:ABC-secret-token-value"
SECRET = "0123456789abcdef0123456789abcdef"
URL = "https://api.storydump.app/webhooks/telegram"
BOT = "storydump_app_bot"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TARGET_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", SECRET)
    monkeypatch.setenv("TARGET_TELEGRAM_BOT_USERNAME", BOT)


@pytest.fixture
def http(monkeypatch):
    """Scripted answers keyed on the path's last segment; records every call.
    A faithful Bot API: what `setWebhook` set is what `getWebhookInfo` reports."""
    calls = []
    answers = {
        "getMe": (200, {"ok": True, "result": {"id": 42, "username": BOT}}),
        "getWebhookInfo": (
            200,
            {
                "ok": True,
                "result": {
                    "url": URL,
                    "pending_update_count": 0,
                    "allowed_updates": ["message"],
                },
            },
        ),
        "setWebhook": (
            200,
            {"ok": True, "result": True, "description": "Webhook was set"},
        ),
        "deleteWebhook": (200, {"ok": True, "result": True}),
        "telegram": (400, {"detail": "malformed body"}),
    }

    def fake(method, url, *, data=None, headers=None):
        calls.append(
            {"method": method, "url": url, "data": data, "headers": headers or {}}
        )
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name == "setWebhook" and answers[name][1].get("ok"):
            answers["getWebhookInfo"][1]["result"]["url"] = data["url"]
        return answers[name]

    monkeypatch.setattr(tool, "_http", fake)
    fake.answers = answers
    fake.calls = calls
    return fake


def _run(capsys, *argv):
    code = tool.main(list(argv))
    out = capsys.readouterr()
    return code, out.out + out.err


class TestSecretsNeverPrint:
    @pytest.mark.parametrize("argv", [("status",), ("register",), ("deregister",)])
    def test_no_output_line_carries_the_token_or_the_secret(
        self, env, http, capsys, argv
    ):
        _, text = _run(capsys, *argv)
        assert TOKEN not in text
        assert SECRET not in text
        assert "ABC-secret" not in text

    def test_a_bot_api_error_body_is_summarised_not_echoed(self, env, http, capsys):
        http.answers["setWebhook"] = (
            400,
            {"ok": False, "description": f"bad token {TOKEN}", "error_code": 400},
        )
        code, text = _run(capsys, "register")
        assert code == 1
        assert TOKEN not in text
        assert "400" in text

    def test_set_webhook_prints_ok_not_telegrams_description(self, env, http, capsys):
        _, text = _run(capsys, "register")
        assert "Webhook was set" not in text
        assert "setWebhook: ok" in text


class TestEnv:
    def test_a_missing_token_is_exit_2_naming_the_variable(self, monkeypatch, capsys):
        monkeypatch.delenv("TARGET_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.setenv("TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", SECRET)
        code, text = _run(capsys, "status")
        assert code == 2
        assert "TARGET_TELEGRAM_BOT_TOKEN" in text

    def test_register_without_a_secret_is_exit_2_naming_it(
        self, monkeypatch, http, capsys
    ):
        monkeypatch.setenv("TARGET_TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.delenv("TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        code, text = _run(capsys, "register")
        assert code == 2
        assert "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN" in text
        assert http.calls == []

    def test_register_without_the_bot_username_is_exit_2_naming_it(
        self, monkeypatch, http, capsys
    ):
        monkeypatch.setenv("TARGET_TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.setenv("TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", SECRET)
        monkeypatch.delenv("TARGET_TELEGRAM_BOT_USERNAME", raising=False)
        code, text = _run(capsys, "register")
        assert code == 2
        assert "TARGET_TELEGRAM_BOT_USERNAME" in text
        assert http.calls == []

    def test_an_http_url_is_refused_before_any_call(self, env, http, capsys):
        code, text = _run(
            capsys, "status", "--url", "http://api.storydump.app/webhooks/telegram"
        )
        assert code == 2
        assert "https" in text
        assert http.calls == []


class TestTheWrongBot:
    def test_register_refuses_a_token_that_is_not_the_configured_bot(
        self, env, http, capsys
    ):
        http.answers["getMe"] = (
            200,
            {"ok": True, "result": {"id": 7, "username": "storydumpapp_bot"}},
        )
        code, text = _run(capsys, "register")
        assert code == 1
        assert "storydumpapp_bot" in text and BOT in text
        assert not any(c["url"].endswith("/setWebhook") for c in http.calls)

    def test_status_reports_the_mismatch_and_fails(self, env, http, capsys):
        http.answers["getMe"] = (
            200,
            {"ok": True, "result": {"id": 7, "username": "storydumpapp_bot"}},
        )
        code, text = _run(capsys, "status")
        assert code == 1
        assert "wrong bot" in text

    def test_the_configured_name_may_carry_an_at_sign(
        self, env, http, capsys, monkeypatch
    ):
        monkeypatch.setenv("TARGET_TELEGRAM_BOT_USERNAME", f"@{BOT}")
        code, _ = _run(capsys, "register")
        assert code == 0


class TestStatus:
    def test_reports_the_bot_the_webhook_and_the_api_door(self, env, http, capsys):
        code, text = _run(capsys, "status")
        assert code == 0
        assert f"@{BOT}" in text
        assert URL in text
        assert "pending: 0" in text
        # The API door check: a 400 means the secret was ACCEPTED and the
        # empty body was refused one step later — exactly the proof wanted.
        assert "API accepts the secret" in text
        api = [c for c in http.calls if c["url"] == URL]
        assert (
            api and api[0]["headers"].get("X-Telegram-Bot-Api-Secret-Token") == SECRET
        )

    def test_a_403_from_the_api_means_the_secret_does_not_match(
        self, env, http, capsys
    ):
        http.answers["telegram"] = (403, {"detail": "forbidden"})
        code, text = _run(capsys, "status")
        assert code == 1
        assert "REFUSES" in text

    def test_a_redirect_from_the_door_is_a_failed_check_never_a_hop(
        self, env, http, capsys
    ):
        http.answers["telegram"] = (302, None)
        code, text = _run(capsys, "status")
        assert code == 1
        assert "redirect" in text.lower()
        assert "NOT followed" in text

    def test_the_opener_never_follows_redirects(self):
        handler = tool._NoRedirects()
        assert (
            handler.redirect_request(
                urllib.request.Request(URL),
                None,
                302,
                "Found",
                {},
                "https://evil.example/",
            )
            is None
        )

    def test_an_unset_secret_means_the_door_was_not_checked_and_that_is_a_failure(
        self, monkeypatch, http, capsys
    ):
        monkeypatch.setenv("TARGET_TELEGRAM_BOT_TOKEN", TOKEN)
        monkeypatch.delenv("TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        code, text = _run(capsys, "status")
        assert code == 1
        assert "NOT CHECKED" in text

    def test_no_webhook_registered_is_said_plainly(self, env, http, capsys):
        http.answers["getWebhookInfo"] = (
            200,
            {"ok": True, "result": {"url": "", "pending_update_count": 0}},
        )
        code, text = _run(capsys, "status")
        assert code == 1
        assert "no webhook" in text.lower()

    def test_a_last_error_from_telegram_is_surfaced(self, env, http, capsys):
        http.answers["getWebhookInfo"] = (
            200,
            {
                "ok": True,
                "result": {
                    "url": URL,
                    "pending_update_count": 3,
                    "last_error_date": 1756900000,
                    "last_error_message": "Wrong response from the webhook: 403 Forbidden",
                },
            },
        )
        code, text = _run(capsys, "status")
        assert code == 1
        assert "403 Forbidden" in text


class TestRegister:
    def test_sets_the_webhook_with_the_secret_and_messages_only_keeping_the_backlog(
        self, env, http, capsys
    ):
        code, _ = _run(capsys, "register")
        assert code == 0
        (call,) = [c for c in http.calls if c["url"].endswith("/setWebhook")]
        assert call["method"] == "POST"
        assert call["data"]["url"] == URL
        assert call["data"]["secret_token"] == SECRET
        assert json.loads(call["data"]["allowed_updates"]) == ["message"]
        assert call["data"]["drop_pending_updates"] == "false"

    def test_dropping_the_backlog_is_opt_in(self, env, http, capsys):
        code, text = _run(capsys, "register", "--drop-pending")
        assert code == 0
        (call,) = [c for c in http.calls if c["url"].endswith("/setWebhook")]
        assert call["data"]["drop_pending_updates"] == "true"
        assert "pending updates dropped" in text

    def test_the_url_can_be_overridden_for_a_preview_deployment(
        self, env, http, capsys
    ):
        other = "https://preview.example.test/webhooks/telegram"
        code, _ = _run(capsys, "register", "--url", other)
        assert code == 0
        (call,) = [c for c in http.calls if c["url"].endswith("/setWebhook")]
        assert call["data"]["url"] == other

    def test_deregister_deletes_the_webhook(self, env, http, capsys):
        code, _ = _run(capsys, "deregister")
        assert code == 0
        assert any(c["url"].endswith("/deleteWebhook") for c in http.calls)
