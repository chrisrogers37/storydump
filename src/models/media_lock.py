"""Media posting lock model - TTL-based repost prevention."""

from sqlalchemy import CheckConstraint, Column, String, DateTime, Index, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import ForeignKey
from datetime import datetime
import uuid

from src.config.database import Base


class MediaPostingLock(Base):
    """
    Media posting lock model.

    TTL-based locks to prevent premature reposts.
    Locks automatically expire (no manual deletion needed).
    """

    __tablename__ = "media_posting_locks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Lock details
    locked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    locked_until = Column(
        DateTime, nullable=True, index=True
    )  # TTL: locked_at + X days, NULL = permanent
    lock_reason = Column(
        String(100), default="recent_post"
    )  # 'recent_post', 'manual_hold', 'seasonal', 'permanent_reject'

    # Who created the lock
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    # Multi-tenant: which chat owns this lock (NULL = legacy single-tenant)
    chat_settings_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_settings.id"),
        nullable=True,
        index=True,
    )

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # One permanent lock per media item — the partial unique from
    # migration 021, expressed as a partial Index (UniqueConstraint cannot
    # carry a WHERE clause; Index can). The old unique_active_lock_per_tenant
    # constraint was broken for NULL locked_until (NULL != NULL in SQL).
    __table_args__ = (
        Index(
            "unique_permanent_lock_per_media",
            "media_item_id",
            unique=True,
            postgresql_where=text("locked_until IS NULL"),
        ),
        CheckConstraint(
            "lock_reason IN ('recent_post', 'skip', 'manual_hold', 'seasonal', 'permanent_reject')",
            name="check_lock_reason",
        ),
    )

    def __repr__(self):
        return f"<MediaPostingLock {self.media_item_id} until {self.locked_until}>"
