-- Existing pre-extension rows retain unknown admitted references/evidence.
-- runner:postcondition SELECT to_regclass('public.uq_archive_admitted_owner') IS NOT NULL
-- runner:postcondition SELECT to_regclass('public.uq_archive_unsettled_intent') IS NOT NULL
-- runner:postcondition SELECT count(*) = 3 FROM information_schema.columns WHERE table_schema='public' AND table_name='instagram_artifacts' AND column_name IN ('admitted_asset_ref','admitted_resource_type','creation_evidence')
ALTER TABLE instagram_artifacts ADD COLUMN admitted_asset_ref TEXT NULL;
ALTER TABLE instagram_artifacts ADD COLUMN admitted_resource_type TEXT NULL;
ALTER TABLE instagram_artifacts ADD COLUMN creation_evidence JSONB NULL;
ALTER TABLE instagram_artifacts ADD CONSTRAINT ck_archive_admitted_reference CHECK ((admitted_asset_ref IS NULL AND admitted_resource_type IS NULL AND creation_evidence IS NULL) OR (kind = 'transit' AND admitted_asset_ref ~ ('^ws/' || workspace_id::text || '/[0-9a-f]{20,64}$') AND admitted_resource_type IN ('image','video')) IS TRUE);
ALTER TABLE instagram_artifacts ADD CONSTRAINT ck_archive_creation_evidence CHECK (creation_evidence IS NULL OR (jsonb_typeof(creation_evidence) = 'object' AND octet_length(creation_evidence::text) <= 2048 AND creation_evidence ?& ARRAY['public_id','asset_id','version','resource_type','delivery_type','signature'] AND creation_evidence - ARRAY['public_id','asset_id','version','resource_type','delivery_type','signature'] = '{}'::jsonb AND creation_evidence->>'public_id' = admitted_asset_ref AND creation_evidence->>'resource_type' = admitted_resource_type AND creation_evidence->>'delivery_type' = 'authenticated' AND jsonb_typeof(creation_evidence->'version') = 'number' AND creation_evidence->>'version' ~ '^[1-9][0-9]{0,18}$' AND (creation_evidence->>'version')::numeric <= 9223372036854775807 AND jsonb_typeof(creation_evidence->'asset_id') = 'string' AND jsonb_typeof(creation_evidence->'signature') = 'string' AND creation_evidence->>'asset_id' ~ '^[a-zA-Z0-9_-]{1,128}$' AND creation_evidence->>'signature' ~ '^[0-9a-f]{40}$') IS TRUE);
ALTER TABLE instagram_artifacts ADD CONSTRAINT ck_archive_managed_completion CHECK (admitted_asset_ref IS NULL OR ((outcome IN ('unsettled','uncertain') AND created_asset_ref IS NULL AND creation_evidence IS NULL AND cleanup_state = 'pending') OR (outcome = 'settled' AND created_asset_ref = admitted_asset_ref AND creation_evidence IS NOT NULL AND cleanup_state IN ('pending','destroyed'))) IS TRUE);
CREATE UNIQUE INDEX uq_archive_unsettled_intent ON instagram_artifacts (workspace_id,intent_id)
 WHERE kind='transit' AND outcome IN ('unsettled','uncertain');
CREATE UNIQUE INDEX uq_archive_admitted_owner ON instagram_artifacts (workspace_id,admitted_asset_ref,admitted_resource_type)
 WHERE admitted_asset_ref IS NOT NULL;
CREATE OR REPLACE FUNCTION trg_archive_artifact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.artifact_ref, NEW.source_id, NEW.data_generation_id, NEW.observation_revision, NEW.media_item_id, NEW.intent_id, NEW.kind, NEW.capture_epoch, NEW.policy_revision, NEW.selected_asset_identity, NEW.validation_fingerprint, NEW.upload_attempt_id, NEW.admitted_at, NEW.admitted_asset_ref, NEW.admitted_resource_type)
     IS DISTINCT FROM (OLD.workspace_id, OLD.artifact_ref, OLD.source_id, OLD.data_generation_id, OLD.observation_revision, OLD.media_item_id, OLD.intent_id, OLD.kind, OLD.capture_epoch, OLD.policy_revision, OLD.selected_asset_identity, OLD.validation_fingerprint, OLD.upload_attempt_id, OLD.admitted_at, OLD.admitted_asset_ref, OLD.admitted_resource_type) THEN
    RAISE EXCEPTION 'archive artifact capture is immutable' USING ERRCODE = '23514';
  END IF;
  IF OLD.creation_evidence IS NOT NULL AND (NEW.creation_evidence,NEW.created_asset_ref,NEW.outcome,NEW.settled_at)
    IS DISTINCT FROM (OLD.creation_evidence,OLD.created_asset_ref,OLD.outcome,OLD.settled_at) THEN
    RAISE EXCEPTION 'archive creation evidence is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
