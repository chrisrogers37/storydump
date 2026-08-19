"""Tests for HealthCheckService."""

import pytest
from unittest.mock import MagicMock, Mock, patch
from datetime import datetime, timedelta, timezone

from src.services.core.health_check import HealthCheckService
from src.models.posting_queue import PostingQueue


@pytest.mark.unit
class TestHealthCheckService:
    """Test suite for HealthCheckService."""

    @pytest.fixture
    def health_service(self):
        """Create HealthCheckService with mocked dependencies."""
        service = HealthCheckService()
        service.queue_repo = Mock()
        service.history_repo = Mock()
        return service

    @patch("src.services.core.health_check.BaseRepository")
    def test_check_database_healthy(self, mock_base_repo, health_service):
        """Test database check returns healthy when DB is accessible."""
        result = health_service._check_database()

        mock_base_repo.check_connection.assert_called_once()
        assert result["healthy"] is True
        assert "Database connection OK" in result["message"]

    @patch("src.services.core.health_check.BaseRepository")
    def test_check_database_unhealthy(self, mock_base_repo, health_service):
        """Test database check returns unhealthy when DB fails."""
        mock_base_repo.check_connection.side_effect = Exception("Connection refused")

        result = health_service._check_database()

        assert result["healthy"] is False
        assert "Database error" in result["message"]

    @patch("src.services.core.health_check.settings")
    def test_check_telegram_config_valid(self, mock_settings, health_service):
        """Test Telegram config check with valid configuration."""
        mock_settings.TELEGRAM_BOT_TOKEN = "123456:ABC-DEF1234ghIkl"
        mock_settings.TELEGRAM_CHANNEL_ID = -1001234567890

        result = health_service._check_telegram_config()

        assert result["healthy"] is True
        assert "Telegram configuration OK" in result["message"]

    @patch("src.services.core.health_check.settings")
    def test_check_telegram_config_missing_token(self, mock_settings, health_service):
        """Test Telegram config check with missing token."""
        mock_settings.TELEGRAM_BOT_TOKEN = ""
        mock_settings.TELEGRAM_CHANNEL_ID = -1001234567890

        result = health_service._check_telegram_config()

        assert result["healthy"] is False
        assert "token" in result["message"].lower()

    @patch("src.services.core.health_check.settings")
    def test_check_telegram_config_missing_channel(self, mock_settings, health_service):
        """Test Telegram config check with missing channel ID."""
        mock_settings.TELEGRAM_BOT_TOKEN = "123456:ABC"
        mock_settings.TELEGRAM_CHANNEL_ID = None

        result = health_service._check_telegram_config()

        assert result["healthy"] is False
        assert "channel" in result["message"].lower()

    def test_check_queue_healthy(self, health_service):
        """Test queue check returns healthy with normal queue."""
        health_service.queue_repo.count_pending.return_value = 5
        health_service.queue_repo.get_oldest_pending.return_value = None

        result = health_service._check_queue()

        assert result["healthy"] is True
        assert result["pending_count"] == 5

    def test_check_queue_backlog(self, health_service):
        """Test queue check detects backlog when too many pending."""
        health_service.queue_repo.count_pending.return_value = 15
        health_service.queue_repo.get_oldest_pending.return_value = None

        result = health_service._check_queue()

        assert result["healthy"] is False
        assert "backlog" in result["message"].lower()
        assert result["pending_count"] == 15

    def test_check_queue_stale_items(self, health_service):
        """Test queue check detects stale items."""
        health_service.queue_repo.count_pending.return_value = 5

        # Create mock old queue item
        old_item = Mock()
        old_item.created_at = datetime.now(timezone.utc) - timedelta(hours=8)
        health_service.queue_repo.get_oldest_pending.return_value = old_item

        result = health_service._check_queue()

        assert result["healthy"] is False
        assert "hours" in result["message"].lower()

    def test_check_queue_error(self, health_service):
        """Test queue check handles errors gracefully."""
        health_service.queue_repo.count_pending.side_effect = Exception("DB error")

        result = health_service._check_queue()

        assert result["healthy"] is False
        assert "error" in result["message"].lower()

    def test_check_recent_posts_healthy(self, health_service):
        """Test recent posts check with healthy post history."""
        mock_post1 = Mock(success=True)
        mock_post2 = Mock(success=True)
        mock_post3 = Mock(success=False)
        health_service.history_repo.get_recent_posts.return_value = [
            mock_post1,
            mock_post2,
            mock_post3,
        ]

        result = health_service._check_recent_posts()

        assert result["healthy"] is True
        assert result["recent_count"] == 3
        assert result["successful_count"] == 2
        assert "2/3" in result["message"]

    def test_check_recent_posts_no_activity(self, health_service):
        """Test recent posts check with no posts."""
        health_service.history_repo.get_recent_posts.return_value = []

        result = health_service._check_recent_posts()

        assert result["healthy"] is False
        assert "no posts" in result["message"].lower()
        assert result["recent_count"] == 0

    def test_check_recent_posts_error(self, health_service):
        """Test recent posts check handles errors gracefully."""
        health_service.history_repo.get_recent_posts.side_effect = Exception("DB error")

        result = health_service._check_recent_posts()

        assert result["healthy"] is False
        assert "error" in result["message"].lower()

    @patch("src.services.core.health_check.SettingsService")
    @patch("src.services.core.health_check.BaseRepository")
    @patch("src.services.core.health_check.settings")
    def test_check_all_all_healthy(
        self,
        mock_settings,
        mock_base_repo,
        mock_settings_service_cls,
        health_service,
    ):
        """Test check_all wires every sub-check (overall status varies with
        IG creds availability — the meaningful assertion is which sub-checks
        ran, not the rolled-up status)."""
        mock_settings.TELEGRAM_BOT_TOKEN = "123456:ABC"
        mock_settings.TELEGRAM_CHANNEL_ID = -1001234567890
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        # IG creds not configured → instagram_api check reports unhealthy
        # (env "phase: Telegram-only" gate is gone in the env→DB refactor).
        mock_settings.INSTAGRAM_ACCOUNT_ID = None
        mock_settings.FACEBOOK_APP_ID = None
        # Admin chat has media sync disabled → media_sync health is "Disabled".
        mock_settings_service_cls.return_value.__enter__.return_value.get_settings.return_value = Mock(
            media_sync_enabled=False
        )
        health_service.queue_repo.count_pending.return_value = 5
        health_service.queue_repo.get_oldest_pending.return_value = None
        mock_post = Mock(success=True)
        health_service.history_repo.get_recent_posts.return_value = [mock_post]

        # Mock media pool: no active chats → healthy
        health_service._settings_service = Mock()
        health_service._settings_service.get_all_active_chats.return_value = []

        # Mock loop liveness so all loops report alive
        all_alive = {
            name: {"alive": True, "message": "OK", "expected_interval_s": 60}
            for name in [
                "scheduler",
                "lock_cleanup",
                "cloud_cleanup",
                "media_sync",
                "transaction_cleanup",
            ]
        }
        with patch(
            "src.services.core.loops.heartbeat.get_loop_liveness",
            return_value=all_alive,
        ):
            result = health_service.check_all()

        # Overall is unhealthy because IG creds aren't configured; the
        # other checks pass. Asserting the keys are present is the test's
        # real intent — it's a smoke test that check_all wires every
        # sub-check.
        assert "database" in result["checks"]
        assert "telegram" in result["checks"]
        assert "instagram_api" in result["checks"]
        assert "queue" in result["checks"]
        assert "recent_posts" in result["checks"]
        assert "media_sync" in result["checks"]
        assert "media_pool" in result["checks"]
        assert "loop_liveness" in result["checks"]
        assert "timestamp" in result
        assert result["checks"]["telegram"]["healthy"] is True
        assert result["checks"]["media_sync"]["enabled"] is False

    @patch("src.services.core.health_check.BaseRepository")
    @patch("src.services.core.health_check.settings")
    def test_check_all_some_unhealthy(
        self, mock_settings, mock_base_repo, health_service
    ):
        """Test check_all returns unhealthy when any check fails."""
        # Telegram unhealthy
        mock_settings.TELEGRAM_BOT_TOKEN = ""
        mock_settings.TELEGRAM_CHANNEL_ID = None

        # Queue healthy
        health_service.queue_repo.count_pending.return_value = 5
        health_service.queue_repo.get_oldest_pending.return_value = None

        # Recent posts healthy
        mock_post = Mock(success=True)
        health_service.history_repo.get_recent_posts.return_value = [mock_post]

        result = health_service.check_all()

        assert result["status"] == "unhealthy"
        assert result["checks"]["database"]["healthy"] is True
        assert result["checks"]["telegram"]["healthy"] is False

    def test_check_all_returns_timestamp(self, health_service):
        """Test check_all includes ISO timestamp."""
        # Mock all dependencies to avoid real checks
        with (
            patch("src.services.core.health_check.BaseRepository"),
            patch("src.services.core.health_check.settings") as mock_settings,
        ):
            mock_settings.TELEGRAM_BOT_TOKEN = "token"
            mock_settings.TELEGRAM_CHANNEL_ID = -123
            health_service.queue_repo.count_pending.return_value = 0
            health_service.queue_repo.get_oldest_pending.return_value = None
            health_service.history_repo.get_recent_posts.return_value = []

            result = health_service.check_all()

            assert "timestamp" in result
            # Verify it's a valid ISO timestamp
            datetime.fromisoformat(result["timestamp"])

    # ==================== Media Sync Health Check Tests ====================

    @patch("src.services.core.health_check.SettingsService")
    @patch("src.services.core.health_check.settings")
    def test_check_media_sync_disabled(
        self, mock_settings, mock_settings_service_cls, health_service
    ):
        """Returns healthy with enabled=False when admin chat has sync disabled."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        admin_chat = Mock(media_sync_enabled=False)
        mock_settings_service = (
            mock_settings_service_cls.return_value.__enter__.return_value
        )
        mock_settings_service.get_settings.return_value = admin_chat

        result = health_service._check_media_sync()

        assert result["healthy"] is True
        assert result["enabled"] is False
        assert "Disabled" in result["message"]

    @patch("src.services.core.health_check.SettingsService")
    @patch("src.services.core.health_check.settings")
    def test_check_media_sync_healthy(
        self, mock_settings, mock_settings_service_cls, health_service
    ):
        """Returns healthy when provider accessible and last sync succeeded."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.MEDIA_DIR = "/media/stories"
        admin_chat = Mock(
            media_sync_enabled=True,
            media_source_type="local",
            media_source_root="/media/stories",
        )
        mock_settings_service = (
            mock_settings_service_cls.return_value.__enter__.return_value
        )
        mock_settings_service.get_settings.return_value = admin_chat
        mock_settings.MEDIA_SYNC_INTERVAL_SECONDS = 300

        mock_sync_info = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "success": True,
            "status": "completed",
            "result": {"new": 2, "errors": 0},
            "duration_ms": 1500,
            "triggered_by": "scheduler",
        }

        with (
            patch("src.services.core.media_sync.MediaSyncService") as mock_sync_class,
            patch(
                "src.services.media_sources.factory.MediaSourceFactory"
            ) as mock_factory,
        ):
            mock_sync_class.return_value.get_last_sync_info.return_value = (
                mock_sync_info
            )
            mock_provider = Mock()
            mock_provider.is_configured.return_value = True
            mock_factory.create.return_value = mock_provider

            result = health_service._check_media_sync()

        assert result["healthy"] is True
        assert result["enabled"] is True
        assert result["source_type"] == "local"
        assert "OK" in result["message"]

    @patch("src.services.core.health_check.SettingsService")
    @patch("src.services.core.health_check.settings")
    def test_check_media_sync_no_runs(
        self, mock_settings, mock_settings_service_cls, health_service
    ):
        """Returns unhealthy when no sync runs recorded."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.MEDIA_DIR = "/media/stories"
        admin_chat = Mock(
            media_sync_enabled=True,
            media_source_type="local",
            media_source_root="/media/stories",
        )
        mock_settings_service = (
            mock_settings_service_cls.return_value.__enter__.return_value
        )
        mock_settings_service.get_settings.return_value = admin_chat

        with (
            patch("src.services.core.media_sync.MediaSyncService") as mock_sync_class,
            patch(
                "src.services.media_sources.factory.MediaSourceFactory"
            ) as mock_factory,
        ):
            mock_sync_class.return_value.get_last_sync_info.return_value = None
            mock_provider = Mock()
            mock_provider.is_configured.return_value = True
            mock_factory.create.return_value = mock_provider

            result = health_service._check_media_sync()

        assert result["healthy"] is False
        assert result["enabled"] is True
        assert "No sync runs" in result["message"]

    @patch("src.services.core.health_check.SettingsService")
    @patch("src.services.core.health_check.settings")
    def test_check_media_sync_stale(
        self, mock_settings, mock_settings_service_cls, health_service
    ):
        """Returns unhealthy when last sync is stale (>3x interval)."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.MEDIA_DIR = "/media/stories"
        admin_chat = Mock(
            media_sync_enabled=True,
            media_source_type="local",
            media_source_root="/media/stories",
        )
        mock_settings_service = (
            mock_settings_service_cls.return_value.__enter__.return_value
        )
        mock_settings_service.get_settings.return_value = admin_chat
        mock_settings.MEDIA_SYNC_INTERVAL_SECONDS = 300  # 5 min

        # Last sync was 30 minutes ago (6x the interval)
        old_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        mock_sync_info = {
            "started_at": old_time.isoformat(),
            "completed_at": old_time.isoformat(),
            "success": True,
            "status": "completed",
            "result": {"new": 0, "errors": 0},
            "duration_ms": 500,
            "triggered_by": "scheduler",
        }

        with (
            patch("src.services.core.media_sync.MediaSyncService") as mock_sync_class,
            patch(
                "src.services.media_sources.factory.MediaSourceFactory"
            ) as mock_factory,
        ):
            mock_sync_class.return_value.get_last_sync_info.return_value = (
                mock_sync_info
            )
            mock_provider = Mock()
            mock_provider.is_configured.return_value = True
            mock_factory.create.return_value = mock_provider

            result = health_service._check_media_sync()

        assert result["healthy"] is False
        assert "stale" in result["message"]

    @patch("src.services.core.health_check.SettingsService")
    @patch("src.services.core.health_check.settings")
    def test_check_media_sync_provider_not_accessible(
        self, mock_settings, mock_settings_service_cls, health_service
    ):
        """Returns unhealthy when provider is not accessible."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        admin_chat = Mock(
            media_sync_enabled=True,
            media_source_type="google_drive",
            media_source_root="folder123",
        )
        mock_settings_service_cls.return_value.__enter__.return_value.get_settings.return_value = admin_chat

        with patch(
            "src.services.media_sources.factory.MediaSourceFactory"
        ) as mock_factory:
            mock_provider = Mock()
            mock_provider.is_configured.return_value = False
            mock_factory.create.return_value = mock_provider

            result = health_service._check_media_sync()

        assert result["healthy"] is False
        assert result["enabled"] is True
        assert "not accessible" in result["message"]
        assert result["source_type"] == "google_drive"

    @patch("src.services.core.health_check.SettingsService")
    @patch("src.services.core.health_check.settings")
    def test_check_media_sync_last_run_failed(
        self, mock_settings, mock_settings_service_cls, health_service
    ):
        """Returns unhealthy when last sync run failed."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        mock_settings.MEDIA_DIR = "/media/stories"
        admin_chat = Mock(
            media_sync_enabled=True,
            media_source_type="local",
            media_source_root="/media/stories",
        )
        mock_settings_service = (
            mock_settings_service_cls.return_value.__enter__.return_value
        )
        mock_settings_service.get_settings.return_value = admin_chat

        mock_sync_info = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "success": False,
            "status": "failed",
            "result": None,
            "duration_ms": 100,
            "triggered_by": "scheduler",
        }

        with (
            patch("src.services.core.media_sync.MediaSyncService") as mock_sync_class,
            patch(
                "src.services.media_sources.factory.MediaSourceFactory"
            ) as mock_factory,
        ):
            mock_sync_class.return_value.get_last_sync_info.return_value = (
                mock_sync_info
            )
            mock_provider = Mock()
            mock_provider.is_configured.return_value = True
            mock_factory.create.return_value = mock_provider

            result = health_service._check_media_sync()

        assert result["healthy"] is False
        assert "failed" in result["message"].lower()
        assert result["source_type"] == "local"

    # ==================== Media Pool Health Check Tests ====================

    @pytest.fixture
    def pool_service(self):
        """Create HealthCheckService with mocked pool dependencies."""
        service = HealthCheckService()
        service.queue_repo = Mock()
        service.history_repo = Mock()
        service._media_repo = Mock()
        service._settings_service = Mock()
        return service

    @pytest.fixture
    def mock_chat(self):
        """Create a mock chat_settings for pool tests."""
        chat = Mock()
        chat.id = 1
        chat.posts_per_day = 4
        chat.telegram_chat_id = -123
        return chat

    def test_check_media_pool_for_chat_healthy(self, pool_service, mock_chat):
        """Pool check returns healthy when all categories have plenty of runway."""
        pool_service._media_repo.count_eligible_by_category.return_value = {
            "memes": 50,
            "merch": 30,
        }

        result = pool_service.check_media_pool_for_chat(-123, chat_settings=mock_chat)

        assert result["total_eligible"] == 80
        assert result["posts_per_day"] == 4
        assert len(result["categories"]) == 2
        assert result["warnings"] == []
        # 2 categories, 4 posts/day -> 2 posts/day each
        # memes: 50/2 = 25 days, merch: 30/2 = 15 days
        memes_cat = next(c for c in result["categories"] if c["category"] == "memes")
        assert memes_cat["runway_days"] == 25.0
        assert memes_cat["eligible"] == 50

    def test_check_media_pool_for_chat_warning(self, pool_service, mock_chat):
        """Pool check returns warnings when a category is running low."""
        pool_service._media_repo.count_eligible_by_category.return_value = {
            "memes": 10,  # 10/2 = 5 days -> warning
            "merch": 30,  # 30/2 = 15 days -> OK
        }

        result = pool_service.check_media_pool_for_chat(-123, chat_settings=mock_chat)

        assert len(result["warnings"]) == 1
        assert "memes" in result["warnings"][0]
        assert "LOW" in result["warnings"][0]

    def test_check_media_pool_for_chat_critical(self, pool_service, mock_chat):
        """Pool check returns critical warning when category nearly empty."""
        pool_service._media_repo.count_eligible_by_category.return_value = {
            "memes": 2,  # 2/2 = 1 day -> critical
            "merch": 30,
        }

        result = pool_service.check_media_pool_for_chat(-123, chat_settings=mock_chat)

        assert len(result["warnings"]) == 1
        assert "CRITICAL" in result["warnings"][0]
        assert "memes" in result["warnings"][0]

    def test_check_media_pool_for_chat_empty(self, pool_service, mock_chat):
        """Pool check handles no eligible media gracefully."""
        pool_service._media_repo.count_eligible_by_category.return_value = {}

        result = pool_service.check_media_pool_for_chat(-123, chat_settings=mock_chat)

        assert result["total_eligible"] == 0
        assert len(result["categories"]) == 0
        assert "No eligible media" in result["warnings"][0]

    def test_check_media_pool_for_chat_single_category(self, pool_service):
        """Pool check works with single category (full posts_per_day allocation)."""
        mock_chat = Mock()
        mock_chat.id = 1
        mock_chat.posts_per_day = 3
        mock_chat.telegram_chat_id = -123

        pool_service._media_repo.count_eligible_by_category.return_value = {
            "memes": 15,  # 15/3 = 5 days -> warning
        }

        result = pool_service.check_media_pool_for_chat(-123, chat_settings=mock_chat)

        assert len(result["categories"]) == 1
        cat = result["categories"][0]
        assert cat["posts_per_day_share"] == 3.0
        assert cat["runway_days"] == 5.0
        assert len(result["warnings"]) == 1

    def test_check_media_pool_aggregated_healthy(self, pool_service, mock_chat):
        """Aggregated pool check returns healthy across all tenants."""
        mock_chat.posts_per_day = 2
        pool_service._settings_service.get_all_active_chats.return_value = [mock_chat]
        pool_service._media_repo.count_eligible_by_category.return_value = {
            "memes": 50,
        }

        result = pool_service._check_media_pool()

        assert result["healthy"] is True
        assert ">" in result["message"]

    def test_check_media_pool_aggregated_warning(self, pool_service, mock_chat):
        """Aggregated pool check returns unhealthy when worst category is low."""
        pool_service._settings_service.get_all_active_chats.return_value = [mock_chat]
        pool_service._media_repo.count_eligible_by_category.return_value = {
            "memes": 5,  # 5/4 = 1.25 days -> critical
        }

        result = pool_service._check_media_pool()

        assert result["healthy"] is False
        assert "Critical" in result["message"]
        assert result["worst_category"]["category"] == "memes"

    def test_check_media_pool_no_active_chats(self, pool_service):
        """Aggregated pool check handles no active chats."""
        pool_service._settings_service.get_all_active_chats.return_value = []

        result = pool_service._check_media_pool()

        assert result["healthy"] is True
        assert "No active chats" in result["message"]

    def test_format_pool_alert_with_warnings(self, pool_service):
        """Format alert returns text when categories are low."""
        pool_info = {
            "categories": [
                {"category": "memes", "eligible": 3, "runway_days": 1.5},
                {"category": "merch", "eligible": 50, "runway_days": 25.0},
            ],
            "warnings": ["LOW: 'memes'..."],
        }

        result = pool_service.format_pool_alert(pool_info)

        assert result is not None
        assert "memes" in result
        assert "3 items left" in result
        assert "merch" not in result  # merch is above threshold

    def test_format_pool_alert_no_warnings(self, pool_service):
        """Format alert returns None when all categories are healthy."""
        pool_info = {
            "categories": [
                {"category": "memes", "eligible": 50, "runway_days": 25.0},
            ],
            "warnings": [],
        }

        result = pool_service.format_pool_alert(pool_info)

        assert result is None

    # ==================== Google Drive Token Health Tests ====================

    @pytest.fixture
    def token_service(self):
        """Create HealthCheckService with mocked token dependencies."""
        service = HealthCheckService()
        service.queue_repo = Mock()
        service.history_repo = Mock()
        service._settings_service = Mock()
        service._token_service = Mock()
        return service

    def _gdrive_chat(self):
        """Create a mock chat configured for Google Drive."""
        chat = Mock()
        chat.id = 1
        chat.telegram_chat_id = -123
        chat.media_sync_enabled = True
        chat.media_source_type = "google_drive"
        return chat

    def test_gdrive_token_healthy(self, token_service):
        """Returns healthy when token has plenty of time left."""
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": True,
            "exists": True,
            "expires_in_hours": 30 * 24,  # 30 days
            "needs_refresh": False,
            "auto_refreshable": False,
            "error": None,
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is True
        assert result["expires_in_days"] == 30.0

    def test_gdrive_token_healthy_auto_refreshable(self, token_service):
        """Access token expired but refresh token exists — healthy."""
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": True,
            "exists": True,
            "expires_in_hours": 0,
            "needs_refresh": True,
            "auto_refreshable": True,
            "error": None,
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is True
        assert "auto-refresh" in result["message"]

    def test_gdrive_token_warning_no_refresh(self, token_service):
        """Returns unhealthy when token expires soon and no refresh token."""
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": True,
            "exists": True,
            "expires_in_hours": 3 * 24,  # 3 days
            "needs_refresh": True,
            "auto_refreshable": False,
            "error": None,
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is False
        assert result["expires_in_days"] == 3.0
        assert "3 days" in result["message"]

    def test_gdrive_token_warning_with_refresh(self, token_service):
        """Access token expiring soon but refresh token exists — healthy."""
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": True,
            "exists": True,
            "expires_in_hours": 3 * 24,
            "needs_refresh": True,
            "auto_refreshable": True,
            "error": None,
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is True

    def test_gdrive_token_expired_no_refresh(self, token_service):
        """Expired access token with no refresh token — unhealthy."""
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": False,
            "exists": True,
            "expires_in_hours": 0,
            "needs_refresh": False,
            "auto_refreshable": False,
            "error": "Token expired and no valid refresh token",
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is False
        assert "expired" in result["message"]

    def test_gdrive_token_not_found(self, token_service):
        """Returns unhealthy when no token exists."""
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": False,
            "exists": False,
            "expires_in_hours": None,
            "needs_refresh": False,
            "auto_refreshable": False,
            "error": "No google_drive token found for this chat",
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is False
        assert "No Google Drive token" in result["message"]

    def test_gdrive_token_sync_disabled(self, token_service):
        """Returns healthy with enabled=False when sync is disabled."""
        chat = Mock()
        chat.media_sync_enabled = False

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is True
        assert result["enabled"] is False

    def test_gdrive_token_local_source(self, token_service):
        """Returns healthy with enabled=False when source is local."""
        chat = Mock()
        chat.media_sync_enabled = True
        chat.media_source_type = "local"

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is True
        assert result["enabled"] is False

    def test_gdrive_refresh_token_expired_testing_mode(self, token_service):
        """Refresh token past TTL in Testing mode triggers unhealthy.

        The access token may still be valid (not yet expired) while the
        refresh token has already passed its 7-day TTL. The Testing mode
        check must fire before the generic not-valid guard.
        """
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": True,
            "exists": True,
            "expires_in_hours": 48,
            "needs_refresh": False,
            "auto_refreshable": True,
            "refresh_token_exists": True,
            "refresh_token_age_days": 8.0,
            "refresh_token_expires_in_days": -1.0,
            "error": None,
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is False
        assert "testing mode" in result["message"].lower()
        assert "7-day" in result["message"].lower()

    def test_gdrive_refresh_token_expiring_soon_testing_mode(self, token_service):
        """Refresh token approaching TTL triggers warning."""
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": True,
            "exists": True,
            "expires_in_hours": 0,
            "needs_refresh": True,
            "auto_refreshable": True,
            "refresh_token_exists": True,
            "refresh_token_age_days": 6.5,
            "refresh_token_expires_in_days": 0.5,
            "error": None,
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is False
        assert "Testing mode" in result["message"]
        assert "reconnect" in result["message"].lower()

    def test_gdrive_refresh_token_no_age_data(self, token_service):
        """No refresh token age data falls through to normal checks."""
        chat = self._gdrive_chat()
        token_service._token_service.check_token_health_for_chat.return_value = {
            "valid": True,
            "exists": True,
            "expires_in_hours": 30 * 24,
            "needs_refresh": False,
            "auto_refreshable": True,
            "refresh_token_exists": True,
            "refresh_token_age_days": None,
            "refresh_token_expires_in_days": None,
            "error": None,
        }

        result = token_service.check_gdrive_token_for_chat(-123, chat_settings=chat)

        assert result["healthy"] is True

    def test_format_token_alert_expiring(self, token_service):
        """Format alert includes the expiry countdown and points at /start."""
        token_info = {"healthy": False, "expires_in_days": 3}

        with patch("src.services.core.health_check.settings") as mock_settings:
            mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
            result = token_service.format_token_alert(token_info)

        assert result is not None
        assert "3 day" in result
        assert "/start" in result

    def test_format_token_alert_carries_no_oauth_deep_link(self, token_service):
        """The alert never hands out a start link (#725).

        This alert is raised by the scheduler for a chat, so there is no member
        to sign a URL token for — and an unsigned link would be a start
        endpoint invocation for a chat_id anyone can read off the message.
        """
        token_info = {"healthy": False, "expires_in_days": 0}

        with patch("src.services.core.health_check.settings") as mock_settings:
            mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
            result = token_service.format_token_alert(token_info)

        assert "/auth/google-drive/start" not in result

    def test_format_token_alert_expired(self, token_service):
        """Format alert for already-expired token."""
        token_info = {"healthy": False, "expires_in_days": 0}

        with patch("src.services.core.health_check.settings") as mock_settings:
            mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
            result = token_service.format_token_alert(token_info)

        assert result is not None
        assert "expired" in result
        assert "reconnect required" in result

    def test_format_token_alert_healthy(self, token_service):
        """Format alert returns None for healthy token."""
        token_info = {"healthy": True, "expires_in_days": 30}

        result = token_service.format_token_alert(token_info)

        assert result is None


@pytest.mark.unit
class TestMediaPoolAggregateSurvivesOneUnreachableTenant:
    """#783 site 1: `_check_media_pool` wrapped its whole per-tenant loop in one
    `try`, so the first unreachable tenant aborted the sweep and every tenant
    after it was never examined.

    This one is worse than a silenced alert. The function's entire job is a
    CROSS-TENANT AGGREGATE — the lowest runway anywhere — and an aggregate
    computed over an arbitrary prefix is not a smaller answer, it is a WRONG
    one. Nothing in the return value distinguished "worst of 12" from
    "worst of 3", which is the same never-happened-versus-lost confusion #764
    and #782 turned on. So isolation alone is not the fix: the population has
    to be disclosed too.
    """

    @pytest.fixture
    def pool_service(self):
        service = HealthCheckService()
        service.queue_repo = Mock()
        service.history_repo = Mock()
        service._media_repo = Mock()
        service._settings_service = Mock()
        return service

    def _chats(self, n):
        return [Mock(telegram_chat_id=-(i + 1)) for i in range(n)]

    def _pool(self, runway):
        return {
            "categories": [
                {"category": "memes", "runway_days": runway, "eligible": int(runway)}
            ]
        }

    def _service(self, pool_service, failing_index):
        chats = self._chats(3)
        pool_service._settings_service.get_all_active_chats.return_value = chats

        def side_effect(chat_id, chat_settings=None):
            if chat_id == chats[failing_index].telegram_chat_id:
                raise RuntimeError("tenant unreachable")
            # the LAST tenant holds the critical runway, so a sweep that stops
            # early cannot see it
            return self._pool(1.0 if chat_id == chats[-1].telegram_chat_id else 99.0)

        pool_service.check_media_pool_for_chat = Mock(side_effect=side_effect)
        return pool_service

    def test_a_failing_first_tenant_does_not_hide_a_critical_one_behind_it(
        self, pool_service
    ):
        """THE REGRESSION. Tenant 1 raises; tenant 3 is critically low. Before
        the fix the sweep aborted at tenant 1 and the critical pool was never
        seen at all."""
        svc = self._service(pool_service, failing_index=0)

        with patch("src.services.core.health_check.logger"):
            result = svc._check_media_pool()

        assert svc.check_media_pool_for_chat.call_count == 3, (
            "the sweep stopped early: "
            f"{svc.check_media_pool_for_chat.call_count} of 3 tenants examined"
        )
        assert result.get("worst_category", {}).get("runway_days") == 1.0, (
            f"the critical tenant behind the failure was never reported: {result}"
        )

    def test_the_aggregate_discloses_how_many_tenants_it_covers(self, pool_service):
        """An aggregate over a silently narrowed population is misleading even
        when it is computed correctly over what it saw. 'Worst of 2, 1
        unreachable' is a different claim from 'worst of 3'."""
        svc = self._service(pool_service, failing_index=0)

        with patch("src.services.core.health_check.logger"):
            result = svc._check_media_pool()

        assert result.get("tenants_checked") == 2, result
        assert result.get("tenants_unreachable") == 1, result

    def test_full_coverage_reports_zero_unreachable(self, pool_service):
        """The control. With every tenant reachable the disclosure must say so
        rather than being present-but-meaningless."""
        chats = self._chats(3)
        pool_service._settings_service.get_all_active_chats.return_value = chats
        pool_service.check_media_pool_for_chat = Mock(return_value=self._pool(99.0))

        result = pool_service._check_media_pool()

        assert result.get("tenants_checked") == 3
        assert result.get("tenants_unreachable") == 0

    def test_the_unreachable_tenant_is_surfaced_not_just_counted(self, pool_service):
        """Matching #781's precedent: the failing tenant stays visible in the
        log, named, so a permanently-unreachable one can be found. A count
        alone says something is wrong without saying which."""
        svc = self._service(pool_service, failing_index=0)

        with patch("src.services.core.health_check.logger") as log:
            svc._check_media_pool()

        lines = [str(c.args[0]) for c in log.warning.call_args_list]
        assert any("-1" in ln for ln in lines), (
            f"the unreachable tenant is not named in any warning: {lines}"
        )


@pytest.mark.unit
class TestIncompleteCoverageIsNotHealthy:
    """#791 review, blocking: isolating each tenant stopped one failure ending
    the sweep — and on its own turned a genuinely fatal condition into a
    per-item shrug. With every tenant unreachable the endpoint reported
    `healthy: True`, so a total pool outage read GREEN.

    Before isolation, any failure reached the outer handler and reported
    unhealthy. So the first version of the fix traded "one tenant hides the
    rest" for "every tenant can fail and it still looks fine", which is the
    worse of the two. This is the #764 distinction once more: a degraded state
    must not be able to masquerade as a clean one, which means some failures
    still have to fail the check rather than be counted and waved through.
    """

    @pytest.fixture
    def svc(self):
        service = HealthCheckService()
        service.queue_repo = Mock()
        service.history_repo = Mock()
        service._media_repo = Mock()
        service._settings_service = Mock()
        return service

    def _run(self, svc, n, failing, runway=99.0):
        chats = [Mock(telegram_chat_id=-(i + 1)) for i in range(n)]
        svc._settings_service.get_all_active_chats.return_value = chats

        def side_effect(chat_id, chat_settings=None):
            if chat_id in failing:
                raise RuntimeError("tenant unreachable")
            return {
                "categories": [
                    {"category": "memes", "runway_days": runway, "eligible": 9}
                ]
            }

        svc.check_media_pool_for_chat = Mock(side_effect=side_effect)
        with patch("src.services.core.health_check.logger"):
            return svc._check_media_pool()

    def test_a_total_outage_is_not_healthy(self, svc):
        """THE BLOCKING REGRESSION. Every tenant unreachable used to return
        healthy: True with nothing checked at all."""
        result = self._run(svc, 3, {-1, -2, -3})

        assert result["healthy"] is False, result
        assert result["tenants_checked"] == 0
        assert result["tenants_unreachable"] == 3

    def test_a_PARTIAL_outage_is_not_healthy_either(self, svc):
        """Broader than the report. The regression was not limited to the
        total case: one unreachable tenant among reachable healthy ones also
        returned healthy: True, because the verdict was computed from whoever
        answered. A verdict over an incomplete population cannot be a healthy
        one — the unknown tenants might be the critical ones."""
        result = self._run(svc, 3, {-1})

        assert result["healthy"] is False, result
        assert result["tenants_checked"] == 2
        assert result["tenants_unreachable"] == 1

    def test_full_coverage_and_good_runway_is_still_healthy(self, svc):
        """The control. Without it, 'healthy is False' could be satisfied by
        never returning healthy at all."""
        result = self._run(svc, 3, set())

        assert result["healthy"] is True
        assert result["tenants_unreachable"] == 0

    def test_full_coverage_with_a_critical_pool_is_still_reported_critical(self, svc):
        """The severity logic must survive the coverage gate. If the gate had
        been layered in front of it, a real critical finding on a fully-covered
        estate could have been masked by the completeness check passing."""
        result = self._run(svc, 3, set(), runway=1.0)

        assert result["healthy"] is False
        assert "memes" in result["message"]
        assert result["tenants_unreachable"] == 0


@pytest.mark.unit
class TestMediaPoolFailClosedGapsFromThe791Merge:
    """#794: the two gaps mason raised on #791 and that were deliberately left
    open, because each already failed closed and neither blocked the merge.

    Failing closed is why they were not urgent. It is also why they needed
    tests that bind to the SPECIFIC failure: a fail-closed path returns
    `healthy: False`, and so does almost every other failure, so an assertion
    on `healthy` alone would pass whether or not the gap was ever exercised —
    a green earned by nothing having been tried. Every test below therefore
    asserts what the sweep DID (call counts, which tenants were reached, which
    counter moved), not merely that the result was unhealthy.
    """

    @pytest.fixture
    def pool_service(self):
        service = HealthCheckService()
        service.queue_repo = Mock()
        service.history_repo = Mock()
        service._media_repo = Mock()
        service._settings_service = Mock()
        return service

    def _chats(self, n):
        return [Mock(telegram_chat_id=-(i + 1)) for i in range(n)]

    def _pool(self, runway):
        return {
            "categories": [
                {"category": "memes", "runway_days": runway, "eligible": int(runway)}
            ]
        }

    # --- gap 1: the per-tenant guard wrapped the CALL, not the UNIT ----------

    def test_a_malformed_category_row_is_isolated_to_its_own_tenant(self, pool_service):
        """THE REGRESSION for gap 1, and it is deliberately NOT a raising call.

        The old guard wrapped `check_media_pool_for_chat` only, so a call that
        RETURNS was already isolated. The work left outside was reading this
        tenant's categories — so the failure that discriminates the two shapes
        is a successful call returning a row with no `runway_days`. Before the
        fix that KeyError escaped to the outer handler and ended the sweep;
        after it, it is this tenant's problem alone.
        """
        chats = self._chats(3)
        pool_service._settings_service.get_all_active_chats.return_value = chats

        def side_effect(chat_id, chat_settings=None):
            if chat_id == chats[0].telegram_chat_id:
                # returns fine — a malformed ROW, not a failing call
                return {"categories": [{"category": "memes", "eligible": 4}]}
            return self._pool(1.0 if chat_id == chats[-1].telegram_chat_id else 99.0)

        pool_service.check_media_pool_for_chat = Mock(side_effect=side_effect)

        with patch("src.services.core.health_check.logger"):
            result = pool_service._check_media_pool()

        assert pool_service.check_media_pool_for_chat.call_count == 3, (
            "the malformed row ended the sweep: only "
            f"{pool_service.check_media_pool_for_chat.call_count} of 3 tenants examined"
        )
        assert result.get("worst_category", {}).get("runway_days") == 1.0, (
            f"the critical tenant behind the malformed one was never seen: {result}"
        )
        assert result["tenants_unreachable"] == 1, result
        assert result["tenants_checked"] == 2, result

    def test_a_tenant_that_fails_mid_unit_is_counted_once_not_twice(self, pool_service):
        """The counting invariant the widened guard has to preserve.

        Committing `checked` before the category loop would count a tenant that
        then fails as BOTH checked and unreachable, so the disclosure would
        over-report the population it covered — the failure mode of a fix that
        merely moves the `try` without moving the commit.
        """
        chats = self._chats(4)
        pool_service._settings_service.get_all_active_chats.return_value = chats

        def side_effect(chat_id, chat_settings=None):
            if chat_id == chats[1].telegram_chat_id:
                return {"categories": [{"category": "memes"}]}  # no runway_days
            if chat_id == chats[2].telegram_chat_id:
                raise RuntimeError("tenant unreachable")
            return self._pool(50.0)

        pool_service.check_media_pool_for_chat = Mock(side_effect=side_effect)

        with patch("src.services.core.health_check.logger"):
            result = pool_service._check_media_pool()

        assert result["tenants_checked"] + result["tenants_unreachable"] == len(
            chats
        ), (
            "coverage does not account for the population: "
            f"{result['tenants_checked']} + {result['tenants_unreachable']} != {len(chats)}"
        )
        assert (result["tenants_checked"], result["tenants_unreachable"]) == (2, 2)

    def test_a_reachable_tenant_with_no_categories_is_checked_not_unreachable(
        self, pool_service
    ):
        """THE CONTROL that stops the widened guard swallowing healthy tenants.

        An empty category list is a normal answer, not a fault. Without this, a
        fix that counted every tenant yielding no worst-category as unreachable
        would pass every assertion above while quietly reporting a fleet-wide
        outage.
        """
        chats = self._chats(2)
        pool_service._settings_service.get_all_active_chats.return_value = chats
        pool_service.check_media_pool_for_chat = Mock(return_value={"categories": []})

        result = pool_service._check_media_pool()

        assert (result["tenants_checked"], result["tenants_unreachable"]) == (2, 0)
        assert result["healthy"] is True, result

    # --- gap 2: **coverage absent from two return paths ---------------------

    def test_the_empty_population_path_discloses_coverage(self, pool_service):
        """`if not active_chats` returned no coverage keys, so a caller reading
        them got them on the normal path and not here."""
        pool_service._settings_service.get_all_active_chats.return_value = []

        result = pool_service._check_media_pool()

        assert result["tenants_checked"] == 0
        assert result["tenants_unreachable"] == 0
        assert result["healthy"] is True

    def test_the_aborted_sweep_path_discloses_coverage(self, pool_service):
        """The outer handler returned no coverage keys either.

        Asserts the enumeration was actually ATTEMPTED, not just that the
        result was unhealthy — otherwise this passes for a service that never
        ran at all, which is the vacuous shape this class exists to avoid.
        """
        pool_service._settings_service.get_all_active_chats.side_effect = RuntimeError(
            "database gone"
        )

        with patch("src.services.core.health_check.logger"):
            result = pool_service._check_media_pool()

        assert pool_service._settings_service.get_all_active_chats.called
        assert result["healthy"] is False
        assert result["tenants_checked"] == 0
        assert result["tenants_unreachable"] == 0

    def test_an_empty_population_and_an_aborted_sweep_are_distinguishable(
        self, pool_service
    ):
        """Both now report 0 of 0, so the keys alone cannot separate them — and
        conflating them is the never-happened-versus-lost confusion the keys
        were added to close. `healthy` is what carries the distinction, and
        that is asserted here rather than assumed, as a PAIR: either verdict
        alone is satisfied by an implementation that always returns it.
        """
        pool_service._settings_service.get_all_active_chats.return_value = []
        empty = pool_service._check_media_pool()

        pool_service._settings_service.get_all_active_chats.side_effect = RuntimeError(
            "database gone"
        )
        pool_service._settings_service.get_all_active_chats.return_value = None
        with patch("src.services.core.health_check.logger"):
            aborted = pool_service._check_media_pool()

        coverage_of = lambda r: (r["tenants_checked"], r["tenants_unreachable"])  # noqa: E731
        assert coverage_of(empty) == coverage_of(aborted) == (0, 0)
        assert (empty["healthy"], aborted["healthy"]) == (True, False), (
            "an aborted sweep is indistinguishable from an empty population"
        )


@pytest.mark.unit
class TestQueueAgeAcrossTheBacklogThreshold:
    """#899 — the age check ran against a NAIVE column and raised TypeError.

    Why the pre-existing coverage could not see it: `test_check_queue_healthy`
    sets `get_oldest_pending` to None, so the age branch never executes, and
    `test_check_queue_stale_items` builds its item with
    `datetime.now(timezone.utc) - timedelta(hours=8)` — an **aware** value.
    Production writes `PostingQueue.created_at` from `default=datetime.utcnow`,
    which is **naive**. The mock was more correct than the database, so the
    branch was green on a code path that raised for every real caller.

    These tests therefore build a real `PostingQueue`, letting the model's own
    default supply the timestamp, and drive `_check_queue` across the whole
    0..>threshold range — because the defect was reachable only in the band
    BETWEEN those two ends, which is the band the constant calls normal
    ("JIT queue is 0-5 items; 10+ indicates a problem").
    """

    def _service_with_pending(self, count, age_hours=0):
        """A health service whose oldest pending item is production-shaped."""
        service = HealthCheckService()
        service.queue_repo = Mock()
        service.history_repo = Mock()
        service.queue_repo.count_pending.return_value = count

        if count:
            item = PostingQueue()
            # No explicit created_at: the column default is what production
            # stores, and its naive-ness is the whole defect.
            item.created_at = datetime.utcnow() - timedelta(hours=age_hours)
            assert item.created_at.tzinfo is None, (
                "fixture must stay naive — an aware value here is what hid #899"
            )
            service.queue_repo.get_oldest_pending.return_value = item
        else:
            service.queue_repo.get_oldest_pending.return_value = None
        return service

    def test_the_model_default_is_naive_which_is_the_premise_of_this_bug(self):
        """If this ever fails the column gained tzinfo and the rest is moot."""
        assert PostingQueue.__table__.c.created_at.default is not None
        produced = PostingQueue.__table__.c.created_at.default.arg(None)
        assert produced.tzinfo is None

    @pytest.mark.parametrize("pending", [1, 2, 5, 9, 10])
    def test_the_normal_band_does_not_raise(self, pending):
        """1..threshold is where #899 lived — every one of these raised."""
        service = self._service_with_pending(pending)
        result = service._check_queue()

        assert "error" not in result["message"].lower(), result["message"]
        assert result["healthy"] is True
        assert result["pending_count"] == pending

    def test_zero_pending_still_works(self):
        """The lower boundary always worked: no item, so no subtraction."""
        result = self._service_with_pending(0)._check_queue()
        assert result["healthy"] is True
        assert result["pending_count"] == 0

    @pytest.mark.parametrize("pending", [11, 62])
    def test_above_threshold_still_returns_the_backlog_verdict(self, pending):
        """The upper boundary always worked: it returns before the age check."""
        result = self._service_with_pending(pending)._check_queue()
        assert result["healthy"] is False
        assert "backlog" in result["message"].lower()

    def test_a_genuinely_stale_naive_item_is_still_detected(self):
        """The fix must not cost the behaviour the branch exists for."""
        service = self._service_with_pending(3, age_hours=8)
        result = service._check_queue()

        assert result["healthy"] is False
        assert "hours" in result["message"].lower()


@pytest.mark.unit
class TestMediaSyncStalenessAcceptsANaiveTimestamp:
    """#899 sibling — same mismatch, reached through an isoformat round-trip.

    `get_last_sync_info` serialises `ServiceRun.completed_at` (a naive column)
    with `.isoformat()`; `datetime.fromisoformat` parses it back naive, and the
    staleness comparison subtracted it from an aware now. Confirmed live before
    the fix by driving the real method:
    "Sync check error: can't subtract offset-naive and offset-aware datetimes".

    These drive `_check_media_sync` itself rather than the helper, so they fail
    when the fix is reverted. A test that called `ensure_utc` directly would
    pass either way and would be coverage in name only.
    """

    def _drive(self, completed_naive):
        """Run the real _check_media_sync with a naive completed_at."""
        service = HealthCheckService()
        service.queue_repo = Mock()
        service.history_repo = Mock()

        admin_chat = Mock(
            media_sync_enabled=True,
            media_source_type="local",
            media_source_root="/tmp",
        )
        settings_ctx = MagicMock()
        settings_ctx.__enter__.return_value.get_settings.return_value = admin_chat

        sync_info = {
            "success": True,
            "status": "completed",
            "started_at": completed_naive.isoformat(),
            "completed_at": completed_naive.isoformat(),
        }
        assert datetime.fromisoformat(sync_info["completed_at"]).tzinfo is None

        provider = Mock()
        provider.is_configured.return_value = True
        with (
            patch(
                "src.services.core.health_check.SettingsService",
                return_value=settings_ctx,
            ),
            patch(
                "src.services.media_sources.factory.MediaSourceFactory.create",
                return_value=provider,
            ),
            patch(
                "src.services.core.media_sync.MediaSyncService.get_last_sync_info",
                return_value=sync_info,
            ),
        ):
            return service._check_media_sync()

    def test_a_fresh_naive_completed_at_does_not_raise(self):
        result = self._drive(datetime.utcnow())
        assert "error" not in result["message"].lower(), result["message"]
        assert result["healthy"] is True

    def test_a_stale_naive_completed_at_is_flagged_stale_not_errored(self):
        result = self._drive(datetime.utcnow() - timedelta(days=2))
        assert "error" not in result["message"].lower(), result["message"]
        assert result["healthy"] is False
        assert "stale" in result["message"].lower()
