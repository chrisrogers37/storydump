"""Posting history model - permanent audit log."""

from sqlalchemy import Column, String, DateTime, Text, Boolean, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import ForeignKey
from datetime import datetime
import uuid

from src.config.database import Base
from src.models.enums import HistoryStatus, PostingMethod, sql_in_list


class PostingHistory(Base):
    """
    Posting history model.

    Permanent audit log - never deleted.
    Preserves complete record of all posting attempts.
    """

    __tablename__ = "posting_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_item_id = Column(
        UUID(as_uuid=True), ForeignKey("media_items.id"), nullable=False, index=True
    )
    queue_item_id = Column(
        UUID(as_uuid=True)
    )  # Link back to queue (nullable after queue cleanup)

    # Queue lifecycle timestamps (preserved from posting_queue)
    queue_created_at = Column(DateTime, nullable=False)  # When item was added to queue
    queue_deleted_at = Column(
        DateTime, nullable=False
    )  # When item was removed from queue
    scheduled_for = Column(
        DateTime, nullable=False, index=True
    )  # When the queue item was created (JIT: same as sent time)

    # Posting outcome
    posted_at = Column(DateTime, nullable=False, index=True)
    status = Column(
        String(50), nullable=False
    )  # allowed values: HistoryStatus enum (src/models/enums.py)
    success = Column(Boolean, nullable=False)

    # Instagram result (if successful)
    instagram_media_id = Column(Text)
    instagram_permalink = Column(Text)
    instagram_story_id = Column(Text)  # Story ID from Meta Graph API

    # Posting method tracking (Phase 2)
    posting_method = Column(
        String(20), default=PostingMethod.TELEGRAM_MANUAL.value
    )  # allowed values: PostingMethod enum (src/models/enums.py)

    # User tracking
    posted_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    posted_by_telegram_username = Column(Text)  # Snapshot of username at posting time

    # Multi-tenant: which chat owns this history record (NULL = legacy single-tenant)
    chat_settings_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_settings.id"),
        nullable=True,
        index=True,
    )

    # Error details (for failed posts)
    error_message = Column(Text, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)

    # CHECK constraints are DERIVED from the code-owned enums (no hand-typed
    # value lists) — the enum is the single source of truth; a CI parity gate
    # (tests/src/models/test_enum_ssot_parity.py) fails on any drift.
    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(HistoryStatus)})",
            name="check_history_status",
        ),
        CheckConstraint(
            f"posting_method IN ({sql_in_list(PostingMethod)})",
            name="check_posting_method",
        ),
    )

    def __repr__(self):
        return f"<PostingHistory {self.id} ({self.status}) posted at {self.posted_at}>"
