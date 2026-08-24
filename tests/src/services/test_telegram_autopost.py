"""Tests for TelegramAutopostHandler."""

import asyncio
import time

import pytest
from unittest.mock import Mock, patch, AsyncMock
from uuid import uuid4
import threading

from src.repositories.tenant_scope import SYSTEM_SCOPE
from src.services.core.telegram_autopost import (
    AutopostContext,
    TelegramAutopostHandler,
)


async def _await_background_tasks(handler):
    """Wait for all background autopost tasks to complete."""
    if handler._background_tasks:
        await asyncio.gather(*handler._background_tasks, return_exceptions=True)


@pytest.fixture
def mock_autopost_handler(mock_telegram_service):
    """Create TelegramAutopostHandler from shared mock_telegram_service."""
    handler = TelegramAutopostHandler(mock_telegram_service)
    yield handler


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostQueueItemNotFound:
    """Tests for autopost when queue/media items are missing."""

    async def test_autopost_queue_item_not_found(self, mock_autopost_handler):
        """Test that autopost handles missing queue item gracefully."""
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        service.queue_repo.claim_for_processing.return_value = None
        service.queue_repo.get_by_id.return_value = None
        service.history_repo.get_by_queue_item_id.return_value = None

        mock_user = Mock()
        mock_user.id = uuid4()
        mock_query = AsyncMock()

        await handler.handle_autopost(queue_id, mock_user, mock_query)

        mock_query.edit_message_caption.assert_called_once()
        call_args = mock_query.edit_message_caption.call_args
        assert "not found" in str(call_args).lower()

    async def test_autopost_media_item_not_found(self, mock_autopost_handler):
        """Test that autopost handles missing media item gracefully."""
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        mock_queue_item = Mock()
        mock_queue_item.media_item_id = uuid4()
        service.queue_repo.claim_for_processing.return_value = mock_queue_item
        service.queue_repo.get_by_id.return_value = mock_queue_item
        service.media_repo.get_by_id.return_value = None

        mock_user = Mock()
        mock_user.id = uuid4()
        mock_query = AsyncMock()

        await handler.handle_autopost(queue_id, mock_user, mock_query)

        # Should have been called at least once with "not found"
        found_not_found = False
        for call in mock_query.edit_message_caption.call_args_list:
            if "not found" in str(call).lower():
                found_not_found = True
                break
        assert found_not_found


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostSafetyGates:
    """Tests for safety check enforcement."""

    async def test_autopost_safety_check_failure_blocks_posting(
        self, mock_autopost_handler
    ):
        """Test that a failed safety check prevents posting."""
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        mock_queue_item = Mock()
        mock_queue_item.media_item_id = uuid4()
        service.queue_repo.get_by_id.return_value = mock_queue_item

        mock_media = Mock()
        mock_media.file_path = "/test/story.jpg"
        mock_media.file_name = "story.jpg"
        service.media_repo.get_by_id.return_value = mock_media

        mock_user = Mock()
        mock_user.id = uuid4()

        mock_query = AsyncMock()
        mock_query.message = Mock(chat_id=-100123, message_id=1)

        mock_chat_settings = Mock()
        mock_chat_settings.dry_run_mode = False
        mock_chat_settings.posts_per_day = 99
        mock_chat_settings.posting_timezone = None
        service.settings_service.get_settings.return_value = mock_chat_settings

        with (
            patch(
                "src.services.integrations.instagram_api.InstagramAPIService"
            ) as mock_ig_class,
            patch(
                "src.services.integrations.cloud_storage.CloudStorageService"
            ) as mock_cloud_class,
        ):
            mock_ig_instance = mock_ig_class.return_value
            mock_ig_instance.safety_check_before_post.return_value = {
                "safe_to_post": False,
                "errors": ["Token expired", "Rate limit exceeded"],
            }
            mock_ig_instance.close = Mock()
            mock_cloud_instance = mock_cloud_class.return_value
            mock_cloud_instance.close = Mock()

            await handler.handle_autopost(queue_id, mock_user, mock_query)
            await _await_background_tasks(handler)

        # Should show safety check failure message
        found_safety_fail = False
        for call in mock_query.edit_message_caption.call_args_list:
            if "SAFETY CHECK FAILED" in str(call):
                found_safety_fail = True
                break
        assert found_safety_fail

        # Should NOT create history or delete from queue
        service.history_repo.create.assert_not_called()
        service.queue_repo.delete.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostDryRun:
    """Tests for dry-run mode behavior."""

    async def test_dry_run_uploads_to_cloudinary_but_skips_instagram(
        self, mock_autopost_handler
    ):
        """Test that dry-run mode uploads to Cloudinary but stops before Instagram API."""
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        mock_queue_item = Mock()
        mock_queue_item.media_item_id = uuid4()
        service.queue_repo.get_by_id.return_value = mock_queue_item

        mock_media = Mock()
        mock_media.id = uuid4()
        mock_media.file_path = "/test/story.jpg"
        mock_media.file_name = "story.jpg"
        mock_media.source_type = "local"
        mock_media.source_identifier = "/test/story.jpg"
        mock_media.mime_type = "image/jpeg"
        service.media_repo.get_by_id.return_value = mock_media

        mock_chat_settings = Mock()
        mock_chat_settings.dry_run_mode = True
        mock_chat_settings.posts_per_day = 99
        mock_chat_settings.posting_timezone = None
        service.settings_service.get_settings.return_value = mock_chat_settings

        mock_user = Mock()
        mock_user.id = uuid4()
        mock_user.telegram_username = "tester"
        mock_user.telegram_first_name = "Test"

        mock_query = AsyncMock()
        mock_query.message = Mock(chat_id=-100123, message_id=1)

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake image bytes"

        with (
            patch(
                "src.services.integrations.instagram_api.InstagramAPIService"
            ) as mock_ig_class,
            patch(
                "src.services.integrations.cloud_storage.CloudStorageService"
            ) as mock_cloud_class,
            patch(
                "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
                return_value=mock_provider,
            ),
        ):
            mock_ig_instance = mock_ig_class.return_value
            mock_ig_instance.safety_check_before_post.return_value = {
                "safe_to_post": True,
                "errors": [],
            }
            mock_ig_instance.get_account_info = AsyncMock(
                return_value={"username": "testaccount"}
            )
            mock_ig_instance.close = Mock()

            mock_cloud_instance = mock_cloud_class.return_value
            mock_cloud_instance.upload_media.return_value = {
                "url": "https://res.cloudinary.com/test/image.jpg",
                "public_id": "instagram_stories/test123",
            }
            mock_cloud_instance.get_story_optimized_url.return_value = (
                "https://res.cloudinary.com/test/image_optimized.jpg"
            )
            mock_cloud_instance.close = Mock()

            await handler.handle_autopost(queue_id, mock_user, mock_query)
            await _await_background_tasks(handler)

        # Cloudinary upload SHOULD have been called
        mock_cloud_instance.upload_media.assert_called_once()

        # Instagram API post SHOULD NOT have been called
        mock_ig_instance.post_story.assert_not_called()

        # Queue item should NOT be deleted (preserved for re-testing)
        service.queue_repo.delete.assert_not_called()

        # History should NOT be created
        service.history_repo.create.assert_not_called()

        # Dry run interaction should be logged
        service.interaction_service.log_callback.assert_called_once()
        log_call = service.interaction_service.log_callback.call_args
        assert log_call.kwargs["context"]["dry_run"] is True

        # Caption should mention DRY RUN
        found_dry_run = False
        for call in mock_query.edit_message_caption.call_args_list:
            if "DRY RUN" in str(call):
                found_dry_run = True
                break
        assert found_dry_run


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostErrorRecovery:
    """Tests for error handling during auto-post."""

    async def test_cloudinary_upload_failure_shows_error(self, mock_autopost_handler):
        """Test that Cloudinary upload failure shows error with retry button."""
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        mock_queue_item = Mock()
        mock_queue_item.media_item_id = uuid4()
        service.queue_repo.get_by_id.return_value = mock_queue_item

        mock_media = Mock()
        mock_media.id = uuid4()
        mock_media.file_path = "/test/story.jpg"
        mock_media.file_name = "story.jpg"
        service.media_repo.get_by_id.return_value = mock_media

        mock_chat_settings = Mock()
        mock_chat_settings.dry_run_mode = False
        mock_chat_settings.posts_per_day = 99
        mock_chat_settings.posting_timezone = None
        service.settings_service.get_settings.return_value = mock_chat_settings

        mock_user = Mock()
        mock_user.id = uuid4()
        mock_user.telegram_username = "poster"

        mock_query = AsyncMock()
        mock_query.message = Mock(chat_id=-100123, message_id=1)

        with (
            patch(
                "src.services.integrations.instagram_api.InstagramAPIService"
            ) as mock_ig_class,
            patch(
                "src.services.integrations.cloud_storage.CloudStorageService"
            ) as mock_cloud_class,
        ):
            mock_ig_instance = mock_ig_class.return_value
            mock_ig_instance.safety_check_before_post.return_value = {
                "safe_to_post": True,
                "errors": [],
            }
            mock_ig_instance.close = Mock()

            mock_cloud_instance = mock_cloud_class.return_value
            mock_cloud_instance.upload_media.side_effect = Exception(
                "Cloudinary timeout"
            )
            mock_cloud_instance.close = Mock()

            await handler.handle_autopost(queue_id, mock_user, mock_query)
            await _await_background_tasks(handler)

        # Should show error message
        found_error = False
        for call in mock_query.edit_message_caption.call_args_list:
            if "Auto Post Failed" in str(call):
                found_error = True
                break
        assert found_error

        # Should log failure interaction
        service.interaction_service.log_callback.assert_called_once()
        log_call = service.interaction_service.log_callback.call_args
        assert log_call.kwargs["context"]["success"] is False


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostEarlyFeedback:
    """Tests for early keyboard removal in autopost handler."""

    async def test_autopost_removes_keyboard_before_processing(
        self, mock_autopost_handler
    ):
        """handle_autopost removes keyboard immediately after lock acquisition."""
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        # claim returns None so no background task is spawned
        service.queue_repo.claim_for_processing.return_value = None
        service.queue_repo.get_by_id.return_value = None
        service.history_repo.get_by_queue_item_id.return_value = None

        mock_user = Mock()
        mock_query = AsyncMock()

        await handler.handle_autopost(queue_id, mock_user, mock_query)

        # Keyboard should be removed before claim_for_processing
        mock_query.edit_message_reply_markup.assert_called_once()

    @patch("src.services.core.telegram_autopost.reconcile_card_messages")
    async def test_autopost_reconciles_card_after_claim(
        self, mock_reconcile, mock_autopost_handler
    ):
        """A successful autopost claim from a clicked card must reconcile the
        row's telegram_message_id with that card (duplicate-card guard)."""
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        queue_item = Mock(media_item_id=uuid4())
        service.queue_repo.claim_for_processing.return_value = queue_item
        # Media lookup fails right after the claim so the flow exits early —
        # the reconcile call is the behavior under test.
        service.media_repo.get_by_id.return_value = None

        mock_query = AsyncMock()

        await handler.handle_autopost(queue_id, Mock(), mock_query)

        mock_reconcile.assert_awaited_once_with(
            service, queue_id, queue_item, mock_query
        )

    async def test_autopost_answers_callback_before_slow_work(
        self, mock_autopost_handler
    ):
        """handle_autopost calls query.answer('Posting…') BEFORE any slow I/O.

        Telegram requires answer_callback_query within ~30s of the click,
        but Cloudinary upload + Meta publish can exceed that. Without an
        early ack the spinner runs indefinitely and the bot logs
        'Could not answer callback query (may be stale)'.
        """
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        # claim returns None so no background task is spawned (fast path)
        service.queue_repo.claim_for_processing.return_value = None
        service.queue_repo.get_by_id.return_value = None
        service.history_repo.get_by_queue_item_id.return_value = None

        mock_user = Mock()
        mock_query = AsyncMock()

        await handler.handle_autopost(queue_id, mock_user, mock_query)

        # Early-answer must have fired with the "Posting…" text
        mock_query.answer.assert_called_once()
        answer_call = mock_query.answer.call_args
        assert "Posting" in str(answer_call), (
            f"Expected 'Posting' in answer call, got: {answer_call}"
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostOperationLock:
    """Tests for the operation lock that prevents duplicate auto-posts."""

    async def test_double_click_returns_already_processing(self, mock_autopost_handler):
        """Tapping autopost while one is already in flight shows feedback and
        does NOT re-claim the row.

        The in-flight marker (not the operation lock) is the durable dupe guard:
        it is held for the whole background task, so a re-tap is rejected even
        though the lock has already been released before the slow edits.
        """
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        # Simulate an autopost already in flight for this item.
        service.mark_autopost_inflight(queue_id)

        mock_user = Mock()
        mock_user.id = uuid4()
        mock_query = AsyncMock()

        await handler.handle_autopost(queue_id, mock_user, mock_query)

        # Should show "already posting/processing" feedback and NOT re-claim.
        mock_query.answer.assert_called_once()
        assert "already" in str(mock_query.answer.call_args).lower()
        service.queue_repo.claim_for_processing.assert_not_called()

        # Clean up
        service.cleanup_operation_state(queue_id)


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostBackgroundTask:
    """Tests for the background task lifecycle."""

    @pytest.fixture
    def background_test_setup(self, mock_autopost_handler):
        """Common setup for background task tests: handler with claimable queue item."""
        handler = mock_autopost_handler
        service = handler.service
        queue_id = str(uuid4())

        mock_queue_item = Mock()
        mock_queue_item.media_item_id = uuid4()
        mock_queue_item.chat_settings_id = None
        service.queue_repo.claim_for_processing.return_value = mock_queue_item

        mock_media = Mock(id=uuid4(), file_name="test.jpg", generated_caption=None)
        service.media_repo.get_by_id.return_value = mock_media

        mock_user = Mock(id=uuid4())
        mock_query = AsyncMock()
        mock_query.message = Mock(chat_id=-100, message_id=1)

        return handler, service, queue_id, mock_user, mock_query

    @staticmethod
    def _patch_services():
        """Context manager patching InstagramAPIService and CloudStorageService."""
        ig_patch = patch("src.services.integrations.instagram_api.InstagramAPIService")
        cloud_patch = patch(
            "src.services.integrations.cloud_storage.CloudStorageService"
        )

        class _Combined:
            def __enter__(self_ctx):
                mock_ig = ig_patch.__enter__()
                mock_cloud = cloud_patch.__enter__()
                mock_ig.return_value.close = Mock()
                mock_ig.return_value.safety_check_before_post.return_value = {
                    "safe_to_post": False,
                    "errors": ["test"],
                }
                mock_cloud.return_value.close = Mock()
                return self_ctx

            def __exit__(self_ctx, *args):
                cloud_patch.__exit__(*args)
                ig_patch.__exit__(*args)

        return _Combined()

    async def test_handle_autopost_spawns_background_task(self, background_test_setup):
        """handle_autopost spawns a background task for the heavy work."""
        handler, _, queue_id, mock_user, mock_query = background_test_setup

        with self._patch_services():
            await handler.handle_autopost(queue_id, mock_user, mock_query)
            assert len(handler._background_tasks) == 1
            await _await_background_tasks(handler)

        assert len(handler._background_tasks) == 0

    async def test_lock_released_before_slow_edits_marker_holds(
        self, background_test_setup
    ):
        """The operation lock is released as soon as the item is claimed and the
        background task is spawned — BEFORE the slow edits — so it never blocks a
        concurrent action across the whole task. The in-flight marker carries the
        dedup for the background task's duration and is cleared when it finishes.
        """
        handler, service, queue_id, mock_user, mock_query = background_test_setup

        with self._patch_services():
            await handler.handle_autopost(queue_id, mock_user, mock_query)

            # Narrowed span: lock already released once handle_autopost returns,
            # but the durable in-flight marker is still set (task not done yet).
            lock = service.get_operation_lock(queue_id)
            assert not lock.locked()
            assert service.is_autopost_inflight(queue_id)

            await _await_background_tasks(handler)

        # Task done → marker cleared.
        assert not lock.locked()
        assert not service.is_autopost_inflight(queue_id)

    async def test_second_concurrent_tap_does_not_double_spawn(
        self, background_test_setup
    ):
        """CRITICAL (#703): a second tap arriving WHILE an autopost is in flight
        — the row is still 'processing' and therefore re-claimable — must NOT
        re-claim the row or spawn a second background task. That double-spawn is
        the #549 double-publish the operation lock used to prevent by being held
        for the whole task; the in-flight marker now provides that guarantee once
        the lock is released before the slow edits.
        """
        handler, service, queue_id, mock_user, mock_query = background_test_setup

        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking_background(*args, **kwargs):
            # Stay 'in flight' (marker held, lock already released) until released.
            started.set()
            await release.wait()
            service.cleanup_operation_state(queue_id)

        handler._autopost_background = blocking_background

        # Tap 1 — claims the row and spawns the (blocked) background task.
        await handler.handle_autopost(queue_id, mock_user, mock_query)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert service.queue_repo.claim_for_processing.call_count == 1
        assert service.is_autopost_inflight(queue_id)
        assert len(handler._background_tasks) == 1

        # Tap 2 — same item, while the first autopost is still in flight. The mock
        # claim returns the item every time (a real 'processing' row IS
        # re-claimable), so ONLY the marker stands between this and a double-spawn.
        query2 = AsyncMock()
        query2.message = Mock(chat_id=-100, message_id=1)
        await handler.handle_autopost(queue_id, mock_user, query2)

        # Rejected by the marker: no second claim, no second background task.
        assert service.queue_repo.claim_for_processing.call_count == 1
        assert len(handler._background_tasks) == 1
        query2.answer.assert_called()
        assert "already" in str(query2.answer.call_args).lower()

        # Release the first task; the marker clears when it finishes.
        release.set()
        await _await_background_tasks(handler)
        assert not service.is_autopost_inflight(queue_id)

    async def test_background_task_calls_cleanup_transactions(
        self, background_test_setup
    ):
        """Background task calls cleanup_transactions on completion."""
        handler, service, queue_id, mock_user, mock_query = background_test_setup
        service.cleanup_transactions = Mock()

        with self._patch_services():
            await handler.handle_autopost(queue_id, mock_user, mock_query)
            await _await_background_tasks(handler)

        service.cleanup_transactions.assert_called()

    async def test_rate_limit_answers_callback_and_shows_graceful_card(
        self, background_test_setup
    ):
        """End-to-end: when the IG publish hits the daily limit, the callback is
        answered up-front (spinner stops) AND the final card is the graceful
        daily-limit card — not the scary generic 'Auto Post Failed' dead-end."""
        from src.exceptions.instagram import RateLimitError

        handler, service, queue_id, mock_user, mock_query = background_test_setup

        # API enabled, not dry-run, so the flow reaches the real publish path.
        chat_settings = Mock(
            id="cs-id",
            dry_run_mode=False,
            enable_instagram_api=True,
            posts_per_day=99,
            posting_timezone=None,
        )
        service.settings_service.get_settings.return_value = chat_settings
        service.media_repo.get_by_id.return_value.file_path = "/x/test.jpg"

        # Skip the real Cloudinary upload — the publish gate is what we exercise.
        handler._upload_to_cloudinary = AsyncMock(return_value=True)

        with (
            patch(
                "src.services.integrations.instagram_api.InstagramAPIService"
            ) as mock_ig,
            patch(
                "src.services.integrations.cloud_storage.CloudStorageService"
            ) as mock_cloud,
        ):
            ig = mock_ig.return_value
            ig.close = Mock()
            ig.safety_check_before_post.return_value = {
                "safe_to_post": True,
                "errors": [],
            }
            ig.post_story = AsyncMock(
                side_effect=RateLimitError(
                    "Instagram daily publishing limit reached "
                    "(100/100 in the last 24h)."
                )
            )
            mock_cloud.return_value.close = Mock()

            await handler.handle_autopost(queue_id, mock_user, mock_query)
            await _await_background_tasks(handler)

        # Callback answered up-front (spinner stopped) regardless of outcome.
        mock_query.answer.assert_called()

        captions = [
            c.kwargs.get("caption", "")
            for c in mock_query.edit_message_caption.call_args_list
        ]
        # Final framing is the graceful daily-limit card, never the failure card.
        assert any("daily limit reached" in cap.lower() for cap in captions)
        assert all("Auto Post Failed" not in cap for cap in captions)


# ==================== Extracted Helper Tests ====================


@pytest.fixture
def make_autopost_ctx():
    """Factory fixture to create AutopostContext with sensible defaults."""

    def _make(
        queue_id=None,
        queue_item=None,
        media_item=None,
        user=None,
        query=None,
        chat_id=-100123,
        chat_settings=None,
        cloud_service=None,
        instagram_service=None,
        cancel_flag=None,
        cloud_url=None,
        cloud_public_id=None,
    ):
        if queue_id is None:
            queue_id = str(uuid4())
        if queue_item is None:
            queue_item = Mock(
                media_item_id=uuid4(),
                created_at="2026-01-01T00:00:00",
                scheduled_for="2026-01-01T12:00:00",
                chat_settings_id=None,
            )
        if media_item is None:
            media_item = Mock(
                id=uuid4(),
                file_path="/test/story.jpg",
                file_name="story.jpg",
                source_identifier="/test/story.jpg",
                mime_type="image/jpeg",
                generated_caption=None,
            )
        if user is None:
            user = Mock(
                id=uuid4(),
                telegram_username="tester",
                telegram_first_name="Test",
            )
        if query is None:
            query = AsyncMock()
            query.message = Mock(chat_id=chat_id, message_id=1)
        if chat_settings is None:
            chat_settings = Mock(
                dry_run_mode=False, posts_per_day=99, posting_timezone=None
            )
        if cloud_service is None:
            cloud_service = Mock()
        if instagram_service is None:
            instagram_service = AsyncMock()

        return AutopostContext(
            queue_id=queue_id,
            queue_item=queue_item,
            media_item=media_item,
            user=user,
            query=query,
            chat_id=chat_id,
            chat_settings=chat_settings,
            cloud_service=cloud_service,
            instagram_service=instagram_service,
            cancel_flag=cancel_flag,
            cloud_url=cloud_url,
            cloud_public_id=cloud_public_id,
        )

    return _make


@pytest.mark.unit
class TestAutopostContext:
    """Tests for the AutopostContext dataclass."""

    def test_creation(self):
        """Test AutopostContext can be created with all fields."""
        ctx = AutopostContext(
            queue_id="q1",
            queue_item=Mock(),
            media_item=Mock(),
            user=Mock(),
            query=Mock(),
            chat_id=-100,
            chat_settings=Mock(),
            cloud_service=Mock(),
            instagram_service=Mock(),
        )
        assert ctx.queue_id == "q1"
        assert ctx.chat_id == -100
        assert ctx.cancel_flag is None
        assert ctx.cloud_url is None
        assert ctx.cloud_public_id is None

    def test_mutable_fields(self):
        """Test that cloud_url and cloud_public_id can be set after creation."""
        ctx = AutopostContext(
            queue_id="q1",
            queue_item=Mock(),
            media_item=Mock(),
            user=Mock(),
            query=Mock(),
            chat_id=-100,
            chat_settings=Mock(),
            cloud_service=Mock(),
            instagram_service=Mock(),
        )
        ctx.cloud_url = "https://example.com/img.jpg"
        ctx.cloud_public_id = "stories/abc123"
        assert ctx.cloud_url == "https://example.com/img.jpg"
        assert ctx.cloud_public_id == "stories/abc123"


@pytest.mark.unit
@pytest.mark.asyncio
class TestGetAccountDisplay:
    """Tests for _get_account_display helper."""

    async def test_returns_username(self, mock_autopost_handler, make_autopost_ctx):
        """Test successful account info lookup returns @username."""
        handler = mock_autopost_handler
        ctx = make_autopost_ctx(
            instagram_service=AsyncMock(
                get_account_info=AsyncMock(return_value={"username": "mybrand"})
            )
        )

        result = await handler._get_account_display(ctx)
        assert result == "@mybrand"

    async def test_fallback_on_exception(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test that exception returns 'Unknown account'."""
        handler = mock_autopost_handler
        ctx = make_autopost_ctx(
            instagram_service=AsyncMock(
                get_account_info=AsyncMock(side_effect=Exception("API error"))
            )
        )

        result = await handler._get_account_display(ctx)
        assert result == "Unknown account"


@pytest.mark.unit
@pytest.mark.asyncio
class TestUploadToCloudinary:
    """Tests for _upload_to_cloudinary helper."""

    async def test_success_sets_ctx_fields(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test successful upload sets cloud_url and cloud_public_id on ctx."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://res.cloudinary.com/test/img.jpg",
            "public_id": "instagram_stories/abc",
        }

        ctx = make_autopost_ctx(cloud_service=mock_cloud)

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            return_value=mock_provider,
        ):
            result = await handler._upload_to_cloudinary(ctx)

        assert result is True
        assert ctx.cloud_url == "https://res.cloudinary.com/test/img.jpg"
        assert ctx.cloud_public_id == "instagram_stories/abc"
        handler.service.media_repo.update_cloud_info.assert_called_once()

    async def test_upload_uses_tenant_folder(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test upload uses tenant-scoped folder when chat_settings_id is set."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://res.cloudinary.com/test/img.jpg",
            "public_id": "instagram_stories/tenant123/abc",
        }

        queue_item = Mock(
            media_item_id=uuid4(),
            created_at="2026-01-01",
            scheduled_for="2026-01-01",
            chat_settings_id="tenant-uuid-123",
        )
        ctx = make_autopost_ctx(cloud_service=mock_cloud, queue_item=queue_item)

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            return_value=mock_provider,
        ):
            await handler._upload_to_cloudinary(ctx)

        call_kwargs = mock_cloud.upload_media.call_args.kwargs
        assert call_kwargs["folder"] == "instagram_stories/tenant-uuid-123"

    async def test_upload_uses_default_folder_when_no_tenant(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test upload uses default folder when chat_settings_id is None."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://res.cloudinary.com/test/img.jpg",
            "public_id": "instagram_stories/abc",
        }

        queue_item = Mock(
            media_item_id=uuid4(),
            created_at="2026-01-01",
            scheduled_for="2026-01-01",
            chat_settings_id=None,
        )
        ctx = make_autopost_ctx(cloud_service=mock_cloud, queue_item=queue_item)

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            return_value=mock_provider,
        ):
            await handler._upload_to_cloudinary(ctx)

        call_kwargs = mock_cloud.upload_media.call_args.kwargs
        assert call_kwargs["folder"] == "instagram_stories"

    async def test_cancelled_returns_false(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test that a set cancel flag after upload returns False."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://res.cloudinary.com/test/img.jpg",
            "public_id": "instagram_stories/abc",
        }

        cancel_flag = threading.Event()
        cancel_flag.set()  # Already cancelled

        ctx = make_autopost_ctx(cloud_service=mock_cloud, cancel_flag=cancel_flag)

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            return_value=mock_provider,
        ):
            result = await handler._upload_to_cloudinary(ctx)

        assert result is False
        # Should show cancelled message
        found_cancelled = False
        for call in ctx.query.edit_message_caption.call_args_list:
            if "cancelled" in str(call).lower():
                found_cancelled = True
                break
        assert found_cancelled


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandleDryRun:
    """Tests for _handle_dry_run helper."""

    async def test_edits_message_with_dry_run_caption(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test dry run edits message with DRY RUN caption."""
        handler = mock_autopost_handler
        handler.service._is_verbose = Mock(return_value=False)
        handler.service._get_display_name = Mock(return_value="@tester")

        ctx = make_autopost_ctx(
            cloud_url="https://example.com/img.jpg",
            cloud_public_id="stories/abc",
            instagram_service=AsyncMock(
                get_account_info=AsyncMock(return_value={"username": "testaccount"})
            ),
        )

        await handler._handle_dry_run(ctx)

        # Should edit message with dry run caption
        ctx.query.edit_message_caption.assert_called_once()
        call_kwargs = ctx.query.edit_message_caption.call_args.kwargs
        assert "DRY RUN" in call_kwargs["caption"]
        assert call_kwargs["reply_markup"] is not None

    async def test_logs_dry_run_interaction(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test dry run logs interaction with dry_run=True."""
        handler = mock_autopost_handler
        handler.service._is_verbose = Mock(return_value=False)
        handler.service._get_display_name = Mock(return_value="@tester")

        ctx = make_autopost_ctx(
            cloud_url="https://example.com/img.jpg",
            cloud_public_id="stories/abc",
            instagram_service=AsyncMock(
                get_account_info=AsyncMock(return_value={"username": "testaccount"})
            ),
        )

        await handler._handle_dry_run(ctx)

        handler.service.interaction_service.log_callback.assert_called_once()
        log_ctx = handler.service.interaction_service.log_callback.call_args.kwargs[
            "context"
        ]
        assert log_ctx["dry_run"] is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestExecuteInstagramPost:
    """Tests for _execute_instagram_post helper."""

    async def test_success_returns_story_id(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test successful post returns story_id."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.get_story_optimized_url.return_value = (
            "https://res.cloudinary.com/optimized.jpg"
        )

        mock_ig = AsyncMock()
        mock_ig.post_story = AsyncMock(return_value={"story_id": "17890012345678"})

        ctx = make_autopost_ctx(
            cloud_service=mock_cloud,
            instagram_service=mock_ig,
            cloud_url="https://res.cloudinary.com/test/img.jpg",
        )

        result = await handler._execute_instagram_post(ctx)

        assert result == "17890012345678"
        mock_ig.post_story.assert_called_once()

    async def test_cancelled_returns_none(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test that a set cancel flag returns None without posting."""
        handler = mock_autopost_handler
        cancel_flag = threading.Event()
        cancel_flag.set()

        ctx = make_autopost_ctx(cancel_flag=cancel_flag)

        result = await handler._execute_instagram_post(ctx)

        assert result is None

    async def test_video_uses_cloud_url_directly(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test VIDEO media type uses cloud_url directly (no optimization)."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_ig = AsyncMock()
        mock_ig.post_story = AsyncMock(return_value={"story_id": "vid123"})

        video_media = Mock(
            id=uuid4(),
            file_path="/test/story.mp4",
            file_name="story.mp4",
            source_identifier="/test/story.mp4",
            mime_type="video/mp4",
        )

        ctx = make_autopost_ctx(
            media_item=video_media,
            cloud_service=mock_cloud,
            instagram_service=mock_ig,
            cloud_url="https://res.cloudinary.com/test/video.mp4",
        )

        await handler._execute_instagram_post(ctx)

        # Should NOT call get_story_optimized_url for video
        mock_cloud.get_story_optimized_url.assert_not_called()
        # Should post with original cloud_url
        call_kwargs = mock_ig.post_story.call_args.kwargs
        assert call_kwargs["media_url"] == "https://res.cloudinary.com/test/video.mp4"
        assert call_kwargs["media_type"] == "VIDEO"


@pytest.mark.unit
class TestRecordSuccessfulPost:
    """Tests for _record_successful_post helper."""

    def test_calls_all_repo_operations(self, mock_autopost_handler, make_autopost_ctx):
        """Test that all 5 repo operations are called."""
        handler = mock_autopost_handler
        ctx = make_autopost_ctx()

        handler._record_successful_post(ctx, story_id="story_abc")

        # 1. Create history (idempotently — #551)
        handler.service.history_repo.create_idempotent.assert_called_once()
        # 2. Increment times posted
        # NULL-owned ctx row: scope_of_row resolves to SYSTEM_SCOPE. Before
        # #841 this passed None, which the fail-closed method rejects.
        handler.service.media_repo.increment_times_posted.assert_called_once_with(
            str(ctx.queue_item.media_item_id), chat_settings_id=SYSTEM_SCOPE
        )
        # 3. Create lock — chat_id passed so per-chat TTL can be applied
        handler.service.lock_service.create_lock.assert_called_once_with(
            str(ctx.queue_item.media_item_id), telegram_chat_id=ctx.chat_id
        )
        # 4. Delete queue item
        handler.service.queue_repo.delete.assert_called_once_with(
            ctx.queue_id, SYSTEM_SCOPE
        )
        # 5. Increment user posts
        handler.service.user_repo.increment_posts.assert_called_once_with(
            str(ctx.user.id)
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandleAutopostError:
    """Tests for _handle_autopost_error helper."""

    async def test_generic_exception_shows_fallback_message(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test generic exceptions show user-friendly fallback."""
        handler = mock_autopost_handler
        ctx = make_autopost_ctx()

        await handler._handle_autopost_error(ctx, Exception("Connection timeout"))

        ctx.query.edit_message_caption.assert_called_once()
        call_kwargs = ctx.query.edit_message_caption.call_args.kwargs
        assert "Auto Post Failed" in call_kwargs["caption"]
        assert "unexpected error" in call_kwargs["caption"]
        assert "Connection timeout" in call_kwargs["caption"]
        assert call_kwargs["reply_markup"] is not None

    async def test_media_upload_error_hides_internals(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test MediaUploadError shows user-friendly message without provider details."""
        from src.exceptions.instagram import MediaUploadError

        handler = mock_autopost_handler
        ctx = make_autopost_ctx()

        await handler._handle_autopost_error(
            ctx, MediaUploadError("Cloudinary upload failed: Unknown API key 123")
        )

        call_kwargs = ctx.query.edit_message_caption.call_args.kwargs
        assert "Cloudinary" not in call_kwargs["caption"]
        assert "server issue" in call_kwargs["caption"]

    async def test_rate_limit_error_shows_graceful_daily_limit_card(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """A RateLimitError renders a DISTINCT graceful daily-limit card (not the
        scary generic 'Auto Post Failed'), and restores the manual action
        buttons so the operator can post manually or retry — never a dead-end."""
        from src.exceptions.instagram import RateLimitError

        handler = mock_autopost_handler
        ctx = make_autopost_ctx()

        await handler._handle_autopost_error(ctx, RateLimitError())

        call_kwargs = ctx.query.edit_message_caption.call_args.kwargs
        caption = call_kwargs["caption"]
        # Graceful framing, not the generic failure card.
        assert "daily limit reached" in caption.lower()
        assert "Auto Post Failed" not in caption

        # Manual action buttons restored (back-to-buttons affordance).
        reply_markup = call_kwargs["reply_markup"]
        assert reply_markup is not None
        callbacks = [
            btn.callback_data for row in reply_markup.inline_keyboard for btn in row
        ]
        assert any(c.startswith("posted:") for c in callbacks)
        assert any(c.startswith("skip:") for c in callbacks)
        assert any(c.startswith("reject:") for c in callbacks)

    async def test_token_expired_error_message(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test TokenExpiredError shows reconnect message."""
        from src.exceptions.instagram import TokenExpiredError

        handler = mock_autopost_handler
        ctx = make_autopost_ctx()

        await handler._handle_autopost_error(ctx, TokenExpiredError())

        call_kwargs = ctx.query.edit_message_caption.call_args.kwargs
        assert "expired" in call_kwargs["caption"].lower()
        assert "reconnect" in call_kwargs["caption"].lower()

    async def test_instagram_api_error_shows_instagram_message(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test InstagramAPIError passes through Instagram's message."""
        from src.exceptions.instagram import InstagramAPIError

        handler = mock_autopost_handler
        ctx = make_autopost_ctx()

        await handler._handle_autopost_error(
            ctx, InstagramAPIError("Media too large for story")
        )

        call_kwargs = ctx.query.edit_message_caption.call_args.kwargs
        assert "Instagram rejected" in call_kwargs["caption"]
        assert "Media too large" in call_kwargs["caption"]

    async def test_media_unsupported_creates_permanent_reject_lock(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """MediaUnsupportedError (Meta 9004) creates a permanent_reject
        lock on the underlying media_item so the same file doesn't keep
        cycling through retries on every scheduler tick."""
        from src.exceptions.instagram import MediaUnsupportedError

        handler = mock_autopost_handler
        ctx = make_autopost_ctx()

        await handler._handle_autopost_error(
            ctx,
            MediaUnsupportedError(
                "Only photo or video can be accepted as media type.",
                error_code="9004",
            ),
        )

        # Permanent_reject lock created on the media_item
        handler.service.lock_service.create_lock.assert_called_once()
        lock_kwargs = handler.service.lock_service.create_lock.call_args.kwargs
        assert lock_kwargs["lock_reason"] == "permanent_reject"
        assert lock_kwargs["ttl_days"] is None  # permanent
        assert lock_kwargs["telegram_chat_id"] == ctx.chat_id
        # First positional arg is the media_item.id
        positional = handler.service.lock_service.create_lock.call_args.args
        assert positional[0] == str(ctx.media_item.id)

        # User-facing message mentions the permanent rejection
        caption = ctx.query.edit_message_caption.call_args.kwargs["caption"]
        assert "9004" in caption
        assert "permanently rejected" in caption.lower()

    async def test_media_unsupported_lock_failure_does_not_mask_error(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """If the permanent_reject lock creation itself fails, the user
        still gets the original 9004 error message (best-effort lock
        must not swallow the underlying failure)."""
        from src.exceptions.instagram import MediaUnsupportedError

        handler = mock_autopost_handler
        ctx = make_autopost_ctx()
        handler.service.lock_service.create_lock.side_effect = RuntimeError("DB down")

        await handler._handle_autopost_error(
            ctx, MediaUnsupportedError("Only photo or video", error_code="9004")
        )

        caption = ctx.query.edit_message_caption.call_args.kwargs["caption"]
        assert "9004" in caption

    async def test_logs_failure_interaction(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test error handler logs interaction with success=False."""
        handler = mock_autopost_handler
        ctx = make_autopost_ctx()

        await handler._handle_autopost_error(ctx, Exception("API error"))

        handler.service.interaction_service.log_callback.assert_called_once()
        log_ctx = handler.service.interaction_service.log_callback.call_args.kwargs[
            "context"
        ]
        assert log_ctx["success"] is False
        assert "API error" in log_ctx["error"]

    @pytest.mark.parametrize("status_code", ["ERROR", "EXPIRED"])
    async def test_confirmed_dead_container_released_for_retry(
        self, mock_autopost_handler, make_autopost_ctx, status_code
    ):
        """IG affirmatively confirms the container failed (ERROR/EXPIRED) after
        the claim → the row is RELEASED (flipped out of 'publishing' back to
        'processing' so the retry button can re-claim it), NOT held forever
        (rajan #564 finding 1)."""
        from src.exceptions.instagram import InstagramAPIError

        handler = mock_autopost_handler
        ctx = make_autopost_ctx()
        ctx.container_id = "container-xyz"  # container created + claimed

        await handler._handle_autopost_error(
            ctx,
            InstagramAPIError("Media container failed", error_code=status_code),
        )

        # Released for retry — not stranded in 'publishing'.
        handler.service.queue_repo.update_status.assert_called_once_with(
            ctx.queue_id, "processing", SYSTEM_SCOPE
        )
        # Falls through to the normal error UI (retry keyboard), not the
        # "held for review" hold message.
        call_kwargs = ctx.query.edit_message_caption.call_args.kwargs
        assert call_kwargs.get("reply_markup") is not None
        assert "held for review" not in call_kwargs["caption"].lower()

    async def test_ambiguous_container_still_held_for_review(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """A container-present failure that is NOT IG-confirmed (ambiguous
        crash/timeout) still HOLDS the row in 'publishing' — the existing
        fail-closed behavior must not regress into a release."""
        handler = mock_autopost_handler
        ctx = make_autopost_ctx()
        ctx.container_id = "container-xyz"

        await handler._handle_autopost_error(ctx, Exception("Connection reset"))

        # Held for review — the row is neither released nor deleted.
        handler.service.queue_repo.update_status.assert_not_called()
        handler.service.queue_repo.delete.assert_not_called()
        caption = ctx.query.edit_message_caption.call_args.kwargs["caption"]
        assert "held for review" in caption.lower()

    async def test_rate_limit_after_container_shows_graceful_card_not_held(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """A Meta rate-limit rejection that fires AFTER a container was created
        (the fail-open pathway: the pre-publish gate let it through, then
        media_publish 429s) must NOT land in the ambiguous 'held for review'
        dead-end. A rate-reject is definitive — nothing published — so the row
        is released for retry and the operator gets the graceful daily-limit
        card WITH the manual buttons, never a zero-button stranded 'publishing'
        orphan (navi #707)."""
        from src.exceptions.instagram import RateLimitError

        handler = mock_autopost_handler
        ctx = make_autopost_ctx()
        ctx.container_id = "container-xyz"  # container created before the 429

        await handler._handle_autopost_error(ctx, RateLimitError())

        # Row released for retry — never stranded in 'publishing'.
        handler.service.queue_repo.update_status.assert_called_once_with(
            ctx.queue_id, "processing", SYSTEM_SCOPE
        )

        call_kwargs = ctx.query.edit_message_caption.call_args.kwargs
        caption = call_kwargs["caption"]
        # Graceful daily-limit card, NOT the ambiguous held-for-review hold.
        assert "daily limit reached" in caption.lower()
        assert "held for review" not in caption.lower()

        # Manual action buttons restored (back-to-buttons affordance).
        reply_markup = call_kwargs["reply_markup"]
        assert reply_markup is not None
        callbacks = [
            btn.callback_data for row in reply_markup.inline_keyboard for btn in row
        ]
        assert any(c.startswith("posted:") for c in callbacks)
        assert any(c.startswith("skip:") for c in callbacks)


@pytest.mark.unit
class TestCloudinaryCleanup:
    """Tests for _cleanup_cloudinary helper and its integration points."""

    def test_cleanup_deletes_and_clears_db(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test successful cleanup deletes from Cloudinary and clears DB fields."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.delete_media.return_value = True

        ctx = make_autopost_ctx(
            cloud_service=mock_cloud,
            cloud_url="https://res.cloudinary.com/test/img.jpg",
            cloud_public_id="instagram_stories/abc",
        )

        handler._cleanup_cloudinary(ctx)

        mock_cloud.delete_media_for_item.assert_called_once()
        handler.service.media_repo.update_cloud_info.assert_called_once_with(
            media_id=str(ctx.media_item.id),
            cloud_url=None,
            cloud_public_id=None,
            cloud_uploaded_at=None,
            cloud_expires_at=None,
            chat_settings_id=SYSTEM_SCOPE,
        )

    def test_cleanup_skipped_when_no_public_id(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test cleanup is a no-op when cloud_public_id is None."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        ctx = make_autopost_ctx(cloud_service=mock_cloud, cloud_public_id=None)

        handler._cleanup_cloudinary(ctx)

        mock_cloud.delete_media.assert_not_called()

    def test_cleanup_failure_does_not_raise(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test that Cloudinary delete failure is swallowed (best-effort)."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.delete_media.side_effect = Exception("Cloudinary timeout")

        ctx = make_autopost_ctx(
            cloud_service=mock_cloud,
            cloud_public_id="instagram_stories/abc",
        )

        # Should NOT raise
        handler._cleanup_cloudinary(ctx)

    def test_cleanup_delete_returns_false_does_not_clear_db(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test that when delete_media returns False, DB fields are not cleared."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.delete_media_for_item.return_value = False

        ctx = make_autopost_ctx(
            cloud_service=mock_cloud,
            cloud_public_id="instagram_stories/abc",
        )

        handler._cleanup_cloudinary(ctx)

        mock_cloud.delete_media_for_item.assert_called_once()
        handler.service.media_repo.update_cloud_info.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
class TestCloudinaryCleanupIntegration:
    """Tests that cleanup is called in the right places during autopost flow."""

    async def test_cleanup_called_after_successful_post(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test cleanup is called between record and success message."""
        handler = mock_autopost_handler
        handler.service.settings_service.get_settings.return_value = Mock(
            dry_run_mode=False, posts_per_day=99, posting_timezone=None
        )
        handler.service._is_verbose = Mock(return_value=False)
        handler.service._get_display_name = Mock(return_value="@tester")

        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://res.cloudinary.com/test/img.jpg",
            "public_id": "instagram_stories/test123",
        }
        mock_cloud.get_story_optimized_url.return_value = (
            "https://res.cloudinary.com/test/optimized.jpg"
        )
        mock_cloud.delete_media.return_value = True

        mock_ig = Mock()
        mock_ig.safety_check_before_post.return_value = {
            "safe_to_post": True,
            "errors": [],
        }
        mock_ig.post_story = AsyncMock(return_value={"story_id": "story_123"})
        mock_ig.get_account_info = AsyncMock(return_value={"username": "testaccount"})

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            return_value=mock_provider,
        ):
            await handler._do_autopost(
                str(uuid4()),
                Mock(
                    media_item_id=uuid4(),
                    created_at="2026-01-01",
                    scheduled_for="2026-01-01",
                    chat_settings_id=None,
                ),
                Mock(
                    id=uuid4(),
                    file_path="/test/story.jpg",
                    file_name="story.jpg",
                    source_identifier="/test/story.jpg",
                    mime_type="image/jpeg",
                ),
                Mock(
                    id=uuid4(), telegram_username="tester", telegram_first_name="Test"
                ),
                AsyncMock(message=Mock(chat_id=-100123, message_id=1)),
                mock_ig,
                mock_cloud,
            )

        mock_cloud.delete_media_for_item.assert_called_once()

    async def test_cleanup_called_after_dry_run(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test cleanup is called at end of dry-run flow."""
        handler = mock_autopost_handler
        handler.service._is_verbose = Mock(return_value=False)
        handler.service._get_display_name = Mock(return_value="@tester")

        mock_cloud = Mock()
        mock_cloud.delete_media.return_value = True
        ctx = make_autopost_ctx(
            cloud_service=mock_cloud,
            cloud_url="https://res.cloudinary.com/test/img.jpg",
            cloud_public_id="instagram_stories/abc",
            instagram_service=AsyncMock(
                get_account_info=AsyncMock(return_value={"username": "testaccount"})
            ),
        )

        await handler._handle_dry_run(ctx)

        mock_cloud.delete_media_for_item.assert_called_once()

    async def test_cleanup_called_on_error(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test cleanup is called when autopost encounters an error."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.delete_media.return_value = True

        ctx = make_autopost_ctx(
            cloud_service=mock_cloud,
            cloud_public_id="instagram_stories/err",
        )

        await handler._handle_autopost_error(ctx, Exception("some error"))

        mock_cloud.delete_media_for_item.assert_called_once()

    async def test_cleanup_called_on_cancel_after_upload(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test Cloudinary resource is deleted when cancel flag is set after upload."""
        handler = mock_autopost_handler
        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://res.cloudinary.com/test/img.jpg",
            "public_id": "instagram_stories/cancelled",
        }

        cancel_flag = threading.Event()
        cancel_flag.set()

        ctx = make_autopost_ctx(cloud_service=mock_cloud, cancel_flag=cancel_flag)

        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            return_value=mock_provider,
        ):
            result = await handler._upload_to_cloudinary(ctx)

        assert result is False
        mock_cloud.delete_media_for_item.assert_called_once()

    async def test_cleanup_failure_does_not_break_success_flow(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test that Cloudinary cleanup failure doesn't prevent success message."""
        handler = mock_autopost_handler
        handler.service.settings_service.get_settings.return_value = Mock(
            dry_run_mode=False, posts_per_day=99, posting_timezone=None
        )
        handler.service._is_verbose = Mock(return_value=False)
        handler.service._get_display_name = Mock(return_value="@tester")

        mock_cloud = Mock()
        mock_cloud.upload_media.return_value = {
            "url": "https://res.cloudinary.com/test/img.jpg",
            "public_id": "instagram_stories/test123",
        }
        mock_cloud.get_story_optimized_url.return_value = (
            "https://res.cloudinary.com/test/optimized.jpg"
        )
        mock_cloud.delete_media.side_effect = Exception("Cloudinary is down")

        mock_ig = Mock()
        mock_ig.safety_check_before_post.return_value = {
            "safe_to_post": True,
            "errors": [],
        }
        mock_ig.post_story = AsyncMock(return_value={"story_id": "story_123"})
        mock_ig.get_account_info = AsyncMock(return_value={"username": "testaccount"})

        mock_query = AsyncMock(message=Mock(chat_id=-100123, message_id=1))
        mock_provider = Mock()
        mock_provider.download_file.return_value = b"fake bytes"

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            return_value=mock_provider,
        ):
            # Should NOT raise despite cleanup failure
            await handler._do_autopost(
                str(uuid4()),
                Mock(
                    media_item_id=uuid4(),
                    created_at="2026-01-01",
                    scheduled_for="2026-01-01",
                    chat_settings_id=None,
                ),
                Mock(
                    id=uuid4(),
                    file_path="/test/story.jpg",
                    file_name="story.jpg",
                    source_identifier="/test/story.jpg",
                    mime_type="image/jpeg",
                ),
                Mock(
                    id=uuid4(), telegram_username="tester", telegram_first_name="Test"
                ),
                mock_query,
                mock_ig,
                mock_cloud,
            )

        # Success message should still be sent
        handler.service.interaction_service.log_callback.assert_called_once()
        log_ctx = handler.service.interaction_service.log_callback.call_args.kwargs[
            "context"
        ]
        assert log_ctx["success"] is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestCloudUrlNotInDryRunLog:
    """Test that cloud_url is not leaked in interaction logs."""

    async def test_cloud_url_not_in_dry_run_interaction_log(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Test that dry-run interaction log does not contain cloud_url."""
        handler = mock_autopost_handler
        handler.service._is_verbose = Mock(return_value=False)
        handler.service._get_display_name = Mock(return_value="@tester")

        ctx = make_autopost_ctx(
            cloud_url="https://res.cloudinary.com/test/img.jpg",
            cloud_public_id="instagram_stories/abc",
            instagram_service=AsyncMock(
                get_account_info=AsyncMock(return_value={"username": "testaccount"})
            ),
        )

        await handler._handle_dry_run(ctx)

        log_ctx = handler.service.interaction_service.log_callback.call_args.kwargs[
            "context"
        ]
        assert "cloud_url" not in log_ctx
        assert "cloud_public_id" in log_ctx


@pytest.mark.unit
class TestGetUserFriendlyError:
    """Tests for _get_user_friendly_error static method."""

    def test_token_revoked_returns_disconnected_message(self):
        """TokenRevokedError produces a distinct 'disconnected' message."""
        from src.exceptions.instagram import TokenRevokedError

        err = TokenRevokedError("App deauthorized", error_subcode=458)
        msg = TelegramAutopostHandler._get_user_friendly_error(err)

        assert "disconnected" in msg.lower()
        assert "reconnect" in msg.lower()


from datetime import datetime, timezone  # noqa: E402
from tests.src.services.conftest import make_query, make_user  # noqa: E402


def _autopost_ctx_bits():
    """Build the queue_item / media_item / chat_settings mocks a finalize needs."""
    now = datetime.now(timezone.utc)
    queue_item = Mock(
        id=uuid4(),
        media_item_id=uuid4(),
        chat_settings_id=uuid4(),
        created_at=now,
        scheduled_for=now,
    )
    media_item = Mock(id=uuid4(), file_name="x.jpg", file_path="x.jpg")
    cs = Mock(
        id=uuid4(),
        dry_run_mode=False,
        enable_instagram_api=True,
        posts_per_day=99,
        posting_timezone=None,
        telegram_chat_id=-100123,
    )
    return queue_item, media_item, cs


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostClaimBeforePublish:
    """The Telegram autopost button path must claim 'publishing' + persist the
    container_id before publishing, and finalize idempotently (#549, #551)."""

    async def test_crash_after_publish_leaves_row_publishing(
        self, mock_autopost_handler
    ):
        """post_story creates the container (fires the callback) and returns a
        story_id, then the finalize crashes. The 'publishing' row must NOT be
        deleted — it stays stuck so the media can't be re-served."""
        handler = mock_autopost_handler
        service = handler.service
        queue_item, media_item, cs = _autopost_ctx_bits()
        service.settings_service.get_settings.return_value = cs
        service.queue_repo.count_by_status.return_value = 0
        # Crash: the atomic finalize raises after publish.
        service.history_repo.create_idempotent.side_effect = RuntimeError("crash")

        ig = Mock()
        ig.safety_check_before_post.return_value = {"safe_to_post": True, "errors": []}

        async def _post(
            media_url, media_type, telegram_chat_id, on_container_created=None
        ):
            if on_container_created is not None:
                on_container_created("container-xyz")
            return {"story_id": "story-1", "container_id": "container-xyz"}

        ig.post_story = AsyncMock(side_effect=_post)
        cloud = Mock()

        with patch.object(
            handler, "_upload_to_cloudinary", AsyncMock(return_value=True)
        ):
            await handler._do_autopost(
                str(queue_item.id),
                queue_item,
                media_item,
                make_user(),
                make_query(),
                ig,
                cloud,
            )

        # The row's OWN stamp, deliberately a different uuid from cs.id in
        # this fixture — so this asserts the scope came from the row, not from
        # ambient chat settings.
        service.queue_repo.mark_publishing.assert_called_once_with(
            str(queue_item.id), "container-xyz", str(queue_item.chat_settings_id)
        )
        service.queue_repo.delete.assert_not_called()

    async def test_execute_instagram_post_persists_container_before_publish(
        self, mock_autopost_handler
    ):
        """_execute_instagram_post wires on_container_created so the container
        id is marked 'publishing' the instant it exists."""
        handler = mock_autopost_handler
        service = handler.service
        queue_item, media_item, cs = _autopost_ctx_bits()

        published = {"container_marked": False}

        async def _post(
            media_url, media_type, telegram_chat_id, on_container_created=None
        ):
            assert on_container_created is not None, "must pass container callback"
            on_container_created("container-xyz")
            published["container_marked"] = True
            return {"story_id": "story-1", "container_id": "container-xyz"}

        ig = Mock()
        ig.post_story = AsyncMock(side_effect=_post)
        ctx = AutopostContext(
            queue_id=str(queue_item.id),
            queue_item=queue_item,
            media_item=media_item,
            user=make_user(),
            query=make_query(),
            chat_id=cs.telegram_chat_id,
            chat_settings=cs,
            cloud_service=Mock(),
            instagram_service=ig,
        )
        ctx.cloud_url = "https://res.cloudinary.com/x.jpg"

        story_id = await handler._execute_instagram_post(ctx)

        assert story_id == "story-1"
        assert published["container_marked"] is True
        # The row's OWN stamp, deliberately a different uuid from cs.id in
        # this fixture — so this asserts the scope came from the row, not from
        # ambient chat settings.
        service.queue_repo.mark_publishing.assert_called_once_with(
            str(queue_item.id), "container-xyz", str(queue_item.chat_settings_id)
        )

    async def test_record_successful_post_is_idempotent(self, mock_autopost_handler):
        """The finalize uses create_idempotent (not create) so a replay can't
        double-insert posting_history (#551)."""
        handler = mock_autopost_handler
        service = handler.service
        queue_item, media_item, cs = _autopost_ctx_bits()
        ctx = AutopostContext(
            queue_id=str(queue_item.id),
            queue_item=queue_item,
            media_item=media_item,
            user=make_user(),
            query=make_query(),
            chat_id=cs.telegram_chat_id,
            chat_settings=cs,
            cloud_service=Mock(),
            instagram_service=Mock(),
        )

        handler._record_successful_post(ctx, "story-1")

        service.history_repo.create_idempotent.assert_called_once()
        service.history_repo.create.assert_not_called()
        service.queue_repo.delete.assert_called_once_with(
            str(queue_item.id), str(queue_item.chat_settings_id)
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestAutopostMediaOffload:
    """The autopost media transfer must not run ON the asyncio event loop.

    The Drive download + Cloudinary upload are synchronous network I/O. Run on
    the single bot event loop, a slow transfer FREEZES it — stalling not just
    this task but everything else scheduled on that loop: the Telegram update
    poller, the scheduler tick, and the cleanup loops. Offloading the transfer
    to a worker thread keeps the loop live for that other work.

    Scope note: this proves the loop is not PARKED. It does NOT prove a
    competing button tap's ack fires sooner — under this repo's PTB config
    (max_concurrent_updates=1, default-blocking handler) updates are still
    dispatched one at a time, so cross-update ack latency needs
    ``concurrent_updates`` + per-callback session isolation (tracked separately).
    """

    async def test_slow_transfer_does_not_park_the_event_loop(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        handler = mock_autopost_handler
        loop = asyncio.get_running_loop()

        # A blocking sleep stands in for slow synchronous network I/O: left on the
        # loop it parks everything; offloaded to a thread it does not.
        TRANSFER_SECONDS = 0.4

        def slow_download(_identifier):
            time.sleep(TRANSFER_SECONDS)
            return b"fake-image-bytes"

        def slow_upload(**_kwargs):
            time.sleep(TRANSFER_SECONDS)
            return {"url": "https://cloud.example/x.jpg", "public_id": "storydump/x"}

        provider = Mock()
        provider.download_file.side_effect = slow_download
        cloud_service = Mock()
        cloud_service.upload_media.side_effect = slow_upload
        ctx = make_autopost_ctx(cloud_service=cloud_service)

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory."
            "get_provider_for_media_item",
            return_value=provider,
        ):
            transfer = asyncio.create_task(handler._upload_to_cloudinary(ctx))

            # Probe stands in for any OTHER work scheduled on the loop (the update
            # poller, a scheduler tick, a heartbeat): while the transfer is in
            # flight the loop must still service it. A transfer left on the loop
            # would freeze this 0.1s timer until the whole transfer finishes.
            probe_start = loop.time()
            await asyncio.sleep(0.1)
            probe_latency = loop.time() - probe_start

            assert probe_latency < TRANSFER_SECONDS, (
                f"event loop parked {probe_latency:.2f}s during the media transfer "
                f"— concurrent acks would fire past Telegram's validity window"
            )
            assert await transfer is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestTenantCredentialResolutionFailsClosed:
    """A failure to RESOLVE this tenant's credentials must not be absorbed here.

    ``_upload_to_cloudinary`` calls the tenant-credential path directly and has
    no exception handling of its own; the guarantee is that it stays that way,
    so the error reaches ``_handle_autopost_error`` and is shown to the tenant.

    Injected at ``get_provider_for_media_item`` rather than at
    ``download_file``: the existing tests exercise a provider that already
    exists, which cannot reach either raise site inside
    ``get_provider_for_chat`` (no stored credentials; no configured root
    folder). Those two are what a tenant with broken OAuth actually hits.
    """

    async def test_a_resolution_auth_failure_is_not_absorbed_by_the_upload_helper(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """The tenant's auth error escapes the helper rather than returning False.

        ``_upload_to_cloudinary`` signals ordinary refusal by returning False,
        which the caller treats as "stop quietly, the card is already updated".
        An auth error absorbed into that return is indistinguishable from a
        user-cancelled post, and the tenant is never told to reconnect Drive.
        """
        from src.exceptions.google_drive import GoogleDriveAuthError

        handler = mock_autopost_handler
        mock_cloud = Mock()
        ctx = make_autopost_ctx(cloud_service=mock_cloud)

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            side_effect=GoogleDriveAuthError("No Google Drive OAuth credentials"),
        ) as mock_resolve:
            with pytest.raises(GoogleDriveAuthError, match="No Google Drive OAuth"):
                await handler._upload_to_cloudinary(ctx)

        # Anti-vacuity: prove the credential path was actually walked, and for
        # THIS tenant. Without this the test also passes against a helper that
        # bails out before ever resolving anything.
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args.kwargs["telegram_chat_id"] == ctx.chat_id
        # It failed AT resolution — nothing was uploaded on the tenant's behalf.
        mock_cloud.upload_media.assert_not_called()

    async def test_a_non_auth_resolution_failure_also_escapes(
        self, mock_autopost_handler, make_autopost_ctx
    ):
        """Control: the helper absorbs NOTHING, rather than special-casing auth.

        This caller's fail-closed property comes from having no handler at all,
        which is a different mechanism from the notification path's explicit
        re-raise. Asserting only the auth case would leave a future ``except
        Exception: return False`` here green for every non-auth failure.
        """
        handler = mock_autopost_handler
        mock_cloud = Mock()
        ctx = make_autopost_ctx(cloud_service=mock_cloud)

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory.get_provider_for_media_item",
            side_effect=ValueError("malformed source config"),
        ) as mock_resolve:
            with pytest.raises(ValueError, match="malformed source config"):
                await handler._upload_to_cloudinary(ctx)

        mock_resolve.assert_called_once()
        mock_cloud.upload_media.assert_not_called()
