-- Current source scanning authority and canonical source-before-lease job doors.
-- No production archive capability is enabled by this migration.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc WHERE oid='fn_source_read_authority(uuid,uuid,boolean)'::regprocedure AND NOT prosecdef)
CREATE FUNCTION fn_source_read_authority(p_workspace_id uuid,p_source_id uuid,p_activated boolean)
RETURNS TABLE(source_id uuid,provider text,admitted boolean,reason text,
 source_use_epoch bigint,generation_id uuid,credential_id uuid,account_ref text)
LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
WITH facts AS (
 SELECT s.id,s.provider,s.source_use_epoch,g.id AS generation_id,c.id AS credential_id,
 a.provider_account_ref AS account_ref,
 CASE
 WHEN s.provider='gdrive' THEN NULL
 WHEN s.provider<>'instagram_archive' THEN 'unsupported_source_provider'
 WHEN p_activated IS NOT TRUE OR w.source_mix_policy_version IS DISTINCT FROM 1
  OR w.source_mix_conversion_version IS DISTINCT FROM 1 OR w.source_mix_revision IS NULL
  OR w.source_mix_converted_at IS NULL OR (w.source_mix_mode IN ('equal','custom')) IS NOT TRUE
  OR w.source_membership_policy IS DISTINCT FROM 'reset_equal' THEN 'archive_not_activated'
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
 LEFT JOIN instagram_archive_generations g ON g.workspace_id=s.workspace_id AND g.source_id=s.id AND g.id=s.current_authorization_generation_id
 LEFT JOIN oauth_credentials c ON c.workspace_id=g.workspace_id AND c.ig_account_id=g.origin_ig_account_id AND c.id=g.credential_id
 WHERE s.workspace_id=p_workspace_id AND s.id=p_source_id
)
SELECT id,provider,reason IS NULL,reason,source_use_epoch,generation_id,credential_id,account_ref FROM facts
$$;
REVOKE ALL ON FUNCTION fn_source_read_authority(uuid,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_read_authority(uuid,uuid,boolean) TO svc_ingress,svc_worker,svc_claim,svc_clock,svc_maintenance;
COMMENT ON FUNCTION fn_source_read_authority(uuid,uuid,boolean) IS 'Internal current source-read facts only; no item, age, attribution, credit or destination-pool prerequisite. Invoker RLS and explicit tenant predicates remain authoritative. Expiry uses fresh database wall time.';

GRANT SELECT (id,source_mix_policy_version,source_mix_conversion_version,source_mix_revision,source_mix_converted_at,source_mix_mode,source_membership_policy) ON workspaces TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON workspaces FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON workspaces TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON workspaces FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (id,workspace_id,state,provider_account_ref) ON ig_accounts TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON ig_accounts FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON ig_accounts TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON ig_accounts FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (id,workspace_id,provider,state,config,origin_ig_account_id,current_authorization_generation_id,source_use_epoch,next_sync_at) ON media_sources TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON media_sources FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON media_sources TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON media_sources FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (id,workspace_id,ig_account_id,provider,state,expires_at) ON oauth_credentials TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON oauth_credentials FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON oauth_credentials TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON oauth_credentials FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (id,workspace_id,source_id,origin_ig_account_id,credential_id,lifecycle) ON instagram_archive_generations TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON instagram_archive_generations FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);
GRANT UPDATE (id) ON instagram_archive_generations TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read_lock ON instagram_archive_generations FOR UPDATE TO svc_claim,svc_clock,svc_maintenance USING (true) WITH CHECK (true);

GRANT SELECT (workspace_id,source_id,ratio,effective_to) ON category_post_case_mix TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON category_post_case_mix FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

GRANT SELECT (workspace_id,source_id,ig_account_id,state) ON media_source_destinations TO svc_claim,svc_clock,svc_maintenance;
CREATE POLICY p_source_read ON media_source_destinations FOR SELECT TO svc_claim,svc_clock,svc_maintenance USING (true);

CREATE FUNCTION fn_lock_source_read_prefix(p_ws uuid)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('sources:'||p_ws,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('case_mix:'||p_ws,0));
 PERFORM id FROM workspaces WHERE id=p_ws FOR UPDATE;
 PERFORM id FROM ig_accounts WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM media_sources WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM oauth_credentials WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
 PERFORM id FROM instagram_archive_generations WHERE workspace_id=p_ws ORDER BY id FOR UPDATE;
END $$;
REVOKE ALL ON FUNCTION fn_lock_source_read_prefix(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_lock_source_read_prefix(uuid) TO svc_claim,svc_clock,svc_maintenance;

CREATE FUNCTION fn_source_read_capture(p_ws uuid,p_sid uuid,p_activated boolean)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('v',1,'workspace_id',p_ws,'source_id',source_id,'provider',provider,
 'source_use_epoch',source_use_epoch,'generation_id',generation_id,'credential_id',credential_id,'account_ref',account_ref)
 FROM fn_source_read_authority(p_ws,p_sid,p_activated) WHERE admitted
$$;
REVOKE ALL ON FUNCTION fn_source_read_capture(uuid,uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_read_capture(uuid,uuid,boolean) TO svc_claim,svc_clock,svc_maintenance;

CREATE FUNCTION fn_source_job_read_reason(p_ws uuid,p_payload jsonb,p_activated boolean)
RETURNS text LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE a record; capture jsonb:=p_payload->'source_read_capture';
BEGIN
 SELECT r.* INTO a FROM media_sources s CROSS JOIN LATERAL fn_source_read_authority(p_ws,s.id,p_activated) r
 WHERE s.workspace_id=p_ws AND s.id::text=p_payload->>'source_id';
 IF NOT FOUND OR a.provider<>'instagram_archive' THEN RETURN NULL; END IF;
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
REVOKE ALL ON FUNCTION fn_source_job_read_reason(uuid,jsonb,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_source_job_read_reason(uuid,jsonb,boolean) TO svc_claim,svc_clock,svc_maintenance;

GRANT CREATE ON SCHEMA public TO svc_claim,svc_clock,svc_maintenance;
CREATE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int,p_archive_enabled boolean)
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
   IF candidate.workspace_id IS NOT NULL AND candidate.kind IN ('sync_media_source','first_ingest_chunk') THEN
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
   IF selected.kind IN ('sync_media_source','first_ingest_chunk') AND
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
CREATE OR REPLACE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int)
RETURNS SETOF jobs LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM fn_claim_job(p_lane,p_worker,p_lease,p_ws_lane_cap,false)
$$;

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb, p_archive_enabled boolean)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, archive_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE locked_workspaces uuid[]:='{}'; source_due record; source_capture jsonb; lock_ws uuid; k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;   -- 083: the singleton's run_at, named so its
                              -- deadline can be measured FROM it
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- Lock a bounded discovery set before the recurring job/account/credential legs.
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
    AND s.next_sync_at<=now() AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
    ORDER BY s.next_sync_at,s.id LIMIT GREATEST(p_max,0))
  ) candidate ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    -- 084: this kind's own expired lease goes back to `ready` BEFORE the guard
    -- reads it (#1329). The reaper re-readies every other kind's dead lease,
    -- but it is one of these singletons and cannot revive itself; left
    -- `leased`, a dead lease reads as live to the guard below for ever. The
    -- four assignments are the reaper's first leg's (076), scoped to the kind:
    UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
     WHERE kind = k AND workspace_id IS NULL
       AND state = 'leased' AND locked_until < now();
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      -- 083: from run_at, NOT from now(). A singleton's run_at is its last
      -- completion plus its cadence, which for a slow cadence is well ahead of
      -- this tick; a deadline anchored at mint would be spent before the job
      -- was ever claimable and would end it on its first run.
      due_at := GREATEST(now(), COALESCE(last_done + cadence, now()));
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              due_at, 3,
              due_at + interval '6 hours',   -- LANE_BUDGETS['bulk'][1]
              jsonb_build_object('v', 1));
      n4 := n4 + 1;
    END IF;
  END LOOP;
  rem := GREATEST(p_max - n4, 0);            -- the running remainder every later leg draws on;
                                             -- each leg's LIMIT keeps its count ≤ rem, so the
                                             -- plain subtractions below cannot go negative
  -- (2) due accounts → plan_slot jobs + slot-cursor advance, one set-based statement
  -- (the O(due) scan, H3; ix_ig_accounts_due serves it):
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
  -- (3) due credential refreshes — one set-based statement (D31: the scheduled refresh is also
  -- the liveness probe; the cadence is decoupled from expiry proximity). Reads ride svc_clock's
  -- payload-free column grant; ix_credentials_refresh_due serves the scan:
  WITH due AS (
    SELECT id, workspace_id FROM oauth_credentials
     WHERE workspace_id=ANY(locked_workspaces) AND state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
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
  -- (4) due source syncs — same shape (H4's slow jittered baseline; pre-slot/demand syncs are
  -- produced by their own sites — the tick owns only the baseline). ix_sources_sync_due serves it:
  FOR source_due IN SELECT s.id,s.workspace_id FROM media_sources s
   WHERE s.workspace_id=ANY(locked_workspaces) AND s.state='active' AND s.next_sync_at IS NOT NULL AND s.next_sync_at<=now()
   AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
   ORDER BY s.next_sync_at,s.id LIMIT rem
  LOOP
   source_capture:=fn_source_read_capture(source_due.workspace_id,source_due.id,p_archive_enabled);
   IF source_capture IS NULL THEN CONTINUE; END IF;
   INSERT INTO jobs(kind,workspace_id,lane,serialization_key,run_at,max_attempts,deadline_at,payload)
    VALUES('sync_media_source',source_due.workspace_id,'bulk','src:'||source_due.id,now(),5,now()+interval '6 hours',
     jsonb_build_object('v',1,'source_id',source_due.id,'reason','baseline') ||
     CASE WHEN source_capture->>'provider'='instagram_archive' THEN jsonb_build_object('source_read_capture',source_capture) ELSE '{}'::jsonb END);
   UPDATE media_sources SET next_sync_at=NULL WHERE id=source_due.id AND workspace_id=source_due.workspace_id;
   n3:=n3+1;
  END LOOP;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
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


ALTER FUNCTION fn_clock_tick(int,interval,jsonb,boolean) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_clock_tick(int,interval,jsonb,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_clock_tick(int,interval,jsonb,boolean) TO svc_worker;
CREATE OR REPLACE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE p_archive_enabled boolean := false; locked_workspaces uuid[]:='{}'; source_due record; source_capture jsonb; lock_ws uuid; k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;   -- 083: the singleton's run_at, named so its
                              -- deadline can be measured FROM it
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- Lock a bounded discovery set before the recurring job/account/credential legs.
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
    AND s.next_sync_at<=now() AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
    ORDER BY s.next_sync_at,s.id LIMIT GREATEST(p_max,0))
  ) candidate ORDER BY workspace_id LOOP
   PERFORM fn_lock_source_read_prefix(lock_ws);
   locked_workspaces:=array_append(locked_workspaces,lock_ws);
  END LOOP;
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    -- 084: this kind's own expired lease goes back to `ready` BEFORE the guard
    -- reads it (#1329). The reaper re-readies every other kind's dead lease,
    -- but it is one of these singletons and cannot revive itself; left
    -- `leased`, a dead lease reads as live to the guard below for ever. The
    -- four assignments are the reaper's first leg's (076), scoped to the kind:
    UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
     WHERE kind = k AND workspace_id IS NULL
       AND state = 'leased' AND locked_until < now();
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      -- 083: from run_at, NOT from now(). A singleton's run_at is its last
      -- completion plus its cadence, which for a slow cadence is well ahead of
      -- this tick; a deadline anchored at mint would be spent before the job
      -- was ever claimable and would end it on its first run.
      due_at := GREATEST(now(), COALESCE(last_done + cadence, now()));
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              due_at, 3,
              due_at + interval '6 hours',   -- LANE_BUDGETS['bulk'][1]
              jsonb_build_object('v', 1));
      n4 := n4 + 1;
    END IF;
  END LOOP;
  rem := GREATEST(p_max - n4, 0);            -- the running remainder every later leg draws on;
                                             -- each leg's LIMIT keeps its count ≤ rem, so the
                                             -- plain subtractions below cannot go negative
  -- (2) due accounts → plan_slot jobs + slot-cursor advance, one set-based statement
  -- (the O(due) scan, H3; ix_ig_accounts_due serves it):
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
  -- (3) due credential refreshes — one set-based statement (D31: the scheduled refresh is also
  -- the liveness probe; the cadence is decoupled from expiry proximity). Reads ride svc_clock's
  -- payload-free column grant; ix_credentials_refresh_due serves the scan:
  WITH due AS (
    SELECT id, workspace_id FROM oauth_credentials
     WHERE workspace_id=ANY(locked_workspaces) AND state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
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
  -- (4) due source syncs — same shape (H4's slow jittered baseline; pre-slot/demand syncs are
  -- produced by their own sites — the tick owns only the baseline). ix_sources_sync_due serves it:
  FOR source_due IN SELECT s.id,s.workspace_id FROM media_sources s
   WHERE s.workspace_id=ANY(locked_workspaces) AND s.state='active' AND s.next_sync_at IS NOT NULL AND s.next_sync_at<=now()
   AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
   ORDER BY s.next_sync_at,s.id LIMIT rem
  LOOP
   source_capture:=fn_source_read_capture(source_due.workspace_id,source_due.id,p_archive_enabled);
   IF source_capture IS NULL THEN CONTINUE; END IF;
   INSERT INTO jobs(kind,workspace_id,lane,serialization_key,run_at,max_attempts,deadline_at,payload)
    VALUES('sync_media_source',source_due.workspace_id,'bulk','src:'||source_due.id,now(),5,now()+interval '6 hours',
     jsonb_build_object('v',1,'source_id',source_due.id,'reason','baseline') ||
     CASE WHEN source_capture->>'provider'='instagram_archive' THEN jsonb_build_object('source_read_capture',source_capture) ELSE '{}'::jsonb END);
   UPDATE media_sources SET next_sync_at=NULL WHERE id=source_due.id AND workspace_id=source_due.workspace_id;
   n3:=n3+1;
  END LOOP;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
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



CREATE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval,p_archive_enabled boolean)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE locked_workspaces uuid[]:='{}'; lock_ws uuid; expired_job record; refusal text; n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  -- Lock all bounded candidate workspaces before any intent/item/lease rows.
  FOR lock_ws IN SELECT DISTINCT workspace_id FROM (
   (SELECT workspace_id FROM jobs WHERE state='leased' AND locked_until<now() ORDER BY id LIMIT GREATEST(p_lim,0))
   UNION (SELECT workspace_id FROM jobs WHERE state='ready' AND deadline_at<=now()
    AND kind IN ('reap_expired','reconcile_ambiguous','alert_stranded_sources','reap_transit_assets',
     'plan_slot','refresh_credential','reauth_prompt','deliver_outbox','sync_media_source','first_ingest_chunk') ORDER BY id LIMIT GREATEST(p_lim,0))
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
                                  'first_ingest_chunk')   -- the kinds a sweep re-mints
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
CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int,p_approval_ttl interval,p_approved_ttl interval)
RETURNS int LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT fn_reaper_sweep(p_lim,p_approval_ttl,p_approved_ttl,false)
$$;
REVOKE CREATE ON SCHEMA public FROM svc_claim,svc_clock,svc_maintenance;
-- runner:postcondition SELECT has_function_privilege('svc_worker','fn_claim_job(text,text,interval,int,boolean)','EXECUTE')
-- runner:postcondition SELECT has_function_privilege('svc_worker','fn_clock_tick(int,interval,jsonb,boolean)','EXECUTE')
-- runner:postcondition SELECT has_function_privilege('svc_worker','fn_reaper_sweep(int,interval,interval,boolean)','EXECUTE')
