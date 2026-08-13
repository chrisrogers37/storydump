"""Tests for MediaSourceFactory."""

import pytest
from unittest.mock import Mock, patch

from src.exceptions.google_drive import GoogleDriveAuthError
from src.services.media_sources.base_provider import MediaSourceProvider
from src.services.media_sources.factory import MediaSourceFactory
from src.services.media_sources.local_provider import LocalMediaProvider


@pytest.mark.unit
class TestMediaSourceFactory:
    """Test suite for MediaSourceFactory."""

    @patch("src.services.media_sources.factory.settings")
    def test_create_local_provider(self, mock_settings):
        """Test creating a local provider with explicit base_path."""
        mock_settings.MEDIA_DIR = "/default/media"

        provider = MediaSourceFactory.create("local", base_path="/custom/path")

        assert isinstance(provider, LocalMediaProvider)
        assert str(provider.base_path) == "/custom/path"

    @patch("src.services.media_sources.factory.settings")
    def test_create_local_provider_default_path(self, mock_settings):
        """Test creating a local provider uses settings.MEDIA_DIR as default."""
        mock_settings.MEDIA_DIR = "/default/media"

        provider = MediaSourceFactory.create("local")

        assert isinstance(provider, LocalMediaProvider)
        assert str(provider.base_path) == "/default/media"

    def test_create_unsupported_type(self):
        """Test creating provider with unsupported type raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported media source type"):
            MediaSourceFactory.create("dropbox")

    def test_create_unsupported_type_lists_supported(self):
        """Test error message lists supported types."""
        with pytest.raises(ValueError, match="local"):
            MediaSourceFactory.create("s3")

    @patch("src.services.media_sources.factory.settings")
    def test_get_provider_for_media_item_local(self, mock_settings):
        """Test getting provider for a local media item."""
        mock_settings.MEDIA_DIR = "/media"
        media_item = Mock(source_type="local")

        provider = MediaSourceFactory.get_provider_for_media_item(media_item)

        assert isinstance(provider, LocalMediaProvider)

    @patch("src.services.media_sources.factory.settings")
    def test_get_provider_for_media_item_none_source_type(self, mock_settings):
        """Test getting provider falls back to local when source_type is None."""
        mock_settings.MEDIA_DIR = "/media"
        media_item = Mock(source_type=None)

        provider = MediaSourceFactory.get_provider_for_media_item(media_item)

        assert isinstance(provider, LocalMediaProvider)

    def test_register_provider(self):
        """Test registering a custom provider type."""

        class FakeProvider(MediaSourceProvider):
            def __init__(self, **kwargs):
                pass

            def list_files(self, folder=None):
                return []

            def download_file(self, file_identifier):
                return b""

            def get_file_info(self, file_identifier):
                return None

            def file_exists(self, file_identifier):
                return False

            def get_folders(self):
                return []

            def is_configured(self):
                return True

            def calculate_file_hash(self, file_identifier):
                return "hash"

        # Register and verify
        MediaSourceFactory.register_provider("fake", FakeProvider)
        try:
            provider = MediaSourceFactory.create("fake")
            assert isinstance(provider, FakeProvider)
        finally:
            # Clean up to avoid affecting other tests
            del MediaSourceFactory._providers["fake"]

    @patch("src.services.media_sources.factory.settings")
    def test_get_provider_for_media_item_empty_string_source_type(self, mock_settings):
        """Test getting provider falls back to local when source_type is empty string."""
        mock_settings.MEDIA_DIR = "/media"
        media_item = Mock(source_type="")

        provider = MediaSourceFactory.get_provider_for_media_item(media_item)

        assert isinstance(provider, LocalMediaProvider)

    def test_get_provider_for_media_item_gdrive_resolves_root_folder(self):
        """For google_drive items, root_folder_id is resolved from chat_settings.

        Regression: previously omitted, which made get_provider_for_chat fail
        with "No root_folder_id configured", the factory's broad except caught
        it, fell back to the service-account path, and surfaced a misleading
        "No Google Drive credentials found" error.
        """
        media_item = Mock(source_type="google_drive")
        chat_settings = Mock(media_source_root="folder-id-abc")

        with (
            patch(
                "src.services.core.settings_service.SettingsService"
            ) as MockSettingsService,
            patch.object(MediaSourceFactory, "create") as mock_create,
        ):
            MockSettingsService.return_value.get_settings_if_exists.return_value = (
                chat_settings
            )

            MediaSourceFactory.get_provider_for_media_item(
                media_item, telegram_chat_id=-100123
            )

        mock_create.assert_called_once_with(
            "google_drive",
            telegram_chat_id=-100123,
            root_folder_id="folder-id-abc",
        )

    def test_get_provider_for_media_item_gdrive_no_chat_settings_omits_root(self):
        """If chat_settings is missing, no root_folder_id is added (fall through to current behavior)."""
        media_item = Mock(source_type="google_drive")

        with (
            patch(
                "src.services.core.settings_service.SettingsService"
            ) as MockSettingsService,
            patch.object(MediaSourceFactory, "create") as mock_create,
        ):
            MockSettingsService.return_value.get_settings_if_exists.return_value = None

            MediaSourceFactory.get_provider_for_media_item(
                media_item, telegram_chat_id=-100123
            )

        mock_create.assert_called_once_with("google_drive", telegram_chat_id=-100123)


@pytest.mark.unit
class TestANamedTenantNeverResolvesToTheServiceAccount:
    """#627, security / multi-tenant.

    A broad `except Exception` around the per-tenant OAuth lookup fell through
    to the deployment-wide service account. Two distinct boundaries crossed:

      - the service account's CREDENTIALS stood in for the tenant's, and
      - because get_provider() reads root_folder_id from the service account's
        own stored metadata when passed none, a tenant with no configured root
        was handed a provider rooted in the OPERATOR's Drive folder, whose
        files would then be indexed under that tenant.

    Both were silent: the auth error that triggered them was downgraded to a
    warning on the way past, so the only visible symptom was a tenant seeing
    media that was never theirs, or a misleading "no credentials" error naming
    an operator CLI command a tenant cannot run.

    Every test here asserts on the negative — that the service account was NOT
    consulted — because the defect is a silent substitution rather than a
    crash. `assert_not_called` on the service-account door is the whole point;
    a test that only checked the return value would pass while the crossing
    happened underneath it.
    """

    @pytest.fixture
    def gdrive_registered(self):
        """Make the 'google_drive' membership check in create() pass.

        create() self-registers the provider itself — but only where
        google-api-python-client imports, since that registration is wrapped
        in `except ImportError`. Where the dependency is absent, create()
        raises ValueError on the unsupported-type branch instead, and every
        test below would go green having never reached the credential
        resolution under test: a suite that proves nothing, failing open.
        Registering here removes that dependence on the environment.

        The provider class is never constructed on this path — the factory
        returns whatever the GoogleDriveService door hands back — so a
        placeholder is enough.
        """
        already = "google_drive" in MediaSourceFactory._providers
        if not already:
            MediaSourceFactory._providers["google_drive"] = Mock()
        yield
        if not already:
            del MediaSourceFactory._providers["google_drive"]

    @pytest.fixture
    def gdrive(self):
        """The GoogleDriveService double, patched where create() imports it."""
        service = Mock()
        with patch(
            "src.services.integrations.google_drive.GoogleDriveService",
            return_value=service,
        ):
            yield service

    def test_a_tenant_oauth_failure_does_not_resolve_to_the_service_account(
        self, gdrive_registered, gdrive
    ):
        """THE REGRESSION. A named tenant whose own OAuth is broken must fail
        as that tenant, not quietly become the operator."""
        gdrive.get_provider_for_chat.side_effect = GoogleDriveAuthError("token revoked")

        with pytest.raises(GoogleDriveAuthError):
            MediaSourceFactory.create(
                "google_drive",
                telegram_chat_id=-100123,
                root_folder_id="tenant-folder",
            )

        gdrive.get_provider.assert_not_called()

    def test_the_tenants_auth_error_is_not_masked_by_the_service_accounts(
        self, gdrive_registered, gdrive
    ):
        """The error that surfaces must be the tenant's own.

        Before, the tenant's error was swallowed and the SERVICE ACCOUNT's
        error surfaced in its place — telling a tenant to run an operator-only
        CLI command for a credential they do not own. Content, not presence:
        an assertion that merely something raised would pass on the masked
        error too.
        """
        gdrive.get_provider_for_chat.side_effect = GoogleDriveAuthError(
            "No Google Drive OAuth credentials found for this chat. "
            "Use /connect_drive to connect your Google Drive."
        )
        gdrive.get_provider.side_effect = GoogleDriveAuthError(
            "No Google Drive credentials found. "
            "Run 'storydump-cli connect-google-drive' first."
        )

        with pytest.raises(GoogleDriveAuthError) as exc:
            MediaSourceFactory.create(
                "google_drive",
                telegram_chat_id=-100123,
                root_folder_id="tenant-folder",
            )

        assert "/connect_drive" in str(exc.value), (
            f"the tenant's own error did not surface: {exc.value}"
        )
        assert "storydump-cli" not in str(exc.value), (
            f"the service account's error masked the tenant's: {exc.value}"
        )

    def test_a_non_auth_failure_also_does_not_resolve_to_the_service_account(
        self, gdrive_registered, gdrive
    ):
        """The `except Exception` half specifically.

        Narrowing only the auth case would leave a database outage, a
        decryption failure or an outright bug still resolving as the operator.
        Anything that is not this tenant's working credential must fail closed.
        """
        gdrive.get_provider_for_chat.side_effect = RuntimeError(
            "database connection lost"
        )

        with pytest.raises(RuntimeError):
            MediaSourceFactory.create(
                "google_drive",
                telegram_chat_id=-100123,
                root_folder_id="tenant-folder",
            )

        gdrive.get_provider.assert_not_called()

    def test_a_tenant_without_a_configured_root_is_not_given_the_operators(
        self, gdrive_registered, gdrive
    ):
        """The sharpest crossing, and a separate one from the credentials.

        With no root_folder_id the tenant lookup raises, and the old fallback
        called get_provider(None) — which resolves the root from the SERVICE
        ACCOUNT's stored metadata. The tenant then indexed the operator's own
        Drive folder as its media. Deleting the credential assertions above
        would not catch this, so it is asserted on its own.
        """
        gdrive.get_provider_for_chat.side_effect = GoogleDriveAuthError(
            "No root_folder_id configured for Google Drive media source."
        )

        with pytest.raises(GoogleDriveAuthError):
            MediaSourceFactory.create("google_drive", telegram_chat_id=-100123)

        gdrive.get_provider.assert_not_called()

    def test_a_named_tenant_with_working_oauth_gets_its_own_provider(
        self, gdrive_registered, gdrive
    ):
        """Control. Without it, 'the service account is never called' could be
        satisfied by a factory that never resolves anything at all."""
        tenant_provider = Mock(name="tenant_provider")
        gdrive.get_provider_for_chat.return_value = tenant_provider

        result = MediaSourceFactory.create(
            "google_drive",
            telegram_chat_id=-100123,
            root_folder_id="tenant-folder",
        )

        assert result is tenant_provider
        gdrive.get_provider_for_chat.assert_called_once_with(-100123, "tenant-folder")
        gdrive.get_provider.assert_not_called()

    def test_an_unnamed_caller_still_uses_the_service_account(
        self, gdrive_registered, gdrive
    ):
        """The positive control on the door the other tests assert is shut.

        No tenant is claimed, so the deployment's service account is the only
        identity in play and crosses nothing. This is what proves the negative
        assertions above are green because the fallback is CLOSED to tenants,
        not because the path is unreachable in this fixture.
        """
        global_provider = Mock(name="global_provider")
        gdrive.get_provider.return_value = global_provider

        result = MediaSourceFactory.create(
            "google_drive", root_folder_id="operator-folder"
        )

        assert result is global_provider
        gdrive.get_provider.assert_called_once_with("operator-folder")
        gdrive.get_provider_for_chat.assert_not_called()
