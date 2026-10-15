-- Refresh stored function definitions and remove unused intent fields.
-- The runner wraps this file and its ledger row in one transaction.
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_description d JOIN pg_proc p ON d.classoid = 'pg_proc'::regclass AND d.objoid = p.oid JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_description d JOIN pg_class c ON d.classoid = 'pg_class'::regclass AND d.objoid = c.oid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prosecdef AND NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) setting WHERE setting LIKE 'search_path=%'))

LOCK TABLE public.post_intents IN ACCESS EXCLUSIVE MODE;

DO $guard$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'public.post_intents'::regclass
             AND attname = 'legacy_queue_item_id' AND NOT attisdropped) THEN
    IF EXISTS (SELECT 1 FROM public.post_intents WHERE legacy_queue_item_id IS NOT NULL) THEN
      RAISE EXCEPTION 'legacy_queue_item_id contains data; catalog refresh refuses';
    END IF;
  END IF;
  IF EXISTS (SELECT 1 FROM public.post_intents WHERE published_via = 'legacy_backfill') THEN
    RAISE EXCEPTION 'published_via legacy_backfill contains data; catalog refresh refuses';
  END IF;
END $guard$;

CREATE OR REPLACE FUNCTION trg_intent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.state IN ('posted','skipped','rejected','expired','failed','cancelled') THEN
    RAISE EXCEPTION 'post_intent % is terminal (%) and immutable', OLD.id, OLD.state
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.state IS DISTINCT FROM OLD.state AND NOT EXISTS (
       SELECT 1 FROM post_intent_transitions t
       WHERE t.from_state = OLD.state AND t.to_state = NEW.state) THEN
    RAISE EXCEPTION 'illegal transition % -> % on post_intent %', OLD.state, NEW.state, OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.state IS DISTINCT FROM OLD.state THEN NEW.entered_state_at := now(); END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_intent_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    IF NULLIF(current_setting('app.actor_kind', true), '') IS NULL THEN
      RAISE EXCEPTION 'state change without app.actor_kind — anonymous writes are forbidden';
    END IF;
    INSERT INTO audit_events (workspace_id, entity_kind, entity_id, from_state, to_state,
                              actor_kind, actor_user_id, channel, detail)
    VALUES (NEW.workspace_id, 'post_intent', NEW.id, OLD.state, NEW.state,
            current_setting('app.actor_kind'),
            NULLIF(current_setting('app.actor_user_id', true), '')::uuid,
            NULLIF(current_setting('app.channel', true), ''),
            NULL);
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION trg_intent_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state <> 'scheduled'
     AND COALESCE(current_setting('app.actor_kind', true), '') <> 'migration' THEN
    RAISE EXCEPTION 'post_intents are born scheduled (got %)', NEW.state;
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION trg_governance_audit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  r    RECORD;
  kind TEXT;
  ws   UUID;
  ent  UUID;
  fs   TEXT;
  ts   TEXT;
BEGIN
  IF NULLIF(current_setting('app.actor_kind', true), '') IS NULL THEN
    RAISE EXCEPTION 'governance mutation on % without app.actor_kind — anonymous writes are forbidden',
      TG_TABLE_NAME;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF TG_TABLE_NAME = 'ig_accounts' THEN
      IF ROW(NEW.workspace_id, NEW.provider_account_ref, NEW.handle, NEW.display_name, NEW.state,
             NEW.posts_per_day, NEW.posting_hours_start, NEW.posting_hours_end, NEW.tz)
         IS NOT DISTINCT FROM
         ROW(OLD.workspace_id, OLD.provider_account_ref, OLD.handle, OLD.display_name, OLD.state,
             OLD.posts_per_day, OLD.posting_hours_start, OLD.posting_hours_end, OLD.tz) THEN
        RETURN NULL;
      END IF;
    ELSIF TG_TABLE_NAME = 'oauth_credentials' THEN
      IF ROW(NEW.workspace_id, NEW.ig_account_id, NEW.media_source_id, NEW.provider,
             NEW.encrypted_payload, NEW.state)
         IS NOT DISTINCT FROM
         ROW(OLD.workspace_id, OLD.ig_account_id, OLD.media_source_id, OLD.provider,
             OLD.encrypted_payload, OLD.state) THEN
        RETURN NULL;
      END IF;
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
  kind := CASE TG_TABLE_NAME
            WHEN 'workspaces'        THEN 'workspace'
            WHEN 'workspace_members' THEN 'member'
            WHEN 'oauth_credentials' THEN 'credential'
            WHEN 'ig_accounts'       THEN 'ig_account'
            WHEN 'channel_bindings'  THEN 'channel_binding'
          END;
  IF TG_TABLE_NAME = 'workspaces' THEN
    ws := r.id;           ent := r.id;
  ELSIF TG_TABLE_NAME = 'workspace_members' THEN
    ws := r.workspace_id; ent := r.user_id;
  ELSE
    ws := r.workspace_id; ent := r.id;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF TG_TABLE_NAME = 'workspace_members' THEN fs := OLD.role;  ts := NEW.role;
    ELSE                                        fs := OLD.state; ts := NEW.state;
    END IF;
  ELSIF TG_OP = 'INSERT' THEN
    IF TG_TABLE_NAME = 'workspace_members' THEN ts := NEW.role;  ELSE ts := NEW.state; END IF;
  ELSE
    IF TG_TABLE_NAME = 'workspace_members' THEN fs := OLD.role;  ELSE fs := OLD.state; END IF;
  END IF;
  INSERT INTO audit_events (workspace_id, entity_kind, entity_id, from_state, to_state,
                            actor_kind, actor_user_id, channel, detail)
  VALUES (ws, kind, ent, fs, ts,
          current_setting('app.actor_kind'),
          NULLIF(current_setting('app.actor_user_id', true), '')::uuid,
          NULLIF(current_setting('app.channel', true), ''),
          jsonb_build_object('v', 1, 'op', TG_OP));
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int)
RETURNS SETOF jobs LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM fn_claim_job(p_lane,p_worker,p_lease,p_ws_lane_cap,false)
$$;

CREATE OR REPLACE FUNCTION fn_next_slot(p_after timestamptz, p_tz text,
                             p_start_h int, p_end_h int, p_ppd int)
RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
DECLARE
  tz         text      := fn_safe_tz(p_tz);
  win_hours  numeric   := CASE WHEN p_end_h = p_start_h THEN 24
                               ELSE ((p_end_h - p_start_h + 24) % 24) END;
  aft_local  timestamp := p_after AT TIME ZONE tz;
  anchor     timestamp := date_trunc('day', aft_local) + p_start_h * interval '1 hour';
  cyc_start  timestamp;
  idx        int;
BEGIN
  cyc_start := CASE WHEN aft_local >= anchor THEN anchor ELSE anchor - interval '1 day' END;
  idx := round(EXTRACT(epoch FROM (aft_local - cyc_start)) * p_ppd / (win_hours * 3600.0));
  IF idx + 1 >= p_ppd THEN
    RETURN (cyc_start + interval '1 day') AT TIME ZONE tz;
  END IF;
  RETURN (cyc_start + ((idx + 1) * win_hours / p_ppd) * interval '1 hour') AT TIME ZONE tz;
END $$;

CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int,p_approval_ttl interval,p_approved_ttl interval)
RETURNS int LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT fn_reaper_sweep(p_lim,p_approval_ttl,p_approved_ttl,false)
$$;

CREATE OR REPLACE FUNCTION fn_retention_batch(p_class text, p_keep interval, p_batch int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE n int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  IF p_class = 'rate_counters' THEN
    DELETE FROM rate_counters WHERE (scope, key, window_start) IN (
      SELECT scope, key, window_start FROM rate_counters
       WHERE window_start < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSE
    RAISE EXCEPTION 'unknown retention class %', p_class;
  END IF;
  RETURN n;
END $$;

CREATE OR REPLACE FUNCTION fn_invitation_accept(p_token_hash text, p_user uuid, p_provider text,
                                                p_verified_email text, p_tg_user_id bigint,
                                                p_channel text)
RETURNS TABLE (o_workspace_id uuid, o_granted_role text, o_matched boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE inv record; m boolean; grant_role text; bind uuid;
BEGIN
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', COALESCE(p_channel, 'web'), true);
  SELECT * INTO inv FROM workspace_invitations i
   WHERE i.token_hash = p_token_hash AND i.state = 'pending' AND i.expires_at > now()
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invitation not acceptable (used, revoked, expired, or unknown)'
      USING ERRCODE = 'no_data_found';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM workspaces w
                     WHERE w.id = inv.workspace_id AND w.state = 'active')
     OR NOT EXISTS (SELECT 1 FROM workspace_members a
                      JOIN users u ON u.id = a.user_id AND u.state = 'active'
                     WHERE a.workspace_id = inv.workspace_id
                       AND a.user_id = inv.invited_by_user_id
                       AND a.role IN ('owner', 'admin'))
     OR EXISTS (SELECT 1 FROM workspace_member_removals r
                 WHERE r.workspace_id = inv.workspace_id AND r.user_id = p_user
                   AND r.removed_at >= inv.created_at) THEN
    RAISE EXCEPTION 'invitation not acceptable (no longer legitimate)'
      USING ERRCODE = 'no_data_found';
  END IF;
  IF p_provider = 'google' THEN
    IF inv.email IS NOT NULL THEN
      IF lower(p_verified_email) IS DISTINCT FROM lower(inv.email) THEN
        RAISE EXCEPTION 'identity proof mismatch' USING ERRCODE = 'check_violation';
      END IF;
      m := true;
    ELSE m := false;
    END IF;
  ELSIF p_provider = 'telegram' THEN
    IF inv.invited_tg_user_id IS NOT NULL THEN
      IF p_tg_user_id IS DISTINCT FROM inv.invited_tg_user_id THEN
        RAISE EXCEPTION 'identity proof mismatch' USING ERRCODE = 'check_violation';
      END IF;
      m := true;
    ELSE m := false;
    END IF;
  ELSE
    RAISE EXCEPTION 'unknown acceptance provider %', p_provider;
  END IF;
  UPDATE workspace_invitations
     SET state = 'accepted', accepted_by_user_id = p_user, accepted_email_matched = m
   WHERE id = inv.id;
  grant_role := CASE WHEN inv.role = 'admin' AND m THEN 'admin' ELSE 'member' END;
  INSERT INTO workspace_members (workspace_id, user_id, role, added_by_user_id)
  VALUES (inv.workspace_id, p_user, grant_role, inv.invited_by_user_id)
  ON CONFLICT (workspace_id, user_id) DO NOTHING;
  IF EXISTS (SELECT 1 FROM workspace_member_removals r
              WHERE r.workspace_id = inv.workspace_id AND r.user_id = p_user
                AND r.removed_at >= inv.created_at) THEN
    RAISE EXCEPTION 'invitation not acceptable (no longer legitimate)'
      USING ERRCODE = 'no_data_found';
  END IF;
  IF inv.role = 'admin' AND NOT m THEN
    SELECT b.id INTO bind FROM channel_bindings b
     WHERE b.workspace_id = inv.workspace_id AND b.state = 'active'
     ORDER BY b.created_at LIMIT 1;
    IF bind IS NOT NULL THEN
      INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload)
      VALUES (inv.workspace_id, bind, 'notification',
              jsonb_build_object('v', 1, 'template', 'elevation_pending',
                                 'invitation_id', inv.id, 'accepted_by', p_user));
    END IF;
  END IF;
  RETURN QUERY SELECT inv.workspace_id, grant_role, m;
END $$;

CREATE OR REPLACE FUNCTION fn_health_posting_freshness()
RETURNS TABLE (o_posted_ever bigint, o_last_post_age_seconds numeric, o_intents_ever bigint, o_oldest_intent_age_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT count(*) FILTER (WHERE state = 'posted' AND published_via <> 'dry_run'),
         min(EXTRACT(EPOCH FROM now() - entered_state_at))
           FILTER (WHERE state = 'posted' AND published_via <> 'dry_run'),
         count(*),
         max(EXTRACT(EPOCH FROM now() - created_at))
    FROM post_intents
$$;

CREATE OR REPLACE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE p_archive_enabled boolean := false; locked_workspaces uuid[]:='{}'; source_due record; source_capture jsonb; lock_ws uuid; k text; cadence interval; last_done timestamptz; rem int;
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
    AND s.next_sync_at<=now() AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
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

CREATE OR REPLACE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int,p_archive_enabled boolean)
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

CREATE OR REPLACE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb, p_archive_enabled boolean)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, archive_reauth_jobs int)
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
    AND s.next_sync_at<=now() AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) r WHERE r.admitted)
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

CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval,p_archive_enabled boolean)
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
    refusal:=fn_source_job_read_reason(expired_job.workspace_id,expired_job.payload,p_archive_enabled);
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
       AND fn_source_job_read_reason(e.workspace_id,e.payload,p_archive_enabled) IS NULL
       AND EXISTS(SELECT 1 FROM fn_source_read_authority(s.workspace_id,s.id,p_archive_enabled) a WHERE a.admitted)
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;

CREATE OR REPLACE FUNCTION fn_mix_conversion_jobs(p_limit int,p_run_id text,p_run_digest text)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE minted int;
BEGIN
 IF p_run_id IS NULL OR p_run_id !~ '^[a-zA-Z0-9_.-]{1,128}$'
  OR p_run_digest IS NULL OR p_run_digest !~ '^[0-9a-f]{64}$' THEN RETURN 0; END IF;
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
        FROM instagram_archive_generations g
        JOIN media_sources s ON s.workspace_id = g.workspace_id AND s.id = g.source_id
       WHERE g.workspace_id = v_ws AND g.authorization_id = m.authorization_id
         AND s.provider = 'instagram_archive' AND s.origin_ig_account_id = m.ig_account_id
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

CREATE OR REPLACE FUNCTION fn_archive_redact_generation_records(p_source uuid, p_generation_ids uuid[])
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
     OR (SELECT count(*) FROM instagram_archive_generations g
          WHERE g.workspace_id = v_ws AND g.source_id = p_source AND g.id = ANY (p_generation_ids)
            AND g.lifecycle IN ('deletion_pending','deleted'))
        <> (SELECT count(DISTINCT x) FROM unnest(p_generation_ids) x) THEN
    RAISE EXCEPTION 'archive redaction refused: scope_unavailable' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_operation_provenance p
               JOIN provider_operations o ON o.workspace_id = p.workspace_id AND o.id = p.operation_id
              WHERE p.workspace_id = v_ws AND p.data_generation_id = ANY (p_generation_ids)
                AND o.state IN ('permitted','ambiguous')) THEN
    RAISE EXCEPTION 'archive redaction refused: operation_unresolved' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_artifacts a
              WHERE a.workspace_id = v_ws AND a.data_generation_id = ANY (p_generation_ids) AND a.kind = 'transit'
                AND NOT (a.outcome = 'settled' AND a.cleanup_state = 'destroyed')) THEN
    RAISE EXCEPTION 'archive redaction refused: transit_unresolved' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_intent_provenance p
               JOIN post_intents i ON i.workspace_id = p.workspace_id AND i.id = p.intent_id
              WHERE p.workspace_id = v_ws
                AND (p.capture_generation_id = ANY (p_generation_ids) OR p.staged_generation_id = ANY (p_generation_ids))
                AND i.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) THEN
    RAISE EXCEPTION 'archive redaction refused: intent_live' USING ERRCODE = 'P0001';
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
           JOIN instagram_archive_generations g ON g.workspace_id = x.workspace_id
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

CREATE OR REPLACE FUNCTION fn_archive_reconcile_jobs(p_limit int, p_run_id text, p_run_digest text)
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
           WHERE s.workspace_id = a.workspace_id AND s.provider = 'instagram_archive'
            AND s.origin_ig_account_id = a.id)))
   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.workspace_id = w.id
    AND j.kind = 'reconcile_archive_accounts' AND j.payload->>'run_digest' = p_run_digest)
  ORDER BY w.id LIMIT LEAST(GREATEST(p_limit, 0), 5)
 )
 INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
 SELECT 'reconcile_archive_accounts', id, 'bulk', 'archive-reconcile:' || id, now(), 5, now() + interval '6 hours',
  jsonb_build_object('v', 1, 'run_id', p_run_id, 'run_digest', p_run_digest)
 FROM pending;
 GET DIAGNOSTICS minted = ROW_COUNT;
 RETURN minted;
END $$;

ALTER TABLE public.post_intents DROP COLUMN IF EXISTS legacy_queue_item_id;

ALTER TABLE public.post_intents DROP CONSTRAINT IF EXISTS ck_intent_via;
ALTER TABLE public.post_intents ADD CONSTRAINT ck_intent_via CHECK (published_via IN ('api','manual','dry_run'));

ALTER TABLE public.post_intents DROP CONSTRAINT IF EXISTS ck_posted_complete;
ALTER TABLE public.post_intents ADD CONSTRAINT ck_posted_complete CHECK (state <> 'posted' OR (published_via = 'manual' AND cap_consumed_on IS NOT NULL) OR (published_via = 'dry_run' AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL) OR (published_via = 'api' AND ig_container_id IS NOT NULL AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL));

DO $comments$
DECLARE item record; object_kind text;
BEGIN
  FOR item IN
    SELECT p.oid::regprocedure AS identity FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
      JOIN pg_description d ON d.classoid = 'pg_proc'::regclass AND d.objoid = p.oid
      WHERE n.nspname = 'public'
  LOOP
    EXECUTE format('COMMENT ON FUNCTION %s IS NULL', item.identity);
  END LOOP;
  FOR item IN
    SELECT c.relname, c.relkind, d.objsubid, a.attname FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_description d ON d.classoid = 'pg_class'::regclass AND d.objoid = c.oid
      LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid
      WHERE n.nspname = 'public'
  LOOP
    IF item.objsubid > 0 THEN
      EXECUTE format('COMMENT ON COLUMN public.%I.%I IS NULL', item.relname, item.attname);
    ELSE
      object_kind := CASE item.relkind
        WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE' WHEN 'v' THEN 'VIEW'
        WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'S' THEN 'SEQUENCE'
        WHEN 'f' THEN 'FOREIGN TABLE' WHEN 'i' THEN 'INDEX'
        WHEN 'I' THEN 'INDEX' WHEN 'c' THEN 'TYPE' END;
      IF object_kind IS NULL THEN
        RAISE EXCEPTION 'unsupported public relation kind %', item.relkind;
      END IF;
      EXECUTE format('COMMENT ON %s public.%I IS NULL', object_kind, item.relname);
    END IF;
  END LOOP;
END $comments$;
