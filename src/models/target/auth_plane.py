"""Plan 07 — the auth-plane models (F.2.9, migration 060).

The mirror of `060_auth_plane_tables.sql`, on the terms
`identity_and_tenancy` set: the second side of the `04` §0.2 parity gate, not
an independent design.

**The first and only models sourced from `07` rather than `02`.** Measured to
the statement: `02-domain-model.md` contributes stream indices 0..240 and `07`
begins at 241, so all three of these tables come from the security model. `07`
numbers its own sections from §1, so the section references here do not
continue `02`'s.

**These three arrive WITH their RLS and policies**, unlike every `02` table
increment — the split's "#746's original property for free". Nothing about that
shows up in this module, because RLS and policies have no declarative
SQLAlchemy representation; it is noted so the difference from
`identity_and_tenancy`'s "no policies here, deliberately" is not read as an
inconsistency.

**`OAuthState` has no surrogate key.** Its primary key is `state` itself — the
128-bit urlsafe random value the OAuth round-trip carries — so it is the one
model here that does not call `pk()`. `reconnect_target` is a bare `UUID` with
no FK: it names a row in a table the plan does not constrain it to, and
inventing a reference would be a red parity test.

**`ck_oauth_state_context` is a CASE expression**, not a conjunction, and it is
the interesting constraint in the increment: a `signin` state carries neither
user nor workspace, a `link` state carries a user but no workspace, and every
other purpose carries both. Getting it wrong would still produce a working
schema and would fail parity on the normalized definition.
"""

from sqlalchemy import CheckConstraint, Column, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from src.models.target.base import TargetBase
from src.models.target.columns import TZ, fk, pk, timestamps


class SessionToken(TargetBase):
    """An opaque web session cookie, stored as a SHA256 hash (§1). Sliding
    expiry on use; `revoked_at` is the explicit kill switch."""

    __tablename__ = "session_tokens"

    id = pk()
    user_id = fk("users.id", "CASCADE", nullable=False)
    token_hash = Column(Text, nullable=False)
    expires_at = Column(TZ, nullable=False)
    revoked_at = Column(TZ, nullable=True)
    last_seen_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (UniqueConstraint("token_hash", name="uq_session_token"),)


class OAuthState(TargetBase):
    """The OAuth round-trip state (§2). PRIMARY KEY IS `state` — the random
    value itself — so there is no surrogate id."""

    __tablename__ = "oauth_states"

    state = Column(Text, primary_key=True)
    user_id = fk("users.id", "CASCADE", nullable=True)
    workspace_id = fk("workspaces.id", "CASCADE", nullable=True)
    provider = Column(Text, nullable=False)
    purpose = Column(Text, nullable=False)
    # No FK: the plan constrains this to no table, and inventing one would be
    # a red parity test.
    reconnect_target = Column(UUID(as_uuid=True), nullable=True)
    cookie_nonce_hash = Column(Text, nullable=True)
    expires_at = Column(TZ, nullable=False)
    consumed_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "provider IN ('ig_login','gdrive','google','telegram')",
            name="ck_oauth_state_provider",
        ),
        CheckConstraint(
            "purpose IN ('connect','reconnect','signin','link')",
            name="ck_oauth_state_purpose",
        ),
        # A CASE expression rather than a conjunction: signin carries neither
        # id, link carries a user only, everything else carries both.
        CheckConstraint(
            "CASE purpose"
            " WHEN 'signin' THEN user_id IS NULL AND workspace_id IS NULL"
            " WHEN 'link' THEN user_id IS NOT NULL AND workspace_id IS NULL"
            " ELSE user_id IS NOT NULL AND workspace_id IS NOT NULL"
            " END",
            name="ck_oauth_state_context",
        ),
        # Every browser-completed purpose carries a binding nonce -- `signin`,
        # `connect` and `reconnect` alike (migration 067). `link` is outside
        # the rule: it completes as a Telegram `/start` tap, so there is no
        # cookie jar to bind to. Kept a biconditional rather than a one-way
        # implication, so a nonce on a purpose that cannot use one is a schema
        # error rather than a silently ignored column.
        CheckConstraint(
            "(purpose IN ('signin', 'connect', 'reconnect'))"
            " = (cookie_nonce_hash IS NOT NULL)",
            name="ck_oauth_state_binding_nonce",
        ),
    )


class ServiceToken(TargetBase):
    """A long-lived operator/readonly API token (§6). Deliberately NOT swept by
    maintenance — it carries no sweep policy and the maintenance grant omits
    it, unlike sessions and states."""

    __tablename__ = "service_tokens"

    id = pk()
    name = Column(Text, nullable=False)
    token_hash = Column(Text, nullable=False)
    role = Column(Text, nullable=False)
    workspace_id = fk("workspaces.id", "CASCADE", nullable=True)
    expires_at = Column(TZ, nullable=True)
    revoked_at = Column(TZ, nullable=True)
    last_used_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()

    __table_args__ = (
        CheckConstraint(
            "role IN ('operator','readonly')", name="ck_service_token_role"
        ),
        UniqueConstraint("token_hash", name="uq_service_token"),
    )
