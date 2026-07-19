"""Tests for SchedulerService (JIT model)."""

import pytest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from src.exceptions.google_drive import GoogleDriveAuthError
from src.services.core.scheduler import SchedulerService
from tests.src.services.conftest import mock_track_execution


@pytest.fixture
def scheduler_service_mocked():
    """Create SchedulerService with all dependencies mocked."""
    with patch.object(SchedulerService, "__init__", lambda self: None):
        service = SchedulerService()
        service.media_repo = Mock()
        service.queue_repo = Mock()
        service.queue_repo.count_by_status.return_value = 0
        service.queue_repo.count_recent_by_status.return_value = 0
        service.queue_repo.get_stale_unsent_pending.return_value = []
        service.history_repo = Mock()
        service.history_repo.count_posts_today.return_value = 0
        service.lock_repo = Mock()
        service.category_mix_repo = Mock()
        service.settings_service = Mock()
        service.telegram_service = AsyncMock()
        service.service_run_repo = Mock()
        service.service_name = "SchedulerService"
        service.SCHEDULE_JITTER_MINUTES = 30
        service._consecutive_send_failures = 0
        service.track_execution = mock_track_execution
        service.set_result_summary = Mock()
        return service


def _make_chat_settings(
    *,
    posts_per_day=3,
    posting_hours_start=9,
    posting_hours_end=21,
    last_post_sent_at=None,
    is_paused=False,
    telegram_chat_id=-100123,
    settings_id=None,
    posting_timezone=None,
    enable_instagram_api=False,
    dry_run_mode=False,
):
    """Helper to build a mock chat_settings object."""
    cs = Mock()
    cs.posts_per_day = posts_per_day
    cs.posting_hours_start = posting_hours_start
    cs.posting_hours_end = posting_hours_end
    cs.last_post_sent_at = last_post_sent_at
    cs.is_paused = is_paused
    cs.telegram_chat_id = telegram_chat_id
    cs.id = settings_id or uuid4()
    cs.posting_timezone = posting_timezone
    cs.enable_instagram_api = enable_instagram_api
    cs.dry_run_mode = dry_run_mode
    return cs


# ------------------------------------------------------------------
# is_slot_due
# ------------------------------------------------------------------


@pytest.mark.unit
class TestIsSlotDue:
    """Tests for SchedulerService.is_slot_due()."""

    def test_in_window_and_first_post_ever(self, scheduler_service_mocked):
        """Slot is due when last_post_sent_at is None (first post)."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {}

        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=None,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
            result = service.is_slot_due(cs)

        # Should be due (None = no category preference)
        assert result is None

    def test_outside_posting_window_returns_false(self, scheduler_service_mocked):
        """Returns False when current time is outside posting window."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=17,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 21, 20, 0, tzinfo=timezone.utc)
            result = service.is_slot_due(cs)

        assert result is False

    def test_too_soon_since_last_post(self, scheduler_service_mocked):
        """Returns False when last post was too recent."""
        service = scheduler_service_mocked
        # Window 9-21 = 12 hours, 3 posts/day => interval = 4 hours
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=datetime(2026, 3, 21, 11, 0),
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # Only 1 hour since last post, interval is 4 hours
            mock_dt.now.return_value = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
            result = service.is_slot_due(cs)

        assert result is False

    def test_due_after_sufficient_interval(self, scheduler_service_mocked):
        """Returns category (or None) when enough time has elapsed."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {}

        # Window 9-21 = 12 hours, 3 posts/day => interval = 4 hours
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=datetime(2026, 3, 21, 8, 0),
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # 5 hours since last post, interval is 4 hours -> due
            mock_dt.now.return_value = datetime(2026, 3, 21, 13, 0, tzinfo=timezone.utc)
            result = service.is_slot_due(cs)

        # No category ratios -> None (due, no preference)
        assert result is None

    def test_midnight_rollover_window(self, scheduler_service_mocked):
        """Slot is due when posting window crosses midnight (e.g. 22-2)."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {}

        cs = _make_chat_settings(
            posting_hours_start=22,
            posting_hours_end=2,
            posts_per_day=2,
            last_post_sent_at=None,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 21, 23, 0, tzinfo=timezone.utc)
            result = service.is_slot_due(cs)

        assert result is not False

    def test_single_post_per_day(self, scheduler_service_mocked):
        """Single post per day with 12-hour window => interval = 12 hours."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {}

        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=1,
            last_post_sent_at=datetime(2026, 3, 21, 9, 0),
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # Only 2 hours since last post, interval is 12 hours
            mock_dt.now.return_value = datetime(2026, 3, 21, 11, 0, tzinfo=timezone.utc)
            result = service.is_slot_due(cs)

        assert result is False

    def test_returns_category_when_ratios_configured(self, scheduler_service_mocked):
        """Returns a category string when category ratios are configured."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {
            "memes": Decimal("0.7"),
            "merch": Decimal("0.3"),
        }

        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=None,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
            result = service.is_slot_due(cs)

        assert isinstance(result, str)
        assert result in ("memes", "merch")


# ------------------------------------------------------------------
# process_slot
# ------------------------------------------------------------------


@pytest.mark.unit
class TestProcessSlot:
    """Tests for SchedulerService.process_slot()."""

    @pytest.mark.asyncio
    async def test_paused_returns_paused(self, scheduler_service_mocked):
        """Returns paused result when chat is paused."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(is_paused=True)
        service.settings_service.get_settings.return_value = cs

        result = await service.process_slot(telegram_chat_id=-100123)

        assert result["posted"] is False
        assert result["reason"] == "paused"

    @pytest.mark.asyncio
    async def test_not_due_returns_not_due(self, scheduler_service_mocked):
        """Returns not_due when is_slot_due returns False."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(is_paused=False)
        service.settings_service.get_settings.return_value = cs
        service.is_slot_due = Mock(return_value=False)

        result = await service.process_slot(telegram_chat_id=-100123)

        assert result["posted"] is False
        assert result["reason"] == "not_due"

    @pytest.mark.asyncio
    async def test_posts_successfully(self, scheduler_service_mocked):
        """Delegates to _select_and_send when slot is due."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(is_paused=False)
        service.settings_service.get_settings.return_value = cs
        service.is_slot_due = Mock(return_value=None)

        expected_result = {
            "posted": True,
            "queue_item_id": "q-1",
            "media_file": "test.jpg",
        }
        service._select_and_send = AsyncMock(return_value=expected_result)

        result = await service.process_slot(telegram_chat_id=-100123)

        assert result["posted"] is True
        service._select_and_send.assert_called_once()
        call_kwargs = service._select_and_send.call_args.kwargs
        assert call_kwargs["category"] is None
        assert call_kwargs["triggered_by"] == "scheduler"

    @pytest.mark.asyncio
    async def test_passes_category_from_is_slot_due(self, scheduler_service_mocked):
        """Passes category string from is_slot_due to _select_and_send."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(is_paused=False)
        service.settings_service.get_settings.return_value = cs
        service.is_slot_due = Mock(return_value="memes")
        service._select_and_send = AsyncMock(return_value={"posted": True})

        await service.process_slot(telegram_chat_id=-100123)

        call_kwargs = service._select_and_send.call_args.kwargs
        assert call_kwargs["category"] == "memes"

    @pytest.mark.asyncio
    async def test_no_eligible_media(self, scheduler_service_mocked):
        """Returns no_eligible_media when _select_media returns None."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(is_paused=False)
        service.settings_service.get_settings.return_value = cs
        service.is_slot_due = Mock(return_value=None)

        # Let _select_and_send flow through to real implementation
        service.media_repo.get_next_eligible_for_posting.return_value = None

        result = await service.process_slot(telegram_chat_id=-100123)

        assert result["posted"] is False
        assert result["reason"] == "no_eligible_media"


# ------------------------------------------------------------------
# force_send_next
# ------------------------------------------------------------------


@pytest.mark.unit
class TestForceSendNext:
    """Tests for SchedulerService.force_send_next()."""

    @pytest.mark.asyncio
    async def test_success(self, scheduler_service_mocked):
        """Sends immediately regardless of is_slot_due."""
        service = scheduler_service_mocked
        cs = _make_chat_settings()
        service.settings_service.get_settings.return_value = cs

        mock_media = Mock(
            id=uuid4(), file_name="force.jpg", category="memes", times_posted=0
        )
        service.media_repo.get_next_eligible_for_posting.return_value = mock_media

        mock_queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = mock_queue_item
        service.telegram_service.send_notification = AsyncMock(return_value=True)

        result = await service.force_send_next(
            telegram_chat_id=-100123, user_id="user-1"
        )

        assert result["posted"] is True
        assert result["media_file"] == "force.jpg"
        service.settings_service.update_last_post_sent_at.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_media_available(self, scheduler_service_mocked):
        """Returns error when no eligible media exists."""
        service = scheduler_service_mocked
        cs = _make_chat_settings()
        service.settings_service.get_settings.return_value = cs
        service.media_repo.get_next_eligible_for_posting.return_value = None

        result = await service.force_send_next(telegram_chat_id=-100123)

        assert result["posted"] is False
        assert result["error"] == "No eligible media available"

    @pytest.mark.asyncio
    async def test_force_sent_indicator_passed_through(self, scheduler_service_mocked):
        """force_sent_indicator is threaded to _send_to_telegram."""
        service = scheduler_service_mocked
        cs = _make_chat_settings()
        service.settings_service.get_settings.return_value = cs

        mock_media = Mock(
            id=uuid4(),
            file_name="f.jpg",
            category=None,
            times_posted=0,
            caption=None,
            generated_caption=None,
        )
        service.media_repo.get_next_eligible_for_posting.return_value = mock_media

        mock_queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = mock_queue_item
        service.telegram_service.send_notification = AsyncMock(return_value=True)

        await service.force_send_next(
            telegram_chat_id=-100123, force_sent_indicator=True
        )

        service.telegram_service.send_notification.assert_called_once_with(
            str(mock_queue_item.id), force_sent=True
        )


# ------------------------------------------------------------------
# _send_to_telegram
# ------------------------------------------------------------------


@pytest.mark.unit
class TestSendToTelegram:
    """Tests for SchedulerService._send_to_telegram()."""

    @pytest.mark.asyncio
    async def test_success(self, scheduler_service_mocked):
        """Marks item as processing, sends, returns True."""
        service = scheduler_service_mocked
        queue_item = Mock(id=uuid4())
        service.telegram_service.send_notification = AsyncMock(return_value=True)

        result = await service._send_to_telegram(queue_item)

        assert result is True
        service.queue_repo.update_status.assert_called_once_with(
            str(queue_item.id), "processing"
        )

    @pytest.mark.asyncio
    async def test_failure_retries_then_marks_failed(self, scheduler_service_mocked):
        """Retries 3 times on failure, then marks queue item as failed."""
        service = scheduler_service_mocked
        queue_item = Mock(
            id=uuid4(),
            media_item_id=uuid4(),
            chat_settings_id=uuid4(),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scheduled_for=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        service.telegram_service.send_notification = AsyncMock(return_value=False)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await service._send_to_telegram(queue_item)

        assert result is False
        assert service.telegram_service.send_notification.call_count == 3
        service.queue_repo.update_status.assert_any_call(str(queue_item.id), "failed")
        service.queue_repo.delete.assert_not_called()
        service.history_repo.create.assert_called_once()
        params = service.history_repo.create.call_args[0][0]
        assert params.status == "failed"
        assert params.success is False
        assert params.error_message == "send_notification returned False"

    @pytest.mark.asyncio
    async def test_success_after_retry(self, scheduler_service_mocked):
        """Succeeds on second attempt after first failure."""
        service = scheduler_service_mocked
        queue_item = Mock(id=uuid4(), media_item_id=uuid4())
        service.telegram_service.send_notification = AsyncMock(
            side_effect=[False, True]
        )
        service._consecutive_send_failures = 1

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await service._send_to_telegram(queue_item)

        assert result is True
        assert service.telegram_service.send_notification.call_count == 2
        assert service._consecutive_send_failures == 0
        service.history_repo.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_ambiguous_delivery_not_retried_stays_processing(
        self, scheduler_service_mocked
    ):
        """An ambiguous send (timed out; the card may already be in the chat)
        must not be resent — that posts a duplicate approval card. The item is
        left in 'processing' (the stale-processing sweep requeues it if it
        never arrived; a button click reconciles it if it did). No retry, no
        'failed' status, no failure history row."""
        from src.exceptions.telegram import AmbiguousDeliveryError

        service = scheduler_service_mocked
        queue_item = Mock(
            id=uuid4(),
            media_item_id=uuid4(),
            chat_settings_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scheduled_for=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        service.telegram_service.send_notification = AsyncMock(
            side_effect=AmbiguousDeliveryError("Telegram send timed out")
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await service._send_to_telegram(queue_item)

        assert result is False
        assert service.telegram_service.send_notification.call_count == 1
        service.queue_repo.update_status.assert_called_once_with(
            str(queue_item.id), "processing"
        )
        service.history_repo.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_retries_then_marks_failed(self, scheduler_service_mocked):
        """Exceptions trigger retries, then mark failed."""
        service = scheduler_service_mocked
        queue_item = Mock(
            id=uuid4(),
            media_item_id=uuid4(),
            chat_settings_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scheduled_for=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        service.telegram_service.send_notification = AsyncMock(
            side_effect=RuntimeError("Network error")
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await service._send_to_telegram(queue_item)

        assert result is False
        assert service.telegram_service.send_notification.call_count == 3
        service.queue_repo.update_status.assert_any_call(str(queue_item.id), "failed")
        params = service.history_repo.create.call_args[0][0]
        assert params.error_message == "Network error"

    @pytest.mark.asyncio
    async def test_google_drive_auth_error_no_retry(self, scheduler_service_mocked):
        """GoogleDriveAuthError fails immediately without retrying."""
        service = scheduler_service_mocked
        queue_item = Mock(
            id=uuid4(),
            media_item_id=uuid4(),
            chat_settings_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scheduled_for=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        service.telegram_service.send_notification = AsyncMock(
            side_effect=GoogleDriveAuthError("Token expired")
        )

        with pytest.raises(GoogleDriveAuthError, match="Token expired"):
            await service._send_to_telegram(queue_item)

        assert service.telegram_service.send_notification.call_count == 1
        service.queue_repo.update_status.assert_any_call(str(queue_item.id), "failed")
        service.history_repo.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_consecutive_failures_logs_critical(self, scheduler_service_mocked):
        """3+ consecutive failures triggers CRITICAL log."""
        service = scheduler_service_mocked
        service._consecutive_send_failures = 2

        queue_item = Mock(
            id=uuid4(),
            media_item_id=uuid4(),
            chat_settings_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scheduled_for=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        service.telegram_service.send_notification = AsyncMock(return_value=False)

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("src.services.core.scheduler.logger") as mock_logger,
        ):
            await service._send_to_telegram(queue_item)

        assert service._consecutive_send_failures == 3
        mock_logger.critical.assert_called_once()
        call_msg = mock_logger.critical.call_args[0][0]
        assert "SYSTEMIC FAILURE" in call_msg
        assert "3 consecutive" in call_msg

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_failures(self, scheduler_service_mocked):
        """Successful send resets the consecutive failure counter."""
        service = scheduler_service_mocked
        service._consecutive_send_failures = 5
        queue_item = Mock(id=uuid4())
        service.telegram_service.send_notification = AsyncMock(return_value=True)

        await service._send_to_telegram(queue_item)

        assert service._consecutive_send_failures == 0


# ------------------------------------------------------------------
# Posting window helpers
# ------------------------------------------------------------------


@pytest.mark.unit
class TestPostingWindowHelpers:
    """Tests for _in_posting_window and _posting_window_hours."""

    def test_in_window_normal_hours(self):
        """Time within normal posting window returns True."""
        cs = _make_chat_settings(posting_hours_start=9, posting_hours_end=21)
        now = datetime(2026, 3, 21, 12, 0)

        assert SchedulerService._in_posting_window(now, cs) is True

    def test_outside_window_normal_hours(self):
        """Time outside normal posting window returns False."""
        cs = _make_chat_settings(posting_hours_start=9, posting_hours_end=17)
        now = datetime(2026, 3, 21, 20, 0)

        assert SchedulerService._in_posting_window(now, cs) is False

    def test_at_start_boundary(self):
        """Time exactly at window start is inside."""
        cs = _make_chat_settings(posting_hours_start=9, posting_hours_end=17)
        now = datetime(2026, 3, 21, 9, 0)

        assert SchedulerService._in_posting_window(now, cs) is True

    def test_at_end_boundary(self):
        """Time exactly at window end is outside (half-open interval)."""
        cs = _make_chat_settings(posting_hours_start=9, posting_hours_end=17)
        now = datetime(2026, 3, 21, 17, 0)

        assert SchedulerService._in_posting_window(now, cs) is False

    def test_midnight_crossing_before_midnight(self):
        """Window 22-2: time at 23 is inside."""
        cs = _make_chat_settings(posting_hours_start=22, posting_hours_end=2)
        now = datetime(2026, 3, 21, 23, 0)

        assert SchedulerService._in_posting_window(now, cs) is True

    def test_midnight_crossing_after_midnight(self):
        """Window 22-2: time at 1 is inside."""
        cs = _make_chat_settings(posting_hours_start=22, posting_hours_end=2)
        now = datetime(2026, 3, 22, 1, 0)

        assert SchedulerService._in_posting_window(now, cs) is True

    def test_midnight_crossing_outside(self):
        """Window 22-2: time at 15 is outside."""
        cs = _make_chat_settings(posting_hours_start=22, posting_hours_end=2)
        now = datetime(2026, 3, 21, 15, 0)

        assert SchedulerService._in_posting_window(now, cs) is False

    def test_posting_window_hours_normal(self):
        """Normal window: 21 - 9 = 12 hours."""
        cs = _make_chat_settings(posting_hours_start=9, posting_hours_end=21)

        assert SchedulerService._posting_window_hours(cs) == 12.0

    def test_posting_window_hours_midnight_crossing(self):
        """Midnight crossing: (24 - 22) + 2 = 4 hours."""
        cs = _make_chat_settings(posting_hours_start=22, posting_hours_end=2)

        assert SchedulerService._posting_window_hours(cs) == 4.0

    def test_posting_window_hours_full_day(self):
        """Full day window: 24 - 0 = 24 hours."""
        cs = _make_chat_settings(posting_hours_start=0, posting_hours_end=24)

        assert SchedulerService._posting_window_hours(cs) == 24.0


# ------------------------------------------------------------------
# Timezone-aware posting window (#351)
# ------------------------------------------------------------------


@pytest.mark.unit
class TestPostingWindowTimezone:
    """Tests for timezone-aware posting window."""

    def test_utc_fallback_when_no_timezone(self):
        """No posting_timezone means hours are compared in UTC."""
        cs = _make_chat_settings(
            posting_hours_start=9, posting_hours_end=17, posting_timezone=None
        )
        # 12:00 UTC — inside 9-17 UTC window
        now = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
        assert SchedulerService._in_posting_window(now, cs) is True

    def test_timezone_converts_utc_to_local(self):
        """UTC time is converted to user timezone before comparing."""
        # Window 9-17 Eastern. At 14:00 UTC = 10:00 ET (inside).
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=17,
            posting_timezone="America/New_York",
        )
        now = datetime(2026, 3, 21, 14, 0, tzinfo=timezone.utc)
        assert SchedulerService._in_posting_window(now, cs) is True

    def test_timezone_outside_window(self):
        """UTC time that maps to outside the local window returns False."""
        # Window 9-17 Eastern. At 23:00 UTC = 19:00 ET (outside).
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=17,
            posting_timezone="America/New_York",
        )
        now = datetime(2026, 3, 21, 23, 0, tzinfo=timezone.utc)
        assert SchedulerService._in_posting_window(now, cs) is False

    def test_timezone_midnight_crossing(self):
        """Timezone conversion with midnight-crossing window."""
        # Window 20-2 Eastern. At 01:00 UTC = 21:00 ET prev day (inside).
        cs = _make_chat_settings(
            posting_hours_start=20,
            posting_hours_end=2,
            posting_timezone="America/New_York",
        )
        now = datetime(2026, 3, 22, 1, 0, tzinfo=timezone.utc)
        assert SchedulerService._in_posting_window(now, cs) is True

    def test_timezone_europe(self):
        """Non-US timezone works correctly."""
        # Window 9-17 Berlin (CET = UTC+1 in winter, CEST = UTC+2 in summer).
        # March 21 2026 is after DST switch (CEST = UTC+2).
        # 10:00 UTC = 12:00 CEST (inside 9-17).
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=17,
            posting_timezone="Europe/Berlin",
        )
        now = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
        assert SchedulerService._in_posting_window(now, cs) is True

    def test_timezone_does_not_affect_window_hours_calculation(self):
        """_posting_window_hours is pure arithmetic, unaffected by timezone."""
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posting_timezone="Asia/Tokyo",
        )
        assert SchedulerService._posting_window_hours(cs) == 12.0

    def test_invalid_timezone_falls_back_to_utc(self):
        """Invalid timezone string falls back to UTC with a warning."""
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=17,
            posting_timezone="Not/A_Real_Zone",
        )
        # 12:00 UTC — inside 9-17 window when treated as UTC
        now = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
        assert SchedulerService._in_posting_window(now, cs) is True

        # 20:00 UTC — outside 9-17 window when treated as UTC
        now_outside = datetime(2026, 3, 21, 20, 0, tzinfo=timezone.utc)
        assert SchedulerService._in_posting_window(now_outside, cs) is False


# ------------------------------------------------------------------
# _pick_category_for_slot
# ------------------------------------------------------------------


@pytest.mark.unit
class TestPickCategoryForSlot:
    """Tests for SchedulerService._pick_category_for_slot()."""

    def test_returns_none_when_no_ratios(self, scheduler_service_mocked):
        """Returns None when the tenant has no category mix configured."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {}

        assert service._pick_category_for_slot("tenant-A") is None

    def test_returns_category_with_ratios(self, scheduler_service_mocked):
        """Returns a valid category when ratios are configured."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {
            "memes": Decimal("0.7"),
            "merch": Decimal("0.3"),
        }

        result = service._pick_category_for_slot("tenant-A")

        assert result in ("memes", "merch")

    def test_single_category_always_returned(self, scheduler_service_mocked):
        """Single category at 100% is always returned."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {
            "memes": Decimal("1.0"),
        }

        for _ in range(10):
            assert service._pick_category_for_slot("tenant-A") == "memes"

    def test_scopes_mix_to_tenant(self, scheduler_service_mocked):
        """The mix read is scoped to the caller's tenant (#542)."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {
            "memes": Decimal("1.0"),
        }

        service._pick_category_for_slot("tenant-A")

        service.category_mix_repo.get_current_mix_as_dict.assert_called_once_with(
            "tenant-A"
        )

    def test_fail_closed_without_tenant(self, scheduler_service_mocked):
        """Fail-closed: a missing tenant reads no mix, never the global merged mix."""
        service = scheduler_service_mocked

        result = service._pick_category_for_slot(None)

        assert result is None
        service.category_mix_repo.get_current_mix_as_dict.assert_not_called()


# ------------------------------------------------------------------
# Category allocation (unchanged methods - kept from original tests)
# ------------------------------------------------------------------


@pytest.mark.unit
class TestSchedulerCategoryAllocation:
    """Test suite for category-based slot allocation."""

    @pytest.fixture
    def scheduler_service(self):
        """Create SchedulerService with mocked dependencies."""
        with patch.object(SchedulerService, "__init__", lambda self: None):
            service = SchedulerService()
            service.media_repo = Mock()
            service.queue_repo = Mock()
            service.queue_repo.count_by_status.return_value = 0
            service.queue_repo.count_recent_by_status.return_value = 0
            service.queue_repo.get_stale_unsent_pending.return_value = []
            service.lock_repo = Mock()
            service.category_mix_repo = Mock()
            service.settings_service = Mock()
            service.service_run_repo = Mock()
            service.service_name = "SchedulerService"
            service.SCHEDULE_JITTER_MINUTES = 30
            service.track_execution = mock_track_execution
            service.set_result_summary = Mock()
            return service

    def test_allocate_slots_with_ratios(self, scheduler_service):
        """Test that slots are allocated according to category ratios."""
        scheduler_service.category_mix_repo.get_current_mix_as_dict.return_value = {
            "memes": Decimal("0.7"),
            "merch": Decimal("0.3"),
        }

        allocation = scheduler_service._allocate_slots_to_categories(10)

        assert len(allocation) == 10

        memes_count = allocation.count("memes")
        merch_count = allocation.count("merch")

        assert memes_count == 7
        assert merch_count == 3

    def test_allocate_slots_with_rounding(self, scheduler_service):
        """Test that slot allocation handles rounding correctly."""
        scheduler_service.category_mix_repo.get_current_mix_as_dict.return_value = {
            "memes": Decimal("0.7"),
            "merch": Decimal("0.3"),
        }

        allocation = scheduler_service._allocate_slots_to_categories(21)

        assert len(allocation) == 21

        memes_count = allocation.count("memes")
        merch_count = allocation.count("merch")

        assert memes_count + merch_count == 21
        assert memes_count >= 14 and memes_count <= 15
        assert merch_count >= 6 and merch_count <= 7

    def test_allocate_slots_no_ratios_configured(self, scheduler_service):
        """Test that empty list is returned when no ratios configured."""
        scheduler_service.category_mix_repo.get_current_mix_as_dict.return_value = {}

        allocation = scheduler_service._allocate_slots_to_categories(10)

        assert allocation == []

    def test_allocate_slots_single_category(self, scheduler_service):
        """Test allocation with single category at 100%."""
        scheduler_service.category_mix_repo.get_current_mix_as_dict.return_value = {
            "memes": Decimal("1.0"),
        }

        allocation = scheduler_service._allocate_slots_to_categories(10)

        assert len(allocation) == 10
        assert all(cat == "memes" for cat in allocation)

    def test_allocate_slots_three_categories(self, scheduler_service):
        """Test allocation with three categories."""
        scheduler_service.category_mix_repo.get_current_mix_as_dict.return_value = {
            "memes": Decimal("0.5"),
            "merch": Decimal("0.3"),
            "misc": Decimal("0.2"),
        }

        allocation = scheduler_service._allocate_slots_to_categories(10)

        assert len(allocation) == 10

        memes = allocation.count("memes")
        merch = allocation.count("merch")
        misc = allocation.count("misc")

        assert memes == 5
        assert merch == 3
        assert misc == 2

    def test_summarize_allocation(self, scheduler_service):
        """Test allocation summary string."""
        allocation = ["memes", "memes", "merch", "memes", "merch"]

        summary = scheduler_service._summarize_allocation(allocation)

        assert "memes: 3" in summary
        assert "merch: 2" in summary

    def test_select_media_with_category(self, scheduler_service):
        """Test that _select_media passes category to pool selection."""
        mock_media = Mock(category="memes", file_name="test.jpg")
        scheduler_service._select_media_from_pool = Mock(return_value=mock_media)

        result = scheduler_service._select_media("tenant-A", category="memes")

        scheduler_service._select_media_from_pool.assert_called_with(
            "tenant-A", category="memes", exclude_ids=None
        )
        assert result == mock_media

    def test_select_media_fallback_when_category_exhausted(self, scheduler_service):
        """Test fallback to any category when target is exhausted."""
        mock_media = Mock(category="merch", file_name="fallback.jpg")

        scheduler_service._select_media_from_pool = Mock(side_effect=[None, mock_media])

        result = scheduler_service._select_media("tenant-A", category="memes")

        assert scheduler_service._select_media_from_pool.call_count == 2
        calls = scheduler_service._select_media_from_pool.call_args_list
        assert calls[0][1]["category"] == "memes"
        assert calls[1][1]["category"] is None
        # Both the category pool and the any-category fallback stay tenant-scoped
        assert calls[0][0][0] == "tenant-A"
        assert calls[1][0][0] == "tenant-A"
        assert result == mock_media

    def test_select_media_no_fallback_when_no_category(self, scheduler_service):
        """Test that no fallback occurs when no category specified."""
        scheduler_service._select_media_from_pool = Mock(return_value=None)

        result = scheduler_service._select_media("tenant-A", category=None)

        scheduler_service._select_media_from_pool.assert_called_once_with(
            "tenant-A", category=None, exclude_ids=None
        )
        assert result is None


# ------------------------------------------------------------------
# Media pool (unchanged methods - kept from original tests)
# ------------------------------------------------------------------


@pytest.mark.unit
class TestSchedulerMediaPool:
    """Tests for _select_media_from_pool method."""

    @pytest.fixture
    def scheduler_service(self):
        """Create SchedulerService with mocked dependencies."""
        with patch.object(SchedulerService, "__init__", lambda self: None):
            service = SchedulerService()
            service.media_repo = Mock()
            service.queue_repo = Mock()
            service.queue_repo.count_by_status.return_value = 0
            service.queue_repo.count_recent_by_status.return_value = 0
            service.queue_repo.get_stale_unsent_pending.return_value = []
            service.lock_repo = Mock()
            service.category_mix_repo = Mock()
            service.settings_service = Mock()
            service.service_run_repo = Mock()
            service.service_name = "SchedulerService"
            service.SCHEDULE_JITTER_MINUTES = 30
            return service

    def test_select_media_from_pool_scopes_to_tenant(self, scheduler_service):
        """_select_media_from_pool scopes the repo query to chat_settings_id (#542)."""
        mock_media = Mock(category="memes", file_name="test.jpg")
        scheduler_service.media_repo.get_next_eligible_for_posting.return_value = (
            mock_media
        )

        result = scheduler_service._select_media_from_pool("tenant-A", category="memes")

        scheduler_service.media_repo.get_next_eligible_for_posting.assert_called_once_with(
            category="memes", chat_settings_id="tenant-A", exclude_ids=None
        )
        assert result == mock_media

    def test_select_media_from_pool_passes_none_category(self, scheduler_service):
        """_select_media_from_pool threads None category with the tenant scope."""
        scheduler_service.media_repo.get_next_eligible_for_posting.return_value = None

        result = scheduler_service._select_media_from_pool("tenant-A", category=None)

        scheduler_service.media_repo.get_next_eligible_for_posting.assert_called_once_with(
            category=None, chat_settings_id="tenant-A", exclude_ids=None
        )
        assert result is None

    def test_select_media_from_pool_fail_closed_without_tenant(self, scheduler_service):
        """Fail-closed: a missing tenant selects nothing, never the global pool.

        The repository tenant filter is a no-op on a None id, so an unscoped
        call would span every tenant. The guard must short-circuit before the
        query — this is the #542 cross-tenant leak.
        """
        result = scheduler_service._select_media_from_pool(None, category="memes")

        assert result is None
        scheduler_service.media_repo.get_next_eligible_for_posting.assert_not_called()


# ------------------------------------------------------------------
# Auto-approval
# ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutoApproval:
    """Tests for smart auto-approval of previously-approved media."""

    @pytest.fixture
    def scheduler_service(self):
        with patch.object(SchedulerService, "__init__", lambda self: None):
            service = SchedulerService()
            service.media_repo = Mock()
            service.queue_repo = Mock()
            service.queue_repo.count_by_status.return_value = 0
            service.queue_repo.count_recent_by_status.return_value = 0
            service.queue_repo.get_stale_unsent_pending.return_value = []
            service.history_repo = Mock()
            service.history_repo.count_posts_today.return_value = 0
            service.lock_repo = Mock()
            service.category_mix_repo = Mock()
            service.settings_service = Mock()
            service.telegram_service = AsyncMock()
            service.service_run_repo = Mock()
            service.service_name = "SchedulerService"
            service.SCHEDULE_JITTER_MINUTES = 30
            service.track_execution = mock_track_execution
            service.set_result_summary = Mock()
            return service

    async def test_auto_approves_previously_posted_media(self, scheduler_service):
        """Media with times_posted > 0 is auto-approved without Telegram."""
        media = Mock(id=uuid4(), file_name="meme.jpg", category="memes", times_posted=3)
        scheduler_service.media_repo.get_next_eligible_for_posting.return_value = media

        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item

        cs = _make_chat_settings()

        with patch("src.services.core.media_lock.MediaLockService"):
            result = await scheduler_service._select_and_send(
                cs, category=None, triggered_by="scheduler"
            )

        assert result["posted"] is True
        assert result["auto_approved"] is True
        assert result["media_file"] == "meme.jpg"
        # History should be created with auto_reapproval method
        scheduler_service.history_repo.create_idempotent.assert_called_once()
        params = scheduler_service.history_repo.create_idempotent.call_args[0][0]
        assert params.posting_method == "auto_reapproval"
        assert params.status == "posted"
        # Telegram notification should NOT have been sent
        scheduler_service.telegram_service.send_notification.assert_not_called()

    async def test_new_media_goes_to_telegram(self, scheduler_service):
        """Media with times_posted == 0 goes through normal Telegram flow."""
        media = Mock(id=uuid4(), file_name="new.jpg", category="memes", times_posted=0)
        scheduler_service.media_repo.get_next_eligible_for_posting.return_value = media

        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item
        scheduler_service.telegram_service.send_notification = AsyncMock(
            return_value=True
        )

        cs = _make_chat_settings()

        result = await scheduler_service._select_and_send(
            cs, category=None, triggered_by="scheduler"
        )

        assert result["posted"] is True
        assert "auto_approved" not in result
        scheduler_service.telegram_service.send_notification.assert_called_once()

    async def test_force_next_skips_auto_approval(self, scheduler_service):
        """Manual /next command always goes to Telegram, even for returning media."""
        media = Mock(id=uuid4(), file_name="old.jpg", category="merch", times_posted=5)
        scheduler_service.media_repo.get_next_eligible_for_posting.return_value = media

        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item
        scheduler_service.telegram_service.send_notification = AsyncMock(
            return_value=True
        )

        cs = _make_chat_settings()

        result = await scheduler_service._select_and_send(
            cs, category=None, triggered_by="telegram"
        )

        assert result["posted"] is True
        assert "auto_approved" not in result
        scheduler_service.telegram_service.send_notification.assert_called_once()

    async def test_auto_approve_creates_lock_and_history(self, scheduler_service):
        """Auto-approve creates history record, lock, and increments times_posted."""
        media = Mock(id=uuid4(), file_name="test.jpg", category="memes", times_posted=2)
        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item
        cs = _make_chat_settings()

        with patch("src.services.core.media_lock.MediaLockService") as MockLock:
            result = await scheduler_service._auto_approve(media, cs)

        assert result["posted"] is True
        assert result["auto_approved"] is True
        scheduler_service.history_repo.create_idempotent.assert_called_once()
        scheduler_service.media_repo.increment_times_posted.assert_called_once()
        MockLock.return_value.create_lock.assert_called_once()
        scheduler_service.queue_repo.delete.assert_called_once()


# ------------------------------------------------------------------
# Auto-approve Instagram posting
# ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutoApproveInstagram:
    """Tests for Instagram posting within the auto-approve flow."""

    @pytest.fixture
    def scheduler_service(self):
        with patch.object(SchedulerService, "__init__", lambda self: None):
            service = SchedulerService()
            service.media_repo = Mock()
            service.queue_repo = Mock()
            service.queue_repo.count_by_status.return_value = 0
            service.queue_repo.count_recent_by_status.return_value = 0
            service.queue_repo.get_stale_unsent_pending.return_value = []
            service.history_repo = Mock()
            service.history_repo.count_posts_today.return_value = 0
            service.lock_repo = Mock()
            service.category_mix_repo = Mock()
            service.settings_service = Mock()
            service.telegram_service = AsyncMock()
            service.service_run_repo = Mock()
            service.service_name = "SchedulerService"
            service.SCHEDULE_JITTER_MINUTES = 30
            service.track_execution = mock_track_execution
            service.set_result_summary = Mock()
            return service

    async def test_posts_to_instagram_when_enabled(self, scheduler_service):
        """Auto-approve calls Instagram API when enable_instagram_api is True."""
        media = Mock(
            id=uuid4(),
            file_name="meme.jpg",
            file_path="meme.jpg",
            category="memes",
            times_posted=3,
            source_identifier="test/meme.jpg",
            mime_type="image/jpeg",
        )
        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item
        cs = _make_chat_settings(enable_instagram_api=True)

        mock_ig = Mock()
        mock_ig.safety_check_before_post.return_value = {
            "safe_to_post": True,
            "errors": [],
        }
        mock_ig.post_story = AsyncMock(
            return_value={
                "success": True,
                "story_id": "17890012345678901",
            }
        )

        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://res.cloudinary.com/test/image/upload/v1/test.jpg",
            "public_id": "test/meme",
        }
        mock_cloud.get_story_optimized_url.return_value = (
            "https://res.cloudinary.com/test/image/upload/transformed/test.jpg"
        )

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake-bytes"

        with (
            patch("src.services.core.media_lock.MediaLockService"),
            patch(
                "src.services.integrations.instagram_api.InstagramAPIService",
                return_value=mock_ig,
            ),
            patch(
                "src.services.integrations.cloud_storage.CloudStorageService",
                return_value=mock_cloud,
            ),
            patch(
                "src.services.media_sources.factory.MediaSourceFactory"
            ) as mock_factory,
        ):
            mock_factory.get_provider_for_media_item.return_value = mock_provider
            result = await scheduler_service._auto_approve(media, cs)

        assert result["posted"] is True
        params = scheduler_service.history_repo.create_idempotent.call_args[0][0]
        assert params.posting_method == "instagram_api"
        assert params.instagram_story_id == "17890012345678901"
        mock_ig.post_story.assert_awaited_once()
        mock_cloud.delete_media.assert_called_once_with("test/meme")

    async def test_surfaces_failure_on_safety_check_failure(self, scheduler_service):
        """Returns posted=False and skips history when safety check fails."""
        media = Mock(
            id=uuid4(),
            file_name="meme.jpg",
            file_path="meme.jpg",
            category="memes",
            times_posted=3,
        )
        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item
        cs = _make_chat_settings(enable_instagram_api=True)

        mock_ig = Mock()
        mock_ig.safety_check_before_post.return_value = {
            "safe_to_post": False,
            "errors": ["No valid token"],
        }

        with (
            patch("src.services.core.media_lock.MediaLockService") as mock_lock_cls,
            patch(
                "src.services.integrations.instagram_api.InstagramAPIService",
                return_value=mock_ig,
            ),
            patch("src.services.integrations.cloud_storage.CloudStorageService"),
            patch("src.services.media_sources.factory.MediaSourceFactory"),
        ):
            result = await scheduler_service._auto_approve(media, cs)

        assert result["posted"] is False
        assert result["error"] == "Instagram API posting failed"
        scheduler_service.history_repo.create_idempotent.assert_not_called()
        scheduler_service.media_repo.increment_times_posted.assert_not_called()
        mock_lock_cls.return_value.create_lock.assert_not_called()

    async def test_surfaces_failure_on_instagram_api_error(self, scheduler_service):
        """Returns posted=False and skips history when Instagram API raises."""
        from src.exceptions.instagram import InstagramAPIError

        media = Mock(
            id=uuid4(),
            file_name="meme.jpg",
            file_path="meme.jpg",
            category="memes",
            times_posted=3,
            source_identifier="test/meme.jpg",
            mime_type="image/jpeg",
        )
        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item
        cs = _make_chat_settings(enable_instagram_api=True)

        mock_ig = Mock()
        mock_ig.safety_check_before_post.return_value = {
            "safe_to_post": True,
            "errors": [],
        }
        mock_ig.post_story = AsyncMock(
            side_effect=InstagramAPIError("Container failed")
        )

        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://example.com/img.jpg",
            "public_id": "test/meme",
        }
        mock_cloud.get_story_optimized_url.return_value = "https://example.com/img.jpg"

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake-bytes"

        with (
            patch("src.services.core.media_lock.MediaLockService") as mock_lock_cls,
            patch(
                "src.services.integrations.instagram_api.InstagramAPIService",
                return_value=mock_ig,
            ),
            patch(
                "src.services.integrations.cloud_storage.CloudStorageService",
                return_value=mock_cloud,
            ),
            patch(
                "src.services.media_sources.factory.MediaSourceFactory"
            ) as mock_factory,
        ):
            mock_factory.get_provider_for_media_item.return_value = mock_provider
            result = await scheduler_service._auto_approve(media, cs)

        assert result["posted"] is False
        assert result["error"] == "Instagram API posting failed"
        scheduler_service.history_repo.create_idempotent.assert_not_called()
        scheduler_service.media_repo.increment_times_posted.assert_not_called()
        mock_lock_cls.return_value.create_lock.assert_not_called()
        mock_cloud.delete_media.assert_called_once_with("test/meme")

    async def test_skips_instagram_when_disabled(self, scheduler_service):
        """Does not attempt Instagram posting when enable_instagram_api is False."""
        media = Mock(
            id=uuid4(),
            file_name="meme.jpg",
            category="memes",
            times_posted=3,
        )
        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item
        cs = _make_chat_settings(enable_instagram_api=False)

        with patch("src.services.core.media_lock.MediaLockService"):
            result = await scheduler_service._auto_approve(media, cs)

        assert result["posted"] is True
        params = scheduler_service.history_repo.create_idempotent.call_args[0][0]
        assert params.posting_method == "auto_reapproval"

    async def test_skips_instagram_in_dry_run(self, scheduler_service):
        """Does not post to Instagram when dry_run_mode is True."""
        media = Mock(
            id=uuid4(),
            file_name="meme.jpg",
            category="memes",
            times_posted=3,
        )
        queue_item = Mock(id=uuid4())
        scheduler_service.queue_repo.create.return_value = queue_item
        cs = _make_chat_settings(enable_instagram_api=True, dry_run_mode=True)

        with patch("src.services.core.media_lock.MediaLockService"):
            result = await scheduler_service._auto_approve(media, cs)

        assert result["posted"] is True
        params = scheduler_service.history_repo.create_idempotent.call_args[0][0]
        assert params.posting_method == "auto_reapproval"


# ------------------------------------------------------------------
# Catch-up after restart (#349)
# ------------------------------------------------------------------


@pytest.mark.unit
class TestCatchupAfterRestart:
    """Tests for scheduler catch-up logic when behind after restart."""

    def test_no_catchup_when_on_schedule(self, scheduler_service_mocked):
        """Returns None when last post is within one interval."""
        service = scheduler_service_mocked
        # Window 9-21 = 12h, 3 PPD => interval = 4h = 14400s
        last_sent = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # 3h since last post, interval is 4h — not behind
            mock_dt.now.return_value = datetime(2026, 3, 21, 13, 0, tzinfo=timezone.utc)
            result = service._compute_catchup_sent_at(cs)

        assert result is None

    def test_no_catchup_when_exactly_one_interval(self, scheduler_service_mocked):
        """Returns None when exactly one interval has passed (normal fire)."""
        service = scheduler_service_mocked
        # interval = 4h
        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # Exactly 4h since last — one interval, not two
            mock_dt.now.return_value = datetime(2026, 3, 21, 13, 0, tzinfo=timezone.utc)
            result = service._compute_catchup_sent_at(cs)

        assert result is None

    def test_catchup_when_behind_two_intervals(self, scheduler_service_mocked):
        """Returns last_sent + interval when behind by >= 2 intervals."""
        service = scheduler_service_mocked
        from datetime import timedelta

        # interval = 4h
        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # 9h since last post = behind by 2+ intervals (9h / 4h = 2.25)
            mock_dt.now.return_value = datetime(2026, 3, 21, 18, 0, tzinfo=timezone.utc)
            result = service._compute_catchup_sent_at(cs)

        assert result == last_sent + timedelta(hours=4)

    def test_catchup_advances_by_one_interval_only(self, scheduler_service_mocked):
        """Even when behind by many slots, advances by exactly one interval."""
        service = scheduler_service_mocked
        from datetime import timedelta

        # 13h window, 15 PPD => interval = 3120s = 52 min
        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=22,
            posts_per_day=15,
            last_post_sent_at=last_sent,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # 3h later = behind by ~3.46 intervals
            mock_dt.now.return_value = datetime(2026, 3, 21, 12, 0, tzinfo=timezone.utc)
            result = service._compute_catchup_sent_at(cs)

        expected = last_sent + timedelta(seconds=3120)
        assert result == expected

    def test_no_catchup_when_last_sent_is_none(self, scheduler_service_mocked):
        """Returns None when last_post_sent_at is None (first post ever)."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(last_post_sent_at=None)

        result = service._compute_catchup_sent_at(cs)

        assert result is None

    @pytest.mark.asyncio
    async def test_process_slot_passes_catchup_override(self, scheduler_service_mocked):
        """process_slot passes sent_at_override to _select_and_send during catchup."""
        service = scheduler_service_mocked
        from datetime import timedelta

        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            is_paused=False,
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )
        service.settings_service.get_settings.return_value = cs
        service._select_and_send = AsyncMock(return_value={"posted": True})

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 21, 18, 0, tzinfo=timezone.utc)
            service.category_mix_repo.get_current_mix_as_dict.return_value = {}
            await service.process_slot(telegram_chat_id=-100123)

        call_kwargs = service._select_and_send.call_args.kwargs
        assert call_kwargs["sent_at_override"] == last_sent + timedelta(hours=4)

    @pytest.mark.asyncio
    async def test_process_slot_no_override_when_on_schedule(
        self, scheduler_service_mocked
    ):
        """process_slot passes sent_at_override=None when not catching up."""
        service = scheduler_service_mocked
        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            is_paused=False,
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )
        service.settings_service.get_settings.return_value = cs
        service._select_and_send = AsyncMock(return_value={"posted": True})

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # 5h since last, interval is 4h — one interval overdue, not two
            mock_dt.now.return_value = datetime(2026, 3, 21, 14, 0, tzinfo=timezone.utc)
            service.category_mix_repo.get_current_mix_as_dict.return_value = {}
            await service.process_slot(telegram_chat_id=-100123)

        call_kwargs = service._select_and_send.call_args.kwargs
        assert call_kwargs["sent_at_override"] is None

    @pytest.mark.asyncio
    async def test_catchup_uses_override_for_last_post_sent_at(
        self, scheduler_service_mocked
    ):
        """When catching up, last_post_sent_at is set to override, not now."""
        service = scheduler_service_mocked

        override_time = datetime(2026, 3, 21, 13, 0, tzinfo=timezone.utc)

        media = Mock(
            id=uuid4(), file_name="catch.jpg", category="memes", times_posted=0
        )
        service.media_repo.get_next_eligible_for_posting.return_value = media
        queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = queue_item
        service.telegram_service.send_notification = AsyncMock(return_value=True)

        cs = _make_chat_settings()

        await service._select_and_send(
            cs,
            category=None,
            triggered_by="scheduler",
            sent_at_override=override_time,
        )

        service.settings_service.update_last_post_sent_at.assert_called_once_with(
            cs.telegram_chat_id, override_time
        )

    @pytest.mark.asyncio
    async def test_auto_approve_uses_catchup_override(self, scheduler_service_mocked):
        """Auto-approved posts during catchup use the override timestamp."""
        service = scheduler_service_mocked
        service.history_repo = Mock()
        service.history_repo.count_posts_today.return_value = 0
        override_time = datetime(2026, 3, 21, 13, 0, tzinfo=timezone.utc)

        media = Mock(
            id=uuid4(), file_name="repost.jpg", category="memes", times_posted=3
        )
        queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = queue_item
        cs = _make_chat_settings()

        with patch("src.services.core.media_lock.MediaLockService"):
            await service._auto_approve(media, cs, sent_at_override=override_time)

        service.settings_service.update_last_post_sent_at.assert_called_once_with(
            cs.telegram_chat_id, override_time
        )


# ------------------------------------------------------------------
# First-tick immediate post after startup (#348)
# ------------------------------------------------------------------


@pytest.mark.unit
class TestFirstTickImmediatePost:
    """Tests for first-tick-after-startup behavior that resets to now."""

    def test_first_tick_returns_none_when_behind(self, scheduler_service_mocked):
        """On first tick, _compute_catchup_sent_at returns None (use now)."""
        service = scheduler_service_mocked
        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # 9h since last = behind by 2+ intervals
            mock_dt.now.return_value = datetime(2026, 3, 21, 18, 0, tzinfo=timezone.utc)
            result = service._compute_catchup_sent_at(cs, first_tick=True)

        assert result is None

    def test_subsequent_tick_advances_gradually(self, scheduler_service_mocked):
        """On non-first tick, still advances by one interval (PR #354 behavior)."""
        service = scheduler_service_mocked
        from datetime import timedelta

        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 21, 18, 0, tzinfo=timezone.utc)
            result = service._compute_catchup_sent_at(cs, first_tick=False)

        assert result == last_sent + timedelta(hours=4)

    def test_first_tick_no_effect_when_on_schedule(self, scheduler_service_mocked):
        """first_tick=True doesn't change behavior when not behind."""
        service = scheduler_service_mocked
        last_sent = datetime(2026, 3, 21, 10, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 21, 13, 0, tzinfo=timezone.utc)
            result = service._compute_catchup_sent_at(cs, first_tick=True)

        assert result is None

    @pytest.mark.asyncio
    async def test_process_slot_first_tick_no_override(self, scheduler_service_mocked):
        """process_slot with first_tick=True passes sent_at_override=None."""
        service = scheduler_service_mocked
        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            is_paused=False,
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )
        service.settings_service.get_settings.return_value = cs
        service._select_and_send = AsyncMock(return_value={"posted": True})

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            # Behind by 2+ intervals
            mock_dt.now.return_value = datetime(2026, 3, 21, 18, 0, tzinfo=timezone.utc)
            service.category_mix_repo.get_current_mix_as_dict.return_value = {}
            await service.process_slot(telegram_chat_id=-100123, first_tick=True)

        call_kwargs = service._select_and_send.call_args.kwargs
        # None means "use now" — immediate post, not gradual advance
        assert call_kwargs["sent_at_override"] is None

    @pytest.mark.asyncio
    async def test_process_slot_second_tick_advances(self, scheduler_service_mocked):
        """process_slot without first_tick still advances gradually."""
        service = scheduler_service_mocked
        from datetime import timedelta

        last_sent = datetime(2026, 3, 21, 9, 0, tzinfo=timezone.utc)
        cs = _make_chat_settings(
            is_paused=False,
            posting_hours_start=9,
            posting_hours_end=21,
            posts_per_day=3,
            last_post_sent_at=last_sent,
        )
        service.settings_service.get_settings.return_value = cs
        service._select_and_send = AsyncMock(return_value={"posted": True})

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 21, 18, 0, tzinfo=timezone.utc)
            service.category_mix_repo.get_current_mix_as_dict.return_value = {}
            await service.process_slot(telegram_chat_id=-100123, first_tick=False)

        call_kwargs = service._select_and_send.call_args.kwargs
        assert call_kwargs["sent_at_override"] == last_sent + timedelta(hours=4)

    def test_first_tick_no_effect_when_last_sent_none(self, scheduler_service_mocked):
        """first_tick doesn't matter when last_post_sent_at is None."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(last_post_sent_at=None)

        result = service._compute_catchup_sent_at(cs, first_tick=True)

        assert result is None


# ------------------------------------------------------------------
# Claim-before-publish: crash-replay safety for the auto-approve IG path (#549)
# ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutoApproveClaimBeforePublish:
    """The scheduler auto-approve IG path must claim the row into 'publishing'
    and persist the container_id BEFORE publishing, so a crash/redeploy after
    the publish can't re-serve the media and duplicate the story."""

    @pytest.fixture
    def scheduler_service(self):
        with patch.object(SchedulerService, "__init__", lambda self: None):
            service = SchedulerService()
            service.media_repo = Mock()
            service.queue_repo = Mock()
            service.queue_repo.count_by_status.return_value = 0
            service.queue_repo.count_recent_by_status.return_value = 0
            service.queue_repo.get_stale_unsent_pending.return_value = []
            service.history_repo = Mock()
            service.history_repo.count_posts_today.return_value = 0
            service.lock_repo = Mock()
            service.settings_service = Mock()
            service.track_execution = mock_track_execution
            service.set_result_summary = Mock()
            return service

    def _media(self):
        return Mock(
            id=uuid4(),
            file_name="meme.jpg",
            file_path="meme.jpg",
            category="memes",
            times_posted=3,
        )

    @staticmethod
    def _ig_container_then(story_id):
        """Fake _auto_approve_instagram that creates a container (fires the
        callback) and then resolves to `story_id` (str=success, None=unknown)."""

        async def _fake(media, cs, on_container_created=None):
            if on_container_created is not None:
                on_container_created("container-xyz")
            return story_id

        return AsyncMock(side_effect=_fake)

    async def test_crash_before_finalize_leaves_row_publishing(self, scheduler_service):
        """Container persisted + publish succeeded, then the finalize crashes.
        The 'publishing' row must survive (never deleted) so the next selection
        pass is blocked and the story is not re-published."""
        service = scheduler_service
        queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = queue_item
        service._auto_approve_instagram = self._ig_container_then("story-999")
        # Simulate the crash: the atomic finalize raises mid-bookkeeping.
        service.history_repo.create_idempotent.side_effect = RuntimeError("crash")

        cs = _make_chat_settings(enable_instagram_api=True)

        with patch("src.services.core.media_lock.MediaLockService"):
            with pytest.raises(RuntimeError, match="crash"):
                await service._auto_approve(self._media(), cs)

        # Row was claimed into 'publishing' with the container anchor…
        service.queue_repo.mark_publishing.assert_called_once_with(
            str(queue_item.id), "container-xyz"
        )
        # …and must NOT be deleted — it stays stuck, blocking reselection.
        service.queue_repo.delete.assert_not_called()

    async def test_ambiguous_publish_stays_stuck(self, scheduler_service):
        """Container created but the publish outcome is unknown (no story_id):
        the row stays 'publishing' (stuck), is not deleted, not re-published,
        and writes no success history."""
        service = scheduler_service
        queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = queue_item
        service._auto_approve_instagram = self._ig_container_then(None)

        cs = _make_chat_settings(enable_instagram_api=True)

        with patch("src.services.core.media_lock.MediaLockService"):
            result = await service._auto_approve(self._media(), cs)

        assert result["posted"] is False
        service.queue_repo.mark_publishing.assert_called_once_with(
            str(queue_item.id), "container-xyz"
        )
        service.queue_repo.delete.assert_not_called()  # stuck, not released
        service.history_repo.create_idempotent.assert_not_called()  # no success row
        service._auto_approve_instagram.assert_awaited_once()  # not re-published

    async def test_safe_retry_when_container_never_created(self, scheduler_service):
        """Failure BEFORE a container exists (nothing published): the transient
        row is released (deleted) and the media stays eligible for retry."""
        service = scheduler_service
        queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = queue_item

        async def _fail_pre_container(media, cs, on_container_created=None):
            return None  # never fired the container callback

        service._auto_approve_instagram = AsyncMock(side_effect=_fail_pre_container)

        cs = _make_chat_settings(enable_instagram_api=True)

        with patch("src.services.core.media_lock.MediaLockService"):
            result = await service._auto_approve(self._media(), cs)

        assert result["posted"] is False
        service.queue_repo.mark_publishing.assert_not_called()
        service.queue_repo.delete.assert_called_once_with(str(queue_item.id))
        service.history_repo.create_idempotent.assert_not_called()

    @staticmethod
    def _ig_container_then_raises(exc):
        """Fake _auto_approve_instagram that creates a container (fires the
        callback, persisting the anchor) and THEN raises `exc` — mirrors
        post_story propagating an IG failure after the claim-before-publish
        write."""

        async def _fake(media, cs, on_container_created=None):
            if on_container_created is not None:
                on_container_created("container-xyz")
            raise exc

        return AsyncMock(side_effect=_fake)

    @pytest.mark.parametrize("status_code", ["ERROR", "EXPIRED"])
    async def test_confirmed_dead_container_released_for_retry(
        self, scheduler_service, status_code
    ):
        """IG affirmatively confirms the container failed (status_code
        ERROR/EXPIRED) → nothing published → the claimed 'publishing' row is
        RELEASED (deleted, same path as 'no container'), NOT stranded forever
        (rajan #564 finding 1)."""
        from src.exceptions.instagram import InstagramAPIError

        service = scheduler_service
        queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = queue_item
        service._auto_approve_instagram = self._ig_container_then_raises(
            InstagramAPIError("Media container failed", error_code=status_code)
        )

        cs = _make_chat_settings(enable_instagram_api=True)

        with patch("src.services.core.media_lock.MediaLockService"):
            result = await service._auto_approve(self._media(), cs)

        assert result["posted"] is False
        # The row was claimed the instant the container existed…
        service.queue_repo.mark_publishing.assert_called_once_with(
            str(queue_item.id), "container-xyz"
        )
        # …but IG confirmed it's dead, so it is released — not left stuck.
        service.queue_repo.delete.assert_called_once_with(str(queue_item.id))
        service.history_repo.create_idempotent.assert_not_called()

    async def test_ambiguous_raise_after_container_stays_stuck(self, scheduler_service):
        """A post-container failure that is NOT IG-confirmed (a crash/timeout
        whose publish outcome is unknown) STILL holds the row in 'publishing' —
        the ambiguous-stays-stuck behavior must not regress."""
        from src.exceptions.instagram import InstagramAPIError

        service = scheduler_service
        queue_item = Mock(id=uuid4())
        service.queue_repo.create.return_value = queue_item
        # No error_code → ambiguous (e.g. the 180s wall-clock timeout wrapper).
        service._auto_approve_instagram = self._ig_container_then_raises(
            InstagramAPIError("Instagram post timed out after 180 seconds")
        )

        cs = _make_chat_settings(enable_instagram_api=True)

        with patch("src.services.core.media_lock.MediaLockService"):
            result = await service._auto_approve(self._media(), cs)

        assert result["posted"] is False
        service.queue_repo.mark_publishing.assert_called_once_with(
            str(queue_item.id), "container-xyz"
        )
        service.queue_repo.delete.assert_not_called()  # stuck, not released
        service.history_repo.create_idempotent.assert_not_called()
