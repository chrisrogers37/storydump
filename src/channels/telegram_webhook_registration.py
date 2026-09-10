"""Registering the target bot's webhook — the ONE spelling of what is asked for.

Telegram delivers only the update kinds a registration asks for, so the
registration is part of the product's correctness, not an operations nicety:
with `message` alone every button tap was dropped before it reached the route
(phase 1 of the 2026-09-09 tap plan, its first blocker). Two callers share
this module so they cannot disagree — the API registers itself at startup
(`src/api/app.py`, idempotent on every deploy: the API holds the token and the
secret already, and a human step that has to follow every deploy is a step
that will be missed) and `scripts/telegram_webhook.py` is the operator's
verify / register / deregister tool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_WEBHOOK_URL = "https://api.storydump.app/webhooks/telegram"
URL_VAR = "TARGET_TELEGRAM_WEBHOOK_URL"
#: The update kinds the target ingress serves: `/start` taps and group messages
#: ride `message`; a button tap on an approval card is a `callback_query`.
#: Typed chat commands (#854) are still not dispatched.
ALLOWED_UPDATES = ["message", "callback_query"]
#: `setWebhook`'s `max_connections`: how many simultaneous deliveries Telegram
#: opens against the route — its true concurrency ceiling (default 40, at most
#: 100). Set deliberately to the ingress's connection budget: one process ×
#: `POOL_SIZE_SEAM` (10) today; 20 if the API runs two workers (F5).
MAX_CONNECTIONS_VAR = "TARGET_TELEGRAM_WEBHOOK_MAX_CONNECTIONS"
DEFAULT_MAX_CONNECTIONS = 10
#: The API's startup registration is ON in Railway's production environment
#: and OFF anywhere else unless this says `1` — any second process holding
#: the production token (a laptop, a tunnel, a preview) must never re-point
#: production's webhook at itself with its own secret. `0`/`false`/`no`
#: switches it off in production too (an operator driving the script by hand).
AUTOREGISTER_VAR = "TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER"
ENVIRONMENT_VAR = "RAILWAY_ENVIRONMENT_NAME"


class BadMaxConnections(ValueError):
    """The connection cap is outside Telegram's 1..100."""


def max_connections_from(raw: Optional[str]) -> int:
    """The connection cap, from an environment value, within Telegram's 1..100."""
    if raw is None or not raw.strip():
        return DEFAULT_MAX_CONNECTIONS
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if not 1 <= value <= 100:
        raise BadMaxConnections(
            f"{MAX_CONNECTIONS_VAR} must be an integer from 1 to 100 (got {raw!r})"
        )
    return value


def autoregister_enabled(raw: Optional[str], *, environment: Optional[str]) -> bool:
    """Explicit `1` → on; explicit `0`/`false`/`no`/`off` → off; unset → on only
    in Railway's `production` environment (the one deployment that owns the
    bot's webhook)."""
    value = (raw or "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    return (environment or "").strip().lower() == "production"


def bot_matches(username: str, expected: Optional[str]) -> bool:
    """The configured bot, if any, must be the token's bot — registering the
    door on the wrong bot is the one mistake this refuses by construction."""
    if not expected:
        return True
    return username.lstrip("@").lower() == expected.lstrip("@").lower()


async def register(
    transport,
    *,
    url: str,
    secret: str,
    expected_bot: Optional[str],
    max_connections: int,
) -> dict[str, Any]:
    """Register the door on the bot *transport* speaks for, then read back
    what Telegram holds. Returns a report `/health` can show — never the token
    or the secret. Never raises: a failure is a report with ``ok: False``.

    Order: `getMe` (the bot must be the configured one), `setWebhook` with the
    URL, the secret, the served update kinds and the connection cap, keeping
    the pending backlog, then `getWebhookInfo` so the report states what is
    registered rather than what was asked for.
    """
    report: dict[str, Any] = {
        "ok": False,
        "sampled": "startup",  # a snapshot from this process's start, not live
        "url": url,
        "asked_updates": list(ALLOWED_UPDATES),
        "max_connections": max_connections,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        username = await transport.probe()
        report["bot"] = username
        if not bot_matches(username, expected_bot):
            report["error"] = (
                f"token belongs to @{username}, not the configured @{expected_bot}"
            )
            logger.error("telegram webhook not registered: %s", report["error"])
            return report
        await transport.set_webhook(
            url=url,
            secret_token=secret,
            allowed_updates=list(ALLOWED_UPDATES),
            max_connections=max_connections,
        )
        info = await transport.webhook_info()
        report["registered_url"] = info.get("url")
        report["allowed_updates"] = info.get("allowed_updates")
        report["pending_update_count"] = info.get("pending_update_count")
        report["telegram_max_connections"] = info.get("max_connections")
        report["ok"] = info.get("url") == url
        if not report["ok"]:
            report["error"] = "getWebhookInfo reports a different URL"
    except Exception as exc:  # noqa: BLE001 — a report, never a failed startup
        # `/health` is unauthenticated: it gets the exception's TYPE only. The
        # prose (Telegram's, or httpx's, which may embed the URL) goes to the
        # log with the token AND the secret struck out.
        report["error"] = type(exc).__name__
        prose = transport.redact(str(exc)).replace(secret, "<SECRET>")
        logger.warning(
            "telegram webhook not registered: %s: %s", type(exc).__name__, prose
        )
        return report
    logger.info(
        "telegram webhook registered on @%s → %s (updates=%s, max_connections=%s,"
        " pending=%s)",
        report.get("bot"),
        url,
        report.get("allowed_updates"),
        report.get("telegram_max_connections"),
        report.get("pending_update_count"),
    )
    return report
