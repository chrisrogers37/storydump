"""The operators' Telegram message for what only an operator can act on.

The worker sends it with the one bot (`TARGET_TELEGRAM_BOT_TOKEN`) as a direct
message to each person in `OPS_USER_IDS` whose account is active and who has
linked Telegram, read afresh for each notice, as the API's waitlist ping does
(`telegram_waitlist_ping`). It is not an outbox row: the outbox delivers to a
workspace's bound chats under that workspace's tenant, and an operator's
private chat is no workspace's. `compose` hands this module the read of whom
it writes to and the latch, so it needs no database.

A notice goes out at most once per key and window: the latch is a
`rate_counters` row in its own scope (:data:`SCOPE`), claimed and committed
before any send, so a burst of one kind of notice reaches the operators once.
Nothing is claimed while no operator has linked Telegram, so the first notice
after someone links still goes. A send that fails is one log line: that
window's notice is lost, and the next window's goes. It never raises, and
nothing here logs a chat id.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from src.channels.telegram_transport import TelegramSendError

logger = logging.getLogger(__name__)

#: The `rate_counters` scope of the latch (`machinery.py`, `ck_rate_scope`).
SCOPE = "ops_notice"

#: The transport's `send_text(chat_id, text, *, reply_markup=None)`.
SendText = Callable[..., Awaitable[str]]
#: The Telegram ids of the operators who have linked Telegram.
Recipients = Callable[[], Awaitable[list[str]]]
#: Claims a key's latch for the current window: True for the first claim.
Claim = Callable[[str], Awaitable[bool]]


class OpsNotice:
    """One send function, the read of whom it writes to, and the latch;
    `compose` builds one as `WorkerDeps.ops_notice`, or None without a bot or
    an operator."""

    def __init__(self, send_text: SendText, recipients: Recipients, claim: Claim):
        self._send_text = send_text
        self._recipients = recipients
        self._claim = claim

    async def __call__(self, key: str, text: str) -> bool:
        """Tell each linked operator *text*, unless a notice with *key* went
        out this window. True when it was sent to anyone. Never raises."""
        try:
            chats = await self._recipients()
            if not chats:
                logger.warning(
                    "ops notice %s: not sent, no one in OPS_USER_IDS has linked"
                    " Telegram",
                    key,
                )
                return False
            if not await self._claim(key):
                logger.info("ops notice %s: already sent this window", key)
                return False
        except Exception:  # noqa: BLE001 — a notice has no caller to raise to
            logger.exception(
                "ops notice %s: NOT SENT, the operators or the latch could not be read",
                key,
            )
            return False
        sent = 0
        for chat in chats:
            try:
                await self._send_text(chat, text)
                sent += 1
            except TelegramSendError as exc:
                # Telegram's words say what to fix (`chat not found`, `bot was
                # blocked by the user`); a lost answer was retried below us.
                logger.error("ops notice %s: not sent to one operator: %s", key, exc)
            except Exception:  # noqa: BLE001 — the next operator still gets theirs
                logger.exception(
                    "ops notice %s: failed, NO MESSAGE WAS SENT to one operator", key
                )
        return sent > 0
