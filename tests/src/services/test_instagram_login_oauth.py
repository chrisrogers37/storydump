"""Tests for InstagramLoginOAuthService."""

import json

import pytest
from unittest.mock import AsyncMock, Mock, patch

from src.services.integrations.instagram_login_oauth import InstagramLoginOAuthService
from tests.src.services.conftest import mock_track_execution


@pytest.mark.unit
class TestInstagramLoginStateTokens:
    """Tests for state token generation and validation."""

    @pytest.fixture(autouse=True)
    def setup_service(self):
        with patch.object(InstagramLoginOAuthService, "__init__", lambda self: None):
            self.service = InstagramLoginOAuthService()
            self.service.service_run_repo = Mock()
            self.service.service_name = "InstagramLoginOAuthService"
            self.service._encryption = None

    @patch("src.services.integrations.instagram_login_oauth.TokenEncryption")
    def test_state_token_contains_chat_id(self, MockEncryption):
        """State token payload includes chat_id and provider."""
        mock_encryption = MockEncryption.return_value
        mock_encryption.encrypt.return_value = "encrypted_state"
        self.service._encryption = mock_encryption

        result = self.service._create_state_token(-1001234567890)

        assert result == "encrypted_state"
        call_args = mock_encryption.encrypt.call_args[0][0]
        payload = json.loads(call_args)
        assert payload["chat_id"] == -1001234567890
        assert payload["provider"] == "instagram_login"

    @patch("src.services.integrations.instagram_login_oauth.TokenEncryption")
    def test_state_token_includes_nonce(self, MockEncryption):
        """State token payload includes a random nonce."""
        mock_encryption = MockEncryption.return_value
        mock_encryption.encrypt.return_value = "encrypted"
        self.service._encryption = mock_encryption

        self.service._create_state_token(-100123)

        call_args = mock_encryption.encrypt.call_args[0][0]
        payload = json.loads(call_args)
        assert "nonce" in payload
        assert len(payload["nonce"]) == 32

    def test_validate_state_token_extracts_chat_id(self):
        """Valid state token returns chat_id."""
        mock_cipher = Mock()
        mock_cipher.decrypt.return_value = json.dumps(
            {"chat_id": -100123, "provider": "instagram_login", "nonce": "abc"}
        ).encode()
        mock_encryption = Mock()
        mock_encryption._cipher = mock_cipher
        self.service._encryption = mock_encryption

        result = self.service.validate_state_token("encrypted_state")
        assert result == -100123

    def test_validate_state_token_missing_chat_id_raises(self):
        """State token without chat_id raises ValueError."""
        mock_cipher = Mock()
        mock_cipher.decrypt.return_value = json.dumps(
            {"provider": "instagram_login", "nonce": "abc"}
        ).encode()
        mock_encryption = Mock()
        mock_encryption._cipher = mock_cipher
        self.service._encryption = mock_encryption

        with pytest.raises(ValueError, match="missing chat_id"):
            self.service.validate_state_token("bad_token")


@pytest.mark.unit
class TestInstagramLoginAuthUrl:
    """Tests for authorization URL generation."""

    @pytest.fixture(autouse=True)
    def setup_service(self):
        with patch.object(InstagramLoginOAuthService, "__init__", lambda self: None):
            self.service = InstagramLoginOAuthService()
            self.service.service_run_repo = Mock()
            self.service.service_name = "InstagramLoginOAuthService"
            self.service._encryption = None

    @patch("src.services.integrations.instagram_login_oauth.settings")
    @patch("src.services.integrations.instagram_login_oauth.TokenEncryption")
    def test_url_contains_instagram_params(self, MockEncryption, mock_settings):
        """Auth URL includes Instagram Login endpoint and scopes."""
        mock_settings.INSTAGRAM_APP_ID = "test_app_id"
        mock_settings.INSTAGRAM_APP_SECRET = "test_secret"
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
        mock_settings.ENCRYPTION_KEY = "test_key"

        mock_encryption = MockEncryption.return_value
        mock_encryption.encrypt.return_value = "state123"
        self.service._encryption = mock_encryption

        url = self.service.generate_authorization_url(-100123)

        assert "api.instagram.com/oauth/authorize" in url
        assert "client_id=test_app_id" in url
        assert "instagram_business_basic" in url
        assert "instagram_business_content_publish" in url
        assert "state=state123" in url

    @patch("src.services.integrations.instagram_login_oauth.settings")
    def test_missing_app_id_raises(self, mock_settings):
        """Missing INSTAGRAM_APP_ID raises ValueError."""
        mock_settings.INSTAGRAM_APP_ID = None
        mock_settings.INSTAGRAM_APP_SECRET = "test"
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"
        mock_settings.ENCRYPTION_KEY = "key"

        with pytest.raises(ValueError, match="INSTAGRAM_APP_ID"):
            self.service.generate_authorization_url(-100123)


@pytest.mark.unit
class TestInstagramLoginExchange:
    """Tests for token exchange and storage."""

    @pytest.fixture(autouse=True)
    def setup_service(self):
        with patch.object(InstagramLoginOAuthService, "__init__", lambda self: None):
            self.service = InstagramLoginOAuthService()
            self.service.service_run_repo = Mock()
            self.service.service_name = "InstagramLoginOAuthService"
            self.service.track_execution = mock_track_execution
            self.service.set_result_summary = Mock()
            self.service.settings_repo = Mock()
            self.service.account_service = Mock()
            self.service._encryption = None

    @patch("src.services.integrations.instagram_login_oauth.settings")
    @pytest.mark.asyncio
    async def test_exchange_stores_new_account(self, mock_settings):
        """Full exchange flow creates a new Instagram account."""
        mock_settings.INSTAGRAM_APP_ID = "app_id"
        mock_settings.INSTAGRAM_APP_SECRET = "secret"
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"

        self.service.account_service.get_account_by_instagram_id.return_value = None
        self.service.account_service.get_account_by_username.return_value = None

        # Mock HTTP calls
        short_response = Mock(status_code=200)
        short_response.json.return_value = {
            "data": [
                {
                    "access_token": "short_token",
                    "user_id": "12345",
                    "permissions": "instagram_business_basic",
                }
            ]
        }

        long_response = Mock(status_code=200)
        long_response.json.return_value = {
            "access_token": "long_lived_token",
            "token_type": "bearer",
            "expires_in": 5184000,
        }

        username_response = Mock(status_code=200)
        username_response.json.return_value = {"username": "testuser"}

        with patch(
            "src.services.integrations.instagram_login_oauth.httpx.AsyncClient"
        ) as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_client.post.return_value = short_response
            mock_client.get.side_effect = [long_response, username_response]

            result = await self.service.exchange_and_store("auth_code#_", -100123)

        assert result["username"] == "testuser"
        assert result["account_id"] == "12345"
        assert result["expires_in_days"] == 60

        self.service.account_service.add_account.assert_called_once()
        call_kwargs = self.service.account_service.add_account.call_args[1]
        assert call_kwargs["auth_method"] == "instagram_login"
        assert call_kwargs["instagram_account_id"] == "12345"

    @patch("src.services.integrations.instagram_login_oauth.settings")
    @pytest.mark.asyncio
    async def test_exchange_updates_existing_account(self, mock_settings):
        """Exchange flow updates existing account instead of creating."""
        mock_settings.INSTAGRAM_APP_ID = "app_id"
        mock_settings.INSTAGRAM_APP_SECRET = "secret"
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"

        self.service.account_service.get_account_by_instagram_id.return_value = Mock(
            id="existing-uuid",
            instagram_account_id="12345",
        )

        short_response = Mock(status_code=200)
        short_response.json.return_value = {
            "data": [{"access_token": "short", "user_id": "12345"}]
        }

        long_response = Mock(status_code=200)
        long_response.json.return_value = {
            "access_token": "long",
            "expires_in": 5184000,
        }

        username_response = Mock(status_code=200)
        username_response.json.return_value = {"username": "existing_user"}

        with patch(
            "src.services.integrations.instagram_login_oauth.httpx.AsyncClient"
        ) as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_client.post.return_value = short_response
            mock_client.get.side_effect = [long_response, username_response]

            result = await self.service.exchange_and_store("auth_code", -100123)

        assert result["username"] == "existing_user"
        self.service.account_service.update_account_token.assert_called_once()
        self.service.account_service.add_account.assert_not_called()
        # Username fallback wasn't needed because we found by ID first.
        self.service.account_service.get_account_by_username.assert_not_called()

    @patch("src.services.integrations.instagram_login_oauth.settings")
    @pytest.mark.asyncio
    async def test_exchange_cross_flow_username_fallback(self, mock_settings):
        """If get_by_id misses (FB Login stored a different ID), fall back to
        username lookup and update the existing row in place rather than
        failing on the duplicate-username uniqueness check."""
        mock_settings.INSTAGRAM_APP_ID = "app_id"
        mock_settings.INSTAGRAM_APP_SECRET = "secret"
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"

        # No match by IG Login user_id (88888) — FB Login stored a different ID.
        self.service.account_service.get_account_by_instagram_id.return_value = None
        # But username match: row stored under the FB Login Business Account ID.
        self.service.account_service.get_account_by_username.return_value = Mock(
            id="existing-uuid",
            instagram_account_id="17841000000001",  # the stored FB Login ID
            instagram_username="gatortails",
        )

        short_response = Mock(status_code=200)
        short_response.json.return_value = {
            "data": [{"access_token": "short", "user_id": "88888"}]
        }
        long_response = Mock(status_code=200)
        long_response.json.return_value = {
            "access_token": "long",
            "expires_in": 5184000,
        }
        username_response = Mock(status_code=200)
        username_response.json.return_value = {"username": "gatortails"}

        with patch(
            "src.services.integrations.instagram_login_oauth.httpx.AsyncClient"
        ) as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_client.post.return_value = short_response
            mock_client.get.side_effect = [long_response, username_response]

            await self.service.exchange_and_store("auth_code", -100123)

        # Username fallback was used.
        self.service.account_service.get_account_by_username.assert_called_once_with(
            "gatortails"
        )
        # update_account_token called with the EXISTING stored ID — not the new
        # IG Login user_id. Rotating it would break FB Login lookups.
        self.service.account_service.update_account_token.assert_called_once()
        call_kwargs = self.service.account_service.update_account_token.call_args[1]
        assert call_kwargs["instagram_account_id"] == "17841000000001"
        assert call_kwargs["instagram_username"] == "gatortails"
        assert call_kwargs["auth_method"] == "instagram_login"
        self.service.account_service.add_account.assert_not_called()

    @patch("src.services.integrations.instagram_login_oauth.settings")
    @pytest.mark.asyncio
    async def test_exchange_strips_hash_suffix(self, mock_settings):
        """Auth code has #_ suffix stripped before exchange."""
        mock_settings.INSTAGRAM_APP_ID = "app_id"
        mock_settings.INSTAGRAM_APP_SECRET = "secret"
        mock_settings.OAUTH_REDIRECT_BASE_URL = "https://example.com"

        self.service.account_service.get_account_by_instagram_id.return_value = None
        self.service.account_service.get_account_by_username.return_value = None

        short_response = Mock(status_code=200)
        short_response.json.return_value = {
            "data": [{"access_token": "tok", "user_id": "1"}]
        }
        long_response = Mock(status_code=200)
        long_response.json.return_value = {
            "access_token": "long",
            "expires_in": 5184000,
        }
        username_response = Mock(status_code=200)
        username_response.json.return_value = {"username": "u"}

        with patch(
            "src.services.integrations.instagram_login_oauth.httpx.AsyncClient"
        ) as MockClient:
            mock_client = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_client.post.return_value = short_response
            mock_client.get.side_effect = [long_response, username_response]

            await self.service.exchange_and_store("mycode#_", -100123)

        # Verify the POST was called with stripped code
        post_call = mock_client.post.call_args
        assert post_call[1]["data"]["code"] == "mycode"
