"""Tests for SetupStateService."""

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import MagicMock, Mock, patch
from sqlalchemy.exc import SQLAlchemyError

from src.services.core.setup_state_service import (
    SetupStateService,
    TOKEN_STALE_DAYS,
    is_token_stale,
)
from tests.src.services.conftest import mock_track_execution


@pytest.mark.unit
class TestGetSetupState:
    """Tests for SetupStateService.get_setup_state()."""

    @pytest.fixture(autouse=True)
    def setup_service(self):
        with patch.object(SetupStateService, "__init__", lambda self: None):
            self.service = SetupStateService()
            self.service.service_run_repo = Mock()
            self.service.service_name = "SetupStateService"
            self.service.track_execution = mock_track_execution
            self.service.settings_service = Mock()
            self.service.ig_account_service = Mock()
            self.service.token_repo = Mock()
            self.service.media_repo = Mock()
            self.service.queue_repo = Mock()
            self.service.history_repo = Mock()

    def _make_chat_settings(self, **overrides):
        defaults = {
            "id": "uuid-123",
            "posts_per_day": 3,
            "posting_hours_start": 14,
            "posting_hours_end": 2,
            "onboarding_completed": False,
            "onboarding_step": None,
            "is_paused": False,
            "dry_run_mode": True,
            "enable_instagram_api": False,
            "show_verbose_notifications": True,
            "media_sync_enabled": False,
            "media_source_root": None,
        }
        defaults.update(overrides)
        return Mock(**defaults)

    @pytest.fixture(autouse=True)
    def setup_default_mocks(self, setup_service):
        """Set all-disconnected baseline. Tests override what they vary."""
        self.service.settings_service.require_settings.return_value = (
            self._make_chat_settings()
        )
        self.service.ig_account_service.get_active_account.return_value = None
        self.service.token_repo.get_token_for_chat.return_value = None
        self.service.media_repo.get_active_by_source_type.return_value = []
        self.service.media_repo.count_active.return_value = 0
        self.service.queue_repo.get_all.return_value = []
        self.service.history_repo.get_recent_posts.return_value = []

    def test_all_disconnected(self):
        """All services disconnected returns default False/None/0 state."""
        state = self.service.get_setup_state(-1001234567890)

        assert state["instagram_connected"] is False
        assert state["instagram_username"] is None
        assert state["gdrive_connected"] is False
        assert state["gdrive_email"] is None
        assert state["gdrive_needs_reconnect"] is False
        assert state["media_folder_configured"] is False
        assert state["media_indexed"] is False
        assert state["media_count"] == 0
        assert state["in_flight_count"] == 0
        assert state["posting_active"] is False

    def test_instagram_connected(self):
        """Active Instagram account is reflected in state."""
        self.service.ig_account_service.get_active_account.return_value = Mock(
            instagram_username="testuser",
            display_name="Test User",
        )

        state = self.service.get_setup_state(-1001234567890)

        assert state["instagram_connected"] is True
        assert state["instagram_username"] == "testuser"

    def test_gdrive_connected_fresh(self):
        """Fresh Google Drive token shows connected, not needing reconnect."""
        self.service.token_repo.get_token_for_chat.return_value = Mock(
            token_metadata={"email": "user@gmail.com"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        state = self.service.get_setup_state(-1001234567890)

        assert state["gdrive_connected"] is True
        assert state["gdrive_email"] == "user@gmail.com"
        assert state["gdrive_needs_reconnect"] is False

    def test_gdrive_needs_reconnect(self):
        """Token expired > 7 days ago triggers needs_reconnect."""
        self.service.token_repo.get_token_for_chat.return_value = Mock(
            token_metadata={"email": "old@gmail.com"},
            expires_at=datetime.now(timezone.utc)
            - timedelta(days=TOKEN_STALE_DAYS + 1),
        )

        state = self.service.get_setup_state(-1001234567890)

        assert state["gdrive_connected"] is True
        assert state["gdrive_needs_reconnect"] is True

    def test_media_count_is_source_agnostic(self):
        """#877: the library count is the tenant's ACTIVE media regardless of
        source type — the same universe the dashboard card's body renders.
        The old code asked the repository for one hardcoded source
        ("google_drive"), so a local or upload library showed an "Empty"
        badge directly above its own category list."""
        self.service.settings_service.require_settings.return_value = (
            self._make_chat_settings(media_source_root="/media/root")
        )
        # Rows exist, but none of them are google_drive: the legacy
        # source-scoped query finds nothing; the tenant-wide count is truth.
        self.service.media_repo.get_active_by_source_type.return_value = []
        self.service.media_repo.count_active.return_value = 3

        state = self.service.get_setup_state(-1001234567890)

        assert state["media_count"] == 3
        assert state["media_indexed"] is True
        self.service.media_repo.count_active.assert_called_once_with("uuid-123")

    def test_uploads_count_without_a_configured_folder(self):
        """#877: an upload-only tenant has media and no folder — the card
        must not call a populated library Empty. folder_configured remains
        its own independent fact; the count is not gated on it."""
        self.service.media_repo.count_active.return_value = 2

        state = self.service.get_setup_state(-1001234567890)

        assert state["media_folder_configured"] is False
        assert state["media_count"] == 2
        assert state["media_indexed"] is True

    def test_media_indexed(self):
        """Configured folder with media items shows indexed."""
        self.service.settings_service.require_settings.return_value = (
            self._make_chat_settings(media_source_root="folder123")
        )
        self.service.media_repo.count_active.return_value = 3

        state = self.service.get_setup_state(-1001234567890)

        assert state["media_folder_configured"] is True
        assert state["media_folder_id"] == "folder123"
        assert state["media_indexed"] is True
        assert state["media_count"] == 3

    def test_posting_active(self):
        """Recent post within 48h makes posting_active True.

        posted_at is NAIVE, because that is what a `timestamp without time
        zone` column returns. This mock used to supply an aware value, which
        sidestepped the aware/naive TypeError entirely — so the test named for
        this behaviour passed against the broken code too (#918 follow-up).
        """
        self.service.history_repo.get_recent_posts.return_value = [
            Mock(posted_at=datetime.utcnow() - timedelta(hours=1)),
        ]

        state = self.service.get_setup_state(-1001234567890)

        assert state["posting_active"] is True
        assert state["last_post_at"] is not None

    def test_instagram_check_exception_returns_disconnected(self):
        """Exception in Instagram check returns disconnected gracefully."""
        self.service.ig_account_service.get_active_account.side_effect = Exception(
            "DB error"
        )

        state = self.service.get_setup_state(-1001234567890)

        assert state["instagram_connected"] is False


@pytest.mark.unit
class TestIsTokenStale:
    """Tests for is_token_stale() utility function."""

    def test_fresh_token_not_stale(self):
        """Token expiring in the future is not stale."""
        token = Mock(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        assert is_token_stale(token) is False

    def test_recently_expired_not_stale(self):
        """Token expired within 7 days is not stale."""
        token = Mock(
            expires_at=datetime.now(timezone.utc) - timedelta(days=TOKEN_STALE_DAYS - 1)
        )
        assert is_token_stale(token) is False

    def test_expired_over_threshold_is_stale(self):
        """Token expired > 7 days ago is stale."""
        token = Mock(
            expires_at=datetime.now(timezone.utc) - timedelta(days=TOKEN_STALE_DAYS + 1)
        )
        assert is_token_stale(token) is True

    def test_no_expiry_not_stale(self):
        """Token with no expires_at is not stale."""
        token = Mock(expires_at=None)
        assert is_token_stale(token) is False


@pytest.mark.unit
class TestFormatSetupStatus:
    """Tests for SetupStateService static formatters."""

    def test_gdrive_needs_reconnect_format(self):
        """Stale GDrive token formats as warning."""
        line, is_configured = SetupStateService._fmt_gdrive(
            {
                "gdrive_connected": True,
                "gdrive_needs_reconnect": True,
                "gdrive_email": "user@gmail.com",
            }
        )
        assert "Needs Reconnection" in line
        assert is_configured is False

    def test_gdrive_connected_format(self):
        """Connected GDrive formats with email."""
        line, is_configured = SetupStateService._fmt_gdrive(
            {
                "gdrive_connected": True,
                "gdrive_needs_reconnect": False,
                "gdrive_email": "user@gmail.com",
            }
        )
        assert "user@gmail.com" in line
        assert is_configured is True

    def test_gdrive_disconnected_format(self):
        """Disconnected GDrive formats as warning."""
        line, is_configured = SetupStateService._fmt_gdrive(
            {
                "gdrive_connected": False,
                "gdrive_needs_reconnect": False,
                "gdrive_email": None,
            }
        )
        assert "Not connected" in line
        assert is_configured is False


@pytest.mark.unit
class TestActivityCheckAgainstNaiveTimestamps:
    """#918 follow-up — regression cover for the defect that actually shipped.

    `posting_active` was silently False for every tenant that had ever posted:
    `_check_activity` computed `now(timezone.utc) - posted_at` with a NAIVE
    `posted_at`, and the resulting TypeError was swallowed by a broad `except`
    logging at debug. `last_post_at` is assigned *before* that line, so the card
    kept showing a last-post time while the flag stayed False — the failure
    presented as data rather than as an error.

    Nothing in this suite could detect that. The pre-existing
    `test_posting_active` supplied an already-AWARE `posted_at`, which is
    precisely the input that does not trigger it, so the fix would have shipped
    with zero protection. These tests use the naive shape a
    `timestamp without time zone` column actually returns, and go red if the
    coercion is removed.
    """

    def setup_method(self):
        self.service = SetupStateService()
        self.service.settings_service = MagicMock()
        self.service.ig_account_service = MagicMock()
        self.service.token_repo = MagicMock()
        self.service.media_repo = MagicMock()
        self.service.queue_repo = MagicMock()
        self.service.history_repo = MagicMock()
        self.service.queue_repo.get_all.return_value = []

    def _recent_post(self, age_hours):
        """A history row shaped the way the database returns one: naive."""
        naive = datetime.utcnow() - timedelta(hours=age_hours)
        assert naive.tzinfo is None, "fixture must stay naive — an aware value hid #918"
        return Mock(posted_at=naive)

    def test_a_naive_recent_post_sets_posting_active(self):
        self.service.history_repo.get_recent_posts.return_value = [self._recent_post(1)]

        activity = self.service._check_activity("tenant-uuid-1")

        assert activity["posting_active"] is True, (
            "a 1-hour-old post left posting_active False — the aware/naive "
            "TypeError is being swallowed again (#918)"
        )
        assert activity["last_post_at"] is not None

    def test_a_naive_old_post_leaves_posting_active_false(self):
        """The negative control: False must mean 'stale', not 'crashed'.

        Without this, a test asserting only the True case cannot tell a working
        check from one that always returns the default.
        """
        self.service.history_repo.get_recent_posts.return_value = [
            self._recent_post(72)
        ]

        with patch("src.services.core.setup_state_service.logger") as mock_logger:
            activity = self.service._check_activity("tenant-uuid-1")

        assert activity["posting_active"] is False
        assert activity["last_post_at"] is not None
        # Without this the test cannot discriminate: under the #918 defect the
        # flag is False because the comparison RAISED, not because the post is
        # stale, and a bare `is False` passes for the wrong reason.
        mock_logger.exception.assert_not_called()

    def test_an_unexpected_error_is_logged_loudly_not_swallowed_at_debug(self):
        """The silence, not the breadth, is what made #918 invisible."""
        self.service.history_repo.get_recent_posts.side_effect = TypeError("boom")

        with patch("src.services.core.setup_state_service.logger") as mock_logger:
            activity = self.service._check_activity("tenant-uuid-1")

        assert activity["posting_active"] is False
        mock_logger.exception.assert_called_once()

    def test_a_database_error_stays_quiet(self):
        """The expected failure at this boundary keeps its debug-level handling."""
        self.service.history_repo.get_recent_posts.side_effect = SQLAlchemyError("db")

        with patch("src.services.core.setup_state_service.logger") as mock_logger:
            activity = self.service._check_activity("tenant-uuid-1")

        assert activity["posting_active"] is False
        mock_logger.exception.assert_not_called()
        mock_logger.debug.assert_called_once()
