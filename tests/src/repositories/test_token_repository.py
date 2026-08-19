"""Tests for TokenRepository."""

import uuid

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timedelta

from src.repositories.token_repository import TokenRepository
from src.models.api_token import ApiToken
from src.repositories.tenant_scope import SYSTEM_SCOPE


@pytest.mark.unit
class TestTokenRepository:
    """Test suite for TokenRepository."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        return MagicMock()

    @pytest.fixture
    def token_repo(self, mock_db):
        """Create TokenRepository with mocked database."""
        with patch("src.repositories.base_repository.get_db") as mock_get_db:
            mock_get_db.return_value = iter([mock_db])
            repo = TokenRepository()
            repo._db = mock_db
            return repo

    def test_get_token_found(self, token_repo, mock_db):
        """Test getting a token by service name and type."""
        mock_token = Mock(spec=ApiToken)
        mock_token.service_name = "instagram"
        mock_token.token_type = "access_token"
        # get_token chains two .filter() calls (service/type + account_id)
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = mock_token

        result = token_repo.get_token("instagram", "access_token")

        assert result is mock_token

    def test_get_token_not_found(self, token_repo, mock_db):
        """Test getting a non-existent token returns None."""
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        result = token_repo.get_token("instagram", "access_token")

        assert result is None

    def test_get_token_with_account_id(self, token_repo, mock_db):
        """Test getting a token filtered by account ID."""
        mock_token = Mock(spec=ApiToken)
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = mock_token

        result = token_repo.get_token(
            "instagram", "access_token", instagram_account_id="acc-uuid-1"
        )

        assert result is mock_token

    def test_get_token_for_update_found(self, token_repo, mock_db):
        """Test getting a token with row lock."""
        mock_token = Mock(spec=ApiToken)
        mock_db.query.return_value.filter.return_value.filter.return_value.with_for_update.return_value.first.return_value = mock_token

        result = token_repo.get_token_for_update("instagram", "access_token")

        assert result is mock_token

    def test_get_token_for_update_skip_locked(self, token_repo, mock_db):
        """Test SKIP LOCKED returns None when row is locked."""
        mock_db.query.return_value.filter.return_value.filter.return_value.with_for_update.return_value.first.return_value = None

        result = token_repo.get_token_for_update("instagram", "access_token")

        assert result is None

    def test_get_token_for_update_with_account_id(self, token_repo, mock_db):
        """Test row-lock with account ID filter."""
        mock_token = Mock(spec=ApiToken)
        mock_db.query.return_value.filter.return_value.filter.return_value.with_for_update.return_value.first.return_value = mock_token

        result = token_repo.get_token_for_update(
            "instagram", "access_token", instagram_account_id="acc-uuid-1"
        )

        assert result is mock_token

    def test_get_token_for_account(self, token_repo, mock_db):
        """Test convenience method for getting Instagram token by account."""
        mock_token = Mock(spec=ApiToken)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_token

        result = token_repo.get_token_for_account("acc-uuid-1")

        assert result is mock_token

    def test_get_all_instagram_tokens(self, token_repo, mock_db):
        """Test getting all Instagram tokens for refresh iteration."""
        mock_tokens = [Mock(spec=ApiToken), Mock(spec=ApiToken)]
        mock_db.query.return_value.filter.return_value.all.return_value = mock_tokens

        result = token_repo.get_all_instagram_tokens()

        assert len(result) == 2

    def test_create_or_update_creates_new_token(self, token_repo, mock_db):
        """Test create_or_update creates a new token when none exists."""
        # get_token uses double filter chain
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="encrypted_value",
            issued_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=60),
            scopes=["instagram_basic", "instagram_content_publish"],
            metadata={"method": "cli_wizard"},
            chat_settings_id=SYSTEM_SCOPE,
        )

        mock_db.add.assert_called_once()
        mock_db.commit.assert_called()
        added_obj = mock_db.add.call_args[0][0]
        assert isinstance(added_obj, ApiToken)
        assert added_obj.service_name == "instagram"
        assert added_obj.token_value == "encrypted_value"

    def test_create_or_update_updates_existing_token(self, token_repo, mock_db):
        """Test create_or_update updates when token already exists."""
        existing_token = Mock(spec=ApiToken)
        existing_token.token_value = "old_encrypted"
        # get_token uses double filter chain
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = existing_token

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="new_encrypted",
            expires_at=datetime.utcnow() + timedelta(days=60),
            scopes=["instagram_basic"],
            chat_settings_id=SYSTEM_SCOPE,
        )

        mock_db.add.assert_not_called()
        assert existing_token.token_value == "new_encrypted"
        mock_db.commit.assert_called()

    def test_create_or_update_persists_auth_method_and_issuing_app_id(
        self, token_repo, mock_db
    ):
        """New tokens carry auth_method + issuing_app_id (#468)."""
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="encrypted",
            issued_at=datetime.utcnow(),
            instagram_account_id="acct-uuid",
            meta_account_id="26060527550287223",
            auth_method="instagram_login",
            issuing_app_id="ig_app_456",
            chat_settings_id=SYSTEM_SCOPE,
        )

        added = mock_db.add.call_args[0][0]
        assert added.auth_method == "instagram_login"
        assert added.issuing_app_id == "ig_app_456"

    def test_create_or_update_updates_auth_method_when_provided(
        self, token_repo, mock_db
    ):
        """Existing tokens get auth_method/issuing_app_id updated on
        re-issue but preserved when the caller omits them (matches the
        meta_account_id pattern — refresh paths don't need to know)."""
        existing = Mock(spec=ApiToken)
        existing.token_value = "old"
        existing.auth_method = "fb_login"
        existing.issuing_app_id = "old_app"
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = existing

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="new",
            auth_method="instagram_login",
            issuing_app_id="new_app",
            chat_settings_id=SYSTEM_SCOPE,
        )

        assert existing.auth_method == "instagram_login"
        assert existing.issuing_app_id == "new_app"

    def test_create_or_update_preserves_auth_method_when_omitted(
        self, token_repo, mock_db
    ):
        """Refresh path doesn't pass auth_method/issuing_app_id; the
        existing row's values must be preserved rather than nulled."""
        existing = Mock(spec=ApiToken)
        existing.token_value = "old"
        existing.auth_method = "instagram_login"
        existing.issuing_app_id = "ig_app_456"
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = existing

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="new",
            chat_settings_id=SYSTEM_SCOPE,
        )

        # Existing fields untouched — only the auth-related fields
        # need to be guarded against accidental nulling.
        assert existing.auth_method == "instagram_login"
        assert existing.issuing_app_id == "ig_app_456"

    def test_update_last_refreshed(self, token_repo, mock_db):
        """Test updating last_refreshed_at timestamp."""
        mock_token = Mock(spec=ApiToken)
        # update_last_refreshed calls get_token which uses double filter
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = mock_token

        result = token_repo.update_last_refreshed("instagram", "access_token")

        assert result is True
        assert mock_token.last_refreshed_at is not None
        mock_db.commit.assert_called()

    def test_update_last_refreshed_not_found(self, token_repo, mock_db):
        """Test update_last_refreshed returns False when token not found."""
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        result = token_repo.update_last_refreshed("instagram", "access_token")

        assert result is False

    def test_get_expiring_tokens(self, token_repo, mock_db):
        """Test getting tokens expiring within threshold."""
        mock_tokens = [Mock(spec=ApiToken)]
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = mock_tokens

        result = token_repo.get_expiring_tokens(hours_until_expiry=168)

        assert len(result) == 1


@pytest.mark.unit
class TestTokenRepositoryTenantScoped:
    """Tests for tenant-scoped (chat_settings_id) token methods."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def token_repo(self, mock_db):
        with patch("src.repositories.base_repository.get_db") as mock_get_db:
            mock_get_db.return_value = iter([mock_db])
            repo = TokenRepository()
            repo._db = mock_db
            return repo

    def test_create_or_update_stamps_new_token_with_chat(self, token_repo, mock_db):
        """#675 — the create arm stamps chat_settings_id so ownership is
        derivable for accounts added by a chat."""
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="enc",
            instagram_account_id="acct-1",
            chat_settings_id="cs-uuid-1",
        )

        added = mock_db.add.call_args[0][0]
        assert added.chat_settings_id == "cs-uuid-1"

    def test_create_or_update_stamps_existing_token_when_chat_provided(
        self, token_repo, mock_db
    ):
        """Re-issue by a chat stamps (or re-stamps) the credential."""
        existing = Mock(spec=ApiToken)
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = existing

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="enc",
            instagram_account_id="acct-1",
            chat_settings_id="cs-uuid-2",
        )

        assert existing.chat_settings_id == "cs-uuid-2"

    def test_create_or_update_preserves_stamp_when_chat_absent(
        self, token_repo, mock_db
    ):
        """Chat-less writes (token refresh) must not clear ownership."""
        existing = Mock(spec=ApiToken)
        existing.chat_settings_id = "cs-uuid-1"
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = existing

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="enc",
            instagram_account_id="acct-1",
            chat_settings_id=SYSTEM_SCOPE,
        )

        assert existing.chat_settings_id == "cs-uuid-1"

    def test_get_token_for_chat_found(self, token_repo, mock_db):
        """Get token scoped to a specific chat."""
        mock_token = Mock(spec=ApiToken)
        mock_db.query.return_value.filter.return_value.first.return_value = mock_token

        result = token_repo.get_token_for_chat(
            "google_drive", "oauth_access", "chat-uuid-1"
        )

        assert result is mock_token

    def test_get_owner_chat_ids_stringifies_stamped_owners(self, token_repo, mock_db):
        """Distinct stamped owners come back as string UUIDs (#583
        ownership derivation; unstamped rows are excluded in SQL)."""
        cs_a, cs_b = uuid.uuid4(), uuid.uuid4()
        mock_db.query.return_value.filter.return_value.distinct.return_value.all.return_value = [
            (cs_a,),
            (cs_b,),
        ]

        result = token_repo.get_owner_chat_ids("acct-uuid-1")

        assert result == {str(cs_a), str(cs_b)}

    def test_get_token_for_chat_not_found(self, token_repo, mock_db):
        """Returns None when no token exists for chat."""
        mock_db.query.return_value.filter.return_value.first.return_value = None

        result = token_repo.get_token_for_chat(
            "google_drive", "oauth_access", "chat-uuid-1"
        )

        assert result is None

    def test_create_or_update_for_chat_creates_new(self, token_repo, mock_db):
        """Creates a new token when none exists for this chat."""
        # get_token_for_chat returns None
        mock_db.query.return_value.filter.return_value.first.return_value = None

        token_repo.create_or_update_for_chat(
            service_name="google_drive",
            token_type="oauth_access",
            token_value="encrypted_access",
            chat_settings_id="chat-uuid-1",
            expires_at=datetime.utcnow() + timedelta(hours=1),
            scopes=["drive.readonly"],
            metadata={"email": "user@gmail.com"},
        )

        mock_db.add.assert_called_once()
        added = mock_db.add.call_args[0][0]
        assert isinstance(added, ApiToken)
        assert added.service_name == "google_drive"
        assert added.chat_settings_id == "chat-uuid-1"

    def test_create_or_update_for_chat_updates_existing(self, token_repo, mock_db):
        """Updates existing token when one exists for this chat."""
        existing = Mock(spec=ApiToken)
        existing.token_value = "old_encrypted"
        mock_db.query.return_value.filter.return_value.first.return_value = existing

        token_repo.create_or_update_for_chat(
            service_name="google_drive",
            token_type="oauth_access",
            token_value="new_encrypted",
            chat_settings_id="chat-uuid-1",
        )

        mock_db.add.assert_not_called()
        assert existing.token_value == "new_encrypted"
        mock_db.commit.assert_called()

    def test_delete_tokens_for_chat(self, token_repo, mock_db):
        """Delete all tokens for a service scoped to a chat."""
        mock_db.query.return_value.filter.return_value.delete.return_value = 2

        count = token_repo.delete_tokens_for_chat("google_drive", "chat-uuid-1")

        assert count == 2
        mock_db.commit.assert_called()
