"""The identity and tenancy table definitions.

`ddl.build()` creates these tables through `TargetBase.metadata`. Their touch
triggers, ownership checks, admission functions and row policies live in
`src/models/target/ddl/`. The database owns `updated_at` on every write path;
there is no Python `onupdate` duplicate.
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
from src.models.target.columns import NOW, TZ, fk, pk, timestamps


class User(TargetBase):
    """The platform-neutral human.

    No Telegram columns — a Telegram-only user has a NULL email and reaches
    their identity through ``user_identities``. ``state='disabled'`` denies at
    the one ingress gate; rows and memberships survive it.
    """

    __tablename__ = "users"

    id = pk()
    primary_email = Column(Text, nullable=True)
    state = Column(Text, nullable=False, server_default=text("'active'"))
    created_at, updated_at = timestamps()
    #: When this person was sent the activation nudge: set once, in the same
    #: transaction that enqueues the email, and never cleared, so nobody is
    #: nudged twice (`users.activation_nudge_at`).
    activation_nudge_at = Column(TZ, nullable=True)

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
    google, the numeric user id for telegram — and never an email address:
    emails are mutable and recyclable, so an identity keyed on email is
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
    and NULL means "app default from env".
    """

    __tablename__ = "workspaces"

    id = pk()
    name = Column(String(100), nullable=False)
    state = Column(Text, nullable=False, server_default=text("'active'"))

    # IANA zone name, workspace default. The CHECK is a write-time backstop
    # calling `trg_touch_updated_at` / `fn_safe_tz`'s fn_safe_tz — the service boundary validates first, and the
    # read-side decay case (a zone withdrawn from tzdata after a valid write)
    # is what fn_safe_tz's fallback exists for.
    tz = Column(Text, nullable=False, server_default=text("'UTC'"))

    posts_per_day = Column(Integer, nullable=False, server_default=text("3"))
    posting_hours_start = Column(Integer, nullable=False, server_default=text("14"))
    # start > end means the window wraps midnight.
    posting_hours_end = Column(Integer, nullable=False, server_default=text("2"))

    approval_mode = Column(Text, nullable=False, server_default=text("'manual'"))
    approval_ttl_minutes = Column(Integer, nullable=True)
    dry_run_mode = Column(Boolean, nullable=False, server_default=text("false"))
    is_paused = Column(Boolean, nullable=False, server_default=text("false"))
    paused_at = Column(TZ, nullable=True)
    paused_by_user_id = fk("users.id", "SET NULL", nullable=True)
    repost_ttl_days = Column(Integer, nullable=True)
    skip_ttl_days = Column(Integer, nullable=True)
    caption_style = Column(Text, nullable=True)
    enable_ai_captions = Column(Boolean, nullable=False, server_default=text("false"))
    # The manual-vs-API mode flag: off = manual-mode cards, on = hybrid
    # with the publish pipeline.
    api_publishing_enabled = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Set when state enters 'offboarding', cleared on restore. The durable
    # grace-window anchor fn_offboard_finalize guards on — audit rows record the
    # transition, but a door must not parse audit to tell time.
    offboarding_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    source_mix_mode = Column(Text, nullable=True)
    source_mix_policy_version = Column(Integer, nullable=True)
    source_membership_policy = Column(Text, nullable=True)
    source_mix_revision = Column(BigInteger, nullable=True)
    source_mix_conversion_version = Column(Integer, nullable=True)
    source_mix_converted_at = Column(TZ, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "source_mix_mode IN ('equal','custom')", name="ck_ws_source_mix_mode"
        ),
        CheckConstraint(
            "source_mix_policy_version > 0 AND source_mix_revision >= 0 AND source_mix_conversion_version > 0",
            name="ck_ws_source_mix_versions",
        ),
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
            "approval_mode IN ('manual','returning','auto')",
            name="ck_ws_approval_mode",
        ),
        CheckConstraint(
            "caption_style IN ('enhanced','simple')",
            name="ck_ws_caption_style",
        ),
    )


class WorkspaceMember(TargetBase):
    """Membership, and the structural home of ownership.

    Exactly one owner at every commit, by two mechanisms that `identity_and_tenancy.py` installs
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
        Index("ix_members_user", "user_id"),
    )


class WorkspaceMemberRemoval(TargetBase):
    """The record that an admin removed a person (workspace member removal). The Telegram join path
    (`fn_group_member_seen`) honours it: a removed non-member is refused until
    invited back. Written only by `fn_member_remove`; an invitation accepted
    later makes it inert, and a second removal stamps it again."""

    __tablename__ = "workspace_member_removals"

    workspace_id = fk("workspaces.id", "CASCADE", primary_key=True)
    user_id = fk("users.id", "CASCADE", primary_key=True)
    removed_by_user_id = fk("users.id", "SET NULL", nullable=True)
    removed_at = Column(TZ, nullable=False, server_default=NOW)


#: The invisible characters an address may not carry (`fn_signup_admitted`, `waitlist_entries`).
_NO_INVISIBLE = (
    "email !~ '[\\u0080-\\u00a0\\u00ad\\u180e\\u2000-\\u200f\\u2028-\\u202f"
    "\\u205f-\\u2064\\u3000\\ufeff]'"
)


class SignupAdmission(TargetBase):
    """An email the owner let in (`fn_signup_admitted`): a new Google account with this verified
    address may create its user. Global, not tenant-plane; read and written
    only through `svc_membership`'s definer functions and the
    `trg_users_consume_admission` trigger function on `users`, so the runtime
    roles hold no grant on it.

    The admission's email carries a one-time claim link. Only its token's
    SHA-256 is stored, with `emailed_at`, once the provider accepted the email.
    `claimed_at` records that the spot was used, either by its link or by an
    account created at its address, and the link is cleared with it; the spot
    then admits nobody (one spot, one account).
    A consumed admission admits no additional account."""

    __tablename__ = "signup_admissions"

    email = Column(Text, primary_key=True)
    admitted_at = Column(TZ, nullable=False, server_default=NOW)
    note = Column(Text, nullable=True)
    claim_token_hash = Column(Text, nullable=True)
    emailed_at = Column(TZ, nullable=True)
    claimed_at = Column(TZ, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "email = lower(email) AND email ~ '^[^[:space:]@]+@[^[:space:]@]+$'"
            " AND " + _NO_INVISIBLE,
            name="ck_signup_admissions_email",
        ),
        # The claim's lookup, as `uq_invite_token` is an invitation's.
        Index(
            "uq_signup_admissions_claim",
            "claim_token_hash",
            unique=True,
            postgresql_where=text("claim_token_hash IS NOT NULL"),
        ),
    )


class WaitlistEntry(TargetBase):
    """An address that joined the marketing waitlist (`waitlist_entries`). Global, not
    tenant-plane; the API (`svc_ingress`) may INSERT and nothing else, and the
    list is read only through `fn_waitlist` (signup admission functions). `utm` is the campaign the
    visitor came from."""

    __tablename__ = "waitlist_entries"

    email = Column(Text, primary_key=True)
    joined_at = Column(TZ, nullable=False, server_default=NOW)
    utm = Column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "email = lower(email) AND length(email) <= 254"
            " AND email ~ '^[^[:space:]@]+@[^[:space:]@]+\\.[^[:space:]@]+$'"
            " AND email !~ '[[:cntrl:]]'"
            " AND email !~ '[\\u061c\\u2066-\\u2069\\ufff9-\\ufffb\\U000e0000-\\U000e007f]'"
            " AND " + _NO_INVISIBLE,
            name="ck_waitlist_entries_email",
        ),
        CheckConstraint(
            "utm IS NULL OR (jsonb_typeof(utm) = 'object' AND length(utm::text) <= 2048)",
            name="ck_waitlist_entries_utm",
        ),
    )


class WorkspaceInvitation(TargetBase):
    """The membership door for both surfaces.

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
    # The provider's immutable numeric user id — the ONLY value the
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
        Index(
            "ix_invite_pending_email",
            text("lower(email)"),
            postgresql_where=text("state = 'pending'"),
        ),
    )


class ChannelBinding(TargetBase):
    """A push channel bound to a workspace — 0..n per workspace.

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
    # table will not create. Escaping preserves valid DDL during the build;
    # catalog parity compares the stored default after construction.
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
    """One live onboarding session per user.

    `pending_workspace_id` references `workspaces`. SQLAlchemy orders the
    tables by their foreign-key dependencies when the schema builder runs.
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
