"""The session, OAuth and service-token table definitions.

`ddl.build()` creates these tables through `TargetBase.metadata`. Their
policies and functions live in `src/models/target/ddl/`.
`OAuthState.state` is the opaque round-trip value and primary key.
`ck_oauth_state_context` binds the user and workspace required for each
purpose. `oauth_connection_results` records the applied Instagram history
choice and is tenant-scoped for `svc_ingress`.
"""

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.dialects.postgresql import UUID

from src.models.target.base import TargetBase
from src.models.target.columns import NOW, TZ, fk, pk, timestamps


class SessionToken(TargetBase):
    """An opaque web session cookie, stored as a SHA256 hash. Sliding
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
    """The OAuth round-trip state. PRIMARY KEY IS `state` — the random
    value itself — so there is no surrogate id."""

    __tablename__ = "oauth_states"

    state = Column(Text, primary_key=True)
    user_id = fk("users.id", "CASCADE", nullable=True)
    workspace_id = fk("workspaces.id", "CASCADE", nullable=True)
    provider = Column(Text, nullable=False)
    purpose = Column(Text, nullable=False)
    # The reconnect target is an unconstrained UUID naming the row for its
    # provider and purpose.
    reconnect_target = Column(UUID(as_uuid=True), nullable=True)
    cookie_nonce_hash = Column(Text, nullable=True)
    expires_at = Column(TZ, nullable=False)
    consumed_at = Column(TZ, nullable=True)
    created_at, updated_at = timestamps()
    #: The PKCE code verifier minted with the state (RFC 7636):
    #: ciphertext under the credential ring, never the verifier itself. NULL on
    #: a state minted without one.
    encrypted_code_verifier = Column(Text, nullable=True)
    #: The Instagram connect's history choice (`OAuth archive choice`): the entry the
    #: start previewed, and the choice with the mix revision it was shown.
    #: NULL on every state that records none.
    archive_entry = Column(Text, nullable=True)
    archive_use_history = Column(Boolean, nullable=True)
    archive_expected_mix_revision = Column(BigInteger, nullable=True)
    archive_preview_version = Column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "provider IN ('ig_login','gdrive','google','telegram')",
            name="ck_oauth_state_provider",
        ),
        CheckConstraint(
            "archive_entry IN ('add','first_connect','reconnect')"
            " AND archive_expected_mix_revision >= 0 AND archive_preview_version = 1",
            name="ck_oauth_state_archive_values",
        ),
        CheckConstraint(
            "(archive_use_history IS NULL) = (archive_expected_mix_revision IS NULL)"
            " AND (archive_use_history IS NULL) = (archive_preview_version IS NULL)"
            " AND (archive_use_history IS NULL"
            " OR archive_entry IN ('add','first_connect')) IS TRUE"
            " AND (archive_entry IS NULL OR (provider = 'ig_login'"
            " AND (archive_entry = 'add') = (reconnect_target IS NULL)))",
            name="ck_oauth_state_archive_choice",
        ),
        CheckConstraint(
            "purpose IN ('connect','reconnect','signin','link','bind')",
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
        CheckConstraint(
            "(purpose = 'signin') = (cookie_nonce_hash IS NOT NULL)",
            name="ck_oauth_state_signin_nonce",
        ),
    )


class ServiceToken(TargetBase):
    """A long-lived operator/readonly API token. Deliberately NOT swept by
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
    #: Exactly one subject (`ck_service_token_subject` `ck_service_token_subject`): a person, or a
    #: workspace. A person-bound token acts as the person across their
    #: memberships; a workspace service identity reads its one workspace.
    user_id = fk("users.id", "CASCADE", nullable=True)
    #: Who minted a workspace service identity (`workspace member removal`), so removing them can
    #: revoke it. NULL for a person-bound token and for every older token.
    created_by_user_id = fk("users.id", "SET NULL", nullable=True)

    __table_args__ = (
        CheckConstraint(
            "role IN ('operator','readonly')", name="ck_service_token_role"
        ),
        CheckConstraint(
            "(user_id IS NULL) <> (workspace_id IS NULL)",
            name="ck_service_token_subject",
        ),
        UniqueConstraint("token_hash", name="uq_service_token"),
        Index(
            "ix_service_tokens_user",
            "user_id",
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )


class OAuthConnectionResult(TargetBase):
    """What one Instagram connect's callback applied (`OAuth archive choice`): read back
    by the person who started it, in the session that returned, until it
    expires fifteen minutes later. A row is written once and never updated."""

    __tablename__ = "oauth_connection_results"

    id = pk()
    workspace_id = fk("workspaces.id", "CASCADE", nullable=False)
    user_id = fk("users.id", "CASCADE", nullable=False)
    session_id = fk("session_tokens.id", "CASCADE", nullable=False)
    ig_account_id = Column(UUID(as_uuid=True), nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=True)
    outcome = Column(Text, nullable=False)
    history_enabled = Column(Boolean, nullable=False)
    mix_revision = Column(BigInteger, nullable=False)
    mix_reset_applied = Column(Boolean, nullable=False)
    next_action = Column(Text, nullable=True)
    created_at = Column(TZ, nullable=False, server_default=NOW)
    expires_at = Column(TZ, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('connected_history_on','connected_history_off',"
            "'connected_history_off_mix_conflict','reconnected_preserved')",
            name="ck_connection_result_outcome",
        ),
        CheckConstraint(
            "next_action IS NOT DISTINCT FROM CASE WHEN outcome ="
            " 'connected_history_off_mix_conflict' THEN 'review_mix' END",
            name="ck_connection_result_next_action",
        ),
        CheckConstraint("mix_revision >= 0", name="ck_connection_result_revision"),
        ForeignKeyConstraint(
            ["workspace_id", "ig_account_id"],
            ["ig_accounts.workspace_id", "ig_accounts.id"],
            name="fk_connection_result_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "ig_account_id"],
            [
                "media_sources.workspace_id",
                "media_sources.id",
                "media_sources.origin_ig_account_id",
            ],
            name="fk_connection_result_source",
            ondelete="CASCADE",
        ),
    )
