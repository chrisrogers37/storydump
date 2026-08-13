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


class TestForwardedForAttribution:
    """Who the app believes the client is, when the caller writes the header (#726).

    Every IP-keyed control in this app reads one value: `request.client.host`,
    as rewritten by uvicorn's ProxyHeadersMiddleware. The limiter keys on it
    (`src/api/rate_limit.py`), and `auth_monitor` buckets failures on it
    (`src/api/routes/onboarding/helpers.py:_client_ip`). If a caller can choose
    that value, they get a fresh bucket per request and both controls stop
    accumulating — silently, which is the part worth testing.

    The topology these tests model, which is the real one:

        attacker box (public IP) -> edge (private IP, our TCP peer) -> app

    A standards-compliant proxy APPENDS the address it observed, so the app
    receives "<whatever the caller sent>, <caller's real address>". Every entry
    except the last is caller-controlled. Under `trusted_hosts=["*"]` uvicorn
    returns the FIRST entry; under a named set it walks from the right and
    returns the first untrusted one.
    """

    EDGE = "10.0.0.5"  # our TCP peer: the platform edge
    CALLER = "192.0.2.50"  # the caller's real address, appended by the edge
    FORGED = "198.51.100.7"  # what the caller writes into the header

    @staticmethod
    def _attributed(trusted_hosts, xff, peer):
        """Run the real middleware; return the client host it attributes."""
        import asyncio

        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        seen = {}

        async def app(scope, receive, send):
            seen["host"] = scope["client"][0] if scope.get("client") else None

        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        middleware = ProxyHeadersMiddleware(app, trusted_hosts=trusted_hosts)
        asyncio.run(
            middleware(
                {"type": "http", "client": (peer, 54321), "headers": headers},
                None,
                None,
            )
        )
        return seen["host"]

    def _configured(self):
        from src.config.settings import settings

        return settings.trusted_proxy_hosts

    def test_the_wildcard_is_not_configured(self):
        """The wildcard is the defect itself, not merely a loose setting.

        Asserted on the parsed value rather than on the literal string, so it
        also catches "*" arriving via the environment.
        """
        assert "*" not in self._configured(), (
            "TRUSTED_PROXY_HOSTS contains the wildcard. uvicorn then honours "
            "X-Forwarded-For from any peer and takes its leftmost entry, which "
            "the caller writes — see #726."
        )

    def test_a_forged_leading_entry_does_not_win_behind_the_edge(self):
        got = self._attributed(
            self._configured(), f"{self.FORGED}, {self.CALLER}", peer=self.EDGE
        )
        assert got == self.CALLER, (
            f"attributed to {got!r}; the caller forged {self.FORGED!r} and the "
            f"edge appended the truth {self.CALLER!r}"
        )

    def test_a_forged_private_hop_does_not_launder_the_claim(self):
        """A caller who pads the chain with a trusted-looking hop still loses.

        The edge appends last, so the rightmost entry is the one it observed
        no matter what precedes it.
        """
        got = self._attributed(
            self._configured(),
            f"{self.FORGED}, 10.0.0.9, {self.CALLER}",
            peer=self.EDGE,
        )
        assert got == self.CALLER

    def test_a_direct_caller_cannot_forge_at_all(self):
        """Reaching the app directly, the caller is not a trusted peer, so the
        header is not read and their real address stands."""
        got = self._attributed(self._configured(), self.FORGED, peer=self.CALLER)
        assert got == self.CALLER

    def test_no_public_address_can_join_the_trusted_set(self):
        """The default is the private ranges precisely because a public client
        can never hold one. Pins that property rather than the literal list."""
        for public in ("192.0.2.50", "198.51.100.7", "203.0.113.9", "8.8.8.8"):
            got = self._attributed(self._configured(), "1.2.3.4", peer=public)
            assert got == public, f"{public} was treated as a trusted proxy"

    def test_rate_limit_buckets_do_not_partition_on_a_forged_value(self):
        """The end-to-end consequence, through the real limiter.

        60 requests against a 30/minute limit, each forging a different address.
        Correct behaviour is that the ceiling still applies.
        """
        from fastapi import FastAPI, Request
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware
        from slowapi.util import get_remote_address
        from starlette.testclient import TestClient
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        limiter = Limiter(
            key_func=get_remote_address,
            default_limits=["30/minute"],
            storage_uri="memory://",
        )
        api = FastAPI()
        api.state.limiter = limiter
        api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        api.add_middleware(SlowAPIMiddleware)

        @api.get("/probe")
        def probe(request: Request):
            return {"ok": True}

        client = TestClient(
            ProxyHeadersMiddleware(api, trusted_hosts=self._configured())
        )
        blocked = 0
        for i in range(60):
            resp = client.get(
                "/probe",
                headers={"X-Forwarded-For": f"198.51.100.{i}, {self.CALLER}"},
            )
            if resp.status_code == 429:
                blocked += 1

        assert blocked == 30, (
            f"{blocked} of 60 rotating-source requests were limited; expected 30. "
            "A caller rotating X-Forwarded-For is getting a fresh bucket per request."
        )

    def test_the_checks_can_fail(self):
        """The predicate against the vulnerable configuration it never sees.

        Without this, every assertion above would also pass against a middleware
        that ignored X-Forwarded-For entirely, and nothing here would prove the
        tests observe the setting at all.
        """
        forged_chain = f"{self.FORGED}, {self.CALLER}"

        # The wildcard takes the leftmost, caller-written entry...
        assert self._attributed(["*"], forged_chain, peer=self.EDGE) == self.FORGED
        # ...and believes a direct caller with no proxy in front of them.
        assert self._attributed(["*"], self.FORGED, peer=self.CALLER) == self.FORGED
        # A named set rejects both.
        assert (
            self._attributed([self.EDGE], forged_chain, peer=self.EDGE) == self.CALLER
        )


class TestForwardedForAmbiguity:
    """Multiple X-Forwarded-For headers bypass the #726/#759 fix (#765).

    ProxyHeadersMiddleware reads headers via ``dict(scope["headers"])``, which
    keeps only the LAST of a repeated header name. A caller who sends
    X-Forwarded-For as two separate header instances instead of one
    comma-joined value can make an attacker-chosen value survive that dict
    collapse regardless of TRUSTED_PROXY_HOSTS -- TestForwardedForAttribution
    above only proves the single-header case is closed.

    An early draft of the fix tried to merge every X-Forwarded-For instance
    into one comma-joined value before ProxyHeadersMiddleware ran. It did not
    work: whichever instance ends up last after concatenation still wins the
    right-to-left trust walk, so an attacker who controls ordering still wins
    -- the same bug, wearing a different mechanism. That draft failed its own
    version of test_two_headers_real_then_forged_does_not_attribute_to_the_forgery
    below before it ever reached this file.

    DropAmbiguousForwardedForMiddleware (src/api/app.py) closes this
    differently: it does not try to reconstruct which instance is "real" from
    an inherently ambiguous wire shape. More than one X-Forwarded-For instance
    drops the header entirely, and ProxyHeadersMiddleware falls back to the
    raw connecting peer -- the same path test_a_direct_caller_cannot_forge_at_all
    above already proves is safe.
    """

    EDGE = "10.0.0.5"
    CALLER = "192.0.2.50"
    FORGED = "198.51.100.7"

    @staticmethod
    def _attributed(headers, peer, trusted_hosts=None):
        """Run the real two-middleware stack; return the attributed client host.

        ``headers`` is a list of (name, value) str tuples, one entry per raw
        header INSTANCE -- multiple x-forwarded-for entries model the
        ambiguous wire shape this middleware exists for.
        """
        import asyncio

        from src.api.app import DropAmbiguousForwardedForMiddleware
        from src.config.settings import settings
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        seen = {}

        async def app(scope, receive, send):
            seen["host"] = scope["client"][0] if scope.get("client") else None

        raw = [(k.encode(), v.encode()) for k, v in headers]
        stack = DropAmbiguousForwardedForMiddleware(
            ProxyHeadersMiddleware(
                app, trusted_hosts=trusted_hosts or settings.trusted_proxy_hosts
            )
        )
        asyncio.run(
            stack(
                {"type": "http", "client": (peer, 54321), "headers": raw}, None, None
            )
        )
        return seen["host"]

    @classmethod
    def _xff(cls, values, peer, trusted_hosts=None):
        """Convenience: one X-Forwarded-For header instance per value."""
        return cls._attributed(
            [("x-forwarded-for", v) for v in values], peer, trusted_hosts
        )

    def test_two_headers_real_then_forged_does_not_attribute_to_the_forgery(self):
        """The exact #765 shape: edge's real header first, forged bare one second."""
        got = self._xff(
            [f"{self.FORGED}, {self.CALLER}", self.FORGED], peer=self.EDGE
        )
        assert got != self.FORGED, f"attributed to the forged value {self.FORGED!r}"

    def test_two_headers_forged_then_real_does_not_attribute_to_the_forgery(self):
        """Same shape, reversed -- the fix must not depend on arrival order."""
        got = self._xff(
            [self.FORGED, f"{self.FORGED}, {self.CALLER}"], peer=self.EDGE
        )
        assert got != self.FORGED

    def test_ambiguous_xff_falls_back_to_the_raw_peer(self):
        """Falls back exactly where "no X-Forwarded-For at all" already lands
        -- not CALLER (unrecoverable from a flattened, ambiguous shape) and
        never FORGED."""
        got = self._xff(
            [self.FORGED, f"{self.FORGED}, {self.CALLER}"], peer=self.EDGE
        )
        assert got == self.EDGE

    def test_three_or_more_headers_still_drops(self):
        """Not fooled by header count -- any count > 1 is ambiguous."""
        got = self._xff(
            [self.FORGED, self.CALLER, self.FORGED, self.FORGED], peer=self.EDGE
        )
        assert got == self.EDGE

    def test_a_single_header_is_unaffected(self):
        """The already-tested single-header path must not regress."""
        got = self._xff([f"{self.FORGED}, {self.CALLER}"], peer=self.EDGE)
        assert got == self.CALLER

    def test_no_header_at_all_is_unaffected(self):
        got = self._xff([], peer=self.EDGE)
        assert got == self.EDGE

    def test_mixed_case_header_name_is_still_counted(self):
        """scope["headers"] names are lowercase per the ASGI spec, but the
        count must not silently miss a non-conformant server/middleware."""
        got = self._attributed(
            [
                ("X-Forwarded-For", f"{self.FORGED}, {self.CALLER}"),
                ("x-forwarded-for", self.FORGED),
            ],
            peer=self.EDGE,
        )
        assert got == self.EDGE

    def test_untrusted_peer_is_unaffected_by_the_drop(self):
        """A direct, untrusted caller was never attributed from XFF anyway --
        the drop must not change that outcome."""
        got = self._xff(
            [self.FORGED, f"{self.FORGED}, {self.CALLER}"], peer=self.CALLER
        )
        assert got == self.CALLER

    def test_rate_limit_buckets_do_not_partition_on_rotating_multi_header_xff(self):
        """End-to-end sibling of TestForwardedForAttribution's single-header
        version: 60 requests, each carrying TWO raw X-Forwarded-For headers
        with a rotating forged value. Correct behaviour is the same 30/60
        ceiling as a single real IP -- not 0 blocked (a fresh bucket per
        request) and not 60 blocked (the fallback peer wrongly rejected).
        """
        from fastapi import FastAPI, Request
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware
        from slowapi.util import get_remote_address
        from starlette.testclient import TestClient
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        from src.api.app import DropAmbiguousForwardedForMiddleware
        from src.config.settings import settings

        rate_limiter = Limiter(
            key_func=get_remote_address,
            default_limits=["30/minute"],
            storage_uri="memory://",
        )
        api = FastAPI()
        api.state.limiter = rate_limiter
        api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        api.add_middleware(SlowAPIMiddleware)

        @api.get("/probe")
        def probe(request: Request):
            return {"ok": True}

        stack = DropAmbiguousForwardedForMiddleware(
            ProxyHeadersMiddleware(api, trusted_hosts=settings.trusted_proxy_hosts)
        )
        client = TestClient(stack, client=(self.EDGE, 54321))

        blocked = 0
        for i in range(60):
            resp = client.get(
                "/probe",
                headers=[
                    ("X-Forwarded-For", f"198.51.100.{i}"),
                    ("X-Forwarded-For", f"198.51.100.{i}, {self.CALLER}"),
                ],
            )
            if resp.status_code == 429:
                blocked += 1

        assert blocked == 30, (
            f"{blocked} of 60 rotating multi-header requests were limited; "
            "expected 30 -- the shared fallback-peer bucket."
        )
