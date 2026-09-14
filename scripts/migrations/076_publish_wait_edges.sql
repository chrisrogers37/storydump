-- 076: the float — two legal edges (publishing → approved, approved → review_required), the
-- reaper's approved leg withdrawn from the door, and the listing door the executor's leg reads
-- through (07 §22; plan 03 of the first-fetch investigation, 2026-09-14).
-- Identical to the 07 §22 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT count(*) = 2 FROM post_intent_transitions WHERE (from_state, to_state) IN (('publishing','approved'), ('approved','review_required'))
-- runner:postcondition SELECT prosrc NOT LIKE '%state = ''approved'' AND entered_state_at%' FROM pg_proc WHERE proname = 'fn_reaper_sweep'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_reaper_stale_approved' AND p.prosecdef AND r.rolname = 'svc_maintenance')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_reaper_stale_approved' AND r.rolname = 'svc_worker' AND a.privilege_type = 'EXECUTE')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND r.rolname = 'svc_maintenance' AND a.privilege_type = 'CREATE')

-- The float (plan 03, 2026-09-14): a story that must wait between attempts steps back from
-- `publishing` to `approved`, keeping its progress and its cap debit, so the account's publish
-- slot (key 4) is free while it waits; and a decision the workspace made is never silently
-- undone — a dead job or the reaper's safety net parks an approved story for the workspace's
-- review instead of expiring it. Two edges; the `02` §4 matrix becomes 29.
INSERT INTO post_intent_transitions (from_state, to_state)
VALUES ('publishing', 'approved'), ('approved', 'review_required')
ON CONFLICT DO NOTHING;

-- The reaper's `approved → expired` leg is withdrawn: an approved story past `p_approved_ttl` is
-- parked for review by the reap executor (`scheduler.execute_reap_expired`), which can restate
-- the card and tell the workspace — a SQL door cannot. The parameter stays so the signature, the
-- grant and the executor's call are unchanged. Every other leg is 059's, verbatim.
CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
   WHERE id IN (SELECT id FROM jobs
                 WHERE state = 'leased' AND locked_until < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                 LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM post_locks
   WHERE id IN (SELECT id FROM post_locks
                 WHERE expires_at IS NOT NULL AND expires_at < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE workspace_invitations SET state = 'expired'
   WHERE id IN (SELECT id FROM workspace_invitations
                 WHERE state = 'pending' AND expires_at < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  DELETE FROM onboarding_sessions
   WHERE id IN (SELECT id FROM onboarding_sessions WHERE expires_at < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c;
  RETURN n;
END $$;

-- The listing door for the executor's approved leg. The reap job is a system singleton
-- (`app.tenant_id = ''`), under which a plain SELECT over post_intents matches nothing once the
-- worker runs as svc_worker (`p_tenant`; the reconciler's own lesson, `reconciler.py`). SECURITY
-- DEFINER as svc_maintenance, which reads every row (`p_maint_intents`, `p_maint_ws`); the
-- executor asserts each row's tenant before it parks or cancels it. A paused workspace's stories
-- wait on purpose and are not listed. The fourteenth `02` §7 door. Bracketed as 059 and 062:
-- ALTER FUNCTION … OWNER TO needs the new owner to hold CREATE on the schema, granted here and
-- revoked below; the steady-state matrix never carries CREATE for a door owner.
GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_reaper_stale_approved(p_approved_ttl interval, p_lim int)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.id, i.workspace_id
    FROM post_intents i JOIN workspaces w ON w.id = i.workspace_id
   WHERE i.state = 'approved' AND NOT w.is_paused
     AND i.entered_state_at < now() - p_approved_ttl
   ORDER BY i.entered_state_at
   LIMIT GREATEST(p_lim, 0)
$$;

ALTER FUNCTION fn_reaper_stale_approved(interval, int) OWNER TO svc_maintenance;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;

REVOKE ALL ON FUNCTION fn_reaper_stale_approved(interval, int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reaper_stale_approved(interval, int) TO svc_worker;
