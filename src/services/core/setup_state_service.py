"""Setup state service - unified setup status for Telegram and API."""

from datetime import datetime, timedelta, timezone

from src.services.base_service import BaseService
from src.services.core.instagram_account_service import InstagramAccountService
from src.services.core.settings_service import SettingsService
from sqlalchemy.exc import SQLAlchemyError

from src.repositories.history_repository import HistoryRepository
from src.repositories.media_repository import MediaRepository
from src.repositories.queue_repository import QueueRepository
from src.repositories.token_repository import TokenRepository
from src.utils.datetime_utils import ensure_utc
from src.utils.logger import logger

# Token is considered stale if expired more than this many days ago
TOKEN_STALE_DAYS = 7


class SetupStateService(BaseService):
    """Check and report setup state for a chat.

    Consolidates setup-checking logic used by both the Telegram bot
    (``/status`` command) and the onboarding API (``/init`` endpoint).
    """

    def __init__(self):
        super().__init__()
        self.settings_service = SettingsService()
        self.ig_account_service = InstagramAccountService()
        self.token_repo = TokenRepository()
        self.media_repo = MediaRepository()
        self.queue_repo = QueueRepository()
        self.history_repo = HistoryRepository()

    def get_setup_state(self, telegram_chat_id: int) -> dict:
        """Build the current setup state for a chat.

        Returns a dict consumed by the onboarding API and convertible
        to Telegram display text via ``format_setup_status()``.
        """
        chat_settings = self.settings_service.require_settings(telegram_chat_id)
        chat_settings_id = str(chat_settings.id)

        instagram = self._check_instagram(telegram_chat_id)
        gdrive = self._check_gdrive(chat_settings_id)
        media = self._check_media(chat_settings, chat_settings_id)
        activity = self._check_activity(chat_settings_id)

        return {
            # Instagram
            "instagram_connected": instagram["connected"],
            "instagram_username": instagram["username"],
            # Google Drive
            "gdrive_connected": gdrive["connected"],
            "gdrive_email": gdrive["email"],
            "gdrive_needs_reconnect": gdrive["needs_reconnect"],
            # Media
            "media_folder_configured": media["folder_configured"],
            "media_folder_id": media["folder_id"],
            "media_indexed": media["indexed"],
            "media_count": media["count"],
            "media_source_type": chat_settings.media_source_type,
            "media_source_root": chat_settings.media_source_root,
            # Schedule
            "posts_per_day": chat_settings.posts_per_day,
            "posting_hours_start": chat_settings.posting_hours_start,
            "posting_hours_end": chat_settings.posting_hours_end,
            "repost_ttl_days": chat_settings.repost_ttl_days,
            "skip_ttl_days": chat_settings.skip_ttl_days,
            "caption_style": chat_settings.caption_style,
            "send_lifecycle_notifications": chat_settings.send_lifecycle_notifications,
            # Onboarding
            "onboarding_completed": chat_settings.onboarding_completed,
            "onboarding_step": chat_settings.onboarding_step,
            # Delivery
            "is_paused": chat_settings.is_paused,
            "dry_run_mode": chat_settings.dry_run_mode,
            "enable_instagram_api": chat_settings.enable_instagram_api,
            "show_verbose_notifications": chat_settings.show_verbose_notifications,
            "media_sync_enabled": chat_settings.media_sync_enabled,
            "enable_ai_captions": chat_settings.enable_ai_captions,
            # Activity
            "in_flight_count": activity["in_flight_count"],
            "last_post_at": activity["last_post_at"],
            "posting_active": activity["posting_active"],
        }

    def format_setup_status(self, telegram_chat_id: int) -> str:
        """Build a Telegram-formatted setup status section.

        Returns Markdown text with tree-style layout suitable for
        ``/status`` output.
        """
        state = self.get_setup_state(telegram_chat_id)

        lines = ["*Setup Status:*"]
        checks = [
            self._fmt_instagram(state),
            self._fmt_gdrive(state),
            self._fmt_media(state),
            self._fmt_schedule(state),
            self._fmt_delivery(state),
        ]

        missing = 0
        for line_text, is_configured in checks:
            lines.append(line_text)
            if not is_configured:
                missing += 1

        if missing > 0:
            lines.append("\n_Use /start to configure missing items._")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal data-gathering helpers
    # ------------------------------------------------------------------

    def _check_instagram(self, telegram_chat_id: int) -> dict:
        try:
            active = self.ig_account_service.get_active_account(telegram_chat_id)
            if active:
                return {
                    "connected": True,
                    "username": active.instagram_username,
                    "display_name": active.display_name,
                }
        except Exception as e:  # noqa: BLE001 — best-effort status check
            logger.debug(f"Instagram setup check failed: {e}")
        return {"connected": False, "username": None, "display_name": None}

    def _check_gdrive(self, chat_settings_id: str) -> dict:
        try:
            token = self.token_repo.get_token_for_chat(
                "google_drive", "oauth_access", chat_settings_id
            )
            if token:
                email = None
                if token.token_metadata:
                    email = token.token_metadata.get("email")
                needs_reconnect = is_token_stale(token)
                return {
                    "connected": True,
                    "email": email,
                    "needs_reconnect": needs_reconnect,
                }
        except Exception as e:  # noqa: BLE001 — best-effort status check
            logger.debug(f"Google Drive setup check failed: {e}")
        return {"connected": False, "email": None, "needs_reconnect": False}

    def _check_media(self, chat_settings, chat_settings_id: str) -> dict:
        folder_configured = bool(chat_settings.media_source_root)
        folder_id = chat_settings.media_source_root
        count = 0
        indexed = False

        try:
            # The library count is the tenant's active media across ALL
            # source types (#877) — the same universe the dashboard card's
            # body renders, so badge and body cannot disagree. It is also
            # not gated on a configured folder: uploads exist without one.
            count = self.media_repo.count_active(chat_settings_id)
            indexed = count > 0
        except Exception as e:  # noqa: BLE001 — best-effort status check
            logger.debug(f"Media setup check failed: {e}")

        return {
            "folder_configured": folder_configured,
            "folder_id": folder_id,
            "count": count,
            "indexed": indexed,
        }

    def _check_activity(self, chat_settings_id: str) -> dict:
        in_flight_count = 0
        last_post_at = None
        posting_active = False
        try:
            pending_items = self.queue_repo.get_all(
                status="pending", chat_settings_id=chat_settings_id
            )
            processing_items = self.queue_repo.get_all(
                status="processing", chat_settings_id=chat_settings_id
            )
            in_flight_count = len(pending_items) + len(processing_items)
            recent_posts = self.history_repo.get_recent_posts(
                hours=720, chat_settings_id=chat_settings_id
            )
            if recent_posts:
                posted_at = ensure_utc(recent_posts[0].posted_at)
                last_post_at = posted_at.isoformat()
                # Consider posting active if last post was within 48 hours
                age = datetime.now(timezone.utc) - posted_at
                posting_active = age < timedelta(hours=48)
        except SQLAlchemyError:
            # Expected here: transient database trouble. Best-effort by design —
            # a status field must degrade rather than break the dashboard.
            logger.debug("Failed to fetch queue/history for setup state")
        except Exception:  # noqa: BLE001 — see below
            # Deliberately still broad, for the same reason: this must not break
            # the dashboard. What changed is the VOLUME. Anything that is not a
            # database error is a defect in this block, and #918 is what silence
            # costs — an aware/naive TypeError kept posting_active permanently
            # False for every tenant that had ever posted, with no crash and
            # nothing above debug. Breadth was never the problem; a swallowed
            # programming error presenting as data was.
            logger.exception("Unexpected error in setup-state activity check")
        return {
            "in_flight_count": in_flight_count,
            "last_post_at": last_post_at,
            "posting_active": posting_active,
        }

    # ------------------------------------------------------------------
    # Telegram-specific formatters
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_instagram(state: dict) -> tuple[str, bool]:
        if state["instagram_connected"]:
            if state["instagram_username"]:
                return (
                    f"├── 📸 Instagram: ✅ Connected (@{state['instagram_username']})",
                    True,
                )
            return ("├── 📸 Instagram: ✅ Connected", True)
        return ("├── 📸 Instagram: ⚠️ Not connected", False)

    @staticmethod
    def _fmt_gdrive(state: dict) -> tuple[str, bool]:
        if state["gdrive_connected"]:
            if state["gdrive_needs_reconnect"]:
                return ("├── 📁 Google Drive: ⚠️ Needs Reconnection", False)
            email = state["gdrive_email"]
            if email:
                return (f"├── 📁 Google Drive: ✅ Connected ({email})", True)
            return ("├── 📁 Google Drive: ✅ Connected", True)
        return ("├── 📁 Google Drive: ⚠️ Not connected", False)

    @staticmethod
    def _fmt_media(state: dict) -> tuple[str, bool]:
        if state["media_count"] > 0:
            return (f"├── 📂 Media Library: ✅ {state['media_count']} files", True)
        if state["media_folder_configured"]:
            return (
                "├── 📂 Media Library: ⚠️ Configured (0 files — run /sync)",
                False,
            )
        return ("├── 📂 Media Library: ⚠️ Not configured", False)

    @staticmethod
    def _fmt_schedule(state: dict) -> tuple[str, bool]:
        ppd = state["posts_per_day"]
        start = state["posting_hours_start"]
        end = state["posting_hours_end"]
        return (
            f"├── 📅 Schedule: ✅ {ppd}/day, {start:02d}:00-{end:02d}:00 UTC",
            True,
        )

    @staticmethod
    def _fmt_delivery(state: dict) -> tuple[str, bool]:
        if state["is_paused"]:
            return ("└── 📦 Delivery: ⏸️ PAUSED", True)
        if state["dry_run_mode"]:
            return ("└── 📦 Delivery: 🧪 Dry Run (not posting)", True)
        return ("└── 📦 Delivery: ✅ Live", True)


def is_token_stale(token) -> bool:
    """Check if an OAuth token is stale (expired > TOKEN_STALE_DAYS ago).

    Shared utility used by SetupStateService and available for import
    elsewhere to avoid duplicating the staleness heuristic.
    """
    expires_at = ensure_utc(token.expires_at)
    if not expires_at:
        return False
    return expires_at < datetime.now(timezone.utc) - timedelta(days=TOKEN_STALE_DAYS)
