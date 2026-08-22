"""Plan 02 §1 — the identity and tenancy models (F.2.2, migration 054).

The mirror of `053_identity_and_tenancy_tables.sql`, and mirror is the whole
job: these models are not an independent design, they are the second side of
the `04` §0.2 parity gate. The migration file installs the schema; `create_all`
on `TargetBase` re-derives it; `scripts/schema_parity.py` requires the two to be
equal on tables, columns (type + nullability), CHECK constraints (by name AND
normalized definition), uniqueness semantics and foreign keys. So a column
retyped, a constraint renamed or a partial predicate reworded on one side alone
is a red test, not a discovery for later.

**They land per increment rather than in one pass, and the gate decided that
rather than a preference.** Lane parity compares the replayed `public` against
`create_all` output; 054 puts seven tables in the first and nothing in the
second, so a models-in-one-pass reading would have to run the gate knowingly
red from F.2.2 to F.2.7 — the same "red and known" cost #806 Fork 1 declined
when it declined option D.

**What is deliberately NOT here.** Triggers, trigger functions and RLS. The
parity comparator is relation-scoped and reads none of them, so restating them
would be an unchecked second copy — the drift class this file exists to
prevent. `trg_touch_updated_at` fires on every table below, is declared once in
053, and is deliberately NOT mirrored as a SQLAlchemy `onupdate`: the database
owns `updated_at` on every write path, including the ones that never go through
the ORM, and a Python-side duplicate would be a second answer to a question
that has one.

**Names are unprefixed on purpose.** At the M.3 cutover the application is
flipped to this base, and a `Target` prefix would make that a rename of every
call site rather than a change of import. The legacy `User` in
``src.models.user`` and this one coexist on separate ``MetaData`` — the
separation is asserted by
``test_lineage_lane.py::test_the_target_base_is_a_separate_metadata_from_the_legacy_one``.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from src.models.target.base import TargetBase
from src.models.target.columns import TZ, fk, pk, timestamps


class User(TargetBase):
    """The platform-neutral human (FC-1.3).

    No Telegram columns — a Telegram-only user has a NULL email and reaches
    their identity through ``user_identities``. ``state='disabled'`` denies at
    the one ingress gate; rows and memberships survive it.
    """

    __tablename__ = "users"

    id = pk()
    primary_email = Column(Text, nullable=True)
    state = Column(Text, nullable=False, server_default=text("'active'"))
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "state IN ('active','disabled')",
            name="ck_users_state",
        ),
        UniqueConstraint("primary_email", name="uq_users_primary_email"),
    )


class UserIdentity(TargetBase):
    """One identity per provider per user (v1).

    ``external_id`` is the provider's IMMUTABLE SUBJECT — the OIDC ``sub`` for
    google, the numeric user id for telegram — and never an email address
    (D32): emails are mutable and recyclable, so an identity keyed on email is
    an account-takeover primitive.
    """

    __tablename__ = "user_identities"

    id = pk()
    user_id = fk("users.id", "CASCADE", nullable=False)
    provider = Column(Text, nullable=False)
    external_id = Column(Text, nullable=False)
    display_name = Column(Text, nullable=True)
    verified_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "provider IN ('telegram','google')",
            name="ck_user_identities_provider",
        ),
        UniqueConstraint("provider", "external_id", name="uq_identity_per_provider"),
        UniqueConstraint("user_id", "provider", name="uq_user_provider"),
    )


class Workspace(TargetBase):
    """THE tenant root — ``tenant_id == workspaces.id`` everywhere.

    Ownership has one home, the ``workspace_members`` row with
    ``role='owner'``; there is deliberately no ``owner_user_id`` column, since a
    second home bought only sync triggers and drift.

    The product-configuration columns are typed rather than a settings blob,
    and NULL means "app default from env" per 02 §1's materialization contract.
    """

    __tablename__ = "workspaces"

    id = pk()
    name = Column(String(100), nullable=False)
    state = Column(Text, nullable=False, server_default=text("'active'"))

    # IANA zone name, workspace default. The CHECK is a write-time backstop
    # calling 053's fn_safe_tz — the service boundary validates first, and the
    # read-side decay case (a zone withdrawn from tzdata after a valid write)
    # is what fn_safe_tz's fallback exists for.
    tz = Column(Text, nullable=False, server_default=text("'UTC'"))

    posts_per_day = Column(Integer, nullable=False, server_default=text("3"))
    posting_hours_start = Column(Integer, nullable=False, server_default=text("14"))
    # start > end means the window wraps midnight — current semantics, kept.
    posting_hours_end = Column(Integer, nullable=False, server_default=text("2"))

    approval_mode = Column(Text, nullable=False, server_default=text("'manual'"))
    auto_reapprove_returning = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    approval_ttl_minutes = Column(Integer, nullable=True)
    dry_run_mode = Column(Boolean, nullable=False, server_default=text("false"))
    is_paused = Column(Boolean, nullable=False, server_default=text("false"))
    paused_at = Column(TZ, nullable=True)
    paused_by_user_id = fk("users.id", "SET NULL", nullable=True)
    repost_ttl_days = Column(Integer, nullable=True)
    skip_ttl_days = Column(Integer, nullable=True)
    caption_style = Column(Text, nullable=True)
    enable_ai_captions = Column(Boolean, nullable=False, server_default=text("false"))
    # The manual-vs-API mode flag (06 §3): off = manual-mode cards, on = hybrid
    # with the publish pipeline.
    api_publishing_enabled = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Set when state enters 'offboarding', cleared on restore. The durable
    # grace-window anchor fn_offboard_finalize guards on — audit rows record the
    # transition, but a door must not parse audit to tell time.
    offboarding_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "state IN ('active','suspended','offboarding')",
            name="ck_workspaces_state",
        ),
        CheckConstraint("fn_safe_tz(tz) = tz", name="ck_ws_tz_valid"),
        CheckConstraint(
            "posts_per_day BETWEEN 1 AND 50",
            name="ck_ws_posts_per_day",
        ),
        CheckConstraint(
            "posting_hours_start BETWEEN 0 AND 23",
            name="ck_ws_hours_start",
        ),
        CheckConstraint(
            "posting_hours_end BETWEEN 0 AND 23",
            name="ck_ws_hours_end",
        ),
        CheckConstraint(
            "approval_mode IN ('manual','auto')",
            name="ck_ws_approval_mode",
        ),
        CheckConstraint(
            "caption_style IN ('enhanced','simple')",
            name="ck_ws_caption_style",
        ),
    )


class WorkspaceMember(TargetBase):
    """Membership, and the structural home of ownership.

    Exactly one owner at every commit, by two mechanisms that 054 installs
    together: ``uq_members_one_owner`` is "at most one", and the deferred
    constraint-trigger PAIR is "at least one" on every path — demotion, removal
    and workspace creation alike. Only the index is a relation, so only the
    index is visible to the parity gate and declared here.
    """

    __tablename__ = "workspace_members"

    workspace_id = fk("workspaces.id", "CASCADE", primary_key=True)
    user_id = fk("users.id", "CASCADE", primary_key=True)
    role = Column(Text, nullable=False)
    added_by_user_id = fk("users.id", "SET NULL", nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner','admin','member')",
            name="ck_members_role",
        ),
        Index(
            "uq_members_one_owner",
            "workspace_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
        ),
    )


class WorkspaceInvitation(TargetBase):
    """The membership door for both surfaces (FC-6 / D33 / D36).

    ``token_hash`` is THE accept credential — possession accepts, and an email
    never resolves an invitation. ``role`` is the invitation's CEILING rather
    than an unconditional grant: an admin invite grants admin only on a matched
    identity proof, and on the recorded-skip path grants member plus an
    elevation-pending notification.
    """

    __tablename__ = "workspace_invitations"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    token_hash = Column(Text, nullable=False)
    delivery_channel = Column(Text, nullable=False)
    email = Column(Text, nullable=True)
    # The provider's immutable numeric user id — the ONLY value the D33
    # Telegram acceptance constraint may match. Never a lookup key.
    invited_tg_user_id = Column(BigInteger, nullable=True)
    # Display and delivery data only; never an authorization input.
    invited_channel_hint = Column(Text, nullable=True)
    role = Column(Text, nullable=False, server_default=text("'member'"))
    invited_by_user_id = fk("users.id", "SET NULL", nullable=True)
    state = Column(Text, nullable=False, server_default=text("'pending'"))
    accepted_by_user_id = fk("users.id", "SET NULL", nullable=True)
    # Audit fact AND authorization input, written and read in one transaction:
    # true = an identity proof ran and matched; false = the constraint was
    # bypassed for lack of comparable proof. A mismatch never lands.
    accepted_email_matched = Column(Boolean, nullable=True)
    expires_at = Column(TZ, nullable=False)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "delivery_channel IN ('email','telegram')",
            name="ck_invite_channel",
        ),
        # Never 'owner' — ownership transfers through the audited role-change
        # gate, not through an invitation.
        CheckConstraint("role IN ('admin','member')", name="ck_invite_role"),
        CheckConstraint(
            "state IN ('pending','accepted','revoked','expired')",
            name="ck_invite_state",
        ),
        CheckConstraint(
            "delivery_channel <> 'email' OR email IS NOT NULL",
            name="ck_invite_email_required",
        ),
        UniqueConstraint("token_hash", name="uq_invite_token"),
        # Survives the nullable email by construction: NULLs never collide, so
        # telegram-delivery rows are exempt and re-invitation (insert new +
        # revoke prior, one transaction) is ENFORCED by this rather than broken.
        Index(
            "uq_invite_live",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=text("state = 'pending'"),
        ),
    )


class ChannelBinding(TargetBase):
    """A push channel bound to a workspace — 0..n per workspace (FC-1.2).

    Bindings exist only for push channels; web is pull, so there is no 'web'
    enum value by design. ``revoked`` means the bot was kicked or blocked, and
    ``uq_binding_external`` holds across states so re-adding it flips the row
    back to active rather than orphaning history.
    """

    __tablename__ = "channel_bindings"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    channel = Column(Text, nullable=False)
    external_ref = Column(Text, nullable=False)
    state = Column(Text, nullable=False, server_default=text("'active'"))
    # {v:1, verbose_notifications?:bool, lifecycle_notifications?:bool};
    # an absent key means the app default.
    #
    # THE COLON IS ESCAPED, and it is not cosmetic: `text()` reads `:1` as a
    # BIND PARAMETER, so the unescaped form emits `DEFAULT '{"v"NULL}'` and the
    # table will not create. The parity gate cannot catch this — it compares
    # types, constraints, uniques and FKs, never defaults — so the failure
    # surfaces as `create_all` raising, not as drift.
    settings = Column(JSONB, nullable=False, server_default=text(r"""'{"v"\:1}'"""))
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "channel IN ('telegram_group','telegram_dm')",
            name="ck_bindings_channel",
        ),
        CheckConstraint(
            "state IN ('active','revoked')",
            name="ck_bindings_state",
        ),
        CheckConstraint(
            "jsonb_typeof(settings->'v') = 'number'",
            name="ck_bindings_settings_v",
        ),
        UniqueConstraint("channel", "external_ref", name="uq_binding_external"),
        # The composite-FK convention's parent key: children reference
        # (workspace_id, id) so a child cannot point across tenants.
        UniqueConstraint("workspace_id", "id", name="uq_bindings_ws_id"),
    )


class OnboardingSession(TargetBase):
    """One live onboarding session per user (02 §9).

    Printed after ``workspaces`` in the advertised stream because
    ``pending_workspace_id`` references it — the ordering is load-bearing and
    the migration preserves it.
    """

    __tablename__ = "onboarding_sessions"

    id = pk()
    user_id = fk("users.id", "CASCADE", nullable=False)
    step = Column(Text, nullable=False, server_default=text("'naming'"))
    pending_workspace_name = Column(Text, nullable=True)
    pending_workspace_id = fk("workspaces.id", "SET NULL", nullable=True)
    expires_at = Column(TZ, nullable=False)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "step IN ('naming','awaiting_group','connect_identity','complete')",
            name="ck_onboarding_step",
        ),
        UniqueConstraint("user_id", name="uq_onboarding_one_per_user"),
    )
