"""Register and check the target bot's Telegram webhook — without a secret
ever reaching a terminal paste (#1224, #1157).

Reads the bot token, the webhook secret and the bot's username from the SAME
variables the deployment uses, so there is one spelling of each and nothing
to retype:

    TARGET_TELEGRAM_BOT_TOKEN             (the worker's; the bot that sends)
    TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN  (the API's; what Telegram must echo)
    TARGET_TELEGRAM_BOT_USERNAME          (the API's; which bot this must be)

    python -m scripts.telegram_webhook status      # who is the bot, is a webhook set,
                                                   # does the API accept the secret
    python -m scripts.telegram_webhook register    # setWebhook → the API, messages only
    python -m scripts.telegram_webhook deregister  # deleteWebhook

Every line this prints is safe to paste: tokens and secrets are read, sent,
and never echoed — a Bot API error body is summarised by status, not quoted,
because Telegram's own error text can carry the token back.

Three guards the secret's safety rests on: the webhook URL must be `https`
(a cleartext door would carry the secret in the clear); redirects are NEVER
followed (the stdlib opener re-sends every header, secret included, to
wherever a 3xx points — so a 3xx is a failed check, not a hop); and `register`
refuses unless the token's bot IS the configured bot — a webhook set on the
wrong bot's token would break whatever that bot is serving.

The `status` check of the API door is the proof the whole set-up wants: a
POST to the webhook URL carrying the secret and an empty body answers **400**
when the secret is accepted (the empty body is refused one step later, as
"malformed body") and **403** when it is not. No session, no real update, no
side effect — and it distinguishes "wrong secret" from "not registered" from
"ingress not wired" (503), which are three different remedies.

Stdlib only, like `fc8_gate.py`, so it runs from a laptop with nothing
installed. Exit codes: 0 everything checked out · 1 a check failed (the
line says which) · 2 usage or a missing variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

BOT_API = "https://api.telegram.org"
DEFAULT_WEBHOOK_URL = "https://api.storydump.app/webhooks/telegram"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
TOKEN_VAR = "TARGET_TELEGRAM_BOT_TOKEN"
SECRET_VAR = "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN"
BOT_VAR = "TARGET_TELEGRAM_BOT_USERNAME"
#: The update kinds the target ingress serves today: `/start` taps ride
#: `message`; nothing else is dispatched yet (#854), so nothing else is asked for.
ALLOWED_UPDATES = ["message"]
TIMEOUT_S = 20


class MissingVariable(Exception):
    """A required deployment variable is not exported in this shell."""


class BotApiError(Exception):
    """A Bot API method did not answer ``ok`` — summarised, never quoted."""


class RedirectRefused(Exception):
    """The door answered a 3xx. Following it would re-send the secret."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow: a redirect is reported as the 3xx it is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects())


def _http(
    method: str,
    url: str,
    *,
    data: Optional[dict[str, str]] = None,
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, Any]:
    """One HTTP call → ``(status, parsed JSON or None)``. The seam the tests
    script; never logs the URL (a Bot API URL carries the token)."""
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingVariable(name)
    return value


def _require_https(url: str) -> str:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise MissingVariable(
            f"an https webhook URL (got {url!r}; --url or TARGET_TELEGRAM_WEBHOOK_URL)"
        )
    return url


#: What to add to a failure line, per method, when it helps the operator.
_HINT = {"getMe": " — is the token the bot's?"}


def _call(token: str, method: str, data: Optional[dict[str, str]] = None) -> dict:
    """One Bot API method → its ``ok`` body, or :class:`BotApiError`.

    The failure is summarised by status and `error_code` WITHOUT quoting the
    description — Telegram echoes request details there, and the request
    carried the token."""
    status, body = _http(
        "POST" if data is not None else "GET",
        f"{BOT_API}/bot{token}/{method}",
        data=data,
    )
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        code = body.get("error_code") if isinstance(body, dict) else None
        raise BotApiError(
            f"{method} failed: HTTP {status}"
            + (f" (error_code {code})" if code else "")
            + _HINT.get(method, "")
        )
    return body


def _bot_username(token: str) -> str:
    me = _call(token, "getMe")["result"]
    username = me.get("username") or ""
    print(f"bot: @{username} (id {me.get('id')})")
    return username


def _bot_matches(username: str, expected: Optional[str]) -> bool:
    """The configured bot, if any, must be the token's bot."""
    if not expected:
        print(f"bot check: skipped — {BOT_VAR} is not set in this shell")
        return True
    if username.casefold() == expected.casefold():
        print(f"bot check: the token is {BOT_VAR}'s bot (@{expected})")
        return True
    print(
        f"bot check: the token belongs to @{username}, but {BOT_VAR} is @{expected}"
        " — a webhook on the wrong bot would break whatever that bot serves"
    )
    return False


def cmd_status(args: argparse.Namespace) -> int:
    token = _env(TOKEN_VAR)
    url = _require_https(args.url)
    secret = os.environ.get(SECRET_VAR, "").strip()
    expected_bot = os.environ.get(BOT_VAR, "").strip().lstrip("@")
    failed = False

    username = _bot_username(token)
    if not _bot_matches(username, expected_bot):
        failed = True

    info = _call(token, "getWebhookInfo")["result"]
    registered = info.get("url") or ""
    if not registered:
        print("webhook: NO WEBHOOK is registered for this bot — run `register`")
        failed = True
    else:
        print(f"webhook: {registered}")
        print(f"  pending: {info.get('pending_update_count', 0)}")
        if info.get("allowed_updates"):
            print(f"  allowed_updates: {info['allowed_updates']}")
        if info.get("last_error_message"):
            print(f"  LAST ERROR from Telegram: {info['last_error_message']}")
            failed = True
        if registered != url:
            print(f"  NOTE: registered URL differs from the expected {url}")
            failed = True

    if not secret:
        print(f"api door: NOT CHECKED — {SECRET_VAR} is not set in this shell")
        return 1
    try:
        status, _ = _http("POST", url, data={}, headers={SECRET_HEADER: secret})
    except RedirectRefused:
        status = -1
    if status == 400:
        print(
            "api door: API accepts the secret (refused the empty body one step"
            " later, as designed)"
        )
    elif status == 403:
        print(
            "api door: the API REFUSES the secret — it does not match the API's"
            f" {SECRET_VAR}, or that variable is not set there"
        )
        failed = True
    elif status == 503:
        print(
            "api door: the API has no ingress wired (503) — TARGET_DATABASE_URL absent?"
        )
        failed = True
    elif 300 <= status < 400:
        print(
            f"api door: the URL answered a redirect (HTTP {status}); NOT followed —"
            " a redirect would carry the secret elsewhere. Point --url at the"
            " API's own host"
        )
        failed = True
    else:
        print(f"api door: unexpected HTTP {status}")
        failed = True
    return 1 if failed else 0


def cmd_register(args: argparse.Namespace) -> int:
    token = _env(TOKEN_VAR)
    secret = _env(SECRET_VAR)
    expected_bot = _env(BOT_VAR).lstrip("@")
    url = _require_https(args.url)
    if not _bot_matches(_bot_username(token), expected_bot):
        return 1
    body = _call(
        token,
        "setWebhook",
        {
            "url": url,
            "secret_token": secret,
            "allowed_updates": json.dumps(ALLOWED_UPDATES),
            "drop_pending_updates": "true" if args.drop_pending else "false",
        },
    )
    # `ok` only: the description is Telegram's prose, and this tool's rule is
    # to summarise Bot API answers rather than quote them.
    print(
        f"setWebhook: {'ok' if body.get('result') else 'not ok'} → {url}"
        + (" (pending updates dropped)" if args.drop_pending else "")
    )
    return cmd_status(args)


def cmd_deregister(args: argparse.Namespace) -> int:
    token = _env(TOKEN_VAR)
    _call(token, "deleteWebhook", {"drop_pending_updates": "false"})
    print(
        "deleteWebhook: ok — Telegram will deliver nothing until `register` runs again"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # `--url` is accepted AFTER the subcommand (`register --url …`), so it is
    # declared on a parent every subcommand inherits rather than on the top
    # level, where argparse would refuse it in that position.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--url",
        default=os.environ.get("TARGET_TELEGRAM_WEBHOOK_URL", DEFAULT_WEBHOOK_URL),
        help=f"the API's webhook door, https only (default {DEFAULT_WEBHOOK_URL})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "status",
        parents=[common],
        help="who is the bot, is a webhook set, does the API accept the secret",
    )
    register = sub.add_parser(
        "register",
        parents=[common],
        help="setWebhook → the API, messages only; refuses unless the token's"
        f" bot is {BOT_VAR}",
    )
    register.add_argument(
        "--drop-pending",
        action="store_true",
        help="discard updates Telegram queued before now (first arming of a new"
        " bot; NOT for re-registering a live one — real taps would be lost)",
    )
    sub.add_parser("deregister", parents=[common], help="deleteWebhook")
    args = parser.parse_args(argv)
    try:
        return {
            "status": cmd_status,
            "register": cmd_register,
            "deregister": cmd_deregister,
        }[args.command](args)
    except MissingVariable as exc:
        print(
            f"{exc} is not set — export it in this shell (never paste it into a chat)"
        )
        return 2
    except BotApiError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
