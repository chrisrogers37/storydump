"""Registering the target bot's webhook — the ONE spelling of what is asked for.

Telegram delivers only the update kinds a registration asks for, so the
registration is part of the product's correctness, not an operations nicety:
with `message` alone every button tap was dropped before it reached the route
(phase 1 of the 2026-09-09 tap plan, its first blocker). Two callers share
this module so they cannot disagree — the API registers itself at startup
(idempotent on every deploy: the API holds the token and the secret already,
and a human step that has to follow every deploy is a step that will be missed)
and `storydump webhook status|register|deregister` is the operator's
verify / register / deregister tool (the v2 CLI, phase 03).

The API's startup pair lives here rather than in `src/api/app.py`
(#1335, TD-C11): `register_at_startup` and `live_samples` are Telegram channel
logic, they read the same variables and the same off-words `autoregister_enabled`
owns, and the composition root's job is to run them and cache what they say.
Neither touches `app.state` — the report is a return value and the samples are
yielded, so the API keeps its own wiring and this module keeps no view of it.
The bot transport is passed in for the same reason: which Telegram a process
speaks to is the composition root's decision (`src/api/app.py`'s
`_telegram_transport`), and passing it is also the seam the factory tests
substitute a fake bot through.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Mapping, Optional

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
    "live_samples",
    "max_connections_from",
    "register",
    "register_at_startup",
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


async def register_at_startup(
    env: Mapping[str, str], *, transport_factory: Callable[[Mapping[str, str]], Any]
) -> dict[str, Any]:
    """Register the bot's webhook on the API this process serves — idempotent,
    on every deploy — and return the report `/health` caches. Never raises.

    A human step that must follow every deploy is a step that will be missed
    (the tap's first blocker was exactly that: a registration asking for
    `message` updates only). Skipped, with the reason in the report, when the
    token or the secret is absent, or when `AUTOREGISTER_VAR` switches it off —
    and the reason names WHICH no it was, from `OFF_WORDS` rather than a second
    spelling of them.

    *transport_factory* builds the bot transport from *env*; the transport is
    closed here whatever happens, because nothing else holds it.
    """
    token = env.get(TOKEN_VAR)
    secret = env.get(SECRET_VAR)
    if not autoregister_enabled(
        env.get(AUTOREGISTER_VAR), environment=env.get(ENVIRONMENT_VAR)
    ):
        switched_off = (env.get(AUTOREGISTER_VAR) or "").strip().lower() in OFF_WORDS
        return {
            "ok": False,
            "skipped": (
                f"autoregister switched off ({AUTOREGISTER_VAR})"
                if switched_off
                else "autoregister off (not the production environment)"
            ),
        }
    if not token or not secret:
        return {"ok": False, "skipped": "bot token or webhook secret not set"}
    transport = None
    try:
        max_connections = max_connections_from(env.get(MAX_CONNECTIONS_VAR))
        transport = transport_factory(env)
        return await register(
            transport,
            url=env.get(URL_VAR) or DEFAULT_WEBHOOK_URL,
            secret=secret,
            expected_bot=env.get(BOT_VAR),
            max_connections=max_connections,
        )
    except BadMaxConnections as exc:
        # Names the variable and the value, never a secret.
        logger.error("telegram webhook not registered: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — diagnostic; never fails startup
        logger.warning(
            "telegram webhook not registered at startup: %s", type(exc).__name__
        )
        return {"ok": False, "error": type(exc).__name__}
    finally:
        if transport is not None:
            try:
                await transport.aclose()
            except Exception:  # noqa: BLE001
                pass


async def live_samples(
    env: Mapping[str, str],
    *,
    transport_factory: Callable[[Mapping[str, str]], Any],
    interval: float,
) -> AsyncIterator[dict[str, Any]]:
    """What Telegram holds for the bot's webhook RIGHT NOW, every *interval*
    seconds — `getWebhookInfo`'s backlog and last delivery error. Read-only, so
    it runs wherever a token exists; a failure is a sample with an `error`,
    never a raise, so the loop cannot die on a bad minute.

    This is the signal that tells "Telegram is not delivering" from "our route
    is failing": the former shows as a growing backlog with no error, the latter
    as `last_error_message` naming our response code.

    Yields forever; the caller's task is cancelled at shutdown and the transport
    is closed on the way out. With no token it yields nothing at all — the
    caller's cached sample stays as it was.
    """
    if not env.get(TOKEN_VAR):
        return
    transport = transport_factory(env)
    try:
        while True:
            try:
                info = await transport.webhook_info()
                sample = {
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "url": info.get("url"),
                    "pending_update_count": info.get("pending_update_count"),
                    "last_error_date": info.get("last_error_date"),
                    "last_error_message": info.get("last_error_message"),
                    "max_connections": info.get("max_connections"),
                    "allowed_updates": info.get("allowed_updates"),
                }
            except Exception as exc:  # noqa: BLE001 — a report, never a failed loop
                sample = {
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "error": type(exc).__name__,
                }
            yield sample
            await asyncio.sleep(interval)
    finally:
        try:
            await transport.aclose()
        except Exception:  # noqa: BLE001
            pass
