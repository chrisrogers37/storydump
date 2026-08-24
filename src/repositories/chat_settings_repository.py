"""Chat settings repository - CRUD operations for runtime settings."""

from typing import Optional, List
from datetime import datetime

from sqlalchemy import or_

from src.repositories.base_repository import BaseRepository
from src.repositories.tenant_scope import (
    SystemScope,
    TenantScope,
    require_tenant_context,
)
from src.exceptions.tenancy import TenantProvisioningError, TenantResolutionError
from src.models.chat_settings import ChatSettings
from src.models.posting_queue import PostingQueue
from src.models.user import User
from src.models.user_chat_membership import UserChatMembership
from src.models.user_interaction import UserInteraction
from src.config import defaults
from src.utils.logger import logger


def _mint_race_window() -> None:
    """Test seam: the instant between the personal-tenant lookup and its mint.

    A no-op in production. The concurrency suite replaces it with a barrier so
    "two requests inside the lookup->mint window" is a constructed fact rather
    than a timing hope — which is what lets the suite prove the FOR UPDATE
    serialization deterministically (removed lock -> both threads enter ->
    double mint -> red).
    """


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

    def get_by_id(
        self, settings_id: str, *, chat_settings_id: TenantScope
    ) -> Optional[ChatSettings]:
        """Get a tenant row by primary key, scoped to the acting tenant (#512).

        The parameter names follow every other repository — ``chat_settings_id``
        is the ACTING scope, as in
        ``MediaRepository.get_by_id(media_id, chat_settings_id=...)`` — and
        ``settings_id`` is the row being asked for. Keeping one meaning for
        ``chat_settings_id`` matters more here than the local convenience of
        reusing it for the target, because a name that means the scope in nine
        repositories and the target in the tenth is a fork waiting to be
        misread.

        **The rule is identity, not a filter, and that is a property of this
        table rather than a shortcut.** A ``ChatSettings`` row IS its own
        tenant: there is no ``chat_settings_id`` column to filter on, so
        ``_apply_tenant_filter`` cannot express the boundary here and the
        entitlement is "the acting tenant is the row being asked for".

        Fail-closed (F.1/#841): absent context raises rather than widening.
        ``SYSTEM_SCOPE`` reads any tenant, for callers dereferencing a
        ``chat_settings_id`` foreign key off a row they already hold. Any other
        scope reads only itself, and a mismatch returns ``None`` — deliberately
        the same answer as a row that does not exist, so the caller learns
        nothing about tenants it is not entitled to. That is the convention
        ``QueueRepository.delete`` already states for the write side.

        The mismatch refusal happens before the session is touched, so a
        refused call never checks out a connection — the property
        ``_tenant_query`` documents for the filtered path.
        """
        require_tenant_context(
            chat_settings_id, where="ChatSettingsRepository.get_by_id"
        )
        if not isinstance(chat_settings_id, SystemScope) and str(
            chat_settings_id
        ) != str(settings_id):
            return None
        result = (
            self.db.query(ChatSettings).filter(ChatSettings.id == settings_id).first()
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
        """Get settings for a specific chat. Refuses a null chat id.

        THE GUARD BELOW IS LOAD-BEARING AND ITS REASONING IS COUNTERINTUITIVE.
        Read this before deleting it; the obvious argument for deleting it is
        correct about SQL and wrong about this code.

        Since 064, ``chat_settings.telegram_chat_id`` is NULLABLE, so rows with
        a null binding exist. Passing ``None`` here must not return one.

        The tempting argument is that ``= NULL`` matches zero rows in SQL, so a
        null input is harmless. That is TRUE OF RAW SQL AND FALSE HERE: the ORM
        does not emit ``= NULL``. SQLAlchemy compiles
        ``ChatSettings.telegram_chat_id == None`` to ``telegram_chat_id IS
        NULL`` (measured on this project's SQLAlchemy 2.0.49), which matches
        EVERY unbound row, and ``.first()`` then returns an arbitrary one --
        another tenant's.

        AND THE CALLER'S REFUSAL CANNOT CATCH IT. ``require_by_chat_id`` above
        raises only when the RESULT is None; a row was found, so it returns it.
        The refusal is keyed on "no row", and a null input now finds one. That
        is why the check has to be on the INPUT and cannot be moved to the
        result.

        Before 064 the ``NOT NULL`` constraint made this unreachable -- nothing
        could match ``IS NULL``. Dropping it is what arms the defect, which is
        why the guard ships in the same change.

        Raises ``ValueError`` rather than ``TenantResolutionError``: a null chat
        id is a caller bug, not a tenancy outcome, and it should surface as a
        loud 500 rather than blend into the ordinary 403 an unknown chat gets.
        ``TenantResolutionError``'s vocabulary is closed and has no member that
        means "the caller passed nothing".
        """
        if telegram_chat_id is None:
            raise ValueError(
                "get_by_chat_id received a null telegram_chat_id; "
                "an unbound workspace must be resolved by chat_settings.id"
            )
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
        if telegram_chat_id is None:
            # SQLAlchemy compiles `== None` to IS NULL, so with the column
            # nullable a None here would MATCH an arbitrary personal tenant
            # (or mint a fresh one when none exists) — steal-or-create, never
            # get-or-create. Personal (chat-less) tenants have their own door.
            raise ValueError(
                "get_or_create is keyed on telegram_chat_id; for a personal "
                "(NULL-chat) tenant use get_or_create_personal(user_id)"
            )
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
        chat_settings = self._new_chat_settings(
            telegram_chat_id=telegram_chat_id, onboarding_completed=True
        )
        self.db.add(chat_settings)
        self.commit_and_refresh(chat_settings)
        return chat_settings

    def _new_chat_settings(
        self, *, telegram_chat_id: Optional[int], onboarding_completed: bool
    ) -> ChatSettings:
        """The one bootstrap-defaults constructor both mint doors share."""
        return ChatSettings(
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
            onboarding_completed=onboarding_completed,
        )

    def get_or_create_personal(self, user_id: str) -> ChatSettings:
        """Get-or-mint the user's personal (NULL-chat) tenant, atomically.

        The user_id-keyed provisioning door (tenant-anchor doc §9): a web-only
        user has no telegram_chat_id, so ``get_or_create``'s identity key does
        not exist for them and the unique constraint that deduplicates chat
        tenants is NULLS-DISTINCT — nothing at the schema level prevents two
        mints. Serialization is therefore this method's job:

        - ``FOR UPDATE`` on the user row is the per-user serialization point:
          a concurrent call blocks there until the first commit, then finds
          the minted tenant. The lock also pins the user against deletion for
          the transaction's life.
        - Tenant + owner membership are minted in ONE transaction on this
          repository's session — the ``migrate_chat_id`` layering precedent:
          a commit between them would strand a tenant no membership reaches.
        - Contract v1: at most one personal tenant per user, enforced by this
          serialization (revisit when multi-workspace arrives with the target
          schema).

        The mint deliberately does NOT mark ``onboarding_completed``: that
        flag means Telegram-posting-ready and feeds ``get_all_active``. The
        sweep exclusion for chat-less tenants is structural there regardless.

        Raises ``TenantProvisioningError("unknown_user")`` when no user row
        exists — provisioning refusal, deliberately not a resolution reason.

        The lock is the WHOLE dedup story, stated so nobody re-adds a net: no
        IntegrityError recovery exists here because none can fire — the
        telegram_chat_id unique is NULLS-DISTINCT (never collides for these
        rows) and racing mints would create distinct chat_settings_id values
        (the membership unique never collides either). A future path that
        mints a personal tenant WITHOUT taking this lock re-opens the race
        the concurrency suite proves; do not weaken the lock on the theory
        that something else catches the conflict — nothing does.
        """

        def _owned_personal_tenant() -> Optional[ChatSettings]:
            return (
                self.db.query(ChatSettings)
                .join(
                    UserChatMembership,
                    UserChatMembership.chat_settings_id == ChatSettings.id,
                )
                .filter(
                    UserChatMembership.user_id == user_id,
                    UserChatMembership.instance_role == "owner",
                    UserChatMembership.is_active == True,  # noqa: E712
                    ChatSettings.telegram_chat_id.is_(None),
                )
                .first()
            )

        # Fast path: the dominant case (tenant already exists) is one
        # read-only query with NO lock, so repeat calls never serialize on
        # the user row.
        existing = _owned_personal_tenant()
        if existing is not None:
            self.end_read_transaction()
            return existing
        # End the read transaction before locking: under REPEATABLE READ the
        # fast-path read would otherwise pin a snapshot that hides a tenant a
        # concurrent call commits while we wait on the lock, and the re-check
        # below must see it.
        self.end_read_transaction()

        locked_user = (
            self.db.query(User.id).filter(User.id == user_id).with_for_update().first()
        )
        if locked_user is None:
            self.end_read_transaction()
            raise TenantProvisioningError("unknown_user", f"no user {user_id}")

        # Re-check under the lock: a concurrent call may have minted between
        # the fast-path miss and lock acquisition.
        existing = _owned_personal_tenant()
        if existing is not None:
            # Commit ends the transaction and releases the user-row lock.
            self.end_read_transaction()
            return existing

        _mint_race_window()

        tenant = self._new_chat_settings(
            telegram_chat_id=None, onboarding_completed=False
        )
        self.db.add(tenant)
        self.db.flush()  # materialize tenant.id for the membership row
        self.db.add(
            UserChatMembership(
                user_id=user_id,
                chat_settings_id=tenant.id,
                instance_role="owner",
            )
        )
        # Raw commit rather than the wrapper: a failed mint must RAISE — the
        # wrapper commit() swallows the error and rolls back, which would hand
        # the caller a phantom tenant with no row behind it. (Pre-DDL
        # production hits the live NOT NULL here and refuses loudly, minting
        # nothing — the intended posture until the relax migration lands.)
        self.db.commit()
        self.db.refresh(tenant)
        self.end_read_transaction()
        return tenant

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
        self.commit_and_refresh(chat_settings)
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
                # The sweep is definitionally the Telegram posting sweep: a
                # personal (NULL-chat) tenant can never be swept, whatever its
                # flags — structural exclusion, not mint-default hygiene.
                ChatSettings.telegram_chat_id.isnot(None),
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
