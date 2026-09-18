"""``storydump webhook status|register|deregister`` — register and check the
target bot's Telegram webhook without a secret ever reaching a terminal
paste (`scripts/telegram_webhook.py`, absorbed in phase 03 of the v2 CLI).

Reads the bot token, the webhook secret and the bot's username from the
SAME variables the deployment uses (their one spelling is the vocabulary
module), so there is nothing to retype. Every line this prints is safe to
paste: tokens and secrets are read, sent, and never echoed — a Bot API error
body is summarised by status, not quoted, because Telegram's own error text
can carry the token back.

Three guards the secret's safety rests on: the webhook URL must be `https`
(a cleartext door would carry the secret in the clear); redirects are NEVER
followed (a client that followed one would re-send every header, secret
included, to wherever a 3xx points — so a 3xx is a failed check, not a
hop); and `register` refuses unless the token's bot IS the configured bot —
a webhook set on the wrong bot's token would break whatever that bot serves.

The `status` check of the API door is the proof the whole set-up wants: a
POST to the webhook URL carrying the secret and an empty body answers
**400** when the secret is accepted (the empty body is refused one step
later, as "malformed body") and **403** when it is not. No session, no real
update, no side effect — and it distinguishes "wrong secret" from "not
registered" from "ingress not wired" (503), three different remedies.

A run is a REPORT — ``{"action", "ok", "checks": [{"check", "state",
"detail"}]}`` — and the verb's exit code is the report's verdict: 0 when
every check passed, 4 when one failed; a missing or refused variable is a
usage error (64) before any call is made.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from src.services.target.vocabulary import (
    ALLOWED_UPDATES,
    DATABASE_URL_VAR,
    DEFAULT_WEBHOOK_URL,
    EXIT_API_UNREACHABLE,
    EXIT_USAGE,
    MAX_CONNECTIONS_VAR,
    TELEGRAM_BOT_VAR,
    TELEGRAM_SECRET_VAR,
    TELEGRAM_TOKEN_VAR,
    WEBHOOK_SECRET_HEADER,
    WEBHOOK_URL_VAR,
    BadMaxConnections,
    bot_matches,
    max_connections_from,
)
from storydump_cli.output import Failure

BOT_API = "https://api.telegram.org"
TIMEOUT_S = 20.0
ENV_FIX = "export the deployment's variable in this shell (never paste it into a chat)"
REGISTER_HINT = "run storydump webhook register"

#: What to add to a failure line, per method, when it helps the operator.
_HINT = {"getMe": " — is the token the bot's?"}


class MissingVariable(Failure):
    """A required deployment variable is not exported in this shell."""

    def __init__(self, name: str) -> None:
        super().__init__(
            code=EXIT_USAGE,
            reason="usage",
            detail=f"{name} is not set",
            fix=ENV_FIX,
        )


class BadVariable(Failure):
    """A variable, or the URL, is set to a value the verb refuses."""

    def __init__(self, detail: str, fix: str = ENV_FIX) -> None:
        super().__init__(code=EXIT_USAGE, reason="usage", detail=detail, fix=fix)


class BotApiError(Exception):
    """A Bot API method did not answer ``ok`` — summarised, never quoted."""


def _http(
    method: str,
    url: str,
    *,
    data: Optional[dict[str, str]] = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, Any]:
    """One HTTP call → ``(status, parsed JSON or None)``. The seam the tests
    script. Redirects are never followed; the URL is never logged (a Bot API
    URL carries the token)."""
    with httpx.Client(follow_redirects=False, timeout=TIMEOUT_S) as http:
        response = http.request(method, url, data=data, headers=headers or {})
    try:
        return response.status_code, response.json() if response.content else None
    except ValueError:
        return response.status_code, None


class Report:
    """The checks a run made, in order, and whether every one passed."""

    def __init__(self, action: str) -> None:
        self.action = action
        self.checks: list[dict[str, str]] = []

    def add(self, check: str, state: str, detail: str) -> None:
        self.checks.append({"check": check, "state": state, "detail": detail})

    @property
    def ok(self) -> bool:
        return all(check["state"] != "failed" for check in self.checks)

    def data(self) -> dict[str, Any]:
        return {"action": self.action, "ok": self.ok, "checks": list(self.checks)}


def exit_code(data: Mapping[str, Any]) -> int:
    return 0 if data.get("ok") else EXIT_API_UNREACHABLE


def _env(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise MissingVariable(name)
    return value


def _optional(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def resolve_url(env: Mapping[str, str], url: Optional[str]) -> str:
    """``--url`` beats the environment beats the default; https only."""
    chosen = (url or _optional(env, WEBHOOK_URL_VAR) or DEFAULT_WEBHOOK_URL).strip()
    if urlsplit(chosen).scheme != "https":
        raise BadVariable(
            f"the webhook URL must be https (got {chosen!r}; --url or {WEBHOOK_URL_VAR})",
            fix="point --url at the API's own https host",
        )
    return chosen


def _call(token: str, method: str, data: Optional[dict[str, str]] = None) -> dict:
    """One Bot API method → its ``ok`` body, or :class:`BotApiError`. The
    failure is summarised by status and `error_code` WITHOUT quoting the
    description — Telegram echoes request details there, and the request
    carried the token."""
    try:
        status, body = _http(
            "POST" if data is not None else "GET",
            f"{BOT_API}/bot{token}/{method}",
            data=data,
        )
    except httpx.TransportError as exc:
        raise BotApiError(f"{method} failed: {type(exc).__name__} reaching the Bot API")
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        code = body.get("error_code") if isinstance(body, dict) else None
        raise BotApiError(
            f"{method} failed: HTTP {status}"
            + (f" (error_code {code})" if code else "")
            + _HINT.get(method, "")
        )
    return body


def _bot(report: Report, token: str, expected: str) -> bool:
    """The bot's identity and whether it is the configured one; False stops
    a registration."""
    me = _call(token, "getMe").get("result") or {}
    username = str(me.get("username") or "")
    report.add("bot", "ok", f"@{username} (id {me.get('id')})")
    if not expected:
        report.add(
            "bot_check",
            "skipped",
            f"skipped — {TELEGRAM_BOT_VAR} is not set in this shell",
        )
        return True
    if bot_matches(username, expected):
        report.add(
            "bot_check", "ok", f"the token is {TELEGRAM_BOT_VAR}'s bot (@{expected})"
        )
        return True
    report.add(
        "bot_check",
        "failed",
        f"wrong bot: the token belongs to @{username}, but {TELEGRAM_BOT_VAR} is"
        f" @{expected} — a webhook on the wrong bot would break whatever that bot serves",
    )
    return False


def _webhook_info(report: Report, token: str, url: str) -> None:
    info = _call(token, "getWebhookInfo").get("result") or {}
    registered = str(info.get("url") or "")
    if not registered:
        report.add(
            "webhook",
            "failed",
            f"NO WEBHOOK is registered for this bot — {REGISTER_HINT}",
        )
        return
    report.add("webhook", "ok", registered)
    report.add("pending", "ok", str(info.get("pending_update_count", 0)))
    if info.get("allowed_updates"):
        report.add(
            "allowed_updates", "ok", ", ".join(map(str, info["allowed_updates"]))
        )
    if info.get("last_error_message"):
        report.add(
            "last_error",
            "failed",
            f"LAST ERROR from Telegram: {info['last_error_message']}",
        )
    if registered != url:
        report.add(
            "url", "failed", f"the registered URL differs from the expected {url}"
        )


def _api_door(report: Report, url: str, secret: str) -> None:
    """A POST with the secret and an empty body: 400 means accepted."""
    if not secret:
        report.add(
            "api_door",
            "failed",
            f"NOT CHECKED — {TELEGRAM_SECRET_VAR} is not set in this shell",
        )
        return
    try:
        status, _ = _http("POST", url, data={}, headers={WEBHOOK_SECRET_HEADER: secret})
    except httpx.TransportError as exc:
        report.add(
            "api_door", "failed", f"could not reach the API: {type(exc).__name__}"
        )
        return
    if status == 400:
        report.add(
            "api_door",
            "ok",
            "API accepts the secret (refused the empty body one step later, as designed)",
        )
    elif status == 403:
        report.add(
            "api_door",
            "failed",
            "the API REFUSES the secret — it does not match the API's"
            f" {TELEGRAM_SECRET_VAR}, or that variable is not set there",
        )
    elif status == 503:
        report.add(
            "api_door",
            "failed",
            f"the API has no ingress wired (503) — {DATABASE_URL_VAR} absent?",
        )
    elif 300 <= status < 400:
        report.add(
            "api_door",
            "failed",
            f"the URL answered a redirect (HTTP {status}); NOT followed — a redirect"
            " would carry the secret elsewhere. Point --url at the API's own host",
        )
    else:
        report.add("api_door", "failed", f"unexpected HTTP {status}")


def _status_checks(
    report: Report, env: Mapping[str, str], token: str, url: str
) -> None:
    _webhook_info(report, token, url)
    _api_door(report, url, _optional(env, TELEGRAM_SECRET_VAR))


def status(env: Mapping[str, str], *, url: Optional[str] = None) -> dict[str, Any]:
    """Who is the bot, is a webhook set, does the API accept the secret."""
    token = _env(env, TELEGRAM_TOKEN_VAR)
    door = resolve_url(env, url)
    report = Report("status")
    try:
        _bot(report, token, _optional(env, TELEGRAM_BOT_VAR).lstrip("@"))
        _status_checks(report, env, token, door)
    except BotApiError as exc:
        report.add("bot_api", "failed", str(exc))
    return report.data()


def register(
    env: Mapping[str, str], *, url: Optional[str] = None, drop_pending: bool = False
) -> dict[str, Any]:
    """setWebhook → the API with the secret, the served update kinds and the
    connection cap; refuses unless the token's bot is the configured one;
    ends with the status checks so the report states what is registered."""
    token = _env(env, TELEGRAM_TOKEN_VAR)
    secret = _env(env, TELEGRAM_SECRET_VAR)
    expected = _env(env, TELEGRAM_BOT_VAR).lstrip("@")
    door = resolve_url(env, url)
    try:
        max_connections = max_connections_from(env.get(MAX_CONNECTIONS_VAR))
    except BadMaxConnections as exc:
        raise BadVariable(str(exc)) from None
    report = Report("register")
    try:
        if not _bot(report, token, expected):
            return report.data()
        body = _call(
            token,
            "setWebhook",
            {
                "url": door,
                "secret_token": secret,
                "allowed_updates": json.dumps(list(ALLOWED_UPDATES)),
                "max_connections": str(max_connections),
                "drop_pending_updates": "true" if drop_pending else "false",
            },
        )
        # `ok` only: the description is Telegram's prose, and this verb's
        # rule is to summarise Bot API answers rather than quote them.
        report.add(
            "setWebhook",
            "ok" if body.get("result") else "failed",
            f"{'ok' if body.get('result') else 'not ok'} → {door}"
            + (" (pending updates dropped)" if drop_pending else ""),
        )
        _status_checks(report, env, token, door)
    except BotApiError as exc:
        report.add("bot_api", "failed", str(exc))
    return report.data()


def deregister(env: Mapping[str, str]) -> dict[str, Any]:
    """deleteWebhook — Telegram delivers nothing until `register` runs again."""
    token = _env(env, TELEGRAM_TOKEN_VAR)
    report = Report("deregister")
    try:
        _call(token, "deleteWebhook", {"drop_pending_updates": "false"})
        report.add(
            "deleteWebhook",
            "ok",
            f"ok — Telegram will deliver nothing until {REGISTER_HINT} runs again",
        )
    except BotApiError as exc:
        report.add("bot_api", "failed", str(exc))
    return report.data()
