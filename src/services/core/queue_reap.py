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

Rows WITHOUT a ``telegram_message_id`` are not safe to hard-delete either: a
timed-out send can deliver a card without the stamp ever landing (#679/#680),
and the two cases are indistinguishable from the row alone. So every age-based
deletion of an unstamped row goes through ``record_expiry_and_delete`` (#687),
which writes the same terminal history row first — a spare row for a
genuinely-never-sent item is harmless (no card exists to tap), while an
orphaned-but-delivered card degrades to the graceful "Expired" caption.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from src.repositories.history_repository import (
    HistoryCreateParams,
    HistoryRepository,
)
from src.repositories.queue_repository import QueueRepository
from src.services.core.telegram_utils import EXPIRED_CAPTION
from src.utils.logger import logger
from src.repositories.tenant_scope import SYSTEM_SCOPE


async def expire_sent_row(row, *, bot, history_repo, queue_repo) -> str:
    """Reap a queue row already sent to Telegram (has ``telegram_message_id``).

    C-edit the card to Expired + strip buttons, write a terminal ``expired``
    history row (idempotent), then delete the queue row.

    Returns ``"reaped"`` (handled + deleted), ``"deferred"`` (a transient
    edit failure left the card possibly still live and tappable, so the row
    is left intact for the next sweep — never orphaned), or ``"failed"``
    (recording/deleting hit a DB error; logged, session rolled back, row
    left for the next sweep).
    """
    row_id = str(row.id)

    # One best-effort edit that BOTH sets the caption AND strips the buttons.
    # Single attempt, classified at this seam: the sweep cadence is the retry
    # loop, and backoff sleeps here feed the very flood window they wait out.
    try:
        await bot.edit_message_caption(
            chat_id=row.telegram_chat_id,
            message_id=row.telegram_message_id,
            caption=EXPIRED_CAPTION,
            reply_markup=InlineKeyboardMarkup([]),
        )
    except BadRequest as edit_error:
        # Permanent rejection — most commonly "Message is not modified": the
        # card already shows Expired with its buttons stripped, so there is
        # nothing left to orphan. Proceed to record + delete. BadRequest
        # subclasses NetworkError, so it must be classified before the
        # transient branch.
        logger.info(
            f"Expire reap: card for {row_id[:8]} already terminal or not "
            f"editable ({edit_error}); recording expiry and deleting"
        )
    except (RetryAfter, TimedOut, NetworkError) as edit_error:
        # Genuinely transient: the card may still be live — defer so its
        # buttons stay tappable and the next sweep can try again.
        logger.warning(
            f"Expire reap: deferring {row_id[:8]} "
            f"({type(edit_error).__name__}); row left tappable for next sweep"
        )
        return "deferred"
    except Exception as edit_error:  # noqa: BLE001 — e.g. Forbidden: bot removed
        # Nothing editable left to orphan — proceed to record + delete.
        logger.info(
            f"Expire reap: card for {row_id[:8]} not editable "
            f"({type(edit_error).__name__}); recording expiry and deleting"
        )

    # PROCEED: idempotent terminal history write, then delete the queue row.
    if not record_expiry_and_delete(
        row, history_repo=history_repo, queue_repo=queue_repo
    ):
        return "failed"
    return "reaped"


def record_expiry_and_delete(row, *, history_repo, queue_repo) -> bool:
    """Write the terminal ``expired`` history row (idempotent), then delete
    the queue row.

    The shared final step of every reap deletion: sent rows reach it via
    ``expire_sent_row`` after their card edit, unstamped rows directly
    (#687 — see the module docstring for why they are not safe to
    hard-delete). The delete only ever runs after the history write
    succeeds — never the other way around.

    Contained: callers run this mid-sweep over many rows, so a DB failure
    here (e.g. a constraint rejecting the write) must not raise and abort
    the rest of the pass — and the failed flush leaves the session dirty,
    poisoning the pass's remaining DB work unless rolled back. Returns True
    when the row was recorded + deleted, False on a contained DB failure
    (row left for the next sweep).
    """
    row_id = str(row.id)
    try:
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
                ),
            )
        queue_repo.delete(row_id)
    except SQLAlchemyError:
        # DB failures only — a programming error (malformed row, bad params)
        # must propagate loudly instead of masquerading as a per-row retry.
        logger.error(
            f"Expire reap: failed to record + delete {row_id[:8]}; "
            f"row left for next sweep",
            exc_info=True,
        )
        history_repo.rollback()
        queue_repo.rollback()
        return False

    logger.info(f"Expire reap: recorded expiry + removed queue item {row_id[:8]}")
    return True


async def reap_pending_rows(rows, *, bot, history_repo, queue_repo) -> int:
    """Delete a set of pending queue rows, routing any button-bearing row (one
    with a live Telegram card / telegram_message_id) through expire_sent_row so
    its buttons are stripped and a terminal history row is written instead of
    orphaning the card. Unstamped rows go through record_expiry_and_delete —
    they may equally carry a delivered card (#679/#680). Returns the count of
    rows actually removed (deferred or failed reaps are not counted)."""
    removed = 0
    for row in rows:
        if row.telegram_message_id is not None:
            if (
                await expire_sent_row(
                    row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
                )
                == "reaped"
            ):
                removed += 1
        elif record_expiry_and_delete(
            row, history_repo=history_repo, queue_repo=queue_repo
        ):
            removed += 1
    return removed


def reconcile_aged_unconfirmed(hours: int = 24, limit: int = 100) -> int:
    """Expire 'sent_unconfirmed' rows that aged out without ever being tapped.

    A ``sent_unconfirmed`` row is a send whose delivery could not be confirmed
    (``AmbiguousDeliveryError``). A tap resolves it one-way to ``delivered``
    (``set_telegram_message``); this is the other half of that lifecycle — the
    never-tapped rows age out to terminal ``expired`` through the shared
    history-first reap (#687), which NEVER re-sends. (The #680 double-post
    class is a re-arm of the send path; expiry has no send path at all — that
    is the invariant this function preserves.)

    Bounded by ``limit`` and built to run OFF the event loop (the caller wraps
    it in ``asyncio.to_thread``): its DB work is synchronous psycopg2, so
    running it inline would block every tenant's callbacks (the #682/#573
    loop-starvation class). It constructs its OWN repositories so their Session
    is opened and closed inside the worker thread and never crosses the thread
    boundary.

    Returns the number of rows expired.
    """
    with QueueRepository() as queue_repo, HistoryRepository() as history_repo:
        expired = 0
        for row in queue_repo.get_aged_sent_unconfirmed(hours=hours, limit=limit):
            if record_expiry_and_delete(
                row, history_repo=history_repo, queue_repo=queue_repo
            ):
                expired += 1
        if expired:
            logger.info(
                f"Aged reconcile: expired {expired} sent_unconfirmed queue "
                f"item(s) older than {hours}h (batch limit {limit})"
            )
        return expired
