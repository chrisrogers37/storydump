"""Tests for credential refactor Phase 3 — backfill + credential-keyed reads."""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import pytest

from src.models.instagram_account import InstagramAccount

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "scripts" / "migrations"


# ---------------------------------------------------------------------------
# Migration 036
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMigration036:
    """Verify backfill migration file structure."""

    def test_migration_file_exists(self):
        path = MIGRATIONS_DIR / "036_credential_refactor_backfill_meta_account_id.sql"
        assert path.exists()

    def test_migration_updates_from_instagram_accounts(self):
        sql = (
            MIGRATIONS_DIR / "036_credential_refactor_backfill_meta_account_id.sql"
        ).read_text()
        assert "UPDATE api_tokens" in sql
        assert "FROM instagram_accounts" in sql
        assert "meta_account_id IS NULL" in sql

    def test_migration_uses_transaction(self):
        sql = (
            MIGRATIONS_DIR / "036_credential_refactor_backfill_meta_account_id.sql"
        ).read_text()
        assert "BEGIN;" in sql
        assert "COMMIT;" in sql

    def test_migration_records_schema_version(self):
        sql = (
            MIGRATIONS_DIR / "036_credential_refactor_backfill_meta_account_id.sql"
        ).read_text()
        assert "INSERT INTO schema_version" in sql
        assert "36" in sql


# ---------------------------------------------------------------------------
# Repository: get_by_meta_account_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRepoGetByMetaAccountId:
    """InstagramAccountRepository.get_by_meta_account_id joins via api_tokens."""

    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def repo(self, mock_db):
        from src.repositories.instagram_account_repository import (
            InstagramAccountRepository,
        )

        with patch("src.repositories.base_repository.get_db") as mock_get_db:
            mock_get_db.return_value = iter([mock_db])
            r = InstagramAccountRepository()
            r._db = mock_db
            return r

    def test_returns_account_when_found(self, repo, mock_db):
        mock_account = Mock(spec=InstagramAccount)
        mock_db.query.return_value.join.return_value.filter.return_value.first.return_value = mock_account

        result = repo.get_by_meta_account_id("17841400123456789")

        assert result is mock_account

    def test_returns_none_when_not_found(self, repo, mock_db):
        mock_db.query.return_value.join.return_value.filter.return_value.first.return_value = None

        result = repo.get_by_meta_account_id("nonexistent")

        assert result is None


# ---------------------------------------------------------------------------
# Service: get_account_by_meta_id (with legacy fallback)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServiceGetAccountByMetaId:
    """get_account_by_meta_id tries credential-keyed, then legacy."""

    @pytest.fixture
    def account_service(self):
        with (
            patch(
                "src.services.core.instagram_account_service.InstagramAccountRepository"
            ) as mock_repo_cls,
            patch("src.services.core.instagram_account_service.TokenRepository"),
            patch("src.services.core.instagram_account_service.ChatSettingsRepository"),
            patch("src.services.core.instagram_account_service.TokenEncryption"),
        ):
            from src.services.core.instagram_account_service import (
                InstagramAccountService,
            )

            svc = InstagramAccountService()
            svc.account_repo = mock_repo_cls.return_value
            yield svc

    def test_returns_from_meta_lookup(self, account_service):
        mock_account = Mock(spec=InstagramAccount)
        account_service.account_repo.get_by_meta_account_id.return_value = mock_account

        result = account_service.get_account_by_meta_id("BIZ_ACCT_ID")

        assert result is mock_account
        account_service.account_repo.get_by_instagram_id.assert_not_called()

    def test_falls_back_to_legacy(self, account_service):
        mock_account = Mock(spec=InstagramAccount)
        account_service.account_repo.get_by_meta_account_id.return_value = None
        account_service.account_repo.get_by_instagram_id.return_value = mock_account

        result = account_service.get_account_by_meta_id("BIZ_ACCT_ID")

        assert result is mock_account
        account_service.account_repo.get_by_instagram_id.assert_called_once_with(
            "BIZ_ACCT_ID"
        )

    def test_returns_none_when_both_miss(self, account_service):
        account_service.account_repo.get_by_meta_account_id.return_value = None
        account_service.account_repo.get_by_instagram_id.return_value = None

        result = account_service.get_account_by_meta_id("NONEXISTENT")

        assert result is None


# ---------------------------------------------------------------------------
# Service: _validate_new_account uses credential-keyed lookup
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateNewAccountPhase3:
    """_validate_new_account checks meta_account_id before legacy column."""

    @pytest.fixture
    def account_service(self):
        with (
            patch(
                "src.services.core.instagram_account_service.InstagramAccountRepository"
            ) as mock_repo_cls,
            patch("src.services.core.instagram_account_service.TokenRepository"),
            patch("src.services.core.instagram_account_service.ChatSettingsRepository"),
            patch("src.services.core.instagram_account_service.TokenEncryption"),
        ):
            from src.services.core.instagram_account_service import (
                InstagramAccountService,
            )

            svc = InstagramAccountService()
            svc.account_repo = mock_repo_cls.return_value
            yield svc

    def test_rejects_duplicate_via_meta_lookup(self, account_service):
        existing = Mock(spec=InstagramAccount, display_name="GT")
        account_service.account_repo.get_by_meta_account_id.return_value = existing

        with pytest.raises(ValueError, match="already exists"):
            account_service._validate_new_account("BIZ_ID", "gatortails")

    def test_rejects_duplicate_via_legacy_fallback(self, account_service):
        account_service.account_repo.get_by_meta_account_id.return_value = None
        existing = Mock(spec=InstagramAccount, display_name="GT")
        account_service.account_repo.get_by_instagram_id.return_value = existing

        with pytest.raises(ValueError, match="already exists"):
            account_service._validate_new_account("BIZ_ID", "gatortails")

    def test_passes_when_no_duplicate(self, account_service):
        account_service.account_repo.get_by_meta_account_id.return_value = None
        account_service.account_repo.get_by_instagram_id.return_value = None
        account_service.account_repo.get_by_username.return_value = None

        # Should not raise
        account_service._validate_new_account("NEW_ID", "newuser")


# ---------------------------------------------------------------------------
# Service: update_account_token uses credential-keyed lookup
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUpdateAccountTokenPhase3:
    """update_account_token resolves via meta_account_id first."""

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
            svc.encryption.encrypt.return_value = "encrypted"

            @contextmanager
            def _fake_track(*_a, **_kw):
                yield "fake-run-id"

            svc.track_execution = _fake_track
            svc.set_result_summary = Mock()

            yield svc

    def test_finds_via_meta_lookup(self, account_service):
        mock_account = Mock()
        mock_account.id = "uuid-1"
        mock_account.instagram_username = "testuser"
        mock_account.is_active = True
        mock_account.auth_method = "oauth"
        account_service.account_repo.get_by_meta_account_id.return_value = mock_account

        account_service.update_account_token(
            instagram_account_id="BIZ_ID", access_token="token"
        )

        account_service.account_repo.get_by_instagram_id.assert_not_called()

    def test_falls_back_to_legacy(self, account_service):
        mock_account = Mock()
        mock_account.id = "uuid-1"
        mock_account.instagram_username = "testuser"
        mock_account.is_active = True
        mock_account.auth_method = "oauth"
        account_service.account_repo.get_by_meta_account_id.return_value = None
        account_service.account_repo.get_by_instagram_id.return_value = mock_account

        account_service.update_account_token(
            instagram_account_id="BIZ_ID", access_token="token"
        )

        account_service.account_repo.get_by_instagram_id.assert_called_once()

    def test_raises_when_not_found(self, account_service):
        account_service.account_repo.get_by_meta_account_id.return_value = None
        account_service.account_repo.get_by_instagram_id.return_value = None

        with pytest.raises(ValueError, match="not found"):
            account_service.update_account_token(
                instagram_account_id="GONE", access_token="token"
            )


# ---------------------------------------------------------------------------
# OAuth callsites switched to get_account_by_meta_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthCallsitesUseMetaId:
    """Verify the three OAuth services call get_account_by_meta_id."""

    def test_oauth_service_uses_meta_id(self):
        import inspect
        from src.services.core.oauth_service import OAuthService

        source = inspect.getsource(OAuthService.exchange_and_store)
        assert "get_account_by_meta_id" in source
        assert "get_account_by_instagram_id" not in source

    def test_instagram_login_uses_meta_id(self):
        """IG Login resolves accounts via the credential-keyed helper
        (find_existing_account_for_oauth), which internally checks
        api_tokens.meta_account_id first. The deprecated direct
        get_account_by_instagram_id is not used at the callsite.

        The helper also accepts a ``username`` arg used for cross-flow
        recovery on legacy rows whose backfilled meta_account_id doesn't
        match the live IG Login user_id — guarded by
        ``test_cross_flow_username_recovery_present_in_exchange_and_store``
        below.
        """
        import inspect
        from src.services.integrations.instagram_login_oauth import (
            InstagramLoginOAuthService,
        )

        source = inspect.getsource(InstagramLoginOAuthService.exchange_and_store)
        assert "find_existing_account_for_oauth" in source
        assert "get_account_by_instagram_id" not in source



# ---------------------------------------------------------------------------
# Cross-flow username recovery (re-introduced after the 2026-05-25 incident)
# ---------------------------------------------------------------------------
#
# The credential refactor assumed migration 036's backfill would make a
# username fallback unnecessary, on the theory that
# api_tokens.meta_account_id (copied from instagram_accounts.instagram_account_id)
# would match what IG Login returns as user_id. For legacy FB-Login-era rows
# this assumption is false — see 00_INVESTIGATION.md in the
# documentation/planning/investigations/ig-oauth-cross-flow-reconnect_2026-05-25/
# directory for the prod stack traces. The username branch is now carried by
# find_existing_account_for_oauth as the third lookup tier (after
# meta_account_id and the legacy column). It self-heals on first reconnect.


@pytest.mark.unit
class TestCrossFlowUsernameRecovery:
    """Username branch of find_existing_account_for_oauth is reachable from
    IG Login and is the recovery path for legacy rows whose backfilled
    meta_account_id doesn't match the live IG Login user_id."""

    def test_exchange_and_store_passes_username_to_helper(self):
        """IG Login passes both meta_account_id and username to the helper
        so the cross-flow recovery branch is available when needed."""
        import inspect
        from src.services.integrations.instagram_login_oauth import (
            InstagramLoginOAuthService,
        )

        source = inspect.getsource(InstagramLoginOAuthService.exchange_and_store)
        assert "find_existing_account_for_oauth" in source
        assert "meta_account_id=ig_user_id" in source
        assert "username=username" in source
