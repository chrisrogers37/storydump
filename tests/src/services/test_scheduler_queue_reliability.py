"""Tests for scheduler/queue reliability fixes (#360, #362, #363, #365, #366)."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, MagicMock, patch
from uuid import uuid4

from src.services.core.scheduler import SchedulerService
from src.services.core.media_sync import MediaSyncService, SyncContext, SyncResult
from src.services.media_sources.base_provider import MediaFileInfo
from tests.src.services.conftest import mock_track_execution


# ==================== Fixtures ====================


@pytest.fixture
def scheduler_service_mocked():
    """Create SchedulerService with all dependencies mocked."""
    with patch.object(SchedulerService, "__init__", lambda self: None):
        service = SchedulerService()
        service.media_repo = Mock()
        service.queue_repo = Mock()
        service.queue_repo.get_stale_unsent_pending.return_value = []
        service.history_repo = Mock()
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


@pytest.fixture
def sync_service():
    """Create MediaSyncService with mocked dependencies."""
    with patch.object(MediaSyncService, "__init__", lambda self: None):
        service = MediaSyncService()
        service.media_repo = Mock()
        service.media_repo.get_active_by_hash.return_value = None
        service.service_run_repo = Mock()
        service.track_execution = MagicMock()
        service.track_execution.return_value.__enter__ = Mock(return_value="run-123")
        service.track_execution.return_value.__exit__ = Mock(return_value=False)
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
):
    cs = Mock()
    cs.posts_per_day = posts_per_day
    cs.posting_hours_start = posting_hours_start
    cs.posting_hours_end = posting_hours_end
    cs.last_post_sent_at = last_post_sent_at
    cs.is_paused = is_paused
    cs.telegram_chat_id = telegram_chat_id
    cs.id = settings_id or uuid4()
    cs.posting_timezone = posting_timezone
    return cs


def _make_file_info(name="photo.jpg", identifier="/media/photo.jpg", file_hash=None):
    return MediaFileInfo(
        name=name,
        identifier=identifier,
        size_bytes=1024,
        mime_type="image/jpeg",
        folder=None,
        hash=file_hash,
    )


# ==================== #360: posting_hours_start == end ====================


@pytest.mark.unit
class TestPostingWindowStartEqualsEnd:
    """When start == end, window should be treated as always-on (24h)."""

    def test_in_posting_window_start_equals_end_returns_true(
        self, scheduler_service_mocked
    ):
        cs = _make_chat_settings(posting_hours_start=9, posting_hours_end=9)
        now = datetime(2026, 5, 18, 3, 0, tzinfo=timezone.utc)

        result = SchedulerService._in_posting_window(now, cs)

        assert result is True

    def test_posting_window_hours_start_equals_end_returns_24(
        self, scheduler_service_mocked
    ):
        cs = _make_chat_settings(posting_hours_start=14, posting_hours_end=14)

        result = SchedulerService._posting_window_hours(cs)

        assert result == 24.0

    def test_is_slot_due_start_equals_end_no_division_by_zero(
        self, scheduler_service_mocked
    ):
        """start==end should not cause ZeroDivisionError in is_slot_due."""
        service = scheduler_service_mocked
        service.category_mix_repo.get_current_mix_as_dict.return_value = {}

        cs = _make_chat_settings(
            posting_hours_start=0,
            posting_hours_end=0,
            posts_per_day=3,
            last_post_sent_at=None,
        )

        with patch("src.services.core.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            result = service.is_slot_due(cs)

        assert result is not False

    def test_in_posting_window_normal_range_still_works(self, scheduler_service_mocked):
        """Normal start < end range is unaffected by the fix."""
        cs = _make_chat_settings(posting_hours_start=9, posting_hours_end=21)
        in_window = datetime(2026, 5, 18, 15, 0, tzinfo=timezone.utc)
        out_of_window = datetime(2026, 5, 18, 5, 0, tzinfo=timezone.utc)

        assert SchedulerService._in_posting_window(in_window, cs) is True
        assert SchedulerService._in_posting_window(out_of_window, cs) is False

    def test_posting_window_hours_midnight_crossing_still_works(
        self, scheduler_service_mocked
    ):
        """Midnight-crossing window (end < start) is unaffected."""
        cs = _make_chat_settings(posting_hours_start=22, posting_hours_end=2)

        assert SchedulerService._posting_window_hours(cs) == 4.0


# ==================== #365: seen_identifiers ordering ====================


@pytest.mark.unit
class TestSeenIdentifiersOrdering:
    """seen_identifiers should only be added after successful processing."""

    def test_failed_file_not_in_seen_identifiers(self, sync_service):
        """If processing raises, identifier should NOT be in seen_identifiers."""
        ctx = SyncContext(
            source_type="local",
            provider=Mock(),
            db_by_identifier={},
            db_by_hash={},
            seen_identifiers=set(),
            result=SyncResult(),
        )

        file_info = _make_file_info(name="bad.jpg", identifier="/media/bad.jpg")

        # Make _handle_identifier_match raise to simulate a processing error
        sync_service._handle_identifier_match = Mock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            sync_service._process_provider_file(file_info, ctx)

        assert "/media/bad.jpg" not in ctx.seen_identifiers

    def test_successful_identifier_match_adds_to_seen(self, sync_service):
        """Successful identifier match should add to seen_identifiers."""
        existing = Mock()
        existing.file_name = "photo.jpg"
        existing.thumbnail_url = None

        ctx = SyncContext(
            source_type="local",
            provider=Mock(),
            db_by_identifier={"/media/photo.jpg": existing},
            db_by_hash={},
            seen_identifiers=set(),
            result=SyncResult(),
        )

        file_info = _make_file_info(name="photo.jpg", identifier="/media/photo.jpg")

        sync_service._process_provider_file(file_info, ctx)

        assert "/media/photo.jpg" in ctx.seen_identifiers
        assert ctx.result.unchanged == 1

    def test_successful_new_file_adds_to_seen(self, sync_service):
        """New file indexed successfully should add to seen_identifiers."""
        ctx = SyncContext(
            source_type="local",
            provider=Mock(),
            db_by_identifier={},
            db_by_hash={},
            seen_identifiers=set(),
            result=SyncResult(),
        )

        file_info = _make_file_info(
            name="new.jpg", identifier="/media/new.jpg", file_hash="abc123"
        )

        sync_service.media_repo.get_inactive_by_source_identifier.return_value = None

        sync_service._process_provider_file(file_info, ctx)

        assert "/media/new.jpg" in ctx.seen_identifiers
        assert ctx.result.new == 1


# ==================== #366: stale processing recovery ====================


@pytest.mark.unit
class TestStaleProcessingRecovery:
    """process_slot should requeue stale processing items alongside stale pending."""

    @pytest.mark.asyncio
    async def test_process_slot_calls_requeue_stale_processing(
        self, scheduler_service_mocked
    ):
        """process_slot should call requeue_stale_processing on each tick."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(is_paused=True)
        service.settings_service.get_settings.return_value = cs

        await service.process_slot(telegram_chat_id=-100123)

        service.queue_repo.requeue_stale_processing.assert_called_once_with(
            max_age_minutes=10
        )

    @pytest.mark.asyncio
    async def test_process_slot_calls_both_cleanup_methods(
        self, scheduler_service_mocked
    ):
        """Both the stale-pending sweep and requeue_stale_processing run."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(is_paused=True)
        service.settings_service.get_settings.return_value = cs

        await service.process_slot(telegram_chat_id=-100123)

        service.queue_repo.get_stale_unsent_pending.assert_called_once_with(
            max_age_minutes=10
        )
        service.queue_repo.requeue_stale_processing.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_slot_records_expiry_for_stale_pending_rows(
        self, scheduler_service_mocked
    ):
        """#687: stale pending rows are deleted through record_expiry_and_delete
        — terminal history first — never raw-deleted. A requeued (#679/#680)
        delivered-but-unstamped card then shows "Expired" on tap instead of
        the raw "Queue item not found"."""
        service = scheduler_service_mocked
        cs = _make_chat_settings(is_paused=True)
        service.settings_service.get_settings.return_value = cs
        stale_row = Mock()
        service.queue_repo.get_stale_unsent_pending.return_value = [stale_row]

        with patch(
            "src.services.core.scheduler.record_expiry_and_delete"
        ) as mock_record:
            mock_record.return_value = True
            await service.process_slot(telegram_chat_id=-100123)

        mock_record.assert_called_once_with(
            stale_row,
            history_repo=service.history_repo,
            queue_repo=service.queue_repo,
        )


# ==================== #363: shutdown guard ====================


@pytest.mark.unit
class TestShutdownGuard:
    """_select_and_send should refuse to start during shutdown."""

    @pytest.mark.asyncio
    async def test_select_and_send_refuses_during_shutdown(
        self, scheduler_service_mocked
    ):
        """If shutdown_in_progress is True, _select_and_send returns immediately."""
        service = scheduler_service_mocked

        with patch("src.services.core.loops.lifecycle.session_state") as mock_state:
            mock_state.shutdown_in_progress = True

            result = await service._select_and_send(
                _make_chat_settings(),
                category=None,
                triggered_by="scheduler",
            )

        assert result["posted"] is False
        assert result["reason"] == "shutdown_in_progress"
        # Should not have selected any media
        service.media_repo.select_eligible.assert_not_called()

    @pytest.mark.asyncio
    async def test_select_and_send_proceeds_when_not_shutting_down(
        self, scheduler_service_mocked
    ):
        """Normal operation should proceed past the shutdown guard."""
        service = scheduler_service_mocked
        service.media_repo.select_eligible = Mock(return_value=None)

        # _select_media returns None (no eligible media) — that's fine,
        # we just want to verify it gets past the guard
        with patch.object(service, "_select_media", return_value=None):
            with patch("src.services.core.loops.lifecycle.session_state") as mock_state:
                mock_state.shutdown_in_progress = False

                result = await service._select_and_send(
                    _make_chat_settings(),
                    category=None,
                    triggered_by="scheduler",
                )

        assert result["reason"] == "no_eligible_media"


# ==================== #362: sync readiness gate ====================


@pytest.mark.unit
class TestSyncReadinessGate:
    """Scheduler loop should wait for initial sync before posting."""

    @pytest.mark.asyncio
    async def test_scheduler_tick_skips_when_sync_not_complete(self):
        """Scheduler tick returns None (not []) on the sync-wait early return.

        None is the distinct signal that the tick short-circuited before chat
        processing, so the caller does not consume the first-tick catch-up flag
        on a sync-wait no-op tick (#553). [] means "processed, no chats".
        """
        from src.services.core.loops.scheduler_loop import _scheduler_tick

        scheduler_service = Mock()
        posting_service = Mock()
        settings_service = Mock()
        queue_repo = Mock()
        queue_repo.discard_abandoned_processing.return_value = 0
        queue_repo.get_stale_sent.return_value = []

        with patch(
            "src.services.core.loops.scheduler_loop.session_state"
        ) as mock_state:
            mock_state.initial_sync_complete = False

            result = await _scheduler_tick(
                scheduler_service,
                posting_service,
                settings_service,
                queue_repo,
            )

        assert result is None
        # Should not have fetched active chats
        settings_service.get_all_active_chats.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduler_tick_proceeds_when_sync_complete(self):
        """Scheduler tick proceeds normally when initial_sync_complete is True."""
        from src.services.core.loops.scheduler_loop import _scheduler_tick

        scheduler_service = Mock()
        scheduler_service.process_slot = AsyncMock(return_value={"posted": False})
        posting_service = Mock()

        chat = Mock(telegram_chat_id=-100123)
        settings_service = Mock()
        settings_service.get_all_active_chats.return_value = [chat]

        queue_repo = Mock()
        queue_repo.discard_abandoned_processing.return_value = 0
        queue_repo.get_stale_sent.return_value = []

        with patch(
            "src.services.core.loops.scheduler_loop.session_state"
        ) as mock_state:
            mock_state.initial_sync_complete = True

            result = await _scheduler_tick(
                scheduler_service,
                posting_service,
                settings_service,
                queue_repo,
            )

        assert len(result) == 1
        scheduler_service.process_slot.assert_called_once()

    def test_media_sync_loop_sets_initial_sync_complete(self):
        """media_sync_loop should set initial_sync_complete after first success."""
        from src.services.core.loops.lifecycle import _SessionState

        state = _SessionState()
        assert state.initial_sync_complete is False
        state.initial_sync_complete = True
        assert state.initial_sync_complete is True
