"""Chat settings repository - CRUD operations for runtime settings."""

from typing import Optional, List
from datetime import datetime

from sqlalchemy import or_

from src.repositories.base_repository import BaseRepository
from src.exceptions.tenancy import TenantResolutionError
from src.models.chat_settings import ChatSettings
from src.models.posting_queue import PostingQueue
from src.models.user_interaction import UserInteraction
from src.config import defaults
from src.utils.logger import logger


class ChatIdMigrationConflict(Exception):
    """A tenant already exists at the new chat id, so the migration cannot be
    applied without choosing which of two tenants survives.

    Raised rather than resolved. By the time this fires, ``get_or_create`` has
    usually already minted a blank tenant at the new id (that is the #743 bug),
    but it may equally be a tenant that has since accumulated real settings.
    Picking a winner silently would be the same data loss this handler exists
    to prevent, so the caller is told and nothing is written.
    """


class ChatSettingsRepository(BaseRepository):
    """
    Repository for ChatSettings CRUD operations.

    First access bootstraps a row from `src.config.defaults` (hardcoded
    starting values). The DB is the runtime source of truth — no env
    fallback after bootstrap.
    """

    def get_by_id(self, chat_settings_id: str) -> Optional[ChatSettings]:
        """Get settings by UUID primary key."""
        result = (
            self.db.query(ChatSettings)
            .filter(ChatSettings.id == chat_settings_id)
            .first()
        )
        self.end_read_transaction()
        return result

    def require_by_chat_id(self, telegram_chat_id: int) -> ChatSettings:
        """Row-or-refuse: the one raise site of the #842 resolution policy.

        A chat with no row refuses typed (``unknown_binding``); nothing is
        ever minted on the way. Service-tier callers go through
        ``SettingsService`` — this primitive exists so repo-composed modules
        (OAuth, account service) share the same single refusal.
        """
        chat_settings = self.get_by_chat_id(telegram_chat_id)
        if chat_settings is None:
            raise TenantResolutionError(
                "unknown_binding", f"no tenant for chat {telegram_chat_id}"
            )
        return chat_settings

    def get_by_chat_id(self, telegram_chat_id: int) -> Optional[ChatSettings]:
        """Get settings for a specific chat."""
        result = (
            self.db.query(ChatSettings)
            .filter(ChatSettings.telegram_chat_id == telegram_chat_id)
            .first()
        )
        self.end_read_transaction()
        return result

    def migrate_chat_id(
        self, old_chat_id: int, new_chat_id: int
    ) -> Optional[ChatSettings]:
        """Re-point a tenant at the chat id Telegram moved it to (#743).

        A group→supergroup migration changes the chat id. Without this, the
        first update from the new id reaches ``get_or_create``, which finds
        nothing and mints a fresh blank tenant — while settings, Instagram
        links, memberships, category mixes and posting history stay attached to
        the dead id, unreachable and with no error raised.

        The repair is small because the schema is already migration-shaped:
        eight tables key on the ``chat_settings_id`` surrogate and survive
        untouched. Only three columns hold the raw id — this row's unique
        anchor, and the two denormalized carriers swept below.

        WHY ALL THREE UPDATES LIVE IN ONE REPOSITORY, against the usual layering
        (services orchestrate, repositories do single-table CRUD): each
        repository owns a ContextVar-scoped Session of its own, so a service
        calling three repositories would run three transactions. A migration
        that committed ``chat_settings`` and then failed would strand the queue
        on a dead id — the exact defect being fixed, in a narrower form. One
        session is the only way this is atomic.

        Idempotent: Telegram delivers the migration update twice, and the
        ``ChatMigrated`` backstop can re-deliver it indefinitely, so a repeat
        call is a no-op rather than an error.

        Returns the migrated tenant, or None when there was nothing to migrate.
        Raises ChatIdMigrationConflict when the new id is already occupied.
        """
        if old_chat_id == new_chat_id:
            return self.get_by_chat_id(new_chat_id)

        existing_old = (
            self.db.query(ChatSettings)
            .filter(ChatSettings.telegram_chat_id == old_chat_id)
            .first()
        )
        existing_new = (
            self.db.query(ChatSettings)
            .filter(ChatSettings.telegram_chat_id == new_chat_id)
            .first()
        )

        if existing_old is None:
            # Already migrated (the duplicate update, or the permanent
            # ChatMigrated backstop firing again), or a chat this bot never held
            # settings for. Either way: do not mint. get_or_create is the only
            # thing allowed to create a tenant.
            self.end_read_transaction()
            return existing_new

        if existing_new is not None:
            raise ChatIdMigrationConflict(
                f"cannot migrate chat {old_chat_id} -> {new_chat_id}: a tenant "
                f"already exists at the new id (chat_settings_id="
                f"{existing_new.id}); refusing rather than choosing which "
                f"tenant survives"
            )

        existing_old.telegram_chat_id = new_chat_id

        # The two raw-id carriers. posting_queue denormalizes the chat id and
        # FILTERS on it, so pending posts strand invisibly otherwise;
        # user_interactions holds the raw id and nothing else tenant-shaped, so
        # analytics fork across the boundary.
        self.db.query(PostingQueue).filter(
            PostingQueue.telegram_chat_id == old_chat_id
        ).update({"telegram_chat_id": new_chat_id}, synchronize_session=False)
        self.db.query(UserInteraction).filter(
            UserInteraction.telegram_chat_id == old_chat_id
        ).update({"telegram_chat_id": new_chat_id}, synchronize_session=False)

        self.commit()
        logger.info(
            "chat id migrated: %s -> %s (chat_settings_id=%s)",
            old_chat_id,
            new_chat_id,
            existing_old.id,
        )
        return existing_old

    def get_or_create(self, telegram_chat_id: int) -> ChatSettings:
        """
        Get settings for chat, creating from .env defaults if not exists.

        This is the primary access method - ensures a record always exists.
        """
        existing = (
            self.db.query(ChatSettings)
            .filter(ChatSettings.telegram_chat_id == telegram_chat_id)
            .first()
        )

        if existing:
            self.end_read_transaction()
            return existing

        # Bootstrap from hardcoded code-level defaults. Mark onboarded so
        # the scheduler's get_all_active() picks the row up — matches the
        # invariant migration 027 backfilled (bootstrapped rows count as
        # deployment-ready, not half-setup).
        chat_settings = ChatSettings(
            telegram_chat_id=telegram_chat_id,
            dry_run_mode=defaults.DEFAULT_DRY_RUN_MODE,
            enable_instagram_api=defaults.DEFAULT_ENABLE_INSTAGRAM_API,
            is_paused=False,
            posts_per_day=defaults.DEFAULT_POSTS_PER_DAY,
            posting_hours_start=defaults.DEFAULT_POSTING_HOURS_START,
            posting_hours_end=defaults.DEFAULT_POSTING_HOURS_END,
            repost_ttl_days=defaults.DEFAULT_REPOST_TTL_DAYS,
            skip_ttl_days=defaults.DEFAULT_SKIP_TTL_DAYS,
            caption_style=defaults.DEFAULT_CAPTION_STYLE,
            send_lifecycle_notifications=defaults.DEFAULT_SEND_LIFECYCLE_NOTIFICATIONS,
            show_verbose_notifications=defaults.DEFAULT_SHOW_VERBOSE_NOTIFICATIONS,
            media_sync_enabled=defaults.DEFAULT_MEDIA_SYNC_ENABLED,
            posting_timezone=defaults.DEFAULT_POSTING_TIMEZONE,
            onboarding_completed=True,
        )
        self.db.add(chat_settings)
        self.db.commit()
        self.db.refresh(chat_settings)
        return chat_settings

    def update(self, telegram_chat_id: int, **kwargs) -> ChatSettings:
        """
        Update settings for a chat.

        Args:
            telegram_chat_id: Chat to update
            **kwargs: Fields to update (dry_run_mode, is_paused, etc.)

        Returns:
            Updated ChatSettings record
        """
        chat_settings = self.get_or_create(telegram_chat_id)

        for key, value in kwargs.items():
            if hasattr(chat_settings, key):
                setattr(chat_settings, key, value)

        chat_settings.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(chat_settings)
        return chat_settings

    def set_paused(
        self, telegram_chat_id: int, is_paused: bool, user_id: Optional[str] = None
    ) -> ChatSettings:
        """
        Set pause state with tracking.

        Args:
            telegram_chat_id: Chat to update
            is_paused: New pause state
            user_id: UUID of user who changed state
        """
        update_data = {
            "is_paused": is_paused,
            "paused_at": datetime.utcnow() if is_paused else None,
            "paused_by_user_id": user_id if is_paused else None,
        }
        return self.update(telegram_chat_id, **update_data)

    def get_all_active(self) -> List[ChatSettings]:
        """Get all eligible active chat settings records.

        Used by the scheduler loop to iterate over all active tenants.
        Returns only records that are:
        - Not paused (is_paused == False)
        - AND have completed onboarding OR have an active Instagram account

        This excludes half-setup test/dev chats that would otherwise
        produce no-op scheduler runs.

        Returns:
            List of active ChatSettings, ordered by created_at
        """
        result = (
            self.db.query(ChatSettings)
            .filter(
                ChatSettings.is_paused == False,  # noqa: E712
                or_(
                    ChatSettings.onboarding_completed == True,  # noqa: E712
                    ChatSettings.active_instagram_account_id.isnot(None),
                ),
            )
            .order_by(ChatSettings.created_at.asc())
            .all()
        )
        self.end_read_transaction()
        return result

    def get_all_sync_enabled(self) -> List[ChatSettings]:
        """Get all chat settings with media sync enabled.

        Used by the media sync loop to iterate over tenants
        that should have their media synced from cloud providers.

        Returns:
            List of ChatSettings where media_sync_enabled=True,
            ordered by created_at
        """
        result = (
            self.db.query(ChatSettings)
            .filter(ChatSettings.media_sync_enabled == True)  # noqa: E712
            .order_by(ChatSettings.created_at.asc())
            .all()
        )
        self.end_read_transaction()
        return result

    def get_all_paused(self) -> List[ChatSettings]:
        """Get all paused chat settings records.

        Used by the scheduler loop to run smart delivery reschedule
        on paused tenants (bumping overdue items +24hr).

        Returns:
            List of paused ChatSettings, ordered by created_at
        """
        result = (
            self.db.query(ChatSettings)
            .filter(ChatSettings.is_paused == True)  # noqa: E712
            .order_by(ChatSettings.created_at.asc())
            .all()
        )
        self.end_read_transaction()
        return result
