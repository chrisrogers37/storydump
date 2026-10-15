"""Verified Meta identity mappings and durable deletion receipts.

`ddl.build()` creates these tables through `TargetBase.metadata`. Services use
SQL under the tenant unit of work and the receipt functions in
`src/models/target/ddl/04_functions.sql`. The receipt tables have no foreign
key to `workspaces` so a receipt outlives the workspace it describes.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.models.target.base import TargetBase
from src.models.target.columns import NOW, TZ, pk


class MetaIdentityMapping(TargetBase):
    __tablename__ = "meta_identity_mappings"

    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    ig_account_id = Column(UUID(as_uuid=True), nullable=False)
    credential_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    authorization_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    app_identity = Column(Text, nullable=False)
    subject_ref = Column(Text, nullable=False)
    account_ref = Column(Text, nullable=False)
    method = Column(Text, nullable=False)
    authorized_at = Column(TZ, nullable=True)
    verified_at = Column(TZ, nullable=False)
    is_current = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = Column(TZ, nullable=False, server_default=NOW)

    __table_args__ = (
        CheckConstraint(
            "app_identity ~ '^instagram:[0-9]{1,32}$'", name="ck_meta_mapping_app"
        ),
        CheckConstraint(
            "subject_ref ~ '^[0-9]{1,64}$' AND account_ref ~ '^[0-9]{1,64}$'",
            name="ck_meta_mapping_refs",
        ),
        CheckConstraint(
            "(method = 'oauth_exchange' AND authorized_at IS NOT NULL)"
            " OR (method = 'reconciled_read' AND authorized_at IS NULL)",
            name="ck_meta_mapping_method",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "ig_account_id", "credential_id"],
            [
                "oauth_credentials.workspace_id",
                "oauth_credentials.ig_account_id",
                "oauth_credentials.id",
            ],
            name="fk_meta_mapping_credential",
            ondelete="CASCADE",
        ),
        Index(
            "uq_meta_mapping_current",
            "workspace_id",
            "credential_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_meta_mapping_subject", "app_identity", "subject_ref"),
    )


class MetaDeletionReceipt(TargetBase):
    __tablename__ = "meta_deletion_receipts"

    id = pk()
    app_identity = Column(Text, nullable=False)
    request_fingerprint = Column(Text, nullable=False)
    fingerprint_key_version = Column(Integer, nullable=False)
    issued_at = Column(TZ, nullable=True)
    received_at = Column(TZ, nullable=False, server_default=NOW)
    state = Column(Text, nullable=False, server_default=text("'pending'"))
    review_reason = Column(Text, nullable=True)
    completed_at = Column(TZ, nullable=True)
    updated_at = Column(TZ, nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint(
            "app_identity", "request_fingerprint", name="uq_meta_receipt_fingerprint"
        ),
        CheckConstraint(
            "app_identity ~ '^(instagram:[0-9]{1,32}|instagram:unconfigured|facebook:legacy)$'",
            name="ck_meta_receipt_app",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$' AND fingerprint_key_version > 0",
            name="ck_meta_receipt_fingerprint",
        ),
        CheckConstraint(
            "state IN ('pending','needs_review','completed')",
            name="ck_meta_receipt_state",
        ),
        CheckConstraint(
            "review_reason IN ('ordering_ambiguous','mapping_incomplete',"
            "'app_unrecognized','transit_unresolved')",
            name="ck_meta_receipt_review_reason",
        ),
        CheckConstraint(
            "(state = 'needs_review') = (review_reason IS NOT NULL)",
            name="ck_meta_receipt_review",
        ),
        CheckConstraint(
            "(state = 'completed') = (completed_at IS NOT NULL)",
            name="ck_meta_receipt_completed",
        ),
    )


class MetaDeletionReceiptScope(TargetBase):
    __tablename__ = "meta_deletion_receipt_scopes"

    receipt_id = Column(
        UUID(as_uuid=True),
        ForeignKey("meta_deletion_receipts.id", ondelete="RESTRICT"),
        nullable=False,
        primary_key=True,
    )
    workspace_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    ig_account_id = Column(UUID(as_uuid=True), nullable=False)
    credential_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    authorization_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    disposition = Column(Text, nullable=False)
    state = Column(Text, nullable=False)
    results = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    next_attempt_at = Column(TZ, nullable=False, server_default=NOW)
    updated_at = Column(TZ, nullable=False, server_default=NOW)

    __table_args__ = (
        CheckConstraint(
            "disposition IN ('in_scope','held')", name="ck_meta_scope_disposition"
        ),
        CheckConstraint(
            "state IN ('pending','cleaning','held','completed','workspace_purged')",
            name="ck_meta_scope_state",
        ),
        CheckConstraint(
            "state = 'workspace_purged' OR (disposition = 'held') = (state = 'held')",
            name="ck_meta_scope_held",
        ),
        CheckConstraint(
            "jsonb_typeof(results) = 'object' AND octet_length(results::text) <= 4096",
            name="ck_meta_scope_results",
        ),
        Index(
            "ix_meta_scope_due",
            "next_attempt_at",
            postgresql_where=text("state IN ('pending','cleaning')"),
        ),
    )


class MetaDeletionReceiptGeneration(TargetBase):
    __tablename__ = "meta_deletion_receipt_generations"

    receipt_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    workspace_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    credential_id = Column(UUID(as_uuid=True), nullable=False)
    authorization_id = Column(UUID(as_uuid=True), nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=False)
    generation_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["receipt_id", "workspace_id", "credential_id", "authorization_id"],
            [
                "meta_deletion_receipt_scopes.receipt_id",
                "meta_deletion_receipt_scopes.workspace_id",
                "meta_deletion_receipt_scopes.credential_id",
                "meta_deletion_receipt_scopes.authorization_id",
            ],
            name="fk_meta_receipt_generation_scope",
            ondelete="RESTRICT",
        ),
    )


class MetaDeletionReceiptCapability(TargetBase):
    __tablename__ = "meta_deletion_receipt_capabilities"

    capability_hash = Column(Text, nullable=False, primary_key=True)
    receipt_id = Column(
        UUID(as_uuid=True),
        ForeignKey("meta_deletion_receipts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = Column(TZ, nullable=False, server_default=NOW)

    __table_args__ = (
        CheckConstraint(
            "capability_hash ~ '^[0-9a-f]{64}$'", name="ck_meta_capability_hash"
        ),
        Index("ix_meta_capability_receipt", "receipt_id"),
    )
