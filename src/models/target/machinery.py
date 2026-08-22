"""Plan 02 §5 and §6 — the execution machinery models (F.2.5, migration 057).

The mirror of `056_machinery_tables.sql`, on the terms `identity_and_tenancy`
set: the second side of the `04` §0.2 parity gate, not an independent design.

**One of this increment's indexes is NOT here, and that is correct.**
`ix_intents_parked` is created by 057 on `post_intents`, which 056 owns, so it
is declared on `PostIntent` in `intent_ledger.py` — the model side renders
indexes from the table's own `__table_args__`, so there is nowhere else it
could come from. It is marked there as belonging to this increment.

**`jobs.workspace_id` is NULLABLE, alone among the tenant columns in this
increment**, and `ck_jobs_system_kinds` is what makes that safe: it asserts the
equivalence "no workspace exactly when the kind is a system kind", so a
tenant-scoped job cannot lose its tenant and a system sweep cannot acquire one.

**`command_dedup` and `rate_counters` carry no `updated_at`** and no touch
trigger — both are write-once coordination records rather than mutable rows, so
`timestamps()` is deliberately not uniform across this module.

**Two `intent_id` columns carry no foreign key.** `channel_outbox.intent_id` is
nullable and `provider_operations.intent_id` is not, but neither references
`post_intents`: an outbox row or a provider operation is evidence of an effect
that was attempted, and it must survive the intent being reaped. The plan
declares no FK on either and none is invented here.
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
    """The execution queue (§5, pg-only per C3). Claim, lease and retire are
    all index-driven: `ix_jobs_claim` finds ready work by lane, and
    `uq_jobs_serialized_lease` is what makes a serialization key exclusive —
    partial on `state = 'leased'`, so exactly one lease per key can exist while
    any number of ready or finished rows share it."""

    __tablename__ = "jobs"

    id = pk()
    kind = Column(Text, nullable=False)
    # Nullable alone in this increment — a system sweep has no tenant. The
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
    # emit `DEFAULT '{"v"NULL}'`, so the table would not create. The parity gate
    # never compares defaults, so this fails at `create_all`, not as drift.
    payload = Column(JSONB, nullable=False, server_default=text(r"""'{"v"\:1}'"""))
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "kind IN ('plan_slot','publish_pipeline','deliver_outbox',"
            "'sync_media_source','first_ingest_chunk','refresh_credential',"
            "'offboard_workspace','revoke_workspace_credentials','reauth_prompt',"
            "'reconcile_ambiguous','reap_expired','reap_transit_assets',"
            "'retention_sweep','reencrypt_credentials','send_email')",
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
            "'retention_sweep','reencrypt_credentials','send_email'))",
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
    )


class ChannelOutbox(TargetBase):
    """Outbound channel effects (§6). `intent_id` carries no FK — an outbox row
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

    __table_args__ = (
        CheckConstraint(
            "kind IN ('approval_prompt','prompt_supersede','notification','ack',"
            "'invitation')",
            name="ck_outbox_kind",
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
            "ix_outbox_retire",
            "updated_at",
            postgresql_where=text(
                "state IN ('sent','superseded','failed','ambiguous')"
            ),
        ),
    )


class ProviderOperation(TargetBase):
    """The permit protocol's ledger (§6). `uq_ops_business_key` is the
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
    """Inbound command idempotency (§6). Composite PK, write-once — no
    `updated_at` and no touch trigger."""

    __tablename__ = "command_dedup"

    channel = Column(Text, nullable=False, primary_key=True)
    principal = Column(Text, nullable=False, primary_key=True)
    external_ref = Column(Text, nullable=False, primary_key=True)
    fingerprint = Column(Text, nullable=False)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))


class RateCounter(TargetBase):
    """Windowed rate counters (§6). Composite PK on (scope, key, window_start);
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
