"""Posting queue model - active work items only."""

from sqlalchemy import (
    Column,
    String,
    BigInteger,
    DateTime,
    CheckConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import ForeignKey
from datetime import datetime
import uuid

from src.config.database import Base
from src.models.enums import QueueStatus, sql_in_list


class PostingQueue(Base):
    """
    Posting queue model.

    Ephemeral table for active work items.
    Items are deleted after completion and moved to posting_history.
    """

    __tablename__ = "posting_queue"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("media_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    scheduled_for = Column(DateTime, nullable=False, index=True)
    status = Column(
        String(50), default="pending", nullable=False, index=True
    )  # allowed values: QueueStatus enum (src/models/enums.py)

    # Instagram claim-before-publish anchor. Set (with status='publishing')
    # the instant the IG media container is created and BEFORE the publish
    # call, so a crash after publish leaves a recoverable, non-reapable row
    # instead of re-serving the media and duplicating the story.
    instagram_container_id = Column(String(255))

    # Telegram tracking (for manual posts)
    telegram_message_id = Column(BigInteger)
    telegram_chat_id = Column(BigInteger)

    # Multi-tenant: which chat owns this queue item (NULL = legacy single-tenant)
    chat_settings_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_settings.id"),
        nullable=True,
        index=True,
    )

    # Timestamps (preserved in history)
    created_at = Column(DateTime, default=datetime.utcnow)

    # CHECK constraint is DERIVED from the code-owned QueueStatus enum (no
    # hand-typed value list) — the enum is the single source of truth; the CI
    # parity gate (tests/src/models/test_enum_ssot_parity.py) fails on any drift.
    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_in_list(QueueStatus)})",
            name="check_status",
        ),
    )

    def __repr__(self):
        return f"<PostingQueue {self.id} ({self.status}) scheduled for {self.scheduled_for}>"
