"""Migration 134: archive ownership, current observations and immutable captures.

Schema definitions only. Services use hand-written SQL under the tenant UoW.
Triggers and RLS are owned by the migration, not mirrored by ORM behavior.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from src.models.target.base import TargetBase
from src.models.target.columns import TZ


class MediaSourceDestination(TargetBase):
    __tablename__ = "media_source_destinations"

    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    source_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    ig_account_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    state = Column(Text, nullable=False, server_default=text("'active'"))
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
    updated_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "state IN ('active','retired')", name="ck_source_destinations_state"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["media_sources.workspace_id", "media_sources.id"],
            name="fk_source_destinations_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "ig_account_id"],
            ["ig_accounts.workspace_id", "ig_accounts.id"],
            name="fk_source_destinations_account",
            ondelete="RESTRICT",
        ),
    )


class InstagramArchiveGeneration(TargetBase):
    __tablename__ = "instagram_archive_generations"

    id = Column(
        UUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id = Column(UUID(as_uuid=True), nullable=False)
    origin_ig_account_id = Column(UUID(as_uuid=True), nullable=False)
    credential_id = Column(UUID(as_uuid=True), nullable=False)
    authorization_id = Column(UUID(as_uuid=True), nullable=False)
    app_id = Column(Text, nullable=False)
    subject_id = Column(Text, nullable=False)
    authorized_at = Column(TZ, nullable=False)
    lifecycle = Column(Text, nullable=False, server_default=text("'active'"))
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
    updated_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "id",
            name="uq_archive_generations_ws_source_id",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "authorization_id",
            name="uq_archive_authorization",
        ),
        CheckConstraint(
            "lifecycle IN ('active','superseded','review_hold','deletion_pending','deleted')",
            name="ck_archive_generation_lifecycle",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "origin_ig_account_id"],
            [
                "media_sources.workspace_id",
                "media_sources.id",
                "media_sources.origin_ig_account_id",
            ],
            name="fk_archive_generation_origin",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "origin_ig_account_id", "credential_id"],
            [
                "oauth_credentials.workspace_id",
                "oauth_credentials.ig_account_id",
                "oauth_credentials.id",
            ],
            name="fk_archive_generation_credential",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_archive_active_generation",
            "workspace_id",
            "source_id",
            unique=True,
            postgresql_where=text("lifecycle = 'active'"),
        ),
    )


class InstagramSourcePolicy(TargetBase):
    __tablename__ = "instagram_source_policies"

    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    source_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    schema_version = Column(Integer, nullable=False, server_default=text("1"))
    revision = Column(BigInteger, nullable=False, server_default=text("1"))
    minimum_age_days = Column(Integer, nullable=False, server_default=text("90"))
    include_credited = Column(Boolean, nullable=False, server_default=text("false"))
    include_unknown = Column(Boolean, nullable=False, server_default=text("false"))
    eligibility = Column(Text, nullable=False, server_default=text("'supported'"))
    ranking = Column(Text, nullable=False, server_default=text("'none'"))
    destination_scope = Column(
        Text, nullable=False, server_default=text("'origin_only'")
    )
    carousel_slides = Column(Text, nullable=False, server_default=text("'all'"))
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
    updated_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "schema_version > 0 AND revision > 0", name="ck_archive_policy_version"
        ),
        CheckConstraint(
            "minimum_age_days BETWEEN 0 AND 36500", name="ck_archive_policy_age"
        ),
        CheckConstraint(
            "eligibility = 'supported' AND ranking = 'none' AND destination_scope = 'origin_only'",
            name="ck_archive_policy_modes",
        ),
        CheckConstraint(
            "carousel_slides IN ('all','first')",
            name="ck_archive_policy_carousel_slides",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["media_sources.workspace_id", "media_sources.id"],
            name="fk_archive_policy_source",
            ondelete="RESTRICT",
        ),
    )


class InstagramMediaIndex(TargetBase):
    __tablename__ = "instagram_media_index"

    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    source_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    parent_media_id = Column(Text, nullable=False, primary_key=True)
    data_generation_id = Column(UUID(as_uuid=True), nullable=False)
    observation_revision = Column(BigInteger, nullable=False, server_default=text("1"))
    media_item_id = Column(UUID(as_uuid=True), nullable=True)
    first_child_id = Column(Text, nullable=True)
    slide_count = Column(Integer, nullable=True)
    slides_complete = Column(Boolean, nullable=False, server_default=text("true"))
    post_ref = Column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    original_at = Column(TZ, nullable=False)
    permalink = Column(Text, nullable=True)
    like_count = Column(BigInteger, nullable=True)
    comments_count = Column(BigInteger, nullable=True)
    observed_at = Column(TZ, nullable=False, server_default=text("now()"))
    attribution = Column(Text, nullable=False, server_default=text("'unknown'"))
    attribution_detector_version = Column(
        Integer, nullable=False, server_default=text("1")
    )
    availability = Column(Text, nullable=False, server_default=text("'available'"))
    exclusion_reason = Column(Text, nullable=True)
    validation_state = Column(Text, nullable=False, server_default=text("'pending'"))
    validation_capture = Column(JSONB, nullable=True)
    validation_generation_id = Column(UUID(as_uuid=True), nullable=True)
    validation_observation_revision = Column(BigInteger, nullable=True)
    validation_fingerprint = Column(Text, nullable=True)
    validated_at = Column(TZ, nullable=True)
    cache_generation_id = Column(UUID(as_uuid=True), nullable=True)
    cache_observation_revision = Column(BigInteger, nullable=True)
    cache_policy_revision = Column(BigInteger, nullable=True)
    cached_eligible = Column(Boolean, nullable=True)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
    updated_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index(
            "ix_archive_generation_lookup",
            "workspace_id",
            "source_id",
            "data_generation_id",
        ),
        CheckConstraint(
            "observation_revision > 0", name="ck_archive_observation_revision"
        ),
        CheckConstraint(
            "slide_count IS NULL OR slide_count BETWEEN 0 AND 20",
            name="ck_archive_slide_count",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "parent_media_id",
            "data_generation_id",
            "observation_revision",
            name="uq_archive_index_bundle",
        ),
        CheckConstraint(
            "like_count >= 0 AND comments_count >= 0",
            name="ck_archive_observation_counts",
        ),
        CheckConstraint(
            "attribution IN ('not_detected','credited','unknown') AND attribution_detector_version > 0",
            name="ck_archive_attribution",
        ),
        CheckConstraint(
            "availability IN ('available','unavailable','excluded')",
            name="ck_archive_availability",
        ),
        CheckConstraint(
            "validation_capture IS NULL OR ((jsonb_typeof(validation_capture)='object' AND validation_capture->>'generation_id'=data_generation_id::text AND (validation_capture->>'observation_revision')::bigint=observation_revision AND validation_capture->>'selected_asset_id'=COALESCE(first_child_id,parent_media_id) AND validation_capture->>'state' IN ('supported','unsupported','validation_pending')) IS TRUE)",
            name="ck_archive_validation_capture",
        ),
        CheckConstraint(
            "validation_state IN ('pending','supported','unsupported')",
            name="ck_archive_validation",
        ),
        CheckConstraint(
            "(validation_state = 'pending' AND validation_generation_id IS NULL AND validation_observation_revision IS NULL AND validation_fingerprint IS NULL AND validated_at IS NULL) OR (validation_state <> 'pending' AND validation_generation_id IS NOT NULL AND validation_observation_revision IS NOT NULL AND validation_generation_id = data_generation_id AND validation_observation_revision = observation_revision AND validation_fingerprint IS NOT NULL AND validated_at IS NOT NULL)",
            name="ck_archive_validation_evidence",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "data_generation_id"],
            [
                "instagram_archive_generations.workspace_id",
                "instagram_archive_generations.source_id",
                "instagram_archive_generations.id",
            ],
            name="fk_archive_index_generation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "data_generation_id", "media_item_id"],
            [
                "media_items.workspace_id",
                "media_items.source_id",
                "media_items.source_data_generation_id",
                "media_items.id",
            ],
            name="fk_archive_index_item",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "uq_archive_index_item",
            "workspace_id",
            "source_id",
            "media_item_id",
            unique=True,
            postgresql_where=text("media_item_id IS NOT NULL"),
        ),
    )


class InstagramMediaAsset(TargetBase):
    __tablename__ = "instagram_media_assets"

    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    source_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    parent_media_id = Column(Text, nullable=False)
    asset_media_id = Column(Text, nullable=False, primary_key=True)
    data_generation_id = Column(UUID(as_uuid=True), nullable=False)
    observation_revision = Column(BigInteger, nullable=False)
    position = Column(Integer, nullable=False)
    media_kind = Column(Text, nullable=False)
    availability = Column(Text, nullable=False, server_default=text("'available'"))
    exclusion_reason = Column(Text, nullable=True)
    media_item_id = Column(UUID(as_uuid=True), nullable=True)
    validation_state = Column(Text, nullable=False, server_default=text("'pending'"))
    validation_generation_id = Column(UUID(as_uuid=True), nullable=True)
    validation_observation_revision = Column(BigInteger, nullable=True)
    validation_fingerprint = Column(Text, nullable=True)
    validated_at = Column(TZ, nullable=True)
    validation_capture = Column(JSONB, nullable=True)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
    updated_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "parent_media_id",
            "position",
            name="uq_archive_asset_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "media_item_id",
            name="uq_archive_asset_item",
        ),
        CheckConstraint(
            "observation_revision > 0 AND position > 0",
            name="ck_archive_asset_revision",
        ),
        CheckConstraint(
            "media_kind IN ('image','video') OR (media_kind='unknown' AND availability='excluded' AND exclusion_reason='unsupported_media' AND media_item_id IS NULL)",
            name="ck_archive_asset_kind",
        ),
        CheckConstraint(
            "availability IN ('available','unavailable','excluded')",
            name="ck_archive_asset_availability",
        ),
        CheckConstraint(
            "validation_state IN ('pending','supported','unsupported')",
            name="ck_archive_asset_validation",
        ),
        CheckConstraint(
            "(validation_state = 'pending' AND validation_generation_id IS NULL AND validation_observation_revision IS NULL AND validation_fingerprint IS NULL AND validated_at IS NULL) OR (validation_state <> 'pending' AND validation_generation_id IS NOT NULL AND validation_observation_revision IS NOT NULL AND validation_generation_id = data_generation_id AND validation_observation_revision = observation_revision AND validation_fingerprint IS NOT NULL AND validated_at IS NOT NULL)",
            name="ck_archive_asset_validation_evidence",
        ),
        CheckConstraint(
            "validation_capture IS NULL OR ((jsonb_typeof(validation_capture)='object' AND validation_capture->>'generation_id'=data_generation_id::text AND (validation_capture->>'observation_revision')::bigint=observation_revision AND validation_capture->>'selected_asset_id'=asset_media_id AND validation_capture->>'state' IN ('supported','unsupported','validation_pending')) IS TRUE)",
            name="ck_archive_asset_validation_capture",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "source_id",
                "parent_media_id",
                "data_generation_id",
                "observation_revision",
            ],
            [
                "instagram_media_index.workspace_id",
                "instagram_media_index.source_id",
                "instagram_media_index.parent_media_id",
                "instagram_media_index.data_generation_id",
                "instagram_media_index.observation_revision",
            ],
            name="fk_archive_asset_bundle",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "data_generation_id"],
            [
                "instagram_archive_generations.workspace_id",
                "instagram_archive_generations.source_id",
                "instagram_archive_generations.id",
            ],
            name="fk_archive_asset_generation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "data_generation_id", "media_item_id"],
            [
                "media_items.workspace_id",
                "media_items.source_id",
                "media_items.source_data_generation_id",
                "media_items.id",
            ],
            name="fk_archive_asset_item",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index(
            "ix_archive_asset_bundle",
            "workspace_id",
            "source_id",
            "parent_media_id",
            "position",
        ),
        Index(
            "ix_archive_asset_suitability",
            "workspace_id",
            "source_id",
            "data_generation_id",
            "media_item_id",
            postgresql_where=text(
                "availability='available' AND validation_state='supported'"
            ),
        ),
    )


class InstagramItemTombstone(TargetBase):
    __tablename__ = "instagram_item_tombstones"

    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    source_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    media_item_id = Column(UUID(as_uuid=True), nullable=False)
    key_version = Column(Integer, nullable=False, primary_key=True)
    parent_hmac = Column(Text, nullable=False, primary_key=True)
    observation_revision = Column(BigInteger, nullable=False, server_default=text("1"))
    created_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index(
            "ix_archive_tombstone_lookup", "workspace_id", "key_version", "parent_hmac"
        ),
        Index(
            "ix_archive_tombstone_item_revision",
            "workspace_id",
            "source_id",
            "media_item_id",
            "observation_revision",
        ),
        CheckConstraint(
            "observation_revision > 0", name="ck_archive_tombstone_revision"
        ),
        CheckConstraint(
            "key_version > 0 AND parent_hmac ~ '^[a-f0-9]{64}$'",
            name="ck_archive_tombstone_key",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "media_item_id"],
            ["media_items.workspace_id", "media_items.source_id", "media_items.id"],
            name="fk_archive_tombstone_item",
            ondelete="RESTRICT",
        ),
    )


class InstagramArtifact(TargetBase):
    __tablename__ = "instagram_artifacts"

    artifact_ref = Column(
        UUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id = Column(UUID(as_uuid=True), nullable=False)
    data_generation_id = Column(UUID(as_uuid=True), nullable=False)
    observation_revision = Column(BigInteger, nullable=False)
    media_item_id = Column(UUID(as_uuid=True), nullable=False)
    intent_id = Column(UUID(as_uuid=True), nullable=True)
    kind = Column(Text, nullable=False)
    capture_epoch = Column(BigInteger, nullable=False)
    policy_revision = Column(BigInteger, nullable=False)
    selected_asset_identity = Column(Text, nullable=False)
    validation_fingerprint = Column(Text, nullable=True)
    upload_attempt_id = Column(UUID(as_uuid=True), nullable=True)
    admitted_at = Column(TZ, nullable=True)
    outcome = Column(Text, nullable=True)
    admitted_asset_ref = Column(Text, nullable=True)
    admitted_resource_type = Column(Text, nullable=True)
    creation_evidence = Column(JSONB, nullable=True)
    created_asset_ref = Column(Text, nullable=True)
    settled_at = Column(TZ, nullable=True)
    cleanup_state = Column(Text, nullable=False, server_default=text("'pending'"))
    destroyed_at = Column(TZ, nullable=True)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
    updated_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "artifact_ref", name="uq_archive_artifact_ws_ref"
        ),
        UniqueConstraint(
            "workspace_id",
            "source_id",
            "data_generation_id",
            "intent_id",
            "media_item_id",
            "artifact_ref",
            "observation_revision",
            "selected_asset_identity",
            "validation_fingerprint",
            "kind",
            name="uq_archive_artifact_capture",
        ),
        UniqueConstraint(
            "workspace_id", "upload_attempt_id", name="uq_archive_upload_attempt"
        ),
        CheckConstraint(
            "(admitted_asset_ref IS NULL AND admitted_resource_type IS NULL AND creation_evidence IS NULL) OR (kind = 'transit' AND admitted_asset_ref ~ ('^ws/' || workspace_id::text || '/[0-9a-f]{20,64}$') AND admitted_resource_type IN ('image','video')) IS TRUE",
            name="ck_archive_admitted_reference",
        ),
        CheckConstraint(
            "creation_evidence IS NULL OR (jsonb_typeof(creation_evidence) = 'object' AND octet_length(creation_evidence::text) <= 2048 AND creation_evidence ?& ARRAY['public_id','asset_id','version','resource_type','delivery_type','signature'] AND creation_evidence - ARRAY['public_id','asset_id','version','resource_type','delivery_type','signature'] = '{}'::jsonb AND creation_evidence->>'public_id' = admitted_asset_ref AND creation_evidence->>'resource_type' = admitted_resource_type AND creation_evidence->>'delivery_type' = 'authenticated' AND jsonb_typeof(creation_evidence->'version') = 'number' AND creation_evidence->>'version' ~ '^[1-9][0-9]{0,18}$' AND (creation_evidence->>'version')::numeric <= 9223372036854775807 AND jsonb_typeof(creation_evidence->'asset_id') = 'string' AND jsonb_typeof(creation_evidence->'signature') = 'string' AND creation_evidence->>'asset_id' ~ '^[a-zA-Z0-9_-]{1,128}$' AND creation_evidence->>'signature' ~ '^[0-9a-f]{40}$') IS TRUE",
            name="ck_archive_creation_evidence",
        ),
        CheckConstraint(
            "admitted_asset_ref IS NULL OR ((outcome IN ('unsettled','uncertain') AND created_asset_ref IS NULL AND creation_evidence IS NULL AND cleanup_state = 'pending') OR (outcome = 'settled' AND created_asset_ref = admitted_asset_ref AND creation_evidence IS NOT NULL AND cleanup_state IN ('pending','destroyed'))) IS TRUE",
            name="ck_archive_managed_completion",
        ),
        CheckConstraint(
            "admitted_asset_ref IS NULL OR ((cleanup_state = 'destroyed') = (destroyed_at IS NOT NULL))",
            name="ck_archive_artifact_destroyed",
        ),
        Index(
            "uq_archive_admitted_owner",
            "workspace_id",
            "admitted_asset_ref",
            "admitted_resource_type",
            unique=True,
            postgresql_where=text("admitted_asset_ref IS NOT NULL"),
        ),
        Index(
            "uq_archive_unsettled_intent",
            "workspace_id",
            "intent_id",
            unique=True,
            postgresql_where=text(
                "kind='transit' AND outcome IN ('unsettled','uncertain')"
            ),
        ),
        CheckConstraint(
            "kind IN ('download','validation','temporary','transit','stage')",
            name="ck_archive_artifact_kind",
        ),
        CheckConstraint(
            "observation_revision > 0 AND capture_epoch >= 0 AND policy_revision > 0",
            name="ck_archive_artifact_versions",
        ),
        CheckConstraint(
            "cleanup_state IN ('pending','destroyed','no_asset')",
            name="ck_archive_artifact_cleanup",
        ),
        CheckConstraint(
            "(kind = 'transit' AND intent_id IS NOT NULL AND upload_attempt_id IS NOT NULL AND admitted_at IS NOT NULL AND outcome IS NOT NULL AND outcome IN ('unsettled','settled','uncertain')) OR (kind <> 'transit' AND upload_attempt_id IS NULL AND admitted_at IS NULL AND outcome IS NULL)",
            name="ck_archive_artifact_attempt",
        ),
        CheckConstraint(
            "(outcome IS NOT DISTINCT FROM 'settled') = (settled_at IS NOT NULL)",
            name="ck_archive_artifact_settlement",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "data_generation_id"],
            [
                "instagram_archive_generations.workspace_id",
                "instagram_archive_generations.source_id",
                "instagram_archive_generations.id",
            ],
            name="fk_archive_artifact_generation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "media_item_id"],
            ["media_items.workspace_id", "media_items.source_id", "media_items.id"],
            name="fk_archive_artifact_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "intent_id", "media_item_id"],
            [
                "post_intents.workspace_id",
                "post_intents.id",
                "post_intents.media_item_id",
            ],
            name="fk_archive_artifact_intent",
            ondelete="RESTRICT",
        ),
    )


class InstagramIntentProvenance(TargetBase):
    __tablename__ = "instagram_intent_provenance"

    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    intent_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    source_id = Column(UUID(as_uuid=True), nullable=False)
    media_item_id = Column(UUID(as_uuid=True), nullable=False)
    origin_ig_account_id = Column(UUID(as_uuid=True), nullable=False)
    selection_generation_id = Column(UUID(as_uuid=True), nullable=False)
    selection_observation_revision = Column(BigInteger, nullable=False)
    capture_generation_id = Column(UUID(as_uuid=True), nullable=False)
    capture_observation_revision = Column(BigInteger, nullable=False)
    staged_generation_id = Column(UUID(as_uuid=True), nullable=True)
    staged_observation_revision = Column(BigInteger, nullable=True)
    selected_asset_identity = Column(Text, nullable=False)
    validation_fingerprint = Column(Text, nullable=True)
    transit_artifact_ref = Column(UUID(as_uuid=True), nullable=True)
    transit_artifact_kind = Column(
        Text, nullable=False, server_default=text("'transit'")
    )
    capture_epoch = Column(BigInteger, nullable=False)
    last_authorized_policy_revision = Column(BigInteger, nullable=False)
    created_at = Column(TZ, nullable=False, server_default=text("now()"))
    updated_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "transit_artifact_kind = 'transit'", name="ck_archive_intent_transit_kind"
        ),
        CheckConstraint(
            "selection_observation_revision > 0 AND capture_observation_revision > 0 AND capture_epoch >= 0 AND last_authorized_policy_revision > 0",
            name="ck_archive_intent_versions",
        ),
        CheckConstraint(
            "(staged_generation_id IS NULL AND staged_observation_revision IS NULL AND transit_artifact_ref IS NULL) OR (staged_generation_id IS NOT NULL AND staged_observation_revision IS NOT NULL AND staged_observation_revision > 0 AND transit_artifact_ref IS NOT NULL AND validation_fingerprint IS NOT NULL)",
            name="ck_archive_staged_bundle",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "origin_ig_account_id"],
            [
                "media_sources.workspace_id",
                "media_sources.id",
                "media_sources.origin_ig_account_id",
            ],
            name="fk_archive_intent_origin",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "media_item_id"],
            ["media_items.workspace_id", "media_items.source_id", "media_items.id"],
            name="fk_archive_intent_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "intent_id", "media_item_id"],
            [
                "post_intents.workspace_id",
                "post_intents.id",
                "post_intents.media_item_id",
            ],
            name="fk_archive_intent_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "selection_generation_id"],
            [
                "instagram_archive_generations.workspace_id",
                "instagram_archive_generations.source_id",
                "instagram_archive_generations.id",
            ],
            name="fk_archive_intent_selection",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "capture_generation_id"],
            [
                "instagram_archive_generations.workspace_id",
                "instagram_archive_generations.source_id",
                "instagram_archive_generations.id",
            ],
            name="fk_archive_intent_capture",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "source_id",
                "staged_generation_id",
                "intent_id",
                "media_item_id",
                "transit_artifact_ref",
                "staged_observation_revision",
                "selected_asset_identity",
                "validation_fingerprint",
                "transit_artifact_kind",
            ],
            [
                "instagram_artifacts.workspace_id",
                "instagram_artifacts.source_id",
                "instagram_artifacts.data_generation_id",
                "instagram_artifacts.intent_id",
                "instagram_artifacts.media_item_id",
                "instagram_artifacts.artifact_ref",
                "instagram_artifacts.observation_revision",
                "instagram_artifacts.selected_asset_identity",
                "instagram_artifacts.validation_fingerprint",
                "instagram_artifacts.kind",
            ],
            name="fk_archive_intent_stage",
            ondelete="RESTRICT",
        ),
    )


class InstagramOperationProvenance(TargetBase):
    __tablename__ = "instagram_operation_provenance"

    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    operation_id = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    intent_id = Column(UUID(as_uuid=True), nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=False)
    media_item_id = Column(UUID(as_uuid=True), nullable=False)
    origin_ig_account_id = Column(UUID(as_uuid=True), nullable=False)
    data_generation_id = Column(UUID(as_uuid=True), nullable=False)
    observation_revision = Column(BigInteger, nullable=False)
    policy_revision = Column(BigInteger, nullable=False)
    capture_epoch = Column(BigInteger, nullable=False)
    selected_asset_identity = Column(Text, nullable=False)
    validation_fingerprint = Column(Text, nullable=False)
    transit_artifact_ref = Column(UUID(as_uuid=True), nullable=False)
    transit_artifact_kind = Column(
        Text, nullable=False, server_default=text("'transit'")
    )
    created_at = Column(TZ, nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "transit_artifact_kind = 'transit'",
            name="ck_archive_operation_transit_kind",
        ),
        CheckConstraint(
            "observation_revision > 0 AND policy_revision > 0 AND capture_epoch >= 0",
            name="ck_archive_operation_versions",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "operation_id", "intent_id"],
            [
                "provider_operations.workspace_id",
                "provider_operations.id",
                "provider_operations.intent_id",
            ],
            name="fk_archive_operation_operation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "origin_ig_account_id"],
            [
                "media_sources.workspace_id",
                "media_sources.id",
                "media_sources.origin_ig_account_id",
            ],
            name="fk_archive_operation_origin",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "source_id", "media_item_id"],
            ["media_items.workspace_id", "media_items.source_id", "media_items.id"],
            name="fk_archive_operation_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "intent_id", "media_item_id"],
            [
                "post_intents.workspace_id",
                "post_intents.id",
                "post_intents.media_item_id",
            ],
            name="fk_archive_operation_intent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "workspace_id",
                "source_id",
                "data_generation_id",
                "intent_id",
                "media_item_id",
                "transit_artifact_ref",
                "observation_revision",
                "selected_asset_identity",
                "validation_fingerprint",
                "transit_artifact_kind",
            ],
            [
                "instagram_artifacts.workspace_id",
                "instagram_artifacts.source_id",
                "instagram_artifacts.data_generation_id",
                "instagram_artifacts.intent_id",
                "instagram_artifacts.media_item_id",
                "instagram_artifacts.artifact_ref",
                "instagram_artifacts.observation_revision",
                "instagram_artifacts.selected_asset_identity",
                "instagram_artifacts.validation_fingerprint",
                "instagram_artifacts.kind",
            ],
            name="fk_archive_operation_artifact",
            ondelete="RESTRICT",
        ),
    )
