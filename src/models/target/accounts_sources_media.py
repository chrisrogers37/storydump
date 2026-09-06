"""Plan 02 §2 — the accounts, sources and media models (F.2.3, migration 054).

The mirror of `054_accounts_sources_media_tables.sql`, on the same terms
`identity_and_tenancy` states: these are not an independent design, they are
the second side of the `04` §0.2 parity gate. `scripts/schema_parity.py`
requires the two sides equal on tables, columns (type + nullability), CHECK
constraints (by name AND normalized definition), uniqueness semantics and
foreign keys.

**Triggers and RLS are deliberately absent**, same as F.2.2: the comparator is
relation-scoped and reads neither, so restating the six `tg_touch_*` triggers
here would be an unchecked second copy. The database owns `updated_at`.

**Composite foreign keys are the shape to notice.** Four of the six FKs in this
increment are composite `(workspace_id, <id>)` references rather than plain
single-column ones — `oauth_credentials` to both `ig_accounts` and
`media_sources`, `media_items` to `media_sources`, and `post_locks` to both
`media_items` and `ig_accounts`. That is the plan's tenant-integrity device: a
composite FK makes a cross-workspace row unrepresentable at the schema level
rather than only forbidden by policy, which is why each parent carries a
`UNIQUE (workspace_id, id)` that exists solely to be their target. Rendering
one as a single-column FK would still create a working schema and would be a
red parity test, which is the point.

**`provider_quarantine` has no surrogate key** — its primary key is the
composite `(workspace_id, scope_ref)`, so it is the one table here that does
not call `pk()`. Its `workspace_id` is both the tenant key and half the PK.
"""

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.target.base import TargetBase
from src.models.target.columns import TZ, fk, pk, timestamps


class IgAccount(TargetBase):
    """A connected Instagram account (§2). The schedule lives HERE, not on the
    workspace: a workspace's accounts each carry their own cadence, window, tz
    and slot cursor, and the workspace columns are only the defaults.

    The same real account in two workspaces is two rows (fork PA-1, default (a)
    independent-connections). `moved` rows are terminal tombstones excluded
    from `uq_ig_account_live`, so an account can move away and later return.
    """

    __tablename__ = "ig_accounts"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    provider_account_ref = Column(Text, nullable=False)
    handle = Column(String(50), nullable=True)
    display_name = Column(String(100), nullable=True)
    state = Column(Text, nullable=False, server_default=text("'active'"))
    posts_per_day = Column(Integer, nullable=True)
    posting_hours_start = Column(Integer, nullable=True)
    posting_hours_end = Column(Integer, nullable=True)
    tz = Column(Text, nullable=True)
    next_slot_at = Column(TZ, nullable=True)
    last_posted_at = Column(TZ, nullable=True)
    # When the reauth-prompt clock leg last minted for this account (062);
    # NULL = never prompted. Stamped at mint, symmetric with next_slot_at.
    last_reauth_prompt_at = Column(TZ, nullable=True)
    # When a slot last found no media for this account and said so (066).
    # NULL = never told. Per-ACCOUNT, not per-workspace: two accounts in one
    # workspace starve independently, so a workspace-keyed marker would let
    # the first one's notice silence the second one's first notice.
    last_no_media_notice_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "state IN ('active','reauth_required','disabled','moved')",
            name="ck_ig_accounts_state",
        ),
        CheckConstraint("posts_per_day BETWEEN 1 AND 50", name="ck_iga_ppd"),
        CheckConstraint("posting_hours_start BETWEEN 0 AND 23", name="ck_iga_hs"),
        CheckConstraint("posting_hours_end BETWEEN 0 AND 23", name="ck_iga_he"),
        # Calls 052's fn_safe_tz. A CHECK naming a missing function is a hard
        # error, so create_all against a database without 052 cannot run.
        CheckConstraint("tz IS NULL OR fn_safe_tz(tz) = tz", name="ck_iga_tz_valid"),
        # Exists to be a composite-FK target, not for its own sake.
        UniqueConstraint("workspace_id", "id", name="uq_ig_accounts_ws_id"),
        Index(
            "ix_ig_accounts_reauth_due",
            "last_reauth_prompt_at",
            postgresql_where=text("state = 'reauth_required'"),
        ),
        Index(
            "uq_ig_account_live",
            "workspace_id",
            "provider_account_ref",
            unique=True,
            postgresql_where=text("state <> 'moved'"),
        ),
        Index(
            "ix_ig_accounts_due",
            "next_slot_at",
            postgresql_where=text("state = 'active' AND next_slot_at IS NOT NULL"),
        ),
    )


class ProviderQuarantine(TargetBase):
    """Account-level fault quarantine (§2), keyed by serialization scope.

    The one table in this increment with no surrogate key: `(workspace_id,
    scope_ref)` IS the primary key, because a scope is quarantined once per
    workspace and there is nothing to reference it by.
    """

    __tablename__ = "provider_quarantine"

    workspace_id = fk("workspaces.id", "CASCADE", nullable=False, primary_key=True)
    provider = Column(Text, nullable=False)
    scope_ref = Column(Text, nullable=False, primary_key=True)
    quarantined_until = Column(TZ, nullable=False)
    strike_count = Column(Integer, nullable=False, server_default=text("1"))
    last_strike_at = Column(TZ, nullable=False, server_default=text("now()"))
    alerted_at = Column(TZ, nullable=True)
    reason = Column(Text, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "provider IN ('ig','telegram','cloudinary','gdrive')",
            name="ck_quarantine_provider",
        ),
    )


class MediaSource(TargetBase):
    """A workspace's media provider connection (§2). `config` carries a version
    discriminator the CHECK requires to be a JSON number."""

    __tablename__ = "media_sources"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    provider = Column(Text, nullable=False)
    config = Column(JSONB, nullable=False)
    sync_checkpoint = Column(JSONB, nullable=True)
    next_sync_at = Column(TZ, nullable=True)
    state = Column(Text, nullable=False, server_default=text("'active'"))
    last_sync_success_at = Column(TZ, nullable=True)
    alerted_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint("provider IN ('gdrive')", name="ck_sources_provider"),
        CheckConstraint(
            "jsonb_typeof(config->'v') = 'number'", name="ck_sources_config_v"
        ),
        CheckConstraint(
            "state IN ('active','paused','error')", name="ck_sources_state"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_sources_ws_id"),
        Index(
            "ix_sources_sync_due",
            "next_sync_at",
            postgresql_where=text("state = 'active' AND next_sync_at IS NOT NULL"),
        ),
    )


class OAuthCredential(TargetBase):
    """One encrypted credential payload. An `ig_login` credential is owned by
    exactly one ig_account; a `gdrive` credential is owned by the WORKSPACE
    (069, #1165 — one Google grant per workspace, folders picked under it) and
    names no owner column. `ck_credentials_one_owner` is provider-conditional,
    and the two partial unique indexes scope liveness per owner kind."""

    __tablename__ = "oauth_credentials"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    # Plain UUID columns: their FKs are the composite constraints below, so a
    # single-column ForeignKey here would be a second, weaker reference.
    ig_account_id = Column(UUID(as_uuid=True), nullable=True)
    media_source_id = Column(UUID(as_uuid=True), nullable=True)
    provider = Column(Text, nullable=False)
    encrypted_payload = Column(Text, nullable=False)
    expires_at = Column(TZ, nullable=True)
    next_refresh_at = Column(TZ, nullable=True)
    state = Column(Text, nullable=False, server_default=text("'active'"))
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "provider IN ('ig_login','gdrive')", name="ck_credentials_provider"
        ),
        CheckConstraint(
            "state IN ('active','expired','revoked')", name="ck_credentials_state"
        ),
        # 069 (#1165): provider-conditional. An `ig_login` credential names its
        # account; a `gdrive` credential names nothing — the workspace is its owner.
        CheckConstraint(
            "CASE provider"
            " WHEN 'ig_login' THEN ig_account_id IS NOT NULL AND media_source_id IS NULL"
            " ELSE ig_account_id IS NULL AND media_source_id IS NULL END",
            name="ck_credentials_one_owner",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "ig_account_id"],
            ["ig_accounts.workspace_id", "ig_accounts.id"],
            ondelete="CASCADE",
            name="fk_credentials_account",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "media_source_id"],
            ["media_sources.workspace_id", "media_sources.id"],
            ondelete="CASCADE",
            name="fk_credentials_source",
        ),
        Index(
            "uq_credential_per_account",
            "workspace_id",
            "ig_account_id",
            "provider",
            unique=True,
            postgresql_where=text("ig_account_id IS NOT NULL"),
        ),
        Index(
            "uq_credential_per_workspace",
            "workspace_id",
            "provider",
            unique=True,
            postgresql_where=text("ig_account_id IS NULL AND media_source_id IS NULL"),
        ),
        Index(
            "ix_credentials_refresh_due",
            "next_refresh_at",
            postgresql_where=text("state = 'active' AND next_refresh_at IS NOT NULL"),
        ),
    )


class MediaItem(TargetBase):
    """One item of source media (§2). `uq_media_dedup` is the content-hash
    dedup key, scoped per workspace so two tenants may hold the same file."""

    __tablename__ = "media_items"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=False)
    provider_file_ref = Column(Text, nullable=False)
    content_hash = Column(Text, nullable=False)
    media_kind = Column(Text, nullable=False)
    mime_type = Column(String(100), nullable=True)
    file_name = Column(Text, nullable=False)
    file_size = Column(BigInteger, nullable=True)
    category = Column(Text, nullable=True)
    title = Column(Text, nullable=True)
    caption = Column(Text, nullable=True)
    generated_caption = Column(Text, nullable=True)
    link_url = Column(Text, nullable=True)
    tags = Column(ARRAY(Text), nullable=True)
    custom_metadata = Column(JSONB, nullable=True)
    thumbnail_url = Column(Text, nullable=True)
    state = Column(Text, nullable=False, server_default=text("'available'"))
    times_posted = Column(Integer, nullable=False, server_default=text("0"))
    last_posted_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint("media_kind IN ('image','video')", name="ck_media_kind"),
        CheckConstraint(
            "state IN ('available','unsupported','removed')", name="ck_media_state"
        ),
        UniqueConstraint("workspace_id", "id", name="uq_media_ws_id"),
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["media_sources.workspace_id", "media_sources.id"],
            ondelete="CASCADE",
            name="fk_media_source",
        ),
        UniqueConstraint("workspace_id", "content_hash", name="uq_media_dedup"),
        Index("ix_media_selection", "workspace_id", "state", "category"),
        Index(
            "ix_media_provider_ref", "workspace_id", "source_id", "provider_file_ref"
        ),
    )


class PostLock(TargetBase):
    """Why an item may not be posted (§2). `ck_locks_recent_scope` ties the
    `recent` kind to account scope with an equivalence — a `recent` lock always
    names an account and every other kind never does — and the two partial
    unique indexes split on the same NULL so the two scopes cannot collide."""

    __tablename__ = "post_locks"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    media_item_id = Column(UUID(as_uuid=True), nullable=False)
    ig_account_id = Column(UUID(as_uuid=True), nullable=True)
    kind = Column(Text, nullable=False)
    expires_at = Column(TZ, nullable=True)
    # No FK: post_intents lands in F.2.4, and a reference to a table that does
    # not exist yet would not be a stream prefix. The plan declares none here.
    created_by_intent_id = Column(UUID(as_uuid=True), nullable=True)
    created_by_user_id = fk("users.id", "SET NULL", nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('recent','skip','reject','unsupported','seasonal','hold')",
            name="ck_locks_kind",
        ),
        CheckConstraint(
            "(kind = 'recent') = (ig_account_id IS NOT NULL)",
            name="ck_locks_recent_scope",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "media_item_id"],
            ["media_items.workspace_id", "media_items.id"],
            ondelete="CASCADE",
            name="fk_locks_media",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "ig_account_id"],
            ["ig_accounts.workspace_id", "ig_accounts.id"],
            ondelete="CASCADE",
            name="fk_locks_account",
        ),
        Index(
            "uq_lock_ws_scope",
            "workspace_id",
            "media_item_id",
            "kind",
            unique=True,
            postgresql_where=text("ig_account_id IS NULL"),
        ),
        Index(
            "uq_lock_acct_scope",
            "workspace_id",
            "media_item_id",
            "kind",
            "ig_account_id",
            unique=True,
            postgresql_where=text("ig_account_id IS NOT NULL"),
        ),
        Index(
            "ix_locks_expiry",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )
