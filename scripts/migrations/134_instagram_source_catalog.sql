-- 134: Instagram source catalog contracts; runtime activation remains deferred (#1644).
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'media_sources' AND column_name = 'origin_ig_account_id'
-- runner:postcondition SELECT count(*) = 8 FROM pg_tables WHERE schemaname = 'public' AND tablename IN ('media_source_destinations','instagram_archive_generations','instagram_source_policies','instagram_media_index','instagram_item_tombstones','instagram_artifacts','instagram_intent_provenance','instagram_operation_provenance')

-- [§77 Instagram archive catalog: ownership, policy and generation provenance]
ALTER TABLE media_sources DROP CONSTRAINT ck_sources_provider;
ALTER TABLE media_sources ADD CONSTRAINT ck_sources_provider CHECK (provider IN ('gdrive','instagram_archive'));
ALTER TABLE media_sources ADD COLUMN origin_ig_account_id UUID NULL;
ALTER TABLE media_sources ADD COLUMN source_use_epoch BIGINT NOT NULL DEFAULT 0;
ALTER TABLE media_sources ADD COLUMN source_epoch_transaction_id BIGINT NULL;
ALTER TABLE media_sources ADD COLUMN current_authorization_generation_id UUID NULL;
ALTER TABLE media_sources ADD CONSTRAINT ck_sources_current_authorization CHECK (current_authorization_generation_id IS NULL OR provider = 'instagram_archive');
ALTER TABLE media_sources ADD CONSTRAINT ck_sources_origin CHECK ((provider = 'instagram_archive') = (origin_ig_account_id IS NOT NULL));
ALTER TABLE media_sources ADD CONSTRAINT ck_sources_epoch CHECK (source_use_epoch >= 0);
ALTER TABLE media_sources ADD CONSTRAINT uq_sources_ws_id_origin UNIQUE (workspace_id, id, origin_ig_account_id);
ALTER TABLE media_sources ADD CONSTRAINT fk_sources_origin FOREIGN KEY (workspace_id, origin_ig_account_id) REFERENCES ig_accounts(workspace_id, id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX uq_archive_origin ON media_sources (workspace_id, origin_ig_account_id) WHERE provider = 'instagram_archive';
ALTER TABLE oauth_credentials ADD CONSTRAINT uq_credentials_ws_account_id UNIQUE (workspace_id, ig_account_id, id);
ALTER TABLE media_items ADD COLUMN source_data_generation_id UUID NULL;
ALTER TABLE media_items ADD COLUMN source_projection_redacted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE media_items ADD CONSTRAINT ck_media_redacted_projection CHECK (NOT source_projection_redacted OR (source_data_generation_id IS NULL AND state = 'removed' AND media_kind = 'image' AND provider_file_ref = 'redacted:' || id::text AND content_hash = 'redacted:' || id::text AND file_name = 'redacted:' || id::text AND mime_type IS NULL AND file_size IS NULL AND category IS NULL AND folder_path IS NULL AND last_listed_at IS NULL AND title IS NULL AND caption IS NULL AND generated_caption IS NULL AND link_url IS NULL AND tags IS NULL AND custom_metadata IS NULL AND thumbnail_url IS NULL));
ALTER TABLE media_items ADD CONSTRAINT uq_media_ws_source_id UNIQUE (workspace_id, source_id, id);
ALTER TABLE media_items ADD CONSTRAINT uq_media_ws_source_generation_id UNIQUE (workspace_id, source_id, source_data_generation_id, id);
ALTER TABLE post_intents ADD CONSTRAINT uq_intents_ws_id UNIQUE (workspace_id, id);
ALTER TABLE post_intents ADD CONSTRAINT uq_intents_ws_id_media UNIQUE (workspace_id, id, media_item_id);
ALTER TABLE provider_operations ADD CONSTRAINT uq_ops_ws_id_intent UNIQUE (workspace_id, id, intent_id);
ALTER TABLE workspaces ADD COLUMN source_mix_mode TEXT NULL;
ALTER TABLE workspaces ADD COLUMN source_mix_policy_version INTEGER NULL;
ALTER TABLE workspaces ADD COLUMN source_membership_policy TEXT NULL;
ALTER TABLE workspaces ADD COLUMN source_mix_revision BIGINT NULL;
ALTER TABLE workspaces ADD COLUMN source_mix_conversion_version INTEGER NULL;
ALTER TABLE workspaces ADD COLUMN source_mix_converted_at TIMESTAMPTZ NULL;
ALTER TABLE workspaces ADD CONSTRAINT ck_ws_source_mix_mode CHECK (source_mix_mode IN ('equal','custom'));
ALTER TABLE workspaces ADD CONSTRAINT ck_ws_source_mix_versions CHECK (source_mix_policy_version > 0 AND source_mix_revision >= 0 AND source_mix_conversion_version > 0);

CREATE TABLE media_source_destinations (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  ig_account_id UUID NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, source_id, ig_account_id),
  CONSTRAINT ck_source_destinations_state CHECK (state IN ('active','retired')),
  CONSTRAINT fk_source_destinations_source FOREIGN KEY (workspace_id, source_id) REFERENCES media_sources(workspace_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_source_destinations_account FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts(workspace_id, id) ON DELETE RESTRICT
);
CREATE INDEX ix_source_destinations_account ON media_source_destinations (workspace_id, ig_account_id, source_id) WHERE state = 'active';

CREATE TABLE instagram_archive_generations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  origin_ig_account_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  authorization_id UUID NOT NULL,
  app_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  authorized_at TIMESTAMPTZ NOT NULL,
  lifecycle TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_archive_generations_ws_source_id UNIQUE (workspace_id, source_id, id),
  CONSTRAINT uq_archive_authorization UNIQUE (workspace_id, source_id, authorization_id),
  CONSTRAINT ck_archive_generation_lifecycle CHECK (lifecycle IN ('active','superseded','deletion_pending','deleted')),
  CONSTRAINT fk_archive_generation_origin FOREIGN KEY (workspace_id, source_id, origin_ig_account_id) REFERENCES media_sources(workspace_id, id, origin_ig_account_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_generation_credential FOREIGN KEY (workspace_id, origin_ig_account_id, credential_id) REFERENCES oauth_credentials(workspace_id, ig_account_id, id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_archive_active_generation ON instagram_archive_generations (workspace_id, source_id) WHERE lifecycle = 'active';
-- Authorization ownership survives lifecycle fencing; receipts must not clear it.
ALTER TABLE media_sources ADD CONSTRAINT fk_sources_current_authorization FOREIGN KEY (workspace_id, id, current_authorization_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT;
ALTER TABLE media_items ADD CONSTRAINT fk_media_generation FOREIGN KEY (workspace_id, source_id, source_data_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT;

CREATE TABLE instagram_source_policies (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  revision BIGINT NOT NULL DEFAULT 1,
  minimum_age_days INTEGER NOT NULL DEFAULT 90,
  include_credited BOOLEAN NOT NULL DEFAULT false,
  include_unknown BOOLEAN NOT NULL DEFAULT false,
  eligibility TEXT NOT NULL DEFAULT 'supported',
  ranking TEXT NOT NULL DEFAULT 'none',
  destination_scope TEXT NOT NULL DEFAULT 'origin_only',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, source_id),
  CONSTRAINT ck_archive_policy_version CHECK (schema_version > 0 AND revision > 0),
  CONSTRAINT ck_archive_policy_age CHECK (minimum_age_days BETWEEN 0 AND 36500),
  CONSTRAINT ck_archive_policy_modes CHECK (eligibility = 'supported' AND ranking = 'none' AND destination_scope = 'origin_only'),
  CONSTRAINT fk_archive_policy_source FOREIGN KEY (workspace_id, source_id) REFERENCES media_sources(workspace_id, id) ON DELETE RESTRICT
);

CREATE TABLE instagram_media_index (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  parent_media_id TEXT NOT NULL,
  data_generation_id UUID NOT NULL,
  observation_revision BIGINT NOT NULL DEFAULT 1,
  media_item_id UUID NULL,
  first_child_id TEXT NULL,
  original_at TIMESTAMPTZ NOT NULL,
  permalink TEXT NULL,
  like_count BIGINT NULL,
  comments_count BIGINT NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  attribution TEXT NOT NULL DEFAULT 'unknown',
  attribution_detector_version INTEGER NOT NULL DEFAULT 1,
  availability TEXT NOT NULL DEFAULT 'available',
  exclusion_reason TEXT NULL,
  validation_state TEXT NOT NULL DEFAULT 'pending',
  validation_generation_id UUID NULL,
  validation_observation_revision BIGINT NULL,
  validation_fingerprint TEXT NULL,
  validated_at TIMESTAMPTZ NULL,
  cache_generation_id UUID NULL,
  cache_observation_revision BIGINT NULL,
  cache_policy_revision BIGINT NULL,
  cached_eligible BOOLEAN NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, source_id, parent_media_id),
  CONSTRAINT ck_archive_observation_revision CHECK (observation_revision > 0),
  CONSTRAINT ck_archive_observation_counts CHECK (like_count >= 0 AND comments_count >= 0),
  CONSTRAINT ck_archive_attribution CHECK (attribution IN ('not_detected','credited','unknown') AND attribution_detector_version > 0),
  CONSTRAINT ck_archive_availability CHECK (availability IN ('available','unavailable','excluded')),
  CONSTRAINT ck_archive_validation CHECK (validation_state IN ('pending','supported','unsupported')),
  CONSTRAINT ck_archive_validation_evidence CHECK ((validation_state = 'pending' AND validation_generation_id IS NULL AND validation_observation_revision IS NULL AND validation_fingerprint IS NULL AND validated_at IS NULL) OR (validation_state <> 'pending' AND validation_generation_id IS NOT NULL AND validation_observation_revision IS NOT NULL AND validation_generation_id = data_generation_id AND validation_observation_revision = observation_revision AND validation_fingerprint IS NOT NULL AND validated_at IS NOT NULL)),
  CONSTRAINT fk_archive_index_generation FOREIGN KEY (workspace_id, source_id, data_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_index_item FOREIGN KEY (workspace_id, source_id, data_generation_id, media_item_id) REFERENCES media_items(workspace_id, source_id, source_data_generation_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
);
CREATE UNIQUE INDEX uq_archive_index_item ON instagram_media_index (workspace_id, source_id, media_item_id) WHERE media_item_id IS NOT NULL;
CREATE INDEX ix_archive_original_time ON instagram_media_index (workspace_id, source_id, original_at, parent_media_id);
CREATE INDEX ix_archive_suitability ON instagram_media_index (workspace_id, source_id, data_generation_id, original_at, media_item_id) WHERE availability = 'available' AND validation_state = 'supported';

CREATE TABLE instagram_item_tombstones (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  media_item_id UUID NOT NULL,
  key_version INTEGER NOT NULL,
  parent_hmac TEXT NOT NULL,
  observation_revision BIGINT NOT NULL DEFAULT 1,
  CONSTRAINT ck_archive_tombstone_revision CHECK (observation_revision > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, source_id, key_version, parent_hmac),
  CONSTRAINT ck_archive_tombstone_key CHECK (key_version > 0 AND parent_hmac ~ '^[a-f0-9]{64}$'),
  CONSTRAINT fk_archive_tombstone_item FOREIGN KEY (workspace_id, source_id, media_item_id) REFERENCES media_items(workspace_id, source_id, id) ON DELETE RESTRICT
);

CREATE TABLE instagram_artifacts (
  artifact_ref UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id UUID NOT NULL,
  data_generation_id UUID NOT NULL,
  observation_revision BIGINT NOT NULL,
  media_item_id UUID NOT NULL,
  intent_id UUID NULL,
  kind TEXT NOT NULL,
  capture_epoch BIGINT NOT NULL,
  policy_revision BIGINT NOT NULL,
  selected_asset_identity TEXT NOT NULL,
  validation_fingerprint TEXT NULL,
  upload_attempt_id UUID NULL,
  admitted_at TIMESTAMPTZ NULL,
  outcome TEXT NULL,
  created_asset_ref TEXT NULL,
  settled_at TIMESTAMPTZ NULL,
  cleanup_state TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_archive_artifact_ws_ref UNIQUE (workspace_id, artifact_ref),
  CONSTRAINT uq_archive_artifact_capture UNIQUE (workspace_id, source_id, data_generation_id, intent_id, media_item_id, artifact_ref, observation_revision, selected_asset_identity, validation_fingerprint, kind),
  CONSTRAINT uq_archive_upload_attempt UNIQUE (workspace_id, upload_attempt_id),
  CONSTRAINT ck_archive_artifact_kind CHECK (kind IN ('download','validation','temporary','transit','stage')),
  CONSTRAINT ck_archive_artifact_versions CHECK (observation_revision > 0 AND capture_epoch >= 0 AND policy_revision > 0),
  CONSTRAINT ck_archive_artifact_cleanup CHECK (cleanup_state IN ('pending','destroyed','no_asset')),
  CONSTRAINT ck_archive_artifact_attempt CHECK ((kind = 'transit' AND intent_id IS NOT NULL AND upload_attempt_id IS NOT NULL AND admitted_at IS NOT NULL AND outcome IS NOT NULL AND outcome IN ('unsettled','settled','uncertain')) OR (kind <> 'transit' AND upload_attempt_id IS NULL AND admitted_at IS NULL AND outcome IS NULL)),
  CONSTRAINT ck_archive_artifact_settlement CHECK ((outcome IS NOT DISTINCT FROM 'settled') = (settled_at IS NOT NULL)),
  CONSTRAINT fk_archive_artifact_generation FOREIGN KEY (workspace_id, source_id, data_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_artifact_item FOREIGN KEY (workspace_id, source_id, media_item_id) REFERENCES media_items(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_artifact_intent FOREIGN KEY (workspace_id, intent_id, media_item_id) REFERENCES post_intents(workspace_id, id, media_item_id) ON DELETE RESTRICT
);
CREATE INDEX ix_archive_cleanup_generation ON instagram_artifacts (workspace_id, source_id, data_generation_id, cleanup_state, outcome);

CREATE TABLE instagram_intent_provenance (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  intent_id UUID NOT NULL,
  source_id UUID NOT NULL,
  media_item_id UUID NOT NULL,
  origin_ig_account_id UUID NOT NULL,
  selection_generation_id UUID NOT NULL,
  selection_observation_revision BIGINT NOT NULL,
  capture_generation_id UUID NOT NULL,
  capture_observation_revision BIGINT NOT NULL,
  staged_generation_id UUID NULL,
  staged_observation_revision BIGINT NULL,
  selected_asset_identity TEXT NOT NULL,
  validation_fingerprint TEXT NULL,
  transit_artifact_ref UUID NULL,
  transit_artifact_kind TEXT NOT NULL DEFAULT 'transit',
  capture_epoch BIGINT NOT NULL,
  last_authorized_policy_revision BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, intent_id),
  CONSTRAINT ck_archive_intent_transit_kind CHECK (transit_artifact_kind = 'transit'),
  CONSTRAINT ck_archive_intent_versions CHECK (selection_observation_revision > 0 AND capture_observation_revision > 0 AND capture_epoch >= 0 AND last_authorized_policy_revision > 0),
  CONSTRAINT ck_archive_staged_bundle CHECK ((staged_generation_id IS NULL AND staged_observation_revision IS NULL AND transit_artifact_ref IS NULL) OR (staged_generation_id IS NOT NULL AND staged_observation_revision IS NOT NULL AND staged_observation_revision > 0 AND transit_artifact_ref IS NOT NULL AND validation_fingerprint IS NOT NULL)),
  CONSTRAINT fk_archive_intent_origin FOREIGN KEY (workspace_id, source_id, origin_ig_account_id) REFERENCES media_sources(workspace_id, id, origin_ig_account_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_item FOREIGN KEY (workspace_id, source_id, media_item_id) REFERENCES media_items(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_intent FOREIGN KEY (workspace_id, intent_id, media_item_id) REFERENCES post_intents(workspace_id, id, media_item_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_selection FOREIGN KEY (workspace_id, source_id, selection_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_capture FOREIGN KEY (workspace_id, source_id, capture_generation_id) REFERENCES instagram_archive_generations(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_intent_stage FOREIGN KEY (workspace_id, source_id, staged_generation_id, intent_id, media_item_id, transit_artifact_ref, staged_observation_revision, selected_asset_identity, validation_fingerprint, transit_artifact_kind) REFERENCES instagram_artifacts(workspace_id, source_id, data_generation_id, intent_id, media_item_id, artifact_ref, observation_revision, selected_asset_identity, validation_fingerprint, kind) ON DELETE RESTRICT
);

CREATE TABLE instagram_operation_provenance (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  operation_id UUID NOT NULL,
  intent_id UUID NOT NULL,
  source_id UUID NOT NULL,
  media_item_id UUID NOT NULL,
  origin_ig_account_id UUID NOT NULL,
  data_generation_id UUID NOT NULL,
  observation_revision BIGINT NOT NULL,
  policy_revision BIGINT NOT NULL,
  capture_epoch BIGINT NOT NULL,
  selected_asset_identity TEXT NOT NULL,
  validation_fingerprint TEXT NOT NULL,
  transit_artifact_ref UUID NOT NULL,
  transit_artifact_kind TEXT NOT NULL DEFAULT 'transit',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, operation_id),
  CONSTRAINT ck_archive_operation_transit_kind CHECK (transit_artifact_kind = 'transit'),
  CONSTRAINT ck_archive_operation_versions CHECK (observation_revision > 0 AND policy_revision > 0 AND capture_epoch >= 0),
  CONSTRAINT fk_archive_operation_operation FOREIGN KEY (workspace_id, operation_id, intent_id) REFERENCES provider_operations(workspace_id, id, intent_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_operation_origin FOREIGN KEY (workspace_id, source_id, origin_ig_account_id) REFERENCES media_sources(workspace_id, id, origin_ig_account_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_operation_item FOREIGN KEY (workspace_id, source_id, media_item_id) REFERENCES media_items(workspace_id, source_id, id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_operation_intent FOREIGN KEY (workspace_id, intent_id, media_item_id) REFERENCES post_intents(workspace_id, id, media_item_id) ON DELETE RESTRICT,
  CONSTRAINT fk_archive_operation_artifact FOREIGN KEY (workspace_id, source_id, data_generation_id, intent_id, media_item_id, transit_artifact_ref, observation_revision, selected_asset_identity, validation_fingerprint, transit_artifact_kind) REFERENCES instagram_artifacts(workspace_id, source_id, data_generation_id, intent_id, media_item_id, artifact_ref, observation_revision, selected_asset_identity, validation_fingerprint, kind) ON DELETE RESTRICT
);

CREATE FUNCTION trg_archive_generation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.source_id, NEW.id, NEW.origin_ig_account_id, NEW.credential_id, NEW.authorization_id, NEW.app_id, NEW.subject_id, NEW.authorized_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.source_id, OLD.id, OLD.origin_ig_account_id, OLD.credential_id, OLD.authorization_id, OLD.app_id, OLD.subject_id, OLD.authorized_at) THEN
    RAISE EXCEPTION 'archive generation identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_generation_immutable BEFORE UPDATE ON instagram_archive_generations FOR EACH ROW EXECUTE FUNCTION trg_archive_generation_immutable();

CREATE FUNCTION trg_archive_artifact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.artifact_ref, NEW.source_id, NEW.data_generation_id, NEW.observation_revision, NEW.media_item_id, NEW.intent_id, NEW.kind, NEW.capture_epoch, NEW.policy_revision, NEW.selected_asset_identity, NEW.validation_fingerprint, NEW.upload_attempt_id, NEW.admitted_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.artifact_ref, OLD.source_id, OLD.data_generation_id, OLD.observation_revision, OLD.media_item_id, OLD.intent_id, OLD.kind, OLD.capture_epoch, OLD.policy_revision, OLD.selected_asset_identity, OLD.validation_fingerprint, OLD.upload_attempt_id, OLD.admitted_at) THEN
    RAISE EXCEPTION 'archive artifact capture is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_artifact_immutable BEFORE UPDATE ON instagram_artifacts FOR EACH ROW EXECUTE FUNCTION trg_archive_artifact_immutable();

CREATE FUNCTION trg_archive_operation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW IS DISTINCT FROM OLD THEN
    RAISE EXCEPTION 'archive issued operation capture is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_operation_immutable BEFORE UPDATE ON instagram_operation_provenance FOR EACH ROW EXECUTE FUNCTION trg_archive_operation_immutable();

ALTER TABLE media_source_destinations ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON media_source_destinations FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON media_source_destinations TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_media_source_destinations BEFORE UPDATE ON media_source_destinations FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_archive_generations ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_archive_generations FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_archive_generations TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_archive_generations BEFORE UPDATE ON instagram_archive_generations FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_source_policies ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_source_policies FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_source_policies TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_source_policies BEFORE UPDATE ON instagram_source_policies FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_media_index ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_media_index FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON instagram_media_index TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_media_index BEFORE UPDATE ON instagram_media_index FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_item_tombstones ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_item_tombstones FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_item_tombstones TO svc_ingress, svc_worker;

ALTER TABLE instagram_artifacts ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_artifacts FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_artifacts TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_artifacts BEFORE UPDATE ON instagram_artifacts FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_intent_provenance ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_intent_provenance FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_intent_provenance TO svc_ingress, svc_worker;
CREATE TRIGGER tg_touch_instagram_intent_provenance BEFORE UPDATE ON instagram_intent_provenance FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

ALTER TABLE instagram_operation_provenance ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON instagram_operation_provenance FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE ON instagram_operation_provenance TO svc_ingress, svc_worker;

CREATE FUNCTION trg_archive_media_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_provider TEXT;
BEGIN
  SELECT provider INTO source_provider FROM media_sources
    WHERE workspace_id = NEW.workspace_id AND id = NEW.source_id;
  IF (source_provider = 'instagram_archive') <> (NEW.source_data_generation_id IS NOT NULL OR NEW.source_projection_redacted) THEN
    RAISE EXCEPTION 'archive projection requires its authorization generation' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_media_generation BEFORE INSERT OR UPDATE OF source_id, source_data_generation_id, source_projection_redacted ON media_items FOR EACH ROW EXECUTE FUNCTION trg_archive_media_generation();

CREATE FUNCTION trg_archive_observation_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.observation_revision < OLD.observation_revision THEN
    RAISE EXCEPTION 'archive observation revision cannot go backwards' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER tg_archive_observation_revision BEFORE UPDATE ON instagram_media_index FOR EACH ROW EXECUTE FUNCTION trg_archive_observation_revision();


-- Trusted source_policy context supplies the one Python-resolved TTL and DB time
-- while retaining canonical workspace/source locks. No SQL TTL fallback or client
-- time/activation override. This read relation never replaces the INSERT arbiter.
CREATE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_archive_enabled boolean,
  p_source_id uuid DEFAULT NULL, p_media_item_id uuid DEFAULT NULL,
  p_existing_intent_id uuid DEFAULT NULL)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp AS $$
WITH source_facts AS (
  SELECT s.*, x.ratio, p.schema_version, p.revision AS policy_revision,
    p.minimum_age_days, p.include_credited, p.include_unknown,
    p.eligibility, p.ranking, p.destination_scope,
    g.id AS generation_id, g.lifecycle, c.state AS credential_state,
    c.expires_at AS credential_expires_at, c.provider AS credential_provider,
    a.state AS origin_state, a.provider_account_ref AS origin_ref,
    EXISTS (SELECT 1 FROM ig_accounts d WHERE d.workspace_id=s.workspace_id
      AND d.id=p_destination_id AND d.state='active') AS destination_active,
    EXISTS (SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id
      AND d.source_id=s.id AND d.ig_account_id=p_destination_id AND d.state='active') AS assigned,
    GREATEST(p.minimum_age_days::bigint,p_resolved_repost_ttl_days::bigint+1) AS effective_age
  FROM media_sources s
  LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL
  LEFT JOIN instagram_source_policies p ON p.workspace_id=s.workspace_id AND p.source_id=s.id
  LEFT JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
  LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id
  LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id
  WHERE s.workspace_id=p_workspace_id AND (p_source_id IS NULL OR s.id=p_source_id)
), assets AS (
  SELECT s.id AS source_id, m.id AS media_item_id, s.provider, s.source_use_epoch,
    NULL::uuid AS data_generation_id, NULL::bigint AS observation_revision,
    NULL::bigint AS policy_revision, NULL::bigint AS effective_age,
    CASE
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state='error' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN s.ratio=0 THEN 'source_off'
      WHEN NOT s.destination_active THEN 'destination_not_allowed'
      WHEN m.state<>'available' THEN 'unavailable'
    END AS base_reason
  FROM source_facts s JOIN media_items m ON m.workspace_id=s.workspace_id AND m.source_id=s.id
  WHERE s.provider='gdrive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
  UNION ALL
  SELECT s.id, m.id, s.provider, s.source_use_epoch,
    i.data_generation_id, i.observation_revision, s.policy_revision, s.effective_age,
    CASE
      WHEN p_archive_enabled IS NOT TRUE THEN 'archive_not_activated'
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN COALESCE(s.ratio>0,false) IS NOT TRUE THEN 'source_off'
      WHEN s.origin_state IS DISTINCT FROM 'active' OR s.origin_ref LIKE 'manual:%' THEN 'origin_unavailable'
      WHEN s.generation_id IS NULL OR s.lifecycle<>'active' THEN 'generation_unavailable'
      WHEN s.credential_state IS DISTINCT FROM 'active' OR s.credential_provider IS DISTINCT FROM 'ig_login'
        OR s.credential_expires_at<=p_evaluated_at THEN 'credential_unavailable'
      WHEN s.schema_version IS DISTINCT FROM 1 OR s.eligibility IS DISTINCT FROM 'supported'
        OR s.ranking IS DISTINCT FROM 'none' OR s.destination_scope IS DISTINCT FROM 'origin_only' THEN 'unsupported_source_policy'
      WHEN i.data_generation_id IS DISTINCT FROM s.generation_id THEN 'awaiting_reobservation'
      WHEN i.exclusion_reason='origin_identity_conflict' OR m.source_projection_redacted IS TRUE THEN 'identity_conflict_or_deleted'
      WHEN i.availability='excluded' AND i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.availability IS DISTINCT FROM 'available' OR (m.id IS NOT NULL AND m.state<>'available') THEN 'unavailable'
      WHEN i.validation_state='unsupported' OR i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN m.id IS NULL OR i.validation_state IS DISTINCT FROM 'supported'
        OR i.validation_generation_id IS DISTINCT FROM i.data_generation_id
        OR i.validation_observation_revision IS DISTINCT FROM i.observation_revision
        OR i.validation_fingerprint IS NULL OR i.validated_at IS NULL
        OR m.source_data_generation_id IS DISTINCT FROM i.data_generation_id THEN 'validation_pending'
      WHEN p_evaluated_at-i.original_at < s.effective_age*interval '1 day' THEN 'too_recent'
      WHEN i.attribution='credited' AND s.include_credited IS NOT TRUE THEN 'credited_excluded'
      WHEN i.attribution='unknown' AND s.include_unknown IS NOT TRUE THEN 'attribution_unknown'
      WHEN NOT s.destination_active OR s.origin_ig_account_id<>p_destination_id OR NOT s.assigned THEN 'destination_not_allowed'
    END
  FROM source_facts s JOIN instagram_media_index i ON i.workspace_id=s.workspace_id AND i.source_id=s.id
  LEFT JOIN media_items m ON m.workspace_id=i.workspace_id AND m.source_id=i.source_id AND m.id=i.media_item_id
  WHERE s.provider='instagram_archive' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
), context AS (
  SELECT a.*,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.media_item_id=a.media_item_id AND p.ig_account_id=p_destination_id
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')
      AND (p_existing_intent_id IS NULL OR p.id<>p_existing_intent_id)) AS live_intent_conflict,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('reject','unsupported','hold','seasonal') ORDER BY l.kind) AS blocking_locks,
    ARRAY(SELECT DISTINCT l.kind FROM post_locks l WHERE l.workspace_id=p_workspace_id
      AND l.media_item_id=a.media_item_id AND (l.ig_account_id IS NULL OR l.ig_account_id=p_destination_id)
      AND (l.expires_at IS NULL OR l.expires_at>p_evaluated_at)
      AND l.kind IN ('skip','recent') ORDER BY l.kind) AS warning_locks
  FROM assets a
)
SELECT c.source_id,c.media_item_id,c.provider,
  c.base_reason IS NULL AND NOT c.live_intent_conflict AND cardinality(c.blocking_locks)=0 AND cardinality(c.warning_locks)=0,
  c.base_reason IS NULL,
  COALESCE(c.base_reason,CASE WHEN c.live_intent_conflict THEN 'live_intent'
    WHEN cardinality(c.blocking_locks)>0 OR cardinality(c.warning_locks)>0 THEN 'locked' END),
  c.data_generation_id,c.observation_revision,c.policy_revision,c.source_use_epoch,c.effective_age,
  c.live_intent_conflict,c.blocking_locks,c.warning_locks
FROM context c
$$;
REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid) TO svc_ingress,svc_worker;
COMMENT ON FUNCTION fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid) IS
  'Invoker/RLS source and asset verdicts. Internal source_policy context owns DB evaluation time, Python-resolved workspace TTL and activation under canonical retained locks. Drive remains workspace-wide. Existing-intent context excludes only its own matching live intent; lock facts do not override base suitability or the database duplicate INSERT arbiter. No phase-1 consumer wiring.';

-- 10K real-role EXPLAIN demonstrates these exact lookup paths; no ranking indexes.
CREATE INDEX ix_archive_tombstone_lookup ON instagram_item_tombstones (workspace_id,key_version,parent_hmac);
CREATE INDEX ix_archive_generation_lookup ON instagram_media_index (workspace_id,source_id,data_generation_id);
