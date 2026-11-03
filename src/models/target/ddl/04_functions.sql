-- Functions, created after the tables. Bodies are checked when they first run.

SET LOCAL check_function_bodies = off;

CREATE FUNCTION trg_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE FUNCTION trg_members_owner_exists() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM workspaces w WHERE w.id = OLD.workspace_id)
     AND NOT EXISTS (SELECT 1 FROM workspace_members m
                     WHERE m.workspace_id = OLD.workspace_id AND m.role = 'owner') THEN
    RAISE EXCEPTION 'workspace % has no owner at commit', OLD.workspace_id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION trg_workspaces_owner_at_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM workspace_members m
                 WHERE m.workspace_id = NEW.id AND m.role = 'owner') THEN
    RAISE EXCEPTION 'workspace % created without an owner member row', NEW.id
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION trg_intent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE FUNCTION trg_intent_audit() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE FUNCTION trg_intent_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state <> 'scheduled'
     AND COALESCE(current_setting('app.actor_kind', true), '') <> 'migration' THEN
    RAISE EXCEPTION 'post_intents are born scheduled (got %)', NEW.state;
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION trg_governance_audit() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int)
RETURNS SETOF jobs LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT * FROM fn_claim_job(p_lane,p_worker,p_lease,p_ws_lane_cap,false)
$$;

CREATE FUNCTION fn_extend_leases(p_tokens uuid[], p_lease interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE n int;
BEGIN
  UPDATE jobs SET locked_until = now() + p_lease
   WHERE lease_token = ANY (p_tokens) AND state = 'leased' AND locked_until > now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

CREATE FUNCTION fn_next_slot(p_after timestamptz, p_tz text,
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

CREATE FUNCTION fn_reconciler_sweep(p_lim int, p_rungs interval[], p_notify_after interval)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid, o_reason text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  RETURN QUERY
  WITH ladder AS (
    SELECT i.id, i.workspace_id, 'ladder_due'::text AS reason
      FROM post_intents i
     WHERE i.state = 'publishing_ambiguous'
       AND COALESCE(
             (i.last_error->'evidence'->>'last_checked_at')::timestamptz
               + p_rungs[LEAST(COALESCE((i.last_error->'evidence'->>'checks')::int, 0) + 1,
                               array_length(p_rungs, 1))],
             i.entered_state_at) <= now()
     ORDER BY i.entered_state_at LIMIT p_lim
  ), notify AS (
    SELECT i.id, i.workspace_id, 'notify_window'::text AS reason
      FROM post_intents i
     WHERE i.state = 'review_required'
       AND i.entered_state_at + p_notify_after <= now()
       AND NOT COALESCE((i.last_error->'evidence'->>'customer_notified')::boolean, false)
     ORDER BY i.entered_state_at
     LIMIT GREATEST(p_lim - (SELECT count(*) FROM ladder), 0)
  )
  SELECT * FROM ladder UNION ALL SELECT * FROM notify;
END $$;

CREATE FUNCTION fn_reaper_sweep(p_lim int,p_approval_ttl interval,p_approved_ttl interval)
RETURNS int LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT fn_reaper_sweep(p_lim,p_approval_ttl,p_approved_ttl,false)
$$;

CREATE FUNCTION fn_retention_batch(p_class text, p_keep interval, p_batch int)
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

CREATE FUNCTION fn_offboard_finalize(p_ws uuid, p_grace interval)
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

CREATE FUNCTION fn_auth_plane_sweep(p_state_slack interval, p_session_keep interval,
                                    p_dedup_keep interval, p_batch int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE n int := 0; c int;
BEGIN
  DELETE FROM oauth_states WHERE state IN (
    SELECT state FROM oauth_states
     WHERE (consumed_at IS NOT NULL AND consumed_at < now() - p_state_slack)
        OR expires_at < now() - p_state_slack
     LIMIT p_batch);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  DELETE FROM session_tokens WHERE id IN (
    SELECT id FROM session_tokens
     WHERE (revoked_at IS NOT NULL AND revoked_at < now() - p_session_keep)
        OR expires_at < now() - p_session_keep
     LIMIT p_batch);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  DELETE FROM command_dedup WHERE (channel, principal, external_ref) IN (
    SELECT channel, principal, external_ref FROM command_dedup
     WHERE created_at < now() - p_dedup_keep LIMIT p_batch);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  RETURN n;
END $$;

-- The pre-membership door: accept a pending, unexpired invitation by its token
-- hash, with the identity proof computed here. Refuses (no_data_found) an
-- invitation that is no longer legitimate: its workspace is not active, its
-- inviter is no longer an owner or admin there with an active account, or the
-- acceptor was removed from the workspace after it was sent. SECURITY DEFINER
-- owned by svc_membership with EXECUTE granted to svc_ingress.
CREATE FUNCTION fn_invitation_accept(p_token_hash text, p_user uuid, p_provider text,
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

CREATE FUNCTION trg_intent_no_self_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'same-state write to post_intent % (state %) — a transition that did not happen', OLD.id, OLD.state
    USING ERRCODE = 'check_violation';
END $$;

-- The calling user's workspace memberships: a SECURITY DEFINER user-plane read
-- owned by svc_membership with EXECUTE granted to svc_ingress. The caller is
-- app.actor_user_id, read internally and never a parameter; unset, it returns
-- no rows. Exists because p_tenant on workspace_members cannot answer a
-- cross-tenant question for one user.
CREATE FUNCTION fn_memberships_for_caller()
RETURNS TABLE (o_workspace_id uuid, o_name varchar, o_state text, o_role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT m.workspace_id, w.name, w.state, m.role
    FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
   WHERE m.user_id = NULLIF(current_setting('app.actor_user_id', true), '')::uuid
   ORDER BY w.created_at, w.id
$$;

-- Chat inbound -> workspace: the one row for a (channel, external_ref), any
-- state, so the caller can tell revoked from never-bound. SECURITY DEFINER
-- owned by svc_membership with EXECUTE granted to svc_ingress; exists because
-- p_tenant on channel_bindings cannot answer a pre-context question about one
-- chat.
CREATE FUNCTION fn_resolve_binding(p_channel text, p_external_ref text)
RETURNS TABLE (o_workspace_id uuid, o_binding_id uuid, o_state text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT b.workspace_id, b.id, b.state
    FROM channel_bindings b
   WHERE b.channel = p_channel AND b.external_ref = p_external_ref
$$;

-- The Telegram join path: a person seen in a bound, active group becomes a
-- member (role member; never a downgrade), unless an admin removed them and
-- they have not been invited back. p_user is the caller's resolution of the
-- sender's linked Telegram identity — the caller proves the person, this door
-- trusts it. Sets the actor GUCs transaction-locally, so it must be the last
-- governance write in its transaction. SECURITY DEFINER owned by
-- svc_membership with EXECUTE granted to svc_ingress. Outcomes: joined,
-- already_member, removed, unbound_chat, revoked_chat, workspace_inactive,
-- unknown_user.
CREATE FUNCTION fn_group_member_seen(p_channel text, p_external_ref text, p_user uuid)
RETURNS TABLE (o_workspace_id uuid, o_outcome text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_ws uuid;
  v_state text;
  v_ws_state text;
  v_inserted int;
BEGIN
  SELECT b.workspace_id, b.state INTO v_ws, v_state
    FROM channel_bindings b
   WHERE b.channel = p_channel AND b.external_ref = p_external_ref;
  IF v_ws IS NULL THEN
    RETURN QUERY SELECT NULL::uuid, 'unbound_chat'::text; RETURN;
  END IF;
  IF v_state <> 'active' THEN
    RETURN QUERY SELECT v_ws, 'revoked_chat'::text; RETURN;
  END IF;
  SELECT w.state INTO v_ws_state FROM workspaces w WHERE w.id = v_ws;
  IF v_ws_state IS DISTINCT FROM 'active' THEN
    RETURN QUERY SELECT v_ws, 'workspace_inactive'::text; RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM workspace_member_removals r
              WHERE r.workspace_id = v_ws AND r.user_id = p_user)
     AND NOT EXISTS (SELECT 1 FROM workspace_members m
                      WHERE m.workspace_id = v_ws AND m.user_id = p_user) THEN
    RETURN QUERY SELECT v_ws, 'removed'::text; RETURN;
  END IF;
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', 'telegram', true);
  BEGIN
    INSERT INTO workspace_members (workspace_id, user_id, role)
    VALUES (v_ws, p_user, 'member')
    ON CONFLICT (workspace_id, user_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
  EXCEPTION WHEN foreign_key_violation THEN
    RETURN QUERY SELECT v_ws, 'unknown_user'::text; RETURN;
  END;
  RETURN QUERY SELECT v_ws, CASE WHEN v_inserted > 0 THEN 'joined' ELSE 'already_member' END;
END $$;

-- The revoke for every join edge: an admin removes a member explicitly, and
-- the removal is recorded so the Telegram join path cannot undo it. The one
-- DELETE on workspace_members in the system lives here (no login role
-- deletes). The door checks its caller itself: p_workspace must be the claimed
-- tenant (app.tenant_id) and p_by_user an owner or admin of it, or it raises.
-- Outcomes: removed, not_found, owner (never removable here), self (never
-- through this door). SECURITY DEFINER owned by svc_membership with EXECUTE
-- granted to svc_ingress.
CREATE FUNCTION fn_member_remove(p_workspace uuid, p_user uuid, p_by_user uuid)
RETURNS TABLE (o_outcome text, o_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_role text;
BEGIN
  IF p_workspace IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid
     OR NOT EXISTS (SELECT 1 FROM workspace_members a
                     WHERE a.workspace_id = p_workspace AND a.user_id = p_by_user
                       AND a.role IN ('owner', 'admin')) THEN
    RAISE EXCEPTION 'member removal outside the caller''s admin scope';
  END IF;
  IF p_user = p_by_user THEN
    RETURN QUERY SELECT 'self'::text, NULL::text; RETURN;
  END IF;
  SELECT m.role INTO v_role FROM workspace_members m
   WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  IF v_role IS NULL THEN
    RETURN QUERY SELECT 'not_found'::text, NULL::text; RETURN;
  END IF;
  IF v_role = 'owner' THEN
    RETURN QUERY SELECT 'owner'::text, v_role; RETURN;
  END IF;
  DELETE FROM workspace_members m WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  INSERT INTO workspace_member_removals (workspace_id, user_id, removed_by_user_id)
  VALUES (p_workspace, p_user, p_by_user)
  ON CONFLICT (workspace_id, user_id)
  DO UPDATE SET removed_by_user_id = EXCLUDED.removed_by_user_id, removed_at = now();
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

CREATE FUNCTION fn_reaper_stale_approved(p_approved_ttl interval, p_lim int)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.id, i.workspace_id
    FROM post_intents i JOIN workspaces w ON w.id = i.workspace_id
   WHERE i.state = 'approved' AND NOT w.is_paused
     AND i.entered_state_at < now() - p_approved_ttl
   ORDER BY i.entered_state_at
   LIMIT GREATEST(p_lim, 0)
$$;

-- Landings and their freshness, estate-wide, for /health/posting: confirmed
-- posts (never a dry_run row), the age of the freshest,
-- every intent ever and the age of the oldest. A SECURITY DEFINER read owned
-- by svc_maintenance; EXECUTE for svc_ingress, the surface's login. Exists
-- because p_tenant hides every row from a tenant-less read.
CREATE FUNCTION fn_health_posting_freshness()
RETURNS TABLE (o_posted_ever bigint, o_last_post_age_seconds numeric, o_intents_ever bigint, o_oldest_intent_age_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT count(*) FILTER (WHERE state = 'posted' AND published_via <> 'dry_run'),
         min(EXTRACT(EPOCH FROM now() - entered_state_at))
           FILTER (WHERE state = 'posted' AND published_via <> 'dry_run'),
         count(*),
         max(EXTRACT(EPOCH FROM now() - created_at))
    FROM post_intents
$$;

-- The cap ledger's attempt record for /health/posting: what was debited in
-- total and how many (workspace, account, day) buckets ever debited. Owned by
-- svc_maintenance; EXECUTE for svc_ingress.
CREATE FUNCTION fn_health_publish_attempts()
RETURNS TABLE (o_debited_total bigint, o_ledger_days bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT coalesce(sum(count), 0), count(*)
    FROM daily_post_counts
$$;

-- The destinations that could receive a post, for /health/posting: a count and
-- the age of the oldest. Owned by svc_maintenance; EXECUTE for svc_ingress.
CREATE FUNCTION fn_health_destinations()
RETURNS TABLE (o_accounts_active bigint, o_oldest_active_destination_age_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT count(*), max(EXTRACT(EPOCH FROM now() - created_at))
    FROM ig_accounts WHERE state = 'active'
$$;

-- The cursor lag for /health/scheduling, over the accounts the clock advances
-- (fn_clock_tick's population: an active account of an active, unpaused
-- workspace): those whose slot is due and unadvanced, all of them, and the
-- worst lag. A paused workspace's cursor is owed no tick, so it is no stall.
-- An active account with no cursor is a provisioning fault, counted but never
-- stalled. Owned by svc_maintenance; EXECUTE for svc_ingress.
CREATE FUNCTION fn_health_scheduling_lag()
RETURNS TABLE (o_stalled bigint, o_accounts_active bigint, o_max_lag_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT count(*) FILTER (WHERE a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()),
         count(*),
         max(EXTRACT(EPOCH FROM now() - a.next_slot_at))
           FILTER (WHERE a.next_slot_at IS NOT NULL AND a.next_slot_at <= now())
    FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id
   WHERE a.state = 'active' AND w.state = 'active' AND NOT w.is_paused
$$;

-- The backpressure signal's ready lanes: due, unclaimed jobs per lane and the
-- oldest wait, every tenant's included. Owned by svc_maintenance; EXECUTE for
-- svc_ingress (/api/v1/ops/health) and svc_worker (its own status line).
CREATE FUNCTION fn_health_ready_lanes()
RETURNS TABLE (o_lane text, o_ready bigint, o_oldest_age numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT lane, count(*), EXTRACT(EPOCH FROM max(now() - run_at))
    FROM jobs WHERE state = 'ready' AND run_at <= now()
   GROUP BY lane
$$;

-- The backpressure signal's pending outbox rows, every tenant's included.
-- Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker.
CREATE FUNCTION fn_health_outbox_pending()
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT count(*) FROM channel_outbox WHERE state = 'pending'
$$;

-- The backpressure signal's longest tenant wait, in seconds, naming no tenant:
-- the shape the API's login may hold. Owned by svc_maintenance; EXECUTE for
-- svc_ingress and svc_worker.
CREATE FUNCTION fn_health_oldest_tenant_wait()
RETURNS TABLE (o_wait numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXTRACT(EPOCH FROM now() - min(run_at))
    FROM jobs WHERE state = 'ready' AND run_at <= now() AND workspace_id IS NOT NULL
   GROUP BY workspace_id ORDER BY 1 DESC LIMIT 1
$$;

-- The backpressure signal's longest-waiting tenant, NAMED — for the worker's
-- own log and nothing else: EXECUTE for svc_worker only, so the API's login
-- never holds a foreign workspace id. Owned by svc_maintenance.
CREATE FUNCTION fn_health_oldest_tenant_wait_named()
RETURNS TABLE (o_workspace_id uuid, o_wait numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT workspace_id, EXTRACT(EPOCH FROM now() - min(run_at))
    FROM jobs WHERE state = 'ready' AND run_at <= now() AND workspace_id IS NOT NULL
   GROUP BY workspace_id ORDER BY 2 DESC LIMIT 1
$$;

-- The accounts a Meta callback's subject names, across every workspace: a
-- deauthorize or data-deletion request carries no tenant, so p_tenant would
-- hide every row from the API's login. A parameterised SECURITY DEFINER read
-- owned by svc_maintenance, an equality on one bound value; EXECUTE for
-- svc_ingress.
CREATE FUNCTION fn_meta_accounts_for_ref(p_ref text)
RETURNS TABLE (o_ig_account_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT id, workspace_id FROM ig_accounts WHERE provider_account_ref = p_ref
$$;

-- The outbox sender sweep: one deliver_outbox job per pushable Telegram
-- binding with pending (or aged ambiguous) outbox rows and no live sender job,
-- an ambiguous row aged p_age times its attempts (outbox.resolve_aged_ambiguous
-- resolves it on the same schedule, so a row still waiting mints no sender;
-- the plain p_age bound keeps ix_outbox_ambiguous_age's range),
-- every workspace's, minted in the one statement whose NOT EXISTS is its
-- idempotence. The key prefix, the lane budget and the bound are its
-- parameters, spelled once in Python; the binding predicate is
-- bindings.push_binding_where, pinned to this body by a test. A SECURITY
-- DEFINER door owned by svc_maintenance; EXECUTE for svc_worker.
CREATE FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int)
RETURNS int
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH minted AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key,
                      run_at, max_attempts, deadline_at, payload)
    SELECT 'deliver_outbox', b.workspace_id, 'interactive',
           p_prefix || b.id, now(), p_attempts,
           now() + make_interval(secs => p_deadline),
           jsonb_build_object('v', 1, 'binding_id', b.id)
      FROM channel_bindings b
     WHERE b.state = 'active' AND b.channel LIKE 'telegram%'
       AND (EXISTS (SELECT 1 FROM channel_outbox o
                     WHERE o.binding_id = b.id AND o.state = 'pending')
            OR EXISTS (SELECT 1 FROM channel_outbox o
                        WHERE o.binding_id = b.id AND o.state = 'ambiguous'
                          AND o.updated_at <= now() - make_interval(secs => p_age)
                          AND o.updated_at <= now() - make_interval(
                                secs => p_age * GREATEST(o.attempts, 1))))
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.serialization_key = p_prefix || b.id
                          AND j.state IN ('ready', 'leased'))
     LIMIT p_limit
    RETURNING 1)
  SELECT count(*)::int FROM minted
$$;

-- The prompt sweep's second phase across every workspace: intents whose card
-- is out and whose state must advance to awaiting_approval, advanced per
-- workspace under its tenant. Owned by svc_maintenance; EXECUTE for
-- svc_worker.
CREATE FUNCTION fn_prompts_pending(p_limit int)
RETURNS TABLE (o_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.id, i.workspace_id FROM post_intents i
   WHERE i.state = 'prompt_pending'
   LIMIT p_limit
$$;

-- The settled-card sweep's selection across every workspace: live approval
-- cards on active bindings whose story has ended, oldest first — the terminal
-- states are the caller's argument (intent_ledger.TERMINAL_STATES, spelled
-- once). The settlement read and the supersede happen per row under that row's
-- workspace. Owned by svc_maintenance; EXECUTE for svc_worker.
CREATE FUNCTION fn_settled_cards(p_terminal text[], p_limit int)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid, o_binding_id uuid, o_state text, o_entered_state_at timestamptz, o_tz text, o_since timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT o.intent_id, o.workspace_id, o.binding_id, i.state,
         i.entered_state_at, w.tz, min(o.created_at) AS since
    FROM channel_outbox o
    JOIN channel_bindings b ON b.id = o.binding_id AND b.state = 'active'
    JOIN post_intents i ON i.id = o.intent_id
    JOIN workspaces w ON w.id = i.workspace_id
   WHERE o.kind = 'approval_prompt'
     AND o.state IN ('pending', 'sending', 'sent', 'ambiguous')
     AND i.state = ANY(p_terminal)
   GROUP BY o.intent_id, o.workspace_id, o.binding_id, i.state,
            i.entered_state_at, w.tz
   ORDER BY since
   LIMIT p_limit
$$;

-- The stranded-source alert's selection across every workspace: sources in
-- error not alerted within the window. The alerted_at stamp and the card are
-- written per workspace under its tenant. Owned by svc_maintenance; EXECUTE
-- for svc_worker.
CREATE FUNCTION fn_stranded_sources(p_age numeric, p_limit int)
RETURNS TABLE (o_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT id, workspace_id FROM media_sources
   WHERE state = 'error'
     AND (alerted_at IS NULL
          OR alerted_at < now() - make_interval(secs => p_age))
   ORDER BY alerted_at NULLS FIRST
   LIMIT p_limit
$$;

-- The scheduled clock tick: a SECURITY DEFINER producer of due work, owned by
-- svc_clock with EXECUTE granted to svc_worker, and pinned to search_path =
-- pg_catalog, public, pg_temp. One call runs five legs in order - recurring
-- system singletons, due account slots, due credential refreshes, due source
-- syncs, and reauth prompts. The legs share one budget: p_max caps the first,
-- and each later leg draws only on what the ones before it left, so one call
-- mints at most p_max rows. It is NOT the only writer of jobs - application
-- services enqueue directly as well. The refresh leg is scoped to provider
-- ig_login and fails closed: a provider with no refresh door of its own is
-- skipped rather than minted. Every leg mints with a deadline measured from
-- the job's own run_at. The recurring leg first returns an expired lease of
-- its own kind to ready: the reaper re-readies every other kind's expired
-- lease, but it is one of these singletons and cannot revive itself.
CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
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

-- The prompt sweep's due stories across every workspace — the card's columns,
-- origin and scheduler for each scheduled, unflagged intent whose time has
-- come in an active, unpaused workspace: every cadence row, and a planned row
-- only while it can be served (its media available, its account active or
-- awaiting reconnection, no live reject, unsupported, hold or seasonal lock)
-- and within p_late of its time. A NULL p_late, the default, serves no planned
-- row. fn_planned_misses lists the complement. A SECURITY DEFINER read owned
-- by svc_maintenance; the prompting happens per workspace under that
-- workspace's tenant. EXECUTE for svc_worker.
CREATE FUNCTION fn_prompts_due(p_limit int, p_late interval DEFAULT NULL)
RETURNS TABLE (o_id uuid, o_state text, o_workspace_id uuid, o_schedule_slot_at timestamptz, o_file_name text, o_media_kind text, o_mime_type varchar, o_source_id uuid, o_provider_file_ref text, o_handle varchar, o_tz text, o_api_publishing_enabled boolean, o_origin text, o_scheduled_by_user_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.id, i.state, i.workspace_id, i.schedule_slot_at,
         m.file_name, m.media_kind, m.mime_type,
         m.source_id, m.provider_file_ref, a.handle, w.tz,
         w.api_publishing_enabled, i.origin, i.scheduled_by_user_id
    FROM post_intents i
    JOIN workspaces w ON w.id = i.workspace_id
    JOIN media_items m ON m.id = i.media_item_id
     AND m.workspace_id = i.workspace_id
    LEFT JOIN ig_accounts a ON a.id = i.ig_account_id
     AND a.workspace_id = i.workspace_id
   WHERE i.state = 'scheduled' AND i.schedule_slot_at <= now()
     AND NOT i.cancel_requested
     AND w.state = 'active' AND NOT w.is_paused
     AND (i.origin = 'cadence'
          OR (i.schedule_slot_at > now() - p_late
              AND m.state = 'available'
              AND a.state IN ('active', 'reauth_required')
              AND NOT EXISTS (SELECT 1 FROM post_locks l
                               WHERE l.workspace_id = i.workspace_id
                                 AND l.ig_account_id IS NULL
                                 AND l.media_item_id = i.media_item_id
                                 AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                 AND (l.expires_at IS NULL OR l.expires_at > now()))))
   ORDER BY i.schedule_slot_at LIMIT p_limit
$$;

-- The planned stories that will not be served, across every workspace: each
-- due, scheduled, unflagged planned intent that cannot be served
-- (item_removed, item_unsupported, item_missing, item_locked, account_removed,
-- in that precedence) or whose p_late window has passed (paused when its
-- workspace was not taking posts, else late) — the complement of
-- fn_prompts_due's planned rows. The worker expires each with its reason and
-- tells the bound chats in the same transaction, per workspace under that
-- workspace's tenant. STRICT: a NULL window lists nothing. A SECURITY DEFINER
-- read owned by svc_maintenance; EXECUTE for svc_worker.
CREATE FUNCTION fn_planned_misses(p_limit int, p_late interval)
RETURNS TABLE (o_id uuid, o_workspace_id uuid, o_reason text, o_schedule_slot_at timestamptz, o_file_name text, o_handle varchar, o_tz text, o_scheduled_by_user_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT d.id, d.workspace_id, d.reason, d.schedule_slot_at, d.file_name, d.handle, d.tz,
         d.scheduled_by_user_id
    FROM (SELECT i.id, i.workspace_id, i.schedule_slot_at, i.scheduled_by_user_id,
                 m.file_name, a.handle, w.tz,
                 CASE
                   WHEN m.id IS NULL OR m.state = 'removed' THEN 'item_removed'
                   WHEN m.state = 'unsupported' THEN 'item_unsupported'
                   WHEN m.state = 'missing' THEN 'item_missing'
                   WHEN EXISTS (SELECT 1 FROM post_locks l
                                 WHERE l.workspace_id = i.workspace_id
                                   AND l.ig_account_id IS NULL
                                   AND l.media_item_id = i.media_item_id
                                   AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                   AND (l.expires_at IS NULL OR l.expires_at > now()))
                     THEN 'item_locked'
                   WHEN a.state IS NULL OR a.state NOT IN ('active', 'reauth_required')
                     THEN 'account_removed'
                   WHEN i.schedule_slot_at > now() - p_late THEN NULL
                   WHEN w.state <> 'active' OR w.is_paused THEN 'paused'
                   ELSE 'late'
                 END AS reason
            FROM post_intents i
            JOIN workspaces w ON w.id = i.workspace_id
            LEFT JOIN media_items m ON m.id = i.media_item_id
             AND m.workspace_id = i.workspace_id
            LEFT JOIN ig_accounts a ON a.id = i.ig_account_id
             AND a.workspace_id = i.workspace_id
           WHERE i.state = 'scheduled' AND i.origin = 'planned'
             AND i.schedule_slot_at <= now() AND NOT i.cancel_requested) d
   WHERE d.reason IS NOT NULL
   ORDER BY d.schedule_slot_at LIMIT p_limit
$$;

-- May a new Google account with this verified email create its user? True when
-- the owner admitted the address (signup_admissions) and its admission was not
-- claimed, or a pending, unexpired invitation is addressed to it from an
-- active workspace by someone still its owner or an admin, with an active
-- account; false for NULL. Compares lower() on both sides. Answers one
-- boolean, never which workspace invited the address. SECURITY DEFINER owned
-- by svc_membership with EXECUTE granted to svc_ingress.
CREATE FUNCTION fn_signup_admitted(p_email text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM signup_admissions a WHERE a.email = lower(p_email) AND a.claimed_at IS NULL)
      OR EXISTS (SELECT 1 FROM workspace_invitations i
                   JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'
                   JOIN workspace_members m ON m.workspace_id = i.workspace_id
                                           AND m.user_id = i.invited_by_user_id
                                           AND m.role IN ('owner', 'admin')
                   JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'
                  WHERE lower(i.email) = lower(p_email)
                    AND i.state = 'pending' AND i.expires_at > now())
$$;

-- A person unlinks their own Telegram identity. Removes the user's telegram
-- row in user_identities only while the user keeps another identity; refuses
-- any other provider by raising. Memberships are untouched. p_user is the
-- caller's session user — the caller proves the person, this door trusts it.
-- Outcomes: unlinked, not_linked, last_identity. SECURITY DEFINER owned by
-- svc_membership with EXECUTE granted to svc_ingress.
CREATE FUNCTION fn_identity_unlink(p_user uuid, p_provider text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_deleted int;
BEGIN
  IF p_provider IS DISTINCT FROM 'telegram' THEN
    RAISE EXCEPTION 'only a telegram identity is unlinked here, not %', p_provider
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  DELETE FROM user_identities i
   WHERE i.user_id = p_user AND i.provider = p_provider
     AND EXISTS (SELECT 1 FROM user_identities o
                  WHERE o.user_id = p_user AND o.provider <> p_provider);
  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  IF v_deleted > 0 THEN
    RETURN 'unlinked';
  END IF;
  IF EXISTS (SELECT 1 FROM user_identities i
              WHERE i.user_id = p_user AND i.provider = p_provider) THEN
    RETURN 'last_identity';
  END IF;
  RETURN 'not_linked';
END $$;

-- Outbox rows whose last failure falls in the window, every tenant's included,
-- by class and code; o_alerting_rows ended failed or sit ambiguous. Counts
-- only. Window clamped to [60 s, 24 h]. Owned by svc_maintenance; EXECUTE for
-- svc_ingress and svc_worker.
CREATE FUNCTION fn_health_outbox_failures(p_window_seconds integer)
RETURNS TABLE (o_failure_class text, o_error_code integer, o_rows bigint, o_alerting_rows bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT last_failure_class, last_error_code, count(*),
         count(*) FILTER (WHERE state IN ('failed', 'ambiguous'))
    FROM channel_outbox
   WHERE last_failed_at >= now() - make_interval(secs => LEAST(GREATEST(p_window_seconds, 60), 86400))
   GROUP BY last_failure_class, last_error_code
$$;

-- Outbox rows sent in the window, every tenant's included: the traffic the
-- failure count is read against. Window clamped to [60 s, 24 h]. Owned by
-- svc_maintenance; EXECUTE for svc_ingress and svc_worker.
CREATE FUNCTION fn_health_outbox_sent(p_window_seconds integer)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT count(*) FROM channel_outbox
   WHERE state = 'sent'
     AND updated_at >= now() - make_interval(secs => LEAST(GREATEST(p_window_seconds, 60), 86400))
$$;

-- The activation funnel across every workspace: one row per stage (signed in,
-- workspace created, Instagram connected, folder added, first approval) with
-- how many people who signed up since p_since reached it, each stage counted
-- on its own, and how many stalled there: a later stage missing and nothing
-- new from them in p_stall, counted against the stage before the missing one.
-- A person who owns no active workspace but belongs to someone else's counts
-- as signed in and never as stalled. Counts only: no id, name or email leaves
-- it. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_worker
-- alone, the login the psql escape hatch connects as, and for no API
-- principal.
CREATE FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval DEFAULT interval '72 hours')
RETURNS TABLE (o_ordinal int, o_stage text, o_reached bigint, o_stalled bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH placed AS (
    SELECT o_s2 AS s2, o_s3 AS s3, o_s4 AS s4, o_s5 AS s5, o_first_missing AS first_missing,
           o_last_active < now() - p_stall AS idle
      FROM fn_activation_stages(p_since)
  ), totals AS (
    SELECT count(*) AS r1, count(s2) AS r2, count(s3) AS r3, count(s4) AS r4, count(s5) AS r5,
           count(*) FILTER (WHERE first_missing = 2 AND idle) AS t1,
           count(*) FILTER (WHERE first_missing = 3 AND idle) AS t2,
           count(*) FILTER (WHERE first_missing = 4 AND idle) AS t3,
           count(*) FILTER (WHERE first_missing = 5 AND idle) AS t4
      FROM placed
  )
  SELECT k.ordinal, k.stage, k.reached, k.stalled
    FROM totals t
   CROSS JOIN LATERAL (VALUES (1, 'signed in', t.r1, t.t1),
                              (2, 'workspace created', t.r2, t.t2),
                              (3, 'Instagram connected', t.r3, t.t3),
                              (4, 'folder added', t.r4, t.t4),
                              (5, 'first approval', t.r5, 0::bigint))
         AS k (ordinal, stage, reached, stalled)
   ORDER BY k.ordinal
$$;

-- Each active person who signed up at or after p_since, with the five
-- activation stage timestamps across the active workspaces they own (signed
-- in, first owned workspace, first ig_accounts row, first media_sources row,
-- first approval of a card), the first card they were offered, whether they
-- belong to someone else's workspace, their first missing stage (NULL when
-- they own none but joined a team) and their latest stage. The one definition
-- both doors read: fn_activation_funnel counts it and fn_activation_stalled
-- lists from it. An invoker helper owned by svc_maintenance with EXECUTE
-- revoked from PUBLIC, so only those doors, which run as svc_maintenance, can
-- call it.
CREATE FUNCTION fn_activation_stages(p_since timestamptz)
RETURNS TABLE (o_user_id uuid, o_s1 timestamptz, o_s2 timestamptz, o_s3 timestamptz,
               o_s4 timestamptz, o_s5 timestamptz, o_first_card timestamptz, o_joined boolean,
               o_first_missing int, o_last_active timestamptz)
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
  WITH owned_ws AS (
    SELECT m.user_id, w.id AS workspace_id, w.created_at
      FROM workspace_members m
      JOIN workspaces w ON w.id = m.workspace_id
     WHERE m.role = 'owner' AND w.state = 'active'
  ), stages AS (
    SELECT u.id AS user_id,
           u.created_at AS s1,
           (SELECT min(o.created_at) FROM owned_ws o WHERE o.user_id = u.id) AS s2,
           (SELECT min(a.created_at) FROM owned_ws o
              JOIN ig_accounts a ON a.workspace_id = o.workspace_id
             WHERE o.user_id = u.id) AS s3,
           (SELECT min(src.created_at) FROM owned_ws o
              JOIN media_sources src ON src.workspace_id = o.workspace_id
             WHERE o.user_id = u.id) AS s4,
           (SELECT min(e.created_at) FROM owned_ws o
              JOIN audit_events e ON e.workspace_id = o.workspace_id
             WHERE o.user_id = u.id
               AND e.entity_kind = 'post_intent' AND e.detail IS NULL
               AND e.from_state = 'awaiting_approval'
               AND e.to_state IN ('approved', 'posted')) AS s5,
           (SELECT min(e.created_at) FROM owned_ws o
              JOIN audit_events e ON e.workspace_id = o.workspace_id
             WHERE o.user_id = u.id
               AND e.entity_kind = 'post_intent' AND e.detail IS NULL
               AND e.to_state = 'awaiting_approval') AS first_card,
           EXISTS (SELECT 1 FROM workspace_members m
                     JOIN workspaces w ON w.id = m.workspace_id
                    WHERE m.user_id = u.id AND m.role <> 'owner'
                      AND w.state = 'active') AS joined
      FROM users u
     WHERE u.state = 'active' AND u.created_at >= p_since
  )
  SELECT user_id, s1, s2, s3, s4, s5, first_card, joined,
         CASE WHEN s2 IS NULL AND joined THEN NULL
              WHEN s2 IS NULL THEN 2
              WHEN s3 IS NULL THEN 3
              WHEN s4 IS NULL THEN 4
              WHEN s5 IS NULL THEN 5
         END,
         GREATEST(s1, s2, s3, s4, s5)
    FROM stages
$$;

-- Who is owed the activation nudge: at most p_limit active people who signed
-- up at or after p_since, have an email and were never nudged, whose first
-- missing stage is 3 (Instagram), 4 (a folder) or 5 (a first approval, only
-- once a card was offered), and who have done nothing for p_stall, the first
-- card counting as activity. Most recent stalls first. User ids and a stage
-- only: the sweep reads the address itself as it sets
-- users.activation_nudge_at. A SECURITY DEFINER read owned by svc_maintenance;
-- EXECUTE for svc_worker alone.
CREATE FUNCTION fn_activation_stalled(p_since timestamptz, p_stall interval, p_limit int)
RETURNS TABLE (o_user_id uuid, o_stage int)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT s.o_user_id, s.o_first_missing
    FROM fn_activation_stages(p_since) s
    JOIN users u ON u.id = s.o_user_id
   WHERE u.activation_nudge_at IS NULL
     AND u.primary_email IS NOT NULL
     AND s.o_first_missing IN (3, 4, 5)
     AND (s.o_first_missing < 5 OR s.o_first_card IS NOT NULL)
     AND GREATEST(s.o_last_active, s.o_first_card) < now() - p_stall
   ORDER BY GREATEST(s.o_last_active, s.o_first_card) DESC, s.o_user_id
   LIMIT p_limit
$$;

CREATE FUNCTION trg_intent_person_or_rule() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  approving boolean := OLD.state = 'awaiting_approval' AND NEW.state = 'approved';
  actor text := COALESCE(NULLIF(current_setting('app.actor_kind', true), ''), 'none');
BEGIN
  IF NEW.origin IS DISTINCT FROM OLD.origin THEN
    RAISE EXCEPTION 'post_intent % origin is fixed at birth (% -> %)', OLD.id, OLD.origin, NEW.origin
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.approval_rule IS DISTINCT FROM OLD.approval_rule AND NOT approving THEN
    RAISE EXCEPTION 'post_intent % approval_rule is stamped only by the approval it names', OLD.id
      USING ERRCODE = 'check_violation';
  END IF;
  IF approving AND NEW.approval_rule IS NULL
     AND (actor <> 'user' OR NULLIF(current_setting('app.actor_user_id', true), '') IS NULL) THEN
    RAISE EXCEPTION 'post_intent %: only a person approves it (actor %)', OLD.id, actor
      USING ERRCODE = 'check_violation';
  END IF;
  IF approving AND NEW.approval_rule IS NOT NULL AND actor <> 'system' THEN
    RAISE EXCEPTION 'post_intent %: an automatic approval (%) is the system''s (actor %)',
      OLD.id, NEW.approval_rule, actor USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

-- The invitations an invited person could still accept: email invitations,
-- pending and unexpired, addressed to p_email (lower() on both sides), from an
-- active workspace whose inviter is still its owner or an admin with an active
-- account, in a workspace p_user does not belong to. Newest first, at most
-- p_limit. Answers the id, workspace name, role, inviter and expiry, never the
-- token hash. The caller proves the person. SECURITY DEFINER owned by
-- svc_membership with EXECUTE granted to svc_ingress.
CREATE FUNCTION fn_invitations_for(p_user uuid, p_email text, p_limit integer)
RETURNS TABLE (o_invitation_id uuid, o_workspace_name text, o_role text, o_invited_by uuid,
               o_expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.id, w.name, i.role, i.invited_by_user_id, i.expires_at
    FROM workspace_invitations i
    JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'
    JOIN workspace_members a ON a.workspace_id = i.workspace_id
                            AND a.user_id = i.invited_by_user_id
                            AND a.role IN ('owner', 'admin')
    JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'
   WHERE i.delivery_channel = 'email' AND lower(i.email) = lower(p_email)
     AND i.state = 'pending' AND i.expires_at > now()
     AND NOT EXISTS (SELECT 1 FROM workspace_members m
                      WHERE m.workspace_id = i.workspace_id AND m.user_id = p_user)
   ORDER BY i.created_at DESC, i.id
   LIMIT p_limit
$$;

-- Email me the link again: stores a fresh token hash on one invitation, in
-- place, when it is a pending, unexpired email invitation addressed to p_email
-- (lower() on both sides); true when it did. Every guard is in the UPDATE's
-- own WHERE, so an accept or a revoke that commits first wins. created_at and
-- expires_at are kept. The API calls it only after the email carrying the new
-- link was sent, and only for an invitation fn_invitations_for lists for the
-- caller, so a failed send leaves the old link working. The caller proves the
-- person. SECURITY DEFINER owned by svc_membership with EXECUTE granted to
-- svc_ingress.
CREATE FUNCTION fn_invitation_reissue(p_invitation_id uuid, p_email text, p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH rotated AS (
    UPDATE workspace_invitations i SET token_hash = p_token_hash
     WHERE i.id = p_invitation_id
       AND i.delivery_channel = 'email' AND lower(i.email) = lower(p_email)
       AND i.state = 'pending' AND i.expires_at > now()
    RETURNING i.id
  )
  SELECT EXISTS (SELECT 1 FROM rotated)
$$;

-- A local time in a zone as an instant: the earliest of two readings that
-- reads back as the local time, which is the first occurrence of a repeated
-- hour, or NULL for a time the clocks skip. The zone is used as given. One-off
-- planning (command_executors._INSTANT) and the repeat grid (fn_repeat_times)
-- both read local times through it. Plain and STABLE.
CREATE FUNCTION fn_wall_instant(p_local timestamp, p_tz text)
RETURNS timestamptz LANGUAGE sql STABLE AS $$
  SELECT min(c.v)
    FROM (VALUES (p_local AT TIME ZONE p_tz),
                 ((p_local - interval '1 day') AT TIME ZONE p_tz + interval '24 hours')) c(v)
   WHERE c.v AT TIME ZONE p_tz = p_local
$$;

-- A repeat rule's times in [p_lo, p_hi), or, with p_hi NULL, its times up to
-- two local days past one period after p_lo, a window that always holds the
-- first time at or after p_lo: every p_every days, or every p_every weeks on
-- the ISO weekdays p_days, at the local time p_at in the zone p_tz, the grid
-- anchored on p_anchor's local date. The one definition of the grid:
-- tg_intent_repeat creates the next occurrence with it, and the calendar
-- projects with it. Local times read through fn_wall_instant; a skipped one
-- takes the offset before the jump; a decayed zone degrades to UTC
-- (fn_safe_tz). Plain and STABLE.
CREATE FUNCTION fn_repeat_times(p_unit text, p_every int, p_days smallint[], p_at time,
                                p_anchor timestamptz, p_tz text,
                                p_lo timestamptz, p_hi timestamptz)
RETURNS SETOF timestamptz LANGUAGE sql STABLE AS $$
  WITH b AS MATERIALIZED (
    SELECT z.tz, x.a, x.a - EXTRACT(isodow FROM x.a)::int + 1 AS w0,
           GREATEST((p_lo AT TIME ZONE z.tz)::date - 1, x.a) AS d0,
           COALESCE((p_hi AT TIME ZONE z.tz)::date,
                    (p_lo AT TIME ZONE z.tz)::date
                      + CASE p_unit WHEN 'week' THEN 7 ELSE 1 END * p_every + 1) + 1 AS d1
      FROM (SELECT fn_safe_tz(p_tz) AS tz) z,
           LATERAL (SELECT (p_anchor AT TIME ZONE z.tz)::date AS a) x
  ), t AS MATERIALIZED (
    SELECT COALESCE(fn_wall_instant(dd.d + p_at, b.tz), (dd.d + p_at) AT TIME ZONE b.tz) AS ts
      FROM b, generate_series(0, b.d1 - b.d0) AS g,
           LATERAL (SELECT b.d0 + g AS d) dd
     WHERE p_every >= 1
       AND CASE p_unit
             WHEN 'day' THEN (dd.d - b.a) % p_every = 0
             WHEN 'week' THEN ((dd.d - b.w0) / 7) % p_every = 0
                          AND EXTRACT(isodow FROM dd.d)::smallint = ANY (p_days)
             ELSE false
           END
  )
  SELECT t.ts FROM t WHERE t.ts >= p_lo AND (p_hi IS NULL OR t.ts < p_hi) ORDER BY t.ts
$$;

-- tg_intent_repeat: when a story carrying a repeat rule enters a terminal
-- state other than rejected, insert its next occurrence (a planned story,
-- scheduled at the rule's next time) in the same transaction. No successor
-- when the miss sweep recorded a reason other than late or paused, when the
-- workspace is offboarding, or when the account is not live. After a late or
-- paused miss the next time follows now; otherwise it follows the ended
-- occurrence's time. After a post it is also on a later local day than the
-- post, in the story's zone, so a series posts at most once a local day. The
-- INSERT names its columns, so every other column starts at its default, and
-- approval_mode is written as 'manual'. Total: a failure records repeat_failed
-- and a warning, and the ending stands.
CREATE FUNCTION trg_intent_repeat() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_ws text; v_tz text; v_ref text; v_lo timestamptz; v_day timestamp; v_next timestamptz;
        v_id uuid;
BEGIN
  IF NEW.last_error->>'class' = 'planned_missed'
     AND NEW.last_error->>'message' NOT IN ('late', 'paused') THEN
    RETURN NULL;
  END IF;
  BEGIN
    SELECT w.state, fn_safe_tz(COALESCE(a.tz, w.tz)), a.provider_account_ref
      INTO v_ws, v_tz, v_ref
      FROM workspaces w
      JOIN ig_accounts a ON a.id = NEW.ig_account_id AND a.workspace_id = w.id
                        AND a.state IN ('active', 'reauth_required')
     WHERE w.id = NEW.workspace_id;
    IF NOT FOUND OR v_ws = 'offboarding' THEN
      RETURN NULL;
    END IF;
    v_lo := CASE WHEN NEW.last_error->>'class' = 'planned_missed' THEN now()
                 ELSE NEW.schedule_slot_at END + interval '1 microsecond';
    IF NEW.state = 'posted' THEN
      v_day := (now() AT TIME ZONE v_tz)::date + 1;
      v_lo := GREATEST(v_lo, COALESCE(fn_wall_instant(v_day, v_tz), v_day AT TIME ZONE v_tz));
    END IF;
    SELECT min(t) INTO v_next
      FROM fn_repeat_times(NEW.repeat_unit, NEW.repeat_every, NEW.repeat_days, NEW.repeat_at,
                           NEW.schedule_slot_at, v_tz, v_lo, NULL) AS t;
    INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id, provider_account_ref,
                              approval_mode, schedule_slot_at, state, origin,
                              scheduled_by_user_id, repeat_unit, repeat_every, repeat_days,
                              repeat_at)
    VALUES (NEW.workspace_id, NEW.ig_account_id, NEW.media_item_id, v_ref,
            'manual', v_next, 'scheduled', 'planned',
            NEW.scheduled_by_user_id, NEW.repeat_unit, NEW.repeat_every, NEW.repeat_days,
            NEW.repeat_at)
    RETURNING id INTO v_id;
    INSERT INTO audit_events (workspace_id, entity_kind, entity_id, from_state, to_state,
                              actor_kind, actor_user_id, channel, detail)
    VALUES (NEW.workspace_id, 'post_intent', v_id, NULL, 'scheduled',
            current_setting('app.actor_kind'),
            NULLIF(current_setting('app.actor_user_id', true), '')::uuid,
            NULLIF(current_setting('app.channel', true), ''),
            jsonb_build_object('v', 1, 'event', 'repeated', 'follows', NEW.id, 'at', v_next));
  EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'post_intent %: its next occurrence was not created (%)', NEW.id, SQLSTATE;
    BEGIN
      INSERT INTO audit_events (workspace_id, entity_kind, entity_id, from_state, to_state,
                                actor_kind, actor_user_id, channel, detail)
      VALUES (NEW.workspace_id, 'post_intent', NEW.id, NEW.state, NEW.state,
              COALESCE(NULLIF(current_setting('app.actor_kind', true), ''), 'system'),
              NULLIF(current_setting('app.actor_user_id', true), '')::uuid,
              NULLIF(current_setting('app.channel', true), ''),
              jsonb_build_object('v', 1, 'event', 'repeat_failed', 'sqlstate', SQLSTATE));
    EXCEPTION WHEN OTHERS THEN
      NULL;
    END;
  END;
  RETURN NULL;
END $$;

-- The owner's waitlist: every waitlist entry, and every admission that has
-- none, newest first by when it joined or, off the list, when it was admitted,
-- at most p_limit rows. Each row is the address, when it joined (NULL off the
-- list), when it was admitted and emailed, and whether it signed up (a claimed
-- admission, or an account holding the address). With p_ref, only the row
-- whose address's SHA-256 hex starts with p_ref's 32 characters. Never a claim
-- token's hash. SECURITY DEFINER owned by svc_membership with EXECUTE granted
-- to svc_ingress.
CREATE FUNCTION fn_waitlist(p_limit integer, p_ref text DEFAULT NULL)
RETURNS TABLE (o_email text, o_joined_at timestamptz, o_admitted_at timestamptz,
               o_emailed_at timestamptz, o_signed_up boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT email, w.joined_at, a.admitted_at, a.emailed_at,
         a.claimed_at IS NOT NULL
           OR EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = email)
    FROM waitlist_entries w FULL JOIN signup_admissions a USING (email)
   WHERE p_ref IS NULL
      OR left(encode(sha256(convert_to(email, 'UTF8')), 'hex'), 32) = p_ref
   ORDER BY COALESCE(w.joined_at, a.admitted_at) DESC, email
   LIMIT GREATEST(p_limit, 0)
$$;

-- Admit an address, on the waitlist or not: signed_up when an account holds it
-- or its admission was claimed, writing nothing; otherwise admitted, or
-- admitted_before when it was already admitted. The table's CHECK is the
-- authority on what an address is, and its refusal reaches the caller.
-- SECURITY DEFINER owned by svc_membership with EXECUTE granted to
-- svc_ingress.
CREATE FUNCTION fn_signup_admit(p_email text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = lower(p_email))
     OR EXISTS (SELECT 1 FROM signup_admissions a
                 WHERE a.email = lower(p_email) AND a.claimed_at IS NOT NULL) THEN
    RETURN 'signed_up';
  END IF;
  INSERT INTO signup_admissions (email) VALUES (lower(p_email)) ON CONFLICT (email) DO NOTHING;
  IF FOUND THEN
    RETURN 'admitted';
  END IF;
  RETURN 'admitted_before';
END $$;

-- Store the claim link an admission email carried: its token's hash and
-- emailed_at together, replacing an earlier link, on an admission not yet
-- claimed whose address no account holds; true when it did. The worker calls
-- it only after the provider accepted the email, so a failed send leaves the
-- last link working. SECURITY DEFINER owned by svc_membership with EXECUTE
-- granted to svc_worker.
CREATE FUNCTION fn_signup_link(p_email text, p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH linked AS (
    UPDATE signup_admissions a SET claim_token_hash = p_token_hash, emailed_at = now()
     WHERE a.email = lower(p_email) AND a.claimed_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = a.email)
    RETURNING a.email
  )
  SELECT EXISTS (SELECT 1 FROM linked)
$$;

-- Spend a claim link: sets claimed_at and clears the hash on the admission
-- whose link it is, while it is unclaimed and no account holds its address;
-- true when it did. Single use, and dead once the address has an account: one
-- spot, one account. SECURITY DEFINER owned by svc_membership with EXECUTE
-- granted to svc_ingress.
CREATE FUNCTION fn_signup_claim(p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH claimed AS (
    UPDATE signup_admissions a SET claimed_at = now(), claim_token_hash = NULL
     WHERE a.claim_token_hash = p_token_hash AND a.claimed_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM users u WHERE lower(u.primary_email) = a.email)
    RETURNING a.email
  )
  SELECT EXISTS (SELECT 1 FROM claimed)
$$;

-- tg_users_consume_admission: when a user is created with an address, the
-- unconsumed admission at that address, compared with lower(), is used up in
-- the same transaction: claimed_at is stamped and its claim link's hash
-- cleared. One spot, one account, whether the account came through the link or
-- as the address. Birth only: an address changed later consumes nothing. No
-- admission, no change; an error fails the user's creation. SECURITY DEFINER
-- owned by svc_membership, under its grants and policies on signup_admissions.
-- EXECUTE is granted to no role: PostgreSQL asks for it when a trigger is
-- created, never when an INSERT fires one.
CREATE FUNCTION trg_users_consume_admission()
RETURNS trigger
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  UPDATE signup_admissions
     SET claimed_at = now(), claim_token_hash = NULL
   WHERE email = lower(NEW.primary_email) AND claimed_at IS NULL;
  RETURN NEW;
END $$;

CREATE FUNCTION trg_past_posts_generation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.workspace_id, NEW.source_id, NEW.id, NEW.origin_ig_account_id, NEW.credential_id, NEW.authorization_id, NEW.app_id, NEW.subject_id, NEW.authorized_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.source_id, OLD.id, OLD.origin_ig_account_id, OLD.credential_id, OLD.authorization_id, OLD.app_id, OLD.subject_id, OLD.authorized_at) THEN
    RAISE EXCEPTION 'past-posts generation identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

CREATE FUNCTION trg_past_posts_artifact_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE FUNCTION trg_past_posts_operation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW IS DISTINCT FROM OLD THEN
    RAISE EXCEPTION 'past-posts issued operation capture is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

CREATE FUNCTION trg_past_posts_media_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_provider TEXT;
BEGIN
  SELECT provider INTO source_provider FROM media_sources
    WHERE workspace_id = NEW.workspace_id AND id = NEW.source_id;
  IF (source_provider = 'instagram_past_posts') <> (NEW.source_data_generation_id IS NOT NULL OR NEW.source_projection_redacted) THEN
    RAISE EXCEPTION 'past-posts projection requires its authorization generation' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

CREATE FUNCTION trg_past_posts_observation_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.observation_revision < OLD.observation_revision THEN
    RAISE EXCEPTION 'past-posts observation revision cannot go backwards' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

-- Invoker/RLS source and asset verdicts. Internal source_policy context owns
-- DB evaluation time, Python-resolved workspace TTL and activation under
-- canonical retained locks. Drive is workspace-wide. Existing-intent context
-- excludes only its own matching live intent; lock facts do not override base
-- suitability or the database duplicate INSERT arbiter.
CREATE FUNCTION fn_source_asset_suitability(
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

-- Internal current source-read facts only; no item, age, attribution, credit
-- or destination-pool prerequisite. Invoker RLS and explicit tenant predicates
-- remain authoritative. Expiry uses fresh database wall time.
CREATE FUNCTION fn_source_read_authority(p_workspace_id uuid,p_source_id uuid,p_activated boolean)
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

CREATE FUNCTION fn_lock_source_read_prefix(p_ws uuid)
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

CREATE FUNCTION fn_source_read_capture(p_ws uuid,p_sid uuid,p_activated boolean)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('v',1,'workspace_id',p_ws,'source_id',source_id,'provider',provider,
 'source_use_epoch',source_use_epoch,'generation_id',generation_id,'credential_id',credential_id,'account_ref',account_ref)
 FROM fn_source_read_authority(p_ws,p_sid,p_activated) WHERE admitted
$$;

CREATE FUNCTION fn_source_job_read_reason(p_ws uuid,p_payload jsonb,p_activated boolean)
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

CREATE FUNCTION fn_claim_job(p_lane text,p_worker text,p_lease interval,p_ws_lane_cap int,p_past_posts_enabled boolean)
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

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
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

CREATE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval,p_past_posts_enabled boolean)
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

CREATE FUNCTION trg_past_posts_validation_capture_reset() RETURNS trigger
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

CREATE FUNCTION fn_mix_conversion_jobs(p_limit int,p_run_id text,p_run_digest text)
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

CREATE FUNCTION fn_source_asset_suitability(
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

CREATE FUNCTION fn_selected_preparation_due(p_limit integer)
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

CREATE FUNCTION trg_touch_outbox_updated_at() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 NEW.updated_at := clock_timestamp();
 RETURN NEW;
END;
$$;

CREATE FUNCTION trg_meta_mapping_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF (NEW.workspace_id, NEW.ig_account_id, NEW.credential_id, NEW.authorization_id, NEW.app_identity, NEW.subject_ref, NEW.account_ref, NEW.method, NEW.authorized_at, NEW.verified_at, NEW.created_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.ig_account_id, OLD.credential_id, OLD.authorization_id, OLD.app_identity, OLD.subject_ref, OLD.account_ref, OLD.method, OLD.authorized_at, OLD.verified_at, OLD.created_at)
     OR (NEW.is_current AND NOT OLD.is_current) THEN
    RAISE EXCEPTION 'meta identity mapping is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;

-- The verified mappings a Meta deauthorize callback's (app identity, subject)
-- names, across every workspace: the callback carries no tenant, so p_tenant
-- would hide every row from the API's login. An equality on the indexed pair,
-- ordered by workspace and verification time. SECURITY DEFINER owned by
-- svc_maintenance; EXECUTE for svc_ingress.
CREATE FUNCTION fn_meta_subject_mappings(p_app_identity text, p_subject text)
RETURNS TABLE (o_workspace_id uuid, o_ig_account_id uuid, o_credential_id uuid, o_authorization_id uuid,
  o_authorized_at timestamptz, o_verified_at timestamptz, o_is_current boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT workspace_id, ig_account_id, credential_id, authorization_id, authorized_at, verified_at, is_current
    FROM meta_identity_mappings
   WHERE app_identity = p_app_identity AND subject_ref = p_subject
   ORDER BY workspace_id, verified_at, authorization_id
$$;

-- A Meta data-deletion request, tenant-free. A fingerprint any key version
-- matches is a replay: the same receipt and a new capability, no new cleanup.
-- Otherwise a new receipt: per mapped workspace, ascending, under the sources
-- advisory lock, each mapping of (app identity, subject) is classified by its
-- authorization against issued_at with the skew (in_scope, held, or excluded
-- and never frozen), and in_scope and held scopes freeze the non-deleted
-- generations of that authorization on that origin's source. Without a
-- credible issued_at a mapping is in_scope only when it is the account's one
-- mapping for the subject and no earlier receipt scoped the account, and held
-- otherwise, so a newer grant is never reached for want of order evidence.
-- Review reasons: app_unrecognized, ordering_ambiguous. Stores no raw payload,
-- subject or secret. SECURITY DEFINER owned by svc_maintenance; EXECUTE for
-- svc_ingress.
CREATE FUNCTION fn_meta_deletion_receive(p_app_identity text, p_fingerprints text[], p_key_version int,
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

-- The public status of the receipt a capability names: completed or
-- in_progress, nothing else; an unknown capability returns no row. SECURITY
-- DEFINER owned by svc_maintenance; EXECUTE for svc_ingress.
CREATE FUNCTION fn_meta_deletion_status(p_capability_hash text)
RETURNS TABLE (o_state text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE WHEN r.state = 'completed' THEN 'completed' ELSE 'in_progress' END
    FROM meta_deletion_receipt_capabilities c
    JOIN meta_deletion_receipts r ON r.id = c.receipt_id
   WHERE c.capability_hash = p_capability_hash
$$;

-- The claimed tenant's scopes of one receipt, each with its source and the
-- generation ids it froze; no tenant reads nothing. SECURITY DEFINER owned by
-- svc_maintenance; EXECUTE for svc_worker.
CREATE FUNCTION fn_meta_receipt_scope(p_receipt uuid)
RETURNS TABLE (o_ig_account_id uuid, o_credential_id uuid, o_authorization_id uuid, o_disposition text,
  o_state text, o_results jsonb, o_source_id uuid, o_generation_ids uuid[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT s.ig_account_id, s.credential_id, s.authorization_id, s.disposition, s.state, s.results, g.source_id,
         COALESCE(array_agg(g.generation_id ORDER BY g.generation_id) FILTER (WHERE g.generation_id IS NOT NULL), '{}')
    FROM meta_deletion_receipt_scopes s
    LEFT JOIN meta_deletion_receipt_generations g ON g.receipt_id = s.receipt_id AND g.workspace_id = s.workspace_id
     AND g.credential_id = s.credential_id AND g.authorization_id = s.authorization_id
   WHERE s.receipt_id = p_receipt
     AND s.workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
   GROUP BY s.receipt_id, s.workspace_id, s.credential_id, s.authorization_id, g.source_id
   ORDER BY s.ig_account_id, s.authorization_id, g.source_id
$$;

-- Reports one of the claimed tenant's scopes: pending to cleaning or
-- completed, cleaning to cleaning or completed; a held, completed or purged
-- scope refuses by name. Completion is structural and refuses by name
-- (generations_not_deleted, transit_unresolved) unless every generation the
-- scope froze is deleted and no transit upload of them is unresolved. Due
-- again after the retry, clamped to 60..3600 seconds. A transit_unresolved
-- reason moves a pending receipt to needs_review; the receipt completes once
-- every scope is completed or purged, from pending or from needs_review for
-- transit_unresolved alone, and never from any other review reason. Returns
-- the receipt state. SECURITY DEFINER owned by svc_maintenance; EXECUTE for
-- svc_worker.
CREATE FUNCTION fn_meta_receipt_progress(p_receipt uuid, p_credential uuid, p_authorization uuid,
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

-- Deletes the claimed tenant's immutable records of fenced generations:
-- operation provenance, then intent provenance captured or staged in the set,
-- then artifacts. Refuses by name: scope_unavailable (no tenant, an empty set,
-- or a generation not of this source or not deletion_pending/deleted),
-- operation_unresolved (a permitted or ambiguous operation),
-- transit_unresolved (a transit attempt not settled and destroyed),
-- intent_live (a non-terminal intent captured or staged in the set). SECURITY
-- DEFINER owned by svc_maintenance; EXECUTE for svc_worker.
CREATE FUNCTION fn_past_posts_redact_generation_records(p_source uuid, p_generation_ids uuid[])
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

-- The lifecycle sweep's deletion leg: marks scopes of purged workspaces
-- workspace_purged; completes a receipt once every scope is completed or
-- purged only if it is pending without review or its sole review reason is
-- transit_unresolved; other review reasons remain held. Mints at most
-- LEAST(p_limit, 25) meta_deletion_cleanup jobs, one per (receipt, workspace)
-- with due work and no ready or leased job on its key. SECURITY DEFINER owned
-- by svc_clock; EXECUTE for svc_worker.
CREATE FUNCTION fn_meta_receipt_cleanup_jobs(p_limit int)
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

-- The lifecycle sweep's reconciliation leg, called only with activation
-- evidence: at most LEAST(p_limit, 5) reconcile_past_posts_accounts jobs for
-- active, converted workspaces holding an active non-manual account whose
-- active ig_login credential lacks a current mapping or whose account lacks an
-- past-posts source, never twice for one run digest. SECURITY DEFINER owned by
-- svc_clock; EXECUTE for svc_worker.
CREATE FUNCTION fn_past_posts_reconcile_jobs(p_limit int, p_run_id text, p_run_digest text)
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

CREATE FUNCTION trg_instagram_media_assets_validation_reset() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF (NEW.data_generation_id,NEW.observation_revision,NEW.asset_media_id)
 IS DISTINCT FROM (OLD.data_generation_id,OLD.observation_revision,OLD.asset_media_id) THEN
  NEW.validation_capture := NULL;
  NEW.validation_state := 'pending';
  NEW.validation_generation_id := NULL;
  NEW.validation_observation_revision := NULL;
  NEW.validation_fingerprint := NULL;
  NEW.validated_at := NULL;
 END IF;
 RETURN NEW;
END $$;
