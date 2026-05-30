"""Tests for security hardening: headers, thumbnail MIME, error sanitization, rate limits."""

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock

from tests.src.api.conftest import CHAT_ID, mock_validate, service_ctx


# =============================================================================
# Security headers (#382)
# =============================================================================


@pytest.mark.unit
class TestSecurityHeaders:
    """Verify security headers are set on every response."""

    def test_health_endpoint_has_security_headers(self, client):
        resp = client.get("/health")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "max-age=" in resp.headers["Strict-Transport-Security"]
        assert "default-src" in resp.headers["Content-Security-Policy"]
        assert "Referrer-Policy" in resp.headers

    def test_csp_blocks_frames(self, client):
        resp = client.get("/health")
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]

    def test_hsts_includes_subdomains(self, client):
        resp = client.get("/health")
        assert "includeSubDomains" in resp.headers["Strict-Transport-Security"]

    def test_mini_app_paths_allow_telegram_frames(self, client):
        """Telegram Mini App paths must allow iframe embedding by Telegram."""
        for path in [
            f"/api/onboarding/init?init_data=fake&chat_id={CHAT_ID}",
            "/webapp/onboarding",
        ]:
            resp = client.get(path)
            csp = resp.headers.get("Content-Security-Policy", "")
            assert "frame-ancestors 'none'" not in csp, f"{path} still blocks frames"
            assert "web.telegram.org" in csp, f"{path} missing Telegram frame-ancestors"
            assert "X-Frame-Options" not in resp.headers, (
                f"{path} still has X-Frame-Options"
            )

    def test_mini_app_paths_allow_telegram_sdk_script(self, client):
        """Mini App CSP must allow the Telegram WebApp SDK from telegram.org."""
        resp = client.get("/webapp/onboarding")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "script-src" in csp, "Missing script-src directive"
        assert "https://telegram.org" in csp, (
            "CSP must allow https://telegram.org for WebApp SDK"
        )

    def test_non_mini_app_paths_block_frames(self, client):
        """Non-Mini App paths must keep strict X-Frame-Options: DENY."""
        resp = client.get("/health")
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


# =============================================================================
# Thumbnail proxy SVG block (#383)
# =============================================================================


@pytest.mark.unit
class TestThumbnailSvgBlock:
    """Verify SVG content type is rejected by thumbnail proxy."""

    def _mock_upstream(self, content_type="image/jpeg", status_code=200):
        """Create a mock httpx response."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {"content-type": content_type}
        resp.content = b"fake-image-bytes"
        return resp

    @patch("src.api.routes.onboarding.dashboard.MediaRepository")
    @patch("src.api.routes.onboarding.dashboard.SettingsService")
    def test_svg_content_type_rejected(self, mock_settings_cls, mock_media_cls, client):
        """image/svg+xml from upstream should be rejected as 502."""
        mock_settings_svc = service_ctx(mock_settings_cls)
        mock_settings_svc.get_settings.return_value = Mock(id="cs-1")

        mock_media_repo = service_ctx(mock_media_cls)
        mock_item = Mock(thumbnail_url="https://lh3.example.com/thumb.svg")
        mock_media_repo.get_by_id.return_value = mock_item

        mock_resp = self._mock_upstream(content_type="image/svg+xml")

        with mock_validate(
            {"user_id": 12345, "first_name": "Chris", "chat_id": CHAT_ID}
        ):
            with patch(
                "src.api.routes.onboarding.dashboard.httpx.AsyncClient"
            ) as mock_httpx:
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_httpx.return_value = mock_client

                resp = client.get(
                    f"/api/onboarding/media/m1/thumbnail?init_data=fake&chat_id={CHAT_ID}"
                )

        assert resp.status_code == 502

    @patch("src.api.routes.onboarding.dashboard.MediaRepository")
    @patch("src.api.routes.onboarding.dashboard.SettingsService")
    def test_jpeg_content_type_allowed(self, mock_settings_cls, mock_media_cls, client):
        """image/jpeg from upstream should pass through."""
        mock_settings_svc = service_ctx(mock_settings_cls)
        mock_settings_svc.get_settings.return_value = Mock(id="cs-1")

        mock_media_repo = service_ctx(mock_media_cls)
        mock_item = Mock(thumbnail_url="https://lh3.example.com/thumb.jpg")
        mock_media_repo.get_by_id.return_value = mock_item

        mock_resp = self._mock_upstream(content_type="image/jpeg")

        with mock_validate(
            {"user_id": 12345, "first_name": "Chris", "chat_id": CHAT_ID}
        ):
            with patch(
                "src.api.routes.onboarding.dashboard.httpx.AsyncClient"
            ) as mock_httpx:
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_httpx.return_value = mock_client

                resp = client.get(
                    f"/api/onboarding/media/m1/thumbnail?init_data=fake&chat_id={CHAT_ID}"
                )

        assert resp.status_code == 200


# =============================================================================
# Error sanitization (#384)
# =============================================================================


@pytest.mark.unit
class TestAddAccountErrorSanitization:
    """Verify Instagram API errors don't leak internal details."""

    @patch("src.api.routes.onboarding.settings.InstagramAccountService")
    def test_oauth_error_returns_generic_message(self, mock_acct_cls, client):
        """Raw Instagram API error messages must not appear in response."""
        mock_ig_response = Mock()
        mock_ig_response.status_code = 400
        mock_ig_response.json.return_value = {
            "error": {
                "message": "Invalid OAuth 2.0 Access Token - token=EAABsb...",
                "type": "OAuthException",
                "code": 190,
            }
        }

        with mock_validate(
            {"user_id": 12345, "first_name": "Chris", "chat_id": CHAT_ID}
        ):
            with patch(
                "src.api.routes.onboarding.settings.httpx.AsyncClient"
            ) as mock_httpx:
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_ig_response
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_httpx.return_value = mock_client

                resp = client.post(
                    "/api/onboarding/add-account",
                    json={
                        "init_data": "fake",
                        "chat_id": CHAT_ID,
                        "display_name": "Test",
                        "instagram_account_id": "12345",
                        "access_token": "EAABsb_fake_token",
                    },
                )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        # Must NOT contain the raw token fragment or "OAuthException"
        assert "EAABsb" not in detail
        assert "OAuthException" not in detail
        assert "Invalid credentials" in detail

    @patch("src.api.routes.onboarding.settings.InstagramAccountService")
    def test_not_found_error_returns_safe_message(self, mock_acct_cls, client):
        """Account-not-found errors get a specific safe message."""
        mock_ig_response = Mock()
        mock_ig_response.status_code = 400
        mock_ig_response.json.return_value = {
            "error": {
                "message": "Unsupported get request. Object with ID '999' does not exist",
                "type": "GraphMethodException",
                "code": 100,
            }
        }

        with mock_validate(
            {"user_id": 12345, "first_name": "Chris", "chat_id": CHAT_ID}
        ):
            with patch(
                "src.api.routes.onboarding.settings.httpx.AsyncClient"
            ) as mock_httpx:
                mock_client = AsyncMock()
                mock_client.get.return_value = mock_ig_response
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_httpx.return_value = mock_client

                resp = client.post(
                    "/api/onboarding/add-account",
                    json={
                        "init_data": "fake",
                        "chat_id": CHAT_ID,
                        "display_name": "Test",
                        "instagram_account_id": "999",
                        "access_token": "fake_token",
                    },
                )

        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"].lower()


# =============================================================================
# Startup validation (#385)
# =============================================================================


@pytest.mark.unit
class TestStartupSecretValidation:
    """Verify startup validation catches missing encryption keys."""

    def _run_validation(self, **overrides):
        """Run ConfigValidator.validate_all with mocked settings."""
        from src.utils.validators import ConfigValidator

        defaults = {
            "TELEGRAM_BOT_TOKEN": "test_token",
            "TELEGRAM_CHANNEL_ID": -1001234567890,
            "ADMIN_TELEGRAM_CHAT_ID": -1001234567890,
            "DB_NAME": "testdb",
            "ENCRYPTION_KEY": "some-key",
            "ENCRYPTION_KEYS": None,
            "DATABASE_URL": None,
            "DB_PASSWORD": "pass",
            "MEDIA_DIR": "/tmp/test-media",
        }
        defaults.update(overrides)

        with (
            patch("src.utils.validators.settings") as mock_settings,
            patch.object(ConfigValidator, "_check_telegram_token", return_value=None),
        ):
            for k, v in defaults.items():
                setattr(mock_settings, k, v)
            return ConfigValidator.validate_all()

    def test_missing_encryption_keys_fails(self):
        is_valid, errors = self._run_validation(
            ENCRYPTION_KEY=None, ENCRYPTION_KEYS=None
        )
        assert not is_valid
        assert any("ENCRYPTION_KEY" in e for e in errors)

    def test_encryption_key_set_passes(self):
        is_valid, errors = self._run_validation(ENCRYPTION_KEY="some-key")
        assert is_valid

    def test_encryption_keys_plural_set_passes(self):
        is_valid, errors = self._run_validation(
            ENCRYPTION_KEY=None, ENCRYPTION_KEYS="key1,key2"
        )
        assert is_valid
