-- A past-posts story's file is fetched by a prepare_past_post job on a media
-- lane, so jobs takes the kind and the lane: ck_jobs_kind and ck_jobs_lane are
-- rebuilt with the lists the model declares (machinery.py), in their order.
-- The reaper door takes two more arguments, each with a default:
-- p_past_posts_park_late, false, and p_late, the caller's late window, none.
-- Its cadence leg expires a cadence story once it is still waiting p_late after
-- its slot, and with no window at its slot, as before. A caller that passes
-- p_past_posts_park_late true parks a late past-posts cadence story of an
-- active, unpaused workspace itself, so the cadence leg leaves that story to
-- it. Any other call, a four-argument one included, expires as before. The
-- four-argument door is dropped and the six-argument one made in its place,
-- and the three-argument door's call reaches it unchanged.
-- fn_reaper_late_past_posts lists the stories such a caller parks, and the
-- leg and that door read which stories those are from one rule,
-- fn_reaper_parks_late. The functions are owned and granted as the schema
-- code has them. The preparation door leaves out a story whose
-- prepare_past_post job is ready or leased.
-- The runner wraps this file and its ledger row in one transaction.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_jobs_kind' AND conrelid = to_regclass('public.jobs') AND pg_get_constraintdef(oid) LIKE '%prepare_past_post%')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_jobs_lane' AND conrelid = to_regclass('public.jobs') AND pg_get_constraintdef(oid) LIKE '%media%')
-- runner:postcondition SELECT to_regprocedure('public.fn_reaper_sweep(integer,interval,interval,boolean,boolean,interval)') IS NOT NULL AND to_regprocedure('public.fn_reaper_sweep(integer,interval,interval,boolean)') IS NULL
-- runner:postcondition SELECT to_regprocedure('public.fn_reaper_late_past_posts(interval,integer)') IS NOT NULL
-- runner:postcondition SELECT to_regprocedure('public.fn_reaper_parks_late(uuid,uuid)') IS NOT NULL
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc WHERE oid = to_regprocedure('public.fn_selected_preparation_due(integer)') AND prosrc LIKE '%prepare_past_post%')

SET LOCAL search_path = public, pg_temp;
SET LOCAL check_function_bodies = off;

-- The job kind and lane
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind, DROP CONSTRAINT ck_jobs_lane;
ALTER TABLE jobs
  ADD CONSTRAINT ck_jobs_kind CHECK (
    kind IN ('plan_slot','publish_pipeline','deliver_outbox',
             'sync_media_source','first_ingest_chunk','refresh_credential',
             'offboard_workspace','revoke_workspace_credentials','reauth_prompt',
             'reconcile_ambiguous','reap_expired','reap_transit_assets',
             'retention_sweep','reencrypt_credentials','send_email',
             'alert_stranded_sources','activation_nudge_sweep','convert_source_mix',
             'past_posts_lifecycle_sweep','meta_deletion_cleanup',
             'reconcile_past_posts_accounts','prepare_past_post')
  ),
  ADD CONSTRAINT ck_jobs_lane CHECK (lane IN ('interactive','bulk','media'));

-- The reaper door changes signature, so it is dropped and made again. Its new
-- arguments have defaults, so a four-argument call still reaches it.
DROP FUNCTION fn_reaper_sweep(integer, interval, interval, boolean);

-- The rule the cadence leg and the late door share
CREATE FUNCTION fn_reaper_parks_late(p_ws uuid, p_item uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM media_items m
                   JOIN media_sources s ON s.workspace_id = m.workspace_id AND s.id = m.source_id
                   JOIN workspaces w ON w.id = m.workspace_id
                  WHERE m.workspace_id = p_ws AND m.id = p_item
                    AND s.provider = 'instagram_past_posts'
                    AND w.state = 'active' AND NOT w.is_paused)
$$;

CREATE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval,p_past_posts_enabled boolean,p_past_posts_park_late boolean DEFAULT false,p_late interval DEFAULT NULL)
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
   UNION (SELECT i.workspace_id FROM post_intents i WHERE i.state IN ('scheduled','prompt_pending')
    AND i.schedule_slot_at<now()-COALESCE(p_late,interval '0') AND i.origin='cadence'
    AND NOT (p_past_posts_park_late AND fn_reaper_parks_late(i.workspace_id,i.media_item_id))
    ORDER BY i.id LIMIT GREATEST(p_lim,0))
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
   WHERE id IN (SELECT i.id FROM post_intents i
                 WHERE i.workspace_id=ANY(locked_workspaces) AND i.state IN ('scheduled','prompt_pending') AND i.schedule_slot_at < now() - COALESCE(p_late, interval '0')
                   AND i.origin = 'cadence'
                   AND NOT (p_past_posts_park_late
                            AND fn_reaper_parks_late(i.workspace_id, i.media_item_id))
                 ORDER BY i.id LIMIT rem);
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

-- The door that lists the late past-posts stories such a caller parks
CREATE FUNCTION fn_reaper_late_past_posts(p_late interval, p_lim int)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid, o_tries int)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.id, i.workspace_id,
         COALESCE((SELECT j.attempts FROM jobs j
                    WHERE j.workspace_id = i.workspace_id AND j.kind = 'prepare_past_post'
                      AND j.serialization_key = 'prep:' || i.id
                    ORDER BY j.created_at DESC, j.id DESC LIMIT 1), 0)
    FROM post_intents i
   WHERE i.state IN ('scheduled', 'prompt_pending') AND i.origin = 'cadence'
     AND NOT i.cancel_requested AND i.schedule_slot_at < now() - p_late
     AND fn_reaper_parks_late(i.workspace_id, i.media_item_id)
     AND NOT EXISTS (SELECT 1 FROM jobs j
                      WHERE j.workspace_id = i.workspace_id AND j.kind = 'prepare_past_post'
                        AND j.serialization_key = 'prep:' || i.id AND j.state = 'leased')
   ORDER BY i.schedule_slot_at, i.id
   LIMIT GREATEST(p_lim, 0)
$$;

-- The preparation door
CREATE OR REPLACE FUNCTION fn_selected_preparation_due(p_limit integer)
RETURNS TABLE(intent_id uuid,workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT i.id,i.workspace_id FROM post_intents i JOIN workspaces w ON w.id=i.workspace_id
 JOIN media_items m ON m.workspace_id=i.workspace_id AND m.id=i.media_item_id
 JOIN media_sources s ON s.workspace_id=m.workspace_id AND s.id=m.source_id
 WHERE s.provider='instagram_past_posts'
 AND w.state='active' AND NOT w.is_paused AND NOT i.cancel_requested
 AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.workspace_id=i.workspace_id AND j.kind='prepare_past_post'
 AND j.serialization_key='prep:'||i.id AND j.state IN ('ready','leased'))
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

-- The two doors are owned, and all three functions granted, as the schema code
-- has them.
GRANT CREATE ON SCHEMA public TO svc_maintenance;
ALTER FUNCTION fn_reaper_sweep(integer, interval, interval, boolean, boolean, interval) OWNER TO svc_maintenance;
ALTER FUNCTION fn_reaper_late_past_posts(interval, integer) OWNER TO svc_maintenance;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
REVOKE ALL ON FUNCTION fn_reaper_sweep(integer, interval, interval, boolean, boolean, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reaper_sweep(integer, interval, interval, boolean, boolean, interval) TO svc_worker;
REVOKE ALL ON FUNCTION fn_reaper_parks_late(uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reaper_parks_late(uuid, uuid) TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_reaper_late_past_posts(interval, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reaper_late_past_posts(interval, integer) TO svc_worker;
