"""Registering the target bot's webhook — the ONE spelling of what is asked for.

Telegram delivers only the update kinds a registration asks for, so the
registration is part of the product's correctness, not an operations nicety:
with `message` alone every button tap was dropped before it reached the route
(phase 1 of the 2026-09-09 tap plan, its first blocker). Two callers share
this module so they cannot disagree — the API registers itself at startup
(`src/api/app.py`, idempotent on every deploy: the API holds the token and the
secret already, and a human step that has to follow every deploy is a step
that will be missed) and `storydump webhook status|register|deregister` is the operator's
verify / register / deregister tool (the v2 CLI, phase 03).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The spellings live in the vocabulary module — the ONE `src` module the CLI
# imports — so the API's self-registration and `storydump webhook` cannot
# disagree; they are re-exported here for the callers that import them.
from src.services.target.vocabulary import (  # noqa: E402
    ALLOWED_UPDATES,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_WEBHOOK_URL,
    MAX_CONNECTIONS_VAR,
    BadMaxConnections,
    bot_matches,
    max_connections_from,
)
from src.services.target.vocabulary import WEBHOOK_URL_VAR as URL_VAR  # noqa: E402
from src.services.target.vocabulary import (  # noqa: E402
    RAILWAY_ENVIRONMENT_VAR as ENVIRONMENT_VAR,
)
from src.services.target.vocabulary import PRODUCTION_ENVIRONMENT  # noqa: E402
from src.services.target.vocabulary import TELEGRAM_BOT_VAR as BOT_VAR  # noqa: E402
from src.services.target.vocabulary import TELEGRAM_SECRET_VAR as SECRET_VAR  # noqa: E402
from src.services.target.vocabulary import TELEGRAM_TOKEN_VAR as TOKEN_VAR  # noqa: E402

__all__ = [
    "ALLOWED_UPDATES",
    "AUTOREGISTER_VAR",
    "BOT_VAR",
    "SECRET_VAR",
    "TOKEN_VAR",
    "DEFAULT_MAX_CONNECTIONS",
    "DEFAULT_WEBHOOK_URL",
    "ENVIRONMENT_VAR",
    "MAX_CONNECTIONS_VAR",
    "OFF_WORDS",
    "ON_WORDS",
    "PRODUCTION_ENVIRONMENT",
    "URL_VAR",
    "BadMaxConnections",
    "autoregister_enabled",
    "bot_matches",
    "max_connections_from",
    "register",
]

#: The API's startup registration is ON in Railway's production environment
#: and OFF anywhere else unless this says `1` — any second process holding
#: the production token (a laptop, a tunnel, a preview) must never re-point
#: production's webhook at itself with its own secret. `0`/`false`/`no`
#: switches it off in production too (an operator driving the CLI by hand).
AUTOREGISTER_VAR = "TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER"

#: An explicit "no". The API's skip reason names WHICH no it was, so the
#: words live here rather than being re-derived by the caller (#1325, TD-C7).
OFF_WORDS: tuple[str, ...] = ("0", "false", "no", "off")
ON_WORDS: tuple[str, ...] = ("1", "true", "yes", "on")


def autoregister_enabled(raw: Optional[str], *, environment: Optional[str]) -> bool:
    """Explicit `1` → on; explicit `0`/`false`/`no`/`off` → off; unset → on only
    in Railway's `production` environment (the one deployment that owns the
    bot's webhook)."""
    value = (raw or "").strip().lower()
    if value in OFF_WORDS:
        return False
    if value in ON_WORDS:
        return True
    return (environment or "").strip().lower() == PRODUCTION_ENVIRONMENT


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
        prose = transport.redact(str(exc))
        if secret:
            prose = prose.replace(secret, "<SECRET>")
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
