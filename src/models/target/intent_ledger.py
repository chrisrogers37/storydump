"""The intent ledger models (migration 055).

The mirror of `055_intent_ledger_tables.sql`, on the terms
`identity_and_tenancy` set: not an independent design, but the second side of
the parity gate. `scripts/schema_parity.py` requires the two equal on
tables, columns (type + nullability), CHECK constraints (by name AND normalized
definition), uniqueness semantics and foreign keys.

**Deliberately absent: the triggers and the seeds.** 055 carries four trigger
functions, eleven triggers and 27 seeded rows (29 with 076's two float edges);
none of that is mirrored here. The comparator is relation-scoped, so restating a
`plpgsql` body or a seed would be an unchecked second copy — the drift class
these modules exist to prevent. The state machine is enforced by
`trg_intent_guard` reading `post_intent_transitions`, not by anything in Python.

**`audit_events` deliberately has NO foreign key on `workspace_id`.** It is the
one table here whose tenant column is a bare `UUID NOT NULL`, because an audit
row must outlive the workspace it describes — a CASCADE would delete exactly
the evidence of the deletion. Adding an `fk()` here would be a red parity test,
and it would also be wrong.

**`post_intents`' two composite FKs carry NO `ON DELETE`**, unlike every
composite FK in 054. That asymmetry is deliberate and load-bearing: an
intent references an account and a media item under default `NO ACTION`, so a
delete that would strand a live intent is REFUSED rather than silently
cascading the ledger away. `daily_post_counts` does cascade, because a count is
derived rather than evidentiary.

**Three of the five tables have no `updated_at`.** `audit_events` and
`post_intent_transitions` are insert-only classes and carry no touch
trigger; `daily_post_counts` and the other two do. `timestamps()` is therefore
not uniform across this module, which is the schema's shape rather than an
oversight.
"""

from sqlalchemy import (
    ARRAY,
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
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.target.base import TargetBase
from src.models.target.columns import TZ, fk, pk, timestamps


class CategoryPostCaseMix(TargetBase):
    """The per-workspace category ratio, row-shaped with Type 2 SCD semantics:
    a change closes the current row with `effective_to` and opens a new
    one, so `uq_case_mix_current` is partial on `effective_to IS NULL`."""

    __tablename__ = "category_post_case_mix"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    category = Column(Text, nullable=False)  # the label the card shows (071)
    source_id = Column(
        UUID(as_uuid=True), nullable=True
    )  # 071: the connected folder — the key
    ratio = Column(Numeric(), nullable=False)
    effective_from = Column(TZ, nullable=False, server_default=text("now()"))
    effective_to = Column(TZ, nullable=True)
    created_by_user_id = fk("users.id", "SET NULL", nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint("ratio >= 0", name="ck_case_mix_ratio"),
        Index(
            "uq_case_mix_current_by_source",
            "workspace_id",
            "source_id",
            unique=True,
            postgresql_where=text("effective_to IS NULL AND source_id IS NOT NULL"),
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
    # Every insert writes 'manual' (110); a row holding another mode carries
    # the workspace's mode as it was copied at mint.
    approval_mode = Column(Text, nullable=False)
    # 110: the rule that approved the story — NULL for a person, or while it
    # waits. `tg_intent_person_or_rule` stamps it only with the approval and
    # keeps `origin` fixed at birth; the parity gate does not compare
    # triggers, so the migration is where both rules are read.
    approval_rule = Column(Text, nullable=True)
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
    # `create_all` rather than as drift.
    attempts_by_step = Column(
        JSONB, nullable=False, server_default=text(r"""'{"v"\:1}'""")
    )
    last_error = Column(JSONB, nullable=True)
    legacy_queue_item_id = Column(UUID(as_uuid=True), nullable=True)
    entered_state_at = Column(TZ, nullable=False, server_default=text("now()"))
    created_at, updated_at = timestamps()
    # 088: a planned story (one a person scheduled for a chosen time) beside the
    # cadence ones.
    origin = Column(Text, nullable=False, server_default=text("'cadence'"))
    scheduled_by_user_id = fk("users.id", "SET NULL", nullable=True)
    # 116: a planned story's repeat rule (#485): the unit ('day' or 'week'), the
    # interval, the ISO weekdays (weekly only) and the local time of day. All
    # four or none, and only on a planned story. `tg_intent_repeat` makes the
    # next occurrence when one ends; the parity gate does not compare triggers,
    # so the migration is where that rule is read.
    repeat_unit = Column(Text, nullable=True)
    repeat_every = Column(SmallInteger, nullable=True)
    repeat_days = Column(ARRAY(SmallInteger), nullable=True)
    repeat_at = Column(Time, nullable=True)

    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_intents_ws_id"),
        UniqueConstraint(
            "workspace_id", "id", "media_item_id", name="uq_intents_ws_id_media"
        ),
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
            "published_via IN ('api','manual','legacy_backfill','dry_run')",
            name="ck_intent_via",
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
            " OR (published_via = 'dry_run' AND publish_step = 'effect_confirmed'"
            " AND cap_consumed_on IS NOT NULL)"
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
        CheckConstraint("origin IN ('cadence','planned')", name="ck_intent_origin"),
        CheckConstraint(
            "approval_rule IN ('auto','item','returning')",
            name="ck_intent_approval_rule",
        ),
        # A waiting story is always one a person decides (110).
        CheckConstraint(
            "approval_rule IS NULL OR state NOT IN "
            "('scheduled','prompt_pending','awaiting_approval')",
            name="ck_intent_waiting_unruled",
        ),
        CheckConstraint("repeat_unit IN ('day','week')", name="ck_intent_repeat_unit"),
        CheckConstraint(
            "CASE"
            " WHEN repeat_unit IS NULL"
            " THEN repeat_every IS NULL AND repeat_days IS NULL AND repeat_at IS NULL"
            " WHEN origin <> 'planned' OR repeat_every IS NULL OR repeat_at IS NULL"
            " THEN false"
            " WHEN repeat_unit = 'day'"
            " THEN repeat_days IS NULL AND repeat_every BETWEEN 1 AND 365"
            " WHEN repeat_unit = 'week'"
            " THEN repeat_days IS NOT NULL AND repeat_every BETWEEN 1 AND 52"
            " AND array_ndims(repeat_days) = 1 AND cardinality(repeat_days) BETWEEN 1 AND 7"
            " AND repeat_days <@ ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]"
            " ELSE true"
            " END",
            name="ck_intent_repeat",
        ),
        # The slot key is cadence's alone (088 expand, 089 contract): a planned
        # row takes no slot, so it never absorbs a slot's mint and a cadence
        # mint never meets it. `plan_slot`'s conflict target carries the
        # predicate, which is how Postgres infers this partial index.
        Index(
            "uq_intent_slot",
            "workspace_id",
            "ig_account_id",
            "schedule_slot_at",
            unique=True,
            postgresql_where=text("origin = 'cadence'"),
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
        # #1640: a workspace's stories by slot, in every state, for the
        # calendar's month, its day view and the Overview's recent activity.
        Index("ix_intents_workspace_slot", "workspace_id", "schedule_slot_at"),
        # 110: "posted through the API before", the Returning items probe.
        Index(
            "ix_intents_api_posted",
            "workspace_id",
            "media_item_id",
            postgresql_where=text("state = 'posted' AND published_via = 'api'"),
        ),
        # Created by migration 056, not 055 — with the outbox rather than with
        # the ledger. It lives here because
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
    UPDATE, so the seeds from 055, 076 and the early archive review edges
    in 140 are load-bearing rather than
    fixture data — an unseeded copy of this schema rejects every transition.

    Insert-only: edges are added or deleted, never updated, so there
    is no `updated_at` and no touch trigger.
    """

    __tablename__ = "post_intent_transitions"

    from_state = Column(Text, nullable=False, primary_key=True)
    to_state = Column(Text, nullable=False, primary_key=True)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
