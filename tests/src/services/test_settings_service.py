"""Comprehensive tests for SettingsService.

Test Categories:
1. Unit tests (mocked, no DB required)
2. Integration tests (require test_db fixture)
3. Architecture validation tests
4. .env fallback behavior tests
5. Multi-chat isolation tests
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime
from uuid import uuid4

from src.services.core.settings_service import (
    SettingsService,
    TOGGLEABLE_SETTINGS,
    NUMERIC_SETTINGS,
    TEXT_SETTINGS,
)
from src.exceptions.tenancy import TenantResolutionError
from src.repositories.chat_settings_repository import ChatSettingsRepository
from src.models.chat_settings import ChatSettings


# =============================================================================
# UNIT TESTS (Mocked - No Database Required)
# =============================================================================


@pytest.mark.unit
class TestSettingsServiceUnit:
    """Unit tests with mocked dependencies - run without database."""

    @pytest.fixture(autouse=True)
    def _mock_audit(self):
        with patch("src.services.core.settings_service.AuditRepository"):
            yield

    def test_get_settings_by_id_delegates_to_repo(self):
        """get_settings_by_id resolves a tenant by chat_settings UUID."""
        service = SettingsService()
        service.settings_repo = Mock(spec=ChatSettingsRepository)
        cs_id = str(uuid4())
        expected = Mock(spec=ChatSettings)
        service.settings_repo.get_by_id.return_value = expected

        result = service.get_settings_by_id(cs_id, chat_settings_id=cs_id)

        assert result is expected
        service.settings_repo.get_by_id.assert_called_once_with(
            cs_id, chat_settings_id=cs_id
        )

    def test_toggleable_settings_are_defined(self):
        """Verify TOGGLEABLE_SETTINGS contains expected settings."""
        assert "dry_run_mode" in TOGGLEABLE_SETTINGS
        assert "enable_instagram_api" in TOGGLEABLE_SETTINGS
        assert "is_paused" in TOGGLEABLE_SETTINGS
        # posts_per_day should NOT be toggleable
        assert "posts_per_day" not in TOGGLEABLE_SETTINGS

    def test_numeric_settings_are_defined(self):
        """Verify NUMERIC_SETTINGS contains expected settings."""
        assert "posts_per_day" in NUMERIC_SETTINGS
        assert "posting_hours_start" in NUMERIC_SETTINGS
        assert "posting_hours_end" in NUMERIC_SETTINGS
        # dry_run_mode should NOT be numeric
        assert "dry_run_mode" not in NUMERIC_SETTINGS

    def test_toggle_invalid_setting_raises_error_without_db(self):
        """Toggling non-toggleable setting should raise ValueError immediately."""
        service = SettingsService()
        # Mock the repo to avoid DB calls
        service.settings_repo = Mock()

        with pytest.raises(ValueError, match="not toggleable"):
            service.toggle_setting(-100, "posts_per_day", None)

        # Repo should NOT have been called
        service.settings_repo.get_or_create.assert_not_called()

    def test_update_unknown_setting_raises_error_without_db(self):
        """Updating unknown setting should raise ValueError immediately."""
        service = SettingsService()
        service.settings_repo = Mock()

        with pytest.raises(ValueError, match="Unknown setting"):
            service.update_setting(-100, "fake_setting", "value", None)

        service.settings_repo.get_or_create.assert_not_called()

    def test_get_settings_calls_repository(self):
        """get_settings delegates to the non-minting read (#842)."""
        service = SettingsService()
        mock_repo = Mock()
        mock_settings = Mock(spec=ChatSettings)
        mock_repo.get_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        result = service.get_settings(-1001234567890)

        mock_repo.get_by_chat_id.assert_called_once_with(-1001234567890)
        mock_repo.get_or_create.assert_not_called()
        assert result == mock_settings

    def test_toggle_setting_flips_value(self):
        """Toggle should flip boolean value."""
        service = SettingsService()

        # Mock repository
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.dry_run_mode = True  # Initial value

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        mock_repo.update.return_value = mock_settings
        service.settings_repo = mock_repo

        # Mock service_run_repo for track_execution
        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        # Toggle
        service.toggle_setting(-100, "dry_run_mode", None)

        # Should have called update with opposite value
        mock_repo.update.assert_called_once()
        call_kwargs = mock_repo.update.call_args[1]
        assert call_kwargs["dry_run_mode"] is False  # Flipped from True

    def test_toggle_is_paused_uses_set_paused(self):
        """Toggling is_paused should call set_paused for tracking."""
        service = SettingsService()

        mock_settings = Mock(spec=ChatSettings)
        mock_settings.is_paused = False

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        mock_user = Mock()
        mock_user.id = uuid4()
        mock_user.telegram_username = "testuser"

        service.toggle_setting(-100, "is_paused", mock_user)

        # Should have called set_paused, not update
        mock_repo.set_paused.assert_called_once()
        mock_repo.update.assert_not_called()

    def test_set_paused_writes_when_state_differs(self):
        """set_paused writes through the repo when the flag actually changes."""
        service = SettingsService()

        mock_settings = Mock(spec=ChatSettings)
        mock_settings.is_paused = True

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        mock_user = Mock()
        mock_user.id = uuid4()
        mock_user.telegram_username = "testuser"

        service.set_paused(-100, False, mock_user)

        mock_repo.set_paused.assert_called_once_with(-100, False, str(mock_user.id))

    def test_set_paused_noops_when_already_at_target(self):
        """Idempotent: no write (and no audit churn) when already in the
        requested state — concurrent resume taps converge instead of
        flipping the flag back."""
        service = SettingsService()

        mock_settings = Mock(spec=ChatSettings)
        mock_settings.is_paused = False

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        service.set_paused(-100, False, Mock(id=uuid4()))

        mock_repo.set_paused.assert_not_called()

    def test_update_posts_per_day_validates_min(self):
        """posts_per_day below 1 should raise ValueError."""
        service = SettingsService()
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.posts_per_day = 5

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        with pytest.raises(ValueError, match="must be between 1 and 50"):
            service.update_setting(-100, "posts_per_day", 0, None)

    def test_update_posts_per_day_validates_max(self):
        """posts_per_day above 50 should raise ValueError."""
        service = SettingsService()
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.posts_per_day = 5

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        with pytest.raises(ValueError, match="must be between 1 and 50"):
            service.update_setting(-100, "posts_per_day", 51, None)

    def test_update_posting_hours_validates_range(self):
        """Hour values must be 0-23."""
        service = SettingsService()
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.posting_hours_start = 14

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        with pytest.raises(ValueError, match="Hour must be between 0 and 23"):
            service.update_setting(-100, "posting_hours_start", 24, None)

        with pytest.raises(ValueError, match="Hour must be between 0 and 23"):
            service.update_setting(-100, "posting_hours_end", -1, None)

    def test_get_settings_display_returns_all_keys(self):
        """get_settings_display should return dict with all expected keys."""
        service = SettingsService()

        mock_settings = Mock(spec=ChatSettings)
        mock_settings.dry_run_mode = True
        mock_settings.enable_instagram_api = False
        mock_settings.is_paused = False
        mock_settings.paused_at = None
        mock_settings.paused_by_user_id = None
        mock_settings.posts_per_day = 10
        mock_settings.posting_hours_start = 14
        mock_settings.posting_hours_end = 2
        mock_settings.show_verbose_notifications = True
        mock_settings.media_sync_enabled = False
        mock_settings.media_source_type = None
        mock_settings.media_source_root = None
        mock_settings.updated_at = datetime.utcnow()

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        display = service.get_settings_display(-100)

        expected_keys = [
            "dry_run_mode",
            "enable_instagram_api",
            "is_paused",
            "paused_at",
            "paused_by_user_id",
            "posts_per_day",
            "posting_hours_start",
            "posting_hours_end",
            "show_verbose_notifications",
            "media_sync_enabled",
            "media_source_type",
            "media_source_root",
            "updated_at",
        ]
        for key in expected_keys:
            assert key in display, f"Missing key: {key}"

    def test_media_sync_enabled_is_toggleable(self):
        """media_sync_enabled should be in TOGGLEABLE_SETTINGS."""
        assert "media_sync_enabled" in TOGGLEABLE_SETTINGS

    def test_get_settings_display_includes_media_sync(self):
        """get_settings_display should include media_sync_enabled value."""
        service = SettingsService()

        mock_settings = Mock(spec=ChatSettings)
        mock_settings.dry_run_mode = False
        mock_settings.enable_instagram_api = False
        mock_settings.is_paused = False
        mock_settings.paused_at = None
        mock_settings.paused_by_user_id = None
        mock_settings.posts_per_day = 3
        mock_settings.posting_hours_start = 14
        mock_settings.posting_hours_end = 2
        mock_settings.show_verbose_notifications = True
        mock_settings.media_sync_enabled = True
        mock_settings.media_source_type = None
        mock_settings.media_source_root = None
        mock_settings.updated_at = datetime.utcnow()

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo

        display = service.get_settings_display(-100)

        assert display["media_sync_enabled"] is True

    def test_get_all_active_chats_delegates_to_repository(self):
        """get_all_active_chats delegates to settings_repo.get_all_active."""
        service = SettingsService()
        mock_repo = Mock()
        mock_chat1 = Mock(spec=ChatSettings)
        mock_chat2 = Mock(spec=ChatSettings)
        mock_repo.get_all_active.return_value = [mock_chat1, mock_chat2]
        service.settings_repo = mock_repo

        result = service.get_all_active_chats()

        assert len(result) == 2
        mock_repo.get_all_active.assert_called_once()

    def test_get_all_active_chats_returns_empty_list(self):
        """get_all_active_chats returns empty list when no active chats."""
        service = SettingsService()
        mock_repo = Mock()
        mock_repo.get_all_active.return_value = []
        service.settings_repo = mock_repo

        result = service.get_all_active_chats()

        assert result == []

    def test_get_all_sync_enabled_chats_delegates_to_repository(self):
        """get_all_sync_enabled_chats delegates to settings_repo.get_all_sync_enabled."""
        service = SettingsService()
        mock_repo = Mock()
        mock_chat = Mock(spec=ChatSettings)
        mock_repo.get_all_sync_enabled.return_value = [mock_chat]
        service.settings_repo = mock_repo

        result = service.get_all_sync_enabled_chats()

        assert len(result) == 1
        mock_repo.get_all_sync_enabled.assert_called_once()

    def test_get_all_sync_enabled_chats_returns_empty_list(self):
        """get_all_sync_enabled_chats returns empty list when none enabled."""
        service = SettingsService()
        mock_repo = Mock()
        mock_repo.get_all_sync_enabled.return_value = []
        service.settings_repo = mock_repo

        result = service.get_all_sync_enabled_chats()

        assert result == []


# =============================================================================
# ARCHITECTURE VALIDATION TESTS
# =============================================================================


@pytest.mark.unit
class TestSettingsArchitecture:
    """Tests validating architectural decisions."""

    def test_chat_settings_model_has_chat_id(self):
        """ChatSettings model should have telegram_chat_id for multi-tenancy."""
        from src.models.chat_settings import ChatSettings

        assert hasattr(ChatSettings, "telegram_chat_id")

    def test_settings_service_accepts_chat_id(self):
        """All SettingsService methods should accept chat_id parameter."""
        import inspect

        service = SettingsService()

        # get_settings
        sig = inspect.signature(service.get_settings)
        assert "telegram_chat_id" in sig.parameters

        # toggle_setting
        sig = inspect.signature(service.toggle_setting)
        assert "telegram_chat_id" in sig.parameters

        # update_setting
        sig = inspect.signature(service.update_setting)
        assert "telegram_chat_id" in sig.parameters

        # get_settings_display
        sig = inspect.signature(service.get_settings_display)
        assert "telegram_chat_id" in sig.parameters

    def test_settings_service_does_not_hardcode_chat_id(self):
        """SettingsService should not import or use ADMIN_TELEGRAM_CHAT_ID."""
        from pathlib import Path

        service_path = Path("src/services/core/settings_service.py")
        content = service_path.read_text()

        # Should not reference ADMIN_TELEGRAM_CHAT_ID
        assert "ADMIN_TELEGRAM_CHAT_ID" not in content, (
            "SettingsService should not hardcode ADMIN_TELEGRAM_CHAT_ID"
        )

    def test_repository_uses_chat_id_for_lookup(self):
        """Repository should use chat_id for unique lookups."""
        from src.repositories.chat_settings_repository import ChatSettingsRepository
        import inspect

        repo = ChatSettingsRepository()

        # get_by_chat_id
        sig = inspect.signature(repo.get_by_chat_id)
        assert "telegram_chat_id" in sig.parameters

        # get_or_create
        sig = inspect.signature(repo.get_or_create)
        assert "telegram_chat_id" in sig.parameters


# =============================================================================
# .ENV FALLBACK BEHAVIOR TESTS
# =============================================================================


@pytest.mark.unit
class TestEnvFallback:
    """Tests for code-default bootstrap when no DB record exists."""

    def test_repository_bootstraps_from_code_defaults(self):
        """Repository should create settings from src.config.defaults on first access."""
        from src.config import defaults

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None  # No existing record
        mock_db.query.return_value = mock_query

        repo = ChatSettingsRepository()
        repo._db = mock_db

        created_settings = None

        def capture_add(obj):
            nonlocal created_settings
            created_settings = obj

        mock_db.add.side_effect = capture_add
        mock_db.refresh = MagicMock()

        repo.get_or_create(-1001234567890)

        mock_db.add.assert_called_once()
        assert created_settings.dry_run_mode is defaults.DEFAULT_DRY_RUN_MODE
        assert created_settings.enable_instagram_api is (
            defaults.DEFAULT_ENABLE_INSTAGRAM_API
        )
        assert created_settings.posts_per_day == defaults.DEFAULT_POSTS_PER_DAY


# =============================================================================
# MULTI-CHAT ISOLATION TESTS
# =============================================================================


@pytest.mark.unit
class TestMultiChatIsolation:
    """Tests ensuring different chats have isolated settings."""

    @pytest.fixture(autouse=True)
    def _mock_audit(self):
        with patch("src.services.core.settings_service.AuditRepository"):
            yield

    def test_different_chat_ids_get_different_settings(self):
        """Two chat IDs should get independent settings."""
        service = SettingsService()

        # Track which chat_id is requested
        requested_ids = []

        def mock_get_by_chat_id(chat_id):
            requested_ids.append(chat_id)
            settings = Mock(spec=ChatSettings)
            settings.telegram_chat_id = chat_id
            settings.dry_run_mode = True
            return settings

        mock_repo = Mock()
        mock_repo.get_by_chat_id.side_effect = mock_get_by_chat_id
        service.settings_repo = mock_repo

        # Get settings for two different chats
        settings1 = service.get_settings(-1001111111111)
        settings2 = service.get_settings(-1002222222222)

        # Both should have been called with their own ID
        assert -1001111111111 in requested_ids
        assert -1002222222222 in requested_ids
        assert settings1.telegram_chat_id != settings2.telegram_chat_id

    def test_toggle_affects_only_specified_chat(self):
        """Toggling setting for one chat should not affect others."""
        service = SettingsService()

        # Track which chat_id gets updated
        updated_chat_ids = []

        def mock_update(chat_id, **kwargs):
            updated_chat_ids.append(chat_id)
            return Mock(spec=ChatSettings)

        mock_settings = Mock(spec=ChatSettings)
        mock_settings.dry_run_mode = True

        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        mock_repo.update.side_effect = mock_update
        service.settings_repo = mock_repo

        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        # Toggle for specific chat
        service.toggle_setting(-1001111111111, "dry_run_mode", None)

        # Only that chat should have been updated
        assert -1001111111111 in updated_chat_ids
        assert len(updated_chat_ids) == 1


# =============================================================================
# INTEGRATION TESTS (Require Database)
# =============================================================================


@pytest.mark.integration
class TestSettingsServiceIntegration:
    """Integration tests requiring database connection."""

    def test_provision_creates_from_env_and_reads_never_do(self, test_db):
        """Minting is only ever provision() (#842): a read of an unknown chat
        returns None, and provisioning bootstraps from .env values."""
        service = SettingsService()
        service.settings_repo = ChatSettingsRepository()
        service.settings_repo._db = test_db

        chat_id = -1001234567890

        assert service.get_settings(chat_id) is None

        settings = service.provision(chat_id)

        assert settings is not None
        assert settings.telegram_chat_id == chat_id
        assert isinstance(settings.dry_run_mode, bool)
        assert isinstance(settings.posts_per_day, int)

    def test_get_settings_returns_existing(self, test_db):
        """Access after provisioning returns the existing record."""
        service = SettingsService()
        service.settings_repo = ChatSettingsRepository()
        service.settings_repo._db = test_db

        chat_id = -1001234567891

        provisioned = service.provision(chat_id)
        settings2 = service.get_settings(chat_id)

        assert provisioned.id == settings2.id

    def test_toggle_setting_persists(self, test_db):
        """Toggled value should be persisted to database."""
        service = SettingsService()
        service.settings_repo = ChatSettingsRepository()
        service.settings_repo._db = test_db
        service.service_run_repo._db = test_db

        chat_id = -1001234567892

        settings = service.provision(chat_id)
        initial_value = settings.dry_run_mode

        service.toggle_setting(chat_id, "dry_run_mode", None)

        # Get fresh from DB
        fresh_settings = service.get_settings(chat_id)
        assert fresh_settings.dry_run_mode != initial_value

    def test_pause_tracks_user_and_timestamp(self, test_db):
        """Pausing should record who paused and when."""
        service = SettingsService()
        service.settings_repo = ChatSettingsRepository()
        service.settings_repo._db = test_db
        service.service_run_repo._db = test_db

        chat_id = -1001234567896

        from src.repositories.user_repository import UserRepository

        user_repo = UserRepository()
        user_repo._db = test_db

        user = user_repo.create(
            telegram_user_id=123456789,
            telegram_username="pauseuser",
            telegram_first_name="Pause",
        )

        # Ensure not paused initially
        settings = service.provision(chat_id)
        if settings.is_paused:
            service.toggle_setting(chat_id, "is_paused", user)

        before_pause = datetime.utcnow()
        service.toggle_setting(chat_id, "is_paused", user)

        updated_settings = service.get_settings(chat_id)
        assert updated_settings.is_paused is True
        assert updated_settings.paused_at is not None
        assert updated_settings.paused_at >= before_pause

    def test_unpause_clears_tracking(self, test_db):
        """Unpausing should clear paused_at and paused_by_user_id."""
        service = SettingsService()
        service.settings_repo = ChatSettingsRepository()
        service.settings_repo._db = test_db
        service.service_run_repo._db = test_db

        chat_id = -1001234567897

        from src.repositories.user_repository import UserRepository

        user_repo = UserRepository()
        user_repo._db = test_db

        user = user_repo.create(
            telegram_user_id=987654321,
            telegram_username="unpauseuser",
            telegram_first_name="Unpause",
        )

        # Ensure paused
        settings = service.provision(chat_id)
        if not settings.is_paused:
            service.toggle_setting(chat_id, "is_paused", user)

        service.toggle_setting(chat_id, "is_paused", user)

        updated_settings = service.get_settings(chat_id)
        assert updated_settings.is_paused is False
        assert updated_settings.paused_at is None
        assert updated_settings.paused_by_user_id is None

    def test_different_chats_have_isolated_settings(self, test_db):
        """Two chats should have independent settings."""
        service = SettingsService()
        service.settings_repo = ChatSettingsRepository()
        service.settings_repo._db = test_db
        service.service_run_repo._db = test_db

        chat_id_1 = -1001111111111
        chat_id_2 = -1002222222222

        # Provision both (reads never mint, #842)
        settings1 = service.provision(chat_id_1)
        settings2 = service.provision(chat_id_2)

        # They should be different records
        assert settings1.id != settings2.id

        # Toggle dry_run for chat 1 only
        initial_chat2_value = settings2.dry_run_mode
        service.toggle_setting(chat_id_1, "dry_run_mode", None)

        # Chat 2 should be unchanged
        fresh_settings2 = service.get_settings(chat_id_2)
        assert fresh_settings2.dry_run_mode == initial_chat2_value


# =============================================================================
# TELEGRAM SERVICE INTEGRATION TESTS
# =============================================================================


@pytest.mark.unit
class TestTelegramSettingsIntegration:
    """Tests for TelegramService settings integration."""

    def test_telegram_service_uses_settings_service(self):
        """TelegramService should have settings_service attribute."""
        from src.services.core.telegram_service import TelegramService

        # Check the class has the initialization
        import inspect

        source = inspect.getsource(TelegramService.__init__)
        assert "settings_service" in source or "SettingsService" in source


# =============================================================================
# POSTING/SCHEDULER SERVICE ARCHITECTURE TESTS
# =============================================================================


@pytest.mark.unit
class TestServiceSettingsUsage:
    """Tests verifying PostingService and SchedulerService use SettingsService."""

    def test_posting_service_has_settings_service(self):
        """PostingService should have settings_service attribute."""
        from src.services.core.posting import PostingService
        import inspect

        source = inspect.getsource(PostingService.__init__)
        assert "settings_service" in source or "SettingsService" in source

    def test_scheduler_service_has_settings_service(self):
        """SchedulerService should have settings_service attribute."""
        from src.services.core.scheduler import SchedulerService
        import inspect

        source = inspect.getsource(SchedulerService.__init__)
        assert "settings_service" in source or "SettingsService" in source


# =============================================================================
# DEPLOYMENT MODEL DOCUMENTATION TEST
# =============================================================================


@pytest.mark.unit
class TestDeploymentModel:
    """Tests documenting and validating the deployment model."""

    def test_current_deployment_is_single_tenant(self):
        """
        DOCUMENTATION TEST: Current deployment model is single-tenant.

        Each deployment of storydump represents:
        - ONE Telegram bot (TELEGRAM_BOT_TOKEN)
        - ONE admin channel (TELEGRAM_CHANNEL_ID / ADMIN_TELEGRAM_CHAT_ID)
        - ONE Instagram account (INSTAGRAM_ACCOUNT_ID)

        This is the CURRENT (env-keyed) shape, not the target one. Storydump
        is a hosted product we operate (design plan FC-9); the target model is
        many tenants as rows on this one deployment, not one deployment per
        group. Onboarding a further group is therefore a provisioning action
        here, never a fork-and-deploy-your-own.

        Until that lands, a single deployment still binds one bot to one admin
        channel, and nothing should try to share a single bot across groups.
        """
        # This test documents the architecture decision
        # Verify the .env template mentions single-tenant
        from pathlib import Path

        env_example = Path(".env.example")
        if env_example.exists():
            content = env_example.read_text()
            # Just verify required single-tenant configs exist
            assert "TELEGRAM_BOT_TOKEN" in content
            assert "TELEGRAM_CHANNEL_ID" in content

    def test_chat_settings_supports_future_multi_tenancy(self):
        """
        DOCUMENTATION TEST: Database schema supports future multi-tenancy.

        The chat_settings table is designed for Phase 3 multi-tenancy:
        - telegram_chat_id is the unique identifier
        - Each chat can have different posts_per_day, posting_hours, etc.

        Current limitation (Phase 1):
        - PostingService and SchedulerService use hardcoded ADMIN_TELEGRAM_CHAT_ID

        Future multi-tenancy (Phase 3) requires:
        - Pass telegram_chat_id through the call stack
        - Add telegram_chat_id to api_tokens for per-chat Instagram accounts
        - Add telegram_chat_id to posting_queue for routing
        """
        from src.models.chat_settings import ChatSettings

        # Verify the model has the multi-tenancy column
        assert hasattr(ChatSettings, "telegram_chat_id")

        # Verify it's indexed (for performance)
        from sqlalchemy import inspect as sa_inspect

        mapper = sa_inspect(ChatSettings)

        # Find the telegram_chat_id column
        for col in mapper.columns:
            if col.name == "telegram_chat_id":
                assert col.unique, "telegram_chat_id should be unique"
                break


# =============================================================================
# ONBOARDING METHODS TESTS
# =============================================================================


@pytest.mark.unit
class TestSettingsServiceOnboarding:
    """Test onboarding convenience methods."""

    @pytest.fixture
    def settings_service(self):
        """Create SettingsService with mocked repository."""
        with patch.object(SettingsService, "__init__", lambda self: None):
            service = SettingsService()
            service.settings_repo = Mock()
            service.audit_repo = Mock()
            return service

    def test_set_onboarding_step(self, settings_service):
        """set_onboarding_step updates the column via repo."""
        settings_service.settings_repo.update.return_value = Mock(
            onboarding_step="instagram"
        )

        result = settings_service.set_onboarding_step(123456, "instagram")

        settings_service.settings_repo.update.assert_called_once_with(
            123456, onboarding_step="instagram"
        )
        assert result.onboarding_step == "instagram"

    def test_set_onboarding_step_none_clears(self, settings_service):
        """set_onboarding_step(None) clears the step."""
        settings_service.settings_repo.update.return_value = Mock(onboarding_step=None)

        settings_service.set_onboarding_step(123456, None)

        settings_service.settings_repo.update.assert_called_once_with(
            123456, onboarding_step=None
        )

    def test_complete_onboarding(self, settings_service):
        """complete_onboarding sets completed=True and clears step."""
        settings_service.settings_repo.update.return_value = Mock(
            onboarding_step=None, onboarding_completed=True
        )

        result = settings_service.complete_onboarding(123456)

        settings_service.settings_repo.update.assert_called_once_with(
            123456, onboarding_step=None, onboarding_completed=True
        )
        assert result.onboarding_completed is True
        assert result.onboarding_step is None


# =============================================================================
# MEDIA SOURCE CONFIGURATION TESTS
# =============================================================================


@pytest.mark.unit
class TestSettingsServiceMediaSource:
    """Tests for per-chat media source configuration."""

    @pytest.fixture(autouse=True)
    def _mock_audit(self):
        with patch("src.services.core.settings_service.AuditRepository"):
            yield

    @pytest.fixture
    def settings_service(self):
        """Create SettingsService with mocked repository."""
        with patch.object(SettingsService, "__init__", lambda self: None):
            service = SettingsService()
            service.settings_repo = Mock()
            service.audit_repo = Mock()
            return service

    def test_text_settings_defined(self):
        """TEXT_SETTINGS contains media source settings."""
        assert "media_source_type" in TEXT_SETTINGS
        assert "media_source_root" in TEXT_SETTINGS

    def test_update_media_source_type_valid(self):
        """Can update media_source_type to a valid value."""
        service = SettingsService()
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.media_source_type = None
        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        mock_repo.update.return_value = mock_settings
        service.settings_repo = mock_repo
        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        service.update_setting(-100, "media_source_type", "google_drive")

        mock_repo.update.assert_called_once()
        call_kwargs = mock_repo.update.call_args[1]
        assert call_kwargs["media_source_type"] == "google_drive"

    def test_update_media_source_type_invalid_raises(self):
        """Invalid media_source_type value raises ValueError."""
        service = SettingsService()
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.media_source_type = None
        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        service.settings_repo = mock_repo
        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        with pytest.raises(ValueError, match="media_source_type must be"):
            service.update_setting(-100, "media_source_type", "dropbox")

    def test_update_media_source_type_none_allowed(self):
        """Setting media_source_type to None is valid (clears override)."""
        service = SettingsService()
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.media_source_type = "google_drive"
        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        mock_repo.update.return_value = mock_settings
        service.settings_repo = mock_repo
        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        service.update_setting(-100, "media_source_type", None)

        mock_repo.update.assert_called_once()

    def test_update_media_source_root_no_validation(self):
        """media_source_root accepts any string (folder IDs and paths are free-form)."""
        service = SettingsService()
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.media_source_root = None
        mock_repo = Mock()
        mock_repo.require_by_chat_id.return_value = mock_settings
        mock_repo.update.return_value = mock_settings
        service.settings_repo = mock_repo
        service.service_run_repo = Mock()
        service.service_run_repo.create_run.return_value = str(uuid4())

        service.update_setting(-100, "media_source_root", "1C9jxiJCU8Sf4Q7M")

        mock_repo.update.assert_called_once()
        call_kwargs = mock_repo.update.call_args[1]
        assert call_kwargs["media_source_root"] == "1C9jxiJCU8Sf4Q7M"

    @patch("src.config.settings.settings")
    def test_get_media_source_config_uses_per_chat_values(
        self, mock_env, settings_service
    ):
        """Per-chat values take priority over env vars."""
        mock_env.MEDIA_SOURCE_TYPE = "local"
        mock_env.MEDIA_SOURCE_ROOT = "/default/path"

        mock_settings = Mock(spec=ChatSettings)
        mock_settings.media_source_type = "google_drive"
        mock_settings.media_source_root = "folder_abc"
        settings_service.settings_repo.get_by_id.return_value = mock_settings

        source_type, source_root = settings_service.get_media_source_config("cs-100")

        assert source_type == "google_drive"
        assert source_root == "folder_abc"

    def test_get_media_source_config_falls_back_to_code_default(self, settings_service):
        """NULL per-chat source_type falls back to defaults.DEFAULT_MEDIA_SOURCE_TYPE;
        NULL per-chat source_root surfaces as None (no env fallback).
        """
        from src.config import defaults

        mock_settings = Mock(spec=ChatSettings)
        mock_settings.media_source_type = None
        mock_settings.media_source_root = None
        settings_service.settings_repo.get_by_id.return_value = mock_settings

        source_type, source_root = settings_service.get_media_source_config("cs-100")

        assert source_type == defaults.DEFAULT_MEDIA_SOURCE_TYPE
        assert source_root is None

    def test_get_media_source_config_partial_override(self, settings_service):
        """Per-chat type set, root NULL — type used, root returns None."""
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.media_source_type = "google_drive"
        mock_settings.media_source_root = None
        settings_service.settings_repo.get_by_id.return_value = mock_settings

        source_type, source_root = settings_service.get_media_source_config("cs-100")

        assert source_type == "google_drive"
        assert source_root is None

    def test_get_settings_display_includes_media_source_fields(self, settings_service):
        """get_settings_display includes media_source_type and media_source_root."""
        mock_settings = Mock(spec=ChatSettings)
        mock_settings.dry_run_mode = False
        mock_settings.enable_instagram_api = False
        mock_settings.is_paused = False
        mock_settings.paused_at = None
        mock_settings.paused_by_user_id = None
        mock_settings.posts_per_day = 3
        mock_settings.posting_hours_start = 14
        mock_settings.posting_hours_end = 2
        mock_settings.show_verbose_notifications = True
        mock_settings.media_sync_enabled = False
        mock_settings.media_source_type = "google_drive"
        mock_settings.media_source_root = "folder_123"
        mock_settings.updated_at = datetime.utcnow()

        settings_service.settings_repo.require_by_chat_id.return_value = mock_settings

        display = settings_service.get_settings_display(-100)

        assert display["media_source_type"] == "google_drive"
        assert display["media_source_root"] == "folder_123"


@pytest.mark.unit
class TestResolveChatSettingsIdDoor:
    """The legacy resolution door (`04` F.3, #842).

    The policy under test: a chat that cannot be resolved to a tenant is a
    typed refusal at this one door — never a default, never None, never a
    mint."""

    @pytest.fixture(autouse=True)
    def _mock_audit(self):
        with patch("src.services.core.settings_service.AuditRepository"):
            yield

    def _service_with_repo(self):
        service = SettingsService()
        service.settings_repo = Mock(spec=ChatSettingsRepository)
        return service

    def test_resolves_to_the_tenant_key(self):
        service = self._service_with_repo()
        row = Mock(spec=ChatSettings)
        row.id = uuid4()
        service.settings_repo.require_by_chat_id.return_value = row

        assert service.resolve_chat_settings_id(123) == str(row.id)
        service.settings_repo.require_by_chat_id.assert_called_once_with(123)

    def test_unknown_chat_is_a_typed_refusal_and_never_mints(self):
        """A refusal is typed AND creates nothing on its way out — minting
        stays at the explicit provisioning doors. The raise itself lives in
        the repo primitive; its semantics are pinned below unbound, so this
        test only wires the delegation."""
        service = self._service_with_repo()
        service.settings_repo.require_by_chat_id.side_effect = TenantResolutionError(
            "unknown_binding"
        )

        with pytest.raises(TenantResolutionError) as exc:
            service.resolve_chat_settings_id(123)
        assert exc.value.reason == "unknown_binding"
        service.settings_repo.get_or_create.assert_not_called()

    def test_the_repo_primitive_refuses_typed_and_never_mints(self):
        """The ONE raise site (#842): row-None -> unknown_binding, no mint.

        Called unbound with a mocked self so no database is anywhere near
        the policy — the same trick integration_verdict uses in conftest."""
        from src.repositories.chat_settings_repository import (
            ChatSettingsRepository,
        )

        repo_self = Mock()
        repo_self.get_by_chat_id.return_value = None

        with pytest.raises(TenantResolutionError) as exc:
            ChatSettingsRepository.require_by_chat_id(repo_self, 123)
        assert exc.value.reason == "unknown_binding"
        repo_self.get_or_create.assert_not_called()

        row = Mock(spec=ChatSettings)
        repo_self.get_by_chat_id.return_value = row
        assert ChatSettingsRepository.require_by_chat_id(repo_self, 123) is row

    def test_the_refusal_is_the_shared_contract_type(self):
        """The exception is the target resolver's own type, from the neutral
        home — what makes the M.3 internals swap invisible to every edge."""
        from src.exceptions import TenantResolutionError as from_package
        from src.exceptions.base import StorydumpError
        from src.services.target import tenant_resolution

        assert from_package is TenantResolutionError
        assert tenant_resolution.TenantResolutionError is TenantResolutionError
        assert issubclass(TenantResolutionError, StorydumpError)
