-- Versioned format evidence is independent of age and attribution policy.
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='instagram_media_index' AND column_name='validation_capture'
ALTER TABLE instagram_media_index ADD COLUMN validation_capture JSONB NULL;
ALTER TABLE instagram_media_index ADD CONSTRAINT ck_archive_validation_capture CHECK (
 validation_capture IS NULL OR ((
 jsonb_typeof(validation_capture)='object'
 AND validation_capture->>'generation_id'=data_generation_id::text
 AND (validation_capture->>'observation_revision')::bigint=observation_revision
 AND validation_capture->>'selected_asset_id'=COALESCE(first_child_id,parent_media_id)
 AND validation_capture->>'state' IN ('supported','unsupported','validation_pending')
 ) IS TRUE));
CREATE FUNCTION trg_archive_validation_capture_reset() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF (NEW.data_generation_id,NEW.observation_revision,NEW.first_child_id)
 IS DISTINCT FROM (OLD.data_generation_id,OLD.observation_revision,OLD.first_child_id) THEN
  NEW.validation_capture := NULL;
  NEW.validation_state := 'pending';
  NEW.validation_generation_id := NULL;
  NEW.validation_observation_revision := NULL;
  NEW.validation_fingerprint := NULL;
  NEW.validated_at := NULL;
  NEW.cache_generation_id := NULL;
  NEW.cache_observation_revision := NULL;
  NEW.cache_policy_revision := NULL;
  NEW.cached_eligible := NULL;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION trg_archive_validation_capture_reset() FROM PUBLIC;
CREATE TRIGGER archive_validation_capture_reset BEFORE UPDATE ON instagram_media_index
FOR EACH ROW EXECUTE FUNCTION trg_archive_validation_capture_reset();
