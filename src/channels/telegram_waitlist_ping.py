"""The admin's Telegram message for each waitlist signup (`POST /public/waitlist`).

The API sends it with the one bot (`TARGET_TELEGRAM_BOT_TOKEN`) to the chat in
`TARGET_WAITLIST_PING_CHAT_ID`, after the route has answered: a background
task on the API's own process, which runs until the send ends. It is not an
outbox row: the outbox delivers to a workspace's bound chats under that
workspace's tenant, and the admin's chat is no workspace's.

Pings go out one at a time, so a burst of signups never meets Telegram's
limit as a crowd. A lost connection or a timeout is already retried by the
egress floor under the transport; the one retry here is a 429, after the wait
Telegram names (capped), up to :data:`ATTEMPTS` sends. Anything else that is
not a delivered message is one log line naming the cause, Telegram's own
reason when it gave one. The transport keeps the token out of every exception
it raises, and nothing here logs the chat id.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping, Optional

from src.channels.telegram_transport import TelegramPaced, TelegramSendError
from src.services.target.vocabulary import TELEGRAM_TOKEN_VAR

logger = logging.getLogger("channels.waitlist_ping")

#: Sends at most, the first included.
ATTEMPTS = 3
#: The longest wait honoured for a 429 before the next send.
MAX_PACED_WAIT_SECONDS = 30.0

SendText = Callable[[str, str], Awaitable[str]]


def message(address: str, at: datetime) -> str:
    """The text the admin reads, the same words the site used to send."""
    return (
        "New waitlist signup!\n\n"
        f"Email: {address}\n"
        f"Time: {at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )


class WaitlistPing:
    """One send function and the chat it sends to; ``create_app`` builds one
    as ``app.state.waitlist_ping``, or None without a bot or a chat."""

    def __init__(self, send_text: SendText, chat_id: str) -> None:
        self._send_text = send_text
        self._chat_id = chat_id
        self._one_at_a_time = asyncio.Semaphore(1)

    async def __call__(self, address: str) -> None:
        """Tell the admin's chat about *address*. Never raises."""
        text = message(address, datetime.now(timezone.utc))
        async with self._one_at_a_time:
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    await self._send_text(self._chat_id, text)
                    return
                except TelegramPaced as exc:
                    if attempt == ATTEMPTS:
                        logger.error(
                            "waitlist ping: not sent after %d tries: %s", attempt, exc
                        )
                        return
                    await asyncio.sleep(min(exc.retry_after_s, MAX_PACED_WAIT_SECONDS))
                except TelegramSendError as exc:
                    # Telegram's words say what to fix (`chat not found`,
                    # `Unauthorized`); a lost answer was retried below us.
                    logger.error("waitlist ping: not sent: %s", exc)
                    return
                except Exception:  # noqa: BLE001 — a background task has no caller
                    logger.exception("waitlist ping: failed, NO MESSAGE WAS SENT")
                    return


def from_env(env: Mapping[str, str], bot) -> Optional[WaitlistPing]:
    """The ping this process sends, or None, saying once at startup which
    setting is missing, so a signup with no ping is explained in the log."""
    # The literal `env.get("…")` is what the pin on the environment the tree
    # reads finds (`tests/src/test_legacy_settings_gone.py`).
    chat_id = (env.get("TARGET_WAITLIST_PING_CHAT_ID") or "").strip()
    missing = [
        name
        for name, value in (
            (TELEGRAM_TOKEN_VAR, bot),
            ("TARGET_WAITLIST_PING_CHAT_ID", chat_id),
        )
        if not value
    ]
    if missing:
        logger.warning("waitlist ping: off, not set: %s", ", ".join(missing))
        return None
    return WaitlistPing(bot.send_text, chat_id)
