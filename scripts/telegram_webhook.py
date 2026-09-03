"""Register and check the target bot's Telegram webhook — without a secret
ever reaching a terminal paste (#1224, #1157).

Reads the bot token and the webhook secret from the SAME variables the
deployment uses, so there is one spelling of each and nothing to retype:

    TARGET_TELEGRAM_BOT_TOKEN             (the worker's; the bot that sends)
    TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN  (the API's; what Telegram must echo)

    python -m scripts.telegram_webhook status      # who is the bot, is a webhook set,
                                                   # does the API accept the secret
    python -m scripts.telegram_webhook register    # setWebhook → the API, messages only
    python -m scripts.telegram_webhook deregister  # deleteWebhook

Every line this prints is safe to paste: tokens and secrets are read, sent,
and never echoed — a Bot API error body is summarised by status, not quoted,
because Telegram's own error text can carry the token back.

The `status` check of the API door is the proof the whole set-up wants: a
POST to the webhook URL carrying the secret and an empty body answers
**400 "missing update_id"** when the secret is accepted (the body is refused
one step later) and **403** when it is not. No session, no real update, no
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
#: The update kinds the target ingress serves today: `/start` taps ride
#: `message`; nothing else is dispatched yet (#854), so nothing else is asked for.
ALLOWED_UPDATES = ["message"]
TIMEOUT_S = 20


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
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


class MissingVariable(Exception):
    """A required deployment variable is not exported in this shell."""


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingVariable(name)
    return value


def _bot(
    token: str, method: str, data: Optional[dict[str, str]] = None
) -> tuple[int, Any]:
    return _http(
        "POST" if data is not None else "GET",
        f"{BOT_API}/bot{token}/{method}",
        data=data,
    )


def _bot_error(method: str, status: int, body: Any) -> str:
    """Summarise a Bot API failure WITHOUT quoting its description — Telegram
    echoes request details there, and the request carried the token."""
    code = body.get("error_code") if isinstance(body, dict) else None
    return f"{method} failed: HTTP {status}" + (f" (error_code {code})" if code else "")


def cmd_status(args: argparse.Namespace) -> int:
    token = _env(TOKEN_VAR)
    secret = os.environ.get(SECRET_VAR, "").strip()
    failed = False

    status, body = _bot(token, "getMe")
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        print(_bot_error("getMe", status, body) + " — is the token the bot's?")
        return 1
    me = body["result"]
    print(f"bot: @{me.get('username')} (id {me.get('id')})")

    status, body = _bot(token, "getWebhookInfo")
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        print(_bot_error("getWebhookInfo", status, body))
        return 1
    info = body["result"]
    url = info.get("url") or ""
    if not url:
        print("webhook: NO WEBHOOK is registered for this bot — run `register`")
        failed = True
    else:
        print(f"webhook: {url}")
        print(f"  pending: {info.get('pending_update_count', 0)}")
        if info.get("allowed_updates"):
            print(f"  allowed_updates: {info['allowed_updates']}")
        if info.get("last_error_message"):
            print(f"  LAST ERROR from Telegram: {info['last_error_message']}")
            failed = True
        if url != args.url:
            print(f"  NOTE: registered URL differs from the expected {args.url}")
            failed = True

    if not secret:
        print(f"api door: skipped — {SECRET_VAR} is not set in this shell")
        return 1 if failed else 0
    status, body = _http(
        "POST",
        args.url,
        data={},
        headers={SECRET_HEADER: secret},
    )
    if status == 400:
        print(
            "api door: API accepts the secret (refused the empty body one step later, as designed)"
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
    else:
        print(f"api door: unexpected HTTP {status}")
        failed = True
    return 1 if failed else 0


def cmd_register(args: argparse.Namespace) -> int:
    token = _env(TOKEN_VAR)
    secret = _env(SECRET_VAR)
    status, body = _bot(
        token,
        "setWebhook",
        {
            "url": args.url,
            "secret_token": secret,
            "allowed_updates": json.dumps(ALLOWED_UPDATES),
            "drop_pending_updates": "true",
        },
    )
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        print(_bot_error("setWebhook", status, body))
        return 1
    print(f"setWebhook: {body.get('description') or 'ok'} → {args.url}")
    return cmd_status(args)


def cmd_deregister(args: argparse.Namespace) -> int:
    token = _env(TOKEN_VAR)
    status, body = _bot(token, "deleteWebhook", {"drop_pending_updates": "false"})
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        print(_bot_error("deleteWebhook", status, body))
        return 1
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
        help=f"the API's webhook door (default {DEFAULT_WEBHOOK_URL})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "status",
        parents=[common],
        help="who is the bot, is a webhook set, does the API accept the secret",
    )
    sub.add_parser(
        "register",
        parents=[common],
        help="setWebhook → the API, messages only, pending updates dropped",
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


if __name__ == "__main__":
    sys.exit(main())
