"""Tests for the shared queue reap behavior (src.services.core.queue_reap).

Covers ``expire_sent_row`` — the single helper both age-based reapers call
to gracefully expire a queue row already sent to Telegram (#560): C-edit the
card to "Expired" + strip the inline buttons, write a terminal ``expired``
posting_history row, then delete the queue row. Also covers the tap-time
caption fallback that shows "Expired" instead of "Queue item not found".
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest

from src.services.core.queue_reap import (
    EXPIRED_CAPTION,
    expire_sent_row,
    reap_pending_rows,
)
from src.services.core.telegram_utils import _build_already_handled_caption

_MODULE = "src.services.core.queue_reap"


def _make_row():
    """A button-bearing queue row (already sent to Telegram)."""
    ts = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    return Mock(
        id=uuid.uuid4(),
        media_item_id=uuid.uuid4(),
        chat_settings_id=uuid.uuid4(),
        telegram_message_id=555,
        telegram_chat_id=-100123,
        scheduled_for=ts,
        created_at=ts,
    )


@pytest.mark.unit
class TestExpireSentRow:
    """Behavioral contract for expire_sent_row."""

    @pytest.mark.asyncio
    async def test_happy_path_edits_records_and_deletes(self):
        """Successful edit → strips buttons, writes 'expired' history, deletes row."""
        row = _make_row()
        bot = AsyncMock()
        bot.edit_message_caption.return_value = Mock()  # truthy Message
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        queue_repo = Mock()

        result = await expire_sent_row(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result == "reaped"

        # One best-effort edit that both sets the caption AND strips buttons.
        bot.edit_message_caption.assert_called_once()
        kwargs = bot.edit_message_caption.call_args.kwargs
        assert kwargs["caption"] == EXPIRED_CAPTION
        assert kwargs["chat_id"] == row.telegram_chat_id
        assert kwargs["message_id"] == row.telegram_message_id
        markup = kwargs["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup)
        assert markup.inline_keyboard == ()  # buttons stripped

        # Terminal 'expired' history row written (audit + tap-time fallback).
        history_repo.create.assert_called_once()
        params = history_repo.create.call_args[0][0]
        assert params.status == "expired"
        assert params.success is False
        assert params.posting_method == "system_expiry"
        assert params.queue_item_id == str(row.id)
        assert params.media_item_id == str(row.media_item_id)

        queue_repo.delete.assert_called_once_with(str(row.id))

    @pytest.mark.asyncio
    async def test_transient_failure_defers_without_side_effects(self):
        """Wrapper returns None (transient exhausted) → no history, no delete, deferred."""
        row = _make_row()
        bot = AsyncMock()
        history_repo = Mock()
        queue_repo = Mock()

        with patch(
            f"{_MODULE}.telegram_edit_with_retry", new_callable=AsyncMock
        ) as mock_edit:
            mock_edit.return_value = None
            result = await expire_sent_row(
                row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
            )

        # Row is left intact & tappable for the next sweep — never orphaned.
        assert result == "deferred"
        history_repo.create.assert_not_called()
        queue_repo.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminal_edit_error_still_records_and_deletes(self):
        """Non-retryable edit error (message not found) → still records + deletes, reaped."""
        row = _make_row()
        bot = AsyncMock()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        queue_repo = Mock()

        with patch(
            f"{_MODULE}.telegram_edit_with_retry", new_callable=AsyncMock
        ) as mock_edit:
            mock_edit.side_effect = BadRequest("Message to edit not found")
            result = await expire_sent_row(
                row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
            )

        # Nothing editable left to orphan → proceed to record + delete.
        assert result == "reaped"
        history_repo.create.assert_called_once()
        queue_repo.delete.assert_called_once_with(str(row.id))

    @pytest.mark.asyncio
    async def test_idempotent_history_skips_create_but_still_deletes(self):
        """Existing history row → create skipped, delete still runs, reaped."""
        row = _make_row()
        bot = AsyncMock()
        bot.edit_message_caption.return_value = Mock()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = Mock()  # already recorded
        queue_repo = Mock()

        result = await expire_sent_row(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result == "reaped"
        history_repo.create.assert_not_called()
        queue_repo.delete.assert_called_once_with(str(row.id))


@pytest.mark.unit
class TestExpiredTapFallback:
    """The terminal 'expired' history row rescues a late tap on a reaped card."""

    def test_expired_history_yields_friendly_caption(self):
        """A late tap finds the 'expired' history and shows 'Expired', not the scary error."""
        history = Mock(status="expired", posting_method="system_expiry")
        caption = _build_already_handled_caption(history)
        assert caption == EXPIRED_CAPTION
        assert "not found" not in caption.lower()


@pytest.mark.unit
class TestReapPendingRows:
    """Batch reaper the live delete paths share: button-bearing rows go through
    expire_sent_row (strip card + write history), button-less rows plain-delete.
    """

    @pytest.mark.asyncio
    async def test_routes_button_rows_and_plain_deletes_the_rest(self):
        """Mixed set → each button-bearing row is expired, each None row deleted."""
        btn1 = _make_row()  # telegram_message_id=555 (live card)
        btn2 = _make_row()
        plain1 = _make_row()
        plain1.telegram_message_id = None  # never sent to Telegram
        plain2 = _make_row()
        plain2.telegram_message_id = None
        bot = AsyncMock()
        history_repo = Mock()
        queue_repo = Mock()

        with patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock) as mock_expire:
            mock_expire.return_value = "reaped"
            removed = await reap_pending_rows(
                [btn1, plain1, btn2, plain2],
                bot=bot,
                history_repo=history_repo,
                queue_repo=queue_repo,
            )

        # Every button-bearing row is routed through expire_sent_row (one each).
        assert mock_expire.await_count == 2
        reaped_rows = [c.args[0] for c in mock_expire.await_args_list]
        assert btn1 in reaped_rows
        assert btn2 in reaped_rows

        # Button-less rows are plain-deleted; the live ones are NOT delete()d.
        deleted_ids = {c.args[0] for c in queue_repo.delete.call_args_list}
        assert deleted_ids == {str(plain1.id), str(plain2.id)}

        # All four rows removed.
        assert removed == 4

    @pytest.mark.asyncio
    async def test_deferred_button_row_is_not_counted_or_double_deleted(self):
        """A transient-deferred reap leaves the row intact — not counted, not deleted."""
        btn = _make_row()  # telegram_message_id=555
        bot = AsyncMock()
        history_repo = Mock()
        queue_repo = Mock()

        with patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock) as mock_expire:
            mock_expire.return_value = "deferred"
            removed = await reap_pending_rows(
                [btn], bot=bot, history_repo=history_repo, queue_repo=queue_repo
            )

        mock_expire.assert_awaited_once()
        # Deferred → row left tappable for the next sweep, never fallback-deleted.
        assert removed == 0
        queue_repo.delete.assert_not_called()
