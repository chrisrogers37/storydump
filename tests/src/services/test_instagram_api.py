"""Tests for InstagramAPIService."""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timezone

import httpx

from src.exceptions import (
    InstagramAPIError,
    MediaUnsupportedError,
    RateLimitError,
    TokenCorruptError,
    TokenExpiredError,
    TokenRevokedError,
)
from tests.src.services.conftest import mock_track_execution


@pytest.mark.unit
class TestInstagramAPIService:
    """Test suite for InstagramAPIService."""

    @pytest.fixture
    def instagram_service(self):
        """Create InstagramAPIService with mocked dependencies."""
        with (
            patch("src.services.integrations.instagram_api.TokenRefreshService"),
            patch("src.services.integrations.instagram_api.CloudStorageService"),
            patch("src.services.integrations.instagram_api.HistoryRepository"),
            patch("src.services.integrations.instagram_api.InstagramAccountService"),
            patch("src.services.integrations.instagram_api.TokenRepository"),
            patch("src.services.integrations.instagram_api.TokenEncryption"),
            patch("src.services.integrations.instagram_api.SettingsService"),
            patch("src.services.base_service.ServiceRunRepository"),
        ):
            from src.services.integrations.instagram_api import (
                InstagramAPIService,
            )

            service = InstagramAPIService()
            service.token_service = Mock()
            service.cloud_service = Mock()
            service.history_repo = Mock()
            service.track_execution = mock_track_execution
            service.set_result_summary = Mock()
            yield service

    # ==================== get_rate_limit_remaining Tests ====================

    @patch("src.services.integrations.instagram_api.settings")
    def test_get_rate_limit_remaining_no_recent_posts(
        self, mock_settings, instagram_service
    ):
        """Test rate limit shows full capacity when no recent posts."""
        mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
        instagram_service.history_repo.count_by_method.return_value = 0

        result = instagram_service.get_rate_limit_remaining()

        assert result == 100

    @patch("src.services.integrations.instagram_api.settings")
    def test_get_rate_limit_remaining_some_posts(
        self, mock_settings, instagram_service
    ):
        """Test rate limit calculation with recent posts."""
        mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
        instagram_service.history_repo.count_by_method.return_value = 10

        result = instagram_service.get_rate_limit_remaining()

        assert result == 90

    @patch("src.services.integrations.instagram_api.settings")
    def test_get_rate_limit_remaining_exhausted(self, mock_settings, instagram_service):
        """Test rate limit shows 0 when exhausted."""
        mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
        instagram_service.history_repo.count_by_method.return_value = 100

        result = instagram_service.get_rate_limit_remaining()

        assert result == 0

    @patch("src.services.integrations.instagram_api.settings")
    def test_get_rate_limit_remaining_over_limit(
        self, mock_settings, instagram_service
    ):
        """Test rate limit doesn't go negative."""
        mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
        instagram_service.history_repo.count_by_method.return_value = 130

        result = instagram_service.get_rate_limit_remaining()

        assert result == 0

    def test_get_rate_limit_remaining_correct_time_window(self, instagram_service):
        """Test rate limit uses a 24-hour trailing window (not 1 hour)."""
        instagram_service.history_repo.count_by_method.return_value = 0

        with patch("src.services.integrations.instagram_api.settings") as mock_settings:
            mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
            instagram_service.get_rate_limit_remaining()

        # Verify correct method and time window
        call_args = instagram_service.history_repo.count_by_method.call_args
        assert call_args[1]["method"] == "instagram_api"
        since = call_args[1]["since"]
        # Should be approximately 24 hours ago.
        now = datetime.now(timezone.utc)
        elapsed = (now - since).total_seconds()
        assert 23 * 3600 < elapsed < 24 * 3600 + 10

    # ============= get_content_publishing_limit Tests (Meta endpoint) =============

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_get_content_publishing_limit_parses_meta_response(
        self, mock_settings, instagram_service
    ):
        """Live Meta quota → remaining = quota_total - quota_usage, source=meta."""
        mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
        mock_settings.meta_ig_graph_base = "https://graph.facebook.com/v21.0"
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service._get_active_account_credentials = Mock(
            return_value=("tok", "acct-1", "user1")
        )

        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "data": [
                {
                    "config": {"quota_total": 100, "quota_duration": 86400},
                    "quota_usage": 3,
                }
            ]
        }

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.get = AsyncMock(return_value=response)

            result = await instagram_service.get_content_publishing_limit(-100123)

        assert result["quota_total"] == 100
        assert result["quota_usage"] == 3
        assert result["remaining"] == 97
        assert result["source"] == "meta"

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_get_content_publishing_limit_fails_open_on_http_error(
        self, mock_settings, instagram_service
    ):
        """A monitoring-endpoint blip must never block posting: fail open."""
        mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
        mock_settings.meta_ig_graph_base = "https://graph.facebook.com/v21.0"
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service._get_active_account_credentials = Mock(
            return_value=("tok", "acct-1", "user1")
        )

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.get = AsyncMock(
                side_effect=httpx.RequestError("connection reset")
            )

            result = await instagram_service.get_content_publishing_limit(-100123)

        assert result["source"] == "fallback"
        assert result["remaining"] == 100
        assert result["quota_total"] == 100
        assert result["quota_usage"] == 0

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_get_content_publishing_limit_no_creds_returns_fallback(
        self, mock_settings, instagram_service
    ):
        """No token/account → permissive fallback (never blocks the publish)."""
        mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service._get_active_account_credentials = Mock(
            return_value=(None, None, None)
        )

        result = await instagram_service.get_content_publishing_limit(-100123)

        assert result["source"] == "fallback"
        assert result["remaining"] == 100

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_get_content_publishing_limit_meta_ratelimit_returns_zero(
        self, mock_settings, instagram_service
    ):
        """If the endpoint itself returns Meta's rate-limit error, treat the
        account as fully consumed (remaining=0) — Meta itself says limited."""
        mock_settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK = 100
        mock_settings.meta_ig_graph_base = "https://graph.facebook.com/v21.0"
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service._get_active_account_credentials = Mock(
            return_value=("tok", "acct-1", "user1")
        )

        err_response = Mock()
        err_response.status_code = 400
        err_response.json.return_value = {
            "error": {"code": 4, "message": "Application request limit reached"}
        }

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.get = AsyncMock(return_value=err_response)

            result = await instagram_service.get_content_publishing_limit(-100123)

        assert result["remaining"] == 0
        assert result["quota_usage"] == 100
        assert result["source"] == "meta"

    # ==================== is_configured Tests ====================

    @patch("src.services.integrations.instagram_credentials.settings")
    def test_is_configured_all_settings(self, mock_settings, instagram_service):
        """Test is_configured returns True when all settings present."""
        mock_settings.ENABLE_INSTAGRAM_API = True
        mock_settings.INSTAGRAM_ACCOUNT_ID = "12345"
        mock_settings.FACEBOOK_APP_ID = "67890"

        assert instagram_service.is_configured() is True

    @patch("src.services.integrations.instagram_credentials.settings")
    def test_is_configured_chat_disabled(self, mock_settings, instagram_service):
        """Test is_configured returns False when the chat's per-chat toggle is off.

        Per-chat `chat_settings.enable_instagram_api` is the source of truth;
        the env `ENABLE_INSTAGRAM_API` is only the bootstrap default for new
        chats and the fallback when no chat_settings row exists.
        """
        mock_settings.ENABLE_INSTAGRAM_API = True
        mock_settings.INSTAGRAM_ACCOUNT_ID = "12345"
        mock_settings.FACEBOOK_APP_ID = "67890"

        chat_settings = Mock(enable_instagram_api=False)
        instagram_service.settings_service.get_settings_if_exists.return_value = (
            chat_settings
        )

        assert instagram_service.is_configured() is False

    @patch("src.services.integrations.instagram_credentials.settings")
    def test_is_configured_falls_back_to_env_when_no_chat_row(
        self, mock_settings, instagram_service
    ):
        """When chat_settings doesn't exist, env ENABLE_INSTAGRAM_API gates."""
        mock_settings.ENABLE_INSTAGRAM_API = False
        mock_settings.INSTAGRAM_ACCOUNT_ID = "12345"
        mock_settings.FACEBOOK_APP_ID = "67890"
        instagram_service.settings_service.get_settings_if_exists.return_value = None

        assert instagram_service.is_configured() is False

    @patch("src.services.integrations.instagram_credentials.settings")
    def test_is_configured_missing_account_id(self, mock_settings, instagram_service):
        """Test is_configured returns False when no active account and no legacy ID."""
        mock_settings.ENABLE_INSTAGRAM_API = True
        mock_settings.INSTAGRAM_ACCOUNT_ID = None
        mock_settings.FACEBOOK_APP_ID = "67890"
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123

        # No multi-account active, no legacy account ID
        instagram_service.account_service.get_active_account.return_value = None

        assert instagram_service.is_configured() is False

    @patch("src.services.integrations.instagram_credentials.settings")
    def test_is_configured_missing_app_id(self, mock_settings, instagram_service):
        """Test is_configured returns False when app ID missing."""
        mock_settings.ENABLE_INSTAGRAM_API = True
        mock_settings.INSTAGRAM_ACCOUNT_ID = "12345"
        mock_settings.FACEBOOK_APP_ID = None

        assert instagram_service.is_configured() is False

    # ==================== _check_response_errors Tests ====================

    def test_check_response_errors_success(self, instagram_service):
        """Test no error raised for 200 response."""
        mock_response = Mock()
        mock_response.status_code = 200

        # Should not raise
        instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_rate_limit_code_4(self, instagram_service):
        """Test RateLimitError for error code 4."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {"code": 4, "message": "Rate limit exceeded"}
        }

        with pytest.raises(RateLimitError, match="Rate limit exceeded"):
            instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_rate_limit_code_17(self, instagram_service):
        """Test RateLimitError for error code 17."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {"code": 17, "message": "User request limit reached"}
        }

        with pytest.raises(RateLimitError):
            instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_token_expired_code_190(self, instagram_service):
        """Test TokenExpiredError for error code 190."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.json.return_value = {
            "error": {"code": 190, "message": "Invalid OAuth access token"}
        }

        with pytest.raises(TokenExpiredError):
            instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_token_corrupt_cannot_parse(self, instagram_service):
        """Test TokenCorruptError for code 190 + 'cannot parse access token'."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {
                "code": 190,
                "message": "Error validating access token: Cannot parse access token",
            }
        }

        with pytest.raises(TokenCorruptError):
            instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_token_corrupt_malformed(self, instagram_service):
        """Test TokenCorruptError for code 190 + 'malformed access token'."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {
                "code": 190,
                "message": "Malformed access token",
            }
        }

        with pytest.raises(TokenCorruptError):
            instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_code_190_invalid_stays_expired(
        self, instagram_service
    ):
        """Regression: 'Invalid OAuth access token' must remain TokenExpiredError.

        Meta uses 'invalid' for legitimately expired tokens — classifying it
        as corrupt would prevent refresh attempts that could succeed.
        """
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.json.return_value = {
            "error": {"code": 190, "message": "Invalid OAuth access token"}
        }

        with pytest.raises(TokenExpiredError):
            instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_revocation_subcode_458(self, instagram_service):
        """Test TokenRevokedError for subcode 458 (app removed)."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {
                "code": 190,
                "message": "App not installed",
                "error_subcode": 458,
            }
        }

        with pytest.raises(TokenRevokedError) as exc_info:
            instagram_service._check_response_errors(mock_response)
        assert exc_info.value.error_subcode == 458

    def test_check_response_errors_oauth_error_102(self, instagram_service):
        """Test TokenExpiredError for OAuth error code 102."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {"code": 102, "message": "OAuth session expired"}
        }

        with pytest.raises(TokenExpiredError, match="OAuth error"):
            instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_general_api_error(self, instagram_service):
        """Test InstagramAPIError for general errors."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.json.return_value = {
            "error": {"code": 1, "message": "Internal server error"}
        }

        with pytest.raises(InstagramAPIError, match="Internal server error"):
            instagram_service._check_response_errors(mock_response)

    def test_check_response_errors_media_unsupported_code_9004(self, instagram_service):
        """Meta code 9004 ("Only photo or video can be accepted as media
        type.") classifies as MediaUnsupportedError so the autopost
        handler can permanent-reject the failing media item instead of
        letting it cycle through retries forever."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "error": {
                "code": 9004,
                "message": "Only photo or video can be accepted as media type.",
            }
        }

        with pytest.raises(MediaUnsupportedError) as exc_info:
            instagram_service._check_response_errors(mock_response)

        assert exc_info.value.error_code == "9004"
        # Must NOT be classified as a generic InstagramAPIError (it IS a
        # subclass, but isinstance() check in autopost flow requires
        # MediaUnsupportedError to come first in the chain to trigger
        # the permanent_reject lock).
        assert isinstance(exc_info.value, MediaUnsupportedError)

    def test_check_response_errors_invalid_json(self, instagram_service):
        """Test InstagramAPIError when response isn't JSON."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.json.side_effect = ValueError("Not JSON")

        with pytest.raises(InstagramAPIError, match="HTTP 500"):
            instagram_service._check_response_errors(mock_response)

    # ==================== post_story Tests ====================

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_post_story_rate_limit_exhausted(
        self, mock_settings, instagram_service
    ):
        """post_story raises RateLimitError when Meta's live quota is exhausted."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service._get_active_account_credentials = Mock(
            return_value=("tok", "acct-1", "user1")
        )
        instagram_service.get_content_publishing_limit = AsyncMock(
            return_value={
                "quota_total": 100,
                "quota_usage": 100,
                "remaining": 0,
                "source": "meta",
            }
        )

        with pytest.raises(RateLimitError, match="daily publishing limit reached"):
            await instagram_service.post_story("https://example.com/image.jpg")

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_post_story_no_token(self, mock_settings, instagram_service):
        """Test post_story raises TokenExpiredError when no token."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service.get_content_publishing_limit = AsyncMock(
            return_value={
                "quota_total": 100,
                "quota_usage": 0,
                "remaining": 100,
                "source": "fallback",
            }
        )
        instagram_service.token_service.get_token.return_value = None

        with pytest.raises(TokenExpiredError, match="No valid Instagram token"):
            await instagram_service.post_story("https://example.com/image.jpg")

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_credentials.settings")
    @patch("src.services.integrations.instagram_api.settings")
    async def test_post_story_no_account_id(
        self, mock_api_settings, mock_cred_settings, instagram_service
    ):
        """Test post_story raises error when no account configured."""
        mock_api_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service.get_content_publishing_limit = AsyncMock(
            return_value={
                "quota_total": 100,
                "quota_usage": 0,
                "remaining": 100,
                "source": "fallback",
            }
        )
        mock_cred_settings.INSTAGRAM_ACCOUNT_ID = None
        instagram_service.history_repo.count_by_method.return_value = 0
        instagram_service.token_service.get_token.return_value = "valid_token"

        # No multi-account active, no legacy account ID → (None, None, None)
        instagram_service.account_service.get_active_account.return_value = None

        with pytest.raises(TokenExpiredError, match="No valid Instagram token"):
            await instagram_service.post_story("https://example.com/image.jpg")

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_post_story_success(self, mock_api_settings, instagram_service):
        """Test successful story posting."""
        mock_api_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service.get_content_publishing_limit = AsyncMock(
            return_value={
                "quota_total": 100,
                "quota_usage": 0,
                "remaining": 100,
                "source": "fallback",
            }
        )
        # Multi-account: active account row + token record in DB
        active_account = Mock(
            id="acct-uuid",
            instagram_account_id="12345678",
            instagram_username="testaccount",
            auth_method="instagram_login",
        )
        instagram_service.account_service.get_active_account.return_value = (
            active_account
        )
        token_record = Mock(is_expired=False, token_value="encrypted")
        instagram_service.token_repo.get_token_for_account.return_value = token_record
        instagram_service.encryption.decrypt.return_value = "valid_token"

        # Mock container creation
        create_response = Mock()
        create_response.status_code = 200
        create_response.json.return_value = {"id": "container_123"}

        # Mock status polling
        status_response = Mock()
        status_response.status_code = 200
        status_response.json.return_value = {"status_code": "FINISHED"}

        # Mock publish
        publish_response = Mock()
        publish_response.status_code = 200
        publish_response.json.return_value = {"id": "story_456"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(
                side_effect=[create_response, publish_response]
            )
            mock_instance.get = AsyncMock(return_value=status_response)

            result = await instagram_service.post_story("https://example.com/image.jpg")

        assert result["success"] is True
        assert result["story_id"] == "story_456"
        assert result["container_id"] == "container_123"
        assert "timestamp" in result

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_post_story_network_error(self, mock_api_settings, instagram_service):
        """Test post_story handles network errors."""
        mock_api_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service.get_content_publishing_limit = AsyncMock(
            return_value={
                "quota_total": 100,
                "quota_usage": 0,
                "remaining": 100,
                "source": "fallback",
            }
        )
        active_account = Mock(
            id="acct-uuid",
            instagram_account_id="12345678",
            instagram_username="testaccount",
            auth_method="instagram_login",
        )
        instagram_service.account_service.get_active_account.return_value = (
            active_account
        )
        token_record = Mock(is_expired=False, token_value="encrypted")
        instagram_service.token_repo.get_token_for_account.return_value = token_record
        instagram_service.encryption.decrypt.return_value = "valid_token"

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(
                side_effect=httpx.RequestError("Connection failed")
            )

            with pytest.raises(InstagramAPIError, match="Network error"):
                await instagram_service.post_story("https://example.com/image.jpg")

    # ==================== _create_media_container Tests ====================

    @pytest.mark.asyncio
    async def test_create_media_container_image(self, instagram_service):
        """Test container creation for image."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "container_123"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=mock_response)

            result = await instagram_service._create_media_container(
                token="token",
                account_id="12345",
                media_url="https://example.com/image.jpg",
                media_type="IMAGE",
            )

        assert result == "container_123"

        # Verify image_url was used
        call_kwargs = mock_instance.post.call_args[1]
        assert "image_url" in call_kwargs["data"]

    @pytest.mark.asyncio
    async def test_create_media_container_video(self, instagram_service):
        """Test container creation for video."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "container_456"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=mock_response)

            result = await instagram_service._create_media_container(
                token="token",
                account_id="12345",
                media_url="https://example.com/video.mp4",
                media_type="VIDEO",
            )

        assert result == "container_456"

        # Verify video_url was used
        call_kwargs = mock_instance.post.call_args[1]
        assert "video_url" in call_kwargs["data"]

    @pytest.mark.asyncio
    async def test_create_media_container_no_id_in_response(self, instagram_service):
        """Test error when no container ID in response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # No "id"

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=mock_response)

            with pytest.raises(InstagramAPIError, match="No container ID"):
                await instagram_service._create_media_container(
                    token="token",
                    account_id="12345",
                    media_url="https://example.com/image.jpg",
                    media_type="IMAGE",
                )

    # ==================== _wait_for_container_ready Tests ====================

    @pytest.mark.asyncio
    async def test_wait_for_container_ready_immediate(self, instagram_service):
        """Test container immediately ready."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status_code": "FINISHED"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.get = AsyncMock(return_value=mock_response)

            # Should not raise
            await instagram_service._wait_for_container_ready("token", "container_123")

    @pytest.mark.asyncio
    async def test_wait_for_container_ready_error_status(self, instagram_service):
        """Test container fails with ERROR status."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status_code": "ERROR",
            "status": "Media processing failed",
        }

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.get = AsyncMock(return_value=mock_response)

            with pytest.raises(InstagramAPIError, match="Media container failed"):
                await instagram_service._wait_for_container_ready(
                    "token", "container_123"
                )

    @pytest.mark.asyncio
    async def test_wait_for_container_ready_expired_status(self, instagram_service):
        """Test container fails with EXPIRED status."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status_code": "EXPIRED"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.get = AsyncMock(return_value=mock_response)

            with pytest.raises(InstagramAPIError, match="expired before publishing"):
                await instagram_service._wait_for_container_ready(
                    "token", "container_123"
                )

    @pytest.mark.asyncio
    @patch(
        "src.services.integrations.instagram_api.asyncio.sleep", new_callable=AsyncMock
    )
    async def test_wait_for_container_ready_timeout(
        self, mock_sleep, instagram_service
    ):
        """Test container polling times out."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status_code": "IN_PROGRESS"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.get = AsyncMock(return_value=mock_response)

            with pytest.raises(InstagramAPIError, match="did not finish"):
                await instagram_service._wait_for_container_ready(
                    "token", "container_123"
                )

        # Verify it polled the maximum number of times
        assert (
            mock_instance.get.await_count
            == instagram_service.CONTAINER_STATUS_MAX_POLLS
        )

    # ==================== _publish_container Tests ====================

    @pytest.mark.asyncio
    async def test_publish_container_success(self, instagram_service):
        """Test successful container publishing."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "story_789"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=mock_response)

            result = await instagram_service._publish_container(
                token="token",
                account_id="12345",
                container_id="container_123",
            )

        assert result == "story_789"

    @pytest.mark.asyncio
    async def test_publish_container_no_story_id(self, instagram_service):
        """Test error when no story ID in response."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {}  # No "id"

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=mock_response)

            with pytest.raises(InstagramAPIError, match="No story ID"):
                await instagram_service._publish_container(
                    token="token",
                    account_id="12345",
                    container_id="container_123",
                )

    # ==================== validate_media_url Tests ====================

    @pytest.mark.asyncio
    async def test_validate_media_url_success(self, instagram_service):
        """Test successful URL validation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {
            "content-type": "image/jpeg",
            "content-length": "123456",
        }

        with patch(
            "src.services.integrations.instagram_credentials.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.head = AsyncMock(return_value=mock_response)

            result = await instagram_service.validate_media_url(
                "https://example.com/image.jpg"
            )

        assert result["valid"] is True
        assert result["content_type"] == "image/jpeg"
        assert result["size_bytes"] == 123456

    @pytest.mark.asyncio
    async def test_validate_media_url_not_found(self, instagram_service):
        """Test URL validation for 404."""
        mock_response = Mock()
        mock_response.status_code = 404

        with patch(
            "src.services.integrations.instagram_credentials.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.head = AsyncMock(return_value=mock_response)

            result = await instagram_service.validate_media_url(
                "https://example.com/missing.jpg"
            )

        assert result["valid"] is False
        assert "404" in result["error"]

    @pytest.mark.asyncio
    async def test_validate_media_url_network_error(self, instagram_service):
        """Test URL validation handles network errors."""
        with patch(
            "src.services.integrations.instagram_credentials.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.head = AsyncMock(side_effect=httpx.RequestError("DNS error"))

            result = await instagram_service.validate_media_url(
                "https://example.com/image.jpg"
            )

        assert result["valid"] is False
        assert "DNS error" in result["error"]

    @pytest.mark.asyncio
    async def test_validate_media_url_no_content_length(self, instagram_service):
        """Test URL validation when no content-length header."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "image/png"}

        with patch(
            "src.services.integrations.instagram_credentials.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.head = AsyncMock(return_value=mock_response)

            result = await instagram_service.validate_media_url(
                "https://example.com/image.png"
            )

        assert result["valid"] is True
        assert result["size_bytes"] is None

    # ==================== _check_response_errors Tests ====================

    def test_check_response_errors_revocation_subcode_458(self, instagram_service):
        """Test _check_response_errors raises TokenRevokedError for subcode 458."""
        response = Mock()
        response.status_code = 400
        response.json.return_value = {
            "error": {
                "message": "App not installed",
                "code": 190,
                "error_subcode": 458,
            }
        }

        with pytest.raises(TokenRevokedError) as exc_info:
            instagram_service._check_response_errors(response)

        assert exc_info.value.error_subcode == 458

    # ==================== IG Login host routing (PR 1) ====================

    @pytest.mark.asyncio
    async def test_create_media_container_posts_to_ig_graph_host(
        self, instagram_service
    ):
        """Container creation must POST to graph.instagram.com — IG-Login tokens
        return Meta code 190 'Cannot parse access token' on graph.facebook.com."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "container_xyz"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=mock_response)

            await instagram_service._create_media_container(
                token="t",
                account_id="12345",
                media_url="https://example.com/img.jpg",
                media_type="IMAGE",
            )

        url = mock_instance.post.call_args[0][0]
        assert url.startswith("https://graph.instagram.com/"), url
        assert "graph.facebook.com" not in url

    @pytest.mark.asyncio
    async def test_publish_container_posts_to_ig_graph_host(self, instagram_service):
        """media_publish must hit graph.instagram.com for the same reason."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "story_xyz"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.post = AsyncMock(return_value=mock_response)

            await instagram_service._publish_container(
                token="t",
                account_id="12345",
                container_id="container_xyz",
            )

        url = mock_instance.post.call_args[0][0]
        assert url.startswith("https://graph.instagram.com/"), url
        assert "media_publish" in url
        assert "graph.facebook.com" not in url

    @pytest.mark.asyncio
    async def test_wait_for_container_ready_uses_ig_graph_host(self, instagram_service):
        """Container status polling must hit graph.instagram.com."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status_code": "FINISHED"}

        with patch(
            "src.services.integrations.instagram_api.httpx.AsyncClient"
        ) as mock_client:
            mock_instance = mock_client.return_value
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            mock_instance.get = AsyncMock(return_value=mock_response)

            await instagram_service._wait_for_container_ready("t", "container_xyz")

        url = mock_instance.get.call_args[0][0]
        assert url.startswith("https://graph.instagram.com/"), url
        assert "graph.facebook.com" not in url

    def test_get_active_account_credentials_rejects_non_ig_login_auth_method(
        self, instagram_service
    ):
        """When the active account has no instagram_login token (e.g. it
        only has a legacy fb_login row), the lookup returns None and we
        surface a reconnect prompt rather than serving an incompatible
        credential. After #468 the auth_method filter happens in the
        repo query — no row matches, so we just hand back (None, …)."""
        active_account = Mock(
            id="acct-uuid",
            instagram_account_id="12345678",
            instagram_username="legacy",
            auth_method="fb_login",
        )
        instagram_service.account_service.get_active_account.return_value = (
            active_account
        )
        # Repo returns nothing for the (account, auth_method='instagram_login')
        # filter — no IG-Login token exists for this account.
        instagram_service.token_repo.get_token_for_account.return_value = None

        token, account_id, username = instagram_service._get_active_account_credentials(
            -100123
        )

        assert token is None
        assert account_id is None
        assert username is None
        # Lookup was filtered by auth_method='instagram_login'.
        call_kwargs = (
            instagram_service.token_repo.get_token_for_account.call_args.kwargs
        )
        assert call_kwargs.get("auth_method") == "instagram_login"

    def test_get_active_account_credentials_rejects_null_auth_method(
        self, instagram_service
    ):
        """Accounts that have no IG-Login token at all (e.g. legacy
        rows from before migration 039 backfilled auth_method) must
        not be served credentials. The repo query filtered by
        auth_method='instagram_login' returns None and we hand back
        (None, …) so the safety check surfaces a clear error."""
        active_account = Mock(
            id="acct-uuid",
            instagram_account_id="12345678",
            instagram_username="legacy",
            auth_method=None,
        )
        instagram_service.account_service.get_active_account.return_value = (
            active_account
        )
        instagram_service.token_repo.get_token_for_account.return_value = None

        token, _, _ = instagram_service._get_active_account_credentials(-100123)
        assert token is None
        call_kwargs = (
            instagram_service.token_repo.get_token_for_account.call_args.kwargs
        )
        assert call_kwargs.get("auth_method") == "instagram_login"


@pytest.mark.unit
class TestPostStoryContainerCallback:
    """post_story exposes an on_container_created seam so the caller can persist
    the container_id BEFORE the publish call (claim-before-publish, #549)."""

    @pytest.fixture
    def instagram_service(self):
        with (
            patch("src.services.integrations.instagram_api.TokenRefreshService"),
            patch("src.services.integrations.instagram_api.CloudStorageService"),
            patch("src.services.integrations.instagram_api.HistoryRepository"),
            patch("src.services.integrations.instagram_api.InstagramAccountService"),
            patch("src.services.integrations.instagram_api.TokenRepository"),
            patch("src.services.integrations.instagram_api.TokenEncryption"),
            patch("src.services.integrations.instagram_api.SettingsService"),
            patch("src.services.base_service.ServiceRunRepository"),
        ):
            from src.services.integrations.instagram_api import InstagramAPIService

            service = InstagramAPIService()
            service.history_repo = Mock()
            service.track_execution = mock_track_execution
            service.set_result_summary = Mock()
            return service

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_callback_fires_after_container_before_publish(
        self, mock_settings, instagram_service
    ):
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service.get_content_publishing_limit = AsyncMock(
            return_value={
                "quota_total": 100,
                "quota_usage": 0,
                "remaining": 100,
                "source": "fallback",
            }
        )
        instagram_service._get_active_account_credentials = Mock(
            return_value=("tok", "acct-1", "user1")
        )

        order = []
        instagram_service._create_media_container = AsyncMock(
            return_value="container-xyz"
        )

        async def _wait(token, cid):
            order.append(("wait", cid))

        instagram_service._wait_for_container_ready = AsyncMock(side_effect=_wait)

        async def _pub(token, account_id, container_id):
            order.append(("publish", container_id))
            return "story-1"

        instagram_service._publish_container = AsyncMock(side_effect=_pub)

        persisted = []

        def _cb(cid):
            persisted.append(cid)
            order.append(("callback", cid))

        result = await instagram_service.post_story(
            "https://example.com/img.jpg",
            media_type="IMAGE",
            telegram_chat_id=-100123,
            on_container_created=_cb,
        )

        assert result["story_id"] == "story-1"
        assert result["container_id"] == "container-xyz"
        # Container persisted exactly once, strictly before the publish step.
        assert persisted == ["container-xyz"]
        assert order == [
            ("callback", "container-xyz"),
            ("wait", "container-xyz"),
            ("publish", "container-xyz"),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", ["ERROR", "EXPIRED"])
    @patch("src.services.integrations.instagram_api.settings")
    async def test_confirmed_dead_container_error_propagates_after_callback(
        self, mock_settings, instagram_service, status_code
    ):
        """When IG marks the container ERROR/EXPIRED, _wait_for_container_ready
        raises an InstagramAPIError carrying error_code=status_code. post_story
        must let it propagate — AFTER the claim-before-publish callback fired
        (anchor persisted) and WITHOUT ever publishing — so callers can classify
        it as an IG-confirmed failure and release the row for retry."""
        from src.exceptions import is_container_confirmed_failed

        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service.get_content_publishing_limit = AsyncMock(
            return_value={
                "quota_total": 100,
                "quota_usage": 0,
                "remaining": 100,
                "source": "fallback",
            }
        )
        instagram_service._get_active_account_credentials = Mock(
            return_value=("tok", "acct-1", "user1")
        )
        instagram_service._create_media_container = AsyncMock(
            return_value="container-xyz"
        )
        instagram_service._wait_for_container_ready = AsyncMock(
            side_effect=InstagramAPIError(
                "Media container failed", error_code=status_code
            )
        )
        instagram_service._publish_container = AsyncMock()

        persisted = []

        with pytest.raises(InstagramAPIError) as excinfo:
            await instagram_service.post_story(
                "https://example.com/img.jpg",
                media_type="IMAGE",
                telegram_chat_id=-100123,
                on_container_created=persisted.append,
            )

        # Anchor persisted before the failure; publish never attempted.
        assert persisted == ["container-xyz"]
        instagram_service._publish_container.assert_not_called()
        # The raised error carries IG's status_code → classifies as confirmed-dead.
        assert excinfo.value.error_code == status_code
        assert is_container_confirmed_failed(excinfo.value) is True

    @pytest.mark.asyncio
    @patch("src.services.integrations.instagram_api.settings")
    async def test_post_story_works_without_callback(
        self, mock_settings, instagram_service
    ):
        """on_container_created is optional — the legacy call still works."""
        mock_settings.ADMIN_TELEGRAM_CHAT_ID = -100123
        instagram_service.get_content_publishing_limit = AsyncMock(
            return_value={
                "quota_total": 100,
                "quota_usage": 0,
                "remaining": 100,
                "source": "fallback",
            }
        )
        instagram_service._get_active_account_credentials = Mock(
            return_value=("tok", "acct-1", "user1")
        )
        instagram_service._create_media_container = AsyncMock(return_value="c-1")
        instagram_service._wait_for_container_ready = AsyncMock()
        instagram_service._publish_container = AsyncMock(return_value="story-1")

        result = await instagram_service.post_story("https://example.com/img.jpg")
        assert result["story_id"] == "story-1"


@pytest.mark.unit
class TestContainerConfirmedFailedClassifier:
    """is_container_confirmed_failed distinguishes an IG-*confirmed* container
    failure (status_code ERROR/EXPIRED — IG says nothing published, safe to
    release for retry) from an ambiguous crash/timeout (publish outcome
    unknown, must stay stuck)."""

    @pytest.mark.parametrize("status_code", ["ERROR", "EXPIRED"])
    def test_true_for_ig_confirmed_status_codes(self, status_code):
        from src.exceptions import is_container_confirmed_failed

        exc = InstagramAPIError("container failed", error_code=status_code)
        assert is_container_confirmed_failed(exc) is True

    def test_false_for_ambiguous_instagram_error(self):
        from src.exceptions import is_container_confirmed_failed

        # A timeout/crash carries no ERROR/EXPIRED status_code → ambiguous.
        assert is_container_confirmed_failed(InstagramAPIError("timed out")) is False

    def test_false_for_non_instagram_exception(self):
        from src.exceptions import is_container_confirmed_failed

        assert is_container_confirmed_failed(RuntimeError("boom")) is False
        assert is_container_confirmed_failed(TimeoutError()) is False
