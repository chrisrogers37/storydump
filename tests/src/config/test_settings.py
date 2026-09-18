"""Tests for Settings configuration model."""

import pytest

from src.config.settings import Settings


@pytest.mark.unit
class TestSettingsDefaults:
    """Tests for Settings default values via model field definitions."""

    def test_db_host_defaults_to_localhost(self):
        assert Settings.model_fields["DB_HOST"].default == "localhost"

    def test_db_port_defaults_to_5432(self):
        assert Settings.model_fields["DB_PORT"].default == 5432

    def test_db_name_defaults(self):
        assert Settings.model_fields["DB_NAME"].default == "storydump"

    def test_log_level_defaults_to_info(self):
        assert Settings.model_fields["LOG_LEVEL"].default == "INFO"

    def test_tap_admission_defaults_to_120_per_minute(self):
        assert Settings.model_fields["TARGET_TAP_ADMISSION_PER_MINUTE"].default == 120

    def test_the_session_cookie_is_secure_by_default(self):
        assert Settings.model_fields["SESSION_COOKIE_SECURE"].default is True

    def test_optional_fields_default_to_none(self):
        assert (
            Settings.model_fields["TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN"].default
            is None
        )
        assert Settings.model_fields["TARGET_TELEGRAM_BOT_USERNAME"].default is None
        assert Settings.model_fields["FACEBOOK_APP_SECRET"].default is None
        assert Settings.model_fields["CLOUDINARY_CLOUD_NAME"].default is None
        assert Settings.model_fields["CLOUDINARY_API_KEY"].default is None
        assert Settings.model_fields["CLOUDINARY_API_SECRET"].default is None
        assert Settings.model_fields["ENCRYPTION_KEY"].default is None


@pytest.mark.unit
class TestSettingsDatabaseUrl:
    """Tests for the database_url computed property."""

    def _make_settings(self, **overrides):
        defaults = {
            "DB_USER": "storydump_user",
            "DB_NAME": "storydump",
            "TEST_DB_NAME": "storydump_test",
        }
        defaults.update(overrides)
        return Settings(_env_file=None, **defaults)

    def test_database_url_with_password(self):
        s = self._make_settings(DB_PASSWORD="secret123")
        url = s.database_url
        assert "secret123" in url
        assert url.startswith("postgresql://")
        assert "storydump_user" in url
        assert "storydump" in url

    def test_database_url_without_password(self):
        s = self._make_settings(DB_PASSWORD="")
        url = s.database_url
        assert ":@" not in url
        assert "storydump_user@" in url

    def test_test_database_url_uses_test_db_name(self):
        s = self._make_settings(DB_PASSWORD="secret")
        url = s.test_database_url
        assert "storydump_test" in url
        assert url != s.database_url

    def test_database_url_includes_host_and_port(self):
        s = self._make_settings(DB_HOST="myhost", DB_PORT=5433, DB_PASSWORD="pw")
        url = s.database_url
        assert "myhost" in url
        assert "5433" in url

    def test_database_url_full_url_overrides_components(self):
        """DATABASE_URL takes precedence over DB_* components."""
        s = self._make_settings(
            DATABASE_URL="postgresql://neon:pass@ep-cool.neon.tech/mydb?sslmode=require",
            DB_HOST="localhost",
            DB_PASSWORD="localpass",
        )
        assert (
            s.database_url
            == "postgresql://neon:pass@ep-cool.neon.tech/mydb?sslmode=require"
        )

    def test_database_url_sslmode_appended(self):
        """DB_SSLMODE appends ?sslmode= to assembled URL."""
        s = self._make_settings(DB_PASSWORD="pw", DB_SSLMODE="require")
        assert s.database_url.endswith("?sslmode=require")

    def test_database_url_no_sslmode_by_default(self):
        """No sslmode appended when DB_SSLMODE is not set."""
        s = self._make_settings(DB_PASSWORD="pw")
        assert "sslmode" not in s.database_url

    def test_test_database_url_sslmode_appended(self):
        """DB_SSLMODE also applies to test_database_url."""
        s = self._make_settings(DB_PASSWORD="pw", DB_SSLMODE="require")
        assert s.test_database_url.endswith("?sslmode=require")

    def test_pool_size_defaults(self):
        """Pool size and max overflow have sensible defaults."""
        assert Settings.model_fields["DB_POOL_SIZE"].default == 10
        assert Settings.model_fields["DB_MAX_OVERFLOW"].default == 20

    def test_pool_size_configurable(self):
        """Pool settings can be overridden (for Neon free tier)."""
        s = self._make_settings(DB_POOL_SIZE=3, DB_MAX_OVERFLOW=2)
        assert s.DB_POOL_SIZE == 3
        assert s.DB_MAX_OVERFLOW == 2
