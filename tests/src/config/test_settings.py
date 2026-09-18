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
        assert Settings.model_fields["ENCRYPTION_KEY"].default is None


@pytest.mark.unit
class TestTheHarnessDatabaseUrl:
    """`test_database_url` — the suite's door to its own database, and the one
    reader `DB_SSLMODE` and `TEST_DB_NAME` have. (The `database_url` property
    and the `DATABASE_URL` field went in the tear-out's phase 02: no deployed
    root built a URL from settings, and nothing called the property.)"""

    def _make_settings(self, **overrides):
        defaults = {
            "DB_USER": "storydump_user",
            "DB_NAME": "storydump",
            "TEST_DB_NAME": "storydump_test",
        }
        defaults.update(overrides)
        return Settings(_env_file=None, **defaults)

    def test_it_names_the_test_database_never_the_application_one(self):
        url = self._make_settings(DB_PASSWORD="secret").test_database_url
        assert url.startswith("postgresql://")
        assert url.endswith("/storydump_test")

    def test_with_a_password(self):
        url = self._make_settings(DB_PASSWORD="secret123").test_database_url
        assert "storydump_user:secret123@" in url

    def test_without_a_password(self):
        url = self._make_settings(DB_PASSWORD="").test_database_url
        assert ":@" not in url
        assert "storydump_user@" in url

    def test_it_includes_host_and_port(self):
        url = self._make_settings(
            DB_HOST="myhost", DB_PORT=5433, DB_PASSWORD="pw"
        ).test_database_url
        assert "@myhost:5433/" in url

    def test_sslmode_is_appended_when_set(self):
        s = self._make_settings(DB_PASSWORD="pw", DB_SSLMODE="require")
        assert s.test_database_url.endswith("?sslmode=require")

    def test_no_sslmode_by_default(self):
        assert "sslmode" not in self._make_settings(DB_PASSWORD="pw").test_database_url
