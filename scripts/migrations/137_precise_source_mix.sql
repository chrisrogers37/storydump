-- Precise allocation and a bounded tenant conversion job behind verified drain evidence.
-- runner:postcondition SELECT a.atttypmod = -1 FROM pg_attribute a WHERE a.attrelid='public.category_post_case_mix'::regclass AND a.attname='ratio'
-- runner:postcondition SELECT to_regprocedure('public.fn_mix_conversion_jobs(integer,text,text)') IS NOT NULL
ALTER TABLE category_post_case_mix ALTER COLUMN ratio TYPE NUMERIC;

-- One named tenant job. Source/mix authority remains the existing converter.
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
 'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
 'first_ingest_chunk','refresh_credential','offboard_workspace',
 'revoke_workspace_credentials','reauth_prompt','reconcile_ambiguous',
 'reap_expired','reap_transit_assets','retention_sweep','reencrypt_credentials',
 'send_email','alert_stranded_sources','activation_nudge_sweep','convert_source_mix'));

GRANT CREATE ON SCHEMA public TO svc_clock;
CREATE FUNCTION fn_mix_conversion_jobs(p_limit int,p_run_id text,p_run_digest text)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE minted int;
BEGIN
 IF p_run_id IS NULL OR p_run_id !~ '^[a-zA-Z0-9_.-]{1,128}$'
  OR p_run_digest IS NULL OR p_run_digest !~ '^[0-9a-f]{64}$' THEN RETURN 0; END IF;
 -- Discovery is a hint only; the tenant executor rereads after canonical locks.
 -- Terminal failure in this run stays visible rather than being reminted forever.
 WITH pending AS (
  SELECT w.id FROM workspaces w
  WHERE w.source_mix_mode IS NULL AND w.source_mix_policy_version IS NULL
   AND w.source_membership_policy IS NULL AND w.source_mix_revision IS NULL
   AND w.source_mix_conversion_version IS NULL AND w.source_mix_converted_at IS NULL
   AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.workspace_id=w.id
    AND j.kind='convert_source_mix' AND j.payload->>'run_digest'=p_run_digest)
  ORDER BY w.id LIMIT LEAST(GREATEST(p_limit,0),1)
 )
 INSERT INTO jobs(kind,workspace_id,lane,serialization_key,run_at,max_attempts,deadline_at,payload)
 SELECT 'convert_source_mix',id,'bulk','mix:'||id||':convert',now(),5,now()+interval '6 hours',
  jsonb_build_object('v',1,'run_id',p_run_id,'run_digest',p_run_digest,'protocol_version',2)
 FROM pending;
 GET DIAGNOSTICS minted=ROW_COUNT;
 RETURN minted;
END $$;
ALTER FUNCTION fn_mix_conversion_jobs(int,text,text) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_mix_conversion_jobs(int,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_mix_conversion_jobs(int,text,text) TO svc_worker;
REVOKE CREATE ON SCHEMA public FROM svc_clock;

CREATE OR REPLACE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int,p_archive_enabled boolean)
RETURNS SETOF jobs LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE candidate record; selected jobs; refusal text; source_prefix_applied boolean;
BEGIN
 -- Discovery owns no job lock. Source-read kinds take the canonical prefix before
 -- the LAST-class lease. Other kinds retain the raw unique-index race mechanism.
 -- Publish/effect consumers adopt their own prefix only in phase 3.
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
    RAISE SQLSTATE 'P0002'; -- changed identity: release the job row; discover afresh next turn
   END IF;
   refusal:=NULL;
   IF selected.kind IN ('sync_media_source','first_ingest_chunk') THEN
    refusal:=fn_source_job_read_reason(selected.workspace_id,selected.payload,p_archive_enabled);
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
ALTER FUNCTION fn_claim_job(text,text,interval,int,boolean) OWNER TO svc_claim;
REVOKE ALL ON FUNCTION fn_claim_job(text,text,interval,int,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_claim_job(text,text,interval,int,boolean) TO svc_worker;
CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval,p_archive_enabled boolean)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE locked_workspaces uuid[]:='{}'; lock_ws uuid; expired_job record; refusal text; n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  -- Lock all bounded candidate workspaces before any intent/item/lease rows.
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
    refusal:=fn_source_job_read_reason(expired_job.workspace_id,expired_job.payload,p_archive_enabled);
   END IF;
   UPDATE jobs SET state=CASE WHEN refusal IS NULL THEN 'ready' ELSE 'failed' END,
    locked_by=NULL,lease_token=NULL,locked_until=NULL,
    payload=CASE WHEN refusal IS NULL THEN payload ELSE payload||jsonb_build_object('source_read_refusal',refusal) END
   WHERE id=expired_job.id AND state='leased' AND locked_until<now();
   IF FOUND THEN c:=c+1; END IF;
  END LOOP;
  n:=n+c;rem:=rem-c;
  -- 087: a cancel the user asked for ends `cancelled` (#1235), ahead of the
  -- expiry legs; a debited row is left to the paths that refund it first.
  UPDATE post_intents SET state = 'cancelled'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE workspace_id=ANY(locked_workspaces) AND cancel_requested AND cap_consumed_on IS NULL
                   AND state IN ('scheduled','prompt_pending','awaiting_approval','approved')
                 ORDER BY id LIMIT rem)
     AND state IN ('scheduled','prompt_pending','awaiting_approval','approved');  -- rechecked on a changed row
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 089: the slot expiry takes cadence rows only (#1413). A planned row
  -- that is due and unserved ends through the miss door
  -- (fn_planned_misses), which tells the bound chats why; ended here, it
  -- would end in silence.
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
  -- 086: a `ready` job past its deadline ends `failed`, and says so (#1429),
  -- for the kinds a sweep re-mints; a sync kind's source is re-armed, as
  -- `work_loop._rearm_source` does on a spent budget. Last, so a backlog of
  -- expired jobs cannot take the budget of the legs above.
  WITH ended AS (
    UPDATE jobs SET state = 'failed',
                    payload = payload || jsonb_build_object('ended', 'deadline')
     WHERE id IN (SELECT id FROM jobs
                   WHERE (workspace_id IS NULL OR workspace_id=ANY(locked_workspaces)) AND state = 'ready' AND deadline_at <= now()
                     AND kind IN ('reap_expired', 'reconcile_ambiguous',
                                  'alert_stranded_sources', 'reap_transit_assets',
                                  'plan_slot', 'refresh_credential', 'reauth_prompt',
                                  'deliver_outbox', 'sync_media_source',
                                  'first_ingest_chunk','convert_source_mix')   -- the kinds a sweep re-mints
                   ORDER BY id LIMIT rem)
       AND state = 'ready' AND deadline_at <= now()   -- rechecked on a changed row
    RETURNING kind, workspace_id, payload, payload->>'source_id' AS source_id
  ), rearmed AS (
    UPDATE media_sources s
       SET next_sync_at = now() + interval '24 hours'           -- work_loop.REARM_AFTER_SECONDS
      FROM ended e
     WHERE e.kind IN ('sync_media_source', 'first_ingest_chunk')  -- work_loop._SYNC_KINDS
       AND s.id::text = e.source_id   -- as text: a malformed payload must not abort the sweep
       AND s.workspace_id = e.workspace_id
       AND s.state = 'active' AND s.next_sync_at IS NULL
       AND fn_source_job_read_reason(e.workspace_id,e.payload,p_archive_enabled) IS NULL
       AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) a WHERE a.admitted)
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;


ALTER FUNCTION fn_reaper_sweep(int,interval,interval,boolean) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_reaper_sweep(int,interval,interval,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_reaper_sweep(int,interval,interval,boolean) TO svc_worker;
