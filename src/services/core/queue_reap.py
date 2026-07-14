"""Shared reap behavior for queue rows already sent to Telegram.

A button-bearing queue row — one that carries a ``telegram_message_id`` and
live inline buttons — must not be hard-deleted by the age-based reapers.
Deleting it orphans the buttons, and a later tap surfaces the scary
"Queue item not found" error with no history to fall back on.

``expire_sent_row`` performs, at reap time, the same edit the click handlers
perform: it rewrites the card to an "Expired" caption, strips the buttons,
and writes a terminal ``expired`` posting_history row (audit trail + tap-time
fallback). Both age-based reap paths — the scheduler's processing sweep and
the hourly queue-cleanup loop — call this one helper so their behavior can
never drift, and a future claim-lease reaper can reuse it too.
"""

from __future__ import annotations

from datetime import datetime, timezone

from telegram import InlineKeyboardMarkup

from src.repositories.history_repository import HistoryCreateParams
from src.services.core.telegram_utils import EXPIRED_CAPTION
from src.utils.logger import logger
from src.utils.resilience import telegram_edit_with_retry


async def expire_sent_row(row, *, bot, history_repo, queue_repo) -> str:
    """Reap a queue row already sent to Telegram (has ``telegram_message_id``).

    C-edit the card to Expired + strip buttons, write a terminal ``expired``
    history row (idempotent), then delete the queue row.

    Returns ``"reaped"`` (handled + deleted) or ``"deferred"`` (a transient
    edit failure left the card possibly still live and tappable, so the row
    is left intact for the next sweep — never orphaned).
    """
    row_id = str(row.id)

    # One best-effort edit that BOTH sets the caption AND strips the buttons.
    try:
        edited = await telegram_edit_with_retry(
            bot.edit_message_caption,
            chat_id=row.telegram_chat_id,
            message_id=row.telegram_message_id,
            caption=EXPIRED_CAPTION,
            reply_markup=InlineKeyboardMarkup([]),
        )
    except Exception as edit_error:  # noqa: BLE001 — classify via the wrapper's contract
        # Non-retryable (message not found / too old / forbidden): there is
        # nothing editable left to orphan, so proceed to record + delete.
        logger.info(
            f"Expire reap: card for {row_id[:8]} not editable "
            f"({type(edit_error).__name__}); recording expiry and deleting"
        )
    else:
        if edited is None:
            # Transient failure exhausted retries (RetryAfter / TimedOut /
            # NetworkError). The card may still be live — defer so its
            # buttons stay tappable and the next sweep can try again.
            logger.warning(
                f"Expire reap: deferring {row_id[:8]} (transient edit failure); "
                f"row left tappable for next sweep"
            )
            return "deferred"

    # PROCEED: idempotent terminal history write, then delete the queue row.
    now = datetime.now(timezone.utc)
    if not history_repo.get_by_queue_item_id(row_id):
        history_repo.create(
            HistoryCreateParams(
                media_item_id=str(row.media_item_id),
                queue_item_id=row_id,
                queue_created_at=row.created_at,
                queue_deleted_at=now,
                scheduled_for=row.scheduled_for,
                posted_at=now,
                status="expired",
                success=False,
                posting_method="system_expiry",
                posted_by_user_id=None,
                chat_settings_id=str(row.chat_settings_id)
                if row.chat_settings_id
                else None,
            )
        )

    queue_repo.delete(row_id)
    logger.info(f"Expire reap: recorded expiry + removed queue item {row_id[:8]}")
    return "reaped"
