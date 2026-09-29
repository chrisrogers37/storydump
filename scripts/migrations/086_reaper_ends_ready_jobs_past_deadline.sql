-- Migration 086: the reaper ends a `ready` job past its deadline when a sweep re-mints its
-- kind (#1429).
--
-- THE GAP. Every mint but publish_pipeline's writes jobs.deadline_at, and 083's header gives
-- the reason: "a job stuck `ready` blocks its own successor for ever ... The deadline is what
-- lets the reaper end it and the next tick re-mint." Nothing did. The deadline was read in
-- one place, jobs.budget_exhausted, which the worker calls only after a run has failed, and
-- fn_reaper_sweep (076) returns expired leases to `ready` without reading a deadline. So a
-- job nothing claimed sat `ready` for ever, holding its serialization key: a key another job
-- held, or a (workspace, key) scope in provider_quarantine, which fn_claim_job skips.
--
-- THE FIX (owner rulings, 2026-09-28). A new leg, the sweep's last, ends a `ready` job past
-- its deadline `failed` and records why in its payload (`ended: deadline`; jobs has no error
-- column, and `payload || jsonb_build_object(...)` is how the outbox records what happened to
-- a row). It does so only for the kinds a sweep re-mints, which is 083's reason for ending
-- one at all:
--   * the clock's recurring singletons (reap_expired, reconcile_ambiguous,
--     alert_stranded_sources, reap_transit_assets) and its slot, refresh and reauth legs
--     (plan_slot, refresh_credential, reauth_prompt);
--   * the sender sweep's deliver_outbox;
--   * the sync kinds (sync_media_source, first_ingest_chunk), whose source the leg re-arms
--     for tomorrow, as `work_loop._rearm_source` does: a sync's mint disarms its source
--     (`next_sync_at = NULL`) and only its run would arm it again.
-- Every other kind is left as it was: publish_pipeline (no deadline: its ceiling is its
-- slot), send_email, offboard_workspace, revoke_workspace_credentials, retention_sweep and
-- reencrypt_credentials. Nothing re-mints them, so ending one would unblock no successor and
-- only lose its work. For the kinds it ends, the leg is the worker's spent-budget path without
-- the tenant notice, which that path calls a courtesy, not the record.
--
-- A deferral carries its deadline (jobs.reschedule_job, since #1429): a park, a pacing wait
-- or a budget deferral moves `deadline_at` with `run_at`, so the job keeps its slack. The
-- deadline test is `<=`, the boundary `budget_exhausted` draws. The outer UPDATE repeats
-- `state` and the deadline test: at READ COMMITTED the recheck of a row that a claim or a
-- deferral changed meanwhile re-evaluates the UPDATE's own quals, not the subquery's. The leg
-- takes the oldest deadlines first, and needs no index of its own: measured at 1-8 ms a sweep
-- for 20k-100k `ready` rows.
--
-- THE GRANT. The re-arm writes media_sources, which svc_maintenance, the door's owner (059),
-- could only read (082). It gains UPDATE on the one column the re-arm writes, column-scoped
-- as 084's grant to svc_clock on jobs, and a policy that admits the row.
--
-- CREATE OR REPLACE, as 076: the signature, the owner and svc_worker's EXECUTE are unchanged.
-- Every other leg is 076's, verbatim, but for the budget line the onboarding leg gains now
-- that a leg follows it.
--
-- Adoption evidence (#997): the leg's text in the function's source; the column grant, read
-- from pg_attribute as 084 reads its own (a probe that must not raise where the role is
-- absent); and the policy, by name.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_reaper_sweep' AND position('deadline_at <= now()' IN p.prosrc) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'media_sources' AND a.attname = 'next_sync_at' AND EXISTS (SELECT 1 FROM aclexplode(a.attacl) x JOIN pg_roles r ON r.oid = x.grantee WHERE r.rolname = 'svc_maintenance' AND x.privilege_type = 'UPDATE'))
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'media_sources' AND policyname = 'p_maint_sources_rearm')

GRANT UPDATE (next_sync_at) ON media_sources TO svc_maintenance;

CREATE POLICY p_maint_sources_rearm ON media_sources FOR UPDATE TO svc_maintenance
  USING (true) WITH CHECK (true);

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
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 086: a `ready` job past its deadline ends `failed`, and says so (#1429),
  -- for the kinds a sweep re-mints; a sync kind's source is re-armed, as
  -- `work_loop._rearm_source` does on a spent budget. Last, so a backlog of
  -- expired jobs cannot take the budget of the legs above.
  WITH ended AS (
    UPDATE jobs SET state = 'failed',
                    payload = payload || jsonb_build_object('ended', 'deadline')
     WHERE id IN (SELECT id FROM jobs
                   WHERE state = 'ready' AND deadline_at <= now()
                     AND kind IN ('reap_expired', 'reconcile_ambiguous',
                                  'alert_stranded_sources', 'reap_transit_assets',
                                  'plan_slot', 'refresh_credential', 'reauth_prompt',
                                  'deliver_outbox', 'sync_media_source',
                                  'first_ingest_chunk')   -- the kinds a sweep re-mints
                   ORDER BY deadline_at LIMIT rem)
       AND state = 'ready' AND deadline_at <= now()   -- rechecked on a changed row
    RETURNING kind, workspace_id, payload->>'source_id' AS source_id
  ), rearmed AS (
    UPDATE media_sources s
       SET next_sync_at = now() + interval '24 hours'           -- work_loop.REARM_AFTER_SECONDS
      FROM ended e
     WHERE e.kind IN ('sync_media_source', 'first_ingest_chunk')  -- work_loop._SYNC_KINDS
       AND s.id::text = e.source_id   -- as text: a malformed payload must not abort the sweep
       AND s.workspace_id = e.workspace_id
       AND s.state = 'active' AND s.next_sync_at IS NULL
  )
  SELECT count(*) INTO c FROM ended;
  n := n + c;
  RETURN n;
END $$;
