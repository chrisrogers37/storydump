"""Tests for credential refactor Phase 2 — dual-write to meta_account_id."""

from contextlib import contextmanager
from unittest.mock import Mock, MagicMock, patch

import pytest

from src.models.api_token import ApiToken
from src.repositories.token_repository import TokenRepository


@pytest.mark.unit
class TestTokenRepoMetaAccountId:
    """TokenRepository.create_or_update persists meta_account_id."""

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

    def test_create_sets_meta_account_id(self, token_repo, mock_db):
        """New token gets meta_account_id when provided."""
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="encrypted",
            instagram_account_id="uuid-1",
            meta_account_id="17841400123456789",
        )

        added = mock_db.add.call_args[0][0]
        assert isinstance(added, ApiToken)
        assert added.meta_account_id == "17841400123456789"

    def test_create_without_meta_account_id(self, token_repo, mock_db):
        """Omitting meta_account_id leaves it None (backward compat)."""
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = None

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="encrypted",
        )

        added = mock_db.add.call_args[0][0]
        assert added.meta_account_id is None

    def test_update_sets_meta_account_id(self, token_repo, mock_db):
        """Updating an existing token populates meta_account_id."""
        existing = Mock(spec=ApiToken)
        existing.meta_account_id = None
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = existing

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="new_encrypted",
            meta_account_id="17841400123456789",
        )

        assert existing.meta_account_id == "17841400123456789"

    def test_update_preserves_meta_when_not_passed(self, token_repo, mock_db):
        """Refresh path (no meta_account_id kwarg) doesn't clobber existing."""
        existing = Mock(spec=ApiToken)
        existing.meta_account_id = "17841400123456789"
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = existing

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="refreshed_encrypted",
            # meta_account_id intentionally omitted
        )

        # Should preserve the existing value, not overwrite with None
        assert existing.meta_account_id == "17841400123456789"

    def test_update_overwrites_meta_when_passed(self, token_repo, mock_db):
        """Re-auth with a different Meta ID updates the value."""
        existing = Mock(spec=ApiToken)
        existing.meta_account_id = "old_id_111"
        mock_db.query.return_value.filter.return_value.filter.return_value.first.return_value = existing

        token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value="new_encrypted",
            meta_account_id="new_id_222",
        )

        assert existing.meta_account_id == "new_id_222"


@pytest.mark.unit
class TestAccountServiceDualWrite:
    """InstagramAccountService writes meta_account_id alongside old fields."""

    @pytest.fixture
    def account_service(self):
        with (
            patch(
                "src.services.core.instagram_account_service.InstagramAccountRepository"
            ) as mock_repo_cls,
            patch(
                "src.services.core.instagram_account_service.TokenRepository"
            ) as mock_token_cls,
            patch("src.services.core.instagram_account_service.ChatSettingsRepository"),
            patch(
                "src.services.core.instagram_account_service.TokenEncryption"
            ) as mock_enc_cls,
        ):
            from src.services.core.instagram_account_service import (
                InstagramAccountService,
            )

            svc = InstagramAccountService()

            svc.account_repo = mock_repo_cls.return_value
            svc.token_repo = mock_token_cls.return_value
            svc.encryption = mock_enc_cls.return_value
            svc.encryption.encrypt.return_value = "encrypted_token_value"

            # Stub track_execution so it doesn't hit the DB
            @contextmanager
            def _fake_track(*_a, **_kw):
                yield "fake-run-id"

            svc.track_execution = _fake_track
            svc.set_result_summary = Mock()

            yield svc

    def test_create_account_writes_meta_account_id(self, account_service):
        """_create_account_with_token passes meta_account_id."""
        mock_account = Mock()
        mock_account.id = "uuid-1"
        account_service.account_repo.create.return_value = mock_account

        account_service._create_account_with_token(
            display_name="Test Account",
            instagram_account_id="17841400123456789",
            instagram_username="testuser",
            access_token="raw_token",
        )

        call_kwargs = account_service.token_repo.create_or_update.call_args
        assert call_kwargs.kwargs.get("meta_account_id") == "17841400123456789"
        # Old location still written (dual-write)
        assert call_kwargs.kwargs["metadata"]["account_id"] == "17841400123456789"

    def test_update_account_token_writes_meta_account_id(self, account_service):
        """update_account_token passes meta_account_id on re-auth."""
        mock_account = Mock()
        mock_account.id = "uuid-1"
        mock_account.instagram_username = "testuser"
        mock_account.is_active = True
        mock_account.auth_method = "oauth"
        account_service.account_repo.get_by_instagram_id.return_value = mock_account

        account_service.update_account_token(
            instagram_account_id="17841400123456789",
            access_token="new_raw_token",
        )

        call_kwargs = account_service.token_repo.create_or_update.call_args
        assert call_kwargs.kwargs.get("meta_account_id") == "17841400123456789"

    def test_dual_write_both_locations(self, account_service):
        """Both old (metadata.account_id) and new (meta_account_id) are set."""
        mock_account = Mock()
        mock_account.id = "uuid-1"
        account_service.account_repo.create.return_value = mock_account

        account_service._create_account_with_token(
            display_name="GT",
            instagram_account_id="BIZ_ACCT_ID_999",
            instagram_username="gatortails",
            access_token="token",
        )

        call_kwargs = account_service.token_repo.create_or_update.call_args
        # New column
        assert call_kwargs.kwargs["meta_account_id"] == "BIZ_ACCT_ID_999"
        # Old JSONB location preserved for backcompat reads
        assert call_kwargs.kwargs["metadata"]["account_id"] == "BIZ_ACCT_ID_999"
        # Old account table column still written via account_repo.create
        account_service.account_repo.create.assert_called_once()
        create_kwargs = account_service.account_repo.create.call_args.kwargs
        assert create_kwargs["instagram_account_id"] == "BIZ_ACCT_ID_999"
