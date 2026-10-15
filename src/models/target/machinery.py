"""The execution machinery table definitions.

`ddl.build()` creates these tables through `TargetBase.metadata`.
`ck_jobs_system_kinds` permits a NULL workspace exactly for system job kinds.
The partial unique index `uq_jobs_serialized_lease` permits one live lease per
serialization key. Outbox and provider-operation intent references carry no
foreign key so delivery evidence can outlive an intent. Functions, triggers
and row policies live in `src/models/target/ddl/`.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.target.base import TargetBase
from src.models.target.columns import TZ, fk, pk, timestamps


class Job(TargetBase):
    """The execution queue (Postgres only). Claim, lease and retire are
    all index-driven: `ix_jobs_claim` finds ready work by lane, and
    `uq_jobs_serialized_lease` is what makes a serialization key exclusive —
    partial on `state = 'leased'`, so exactly one lease per key can exist while
    any number of ready or finished rows share it."""

    __tablename__ = "jobs"

    id = pk()
    kind = Column(Text, nullable=False)
    # Nullable alone in this module — a system sweep has no tenant. The
    # equivalence CHECK below is what keeps that from being a hole.
    workspace_id = fk("workspaces.id", "CASCADE", nullable=True)
    lane = Column(Text, nullable=False)
    serialization_key = Column(Text, nullable=False)
    run_at = Column(TZ, nullable=False, server_default=text("now()"))
    state = Column(Text, nullable=False, server_default=text("'ready'"))
    cancel_requested = Column(Boolean, nullable=False, server_default=text("false"))
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    max_attempts = Column(Integer, nullable=False)
    deadline_at = Column(TZ, nullable=True)
    locked_by = Column(Text, nullable=True)
    locked_until = Column(TZ, nullable=True)
    lease_token = Column(UUID(as_uuid=True), nullable=True)
    # Backslash-escaped colon: `text()` reads `:1` as a bind parameter and would
    # emit `DEFAULT '{"v"NULL}'`, so the table would not create. Escaping
    # preserves valid DDL; catalog parity compares the stored default.
    payload = Column(JSONB, nullable=False, server_default=text(r"""'{"v"\:1}'"""))
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('plan_slot','publish_pipeline','deliver_outbox',"
            "'sync_media_source','first_ingest_chunk','refresh_credential',"
            "'offboard_workspace','revoke_workspace_credentials','reauth_prompt',"
            "'reconcile_ambiguous','reap_expired','reap_transit_assets',"
            "'retention_sweep','reencrypt_credentials','send_email',"
            "'alert_stranded_sources','activation_nudge_sweep','convert_source_mix',"
            "'archive_lifecycle_sweep','meta_deletion_cleanup',"
            "'reconcile_archive_accounts')",
            name="ck_jobs_kind",
        ),
        CheckConstraint("lane IN ('interactive','bulk')", name="ck_jobs_lane"),
        CheckConstraint(
            "state IN ('ready','leased','succeeded','failed','review_required',"
            "'cancelled')",
            name="ck_jobs_state",
        ),
        CheckConstraint(
            "jsonb_typeof(payload->'v') = 'number'", name="ck_jobs_payload_v"
        ),
        CheckConstraint(
            "(workspace_id IS NULL) = (kind IN"
            " ('reconcile_ambiguous','reap_expired','reap_transit_assets',"
            "'retention_sweep','reencrypt_credentials','send_email',"
            "'alert_stranded_sources','activation_nudge_sweep',"
            "'archive_lifecycle_sweep'))",
            name="ck_jobs_system_kinds",
        ),
        Index(
            "ix_jobs_claim", "lane", "run_at", postgresql_where=text("state = 'ready'")
        ),
        Index(
            "ix_jobs_lease_expiry",
            "locked_until",
            postgresql_where=text("state = 'leased'"),
        ),
        Index(
            "uq_jobs_serialized_lease",
            "serialization_key",
            unique=True,
            postgresql_where=text("state = 'leased'"),
        ),
        Index(
            "ix_jobs_retire",
            "updated_at",
            postgresql_where=text(
                "state IN ('succeeded','cancelled','failed','review_required')"
            ),
        ),
        # `ix_jobs_leased_lane_ws`: `fn_claim_job`'s per-workspace count
        # of leased rows runs K times as often under K claimers per lane.
        Index(
            "ix_jobs_leased_lane_ws",
            "lane",
            "workspace_id",
            postgresql_where=text("state = 'leased'"),
        ),
    )


class ChannelOutbox(TargetBase):
    """Outbound channel effects. `intent_id` carries no FK — an outbox row
    is evidence an effect was attempted and must survive the intent's reaping."""

    __tablename__ = "channel_outbox"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    binding_id = Column(UUID(as_uuid=True), nullable=False)
    kind = Column(Text, nullable=False)
    intent_id = Column(UUID(as_uuid=True), nullable=True)
    payload = Column(JSONB, nullable=False)
    state = Column(Text, nullable=False, server_default=text("'pending'"))
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    external_message_ref = Column(Text, nullable=True)
    created_at, updated_at = timestamps()
    # The row's LAST failed attempt, not how it ended (outbox failure records): the class
    # (`vocabulary.OUTBOX_FAILURE_CLASSES`), the provider's own code for it
    # (Telegram's `error_code`; NULL when no answer came back) and when. A later
    # success does not clear them; `state` says how the row ended.
    last_failure_class = Column(Text, nullable=True)
    last_error_code = Column(Integer, nullable=True)
    last_failed_at = Column(TZ, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('approval_prompt','prompt_supersede','notification','ack',"
            "'invitation')",
            name="ck_outbox_kind",
        ),
        CheckConstraint(
            "last_failure_class IN ('rate_limited','destination_gone','refused',"
            "'credential_dead','ambiguous')",
            name="ck_outbox_failure_class",
        ),
        CheckConstraint(
            "jsonb_typeof(payload->'v') = 'number'", name="ck_outbox_payload_v"
        ),
        CheckConstraint(
            "state IN ('pending','sending','sent','ambiguous','failed','superseded')",
            name="ck_outbox_state",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "binding_id"],
            ["channel_bindings.workspace_id", "channel_bindings.id"],
            ondelete="CASCADE",
            name="fk_outbox_binding",
        ),
        Index(
            "ix_outbox_due",
            "binding_id",
            "created_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index(
            "ix_outbox_last_failed",
            "last_failed_at",
            postgresql_where=text("last_failed_at IS NOT NULL"),
        ),
        # `ix_outbox_intent`: the tap's supersede-everywhere and the settled-card sweep select
        # by intent.
        Index(
            "ix_outbox_intent",
            "intent_id",
            postgresql_where=text("intent_id IS NOT NULL"),
        ),
        Index(
            "ix_outbox_retire",
            "updated_at",
            postgresql_where=text(
                "state IN ('sent','superseded','failed','ambiguous')"
            ),
        ),
        # `ix_outbox_ambiguous_age`: a binding's `ambiguous` rows older than the
        # resolution backoff — the sender sweep and the sender's own tick.
        Index(
            "ix_outbox_ambiguous_age",
            "binding_id",
            "updated_at",
            postgresql_where=text("state = 'ambiguous'"),
        ),
    )


class ProviderOperation(TargetBase):
    """The permit protocol's ledger. `uq_ops_business_key` is the
    exclusivity: one permitted operation per business key, which is what makes
    a retry after an ambiguous provider response safe."""

    __tablename__ = "provider_operations"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    intent_id = Column(UUID(as_uuid=True), nullable=False)
    provider = Column(Text, nullable=False)
    op_kind = Column(Text, nullable=False)
    business_key = Column(Text, nullable=False)
    generation = Column(Integer, nullable=False, server_default=text("1"))
    state = Column(Text, nullable=False, server_default=text("'permitted'"))
    lease_token = Column(UUID(as_uuid=True), nullable=False)
    response_ref = Column(JSONB, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        UniqueConstraint("workspace_id", "id", "intent_id", name="uq_ops_ws_id_intent"),
        CheckConstraint("provider IN ('ig')", name="ck_ops_provider"),
        CheckConstraint(
            "op_kind IN ('container_create','publish')", name="ck_ops_kind"
        ),
        CheckConstraint(
            "state IN ('permitted','succeeded','failed','ambiguous')",
            name="ck_ops_state",
        ),
        UniqueConstraint("business_key", name="uq_ops_business_key"),
        Index(
            "ix_ops_retire",
            "updated_at",
            postgresql_where=text("state IN ('succeeded','failed')"),
        ),
    )


class CommandDedup(TargetBase):
    """Inbound command idempotency. Composite PK, write-once — no
    `updated_at` and no touch trigger."""

    __tablename__ = "command_dedup"

    channel = Column(Text, nullable=False, primary_key=True)
    principal = Column(Text, nullable=False, primary_key=True)
    external_ref = Column(Text, nullable=False, primary_key=True)
    fingerprint = Column(Text, nullable=False)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))


class RateCounter(TargetBase):
    """Windowed rate counters. Composite PK on (scope, key, window_start);
    write-once per window, so no `updated_at` and no touch trigger."""

    __tablename__ = "rate_counters"

    scope = Column(Text, nullable=False, primary_key=True)
    key = Column(Text, nullable=False, primary_key=True)
    window_start = Column(TZ, nullable=False, primary_key=True)
    count = Column(Integer, nullable=False, server_default=text("0"))
    created_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "scope IN ('tg_chat','tg_global','ws_admission','preauth_ip',"
            "'email_global')",
            name="ck_rate_scope",
        ),
        CheckConstraint("count >= 0", name="ck_rate_nonneg"),
        Index("ix_rate_retire", "window_start"),
    )
