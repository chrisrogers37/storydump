"""The admin's Telegram message for each waitlist signup (`POST /public/waitlist`).

The API sends it with the one bot (`TARGET_TELEGRAM_BOT_TOKEN`) as a direct
message to each person in `OPS_USER_IDS` whose account is active and who has
linked Telegram, read afresh for each ping before it queues for its turn
(``create_app`` hands it the read, so this module needs no database), since a
person's private chat with the bot has their user id.
Linking began with `/start` in that chat, so the bot may write to it. All of
it happens after the route has answered: a background task on the API's own
process, which runs until the send ends, so neither the read nor Telegram can
cost a signup. It is not an outbox row: the outbox delivers to a
workspace's bound chats under that workspace's tenant, and an operator's
private chat is no workspace's.

Pings go out one at a time, so a burst of signups never meets Telegram's
limit as a crowd, and at most :data:`MAX_WAITING` wait their turn. A lost
connection or a timeout is already retried by the egress floor under the
transport; the one retry here is a 429, after the wait Telegram names
(capped), up to :data:`ATTEMPTS` sends. Anything else that is
not a delivered message is one log line naming the cause, Telegram's own
reason when it gave one. The transport keeps the token out of every exception
it raises, and nothing here logs a chat id or the address.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

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
#: The Telegram ids of the operators who have linked Telegram, read anew for
#: each ping, so a link made after startup counts.
Recipients = Callable[[], Awaitable[list[str]]]


def message(address: str, at: datetime) -> str:
    """The text the admin reads, the same words the site used to send."""
    return (
        "New waitlist signup!\n\n"
        f"Email: {address}\n"
        f"Time: {at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )


class WaitlistPing:
    """One send function and the read of whom it writes to; ``create_app``
    builds one as ``app.state.waitlist_ping``, or None without a bot or an
    operator."""

    def __init__(self, send_text: SendText, recipients: Recipients) -> None:
        self._send_text = send_text
        self._recipients = recipients
        self._one_at_a_time = asyncio.Semaphore(1)
        #: Pings reading their recipients or queued behind the one being sent.
        self._waiting = 0

    async def __call__(self, address: str) -> None:
        """Tell each linked operator about *address*. Never raises."""
        await self.send(message(address, datetime.now(timezone.utc)))

    async def send(self, text: str, *, alert: bool = False) -> None:
        """Send *text* to each linked operator, in turn with every other ping.
        An *alert* is never dropped for a full queue: it is rare (its sender
        throttles it), and it comes when signups have filled the queue.
        Never raises."""
        if not alert and self._waiting >= MAX_WAITING:
            # Telegram is holding us back and the queue is full: one line
            # per dropped ping, so the log counts what was not announced.
            logger.error("waitlist ping: dropped, %d already waiting", self._waiting)
            return
        self._waiting += 1
        queued = True
        try:
            # Read before the turn, so a slow read (a busy pool) never holds
            # the pings queued behind this one.
            chats = await self._recipients()
            if not chats:
                logger.warning(
                    "waitlist ping: not sent, no one in OPS_USER_IDS"
                    " has linked Telegram"
                )
                return
            async with self._one_at_a_time:
                self._waiting -= 1
                queued = False
                for chat in chats:
                    await self._send(chat, text)
        except Exception:  # noqa: BLE001 — a background task has no caller
            logger.exception("waitlist ping: not sent, the operators could not be read")
        finally:
            # Cancelled, unread or no one to tell before its turn: it no
            # longer waits.
            if queued:
                self._waiting -= 1

    async def _send(self, chat: str, text: str) -> None:
        """Send to one chat; whatever stops it is logged, never raised, so the
        next operator still gets theirs."""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                await self._send_text(chat, text)
                return
            except TelegramPaced as exc:
                if attempt == ATTEMPTS:
                    logger.error(
                        "waitlist ping: not sent after %d tries: %s", attempt, exc
                    )
                    return
                await asyncio.sleep(min(exc.retry_after_s, MAX_PACED_WAIT_SECONDS))
            except TelegramSendError as exc:
                # Telegram's words say what to fix (`chat not found`, `bot
                # was blocked by the user`, `Unauthorized`); a lost answer
                # was retried below us.
                logger.error("waitlist ping: not sent: %s", exc)
                return
            except Exception:  # noqa: BLE001 — a background task has no caller
                logger.exception(
                    "waitlist ping: failed, NO MESSAGE WAS SENT to one operator"
                )
                return


def from_settings(
    bot, operators: frozenset[str], recipients: Recipients
) -> Optional[WaitlistPing]:
    """The ping this process sends, or None, saying once at startup which
    setting is missing, so a signup with no ping is explained in the log.
    *recipients* reads the linked Telegram ids of *operators*."""
    missing = [
        name
        for name, value in ((TELEGRAM_TOKEN_VAR, bot), ("OPS_USER_IDS", operators))
        if not value
    ]
    if missing:
        logger.warning("waitlist ping: off, not set: %s", ", ".join(missing))
        return None
    return WaitlistPing(bot.send_text, recipients)
