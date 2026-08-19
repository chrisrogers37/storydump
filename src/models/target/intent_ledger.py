"""Plan 02 §3 — the intent ledger models (F.2.4, migration 055).

The mirror of `055_intent_ledger_tables.sql`, on the terms
`identity_and_tenancy` set: not an independent design, but the second side of
the `04` §0.2 parity gate. `scripts/schema_parity.py` requires the two equal on
tables, columns (type + nullability), CHECK constraints (by name AND normalized
definition), uniqueness semantics and foreign keys.

**Deliberately absent, and this increment has more of it than the last two.**
055 carries four trigger functions, eleven triggers and 27 seeded rows; none of
that is mirrored here. The comparator is relation-scoped, so restating a
`plpgsql` body or a seed would be an unchecked second copy — the drift class
these modules exist to prevent. The state machine is enforced by
`trg_intent_guard` reading `post_intent_transitions`, not by anything in Python.

**`audit_events` deliberately has NO foreign key on `workspace_id`.** It is the
one table here whose tenant column is a bare `UUID NOT NULL`, because an audit
row must outlive the workspace it describes — a CASCADE would delete exactly
the evidence of the deletion. Adding an `fk()` here would be a red parity test,
and it would also be wrong.

**`post_intents`' two composite FKs carry NO `ON DELETE`**, unlike every
composite FK in 054. That asymmetry is the plan's and is load-bearing: an
intent references an account and a media item under default `NO ACTION`, so a
delete that would strand a live intent is REFUSED rather than silently
cascading the ledger away. `daily_post_counts` does cascade, because a count is
derived rather than evidentiary.

**Three of the five tables have no `updated_at`.** `audit_events` and
`post_intent_transitions` are §0 insert-only classes and carry no touch
trigger; `daily_post_counts` and the other two do. `timestamps()` is therefore
not uniform across this module, which is the plan's shape rather than an
oversight.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.target.base import TargetBase
from src.models.target.columns import TZ, fk, pk, timestamps


class CategoryPostCaseMix(TargetBase):
    """The per-workspace category ratio, row-shaped with Type 2 SCD semantics
    (D23): a change closes the current row with `effective_to` and opens a new
    one, so `uq_case_mix_current` is partial on `effective_to IS NULL`."""

    __tablename__ = "category_post_case_mix"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    category = Column(Text, nullable=False)
    ratio = Column(Numeric(5, 4), nullable=False)
    effective_from = Column(TZ, nullable=False, server_default=text("now()"))
    effective_to = Column(TZ, nullable=True)
    created_by_user_id = fk("users.id", "SET NULL", nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint("ratio >= 0", name="ck_case_mix_ratio"),
        Index(
            "uq_case_mix_current",
            "workspace_id",
            "category",
            unique=True,
            postgresql_where=text("effective_to IS NULL"),
        ),
        Index(
            "ix_case_mix_current",
            "workspace_id",
            postgresql_where=text("effective_to IS NULL"),
        ),
    )


class PostIntent(TargetBase):
    """One intended post, and the row the whole state machine turns on.

    The four cap/publish CHECKs are the ledger's integrity core: a `posted` row
    must be complete for its publication route, a row that is `publishing` must
    already have debited the daily cap, an `publishing_ambiguous` row must have
    actually called publish, and a refund cannot precede a debit.
    """

    __tablename__ = "post_intents"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    # Plain UUIDs: their references are the composite constraints below.
    ig_account_id = Column(UUID(as_uuid=True), nullable=False)
    media_item_id = Column(UUID(as_uuid=True), nullable=False)
    state = Column(Text, nullable=False, server_default=text("'scheduled'"))
    cancel_requested = Column(Boolean, nullable=False, server_default=text("false"))
    schedule_slot_at = Column(TZ, nullable=False)
    approval_mode = Column(Text, nullable=False)
    approved_by_user_id = fk("users.id", "SET NULL", nullable=True)
    published_via = Column(Text, nullable=False, server_default=text("'api'"))
    provider_account_ref = Column(Text, nullable=False)
    publish_step = Column(Text, nullable=False, server_default=text("'none'"))
    ig_container_id = Column(Text, nullable=True)
    ig_media_id = Column(Text, nullable=True)
    ig_permalink = Column(Text, nullable=True)
    transit_asset_ref = Column(Text, nullable=True)
    cap_consumed_on = Column(Date, nullable=True)
    cap_refunded_at = Column(TZ, nullable=True)
    # The colon is backslash-escaped: SQLAlchemy `text()` reads `:1` as a BIND
    # PARAMETER and would emit `DEFAULT '{"v"NULL}'`, so the table would not
    # create. The parity gate never compares defaults, so this fails loudly at
    # `create_all` rather than as drift — the 053 finding, applied here.
    attempts_by_step = Column(
        JSONB, nullable=False, server_default=text(r"""'{"v"\:1}'""")
    )
    last_error = Column(JSONB, nullable=True)
    legacy_queue_item_id = Column(UUID(as_uuid=True), nullable=True)
    entered_state_at = Column(TZ, nullable=False, server_default=text("now()"))
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "state IN ('scheduled','prompt_pending','awaiting_approval','approved',"
            "'publishing','publishing_ambiguous','review_required',"
            "'posted','skipped','rejected','expired','failed','cancelled')",
            name="ck_intent_state",
        ),
        CheckConstraint(
            "approval_mode IN ('auto','manual')", name="ck_intent_approval"
        ),
        CheckConstraint(
            "published_via IN ('api','manual','legacy_backfill')", name="ck_intent_via"
        ),
        CheckConstraint(
            "publish_step IN ('none','transit_uploaded','container_created',"
            "'container_ready','publish_called','effect_confirmed')",
            name="ck_intent_step",
        ),
        CheckConstraint(
            "jsonb_typeof(attempts_by_step->'v') = 'number'",
            name="ck_intent_attempts_v",
        ),
        # NO ondelete, deliberately — see the module docstring. A delete that
        # would strand a live intent is refused, not cascaded.
        ForeignKeyConstraint(
            ["workspace_id", "ig_account_id"],
            ["ig_accounts.workspace_id", "ig_accounts.id"],
            name="fk_intent_account",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "media_item_id"],
            ["media_items.workspace_id", "media_items.id"],
            name="fk_intent_media",
        ),
        CheckConstraint(
            "state <> 'posted'"
            " OR published_via = 'legacy_backfill'"
            " OR (published_via = 'manual' AND cap_consumed_on IS NOT NULL)"
            " OR (published_via = 'api' AND ig_container_id IS NOT NULL"
            " AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL)",
            name="ck_posted_complete",
        ),
        CheckConstraint(
            "state NOT IN ('publishing','publishing_ambiguous')"
            " OR cap_consumed_on IS NOT NULL",
            name="ck_publishing_debited",
        ),
        CheckConstraint(
            "state <> 'publishing_ambiguous' OR publish_step = 'publish_called'",
            name="ck_ambiguous_called",
        ),
        CheckConstraint(
            "cap_refunded_at IS NULL OR cap_consumed_on IS NOT NULL",
            name="ck_refund_after_debit",
        ),
        Index(
            "uq_intent_slot",
            "workspace_id",
            "ig_account_id",
            "schedule_slot_at",
            unique=True,
        ),
        Index(
            "uq_intent_live_subject",
            "workspace_id",
            "media_item_id",
            "ig_account_id",
            unique=True,
            postgresql_where=text(
                "state NOT IN ('posted','skipped','rejected','expired','failed',"
                "'cancelled')"
            ),
        ),
        Index(
            "uq_publish_exclusive",
            "provider_account_ref",
            unique=True,
            postgresql_where=text("state IN ('publishing','publishing_ambiguous')"),
        ),
        Index(
            "ix_intents_reap_slot",
            "schedule_slot_at",
            postgresql_where=text("state IN ('scheduled','prompt_pending')"),
        ),
        Index(
            "ix_intents_reap_age",
            "entered_state_at",
            postgresql_where=text("state IN ('awaiting_approval','approved')"),
        ),
        # Created by migration 056 (F.2.5), not 055 — the plan groups it under
        # §6 with the outbox rather than with the ledger. It lives here because
        # `create_all` renders a table's indexes from its own `__table_args__`,
        # so this is the only place the model side can produce it.
        Index(
            "ix_intents_parked",
            "entered_state_at",
            postgresql_where=text(
                "state IN ('publishing_ambiguous','review_required')"
            ),
        ),
    )


class AuditEvent(TargetBase):
    """The governance audit log — insert-only, so no `updated_at` and no touch
    trigger. `workspace_id` carries no FK on purpose (module docstring): the
    row must outlive the workspace it records."""

    __tablename__ = "audit_events"

    id = Column(BigInteger, Identity(always=True), primary_key=True)
    workspace_id = Column(UUID(as_uuid=True), nullable=False)
    entity_kind = Column(Text, nullable=False)
    entity_id = Column(UUID(as_uuid=True), nullable=True)
    from_state = Column(Text, nullable=True)
    to_state = Column(Text, nullable=True)
    actor_kind = Column(Text, nullable=False)
    actor_user_id = Column(UUID(as_uuid=True), nullable=True)
    channel = Column(Text, nullable=True)
    detail = Column(JSONB, nullable=True)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "actor_kind IN ('user','system','clock','reaper','reconciler','operator',"
            "'migration')",
            name="ck_audit_actor",
        ),
        CheckConstraint(
            "channel IN ('telegram','web','cli','system')", name="ck_audit_channel"
        ),
        Index("ix_audit_entity", "workspace_id", "entity_kind", "entity_id", "id"),
        Index("ix_audit_time", "workspace_id", "created_at"),
        Index("ix_audit_retire", "created_at"),
    )


class DailyPostCount(TargetBase):
    """The per-account daily cap ledger, keyed by local date. Composite PK, and
    its composite FK DOES cascade — a count is derived, not evidentiary."""

    __tablename__ = "daily_post_counts"

    workspace_id = fk("workspaces.id", "CASCADE", nullable=False, primary_key=True)
    ig_account_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    local_date = Column(Date, nullable=False, primary_key=True)
    count = Column(Integer, nullable=False, server_default=text("0"))
    cap_at_write = Column(Integer, nullable=False)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint("count >= 0", name="ck_dpc_nonneg"),
        ForeignKeyConstraint(
            ["workspace_id", "ig_account_id"],
            ["ig_accounts.workspace_id", "ig_accounts.id"],
            ondelete="CASCADE",
            name="fk_dpc_account",
        ),
        Index("ix_dpc_retire", "local_date"),
    )


class PostIntentTransition(TargetBase):
    """The legal-edge reference table. `trg_intent_guard` reads it on every
    UPDATE, so the 27 rows migration 055 seeds are load-bearing rather than
    fixture data — an unseeded copy of this schema rejects every transition.

    Insert-only (§0 class): edges are added or deleted, never updated, so there
    is no `updated_at` and no touch trigger.
    """

    __tablename__ = "post_intent_transitions"

    from_state = Column(Text, nullable=False, primary_key=True)
    to_state = Column(Text, nullable=False, primary_key=True)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
