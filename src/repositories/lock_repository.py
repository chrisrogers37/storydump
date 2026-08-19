"""Media lock repository - CRUD operations for media locks."""

from typing import Optional, List
from datetime import datetime, timedelta

from sqlalchemy import func

from src.repositories.base_repository import BaseRepository
from src.repositories.tenant_scope import (
    SYSTEM_SCOPE,
    TenantScope,
    require_tenant_context,
    tenant_value,
)
from src.models.media_lock import MediaPostingLock


class LockRepository(BaseRepository):
    """Repository for MediaPostingLock CRUD operations."""

    def __init__(self):
        super().__init__()

    def get_by_id(
        self, lock_id: str, chat_settings_id: TenantScope
    ) -> Optional[MediaPostingLock]:
        """Get lock by ID."""
        require_tenant_context(chat_settings_id, where="lock.get_by_id")
        result = (
            self._tenant_query(MediaPostingLock, chat_settings_id)
            .filter(MediaPostingLock.id == lock_id)
            .first()
        )
        self.end_read_transaction()
        return result

    def get_active_lock(
        self, media_id: str, chat_settings_id: TenantScope
    ) -> Optional[MediaPostingLock]:
        """Get active lock for media item (if any)."""
        require_tenant_context(chat_settings_id, where="lock.get_active_lock")
        now = datetime.utcnow()
        result = (
            self._tenant_query(MediaPostingLock, chat_settings_id)
            .filter(
                MediaPostingLock.media_item_id == media_id,
                # Lock is active if: locked_until is NULL (permanent) OR locked_until > now
                (MediaPostingLock.locked_until.is_(None))
                | (MediaPostingLock.locked_until > now),
            )
            .first()
        )
        self.end_read_transaction()
        return result

    def is_locked(self, media_id: str, chat_settings_id: TenantScope) -> bool:
        """Check if media item is currently locked."""
        require_tenant_context(chat_settings_id, where="lock.is_locked")
        return self.get_active_lock(media_id, chat_settings_id) is not None

    def get_all_active(self, chat_settings_id: TenantScope) -> List[MediaPostingLock]:
        """Get all active locks."""
        require_tenant_context(chat_settings_id, where="lock.get_all_active")
        now = datetime.utcnow()
        result = (
            self._tenant_query(MediaPostingLock, chat_settings_id)
            .filter(
                (MediaPostingLock.locked_until.is_(None))
                | (MediaPostingLock.locked_until > now)
            )
            .order_by(MediaPostingLock.locked_until.asc().nulls_last())
            .all()
        )
        self.end_read_transaction()
        return result

    def create(
        self,
        media_item_id: str,
        ttl_days: Optional[int],
        lock_reason: str = "recent_post",
        created_by_user_id: Optional[str] = None,
        *,
        chat_settings_id: TenantScope,
    ) -> MediaPostingLock:
        """Create a new TTL lock. If ttl_days is None, creates permanent lock."""
        require_tenant_context(chat_settings_id, where="lock.create")
        if ttl_days is None:
            locked_until = None  # Permanent lock
        else:
            locked_until = datetime.utcnow() + timedelta(days=ttl_days)

        lock = MediaPostingLock(
            media_item_id=media_item_id,
            locked_until=locked_until,
            lock_reason=lock_reason,
            created_by_user_id=created_by_user_id,
            chat_settings_id=tenant_value(chat_settings_id),
        )
        self.db.add(lock)
        self.db.commit()
        self.db.refresh(lock)
        return lock

    def delete(self, lock_id: str) -> bool:
        """Delete a lock."""
        lock = self.get_by_id(lock_id, chat_settings_id=SYSTEM_SCOPE)
        if lock:
            self.db.delete(lock)
            self.db.commit()
            return True
        return False

    def get_permanent_locks(
        self, chat_settings_id: TenantScope
    ) -> List[MediaPostingLock]:
        """Get all permanent locks (locked_until IS NULL)."""
        require_tenant_context(chat_settings_id, where="lock.get_permanent_locks")
        result = (
            self._tenant_query(MediaPostingLock, chat_settings_id)
            .filter(MediaPostingLock.locked_until.is_(None))
            .order_by(MediaPostingLock.created_at.desc())
            .all()
        )
        self.end_read_transaction()
        return result

    def count_permanent_locks(self, chat_settings_id: TenantScope) -> int:
        """Count permanent locks (locked_until IS NULL)."""
        require_tenant_context(chat_settings_id, where="lock.count_permanent_locks")
        result = (
            self._tenant_query(MediaPostingLock, chat_settings_id)
            .with_entities(func.count(MediaPostingLock.id))
            .filter(MediaPostingLock.locked_until.is_(None))
            .scalar()
        )
        self.end_read_transaction()
        return result or 0

    def count_by_reason(self, chat_settings_id: TenantScope) -> dict:
        """Count active locks grouped by lock_reason."""
        require_tenant_context(chat_settings_id, where="lock.count_by_reason")
        now = datetime.utcnow()
        rows = (
            self._tenant_query(MediaPostingLock, chat_settings_id)
            .with_entities(
                MediaPostingLock.lock_reason,
                func.count(MediaPostingLock.id),
            )
            .filter(
                (MediaPostingLock.locked_until.is_(None))
                | (MediaPostingLock.locked_until > now)
            )
            .group_by(MediaPostingLock.lock_reason)
            .all()
        )
        self.end_read_transaction()
        return {reason: count for reason, count in rows}

    def cleanup_expired(self, chat_settings_id: TenantScope) -> int:
        """Delete all expired locks. Returns count of deleted locks."""
        require_tenant_context(chat_settings_id, where="lock.cleanup_expired")
        now = datetime.utcnow()
        count = (
            self._tenant_query(MediaPostingLock, chat_settings_id)
            .filter(
                MediaPostingLock.locked_until.isnot(
                    None
                ),  # Don't delete permanent locks
                MediaPostingLock.locked_until <= now,
            )
            .delete()
        )
        self.db.commit()
        return count
