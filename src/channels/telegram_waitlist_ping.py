"""The admin's Telegram message for each waitlist signup (`POST /public/waitlist`).

The API sends it with the one bot (`TARGET_TELEGRAM_BOT_TOKEN`) to the chat in
`TARGET_WAITLIST_PING_CHAT_ID`, after the route has answered: a background
task on the API's own process, which runs until the send ends. It is not an
outbox row: the outbox delivers to a workspace's bound chats under that
workspace's tenant, and the admin's chat is no workspace's.

Pings go out one at a time, so a burst of signups never meets Telegram's
limit as a crowd, and at most :data:`MAX_WAITING` wait their turn. A lost connection or a timeout is already retried by the
egress floor under the transport; the one retry here is a 429, after the wait
Telegram names (capped), up to :data:`ATTEMPTS` sends. Anything else that is
not a delivered message is one log line naming the cause, Telegram's own
reason when it gave one. The transport keeps the token out of every exception
it raises, and nothing here logs the chat id or the address.

The chat should be one the bot only posts to: a channel with the bot as an
admin, or a group with the bot's privacy mode on. The bot's webhook serves
updates from every chat it is in (`telegram_dispatch.py`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping, Optional

from src.channels.telegram_transport import TelegramPaced, TelegramSendError
from src.services.target.vocabulary import TELEGRAM_TOKEN_VAR

logger = logging.getLogger(__name__)

#: Sends at most, the first included.
ATTEMPTS = 3
#: The longest wait honoured for a 429 before the next send.
MAX_PACED_WAIT_SECONDS = 30.0
#: Pings that may wait behind the one being sent. Past it a ping is dropped
#: and logged: under a long 429, a deeper queue would only deliver hours late
#: and hold the API's shutdown on tasks still waiting.
MAX_WAITING = 20

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
        #: Pings queued behind the one being sent.
        self._waiting = 0

    async def __call__(self, address: str) -> None:
        """Tell the admin's chat about *address*. Never raises."""
        if self._waiting >= MAX_WAITING:
            # Telegram is holding us back and the queue is full: one line
            # per dropped ping, so the log counts what was not announced.
            logger.error("waitlist ping: dropped, %d already waiting", self._waiting)
            return
        text = message(address, datetime.now(timezone.utc))
        self._waiting += 1
        try:
            async with self._one_at_a_time:
                self._waiting -= 1
                await self._send(text)
        except Exception:  # noqa: BLE001 — a background task has no caller
            logger.exception("waitlist ping: failed, NO MESSAGE WAS SENT")

    async def _send(self, text: str) -> None:
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
