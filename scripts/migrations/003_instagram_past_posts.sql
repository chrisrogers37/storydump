-- Rename the Instagram archive to Instagram past posts: its table, the connect
-- columns, constraints, indexes, triggers and functions, the provider value and
-- the job kinds. It runs after 001 and 002 and only where no past-posts data
-- exists yet, because a stored item's identity and its deletion tombstones are
-- derived from the provider name.
-- The runner wraps this file and its ledger row in one transaction.
-- runner:postcondition SELECT to_regclass('public.instagram_past_posts_generations') IS NOT NULL AND to_regclass('public.instagram_archive_generations') IS NULL
-- runner:postcondition SELECT to_regprocedure('public.fn_past_posts_reconcile_jobs(integer,text,text)') IS NOT NULL AND to_regprocedure('public.fn_past_posts_redact_generation_records(uuid,uuid[])') IS NOT NULL
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.media_sources'::regclass AND conname = 'ck_sources_provider' AND pg_get_constraintdef(oid) LIKE '%instagram_past_posts%')

SET LOCAL search_path = public, pg_temp;
SET LOCAL check_function_bodies = off;

DO $order$
BEGIN
  IF (SELECT count(*) FROM runner.schema_migrations WHERE version IN (1, 2)) <> 2 THEN
    RAISE EXCEPTION '003 runs after 001 and 002; apply them first with apply --manual';
  END IF;
END $order$;

LOCK TABLE jobs, media_sources, media_items, oauth_states, instagram_archive_generations, instagram_media_index, instagram_media_assets, instagram_artifacts, instagram_item_tombstones, instagram_intent_provenance, instagram_operation_provenance, instagram_source_policies IN ACCESS EXCLUSIVE MODE;

DO $data$
BEGIN
  IF EXISTS (SELECT 1 FROM media_sources WHERE provider = 'instagram_archive')
     OR EXISTS (SELECT 1 FROM instagram_archive_generations)
     OR EXISTS (SELECT 1 FROM instagram_item_tombstones) THEN
    RAISE EXCEPTION 'Instagram past-posts data exists; its stored identities name the old provider';
  END IF;
END $data$;

-- Names
ALTER TABLE instagram_archive_generations RENAME TO instagram_past_posts_generations;
ALTER TABLE oauth_states RENAME COLUMN archive_entry TO past_posts_entry;
ALTER TABLE oauth_states RENAME COLUMN archive_use_history TO past_posts_use_history;
ALTER TABLE oauth_states RENAME COLUMN archive_expected_mix_revision TO past_posts_expected_mix_revision;
ALTER TABLE oauth_states RENAME COLUMN archive_preview_version TO past_posts_preview_version;
ALTER TABLE instagram_past_posts_generations RENAME CONSTRAINT ck_archive_generation_lifecycle TO ck_past_posts_generation_lifecycle;
ALTER TABLE instagram_past_posts_generations RENAME CONSTRAINT fk_archive_generation_credential TO fk_past_posts_generation_credential;
ALTER TABLE instagram_past_posts_generations RENAME CONSTRAINT fk_archive_generation_origin TO fk_past_posts_generation_origin;
ALTER TABLE instagram_past_posts_generations RENAME CONSTRAINT instagram_archive_generations_pkey TO instagram_past_posts_generations_pkey;
ALTER TABLE instagram_past_posts_generations RENAME CONSTRAINT instagram_archive_generations_workspace_id_fkey TO instagram_past_posts_generations_workspace_id_fkey;
ALTER TABLE instagram_past_posts_generations RENAME CONSTRAINT uq_archive_authorization TO uq_past_posts_authorization;
ALTER TABLE instagram_past_posts_generations RENAME CONSTRAINT uq_archive_generations_ws_source_id TO uq_past_posts_generations_ws_source_id;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_admitted_reference TO ck_past_posts_admitted_reference;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_artifact_attempt TO ck_past_posts_artifact_attempt;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_artifact_cleanup TO ck_past_posts_artifact_cleanup;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_artifact_destroyed TO ck_past_posts_artifact_destroyed;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_artifact_kind TO ck_past_posts_artifact_kind;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_artifact_settlement TO ck_past_posts_artifact_settlement;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_artifact_versions TO ck_past_posts_artifact_versions;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_creation_evidence TO ck_past_posts_creation_evidence;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT ck_archive_managed_completion TO ck_past_posts_managed_completion;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT fk_archive_artifact_generation TO fk_past_posts_artifact_generation;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT fk_archive_artifact_intent TO fk_past_posts_artifact_intent;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT fk_archive_artifact_item TO fk_past_posts_artifact_item;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT uq_archive_artifact_capture TO uq_past_posts_artifact_capture;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT uq_archive_artifact_ws_ref TO uq_past_posts_artifact_ws_ref;
ALTER TABLE instagram_artifacts RENAME CONSTRAINT uq_archive_upload_attempt TO uq_past_posts_upload_attempt;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT ck_archive_intent_transit_kind TO ck_past_posts_intent_transit_kind;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT ck_archive_intent_versions TO ck_past_posts_intent_versions;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT ck_archive_staged_bundle TO ck_past_posts_staged_bundle;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT fk_archive_intent_capture TO fk_past_posts_intent_capture;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT fk_archive_intent_intent TO fk_past_posts_intent_intent;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT fk_archive_intent_item TO fk_past_posts_intent_item;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT fk_archive_intent_origin TO fk_past_posts_intent_origin;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT fk_archive_intent_selection TO fk_past_posts_intent_selection;
ALTER TABLE instagram_intent_provenance RENAME CONSTRAINT fk_archive_intent_stage TO fk_past_posts_intent_stage;
ALTER TABLE instagram_item_tombstones RENAME CONSTRAINT ck_archive_tombstone_key TO ck_past_posts_tombstone_key;
ALTER TABLE instagram_item_tombstones RENAME CONSTRAINT ck_archive_tombstone_revision TO ck_past_posts_tombstone_revision;
ALTER TABLE instagram_item_tombstones RENAME CONSTRAINT fk_archive_tombstone_item TO fk_past_posts_tombstone_item;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT ck_archive_asset_availability TO ck_past_posts_asset_availability;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT ck_archive_asset_kind TO ck_past_posts_asset_kind;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT ck_archive_asset_revision TO ck_past_posts_asset_revision;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT ck_archive_asset_validation TO ck_past_posts_asset_validation;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT ck_archive_asset_validation_capture TO ck_past_posts_asset_validation_capture;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT ck_archive_asset_validation_evidence TO ck_past_posts_asset_validation_evidence;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT fk_archive_asset_bundle TO fk_past_posts_asset_bundle;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT fk_archive_asset_generation TO fk_past_posts_asset_generation;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT fk_archive_asset_item TO fk_past_posts_asset_item;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT uq_archive_asset_item TO uq_past_posts_asset_item;
ALTER TABLE instagram_media_assets RENAME CONSTRAINT uq_archive_asset_position TO uq_past_posts_asset_position;
ALTER TABLE instagram_media_index RENAME CONSTRAINT ck_archive_attribution TO ck_past_posts_attribution;
ALTER TABLE instagram_media_index RENAME CONSTRAINT ck_archive_availability TO ck_past_posts_availability;
ALTER TABLE instagram_media_index RENAME CONSTRAINT ck_archive_observation_counts TO ck_past_posts_observation_counts;
ALTER TABLE instagram_media_index RENAME CONSTRAINT ck_archive_observation_revision TO ck_past_posts_observation_revision;
ALTER TABLE instagram_media_index RENAME CONSTRAINT ck_archive_slide_count TO ck_past_posts_slide_count;
ALTER TABLE instagram_media_index RENAME CONSTRAINT ck_archive_validation TO ck_past_posts_validation;
ALTER TABLE instagram_media_index RENAME CONSTRAINT ck_archive_validation_capture TO ck_past_posts_validation_capture;
ALTER TABLE instagram_media_index RENAME CONSTRAINT ck_archive_validation_evidence TO ck_past_posts_validation_evidence;
ALTER TABLE instagram_media_index RENAME CONSTRAINT fk_archive_index_generation TO fk_past_posts_index_generation;
ALTER TABLE instagram_media_index RENAME CONSTRAINT fk_archive_index_item TO fk_past_posts_index_item;
ALTER TABLE instagram_media_index RENAME CONSTRAINT uq_archive_index_bundle TO uq_past_posts_index_bundle;
ALTER TABLE instagram_operation_provenance RENAME CONSTRAINT ck_archive_operation_transit_kind TO ck_past_posts_operation_transit_kind;
ALTER TABLE instagram_operation_provenance RENAME CONSTRAINT ck_archive_operation_versions TO ck_past_posts_operation_versions;
ALTER TABLE instagram_operation_provenance RENAME CONSTRAINT fk_archive_operation_artifact TO fk_past_posts_operation_artifact;
ALTER TABLE instagram_operation_provenance RENAME CONSTRAINT fk_archive_operation_intent TO fk_past_posts_operation_intent;
ALTER TABLE instagram_operation_provenance RENAME CONSTRAINT fk_archive_operation_item TO fk_past_posts_operation_item;
ALTER TABLE instagram_operation_provenance RENAME CONSTRAINT fk_archive_operation_operation TO fk_past_posts_operation_operation;
ALTER TABLE instagram_operation_provenance RENAME CONSTRAINT fk_archive_operation_origin TO fk_past_posts_operation_origin;
ALTER TABLE instagram_source_policies RENAME CONSTRAINT ck_archive_policy_age TO ck_past_posts_policy_age;
ALTER TABLE instagram_source_policies RENAME CONSTRAINT ck_archive_policy_carousel_slides TO ck_past_posts_policy_carousel_slides;
ALTER TABLE instagram_source_policies RENAME CONSTRAINT ck_archive_policy_modes TO ck_past_posts_policy_modes;
ALTER TABLE instagram_source_policies RENAME CONSTRAINT ck_archive_policy_version TO ck_past_posts_policy_version;
ALTER TABLE instagram_source_policies RENAME CONSTRAINT fk_archive_policy_source TO fk_past_posts_policy_source;
ALTER TABLE oauth_states RENAME CONSTRAINT ck_oauth_state_archive_choice TO ck_oauth_state_past_posts_choice;
ALTER TABLE oauth_states RENAME CONSTRAINT ck_oauth_state_archive_values TO ck_oauth_state_past_posts_values;
ALTER INDEX ix_archive_asset_bundle RENAME TO ix_past_posts_asset_bundle;
ALTER INDEX ix_archive_asset_suitability RENAME TO ix_past_posts_asset_suitability;
ALTER INDEX ix_archive_cleanup_generation RENAME TO ix_past_posts_cleanup_generation;
ALTER INDEX ix_archive_generation_lookup RENAME TO ix_past_posts_generation_lookup;
ALTER INDEX ix_archive_original_time RENAME TO ix_past_posts_original_time;
ALTER INDEX ix_archive_suitability RENAME TO ix_past_posts_suitability;
ALTER INDEX ix_archive_tombstone_item_revision RENAME TO ix_past_posts_tombstone_item_revision;
ALTER INDEX ix_archive_tombstone_lookup RENAME TO ix_past_posts_tombstone_lookup;
ALTER INDEX uq_archive_active_generation RENAME TO uq_past_posts_active_generation;
ALTER INDEX uq_archive_admitted_owner RENAME TO uq_past_posts_admitted_owner;
ALTER INDEX uq_archive_index_item RENAME TO uq_past_posts_index_item;
ALTER INDEX uq_archive_unsettled_intent RENAME TO uq_past_posts_unsettled_intent;
ALTER TRIGGER tg_archive_generation_immutable ON instagram_past_posts_generations RENAME TO tg_past_posts_generation_immutable;
ALTER TRIGGER tg_touch_instagram_archive_generations ON instagram_past_posts_generations RENAME TO tg_touch_instagram_past_posts_generations;
ALTER TRIGGER tg_archive_artifact_immutable ON instagram_artifacts RENAME TO tg_past_posts_artifact_immutable;
ALTER TRIGGER archive_validation_capture_reset ON instagram_media_index RENAME TO past_posts_validation_capture_reset;
ALTER TRIGGER tg_archive_observation_revision ON instagram_media_index RENAME TO tg_past_posts_observation_revision;
ALTER TRIGGER tg_archive_operation_immutable ON instagram_operation_provenance RENAME TO tg_past_posts_operation_immutable;
ALTER TRIGGER tg_archive_media_generation ON media_items RENAME TO tg_past_posts_media_generation;
ALTER FUNCTION fn_archive_reconcile_jobs(integer,text,text) RENAME TO fn_past_posts_reconcile_jobs;
ALTER FUNCTION fn_archive_redact_generation_records(uuid,uuid[]) RENAME TO fn_past_posts_redact_generation_records;
ALTER FUNCTION trg_archive_artifact_immutable() RENAME TO trg_past_posts_artifact_immutable;
ALTER FUNCTION trg_archive_generation_immutable() RENAME TO trg_past_posts_generation_immutable;
ALTER FUNCTION trg_archive_media_generation() RENAME TO trg_past_posts_media_generation;
ALTER FUNCTION trg_archive_observation_revision() RENAME TO trg_past_posts_observation_revision;
ALTER FUNCTION trg_archive_operation_immutable() RENAME TO trg_past_posts_operation_immutable;
ALTER FUNCTION trg_archive_validation_capture_reset() RENAME TO trg_past_posts_validation_capture_reset;

-- The provider value
DROP INDEX uq_archive_origin;
ALTER TABLE media_sources DROP CONSTRAINT ck_sources_provider, DROP CONSTRAINT ck_sources_origin, DROP CONSTRAINT ck_sources_current_authorization;
ALTER TABLE media_sources
  ADD CONSTRAINT ck_sources_provider CHECK ((provider = ANY (ARRAY['gdrive'::text, 'instagram_past_posts'::text]))),
  ADD CONSTRAINT ck_sources_origin CHECK (((provider = 'instagram_past_posts'::text) = (origin_ig_account_id IS NOT NULL))),
  ADD CONSTRAINT ck_sources_current_authorization CHECK (((current_authorization_generation_id IS NULL) OR (provider = 'instagram_past_posts'::text)));
CREATE UNIQUE INDEX uq_past_posts_origin ON media_sources USING btree (workspace_id, origin_ig_account_id) WHERE (provider = 'instagram_past_posts'::text);

-- The job kinds, and the jobs already stored under them
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind, DROP CONSTRAINT ck_jobs_system_kinds;
UPDATE jobs SET kind = 'past_posts_lifecycle_sweep',
  serialization_key = CASE WHEN serialization_key = 'archive_lifecycle_sweep' THEN 'past_posts_lifecycle_sweep' ELSE serialization_key END
  WHERE kind = 'archive_lifecycle_sweep';
UPDATE jobs SET kind = 'reconcile_past_posts_accounts',
  serialization_key = regexp_replace(serialization_key, '^archive-reconcile:', 'past-posts-reconcile:')
  WHERE kind = 'reconcile_archive_accounts';
ALTER TABLE jobs
  ADD CONSTRAINT ck_jobs_kind CHECK ((kind = ANY (ARRAY['plan_slot'::text, 'publish_pipeline'::text, 'deliver_outbox'::text, 'sync_media_source'::text, 'first_ingest_chunk'::text, 'refresh_credential'::text, 'offboard_workspace'::text, 'revoke_workspace_credentials'::text, 'reauth_prompt'::text, 'reconcile_ambiguous'::text, 'reap_expired'::text, 'reap_transit_assets'::text, 'retention_sweep'::text, 'reencrypt_credentials'::text, 'send_email'::text, 'alert_stranded_sources'::text, 'activation_nudge_sweep'::text, 'convert_source_mix'::text, 'past_posts_lifecycle_sweep'::text, 'meta_deletion_cleanup'::text, 'reconcile_past_posts_accounts'::text]))),
  ADD CONSTRAINT ck_jobs_system_kinds CHECK (((workspace_id IS NULL) = (kind = ANY (ARRAY['reconcile_ambiguous'::text, 'reap_expired'::text, 'reap_transit_assets'::text, 'retention_sweep'::text, 'reencrypt_credentials'::text, 'send_email'::text, 'alert_stranded_sources'::text, 'activation_nudge_sweep'::text, 'past_posts_lifecycle_sweep'::text]))));

-- Functions whose parameter or result names change are dropped and made again;
-- every function whose text names the renamed objects is replaced, in schema order.
DROP FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid);
DROP FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, boolean);
DROP FUNCTION fn_claim_job(text, text, interval, integer, boolean);
DROP FUNCTION fn_clock_tick(integer, interval, jsonb, boolean);
DROP FUNCTION fn_reaper_sweep(integer, interval, interval, boolean);

CREATE OR REPLACE FUNCTION fn_offboard_finalize(p_ws uuid, p_grace interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  PERFORM set_config('app.actor_kind', 'system', true);
  IF NOT EXISTS (SELECT 1 FROM workspaces
                  WHERE id = p_ws AND state = 'offboarding'
                    AND offboarding_at IS NOT NULL AND offboarding_at + p_grace <= now()) THEN
    RAISE EXCEPTION 'workspace % is not finalizable (state/grace guard)', p_ws;
  END IF;
  IF EXISTS (SELECT 1 FROM post_intents WHERE workspace_id = p_ws
              AND state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) THEN
    RAISE EXCEPTION 'workspace % still holds live intents', p_ws;
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_artifacts WHERE workspace_id = p_ws AND kind = 'transit'
              AND (outcome IN ('unsettled','uncertain') OR cleanup_state = 'pending')) THEN
    RAISE EXCEPTION 'workspace % still holds past-posts transit cleanup obligations', p_ws;
  END IF;
  DELETE FROM workspaces WHERE id = p_ws;
END $$;

CREATE OR REPLACE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE p_past_posts_enabled boolean := false; locked_workspaces uuid[]:='{}'; source_due record; source_capture jsonb; lock_ws uuid; k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  FOR lock_ws IN SELECT DISTINCT workspace_id FROM (
   (SELECT a.workspace_id FROM ig_accounts a JOIN workspaces w ON w.id=a.workspace_id
    WHERE a.state='active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at<=now()
    AND w.state='active' AND NOT w.is_paused ORDER BY a.next_slot_at,a.id LIMIT GREATEST(p_max,0))
   UNION (SELECT workspace_id FROM ig_accounts WHERE state='reauth_required'
    AND (last_reauth_prompt_at IS NULL OR last_reauth_prompt_at<=now()-interval '7 days')
    ORDER BY id LIMIT GREATEST(p_max,0))
   UNION (SELECT workspace_id FROM oauth_credentials WHERE state='active' AND provider='ig_login'
    AND next_refresh_at IS NOT NULL AND next_refresh_at<=now() ORDER BY id LIMIT GREATEST(p_max,0))
   UNION (SELECT s.workspace_id FROM media_sources s WHERE s.state='active' AND s.next_sync_at IS NOT NULL
    AND s.next_sync_at<=now() AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_past_posts_enabled) r WHERE r.admitted)
    ORDER BY s.next_sync_at,s.id LIMIT GREATEST(p_max,0))
  ) candidate ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
     WHERE kind = k AND workspace_id IS NULL
       AND state = 'leased' AND locked_until < now();
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      due_at := GREATEST(now(), COALESCE(last_done + cadence, now()));
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
      VALUES (k, NULL, 'bulk', k,
              due_at, 3,
              due_at + interval '6 hours',
              jsonb_build_object('v', 1));
      n4 := n4 + 1;
    END IF;
  END LOOP;
  rem := GREATEST(p_max - n4, 0);
  WITH due AS (
    SELECT a.id, a.workspace_id, a.next_slot_at,
           COALESCE(a.tz, w.tz)                                   AS eff_tz,
           COALESCE(a.posts_per_day, w.posts_per_day)             AS eff_ppd,
           COALESCE(a.posting_hours_start, w.posting_hours_start) AS eff_start,
           COALESCE(a.posting_hours_end, w.posting_hours_end)     AS eff_end
      FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id
     WHERE a.workspace_id=ANY(locked_workspaces) AND a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at,a.id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'ig_account_id', d.id, 'slot_at', d.next_slot_at)
      FROM due d
  )
  UPDATE ig_accounts a
     SET next_slot_at = fn_next_slot(d.next_slot_at, d.eff_tz, d.eff_start, d.eff_end, d.eff_ppd)
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n1 = ROW_COUNT;
  rem := rem - n1;
  WITH due AS (
    SELECT id, workspace_id FROM oauth_credentials
     WHERE workspace_id=ANY(locked_workspaces) AND state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
       AND provider = 'ig_login'
     ORDER BY id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'credential_id', d.id)
      FROM due d
  )
  UPDATE oauth_credentials c SET next_refresh_at = now() + p_refresh_cadence
    FROM due d WHERE c.id = d.id;
  GET DIAGNOSTICS n2 = ROW_COUNT;
  rem := rem - n2;
  FOR source_due IN SELECT s.id,s.workspace_id FROM media_sources s
   WHERE s.workspace_id=ANY(locked_workspaces) AND s.state='active' AND s.next_sync_at IS NOT NULL AND s.next_sync_at<=now()
   AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_past_posts_enabled) r WHERE r.admitted)
   ORDER BY s.next_sync_at,s.id LIMIT rem
  LOOP
   source_capture:=fn_source_read_capture(source_due.workspace_id,source_due.id,p_past_posts_enabled);
   IF source_capture IS NULL THEN CONTINUE; END IF;
   INSERT INTO jobs(kind,workspace_id,lane,serialization_key,run_at,max_attempts,deadline_at,payload)
    VALUES('sync_media_source',source_due.workspace_id,'bulk','src:'||source_due.id,now(),5,now()+interval '6 hours',
     jsonb_build_object('v',1,'source_id',source_due.id,'reason','baseline') ||
     CASE WHEN source_capture->>'provider'='instagram_past_posts' THEN jsonb_build_object('source_read_capture',source_capture) ELSE '{}'::jsonb END);
   UPDATE media_sources SET next_sync_at=NULL WHERE id=source_due.id AND workspace_id=source_due.workspace_id;
   n3:=n3+1;
  END LOOP;
  rem := rem - n3;
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.workspace_id=ANY(locked_workspaces) AND a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     ORDER BY a.id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;

CREATE OR REPLACE FUNCTION trg_past_posts_generation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.source_id, NEW.id, NEW.origin_ig_account_id, NEW.credential_id, NEW.authorization_id, NEW.app_id, NEW.subject_id, NEW.authorized_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.source_id, OLD.id, OLD.origin_ig_account_id, OLD.credential_id, OLD.authorization_id, OLD.app_id, OLD.subject_id, OLD.authorized_at) THEN
    RAISE EXCEPTION 'past-posts generation identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION trg_past_posts_artifact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.artifact_ref, NEW.source_id, NEW.data_generation_id, NEW.observation_revision, NEW.media_item_id, NEW.intent_id, NEW.kind, NEW.capture_epoch, NEW.policy_revision, NEW.selected_asset_identity, NEW.validation_fingerprint, NEW.upload_attempt_id, NEW.admitted_at, NEW.admitted_asset_ref, NEW.admitted_resource_type)
     IS DISTINCT FROM (OLD.workspace_id, OLD.artifact_ref, OLD.source_id, OLD.data_generation_id, OLD.observation_revision, OLD.media_item_id, OLD.intent_id, OLD.kind, OLD.capture_epoch, OLD.policy_revision, OLD.selected_asset_identity, OLD.validation_fingerprint, OLD.upload_attempt_id, OLD.admitted_at, OLD.admitted_asset_ref, OLD.admitted_resource_type) THEN
    RAISE EXCEPTION 'past-posts artifact capture is immutable' USING ERRCODE = '23514';
  END IF;
  IF OLD.creation_evidence IS NOT NULL AND (NEW.creation_evidence,NEW.created_asset_ref,NEW.outcome,NEW.settled_at)
    IS DISTINCT FROM (OLD.creation_evidence,OLD.created_asset_ref,OLD.outcome,OLD.settled_at) THEN
    RAISE EXCEPTION 'past-posts creation evidence is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION trg_past_posts_operation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW IS DISTINCT FROM OLD THEN
    RAISE EXCEPTION 'past-posts issued operation capture is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION trg_past_posts_media_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_provider TEXT;
BEGIN
  SELECT provider INTO source_provider FROM media_sources
    WHERE workspace_id = NEW.workspace_id AND id = NEW.source_id;
  IF (source_provider = 'instagram_past_posts') <> (NEW.source_data_generation_id IS NOT NULL OR NEW.source_projection_redacted) THEN
    RAISE EXCEPTION 'past-posts projection requires its authorization generation' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION trg_past_posts_observation_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.observation_revision < OLD.observation_revision THEN
    RAISE EXCEPTION 'past-posts observation revision cannot go backwards' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_past_posts_enabled boolean,
  p_source_id uuid DEFAULT NULL, p_media_item_id uuid DEFAULT NULL,
  p_existing_intent_id uuid DEFAULT NULL)
RETURNS TABLE (source_id uuid, media_item_id uuid, provider text,
  suitable boolean, base_suitable boolean, reason text,
  data_generation_id uuid, observation_revision bigint, policy_revision bigint,
  source_use_epoch bigint, effective_minimum_age_days bigint,
  live_intent_conflict boolean, blocking_locks text[], warning_locks text[])
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM fn_source_asset_suitability(p_workspace_id,p_destination_id,
  p_resolved_repost_ttl_days,p_evaluated_at,p_past_posts_enabled,p_source_id,
  p_media_item_id,p_existing_intent_id,true)
$$;

CREATE OR REPLACE FUNCTION fn_source_read_authority(p_workspace_id uuid,p_source_id uuid,p_activated boolean)
RETURNS TABLE(source_id uuid,provider text,admitted boolean,reason text,
 source_use_epoch bigint,generation_id uuid,credential_id uuid,account_ref text)
LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
WITH facts AS (
 SELECT s.id,s.provider,s.source_use_epoch,g.id AS generation_id,c.id AS credential_id,
 a.provider_account_ref AS account_ref,
 CASE
 WHEN s.provider='gdrive' THEN NULL
 WHEN s.provider<>'instagram_past_posts' THEN 'unsupported_source_provider'
 WHEN p_activated IS NOT TRUE OR w.source_mix_policy_version IS DISTINCT FROM 1
  OR w.source_mix_conversion_version IS DISTINCT FROM 1 OR w.source_mix_revision IS NULL
  OR w.source_mix_converted_at IS NULL OR (w.source_mix_mode IN ('equal','custom')) IS NOT TRUE
  OR w.source_membership_policy IS DISTINCT FROM 'reset_equal' THEN 'past_posts_not_activated'
 WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
 WHEN COALESCE(x.ratio>0,false) IS NOT TRUE THEN 'source_off'
 WHEN a.state IS DISTINCT FROM 'active' OR a.provider_account_ref LIKE 'manual:%'
  OR NOT EXISTS(SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id
   AND d.source_id=s.id AND d.ig_account_id=s.origin_ig_account_id AND d.state='active') THEN 'origin_unavailable'
 WHEN g.id IS NULL OR g.lifecycle<>'active' THEN 'generation_unavailable'
 WHEN c.state IS DISTINCT FROM 'active' OR c.provider IS DISTINCT FROM 'ig_login'
  OR c.expires_at<=clock_timestamp() THEN 'credential_unavailable'
 END AS reason
 FROM media_sources s JOIN workspaces w ON w.id=s.workspace_id
 LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL
 LEFT JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND a.id=s.origin_ig_account_id
 LEFT JOIN instagram_past_posts_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
 LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id
 WHERE s.workspace_id=p_workspace_id AND s.id=p_source_id
)
SELECT id,provider,reason IS NULL,reason,source_use_epoch,generation_id,credential_id,account_ref FROM facts
$$;

CREATE OR REPLACE FUNCTION fn_lock_source_read_prefix(p_ws uuid)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('sources:'||p_ws,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('case_mix:'||p_ws,0));
 PERFORM id FROM workspaces WHERE id=p_ws FOR UPDATE;
 PERFORM id FROM ig_accounts WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM media_sources WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM oauth_credentials WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM instagram_past_posts_generations WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
END $$;

CREATE OR REPLACE FUNCTION fn_source_job_read_reason(p_ws uuid,p_payload jsonb,p_activated boolean)
RETURNS text LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE a record; capture jsonb:=p_payload->'source_read_capture';
BEGIN
 SELECT r.* INTO a FROM media_sources s CROSS JOIN LATERAL fn_source_read_authority(p_ws,s.id,p_activated) r
 WHERE s.workspace_id=p_ws AND s.id::text=p_payload->>'source_id';
 IF NOT FOUND OR a.provider<>'instagram_past_posts' THEN RETURN NULL; END IF;
 IF jsonb_typeof(capture) IS DISTINCT FROM 'object' OR capture->'v' IS DISTINCT FROM '1'::jsonb
  OR NOT capture ?& ARRAY['workspace_id','source_id','provider','source_use_epoch','generation_id','credential_id','account_ref']
  OR capture->>'source_use_epoch' IS NULL OR capture->>'generation_id' IS NULL OR capture->>'credential_id' IS NULL OR capture->>'account_ref' IS NULL
  THEN RETURN 'source_capture_missing'; END IF;
 IF NOT a.admitted THEN RETURN a.reason; END IF;
 IF capture->>'workspace_id' IS DISTINCT FROM p_ws::text OR capture->>'source_id' IS DISTINCT FROM a.source_id::text
  OR capture->>'provider' IS DISTINCT FROM a.provider THEN RETURN 'source_capture_mismatch'; END IF;
 IF capture->>'generation_id' IS DISTINCT FROM a.generation_id::text THEN RETURN 'source_generation_changed'; END IF;
 IF capture->>'source_use_epoch' IS DISTINCT FROM a.source_use_epoch::text THEN RETURN 'source_epoch_changed'; END IF;
 IF capture->>'credential_id' IS DISTINCT FROM a.credential_id::text OR capture->>'account_ref' IS DISTINCT FROM a.account_ref
  THEN RETURN 'source_credential_changed'; END IF;
 RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int,p_past_posts_enabled boolean)
RETURNS SETOF jobs LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE candidate record; selected jobs; refusal text; source_prefix_applied boolean;
BEGIN
 FOR candidate IN SELECT j.id,j.workspace_id,j.kind FROM jobs j
  WHERE j.state='ready' AND j.lane=p_lane AND j.run_at<=now()
  ORDER BY j.run_at,j.id
 LOOP
  BEGIN
   source_prefix_applied:=false;
   IF candidate.workspace_id IS NOT NULL AND candidate.kind IN ('sync_media_source','first_ingest_chunk','convert_source_mix') THEN
    IF NOT pg_try_advisory_xact_lock(hashtextextended('sources:'||candidate.workspace_id,0)) THEN
     RAISE SQLSTATE 'P0002';
    END IF;
    PERFORM fn_lock_source_read_prefix(candidate.workspace_id);
    source_prefix_applied:=true;
   END IF;
   SELECT j.* INTO selected FROM jobs j WHERE j.id=candidate.id AND j.state='ready' AND j.run_at<=now()
    AND NOT EXISTS(SELECT 1 FROM jobs h WHERE h.serialization_key=j.serialization_key AND h.state='leased')
    AND NOT EXISTS(SELECT 1 FROM provider_quarantine q WHERE q.workspace_id=j.workspace_id
     AND q.scope_ref=j.serialization_key AND q.quarantined_until>now())
    AND (j.workspace_id IS NULL OR (SELECT count(*) FROM jobs w WHERE w.state='leased' AND w.lane=j.lane
     AND w.workspace_id=j.workspace_id)<p_ws_lane_cap)
    FOR UPDATE OF j SKIP LOCKED;
   IF NOT FOUND THEN RAISE SQLSTATE 'P0002'; END IF;
   IF selected.kind IN ('sync_media_source','first_ingest_chunk','convert_source_mix') AND
    (NOT source_prefix_applied OR selected.workspace_id IS DISTINCT FROM candidate.workspace_id) THEN
    RAISE SQLSTATE 'P0002';
   END IF;
   refusal:=NULL;
   IF selected.kind IN ('sync_media_source','first_ingest_chunk') THEN
    refusal:=fn_source_job_read_reason(selected.workspace_id,selected.payload,p_past_posts_enabled);
   END IF;
   IF refusal IS NOT NULL THEN
    UPDATE jobs SET state='failed',payload=payload||jsonb_build_object('source_read_refusal',refusal)
     WHERE id=selected.id AND state='ready';
    CONTINUE;
   END IF;
   RETURN QUERY UPDATE jobs SET state='leased',locked_by=p_worker,lease_token=gen_random_uuid(),
    locked_until=now()+p_lease,attempts=attempts+1 WHERE id=selected.id AND state='ready' RETURNING jobs.*;
   RETURN;
  EXCEPTION WHEN no_data_found THEN NULL;
  END;
 END LOOP;
END $$;

CREATE OR REPLACE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb, p_past_posts_enabled boolean)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, past_posts_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE locked_workspaces uuid[]:='{}'; source_due record; source_capture jsonb; lock_ws uuid; k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  FOR lock_ws IN SELECT DISTINCT workspace_id FROM (
   (SELECT a.workspace_id FROM ig_accounts a JOIN workspaces w ON w.id=a.workspace_id
    WHERE a.state='active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at<=now()
    AND w.state='active' AND NOT w.is_paused ORDER BY a.next_slot_at,a.id LIMIT GREATEST(p_max,0))
   UNION (SELECT workspace_id FROM ig_accounts WHERE state='reauth_required'
    AND (last_reauth_prompt_at IS NULL OR last_reauth_prompt_at<=now()-interval '7 days')
    ORDER BY id LIMIT GREATEST(p_max,0))
   UNION (SELECT workspace_id FROM oauth_credentials WHERE state='active' AND provider='ig_login'
    AND next_refresh_at IS NOT NULL AND next_refresh_at<=now() ORDER BY id LIMIT GREATEST(p_max,0))
   UNION (SELECT s.workspace_id FROM media_sources s WHERE s.state='active' AND s.next_sync_at IS NOT NULL
    AND s.next_sync_at<=now() AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_past_posts_enabled) r WHERE r.admitted)
    ORDER BY s.next_sync_at,s.id LIMIT GREATEST(p_max,0))
  ) candidate ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
     WHERE kind = k AND workspace_id IS NULL
       AND state = 'leased' AND locked_until < now();
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      due_at := GREATEST(now(), COALESCE(last_done + cadence, now()));
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
      VALUES (k, NULL, 'bulk', k,
              due_at, 3,
              due_at + interval '6 hours',
              jsonb_build_object('v', 1));
      n4 := n4 + 1;
    END IF;
  END LOOP;
  rem := GREATEST(p_max - n4, 0);
  WITH due AS (
    SELECT a.id, a.workspace_id, a.next_slot_at,
           COALESCE(a.tz, w.tz)                                   AS eff_tz,
           COALESCE(a.posts_per_day, w.posts_per_day)             AS eff_ppd,
           COALESCE(a.posting_hours_start, w.posting_hours_start) AS eff_start,
           COALESCE(a.posting_hours_end, w.posting_hours_end)     AS eff_end
      FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id
     WHERE a.workspace_id=ANY(locked_workspaces) AND a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at,a.id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'ig_account_id', d.id, 'slot_at', d.next_slot_at)
      FROM due d
  )
  UPDATE ig_accounts a
     SET next_slot_at = fn_next_slot(d.next_slot_at, d.eff_tz, d.eff_start, d.eff_end, d.eff_ppd)
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n1 = ROW_COUNT;
  rem := rem - n1;
  WITH due AS (
    SELECT id, workspace_id FROM oauth_credentials
     WHERE workspace_id=ANY(locked_workspaces) AND state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
       AND provider = 'ig_login'
     ORDER BY id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'credential_id', d.id)
      FROM due d
  )
  UPDATE oauth_credentials c SET next_refresh_at = now() + p_refresh_cadence
    FROM due d WHERE c.id = d.id;
  GET DIAGNOSTICS n2 = ROW_COUNT;
  rem := rem - n2;
  FOR source_due IN SELECT s.id,s.workspace_id FROM media_sources s
   WHERE s.workspace_id=ANY(locked_workspaces) AND s.state='active' AND s.next_sync_at IS NOT NULL AND s.next_sync_at<=now()
   AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_past_posts_enabled) r WHERE r.admitted)
   ORDER BY s.next_sync_at,s.id LIMIT rem
  LOOP
   source_capture:=fn_source_read_capture(source_due.workspace_id,source_due.id,p_past_posts_enabled);
   IF source_capture IS NULL THEN CONTINUE; END IF;
   INSERT INTO jobs(kind,workspace_id,lane,serialization_key,run_at,max_attempts,deadline_at,payload)
    VALUES('sync_media_source',source_due.workspace_id,'bulk','src:'||source_due.id,now(),5,now()+interval '6 hours',
     jsonb_build_object('v',1,'source_id',source_due.id,'reason','baseline') ||
     CASE WHEN source_capture->>'provider'='instagram_past_posts' THEN jsonb_build_object('source_read_capture',source_capture) ELSE '{}'::jsonb END);
   UPDATE media_sources SET next_sync_at=NULL WHERE id=source_due.id AND workspace_id=source_due.workspace_id;
   n3:=n3+1;
  END LOOP;
  rem := rem - n3;
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.workspace_id=ANY(locked_workspaces) AND a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     ORDER BY a.id LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'ig_account_id', d.id)
      FROM due d
  )
  UPDATE ig_accounts a SET last_reauth_prompt_at = now()
    FROM due d WHERE a.id = d.id;
  GET DIAGNOSTICS n5 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4, n5;
END $$;

CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval,p_past_posts_enabled boolean)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE locked_workspaces uuid[]:='{}'; lock_ws uuid; expired_job record; refusal text; n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  FOR lock_ws IN SELECT DISTINCT workspace_id FROM (
   (SELECT workspace_id FROM jobs WHERE state='leased' AND locked_until<now() ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM jobs WHERE state='ready' AND deadline_at<=now()
    AND kind IN ('reap_expired','reconcile_ambiguous','alert_stranded_sources','reap_transit_assets',
     'plan_slot','refresh_credential','reauth_prompt','deliver_outbox','sync_media_source','first_ingest_chunk','convert_source_mix') ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_intents WHERE cancel_requested AND cap_consumed_on IS NULL
    AND state IN ('scheduled','prompt_pending','awaiting_approval','approved') ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_intents WHERE state IN ('scheduled','prompt_pending')
    AND schedule_slot_at<now() AND origin='cadence' ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT i.workspace_id FROM post_intents i JOIN workspaces w ON w.id=i.workspace_id
    WHERE i.state='awaiting_approval' AND i.entered_state_at<now()-COALESCE(w.approval_ttl_minutes*interval '1 minute',p_approval_ttl)
    ORDER BY i.id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM post_locks WHERE expires_at IS NOT NULL AND expires_at<now() ORDER BY id LIMIT GREATEST(p_lim,0))
  ) candidate WHERE workspace_id IS NOT NULL ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  c:=0;
  FOR expired_job IN SELECT id,workspace_id,kind,payload FROM jobs
   WHERE (workspace_id IS NULL OR workspace_id=ANY(locked_workspaces)) AND state='leased' AND locked_until<now() ORDER BY id LIMIT rem
  LOOP
   refusal:=NULL;
   IF expired_job.kind IN ('sync_media_source','first_ingest_chunk') THEN
    refusal:=fn_source_job_read_reason(expired_job.workspace_id,expired_job.payload,p_past_posts_enabled);
   END IF;
   UPDATE jobs SET state=CASE WHEN refusal IS NULL THEN 'ready' ELSE 'failed' END,
    locked_by=NULL,lease_token=NULL,locked_until=NULL,
    payload=CASE WHEN refusal IS NULL THEN payload ELSE payload||jsonb_build_object('source_read_refusal',refusal) END
   WHERE id=expired_job.id AND state='leased' AND locked_until<now();
   IF FOUND THEN c:=c+1; END IF;
  END LOOP;
  n:=n+c;rem:=rem-c;
  UPDATE post_intents SET state = 'cancelled'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE workspace_id=ANY(locked_workspaces) AND cancel_requested AND cap_consumed_on IS NULL
                   AND state IN ('scheduled','prompt_pending','awaiting_approval','approved')
                 ORDER BY id LIMIT rem)
     AND state IN ('scheduled','prompt_pending','awaiting_approval','approved');
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE workspace_id=ANY(locked_workspaces) AND state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                   AND origin = 'cadence'
                 ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.workspace_id=ANY(locked_workspaces) AND i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      ORDER BY i2.id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM post_locks
   WHERE id IN (SELECT id FROM post_locks
                 WHERE workspace_id=ANY(locked_workspaces) AND expires_at IS NOT NULL AND expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE workspace_invitations SET state = 'expired'
   WHERE id IN (SELECT id FROM workspace_invitations
                 WHERE state = 'pending' AND expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM onboarding_sessions
   WHERE id IN (SELECT id FROM onboarding_sessions WHERE expires_at < now() ORDER BY id LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  WITH ended AS (
    UPDATE jobs SET state = 'failed',
                    payload = payload || jsonb_build_object('ended', 'deadline')
     WHERE id IN (SELECT id FROM jobs
                   WHERE (workspace_id IS NULL OR workspace_id=ANY(locked_workspaces)) AND state = 'ready' AND deadline_at <= now()
                     AND kind IN ('reap_expired', 'reconcile_ambiguous',
                                  'alert_stranded_sources', 'reap_transit_assets',
                                  'plan_slot', 'refresh_credential', 'reauth_prompt',
                                  'deliver_outbox', 'sync_media_source',
                                  'first_ingest_chunk','convert_source_mix')
                   ORDER BY id LIMIT rem)
       AND state = 'ready' AND deadline_at <= now()
    RETURNING kind, workspace_id, payload, payload->>'source_id' AS source_id
  ), rearmed AS (
    UPDATE media_sources s
       SET next_sync_at = now() + interval '24 hours'
      FROM ended e
     WHERE e.kind IN ('sync_media_source', 'first_ingest_chunk')
       AND s.id::text = e.source_id
       AND s.workspace_id = e.workspace_id
       AND s.state = 'active' AND s.next_sync_at IS NULL
       AND fn_source_job_read_reason(e.workspace_id,e.payload,p_past_posts_enabled) IS NULL
       AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_past_posts_enabled) a WHERE a.admitted)
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;

CREATE OR REPLACE FUNCTION trg_past_posts_validation_capture_reset() RETURNS trigger
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

CREATE OR REPLACE FUNCTION fn_source_asset_suitability(
  p_workspace_id uuid, p_destination_id uuid, p_resolved_repost_ttl_days integer,
  p_evaluated_at timestamptz, p_past_posts_enabled boolean,
  p_source_id uuid, p_media_item_id uuid,
  p_existing_intent_id uuid, p_require_validation boolean)
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
    p.eligibility, p.ranking, p.destination_scope, p.carousel_slides,
    g.id AS generation_id, g.lifecycle, c.state AS credential_state,
    c.expires_at AS credential_expires_at, c.provider AS credential_provider,
    a.state AS origin_state, a.provider_account_ref AS origin_ref,
    EXISTS (SELECT 1 FROM ig_accounts d WHERE d.workspace_id=s.workspace_id
      AND d.id=p_destination_id AND (d.state='active' OR (s.provider='gdrive' AND p_require_validation IS FALSE AND d.state='reauth_required'))) AS destination_active,
    EXISTS (SELECT 1 FROM media_source_destinations d WHERE d.workspace_id=s.workspace_id
      AND d.source_id=s.id AND d.ig_account_id=p_destination_id AND d.state='active') AS assigned,
    GREATEST(p.minimum_age_days::bigint,p_resolved_repost_ttl_days::bigint+1) AS effective_age
  FROM media_sources s
  LEFT JOIN category_post_case_mix x ON x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL
  LEFT JOIN instagram_source_policies p ON p.workspace_id=s.workspace_id AND p.source_id=s.id
  LEFT JOIN instagram_past_posts_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
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
      WHEN p_past_posts_enabled IS NOT TRUE THEN 'past_posts_not_activated'
      WHEN p_evaluated_at IS NULL OR p_resolved_repost_ttl_days IS NULL THEN 'context_unavailable'
      WHEN s.state<>'active' OR COALESCE((s.config->>'removed')::boolean,false) THEN 'source_unavailable'
      WHEN COALESCE(s.ratio>0,false) IS NOT TRUE THEN 'source_off'
      WHEN s.origin_state IS DISTINCT FROM 'active' OR s.origin_ref LIKE 'manual:%' THEN 'origin_unavailable'
      WHEN s.generation_id IS NULL OR s.lifecycle<>'active' THEN 'generation_unavailable'
      WHEN s.credential_state IS DISTINCT FROM 'active' OR s.credential_provider IS DISTINCT FROM 'ig_login'
        OR s.credential_expires_at<=p_evaluated_at THEN 'credential_unavailable'
      WHEN (s.schema_version IS DISTINCT FROM 1 AND s.schema_version IS DISTINCT FROM 2)
        OR s.carousel_slides NOT IN ('all','first') OR s.carousel_slides IS NULL
        OR s.eligibility IS DISTINCT FROM 'supported'
        OR s.ranking IS DISTINCT FROM 'none' OR s.destination_scope IS DISTINCT FROM 'origin_only' THEN 'unsupported_source_policy'
      WHEN i.data_generation_id IS DISTINCT FROM s.generation_id THEN 'awaiting_reobservation'
      WHEN i.exclusion_reason='origin_identity_conflict' OR m.source_projection_redacted IS TRUE THEN 'identity_conflict_or_deleted'
      WHEN i.availability='excluded' AND i.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN i.availability IS DISTINCT FROM 'available' THEN 'unavailable'
      WHEN a.exclusion_reason='origin_identity_conflict' THEN 'identity_conflict_or_deleted'
      WHEN a.availability='excluded' AND a.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN a.availability IS NOT NULL AND a.availability<>'available' THEN 'unavailable'
      WHEN m.id IS NOT NULL AND m.state<>'available' THEN 'unavailable'
      WHEN s.carousel_slides='first' AND a.position>1 THEN 'slide_not_first'
      WHEN a.validation_state='unsupported' OR a.exclusion_reason='unsupported_media' THEN 'unsupported'
      WHEN a.validation_capture->>'generation_id'=s.generation_id::text
        AND a.validation_capture->>'observation_revision'=a.observation_revision::text
        AND a.validation_capture->>'selected_asset_id'=a.asset_media_id
        AND a.validation_capture->>'state'='unsupported' THEN 'unsupported'
      WHEN a.validation_capture->>'generation_id'=s.generation_id::text
        AND a.validation_capture->>'observation_revision'=a.observation_revision::text
        AND a.validation_capture->>'selected_asset_id'=a.asset_media_id
        AND a.validation_capture->>'state'='validation_pending'
        AND a.validation_capture->>'reason' IN ('helper_unavailable','asset_unavailable')
        THEN a.validation_capture->>'reason'
      WHEN m.id IS NULL OR m.source_data_generation_id IS DISTINCT FROM a.data_generation_id THEN 'validation_pending'
      WHEN p_require_validation IS DISTINCT FROM false AND (
        a.validation_state IS DISTINCT FROM 'supported'
        OR a.validation_generation_id IS DISTINCT FROM a.data_generation_id
        OR a.validation_observation_revision IS DISTINCT FROM a.observation_revision
        OR a.validation_fingerprint IS NULL OR a.validated_at IS NULL) THEN 'validation_pending'
      WHEN EXISTS (SELECT 1 FROM instagram_intent_provenance b WHERE b.workspace_id=p_workspace_id
        AND b.intent_id=p_existing_intent_id AND (b.source_id IS DISTINCT FROM s.id
          OR b.media_item_id IS DISTINCT FROM m.id OR b.origin_ig_account_id IS DISTINCT FROM s.origin_ig_account_id
          OR b.selected_asset_identity IS DISTINCT FROM a.asset_media_id)) THEN 'selected_asset_changed'
      WHEN p_require_validation IS DISTINCT FROM false AND EXISTS (SELECT 1 FROM instagram_intent_provenance b
        WHERE b.workspace_id=p_workspace_id AND b.intent_id=p_existing_intent_id
        AND (b.capture_generation_id IS DISTINCT FROM a.data_generation_id
          OR b.capture_observation_revision IS DISTINCT FROM a.observation_revision
          OR b.validation_fingerprint IS DISTINCT FROM a.validation_fingerprint)) THEN 'past_posts_revalidation_required'
      WHEN p_evaluated_at-i.original_at < s.effective_age*interval '1 day' THEN 'too_recent'
      WHEN i.attribution='credited' AND s.include_credited IS NOT TRUE THEN 'credited_excluded'
      WHEN i.attribution='unknown' AND s.include_unknown IS NOT TRUE THEN 'attribution_unknown'
      WHEN NOT s.destination_active OR s.origin_ig_account_id<>p_destination_id OR NOT s.assigned THEN 'destination_not_allowed'
    END
  FROM source_facts s JOIN instagram_media_index i ON i.workspace_id=s.workspace_id AND i.source_id=s.id
  LEFT JOIN instagram_media_assets a ON a.workspace_id=i.workspace_id AND a.source_id=i.source_id
    AND a.parent_media_id=i.parent_media_id
  LEFT JOIN media_items m ON m.workspace_id=a.workspace_id AND m.source_id=a.source_id AND m.id=a.media_item_id
  WHERE s.provider='instagram_past_posts' AND (p_media_item_id IS NULL OR m.id=p_media_item_id)
), context AS (
  SELECT a.*,
    EXISTS (SELECT 1 FROM post_intents p WHERE p.workspace_id=p_workspace_id
      AND p.id=p_existing_intent_id AND p.media_item_id=a.media_item_id
      AND p.ig_account_id=p_destination_id AND p.origin='planned' AND NOT p.cancel_requested
      AND p.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) AS planned_warning_override,
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
  c.base_reason IS NULL AND NOT c.live_intent_conflict AND cardinality(c.blocking_locks)=0 AND (cardinality(c.warning_locks)=0 OR c.planned_warning_override),
  c.base_reason IS NULL,
  COALESCE(c.base_reason,CASE WHEN c.live_intent_conflict THEN 'live_intent'
    WHEN cardinality(c.blocking_locks)>0 OR (cardinality(c.warning_locks)>0 AND NOT c.planned_warning_override) THEN 'locked' END),
  c.data_generation_id,c.observation_revision,c.policy_revision,c.source_use_epoch,c.effective_age,
  c.live_intent_conflict,c.blocking_locks,c.warning_locks
FROM context c
$$;

CREATE OR REPLACE FUNCTION fn_selected_preparation_due(p_limit integer)
RETURNS TABLE(intent_id uuid,workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT i.id,i.workspace_id FROM post_intents i JOIN workspaces w ON w.id=i.workspace_id
 JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id
 JOIN media_sources s ON s.workspace_id=m.workspace_id AND s.id=m.source_id
 WHERE s.provider='instagram_past_posts'
 AND w.state='active' AND NOT w.is_paused AND NOT i.cancel_requested
 AND NOT EXISTS (
 SELECT 1 FROM instagram_intent_provenance p
 JOIN instagram_media_assets a ON a.workspace_id=p.workspace_id
   AND a.source_id=p.source_id AND a.media_item_id=p.media_item_id
 JOIN instagram_past_posts_generations g ON g.workspace_id=s.workspace_id
   AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
 WHERE p.workspace_id=i.workspace_id AND p.intent_id=i.id
 AND p.source_id=s.id AND p.media_item_id=m.id
 AND p.origin_ig_account_id=s.origin_ig_account_id
 AND g.lifecycle='active' AND a.data_generation_id=g.id
 AND m.source_data_generation_id=a.data_generation_id
 AND p.selected_asset_identity=a.asset_media_id
 AND p.capture_generation_id=a.data_generation_id
 AND p.capture_observation_revision=a.observation_revision
 AND p.validation_fingerprint=a.validation_fingerprint
 AND a.validation_state='supported'
 AND a.validation_generation_id=a.data_generation_id
 AND a.validation_observation_revision=a.observation_revision
 AND a.validation_fingerprint IS NOT NULL AND a.validated_at IS NOT NULL)
 AND ((i.state IN ('scheduled','prompt_pending') AND i.schedule_slot_at<=now())
 OR (i.state='awaiting_approval' AND EXISTS(SELECT 1 FROM channel_outbox o
 WHERE o.workspace_id=i.workspace_id AND o.intent_id=i.id AND o.kind='approval_prompt'
 AND o.state IN ('pending','ambiguous'))))
 ORDER BY i.schedule_slot_at,i.id LIMIT p_limit
$$;

CREATE OR REPLACE FUNCTION fn_meta_deletion_receive(p_app_identity text, p_fingerprints text[], p_key_version int,
  p_subject text, p_issued_at timestamptz, p_capability_hash text, p_skew_seconds int)
RETURNS TABLE (o_receipt_id uuid, o_replay boolean, o_state text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_receipt uuid;
  v_state text;
  v_reason text;
  v_issued timestamptz;
  v_skew interval;
  v_ws uuid;
  v_disposition text;
  m record;
BEGIN
  IF p_app_identity IS NULL OR p_app_identity !~ '^(instagram:[0-9]{1,32}|instagram:unconfigured|facebook:legacy)$'
     OR p_fingerprints IS NULL OR array_ndims(p_fingerprints) IS DISTINCT FROM 1
     OR array_lower(p_fingerprints, 1) <> 1 OR cardinality(p_fingerprints) > 16
     OR array_position(p_fingerprints, NULL) IS NOT NULL
     OR EXISTS (SELECT 1 FROM unnest(p_fingerprints) f WHERE f !~ '^[0-9a-f]{64}$')
     OR p_key_version IS NULL OR p_key_version < 1
     OR p_subject IS NULL OR p_subject !~ '^[0-9]{1,64}$'
     OR p_capability_hash IS NULL OR p_capability_hash !~ '^[0-9a-f]{64}$'
     OR p_skew_seconds IS NULL OR p_skew_seconds NOT BETWEEN 0 AND 3600 THEN
    RAISE EXCEPTION 'meta receipt refused: invalid input' USING ERRCODE = 'P0001';
  END IF;
  v_skew := make_interval(secs => p_skew_seconds);
  v_issued := CASE WHEN p_issued_at <= now() + v_skew THEN p_issued_at END;
  PERFORM pg_advisory_xact_lock(hashtextextended('meta_receipt:' || p_app_identity || ':' || p_fingerprints[1], 0));
  SELECT r.id, r.state INTO v_receipt, v_state FROM meta_deletion_receipts r
   WHERE r.app_identity = p_app_identity AND r.request_fingerprint = ANY (p_fingerprints)
   ORDER BY r.received_at, r.id LIMIT 1;
  IF FOUND THEN
    INSERT INTO meta_deletion_receipt_capabilities (capability_hash, receipt_id) VALUES (p_capability_hash, v_receipt);
    RETURN QUERY SELECT v_receipt, true, v_state;
    RETURN;
  END IF;
  INSERT INTO meta_deletion_receipts (app_identity, request_fingerprint, fingerprint_key_version, issued_at)
  VALUES (p_app_identity, p_fingerprints[1], p_key_version, v_issued)
  RETURNING id INTO v_receipt;
  FOR v_ws IN SELECT DISTINCT workspace_id FROM meta_identity_mappings
               WHERE app_identity = p_app_identity AND subject_ref = p_subject ORDER BY workspace_id LOOP
    PERFORM pg_advisory_xact_lock(hashtextextended('sources:' || v_ws, 0));
    FOR m IN SELECT * FROM meta_identity_mappings
              WHERE workspace_id = v_ws AND app_identity = p_app_identity AND subject_ref = p_subject
              ORDER BY ig_account_id, verified_at, authorization_id LOOP
      IF v_issued IS NULL THEN
        v_disposition := CASE WHEN (
            SELECT count(*) FROM meta_identity_mappings o
             WHERE o.workspace_id = v_ws AND o.ig_account_id = m.ig_account_id
               AND o.app_identity = p_app_identity AND o.subject_ref = p_subject) > 1
          OR EXISTS (
            SELECT 1 FROM meta_deletion_receipt_scopes ps
              JOIN meta_identity_mappings pm ON pm.workspace_id = ps.workspace_id
               AND pm.credential_id = ps.credential_id AND pm.authorization_id = ps.authorization_id
             WHERE ps.workspace_id = v_ws AND ps.ig_account_id = m.ig_account_id
               AND ps.receipt_id <> v_receipt
               AND pm.app_identity = p_app_identity AND pm.subject_ref = p_subject)
          THEN 'held' ELSE 'in_scope' END;
      ELSIF COALESCE(m.authorized_at, m.verified_at) <= v_issued - v_skew THEN
        v_disposition := 'in_scope';
      ELSIF m.authorized_at >= v_issued + v_skew THEN
        v_disposition := 'excluded';
      ELSE
        v_disposition := 'held';
      END IF;
      CONTINUE WHEN v_disposition = 'excluded';
      INSERT INTO meta_deletion_receipt_scopes (receipt_id, workspace_id, ig_account_id, credential_id,
        authorization_id, disposition, state)
      VALUES (v_receipt, v_ws, m.ig_account_id, m.credential_id, m.authorization_id, v_disposition,
        CASE WHEN v_disposition = 'held' THEN 'held' ELSE 'pending' END);
      INSERT INTO meta_deletion_receipt_generations (receipt_id, workspace_id, credential_id,
        authorization_id, source_id, generation_id)
      SELECT v_receipt, v_ws, m.credential_id, m.authorization_id, g.source_id, g.id
        FROM instagram_past_posts_generations g
        JOIN media_sources s ON s.workspace_id = g.workspace_id AND s.id = g.source_id
       WHERE g.workspace_id = v_ws AND g.authorization_id = m.authorization_id
         AND s.provider = 'instagram_past_posts' AND s.origin_ig_account_id = m.ig_account_id
         AND g.lifecycle <> 'deleted';
    END LOOP;
  END LOOP;
  v_reason := CASE
    WHEN p_app_identity !~ '^instagram:[0-9]{1,32}$' THEN 'app_unrecognized'
    WHEN EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes
                  WHERE receipt_id = v_receipt AND disposition = 'held') THEN 'ordering_ambiguous'
  END;
  v_state := CASE
    WHEN v_reason IS NOT NULL THEN 'needs_review'
    WHEN NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes WHERE receipt_id = v_receipt) THEN 'completed'
    ELSE 'pending'
  END;
  UPDATE meta_deletion_receipts
     SET state = v_state, review_reason = v_reason,
         completed_at = CASE WHEN v_state = 'completed' THEN now() END
   WHERE id = v_receipt;
  INSERT INTO meta_deletion_receipt_capabilities (capability_hash, receipt_id) VALUES (p_capability_hash, v_receipt);
  RETURN QUERY SELECT v_receipt, false, v_state;
END $$;

CREATE OR REPLACE FUNCTION fn_meta_receipt_progress(p_receipt uuid, p_credential uuid, p_authorization uuid,
  p_state text, p_results jsonb, p_review_reason text, p_retry_after_seconds int)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_ws uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
  v_from text;
  v_state text;
  v_reason text;
BEGIN
  IF p_state IS NULL OR p_state NOT IN ('cleaning','completed')
     OR p_results IS NULL OR jsonb_typeof(p_results) <> 'object' OR octet_length(p_results::text) > 4096
     OR (p_review_reason IS NOT NULL AND (p_review_reason <> 'transit_unresolved' OR p_state = 'completed')) THEN
    RAISE EXCEPTION 'meta receipt progress refused: invalid input' USING ERRCODE = 'P0001';
  END IF;
  SELECT s.state INTO v_from FROM meta_deletion_receipt_scopes s
   WHERE s.receipt_id = p_receipt AND s.workspace_id = v_ws
     AND s.credential_id = p_credential AND s.authorization_id = p_authorization
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'meta receipt progress refused: scope_unavailable' USING ERRCODE = 'P0001';
  END IF;
  IF v_from NOT IN ('pending','cleaning') THEN
    RAISE EXCEPTION 'meta receipt progress refused: scope_%', v_from USING ERRCODE = 'P0001';
  END IF;
  IF p_state = 'completed' THEN
    IF EXISTS (SELECT 1 FROM meta_deletion_receipt_generations x
                 LEFT JOIN instagram_past_posts_generations g ON g.workspace_id = x.workspace_id
                  AND g.source_id = x.source_id AND g.id = x.generation_id
                WHERE x.receipt_id = p_receipt AND x.workspace_id = v_ws
                  AND x.credential_id = p_credential AND x.authorization_id = p_authorization
                  AND g.lifecycle IS DISTINCT FROM 'deleted') THEN
      RAISE EXCEPTION 'meta receipt progress refused: generations_not_deleted' USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM instagram_artifacts a
                WHERE a.workspace_id = v_ws AND a.kind = 'transit'
                  AND a.data_generation_id IN (SELECT x.generation_id FROM meta_deletion_receipt_generations x
                        WHERE x.receipt_id = p_receipt AND x.workspace_id = v_ws
                          AND x.credential_id = p_credential AND x.authorization_id = p_authorization)
                  AND NOT (a.outcome = 'settled' AND a.cleanup_state = 'destroyed')) THEN
      RAISE EXCEPTION 'meta receipt progress refused: transit_unresolved' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  UPDATE meta_deletion_receipt_scopes
     SET state = p_state, results = p_results,
         next_attempt_at = now() + make_interval(secs => LEAST(GREATEST(COALESCE(p_retry_after_seconds, 60), 60), 3600))
   WHERE receipt_id = p_receipt AND workspace_id = v_ws
     AND credential_id = p_credential AND authorization_id = p_authorization;
  SELECT r.state, r.review_reason INTO v_state, v_reason FROM meta_deletion_receipts r WHERE r.id = p_receipt FOR UPDATE;
  IF p_review_reason IS NOT NULL AND v_state <> 'needs_review' THEN
    UPDATE meta_deletion_receipts SET state = 'needs_review', review_reason = p_review_reason WHERE id = p_receipt;
    v_state := 'needs_review';
  ELSIF (v_state = 'pending' OR (v_state = 'needs_review' AND v_reason = 'transit_unresolved'))
        AND NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes s
         WHERE s.receipt_id = p_receipt AND s.state NOT IN ('completed','workspace_purged')) THEN
    UPDATE meta_deletion_receipts SET state = 'completed', review_reason = NULL, completed_at = now() WHERE id = p_receipt;
    v_state := 'completed';
  END IF;
  RETURN v_state;
END $$;

CREATE OR REPLACE FUNCTION fn_past_posts_redact_generation_records(p_source uuid, p_generation_ids uuid[])
RETURNS TABLE (o_operation_rows int, o_intent_rows int, o_artifact_rows int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_ws uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
  v_operations int;
  v_intents int;
  v_artifacts int;
BEGIN
  IF v_ws IS NULL OR p_source IS NULL OR p_generation_ids IS NULL OR cardinality(p_generation_ids) = 0
     OR array_position(p_generation_ids, NULL) IS NOT NULL
     OR (SELECT count(*) FROM instagram_past_posts_generations g
          WHERE g.workspace_id = v_ws AND g.source_id = p_source AND g.id = ANY (p_generation_ids)
            AND g.lifecycle IN ('deletion_pending','deleted'))
        <> (SELECT count(DISTINCT x) FROM unnest(p_generation_ids) x) THEN
    RAISE EXCEPTION 'past-posts redaction refused: scope_unavailable' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_operation_provenance p
               JOIN provider_operations o ON o.workspace_id = p.workspace_id AND o.id = p.operation_id
              WHERE p.workspace_id = v_ws AND p.data_generation_id = ANY (p_generation_ids)
                AND o.state IN ('permitted','ambiguous')) THEN
    RAISE EXCEPTION 'past-posts redaction refused: operation_unresolved' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_artifacts a
              WHERE a.workspace_id = v_ws AND a.data_generation_id = ANY (p_generation_ids) AND a.kind = 'transit'
                AND NOT (a.outcome = 'settled' AND a.cleanup_state = 'destroyed')) THEN
    RAISE EXCEPTION 'past-posts redaction refused: transit_unresolved' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_intent_provenance p
               JOIN post_intents i ON i.workspace_id = p.workspace_id AND i.id = p.intent_id
              WHERE p.workspace_id = v_ws
                AND (p.capture_generation_id = ANY (p_generation_ids) OR p.staged_generation_id = ANY (p_generation_ids))
                AND i.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) THEN
    RAISE EXCEPTION 'past-posts redaction refused: intent_live' USING ERRCODE = 'P0001';
  END IF;
  DELETE FROM instagram_operation_provenance
   WHERE workspace_id = v_ws AND data_generation_id = ANY (p_generation_ids);
  GET DIAGNOSTICS v_operations = ROW_COUNT;
  DELETE FROM instagram_intent_provenance
   WHERE workspace_id = v_ws
     AND (capture_generation_id = ANY (p_generation_ids) OR staged_generation_id = ANY (p_generation_ids));
  GET DIAGNOSTICS v_intents = ROW_COUNT;
  DELETE FROM instagram_artifacts
   WHERE workspace_id = v_ws AND data_generation_id = ANY (p_generation_ids);
  GET DIAGNOSTICS v_artifacts = ROW_COUNT;
  RETURN QUERY SELECT v_operations, v_intents, v_artifacts;
END $$;

CREATE OR REPLACE FUNCTION fn_meta_receipt_cleanup_jobs(p_limit int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE minted int;
BEGIN
 UPDATE meta_deletion_receipt_scopes s SET state = 'workspace_purged'
  WHERE s.state IN ('pending','cleaning','held')
    AND NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id);
 UPDATE meta_deletion_receipts r
    SET state = 'completed', review_reason = NULL, completed_at = now()
  WHERE (r.state = 'pending' AND r.review_reason IS NULL
         OR r.state = 'needs_review' AND r.review_reason = 'transit_unresolved')
    AND NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes s
                     WHERE s.receipt_id = r.id AND s.state NOT IN ('completed','workspace_purged'));
 WITH due AS (
  SELECT s.receipt_id, s.workspace_id FROM meta_deletion_receipt_scopes s
   WHERE s.next_attempt_at <= now()
     AND (s.state IN ('pending','cleaning')
      OR (s.state = 'held' AND EXISTS (SELECT 1 FROM meta_deletion_receipt_generations x
           JOIN instagram_past_posts_generations g ON g.workspace_id = x.workspace_id
            AND g.source_id = x.source_id AND g.id = x.generation_id
          WHERE x.receipt_id = s.receipt_id AND x.workspace_id = s.workspace_id
            AND x.credential_id = s.credential_id AND x.authorization_id = s.authorization_id
            AND g.lifecycle IN ('active','superseded'))))
     AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id)
     AND NOT EXISTS (SELECT 1 FROM jobs j
                      WHERE j.serialization_key = 'receipt:' || s.receipt_id || ':' || s.workspace_id
                        AND j.state IN ('ready','leased'))
   GROUP BY s.receipt_id, s.workspace_id
   ORDER BY min(s.next_attempt_at), s.receipt_id, s.workspace_id
   LIMIT LEAST(GREATEST(p_limit, 0), 25)
 )
 INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
 SELECT 'meta_deletion_cleanup', workspace_id, 'bulk', 'receipt:' || receipt_id || ':' || workspace_id, now(), 5,
  now() + interval '6 hours', jsonb_build_object('v', 1, 'receipt_id', receipt_id)
 FROM due;
 GET DIAGNOSTICS minted = ROW_COUNT;
 RETURN minted;
END $$;

CREATE OR REPLACE FUNCTION fn_past_posts_reconcile_jobs(p_limit int, p_run_id text, p_run_digest text)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE minted int;
BEGIN
 IF p_run_id IS NULL OR p_run_id !~ '^[a-zA-Z0-9_.-]{1,128}$'
  OR p_run_digest IS NULL OR p_run_digest !~ '^[0-9a-f]{64}$' THEN RETURN 0; END IF;
 WITH pending AS (
  SELECT w.id FROM workspaces w
  WHERE w.state = 'active'
   AND w.source_mix_converted_at IS NOT NULL AND w.source_mix_revision IS NOT NULL
   AND w.source_mix_mode IN ('equal','custom') AND w.source_membership_policy = 'reset_equal'
   AND EXISTS (SELECT 1 FROM ig_accounts a
     JOIN oauth_credentials c ON c.workspace_id = a.workspace_id AND c.ig_account_id = a.id
    WHERE a.workspace_id = w.id AND a.state = 'active' AND a.provider_account_ref NOT LIKE 'manual:%'
     AND c.provider = 'ig_login' AND c.state = 'active'
     AND (NOT EXISTS (SELECT 1 FROM meta_identity_mappings m
           WHERE m.workspace_id = c.workspace_id AND m.credential_id = c.id AND m.is_current)
      OR NOT EXISTS (SELECT 1 FROM media_sources s
           WHERE s.workspace_id = a.workspace_id AND s.provider = 'instagram_past_posts'
            AND s.origin_ig_account_id = a.id)))
   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.workspace_id = w.id
    AND j.kind = 'reconcile_past_posts_accounts' AND j.payload->>'run_digest' = p_run_digest)
  ORDER BY w.id LIMIT LEAST(GREATEST(p_limit, 0), 5)
 )
 INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
 SELECT 'reconcile_past_posts_accounts', id, 'bulk', 'past-posts-reconcile:' || id, now(), 5, now() + interval '6 hours',
  jsonb_build_object('v', 1, 'run_id', p_run_id, 'run_digest', p_run_digest)
 FROM pending;
 GET DIAGNOSTICS minted = ROW_COUNT;
 RETURN minted;
END $$;

-- The door roles own their functions again, with the grants schema code gives them.
GRANT CREATE ON SCHEMA public TO svc_claim, svc_clock, svc_maintenance;
ALTER FUNCTION fn_claim_job(text, text, interval, integer, boolean) OWNER TO svc_claim;
ALTER FUNCTION fn_clock_tick(integer, interval, jsonb, boolean) OWNER TO svc_clock;
ALTER FUNCTION fn_reaper_sweep(integer, interval, interval, boolean) OWNER TO svc_maintenance;
REVOKE CREATE ON SCHEMA public FROM svc_claim, svc_clock, svc_maintenance;
REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid) TO svc_ingress, svc_worker;
REVOKE ALL ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_asset_suitability(uuid, uuid, integer, timestamp with time zone, boolean, uuid, uuid, uuid, boolean) TO svc_ingress, svc_worker;
REVOKE ALL ON FUNCTION fn_claim_job(text, text, interval, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_claim_job(text, text, interval, integer, boolean) TO svc_worker;
REVOKE ALL ON FUNCTION fn_clock_tick(integer, interval, jsonb, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_clock_tick(integer, interval, jsonb, boolean) TO svc_worker;
REVOKE ALL ON FUNCTION fn_reaper_sweep(integer, interval, interval, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reaper_sweep(integer, interval, interval, boolean) TO svc_worker;
