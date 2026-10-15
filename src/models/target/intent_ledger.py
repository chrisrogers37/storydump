"""The intent ledger table definitions.

`ddl.build()` creates these tables through `TargetBase.metadata`. The state
machine's functions, triggers and reference edges live in
`src/models/target/ddl/04_functions.sql`, `06_triggers.sql` and
`05_reference_rows.sql`.

`audit_events.workspace_id` has no foreign key so evidence survives workspace
deletion. `post_intents` uses composite tenant foreign keys with `NO ACTION`
so deleting an account or media item cannot silently delete its ledger.
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
    one. `uq_case_mix_current_by_source` permits one current row per source,
    using `effective_to IS NULL AND source_id IS NOT NULL`."""

    __tablename__ = "category_post_case_mix"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    category = Column(
        Text, nullable=False
    )  # the connected folder's display label, captured when the mix is saved
    ratio = Column(Numeric(), nullable=False)
    effective_from = Column(TZ, nullable=False, server_default=text("now()"))
    effective_to = Column(TZ, nullable=True)
    created_by_user_id = fk("users.id", "SET NULL", nullable=True)
    created_at, updated_at = timestamps()
    source_id = Column(
        UUID(as_uuid=True), nullable=True
    )  # `category_post_case_mix.source_id`: the connected folder — the key

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
    # Every insert writes 'manual' (approval rules); a row holding another mode carries
    # the workspace's mode as it was copied at mint.
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
    # create. Escaping preserves the JSON default when the schema is built;
    # the catalog comparator checks the stored default after construction.
    attempts_by_step = Column(
        JSONB, nullable=False, server_default=text(r"""'{"v"\:1}'""")
    )
    last_error = Column(JSONB, nullable=True)
    entered_state_at = Column(TZ, nullable=False, server_default=text("now()"))
    created_at, updated_at = timestamps()
    # `post_intents.origin`: a planned story (one a person scheduled for a chosen time) beside the
    # cadence ones.
    origin = Column(Text, nullable=False, server_default=text("'cadence'"))
    scheduled_by_user_id = fk("users.id", "SET NULL", nullable=True)
    # approval rules: the rule that approved the story — NULL for a person, or while it
    # waits. `tg_intent_person_or_rule` stamps it only with the approval and
    # keeps `origin` fixed at birth. The trigger functions and bindings live
    # in `ddl/04_functions.sql` and `ddl/06_triggers.sql`.
    approval_rule = Column(Text, nullable=True)
    # repeat rules: a planned story's repeat rule: the unit ('day' or 'week'), the
    # interval, the ISO weekdays (weekly only) and the local time of day. All
    # four or none, and only on a planned story. `tg_intent_repeat` makes the
    # next occurrence when one ends. Its definition lives in
    # `ddl/04_functions.sql` and its binding in `ddl/06_triggers.sql`.
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
            "published_via IN ('api','manual','dry_run')",
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
        # A waiting story is always one a person decides (approval rules).
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
        # The slot key applies only to cadence rows: a planned
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
        # a workspace's stories by slot, in every state, for the
        # calendar's month, its day view and the Overview's recent activity.
        Index("ix_intents_workspace_slot", "workspace_id", "schedule_slot_at"),
        # approval rules: "posted through the API before", the Returning items probe.
        Index(
            "ix_intents_api_posted",
            "workspace_id",
            "media_item_id",
            postgresql_where=text("state = 'posted' AND published_via = 'api'"),
        ),
        # The parked-intent index belongs to this table's model.
        # `create_all` renders indexes from the table's `__table_args__`.
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
    UPDATE, so the seeds from `intent_ledger.py`, `post_intent_transitions` and the early archive review edges
    in the archive review transitions enforce behavior rather than
    fixture data — an unseeded copy of this schema rejects every transition.

    Insert-only: edges are added or deleted, never updated, so there
    is no `updated_at` and no touch trigger.
    """

    __tablename__ = "post_intent_transitions"

    from_state = Column(Text, nullable=False, primary_key=True)
    to_state = Column(Text, nullable=False, primary_key=True)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
