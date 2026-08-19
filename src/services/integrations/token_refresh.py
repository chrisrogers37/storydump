"""Token refresh service for managing OAuth tokens."""

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from src.services.base_service import BaseService
from src.repositories.token_repository import TokenRepository
from src.repositories.instagram_account_repository import InstagramAccountRepository
from src.utils.encryption import TokenEncryption
from src.config.constants import IG_LOGIN_GRAPH_BASE
from src.config.settings import settings
from src.exceptions import TokenCorruptError, TokenExpiredError, TokenRevokedError
from src.utils.datetime_utils import ensure_utc
from src.utils.logger import logger
from src.repositories.tenant_scope import SYSTEM_SCOPE


class TokenRefreshService(BaseService):
    """
    Manage OAuth tokens for external services.

    Handles:
    - Token retrieval (DB first, fallback to .env)
    - Automatic refresh before expiry
    - Token health monitoring
    - Bootstrap from .env to DB

    Usage:
        service = TokenRefreshService()

        # Get current valid token
        token = service.get_token("instagram")

        # Check token health
        health = service.check_token_health("instagram")
        if health["needs_refresh"]:
            await service.refresh_instagram_token()
    """

    # Instagram Login token refresh endpoint (different from Facebook Login)
    IG_LOGIN_REFRESH_ENDPOINT = f"{IG_LOGIN_GRAPH_BASE}/refresh_access_token"

    # Refresh buffer: refresh tokens this many hours before expiry
    REFRESH_BUFFER_HOURS = 168  # 7 days

    # After this many consecutive refresh failures per account, flag for alert
    CONSECUTIVE_FAILURE_ALERT_THRESHOLD = 3

    def __init__(self):
        super().__init__()
        self.token_repo = TokenRepository()
        self.account_repo = InstagramAccountRepository()
        self._encryption: Optional[TokenEncryption] = None
        # Track consecutive refresh failures per account label
        self._consecutive_failures: dict[str, int] = {}

    @property
    def encryption(self) -> TokenEncryption:
        """Lazy-load encryption to avoid errors when ENCRYPTION_KEY not set."""
        if self._encryption is None:
            self._encryption = TokenEncryption()
        return self._encryption

    def get_token(
        self, service: str, token_type: str = "access_token"
    ) -> Optional[str]:
        """
        Get current valid token for a service.

        Strategy:
        1. Check database for stored token
        2. If not in DB, check .env settings
        3. If in .env but not DB, bootstrap to DB

        Args:
            service: Service name (e.g., 'instagram')
            token_type: Token type (default: 'access_token')

        Returns:
            Decrypted token string, or None if not available

        Raises:
            TokenExpiredError: If token exists but is expired
        """
        # DB is the only source of truth. Tokens land here via the OAuth
        # callback (instagram_login_oauth.py / oauth.py).
        db_token = self.token_repo.get_token(service, token_type)

        if db_token:
            if db_token.is_expired:
                logger.warning(f"Token for {service}/{token_type} has expired")
                raise TokenExpiredError(
                    f"Token for {service} has expired. Please refresh or re-authenticate."
                )
            try:
                return self.encryption.decrypt(db_token.token_value)
            except ValueError as e:
                logger.error(f"Failed to decrypt token for {service}: {e}")
                return None

        return None

    def _get_refresh_endpoint(self, instagram_account_id: Optional[str]) -> str:
        """Return the correct token refresh URL based on the token's
        OAuth flow.

        Instagram Login tokens refresh via graph.instagram.com.
        Facebook Login / legacy tokens refresh via graph.facebook.com.

        Reads ``auth_method`` directly off the token row rather than
        joining through ``instagram_accounts.auth_method`` — part of
        the credential refactor (#468) that moves provenance onto the
        credential itself. Falls back to the FB host when no token is
        found or the field is unset.
        """
        if instagram_account_id:
            token = self.token_repo.get_token_for_account(
                instagram_account_id, token_type="access_token"
            )
            if token and token.auth_method == "instagram_login":
                return self.IG_LOGIN_REFRESH_ENDPOINT

        return f"{settings.meta_graph_base}/oauth/access_token"

    def _get_current_token(self, instagram_account_id: Optional[str] = None):
        """Retrieve the current token from the database.

        Returns:
            The token DB record, or None if not found.
        """
        if instagram_account_id:
            return self.token_repo.get_token_for_account(
                instagram_account_id, token_type="access_token"
            )
        return self.token_repo.get_token("instagram", "access_token")

    def _get_current_token_locked(self, instagram_account_id: Optional[str] = None):
        """Retrieve the current token with a row lock for safe concurrent refresh.

        Uses SELECT ... FOR UPDATE SKIP LOCKED. If another process is already
        refreshing this token, returns None to avoid clobbering.

        Returns:
            The locked token DB record, or None if not found / already locked.
        """
        return self.token_repo.get_token_for_update(
            "instagram", "access_token", instagram_account_id
        )

    async def _call_meta_refresh(
        self, current_token: str, instagram_account_id: Optional[str]
    ) -> httpx.Response:
        """Call Meta's token refresh endpoint.

        Meta exposes two different long-lived token refresh contracts that
        share neither parameter names nor required fields:

        - `graph.instagram.com/refresh_access_token` (IG Login flow) —
          accepts the token alone: `grant_type=ig_refresh_token` +
          `access_token`.
        - `graph.facebook.com/.../oauth/access_token` (FB Login / legacy
          flow) — requires the FB-style grant + app credentials:
          `grant_type=fb_exchange_token`, `client_id`, `client_secret`,
          and `fb_exchange_token` (the existing long-lived token).

        The previous implementation always sent the IG-flavored params,
        so any refresh against the FB host failed with Meta error 101
        "Missing client_id parameter." Branch on the resolved URL so
        each host gets its expected payload.

        Returns:
            The HTTP response from Meta's API.
        """
        refresh_url = self._get_refresh_endpoint(instagram_account_id)
        if refresh_url == self.IG_LOGIN_REFRESH_ENDPOINT:
            params = {
                "grant_type": "ig_refresh_token",
                "access_token": current_token,
            }
        else:
            params = {
                "grant_type": "fb_exchange_token",
                "client_id": settings.FACEBOOK_APP_ID,
                "client_secret": settings.FACEBOOK_APP_SECRET,
                "fb_exchange_token": current_token,
            }
        async with httpx.AsyncClient() as client:
            return await client.get(refresh_url, params=params, timeout=30.0)

    def _store_refreshed_token(
        self, new_token: str, expires_in: int, instagram_account_id: Optional[str]
    ) -> datetime:
        """Encrypt and persist a refreshed token to the database.

        Returns:
            The new expiry datetime.
        """
        issued_at = datetime.now(timezone.utc)
        expires_at = issued_at + timedelta(seconds=expires_in)
        encrypted = self.encryption.encrypt(new_token)

        self.token_repo.create_or_update(
            service_name="instagram",
            token_type="access_token",
            token_value=encrypted,
            issued_at=issued_at,
            expires_at=expires_at,
            metadata={
                "refreshed_at": issued_at.isoformat(),
                "expires_in_seconds": expires_in,
            },
            instagram_account_id=instagram_account_id,
            chat_settings_id=SYSTEM_SCOPE,
        )
        return expires_at

    async def refresh_instagram_token(
        self, instagram_account_id: Optional[str] = None
    ) -> bool:
        """
        Refresh Instagram long-lived access token.

        Meta's long-lived tokens last 60 days but can be refreshed.
        A refreshed token is valid for another 60 days.

        Supports both:
        - Multi-account mode: Pass instagram_account_id to refresh specific account
        - Legacy mode: Pass None to refresh the legacy token (no account FK)

        Args:
            instagram_account_id: UUID of Instagram account to refresh (None for legacy)

        Returns:
            True if refresh successful

        Raises:
            TokenExpiredError: If current token is invalid/expired
        """
        account_label = instagram_account_id or "legacy"

        with self.track_execution(
            method_name="refresh_instagram_token",
            input_params={"account_id": account_label},
        ) as run_id:
            # Acquire row lock to prevent concurrent refresh clobbering.
            # SKIP LOCKED: if another process is already refreshing, skip.
            db_token = self._get_current_token_locked(instagram_account_id)
            if not db_token:
                logger.info(
                    f"Token refresh skipped for {account_label}: "
                    "not found or locked by another process"
                )
                self.set_result_summary(
                    run_id, {"success": False, "reason": "no_token_or_locked"}
                )
                return False

            current_token = self.encryption.decrypt(db_token.token_value)

            try:
                response = await self._call_meta_refresh(
                    current_token, instagram_account_id
                )

                if response.status_code != 200:
                    error_data = response.json()
                    error_info = error_data.get("error", {})
                    error_subcode = error_info.get("error_subcode")

                    # Distinguish revocation from normal refresh failure
                    if error_subcode in TokenRevokedError.REVOCATION_SUBCODES:
                        logger.error(
                            f"Instagram token REVOKED for {account_label} "
                            f"(subcode {error_subcode}): {error_data}"
                        )
                        self.set_result_summary(
                            run_id,
                            {
                                "success": False,
                                "reason": "token_revoked",
                                "error_subcode": error_subcode,
                                "error": error_data,
                            },
                        )
                        raise TokenRevokedError(
                            f"Instagram token revoked for {account_label}. "
                            "User must reconnect their account.",
                            error_subcode=error_subcode,
                        )

                    logger.error(
                        f"Instagram token refresh failed for {account_label}: {error_data}"
                    )
                    self.set_result_summary(
                        run_id,
                        {
                            "success": False,
                            "status_code": response.status_code,
                            "error": error_data,
                        },
                    )
                    return False

                data = response.json()
                new_token = data.get("access_token")
                expires_in = data.get("expires_in", 5184000)  # Default 60 days

                if not new_token:
                    logger.error(
                        f"No access_token in refresh response for {account_label}"
                    )
                    self.set_result_summary(
                        run_id, {"success": False, "reason": "no_token_in_response"}
                    )
                    return False

                expires_at = self._store_refreshed_token(
                    new_token, expires_in, instagram_account_id
                )

                logger.info(
                    f"Instagram token refreshed successfully for {account_label}. "
                    f"New expiry: {expires_at.isoformat()}"
                )

                self.set_result_summary(
                    run_id,
                    {
                        "success": True,
                        "account": account_label,
                        "expires_at": expires_at.isoformat(),
                        "expires_in_days": expires_in // 86400,
                    },
                )
                return True

            except httpx.RequestError as e:
                logger.error(
                    f"Network error refreshing Instagram token for {account_label}: {e}"
                )
                self.set_result_summary(run_id, {"success": False, "error": str(e)})
                return False

    async def refresh_all_instagram_tokens(self) -> dict:
        """
        Refresh all Instagram tokens that need refreshing.

        Iterates through all Instagram accounts and refreshes tokens
        that are expiring within the buffer window.

        Returns:
            dict with:
                - refreshed: int - count of successfully refreshed tokens
                - failed: int - count of failed refreshes
                - skipped: int - count skipped (not expiring soon)
                - details: list - per-account results
                - alerts: list - accounts that have failed >= threshold
                  consecutive times and need user attention
        """
        with self.track_execution(method_name="refresh_all_instagram_tokens") as run_id:
            results = {
                "refreshed": 0,
                "failed": 0,
                "skipped": 0,
                "details": [],
                "alerts": [],
            }

            # Get all Instagram tokens
            all_tokens = self.token_repo.get_all_instagram_tokens()

            for token in all_tokens:
                account_id = (
                    str(token.instagram_account_id)
                    if token.instagram_account_id
                    else None
                )
                account_label = account_id or "legacy"

                # Check if needs refresh
                hours_until_expiry = token.hours_until_expiry()
                needs_refresh = (
                    hours_until_expiry is not None
                    and hours_until_expiry <= self.REFRESH_BUFFER_HOURS
                )

                if not needs_refresh:
                    results["skipped"] += 1
                    results["details"].append(
                        {
                            "account": account_label,
                            "status": "skipped",
                            "reason": f"Not expiring soon ({int(hours_until_expiry or 0)}h remaining)",
                        }
                    )
                    continue

                # Refresh this token — catch revocation per-account so one
                # revoked account doesn't abort the loop for the rest.
                try:
                    success = await self.refresh_instagram_token(
                        instagram_account_id=account_id
                    )
                except (TokenRevokedError, TokenCorruptError) as e:
                    logger.error(f"Token revoked/corrupt for {account_label}: {e}")
                    self._consecutive_failures[account_label] = (
                        self._consecutive_failures.get(account_label, 0) + 1
                    )
                    results["failed"] += 1
                    results["details"].append(
                        {"account": account_label, "status": "revoked"}
                    )
                    continue

                if success:
                    self._consecutive_failures.pop(account_label, None)
                    results["refreshed"] += 1
                    results["details"].append(
                        {
                            "account": account_label,
                            "status": "refreshed",
                        }
                    )
                else:
                    self._consecutive_failures[account_label] = (
                        self._consecutive_failures.get(account_label, 0) + 1
                    )
                    results["failed"] += 1
                    results["details"].append(
                        {
                            "account": account_label,
                            "status": "failed",
                        }
                    )

            # Flag accounts that have hit the consecutive failure threshold
            for label, count in self._consecutive_failures.items():
                if count >= self.CONSECUTIVE_FAILURE_ALERT_THRESHOLD:
                    results["alerts"].append(
                        {
                            "account": label,
                            "consecutive_failures": count,
                            "message": (
                                f"Instagram token refresh has failed {count} "
                                f"consecutive times. The token may be corrupt "
                                f"or invalidated. Please reconnect the account "
                                f"in Settings."
                            ),
                        }
                    )

            logger.info(
                f"Token refresh complete: {results['refreshed']} refreshed, "
                f"{results['failed']} failed, {results['skipped']} skipped"
            )
            if results["alerts"]:
                logger.warning(
                    f"Token refresh alerts: {len(results['alerts'])} account(s) "
                    f"need attention"
                )

            self.set_result_summary(run_id, results)
            return results

    def check_token_health(self, service: str) -> dict:
        """
        Check token status for a service.

        Returns:
            dict with:
                - valid: bool - whether token exists and is not expired
                - exists: bool - whether token record exists
                - expires_at: datetime or None
                - expires_in_hours: float or None
                - needs_refresh: bool - whether refresh should be scheduled
                - last_refreshed: datetime or None
                - error: str or None - error message if invalid
        """
        db_token = self.token_repo.get_token(service, "access_token")

        if not db_token:
            return {
                "valid": False,
                "exists": False,
                "source": None,
                "expires_at": None,
                "expires_in_hours": None,
                "needs_refresh": False,
                "needs_bootstrap": False,
                "last_refreshed": None,
                "error": f"No token found for {service}",
            }

        expires_in_hours = db_token.hours_until_expiry()
        needs_refresh = (
            expires_in_hours is not None
            and expires_in_hours <= self.REFRESH_BUFFER_HOURS
        )

        return {
            "valid": not db_token.is_expired,
            "exists": True,
            "source": "database",
            "expires_at": db_token.expires_at,
            "expires_in_hours": expires_in_hours,
            "needs_refresh": needs_refresh,
            "needs_bootstrap": False,
            "last_refreshed": db_token.last_refreshed_at,
            "error": "Token expired" if db_token.is_expired else None,
        }

    def check_token_health_for_chat(self, service: str, chat_settings_id: str) -> dict:
        """Check token status for a per-tenant service (e.g., google_drive).

        Unlike check_token_health() which queries by instagram_account_id,
        this queries by chat_settings_id for tenant-scoped tokens.

        For services with refresh tokens (e.g., Google Drive), an expired
        access token is normal — the OAuth library auto-refreshes it. Only
        report unhealthy when the refresh token is missing or expired.

        Also checks refresh token age against GOOGLE_REFRESH_TOKEN_TTL_DAYS
        (default 7 — Google Testing mode silently expires refresh tokens
        after 7 days).
        """
        db_token = self.token_repo.get_token_for_chat(
            service, "oauth_access", chat_settings_id
        )

        if not db_token:
            return {
                "valid": False,
                "exists": False,
                "source": None,
                "expires_at": None,
                "expires_in_hours": None,
                "needs_refresh": False,
                "auto_refreshable": False,
                "refresh_token_exists": False,
                "refresh_token_age_days": None,
                "refresh_token_expires_in_days": None,
                "last_refreshed": None,
                "error": f"No {service} token found for this chat",
            }

        refresh_token = self.token_repo.get_token_for_chat(
            service, "oauth_refresh", chat_settings_id
        )
        refresh_token_exists = refresh_token is not None
        auto_refreshable = refresh_token_exists and not refresh_token.is_expired

        # Check refresh token age against TTL (Testing mode: 7 days)
        refresh_token_age_days = None
        refresh_token_expires_in_days = None
        ttl_days = settings.GOOGLE_REFRESH_TOKEN_TTL_DAYS

        if refresh_token_exists and ttl_days > 0:
            issued_at = (
                ensure_utc(refresh_token.issued_at) if refresh_token.issued_at else None
            )
            if issued_at:
                age = datetime.now(timezone.utc) - issued_at
                refresh_token_age_days = round(age.total_seconds() / 86400, 1)
                refresh_token_expires_in_days = round(
                    ttl_days - refresh_token_age_days, 1
                )

                if refresh_token_expires_in_days <= 0:
                    auto_refreshable = False

        expires_in_hours = db_token.hours_until_expiry()
        needs_refresh = (
            expires_in_hours is not None
            and expires_in_hours <= self.REFRESH_BUFFER_HOURS
        )

        access_expired = db_token.is_expired
        valid = not access_expired or auto_refreshable
        error = (
            "Token expired and no valid refresh token"
            if access_expired and not auto_refreshable
            else None
        )

        return {
            "valid": valid,
            "exists": True,
            "source": "database",
            "expires_at": db_token.expires_at,
            "expires_in_hours": expires_in_hours,
            "needs_refresh": needs_refresh,
            "auto_refreshable": auto_refreshable,
            "refresh_token_exists": refresh_token_exists,
            "refresh_token_age_days": refresh_token_age_days,
            "refresh_token_expires_in_days": refresh_token_expires_in_days,
            "last_refreshed": db_token.last_refreshed_at,
            "error": error,
        }

    def get_tokens_needing_refresh(self) -> list:
        """Get all tokens that need to be refreshed soon."""
        return self.token_repo.get_expiring_tokens(
            hours_until_expiry=self.REFRESH_BUFFER_HOURS
        )

    async def revoke_tokens(
        self,
        service_name: str,
        instagram_account_id: Optional[str] = None,
        chat_settings_id: Optional[str] = None,
        skip_provider: bool = False,
    ) -> dict:
        """
        Revoke tokens for a service by calling provider APIs then marking revoked_at.

        For compromised tokens: call the provider's revocation endpoint first
        (best-effort — the token may already be invalid), then set revoked_at
        in the database so the token is excluded from all future queries.

        Args:
            service_name: 'instagram' or 'google_drive'
            instagram_account_id: Scope to a specific Instagram account
            chat_settings_id: Scope to a specific tenant (Google Drive)
            skip_provider: If True, skip calling provider revoke APIs

        Returns:
            dict with revoked count and provider revocation results
        """
        with self.track_execution(
            method_name="revoke_tokens",
            input_params={
                "service": service_name,
                "account_id": instagram_account_id,
                "chat_settings_id": chat_settings_id,
            },
        ) as run_id:
            tokens = self.token_repo.revoke_tokens_for_service(
                service_name=service_name,
                instagram_account_id=instagram_account_id,
                chat_settings_id=chat_settings_id,
            )

            if not tokens:
                result = {"revoked": 0, "provider_results": []}
                self.set_result_summary(run_id, result)
                return result

            provider_results = []
            if not skip_provider:
                for token in tokens:
                    try:
                        raw_value = self.encryption.decrypt(token.token_value)
                        revoke_result = await self._call_provider_revoke(
                            service_name, raw_value
                        )
                        provider_results.append(revoke_result)
                    except Exception as e:  # noqa: BLE001 — best-effort, don't block DB revocation
                        provider_results.append(
                            {"token_type": token.token_type, "error": str(e)}
                        )

            result = {
                "revoked": len(tokens),
                "provider_results": provider_results,
            }

            logger.info(
                f"Revoked {len(tokens)} token(s) for {service_name}"
                + (
                    f" (provider calls: {len(provider_results)})"
                    if provider_results
                    else ""
                )
            )

            self.set_result_summary(run_id, result)
            return result

    async def _call_provider_revoke(self, service_name: str, token_value: str) -> dict:
        """Call the provider's token revocation API (best-effort)."""
        if service_name == "instagram":
            return await self._revoke_instagram_token(token_value)
        elif service_name == "google_drive":
            return await self._revoke_google_token(token_value)
        return {"status": "no_provider_api", "service": service_name}

    async def _revoke_instagram_token(self, token_value: str) -> dict:
        """Revoke an Instagram/Meta token via DELETE /me/permissions."""
        url = f"{settings.meta_graph_base}/me/permissions"
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                url,
                params={"access_token": token_value},
                timeout=15.0,
            )
            return {
                "service": "instagram",
                "status_code": response.status_code,
                "success": response.status_code == 200,
            }

    async def _revoke_google_token(self, token_value: str) -> dict:
        """Revoke a Google OAuth token via POST /revoke."""
        url = "https://oauth2.googleapis.com/revoke"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                data={"token": token_value},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )
            return {
                "service": "google_drive",
                "status_code": response.status_code,
                "success": response.status_code == 200,
            }
