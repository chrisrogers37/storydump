"""Tests for TelegramCallbackAdminHandlers — batch approve, resume, reset."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.core.telegram_callbacks_admin import TelegramCallbackAdminHandlers
from src.services.core.telegram_callbacks_core import TelegramCallbackCore
from tests.src.services.conftest import make_query as _make_query
from tests.src.services.conftest import make_user as _make_user


@pytest.fixture
def mock_service():
    """Minimal TelegramService mock for admin handler tests."""
    service = Mock()
    service.queue_repo = Mock()
    service.interaction_service = Mock()
    service._get_display_name.return_value = "AdminUser"
    # Default: the calling chat resolves to tenant "cs-1" (make_query's
    # default chat). Cross-tenant tests override the door they exercise:
    # batch-approve resolves via resolve_chat_settings_id; resume/reset read
    # the row via get_settings (#842).
    service.settings_service = Mock()
    service.settings_service.resolve_chat_settings_id.return_value = "cs-1"
    service.settings_service.get_settings.return_value = Mock(
        id="cs-1", telegram_chat_id=-100123
    )
    return service


@pytest.fixture
def mock_core(mock_service):
    """TelegramCallbackCore mock."""
    core = Mock(spec=TelegramCallbackCore)
    core.service = mock_service
    core._execute_complete_db_ops = Mock()
    return core


@pytest.fixture
def handlers(mock_service, mock_core):
    """TelegramCallbackAdminHandlers with mocked dependencies."""
    return TelegramCallbackAdminHandlers(mock_service, mock_core)


# ──────────────────────────────────────────────────────────────
# handle_batch_approve
# ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandleBatchApprove:
    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_approves_all_items(self, mock_retry, handlers):
        """Approves all pending+processing items and reports count."""
        qi1, qi2 = Mock(id="q-1"), Mock(id="q-2")
        handlers.service.queue_repo.get_all_with_media.side_effect = [
            [(qi1, "f1.jpg", "memes")],  # pending
            [(qi2, "f2.jpg", "memes")],  # processing
            [],  # sent_unconfirmed
            [],  # delivered
        ]
        handlers.service.queue_repo.claim_for_processing.return_value = Mock()

        user = _make_user()
        query = _make_query()

        await handlers.handle_batch_approve("cs-1", user, query)

        assert handlers.core._execute_complete_db_ops.call_count == 2
        handlers.service.interaction_service.log_callback.assert_called_once()

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_no_items_to_approve(self, mock_retry, handlers):
        """Shows 'no pending items' when both lists are empty."""
        handlers.service.queue_repo.get_all_with_media.return_value = []

        user = _make_user()
        query = _make_query()

        await handlers.handle_batch_approve("cs-1", user, query)

        mock_retry.assert_called()
        edit_texts = [
            c.args[1]
            for c in mock_retry.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], str)
        ]
        assert any("No pending items" in t for t in edit_texts)

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_mixed_success_and_failure(self, mock_retry, handlers):
        """Reports both approved and failed counts."""
        qi1, qi2 = Mock(id="q-1"), Mock(id="q-2")
        handlers.service.queue_repo.get_all_with_media.side_effect = [
            [(qi1, "f1.jpg", "m"), (qi2, "f2.jpg", "m")],
            [],
            [],
            [],
        ]
        handlers.service.queue_repo.claim_for_processing.side_effect = [Mock(), Mock()]
        handlers.core._execute_complete_db_ops.side_effect = [
            None,
            Exception("db error"),
        ]

        user = _make_user()
        query = _make_query()

        await handlers.handle_batch_approve("cs-1", user, query)

        last_call_text = mock_retry.call_args_list[-1][0][1]
        assert "1 item marked as posted" in last_call_text
        assert "1 item failed" in last_call_text

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_claim_failure_counts_as_failed(self, mock_retry, handlers):
        """If claim_for_processing returns None, it counts as failed."""
        qi1 = Mock(id="q-1")
        handlers.service.queue_repo.get_all_with_media.side_effect = [
            [(qi1, "f1.jpg", "m")],
            [],
            [],
            [],
        ]
        handlers.service.queue_repo.claim_for_processing.return_value = None

        user = _make_user()
        query = _make_query()

        await handlers.handle_batch_approve("cs-1", user, query)

        handlers.core._execute_complete_db_ops.assert_not_called()


# ──────────────────────────────────────────────────────────────
# handle_batch_approve_cancel
# ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandleBatchApproveCancel:
    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_shows_cancelled_message(self, mock_retry, handlers):
        """Shows cancellation message."""
        user = _make_user()
        query = _make_query()

        await handlers.handle_batch_approve_cancel("cs-1", user, query)

        mock_retry.assert_called_once()
        assert "cancelled" in mock_retry.call_args[0][1].lower()


# ──────────────────────────────────────────────────────────────
# handle_resume_callback
# ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandleResumeCallback:
    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_reschedule_action(self, mock_retry, handlers):
        """Reschedules overdue posts and resumes delivery."""
        now = datetime.now(timezone.utc)
        overdue = [Mock(id="q-1", scheduled_for=now - timedelta(hours=2))]
        future = [Mock(id="q-2", scheduled_for=now + timedelta(hours=1))]
        handlers.service.queue_repo.get_all.return_value = overdue + future

        user = _make_user()
        query = _make_query()

        await handlers.handle_resume_callback("reschedule", user, query)

        handlers.service.queue_repo.update_scheduled_time.assert_called_once()
        handlers.service.settings_service.set_paused.assert_called_once_with(
            -100123, False, user
        )
        handlers.service.interaction_service.log_callback.assert_called_once()

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_clear_action(self, mock_retry, handlers):
        """Clears overdue posts and resumes delivery."""
        now = datetime.now(timezone.utc)
        overdue = [
            Mock(
                id="q-1",
                scheduled_for=now - timedelta(hours=2),
                telegram_message_id=None,  # button-less → plain delete
            )
        ]
        handlers.service.queue_repo.get_all.return_value = overdue

        user = _make_user()
        query = _make_query()

        await handlers.handle_resume_callback("clear", user, query)

        handlers.service.queue_repo.delete.assert_called_once()
        handlers.service.settings_service.set_paused.assert_called_once_with(
            -100123, False, user
        )

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_force_action(self, mock_retry, handlers):
        """Force resumes without handling overdue posts, unpausing the chat
        the button was pressed in — never the deployment-wide
        TELEGRAM_CHANNEL_ID chat."""
        now = datetime.now(timezone.utc)
        overdue = [Mock(id="q-1", scheduled_for=now - timedelta(hours=2))]
        handlers.service.queue_repo.get_all.return_value = overdue
        handlers.service.settings_service.get_settings.return_value = Mock(
            id="cs-9", telegram_chat_id=-100987
        )

        user = _make_user()
        query = _make_query(chat_id=-100987)

        await handlers.handle_resume_callback("force", user, query)

        handlers.service.settings_service.set_paused.assert_called_once_with(
            -100987, False, user
        )
        handlers.service.queue_repo.delete.assert_not_called()
        handlers.service.queue_repo.update_scheduled_time.assert_not_called()

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_error_shows_fallback_message(self, mock_retry, handlers):
        """On exception, shows error message."""
        handlers.service.queue_repo.get_all.side_effect = RuntimeError("db down")

        user = _make_user()
        query = _make_query()

        await handlers.handle_resume_callback("reschedule", user, query)

        mock_retry.assert_called_once()
        assert "Error" in mock_retry.call_args[0][1]


# ──────────────────────────────────────────────────────────────
# handle_reset_callback
# ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
class TestHandleResetCallback:
    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_confirm_clears_queue(self, mock_retry, handlers):
        """Confirm action deletes all pending posts."""
        pending = [
            Mock(id="q-1", telegram_message_id=None),
            Mock(id="q-2", telegram_message_id=None),
        ]
        handlers.service.queue_repo.get_all.return_value = pending

        user = _make_user()
        query = _make_query()

        await handlers.handle_reset_callback("confirm", user, query)

        assert handlers.service.queue_repo.delete.call_count == 2
        handlers.service.interaction_service.log_callback.assert_called_once()

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_cancel_does_not_clear(self, mock_retry, handlers):
        """Cancel action shows message but doesn't delete anything."""
        user = _make_user()
        query = _make_query()

        await handlers.handle_reset_callback("cancel", user, query)

        handlers.service.queue_repo.delete.assert_not_called()
        mock_retry.assert_called_once()
        assert "Cancelled" in mock_retry.call_args[0][1]

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_error_during_confirm(self, mock_retry, handlers):
        """On exception during confirm, shows error message."""
        handlers.service.queue_repo.get_all.side_effect = RuntimeError("db down")

        user = _make_user()
        query = _make_query()

        await handlers.handle_reset_callback("confirm", user, query)

        mock_retry.assert_called_once()
        assert "Error" in mock_retry.call_args[0][1]

    @patch("src.services.core.queue_reap.expire_sent_row", new_callable=AsyncMock)
    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_confirm_reaps_button_rows_and_plain_deletes_the_rest(
        self, mock_retry, mock_expire, handlers
    ):
        """Live-card rows are expired gracefully, not blind-deleted.

        A button-bearing pending row routes through expire_sent_row (strip card
        + terminal history); a button-less row is plain-deleted. This is the fix
        for the remaining orphaned-button delete path (#561).
        """
        mock_expire.return_value = "reaped"
        button_row = Mock(id="q-live", telegram_message_id=555, chat_settings_id="cs-1")
        plain_row = Mock(id="q-dead", telegram_message_id=None, chat_settings_id="cs-1")
        handlers.service.queue_repo.get_all.return_value = [button_row, plain_row]

        await handlers.handle_reset_callback("confirm", _make_user(), _make_query())

        # Live card routed through expire_sent_row (strips buttons + writes history).
        mock_expire.assert_awaited_once()
        assert mock_expire.await_args.args[0] is button_row
        # Only the button-less row is hard-deleted; the live one is never delete()d.
        handlers.service.queue_repo.delete.assert_called_once_with("q-dead", "cs-1")


# ──────────────────────────────────────────────────────────────
# Cross-tenant isolation (#512)
# ──────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
class TestCrossTenantIsolation:
    """Queue mutations must never reach across tenants."""

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_resume_clear_scopes_to_caller_tenant(self, mock_retry, handlers):
        """resume:clear queries and deletes only the calling chat's rows."""
        handlers.service.settings_service.get_settings.return_value = Mock(id="cs-A")
        now = datetime.now(timezone.utc)
        handlers.service.queue_repo.get_all.return_value = [
            Mock(
                id="q-1",
                scheduled_for=now - timedelta(hours=2),
                telegram_message_id=None,
            )
        ]

        await handlers.handle_resume_callback("clear", _make_user(), _make_query())

        handlers.service.queue_repo.get_all.assert_called_once_with(
            status="pending", chat_settings_id="cs-A"
        )
        handlers.service.queue_repo.delete.assert_called_once()

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_resume_bails_when_chat_has_no_tenant(self, mock_retry, handlers):
        """No chat_settings → never run an unscoped query or delete anything."""
        handlers.service.settings_service.get_settings.return_value = None

        await handlers.handle_resume_callback("clear", _make_user(), _make_query())

        handlers.service.queue_repo.get_all.assert_not_called()
        handlers.service.queue_repo.delete.assert_not_called()

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_reset_confirm_scopes_to_caller_tenant(self, mock_retry, handlers):
        """reset:confirm queries and deletes only the calling chat's rows."""
        handlers.service.settings_service.get_settings.return_value = Mock(id="cs-A")
        handlers.service.queue_repo.get_all.return_value = [
            Mock(id="q-1", telegram_message_id=None)
        ]

        await handlers.handle_reset_callback("confirm", _make_user(), _make_query())

        handlers.service.queue_repo.get_all.assert_called_once_with(
            status="pending", chat_settings_id="cs-A"
        )
        handlers.service.queue_repo.delete.assert_called_once()

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_reset_confirm_bails_when_chat_has_no_tenant(
        self, mock_retry, handlers
    ):
        """No chat_settings → reset:confirm refuses rather than wipe everything."""
        handlers.service.settings_service.get_settings.return_value = None

        await handlers.handle_reset_callback("confirm", _make_user(), _make_query())

        handlers.service.queue_repo.get_all.assert_not_called()
        handlers.service.queue_repo.delete.assert_not_called()

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_batch_approve_rejects_foreign_chat_settings_id(
        self, mock_retry, handlers
    ):
        """A button carrying another tenant's chat_settings_id is refused."""
        handlers.service.settings_service.get_settings.return_value = Mock(id="cs-A")

        await handlers.handle_batch_approve("cs-VICTIM", _make_user(), _make_query())

        handlers.service.queue_repo.get_all_with_media.assert_not_called()
        handlers.core._execute_complete_db_ops.assert_not_called()
        assert "Not authorized" in mock_retry.call_args[0][1]

    @patch("src.services.core.telegram_callbacks_admin.telegram_edit_with_retry")
    async def test_batch_approve_allows_own_chat_settings_id(
        self, mock_retry, handlers
    ):
        """A button carrying the caller's own chat_settings_id proceeds."""
        handlers.service.settings_service.get_settings.return_value = Mock(id="cs-A")
        handlers.service.queue_repo.get_all_with_media.return_value = []

        await handlers.handle_batch_approve("cs-A", _make_user(), _make_query())

        handlers.service.queue_repo.get_all_with_media.assert_called()
