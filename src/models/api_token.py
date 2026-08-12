"""API token model for OAuth token storage."""

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import (
    Column,
    String,
    DateTime,
    Text,
    UniqueConstraint,
    ForeignKey,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import relationship

from src.config.database import Base
from src.utils.datetime_utils import ensure_utc


class ApiToken(Base):
    """
    API token model for storing OAuth tokens for external services.

    Tokens are encrypted at the application level before storage.
    Supports multiple token types per service (access_token, refresh_token).

    Lifecycle:
    - Initial token created via CLI auth flow or .env bootstrap
    - Tokens refreshed automatically before expiry
    - Old tokens overwritten (UPSERT pattern via unique constraint)
    """

    __tablename__ = "api_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Token identification
    service_name = Column(
        String(50), nullable=False, index=True
    )  # 'instagram', 'shopify'
    token_type = Column(String(50), nullable=False)  # 'access_token', 'refresh_token'

    # Link to Instagram account (NULL for non-Instagram services)
    instagram_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("instagram_accounts.id"),
        nullable=True,
        index=True,
    )

    # Meta-side identifier that issued this token (Business Account ID
    # for FB Login, Instagram User ID for IG Login).  Nullable during
    # the dual-write migration window; backfilled in phase 3.
    meta_account_id = Column(String(100), nullable=True, index=True)

    # Which OAuth flow / Meta app issued this token. Values today:
    # 'instagram_login' (graph.instagram.com), 'fb_login' (legacy,
    # graph.facebook.com), 'manual' (admin-pasted). Lives here rather
    # than on instagram_accounts because it's a property of the
    # credential, not the identity — an account can hold tokens from
    # multiple flows simultaneously (per #380 acceptance criteria).
    # Nullable during the dual-write migration window (#468); backfilled
    # by migration 039.
    auth_method = Column(String(50), nullable=True, index=True)

    # Which Meta App ID issued this token (explicit, not env-derived
    # at use time). Eliminates env-var drift as a failure mode and
    # gives an audit trail of which app produced which credential.
    issuing_app_id = Column(String(100), nullable=True)

    # Owning tenant — the chat that connected this credential. NULL =
    # legacy single-tenant rows, owned by the deployment's env chat.
    # Feeds the account-ownership predicate; Drive tokens also use it
    # as part of their upsert identity.
    chat_settings_id = Column(
        UUID(as_uuid=True),
        ForeignKey("chat_settings.id"),
        nullable=True,
        index=True,
    )

    # Token data (encrypted at application level)
    token_value = Column(Text, nullable=False)

    # Lifecycle tracking
    issued_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=True, index=True)  # NULL = never expires
    last_refreshed_at = Column(DateTime, nullable=True)

    # OAuth metadata
    scopes = Column(ARRAY(Text), nullable=True)  # Array of granted scopes
    token_metadata = Column(
        JSONB, nullable=True
    )  # Service-specific data (e.g., account_id)

    # Revocation (set when token is compromised; filtered out of all queries)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    # Audit timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship to InstagramAccount
    instagram_account = relationship("InstagramAccount", back_populates="tokens")

    __table_args__ = (
        # Partial unique mirrored from migration 015 (parity gate): one
        # Google Drive token per chat.
        Index(
            "unique_google_drive_token_per_chat",
            "service_name",
            "token_type",
            "chat_settings_id",
            unique=True,
            postgresql_where=text(
                "service_name = 'google_drive' AND chat_settings_id IS NOT NULL"
            ),
        ),
        # One token per (service, type, account, auth_method) — the
        # auth_method dimension lets a single account hold both an
        # instagram_login token AND an fb_login token simultaneously
        # (#380 acceptance criteria). Constraint name matches
        # migration 040 (`unique_credential_per_account`); the old
        # `unique_service_token_type_account` is dropped by that
        # migration.
        UniqueConstraint(
            "service_name",
            "token_type",
            "instagram_account_id",
            "auth_method",
            name="unique_credential_per_account",
        ),
    )

    def __repr__(self):
        expires_info = f"expires {self.expires_at}" if self.expires_at else "no expiry"
        revoked = " REVOKED" if self.revoked_at else ""
        return f"<ApiToken {self.service_name}/{self.token_type} ({expires_info}{revoked})>"

    @property
    def is_revoked(self) -> bool:
        """Check if the token has been revoked."""
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        """Check if the token has expired."""
        expires_at = ensure_utc(self.expires_at)
        if expires_at is None:
            return False
        return datetime.now(timezone.utc) > expires_at

    def hours_until_expiry(self) -> Optional[float]:
        """Get hours until token expires, or None if no expiry."""
        expires_at = ensure_utc(self.expires_at)
        if expires_at is None:
            return None
        delta = expires_at - datetime.now(timezone.utc)
        return max(0, delta.total_seconds() / 3600)
