"""Instagram Graph API service for Story posting."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import httpx

from src.services.base_service import BaseService
from src.services.integrations.instagram_credentials import InstagramCredentialManager
from src.services.integrations.token_refresh import TokenRefreshService
from src.services.integrations.cloud_storage import CloudStorageService
from src.services.core.instagram_account_service import InstagramAccountService
from src.services.core.settings_service import SettingsService
from src.repositories.history_repository import HistoryRepository
from src.repositories.token_repository import TokenRepository
from src.utils.encryption import TokenEncryption
from src.config.settings import settings
from src.exceptions import (
    InstagramAPIError,
    MediaUnsupportedError,
    RateLimitError,
    TokenCorruptError,
    TokenExpiredError,
    TokenRevokedError,
)
from src.utils.logger import logger

# Phrases in Meta code-190 error messages that indicate a corrupt/unparseable
# token rather than a genuinely expired one.
_TOKEN_CORRUPT_PHRASES = (
    "cannot parse access token",
    "malformed access token",
)


class InstagramAPIService(BaseService):
    """
    Instagram Graph API integration for Stories.

    Handles:
    - Story creation and publishing
    - Media container status polling
    - Rate limit tracking
    - Error categorization

    Usage:
        service = InstagramAPIService()

        # Post a story
        result = await service.post_story(media_url="https://...")
        # result["story_id"] → The Instagram story ID

        # Check rate limits
        remaining = service.get_rate_limit_remaining()
    """

    # Meta Graph API configuration
    CONTAINER_STATUS_POLL_INTERVAL = 2  # seconds
    CONTAINER_STATUS_MAX_POLLS = 30  # max ~60 seconds wait
    MIN_ACCOUNT_ID_LENGTH = 10  # Instagram account IDs are typically 15-17 digits

    def __init__(self):
        super().__init__()
        self.token_service = TokenRefreshService()
        self.cloud_service = CloudStorageService()
        self.history_repo = HistoryRepository()
        self.account_service = InstagramAccountService()
        self.token_repo = TokenRepository()
        self.encryption = TokenEncryption()
        self.settings_service = SettingsService()
        self.credentials = InstagramCredentialManager(self)

    def _get_active_account_credentials(
        self, telegram_chat_id: int
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Get credentials for the active Instagram account. Delegates to credentials."""
        return self.credentials.get_active_account_credentials(telegram_chat_id)

    async def post_story(
        self,
        media_url: str,
        media_type: str = "IMAGE",
        telegram_chat_id: Optional[int] = None,
        on_container_created: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """
        Post a Story to Instagram.

        Flow:
        1. Create media container
        2. Poll until status is FINISHED
        3. Publish the container

        Args:
            media_url: Public URL to media (from CloudStorageService)
            media_type: IMAGE or VIDEO
            telegram_chat_id: Chat ID to get active account for (uses ADMIN chat if not specified)
            on_container_created: Optional callback invoked with the container_id
                the instant the media container is created and BEFORE the
                publish call. Lets the caller persist the container_id as a
                claim-before-publish anchor (#549) so a crash after publish
                can't duplicate the story. Runs inside the 180s wall-clock cap;
                if it raises, the publish is aborted (no anchor => nothing
                published => safe to retry).

        Returns:
            dict with:
                - success: bool
                - story_id: str (Instagram media ID)
                - container_id: str
                - timestamp: datetime
                - account_username: str (which account was used)

        Raises:
            InstagramAPIError: On API failure
            RateLimitError: When rate limited
            TokenExpiredError: When token needs refresh
        """
        # Default to admin chat if not specified (for backward compatibility)
        if telegram_chat_id is None:
            telegram_chat_id = settings.ADMIN_TELEGRAM_CHAT_ID

        with self.track_execution(
            method_name="post_story",
            input_params={
                "media_url": media_url[:50] + "...",
                "media_type": media_type,
            },
        ) as run_id:
            # Resolve the active account first — the publishing-quota gate below
            # reuses these creds (no duplicate lookup) and a missing token fails
            # fast before we ever call Meta.
            token, account_id, account_username = self._get_active_account_credentials(
                telegram_chat_id
            )

            if not token:
                raise TokenExpiredError(
                    "No valid Instagram token available for active account"
                )

            if not account_id:
                raise InstagramAPIError(
                    "No Instagram account selected. Use /settings to select one."
                )

            # Gate on Meta's authoritative rolling-24h publishing quota (fetched
            # live per-account, reusing the creds above); fail-open so a
            # monitoring blip can't block a legitimate post — the real backstop
            # is Meta's server-side 429 on the publish call itself.
            publish_limit = await self.get_content_publishing_limit(
                telegram_chat_id, token=token, account_id=account_id
            )
            if publish_limit["remaining"] <= 0:
                raise RateLimitError(
                    f"Instagram daily publishing limit reached "
                    f"({publish_limit['quota_usage']}/{publish_limit['quota_total']} "
                    f"in the last 24h)."
                )

            try:
                # Hard wall-clock cap on the whole 3-step flow so a single
                # hung Instagram call can't camp on DB connections / pool
                # slots indefinitely (was observed at 7+ minutes per failed
                # post, exhausting the connection pool).
                story_id, container_id = await asyncio.wait_for(
                    self._post_story_steps(
                        token=token,
                        account_id=account_id,
                        media_url=media_url,
                        media_type=media_type,
                        on_container_created=on_container_created,
                    ),
                    timeout=180.0,
                )

                logger.info(f"Published Instagram Story: {story_id}")

                result = {
                    "success": True,
                    "story_id": story_id,
                    "container_id": container_id,
                    "timestamp": datetime.now(timezone.utc),
                    "account_username": account_username,
                    "account_id": account_id,
                }

                self.set_result_summary(
                    run_id,
                    {
                        "success": True,
                        "story_id": story_id,
                        "account": account_username,
                    },
                )

                return result

            except asyncio.TimeoutError:
                logger.error(
                    "Instagram post exceeded 180s wall-clock cap "
                    f"(account={account_username}). Aborting to release "
                    "DB connections."
                )
                raise InstagramAPIError("Instagram post timed out after 180 seconds")
            except httpx.RequestError as e:
                logger.error(f"Network error posting to Instagram: {e}")
                raise InstagramAPIError(f"Network error: {e}")

    async def _post_story_steps(
        self,
        token: str,
        account_id: str,
        media_url: str,
        media_type: str,
        on_container_created: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, str]:
        """Run the 3-step Instagram story flow. Returns (story_id, container_id).

        When ``on_container_created`` is provided it fires with the container_id
        immediately after creation and before polling/publishing, so the caller
        can persist the claim-before-publish anchor. A raise from the callback
        propagates and aborts the flow before any publish attempt.
        """
        container_id = await self._create_media_container(
            token=token,
            account_id=account_id,
            media_url=media_url,
            media_type=media_type,
        )
        logger.info(f"Created media container: {container_id}")

        if on_container_created is not None:
            on_container_created(container_id)

        await self._wait_for_container_ready(token, container_id)

        story_id = await self._publish_container(
            token=token,
            account_id=account_id,
            container_id=container_id,
        )
        return story_id, container_id

    async def _create_media_container(
        self,
        token: str,
        account_id: str,
        media_url: str,
        media_type: str,
    ) -> str:
        """Create a media container for the story."""
        async with httpx.AsyncClient() as client:
            # For Stories, use the stories_media endpoint
            params = {
                "access_token": token,
                "media_type": "STORIES",
            }

            if media_type == "VIDEO":
                params["video_url"] = media_url
            else:
                params["image_url"] = media_url

            response = await client.post(
                f"{settings.meta_ig_graph_base}/{account_id}/media",
                data=params,
                timeout=60.0,
            )

            self._check_response_errors(response)

            data = response.json()
            container_id = data.get("id")

            if not container_id:
                raise InstagramAPIError(
                    "No container ID in response",
                )

            return container_id

    async def _wait_for_container_ready(self, token: str, container_id: str) -> None:
        """Poll container status until FINISHED."""
        async with httpx.AsyncClient() as client:
            for poll_num in range(self.CONTAINER_STATUS_MAX_POLLS):
                response = await client.get(
                    f"{settings.meta_ig_graph_base}/{container_id}",
                    params={
                        "fields": "status_code,status",
                        "access_token": token,
                    },
                    timeout=10.0,
                )

                self._check_response_errors(response)

                data = response.json()
                status_code = data.get("status_code")

                if status_code == "FINISHED":
                    logger.debug(
                        f"Container {container_id} ready after {poll_num + 1} polls"
                    )
                    return

                if status_code == "ERROR":
                    error_msg = data.get("status", "Unknown error")
                    raise InstagramAPIError(
                        f"Media container failed: {error_msg}",
                        error_code=status_code,
                    )

                if status_code == "EXPIRED":
                    raise InstagramAPIError(
                        "Media container expired before publishing",
                        error_code=status_code,
                    )

                # Still processing, wait and retry
                logger.debug(f"Container status: {status_code}, polling again...")
                await asyncio.sleep(self.CONTAINER_STATUS_POLL_INTERVAL)

            # Exhausted polls
            raise InstagramAPIError(
                f"Media container did not finish after {self.CONTAINER_STATUS_MAX_POLLS} polls"
            )

    async def _publish_container(
        self,
        token: str,
        account_id: str,
        container_id: str,
    ) -> str:
        """Publish the media container as a Story."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.meta_ig_graph_base}/{account_id}/media_publish",
                data={
                    "creation_id": container_id,
                    "access_token": token,
                },
                timeout=60.0,
            )

            self._check_response_errors(response)

            data = response.json()
            story_id = data.get("id")

            if not story_id:
                raise InstagramAPIError(
                    "No story ID in publish response",
                )

            return story_id

    def _check_response_errors(self, response: httpx.Response) -> None:
        """Check API response for errors and raise appropriate exceptions."""
        if response.status_code == 200:
            return

        try:
            data = response.json()
            error = data.get("error", {})
        except (ValueError, UnicodeDecodeError):
            raise InstagramAPIError(
                f"HTTP {response.status_code}: {response.text}",
            )

        error_code = error.get("code")
        error_subcode = error.get("error_subcode")
        error_message = error.get("message", "Unknown error")

        # Rate limit errors
        if error_code == 4 or error_code == 17:
            raise RateLimitError(
                error_message,
                error_code=str(error_code),
                error_subcode=error_subcode,
            )

        # Token errors — distinguish revocation, corruption, and expiry
        if error_code == 190:
            if error_subcode in TokenRevokedError.REVOCATION_SUBCODES:
                raise TokenRevokedError(
                    error_message,
                    error_code=str(error_code),
                    error_subcode=error_subcode,
                )
            # "Cannot parse access token" means the token value itself is
            # wrong — not expired, but corrupt or invalidated server-side.
            # Refresh will also fail; only a full OAuth re-auth fixes this.
            if any(p in error_message.lower() for p in _TOKEN_CORRUPT_PHRASES):
                raise TokenCorruptError(
                    error_message,
                    error_code=str(error_code),
                    error_subcode=error_subcode,
                )
            raise TokenExpiredError(
                error_message,
                error_code=str(error_code),
                error_subcode=error_subcode,
            )

        # OAuth errors
        if error_code in (102, 104):
            raise TokenExpiredError(
                f"OAuth error: {error_message}",
                error_code=str(error_code),
            )

        # Media format errors — Meta couldn't parse the file we served
        # from Cloudinary. Caused by HEIC-as-JPG, GIFs (not allowed for
        # Stories), or a transformation failure that returned non-image
        # bytes. Distinct from token errors: a retry won't help; the
        # autopost handler creates a permanent_reject lock for the
        # underlying media_item so we don't keep cycling on it.
        if error_code == 9004:
            raise MediaUnsupportedError(
                error_message,
                error_code=str(error_code),
                error_subcode=error_subcode,
            )

        # General API error
        raise InstagramAPIError(
            error_message,
            error_code=str(error_code) if error_code else None,
            error_subcode=error_subcode,
        )

    def get_rate_limit_remaining(self, chat_settings_id: Optional[str] = None) -> int:
        """
        Best-effort LOCAL estimate of remaining posts over a trailing 24h window.

        Derived from our own posting history and used for STATUS DISPLAYS ONLY
        (health check, settings screen) — it is NOT authoritative. The real
        publishing gate is the async ``get_content_publishing_limit``, which
        reads the account's live quota from Meta's content_publishing_limit
        endpoint.

        Args:
            chat_settings_id: Optional tenant filter for multi-tenant scoping

        Returns:
            Estimated number of posts remaining in the trailing 24h window
        """
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        recent_api_posts = self.history_repo.count_by_method(
            method="instagram_api",
            since=since,
            chat_settings_id=chat_settings_id,
        )
        return max(0, settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK - recent_api_posts)

    async def get_content_publishing_limit(
        self,
        telegram_chat_id: Optional[int] = None,
        token: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> dict:
        """
        Fetch the account's live content-publishing quota from Meta.

        Reads ``GET {graph}/{ig_user_id}/content_publishing_limit``, which
        returns the account's rolling-24h publishing quota and current usage.
        This is the AUTHORITATIVE gate for ``post_story`` (the local
        ``get_rate_limit_remaining`` is only a best-effort display estimate).
        ``post_story`` passes the creds it already resolved; standalone callers
        may omit ``token``/``account_id`` and they are fetched from the active
        account.

        FAIL-OPEN by design: Meta's server-side 429 (error 4/17 → RateLimitError
        on the actual publish) is the true backstop, so a blip on this monitoring
        endpoint must never block a legitimate post. On any error other than a
        RateLimitError — missing creds, empty data, network/parse failure — we
        return a permissive fallback. A RateLimitError from the endpoint itself
        means Meta says the account is limited, so we report remaining=0.

        Returns:
            dict with ``quota_total``, ``quota_usage``, ``remaining``, and
            ``source`` ("meta" when the value came from Meta, else "fallback").
        """
        fallback = settings.INSTAGRAM_PUBLISH_LIMIT_FALLBACK

        def _limit(quota_total: int, quota_usage: int, source: str) -> dict:
            return {
                "quota_total": quota_total,
                "quota_usage": quota_usage,
                "remaining": max(0, quota_total - quota_usage),
                "source": source,
            }

        if token is None or account_id is None:
            chat_id = telegram_chat_id or settings.ADMIN_TELEGRAM_CHAT_ID
            token, account_id, _ = self._get_active_account_credentials(chat_id)
        if not token or not account_id:
            logger.warning(
                "content_publishing_limit: no active Instagram credentials; "
                "using permissive fallback"
            )
            return _limit(fallback, 0, "fallback")

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{settings.meta_ig_graph_base}/{account_id}"
                    "/content_publishing_limit",
                    params={
                        "fields": "config,quota_usage",
                        "access_token": token,
                    },
                    timeout=10.0,
                )

                self._check_response_errors(response)

                rows = response.json().get("data", [])
                if not rows:
                    logger.warning(
                        "content_publishing_limit: empty data from Meta; "
                        "using permissive fallback"
                    )
                    return _limit(fallback, 0, "fallback")

                row = rows[0]
                config = row.get("config", {})
                quota_total = int(config.get("quota_total", fallback))
                quota_usage = int(row.get("quota_usage", 0))
                return _limit(quota_total, quota_usage, "meta")
        except RateLimitError:
            # Meta itself says the account is limited — report fully consumed.
            logger.warning(
                "content_publishing_limit: Meta reports rate limited; remaining=0"
            )
            return _limit(fallback, fallback, "meta")
        except Exception as e:  # noqa: BLE001 — monitoring blip must not block posting
            logger.warning(
                f"content_publishing_limit: fetch failed ({e}); "
                "using permissive fallback"
            )
            return _limit(fallback, 0, "fallback")

    async def validate_media_url(self, url: str) -> dict:
        """Validate that a media URL is accessible. Delegates to credentials."""
        return await self.credentials.validate_media_url(url)

    def is_configured(self, telegram_chat_id: Optional[int] = None) -> bool:
        """Check if Instagram API is properly configured. Delegates to credentials."""
        return self.credentials.is_configured(telegram_chat_id)

    def validate_instagram_account_id(self) -> dict:
        """Validate that the account ID is configured. Delegates to credentials."""
        return self.credentials.validate_instagram_account_id()

    async def get_account_info(self, telegram_chat_id: Optional[int] = None) -> dict:
        """Fetch Instagram account info. Delegates to credentials."""
        return await self.credentials.get_account_info(telegram_chat_id)

    def safety_check_before_post(self, telegram_chat_id: Optional[int] = None) -> dict:
        """CRITICAL SAFETY GATE: Run all safety checks. Delegates to credentials."""
        return self.credentials.safety_check_before_post(telegram_chat_id)
