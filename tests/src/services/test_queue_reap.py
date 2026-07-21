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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from src.services.core.queue_reap import (
    EXPIRED_CAPTION,
    expire_sent_row,
    reap_pending_rows,
    reconcile_aged_unconfirmed,
    record_expiry_and_delete,
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


def _make_unsent_row():
    """A queue row with no stamped telegram_message_id.

    Either genuinely never sent, or — the #679/#680 class — delivered to
    Telegram but never stamped because the send raised after delivery.
    The two are indistinguishable from the row alone.
    """
    row = _make_row()
    row.telegram_message_id = None
    row.telegram_chat_id = None
    return row


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
    async def test_not_modified_is_already_expired_so_records_and_deletes(self):
        """'Message is not modified' = card already Expired + stripped → record + delete.

        The #682 regression: this permanent rejection was classified as
        transient, deferring the same rows every sweep forever and flooding
        the chat with no-op edits until Telegram rate-limited the bot.
        """
        row = _make_row()
        bot = AsyncMock()
        bot.edit_message_caption.side_effect = BadRequest(
            "Message is not modified: specified new message content and reply "
            "markup are exactly the same as a current content and reply markup "
            "of the message"
        )
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        queue_repo = Mock()

        result = await expire_sent_row(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result == "reaped"
        history_repo.create.assert_called_once()
        queue_repo.delete.assert_called_once_with(str(row.id))
        # One attempt only — in-reap retries would feed an active flood window.
        assert bot.edit_message_caption.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "transient",
        [RetryAfter(0), TimedOut(), NetworkError("connection dropped")],
        ids=["retry_after", "timed_out", "network_error"],
    )
    async def test_transient_failure_defers_without_side_effects(self, transient):
        """Transient edit failure (flood wait / timeout / network) → defer, untouched.

        Deferral is immediate and single-attempt: the sweep cadence is the
        retry loop, so backoff-sleeping here only stalls the reaper (and a
        RetryAfter sleep would wait out a flood window just to re-enter it).
        """
        row = _make_row()
        bot = AsyncMock()
        bot.edit_message_caption.side_effect = transient
        history_repo = Mock()
        queue_repo = Mock()

        result = await expire_sent_row(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )

        # Row is left intact & tappable for the next sweep — never orphaned.
        assert result == "deferred"
        history_repo.create.assert_not_called()
        queue_repo.delete.assert_not_called()
        assert bot.edit_message_caption.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "terminal",
        [BadRequest("Message to edit not found"), Forbidden("bot was kicked")],
        ids=["message_gone", "forbidden"],
    )
    async def test_terminal_edit_error_still_records_and_deletes(self, terminal):
        """Non-retryable edit error → nothing editable left, record + delete."""
        row = _make_row()
        bot = AsyncMock()
        bot.edit_message_caption.side_effect = terminal
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        queue_repo = Mock()

        result = await expire_sent_row(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )

        # Nothing editable left to orphan → proceed to record + delete.
        assert result == "reaped"
        history_repo.create.assert_called_once()
        queue_repo.delete.assert_called_once_with(str(row.id))

    @pytest.mark.asyncio
    async def test_record_failure_returns_failed_rolls_back_keeps_row(self):
        """A DB rejection recording the expiry must not raise out of the reap.

        Callers are mid-sweep over many rows — raising aborts the whole pass
        (and a dirty session poisons its remaining DB work). Log, roll back,
        leave the row for the next sweep. Surfaced by prod's
        check_posting_method constraint rejecting the 'system_expiry' write.
        """
        row = _make_row()
        bot = AsyncMock()
        bot.edit_message_caption.return_value = Mock()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        history_repo.create.side_effect = IntegrityError(
            "INSERT INTO posting_history", {}, Exception("check_posting_method")
        )
        queue_repo = Mock()

        result = await expire_sent_row(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result == "failed"
        queue_repo.delete.assert_not_called()
        history_repo.rollback.assert_called_once()
        queue_repo.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_failure_returns_failed_and_rolls_back(self):
        """A failure deleting the queue row is contained the same way."""
        row = _make_row()
        bot = AsyncMock()
        bot.edit_message_caption.return_value = Mock()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        queue_repo = Mock()
        queue_repo.delete.side_effect = SQLAlchemyError("connection lost")

        result = await expire_sent_row(
            row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result == "failed"
        history_repo.rollback.assert_called_once()
        queue_repo.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_db_error_still_raises(self):
        """Only DB failures are contained — programming errors surface loudly.

        A malformed row or bad params is a code defect: containing it would
        retry the same row every sweep forever, indistinguishable from a
        transient DB blip. Let it propagate to the loop-level handler.
        """
        row = _make_row()
        bot = AsyncMock()
        bot.edit_message_caption.return_value = Mock()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.side_effect = AttributeError("malformed row")
        queue_repo = Mock()

        with pytest.raises(AttributeError):
            await expire_sent_row(
                row, bot=bot, history_repo=history_repo, queue_repo=queue_repo
            )

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
class TestRecordExpiryAndDelete:
    """Contract for the shared terminal-history-then-delete step (#687).

    Every age-based deletion of an unstamped (telegram_message_id IS NULL)
    row must write the terminal 'expired' history row BEFORE deleting: an
    unstamped row may still have delivered a live card (#679/#680), and
    without history a tap on that card surfaces the raw "Queue item not
    found" error instead of the graceful "Expired" caption.
    """

    def test_writes_terminal_history_then_deletes(self):
        """History row (expired/system_expiry) written, then the row deleted."""
        row = _make_unsent_row()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        queue_repo = Mock()

        result = record_expiry_and_delete(
            row, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result is True
        history_repo.create.assert_called_once()
        params = history_repo.create.call_args[0][0]
        assert params.status == "expired"
        assert params.success is False
        assert params.posting_method == "system_expiry"
        assert params.queue_item_id == str(row.id)
        assert params.media_item_id == str(row.media_item_id)
        assert params.chat_settings_id == str(row.chat_settings_id)
        queue_repo.delete.assert_called_once_with(str(row.id))

    def test_idempotent_history_skips_create_but_still_deletes(self):
        """Existing history row → create skipped, delete still runs."""
        row = _make_unsent_row()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = Mock()  # already recorded
        queue_repo = Mock()

        result = record_expiry_and_delete(
            row, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result is True
        history_repo.create.assert_not_called()
        queue_repo.delete.assert_called_once_with(str(row.id))

    def test_history_write_failure_keeps_row_and_rolls_back(self):
        """History write rejected → row NOT deleted (never delete without the
        terminal record — that is the orphaning bug), both sessions rolled
        back, False returned so callers can skip the row without aborting."""
        row = _make_unsent_row()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        history_repo.create.side_effect = IntegrityError(
            "INSERT INTO posting_history", {}, Exception("check_posting_method")
        )
        queue_repo = Mock()

        result = record_expiry_and_delete(
            row, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result is False
        queue_repo.delete.assert_not_called()
        history_repo.rollback.assert_called_once()
        queue_repo.rollback.assert_called_once()

    def test_delete_failure_rolls_back_and_returns_false(self):
        """A DB failure on the delete is contained the same way."""
        row = _make_unsent_row()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        queue_repo = Mock()
        queue_repo.delete.side_effect = SQLAlchemyError("connection lost")

        result = record_expiry_and_delete(
            row, history_repo=history_repo, queue_repo=queue_repo
        )

        assert result is False
        history_repo.rollback.assert_called_once()
        queue_repo.rollback.assert_called_once()

    def test_non_db_error_still_raises(self):
        """Programming errors surface loudly instead of per-row containment."""
        row = _make_unsent_row()
        history_repo = Mock()
        history_repo.get_by_queue_item_id.side_effect = AttributeError("malformed row")
        queue_repo = Mock()

        with pytest.raises(AttributeError):
            record_expiry_and_delete(
                row, history_repo=history_repo, queue_repo=queue_repo
            )

    def test_687_regression_tap_after_sweep_shows_expired_not_qinf(self):
        """The #687 orphan, end to end: a delivered-but-unstamped row swept by
        an age-based reaper leaves history behind, so the tap-time fallback
        renders the graceful "Expired" caption — never the raw
        "Queue item not found" error."""
        row = _make_unsent_row()  # delivered card, stamp never landed (#679)
        history_repo = Mock()
        history_repo.get_by_queue_item_id.return_value = None
        queue_repo = Mock()

        record_expiry_and_delete(row, history_repo=history_repo, queue_repo=queue_repo)

        # The tap-time fallback (validate_queue_item) finds the history row
        # this sweep just wrote and builds the friendly caption from it.
        written = history_repo.create.call_args[0][0]
        caption = _build_already_handled_caption(written)
        assert caption == EXPIRED_CAPTION
        assert "not found" not in caption.lower()


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
    expire_sent_row (strip card + write history), unstamped rows through
    record_expiry_and_delete (write history, then delete — #687).
    """

    @pytest.mark.asyncio
    async def test_routes_button_rows_and_records_expiry_for_the_rest(self):
        """Mixed set → button-bearing rows expired, unstamped rows recorded +
        deleted through the shared helper — never raw-deleted (#687)."""
        btn1 = _make_row()  # telegram_message_id=555 (live card)
        btn2 = _make_row()
        plain1 = _make_unsent_row()  # unstamped — may still carry a card
        plain2 = _make_unsent_row()
        bot = AsyncMock()
        history_repo = Mock()
        queue_repo = Mock()

        with (
            patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock) as mock_expire,
            patch(f"{_MODULE}.record_expiry_and_delete") as mock_record,
        ):
            mock_expire.return_value = "reaped"
            mock_record.return_value = True
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

        # Unstamped rows go through the history-then-delete helper.
        assert mock_record.call_count == 2
        recorded_rows = [c.args[0] for c in mock_record.call_args_list]
        assert plain1 in recorded_rows
        assert plain2 in recorded_rows
        queue_repo.delete.assert_not_called()

        # All four rows removed.
        assert removed == 4

    @pytest.mark.asyncio
    async def test_failed_record_of_unstamped_row_is_not_counted(self):
        """A contained record+delete failure leaves the row for the next pass."""
        plain = _make_unsent_row()
        bot = AsyncMock()
        history_repo = Mock()
        queue_repo = Mock()

        with patch(f"{_MODULE}.record_expiry_and_delete") as mock_record:
            mock_record.return_value = False
            removed = await reap_pending_rows(
                [plain], bot=bot, history_repo=history_repo, queue_repo=queue_repo
            )

        assert removed == 0

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

    @pytest.mark.asyncio
    async def test_failed_row_does_not_abort_batch_or_count(self):
        """A row whose record+delete failed is skipped, the batch continues."""
        bad = _make_row()
        good = _make_row()
        bot = AsyncMock()
        history_repo = Mock()
        queue_repo = Mock()

        with patch(f"{_MODULE}.expire_sent_row", new_callable=AsyncMock) as mock_expire:
            mock_expire.side_effect = ["failed", "reaped"]
            removed = await reap_pending_rows(
                [bad, good], bot=bot, history_repo=history_repo, queue_repo=queue_repo
            )

        assert mock_expire.await_count == 2
        assert removed == 1


@pytest.mark.unit
class TestReconcileAgedUnconfirmed:
    """The bounded aged-reconcile for never-tapped 'sent_unconfirmed' rows.

    It builds its OWN repositories (so its Session lives entirely inside the
    worker thread the loop offloads it to) and expires each aged row through
    the shared history-first reap — it carries NO ``bot`` and no send path, so
    it can never re-post (the #680 class it must not reintroduce)."""

    def test_expires_each_aged_row_and_returns_count(self):
        rows = [_make_unsent_row(), _make_unsent_row()]
        queue_repo = Mock()
        queue_repo.get_aged_sent_unconfirmed.return_value = rows
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.QueueRepository", return_value=queue_repo),
            patch(f"{_MODULE}.HistoryRepository", return_value=history_repo),
            patch(f"{_MODULE}.record_expiry_and_delete", return_value=True) as rec,
        ):
            expired = reconcile_aged_unconfirmed(hours=24, limit=100)

        assert expired == 2
        queue_repo.get_aged_sent_unconfirmed.assert_called_once_with(
            hours=24, limit=100
        )
        assert rec.call_count == 2
        for row in rows:
            rec.assert_any_call(row, history_repo=history_repo, queue_repo=queue_repo)
        # It owns its repos and closes them.
        queue_repo.close.assert_called_once()
        history_repo.close.assert_called_once()

    def test_counts_only_successful_reaps(self):
        """A contained per-row DB failure (record_expiry_and_delete -> False)
        is not counted — that row simply waits for the next pass."""
        rows = [_make_unsent_row(), _make_unsent_row()]
        queue_repo = Mock()
        queue_repo.get_aged_sent_unconfirmed.return_value = rows
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.QueueRepository", return_value=queue_repo),
            patch(f"{_MODULE}.HistoryRepository", return_value=history_repo),
            patch(f"{_MODULE}.record_expiry_and_delete", side_effect=[True, False]),
        ):
            expired = reconcile_aged_unconfirmed()

        assert expired == 1

    def test_closes_repos_even_when_the_pass_raises(self):
        """A raise mid-pass must still close both repos — no session leak."""
        queue_repo = Mock()
        queue_repo.get_aged_sent_unconfirmed.side_effect = RuntimeError("boom")
        history_repo = Mock()

        with (
            patch(f"{_MODULE}.QueueRepository", return_value=queue_repo),
            patch(f"{_MODULE}.HistoryRepository", return_value=history_repo),
        ):
            with pytest.raises(RuntimeError):
                reconcile_aged_unconfirmed()

        queue_repo.close.assert_called_once()
        history_repo.close.assert_called_once()
