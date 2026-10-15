-- Migration 087: the reaper ends a cancel the user asked for (#1235), on 086's sweep.
--
-- THE GAP. `cancel` and `disable_account` set `cancel_requested` and nothing
-- else: the user never writes a waiting story's terminal state, and every
-- `→ cancelled` edge out of a waiting state is the worker's (`02` §4). The one
-- worker that honoured the flag was publish admission, for an `approved` row at
-- its job's next run. A flagged row in `scheduled`, `prompt_pending` or
-- `awaiting_approval` had no worker checkpoint at all, so it stayed live until
-- the approval TTL ended it as `expired` — the wrong label — and while it stayed
-- live it held `uq_intent_live_subject`, refusing that item for that account.
--
-- THE LEG. A flagged row in any of the four waiting states ends `cancelled` at
-- the next sweep. The four edges are seeded (055); the leg draws on the
-- sweep's running remainder like every other leg; the transition is audited
-- under the door's own `reaper` actor. The flag's writers strip the story's
-- cards themselves; a card sent after the flag (the prompt sweep does not read
-- it) is retired by the settled-card sweep that follows the reap (082), the
-- backstop for every terminal intent.
--
-- ITS PLACE: after the lease leg, because liveness comes first (059), and
-- before the expiry legs, because a flagged row they reached first would end
-- `expired` — the very label #1235 corrects. Its candidates are only flagged
-- rows, each ended once, so a full budget delays the expiry legs and never
-- blocks them. Its scan is bounded to the live set: a waiting state implies
-- uq_intent_live_subject's predicate, so the planner can walk that partial
-- index rather than the terminal history.
--
-- THE RACE. The publish job cancels a flagged `approved` story itself
-- (`publish_pipeline._cancel_in`), so the two can meet on one row. At READ
-- COMMITTED a row changed under the sweep is rechecked against the UPDATE's own
-- quals, not its subquery's, so the outer UPDATE repeats the waiting states, as
-- 086's leg repeats its own. Without that, the sweep would write to a row the
-- job had just cancelled, the terminal freeze would refuse it, and every leg
-- would roll back. The job's side tolerates the other order.
--
-- A ROW CARRYING A DEBIT IS NOT THIS LEG'S. A floating story steps back
-- `publishing → approved` with its cap debit (`cap_consumed_on`, 076), and a
-- cancel owes that debit back FIRST: the terminal freeze (055) refuses every
-- write to a cancelled row, a refund included. The refund is the cap ledger's
-- (`publish_cap.refund_cap`), and the two paths that cancel a debited story
-- already take it before the flip: the story's own job at admission, and the
-- reap executor's stale-approved leg, which runs after this door. Cancelled
-- here, the debit would be stranded: nothing refunds a terminal row (its job
-- finalizes on sight, the stale-approved door lists `approved` rows only), and
-- the freeze would refuse a late refund anyway. `scheduled`, `prompt_pending`
-- and `awaiting_approval` precede the flip that debits, so only `approved` can
-- carry one. The same guard means no provider work is owed either: a transit
-- asset is written only while `publishing`, which is always debited
-- (ck_publishing_debited), and resolve-retry clears both together.
--
-- The rest of fn_reaper_sweep is 086's, carried forward byte-for-byte: 076's six
-- legs, their order and the shared budget, and 086's deadline leg (#1429), still
-- the sweep's last, so a backlog of expired jobs cannot take this leg's budget
-- either. CREATE OR REPLACE keeps the owner (svc_maintenance) and the EXECUTE
-- grant (svc_worker); svc_maintenance holds UPDATE on post_intents (057) under
-- p_maint_intents (058).
--
-- Adoption evidence (#997): the delta is opaque plpgsql text, so the probes read
-- the function's source. The first is the column the leg introduces: before this
-- file fn_reaper_sweep never read `cancel_requested`. The second is 086's own
-- probe, repeated as this file's: 086's copy ran once, when 086 applied, so only a
-- postcondition of the file that replaces the function can catch a replacement
-- that drops the deadline leg. Adoption needs every probe true, so at 086 (the
-- second true, the first false) this file still reads pending.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_reaper_sweep' AND position('cancel_requested' IN p.prosrc) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_reaper_sweep' AND position('deadline_at <= now()' IN p.prosrc) > 0)

CREATE OR REPLACE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
   WHERE id IN (SELECT id FROM jobs
                 WHERE state = 'leased' AND locked_until < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  -- 087: a cancel the user asked for ends `cancelled` (#1235), ahead of the
  -- expiry legs; a debited row is left to the paths that refund it first.
  UPDATE post_intents SET state = 'cancelled'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE cancel_requested AND cap_consumed_on IS NULL
                   AND state IN ('scheduled','prompt_pending','awaiting_approval','approved')
                 LIMIT rem)
     AND state IN ('scheduled','prompt_pending','awaiting_approval','approved');  -- rechecked on a changed row
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
