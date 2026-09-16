"""``storydump webhook status|register|deregister`` — `scripts/telegram_webhook.py`
absorbed (phase 03 of the v2 CLI), with its rule that no secret ever reaches
a terminal paste.

The Bot API and the API's webhook door are behind one seam (``_http``), so
what is pinned is the verb's own behaviour: which calls it makes with which
parameters, how it classifies the answers, that a redirect is never
followed, that the wrong bot is refused, and that NO output line ever
carries the bot token or the webhook secret. The deployment's variables are
read from the runtime's environment under the SAME names the deployment
uses — one spelling, in the vocabulary module.
"""

from __future__ import annotations

import json

import pytest

from src.services.target.vocabulary import (
    ALLOWED_UPDATES,
    DEFAULT_MAX_CONNECTIONS,
    EXIT_API_UNREACHABLE,
    EXIT_OK,
    EXIT_USAGE,
    MAX_CONNECTIONS_VAR,
    TELEGRAM_BOT_VAR,
    TELEGRAM_SECRET_VAR,
    TELEGRAM_TOKEN_VAR,
    WEBHOOK_SECRET_HEADER,
)
from storydump_cli import webhook
from tests.storydump_cli.test_main import Api, one_envelope, run, runtime

TOKEN = "123456:ABC-secret-token-value"
SECRET = "0123456789abcdef0123456789abcdef"
URL = "https://api.storydump.app/webhooks/telegram"
BOT = "storydump_app_bot"

ENV = {TELEGRAM_TOKEN_VAR: TOKEN, TELEGRAM_SECRET_VAR: SECRET, TELEGRAM_BOT_VAR: BOT}


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

    monkeypatch.setattr(webhook, "_http", fake)
    fake.answers = answers
    fake.calls = calls
    return fake


def _run(tmp_path, env, *argv, json_mode=False):
    rt = runtime(tmp_path, Api({}), env=dict(env))
    flags = ["--json"] if json_mode else []
    result = run(rt, *flags, "webhook", *argv)
    return result.exit_code, result.stdout + result.stderr, result


class TestTapsAreAskedFor:
    """Phase 1 of the 2026-09-09 tap plan: Telegram delivers only the update
    kinds the registration asks for. Without `callback_query` every tap is
    dropped before it reaches the route — the plan's first blocker."""

    def test_the_registration_asks_for_taps(self):
        assert "callback_query" in ALLOWED_UPDATES
        assert "message" in ALLOWED_UPDATES

    def test_register_sends_the_allowed_updates_and_a_connection_cap(
        self, tmp_path, http
    ):
        code, out, _ = _run(tmp_path, ENV, "register")
        assert code == EXIT_OK, out
        body = [c for c in http.calls if c["url"].endswith("setWebhook")][0]["data"]
        assert json.loads(body["allowed_updates"]) == list(ALLOWED_UPDATES)
        assert body["max_connections"] == str(DEFAULT_MAX_CONNECTIONS)

    def test_the_connection_cap_comes_from_the_environment(self, tmp_path, http):
        code, _, _ = _run(tmp_path, {**ENV, MAX_CONNECTIONS_VAR: "20"}, "register")
        assert code == EXIT_OK
        body = [c for c in http.calls if c["url"].endswith("setWebhook")][0]["data"]
        assert body["max_connections"] == "20"

    @pytest.mark.parametrize("bad", ["0", "101", "many"])
    def test_a_cap_outside_telegrams_range_is_usage_before_any_call(
        self, tmp_path, http, bad
    ):
        code, out, _ = _run(tmp_path, {**ENV, MAX_CONNECTIONS_VAR: bad}, "register")
        assert code == EXIT_USAGE
        assert MAX_CONNECTIONS_VAR in out
        assert not [c for c in http.calls if c["url"].endswith("setWebhook")]


class TestSecretsNeverPrint:
    @pytest.mark.parametrize("argv", [("status",), ("register",), ("deregister",)])
    @pytest.mark.parametrize("json_mode", [False, True])
    def test_no_output_line_carries_the_token_or_the_secret(
        self, tmp_path, http, argv, json_mode
    ):
        _, text, _ = _run(tmp_path, ENV, *argv, json_mode=json_mode)
        assert TOKEN not in text
        assert SECRET not in text
        assert "ABC-secret" not in text

    def test_a_bot_api_error_body_is_summarised_not_echoed(self, tmp_path, http):
        http.answers["setWebhook"] = (
            400,
            {"ok": False, "description": f"bad token {TOKEN}", "error_code": 400},
        )
        code, text, _ = _run(tmp_path, ENV, "register")
        assert code == EXIT_API_UNREACHABLE
        assert TOKEN not in text
        assert "400" in text

    def test_set_webhook_prints_ok_not_telegrams_description(self, tmp_path, http):
        _, text, _ = _run(tmp_path, ENV, "register")
        assert "Webhook was set" not in text
        assert "setWebhook: ok" in text

    def test_a_bot_token_in_any_rendered_string_is_redacted(self):
        from storydump_cli.output import redact

        assert TOKEN not in redact(f"bot{TOKEN}/getMe answered 401")


class TestEnv:
    def test_a_missing_token_is_usage_naming_the_variable(self, tmp_path, http):
        code, text, _ = _run(tmp_path, {TELEGRAM_SECRET_VAR: SECRET}, "status")
        assert code == EXIT_USAGE
        assert TELEGRAM_TOKEN_VAR in text
        assert http.calls == []

    def test_register_without_a_secret_is_usage_naming_it(self, tmp_path, http):
        code, text, _ = _run(
            tmp_path, {TELEGRAM_TOKEN_VAR: TOKEN, TELEGRAM_BOT_VAR: BOT}, "register"
        )
        assert code == EXIT_USAGE
        assert TELEGRAM_SECRET_VAR in text
        assert http.calls == []

    def test_register_without_the_bot_username_is_usage_naming_it(self, tmp_path, http):
        code, text, _ = _run(
            tmp_path,
            {TELEGRAM_TOKEN_VAR: TOKEN, TELEGRAM_SECRET_VAR: SECRET},
            "register",
        )
        assert code == EXIT_USAGE
        assert TELEGRAM_BOT_VAR in text
        assert http.calls == []

    def test_an_http_url_is_refused_before_any_call(self, tmp_path, http):
        code, text, _ = _run(
            tmp_path,
            ENV,
            "status",
            "--url",
            "http://api.storydump.app/webhooks/telegram",
        )
        assert code == EXIT_USAGE
        assert "https" in text
        assert http.calls == []

    def test_the_url_defaults_from_the_environment(self, tmp_path, http):
        other = "https://preview.example.test/webhooks/telegram"
        from src.services.target.vocabulary import WEBHOOK_URL_VAR

        code, _, _ = _run(tmp_path, {**ENV, WEBHOOK_URL_VAR: other}, "register")
        assert code == EXIT_OK
        (call,) = [c for c in http.calls if c["url"].endswith("/setWebhook")]
        assert call["data"]["url"] == other


class TestTheWrongBot:
    def test_register_refuses_a_token_that_is_not_the_configured_bot(
        self, tmp_path, http
    ):
        http.answers["getMe"] = (
            200,
            {"ok": True, "result": {"id": 7, "username": "storydumpapp_bot"}},
        )
        code, text, _ = _run(tmp_path, ENV, "register")
        assert code == EXIT_API_UNREACHABLE
        assert "storydumpapp_bot" in text and BOT in text
        assert not any(c["url"].endswith("/setWebhook") for c in http.calls)

    def test_status_reports_the_mismatch_and_fails(self, tmp_path, http):
        http.answers["getMe"] = (
            200,
            {"ok": True, "result": {"id": 7, "username": "storydumpapp_bot"}},
        )
        code, text, _ = _run(tmp_path, ENV, "status")
        assert code == EXIT_API_UNREACHABLE
        assert "wrong bot" in text

    def test_the_configured_name_may_carry_an_at_sign(self, tmp_path, http):
        code, _, _ = _run(tmp_path, {**ENV, TELEGRAM_BOT_VAR: f"@{BOT}"}, "register")
        assert code == EXIT_OK


class TestStatus:
    def test_reports_the_bot_the_webhook_and_the_api_door(self, tmp_path, http):
        code, text, _ = _run(tmp_path, ENV, "status")
        assert code == EXIT_OK, text
        assert f"@{BOT}" in text
        assert URL in text
        assert "pending: 0" in text
        # The API door check: a 400 means the secret was ACCEPTED and the
        # empty body was refused one step later — exactly the proof wanted.
        assert "API accepts the secret" in text
        api = [c for c in http.calls if c["url"] == URL]
        assert api and api[0]["headers"].get(WEBHOOK_SECRET_HEADER) == SECRET

    def test_json_is_one_envelope_of_checks(self, tmp_path, http):
        code, _, result = _run(tmp_path, ENV, "status", json_mode=True)
        assert code == EXIT_OK
        document = one_envelope(result)
        assert document["kind"] == "webhook"
        assert document["data"]["action"] == "status"
        assert document["data"]["ok"] is True
        checks = {c["check"]: c for c in document["data"]["checks"]}
        assert {"bot", "webhook", "api_door"} <= set(checks)
        assert all(set(c) == {"check", "state", "detail"} for c in checks.values())
        assert checks["api_door"]["state"] == "ok"

    def test_a_registered_url_that_is_not_the_expected_one_is_a_failed_check(
        self, tmp_path, http
    ):
        """The bot pointing at another host is the one misregistration a
        status check exists to catch — a failed `url` check, exit 4."""
        http.answers["getWebhookInfo"][1]["result"]["url"] = (
            "https://old-host.example/webhooks/telegram"
        )
        code, text, result = _run(tmp_path, ENV, "status", json_mode=True)
        assert code == EXIT_API_UNREACHABLE, text
        document = one_envelope(result)
        checks = {c["check"]: c for c in document["data"]["checks"]}
        assert checks["url"]["state"] == "failed"
        assert URL in checks["url"]["detail"]

    def test_json_before_the_group_is_honoured(self, tmp_path, http):
        """`storydump --json webhook status` and `storydump webhook --json status`
        are the same command — the package promises the flag in any position."""
        rt = runtime(tmp_path, Api({}), env=dict(ENV))
        result = run(rt, "webhook", "--json", "status")
        assert result.exit_code == EXIT_OK, result.output
        assert one_envelope(result)["kind"] == "webhook"

    def test_a_403_from_the_api_means_the_secret_does_not_match(self, tmp_path, http):
        http.answers["telegram"] = (403, {"detail": "forbidden"})
        code, text, _ = _run(tmp_path, ENV, "status")
        assert code == EXIT_API_UNREACHABLE
        assert "REFUSES" in text

    def test_a_redirect_from_the_door_is_a_failed_check_never_a_hop(
        self, tmp_path, http
    ):
        http.answers["telegram"] = (302, None)
        code, text, _ = _run(tmp_path, ENV, "status")
        assert code == EXIT_API_UNREACHABLE
        assert "redirect" in text.lower()
        assert "NOT followed" in text

    def test_the_http_seam_never_follows_redirects(self, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 302
            content = b""

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def request(self, method, url, **kwargs):
                captured["request"] = (method, url, kwargs)
                return FakeResponse()

        monkeypatch.setattr(webhook.httpx, "Client", FakeClient)
        status, body = webhook._http("GET", URL)
        assert (status, body) == (302, None)
        assert captured["follow_redirects"] is False, (
            "a redirect must be reported as the 3xx it is — following it would"
            " re-send the secret to wherever it points"
        )

    def test_an_unset_secret_means_the_door_was_not_checked_and_that_is_a_failure(
        self, tmp_path, http
    ):
        code, text, _ = _run(tmp_path, {TELEGRAM_TOKEN_VAR: TOKEN}, "status")
        assert code == EXIT_API_UNREACHABLE
        assert "NOT CHECKED" in text

    def test_no_webhook_registered_is_said_plainly(self, tmp_path, http):
        http.answers["getWebhookInfo"] = (
            200,
            {"ok": True, "result": {"url": "", "pending_update_count": 0}},
        )
        code, text, _ = _run(tmp_path, ENV, "status")
        assert code == EXIT_API_UNREACHABLE
        assert "no webhook" in text.lower()

    def test_a_last_error_from_telegram_is_surfaced(self, tmp_path, http):
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
        code, text, _ = _run(tmp_path, ENV, "status")
        assert code == EXIT_API_UNREACHABLE
        assert "403 Forbidden" in text


class TestRegister:
    def test_sets_the_webhook_with_the_secret_and_the_served_updates_keeping_the_backlog(
        self, tmp_path, http
    ):
        code, _, _ = _run(tmp_path, ENV, "register")
        assert code == EXIT_OK
        (call,) = [c for c in http.calls if c["url"].endswith("/setWebhook")]
        assert call["method"] == "POST"
        assert call["data"]["url"] == URL
        assert call["data"]["secret_token"] == SECRET
        assert json.loads(call["data"]["allowed_updates"]) == list(ALLOWED_UPDATES)
        assert call["data"]["drop_pending_updates"] == "false"

    def test_dropping_the_backlog_is_opt_in(self, tmp_path, http):
        code, text, _ = _run(tmp_path, ENV, "register", "--drop-pending")
        assert code == EXIT_OK
        (call,) = [c for c in http.calls if c["url"].endswith("/setWebhook")]
        assert call["data"]["drop_pending_updates"] == "true"
        assert "pending updates dropped" in text

    def test_the_url_can_be_overridden_for_a_preview_deployment(self, tmp_path, http):
        other = "https://preview.example.test/webhooks/telegram"
        code, _, _ = _run(tmp_path, ENV, "register", "--url", other)
        assert code == EXIT_OK
        (call,) = [c for c in http.calls if c["url"].endswith("/setWebhook")]
        assert call["data"]["url"] == other

    def test_register_ends_with_the_status_check(self, tmp_path, http):
        code, text, _ = _run(tmp_path, ENV, "register")
        assert code == EXIT_OK
        assert "API accepts the secret" in text
        assert [c["url"].rsplit("/", 1)[-1] for c in http.calls][-1] == "telegram"

    def test_deregister_deletes_the_webhook(self, tmp_path, http):
        code, text, _ = _run(tmp_path, ENV, "deregister")
        assert code == EXIT_OK
        assert any(c["url"].endswith("/deleteWebhook") for c in http.calls)
        assert "deleteWebhook: ok" in text
