"""The admin's Telegram message for each waitlist signup (`POST /public/waitlist`).

The API sends it with the one bot (`TARGET_TELEGRAM_BOT_TOKEN`) to the chat in
`TARGET_WAITLIST_PING_CHAT_ID`, after the route has answered: a background
task on the API's own process, which runs until the send ends. It is not an
outbox row: the outbox delivers to a workspace's bound chats under that
workspace's tenant, and the admin's chat is no workspace's.

So the retry is here, bounded: a 429 waits the `retry_after` Telegram names
(capped) and a send that got no definite answer waits a few seconds, up to
:data:`ATTEMPTS` sends; a refusal that will not change (a gone chat, a dead
token, a bad request) is not retried. A send with no definite answer may have
landed, so a retry can repeat a message; a repeated ping is harmless.

Every outcome other than a delivered message is one log line naming the cause.
The transport keeps the token out of every exception it raises, and nothing
here logs the chat id.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping, Optional

from src.services.target.vocabulary import TELEGRAM_TOKEN_VAR
from src.channels.telegram_transport import (
    TelegramAuthDead,
    TelegramChatGone,
    TelegramPaced,
    TelegramRefused,
    TelegramSendError,
)

logger = logging.getLogger("channels.waitlist_ping")

#: The chat the ping goes to, prefixed like the bot's own variables (the
#: unprefixed admin-chat name belonged to the retired legacy tier).
CHAT_VAR = "TARGET_WAITLIST_PING_CHAT_ID"
#: Sends at most, the first included.
ATTEMPTS = 3
#: The longest wait honoured for a 429 before the next send.
MAX_PACED_WAIT_SECONDS = 30.0
#: The wait after a send that got no definite answer, doubled each time.
RETRY_WAIT_SECONDS = 2.0

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

    async def __call__(self, address: str) -> None:
        """Tell the admin's chat about *address*. Never raises."""
        text = message(address, datetime.now(timezone.utc))
        wait = RETRY_WAIT_SECONDS
        for attempt in range(1, ATTEMPTS + 1):
            last = attempt == ATTEMPTS
            try:
                await self._send_text(self._chat_id, text)
                return
            except (TelegramChatGone, TelegramAuthDead, TelegramRefused) as exc:
                # Telegram's own words say what to fix (`chat not found`,
                # `Unauthorized`), and sending again would get the same.
                logger.error("waitlist ping: refused, not retried: %s", exc)
                return
            except TelegramPaced as exc:
                if last:
                    logger.error(
                        "waitlist ping: not sent after %d tries: %s", attempt, exc
                    )
                    return
                await asyncio.sleep(min(exc.retry_after_s, MAX_PACED_WAIT_SECONDS))
            except TelegramSendError as exc:  # no definite answer: a timeout, a 5xx
                if last:
                    logger.error(
                        "waitlist ping: not sent after %d tries: %s", attempt, exc
                    )
                    return
                await asyncio.sleep(wait)
                wait *= 2
            except Exception:  # noqa: BLE001 — a background task has no caller to raise to
                logger.exception("waitlist ping: failed, NO MESSAGE WAS SENT")
                return


def from_env(env: Mapping[str, str], bot) -> Optional[WaitlistPing]:
    """The ping this process sends, or None, saying once at startup which
    setting is missing, so a signup with no ping is explained in the log."""
    chat_id = (env.get("TARGET_WAITLIST_PING_CHAT_ID") or "").strip()
    missing = [
        name
        for name, value in ((TELEGRAM_TOKEN_VAR, bot), (CHAT_VAR, chat_id))
        if not value
    ]
    if missing:
        logger.warning("waitlist ping: off, not set: %s", ", ".join(missing))
        return None
    return WaitlistPing(bot.send_text, chat_id)
