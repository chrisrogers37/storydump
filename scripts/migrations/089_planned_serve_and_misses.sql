-- Migration 089: planned stories are served on time or missed out loud (content schedule, phase 3;
-- #1413, plan PR #1414).
--
-- A planned story (`origin = 'planned'`, 088) is served by the prompt sweep at its time, not minted
-- and prompted in one transaction as a cadence story is, so the machinery a cadence story relies
-- on would drop one in silence: the reaper expired any due `scheduled` row the instant its time
-- passed, and the prompt sweep served a row whatever its cancel flag, media, account or locks said.
-- This file makes every ending of a planned row either a serve or a miss that says why.
--
-- THE SLOT KEY, CONTRACT HALF. 088 added `uq_intent_slot_cadence` beside the unconditional
-- `uq_intent_slot`; this drops the unconditional key and gives the partial one its name. Conflict
-- inference matches columns and predicate, never the name, so `plan_slot`'s
-- `ON CONFLICT (...) WHERE origin = 'cadence'` resolves as before, and a planned row no longer
-- holds its instant against a cadence mint. MERGE PRECONDITION: 088 deployed AND drained on every
-- worker — a worker still spelling the conflict target without the predicate finds no arbiter in
-- the partial index and raises on every mint (`railway.toml`'s `drainingSeconds` is the overlap).
--
-- THE SERVE DOOR. `fn_prompts_due` skips a cancel-flagged row whatever its origin (the reaper's
-- cancel leg, 087, ends it `cancelled`), and serves a planned row only while it can be served —
-- its media `available`, its account `active` or `reauth_required`, no live `reject`,
-- `unsupported`, `hold` or `seasonal` lock on it (a `skip` or `recent` lock never blocks one; the
-- four blocking kinds are workspace-wide by `ck_locks_recent_scope`, so no account scope is read) —
-- and only within `p_late` of its time. It returns each row's origin and scheduler, which the card
-- names. Its row type changes, so it is dropped and created again. `p_late` DEFAULTS TO NULL, and a
-- NULL window serves no planned row: that is also what keeps the draining worker's one-argument
-- call resolving during the deploy that applies this file, which the drop alone would break.
--
-- THE MISS DOOR. `fn_planned_misses` returns the due, `scheduled`, unflagged planned rows that will
-- not be served: those that cannot be (`item_removed`, `item_unsupported`, `item_locked`,
-- `account_removed`, in that precedence) and those whose window has passed (`paused` when the
-- workspace was not taking posts, else `late`). Its predicate is the serve door's complement, so a
-- due planned row is served, missed, or waiting out a pause inside its window, and nothing else.
-- The worker expires each with its reason and tells the bound chats in the same transaction. It
-- is STRICT: a NULL window lists nothing, so a caller's missing number can never expire a row.
--
-- THE REAPER. `fn_reaper_sweep` is 087's, carried forward byte for byte, but its slot expiry leg
-- takes cadence rows only: a planned row ended there would end in silence. Its other legs — the
-- lease leg, 087's cancel leg, the approval TTL, locks, invitations, onboarding sessions, and
-- 086's deadline leg, still last — are untouched, and so is the text 086's and 087's adoption
-- probes read (`deadline_at <= now()`, `cancel_requested`). CREATE OR REPLACE keeps its owner
-- and grant.
--
-- The two doors are owned by svc_maintenance, EXECUTE for svc_worker, in 082's pattern and inside
-- 062's CREATE bracket.
--
-- Adoption evidence (#997): the partial key under its old name, the two doors by owner and arity,
-- their EXECUTE rows as a floor, the reaper leg's text, and the bracket closed — catalog state this
-- file creates. Each probe reads false, without raising, on a database without these objects.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'post_intents' AND indexname = 'uq_intent_slot' AND indexdef LIKE '%WHERE (origin = ''cadence''::text)')
-- runner:postcondition SELECT count(*) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname IN ('fn_prompts_due', 'fn_planned_misses') AND p.pronargs = 2 AND r.rolname = 'svc_maintenance' AND p.prosecdef
-- runner:postcondition SELECT count(*) >= 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname IN ('fn_prompts_due', 'fn_planned_misses') AND p.pronargs = 2 AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_reaper_sweep' AND position('AND origin = ''cadence''' IN p.prosrc) > 0)
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND r.rolname = 'svc_maintenance' AND a.privilege_type = 'CREATE')

DROP INDEX uq_intent_slot;

ALTER INDEX uq_intent_slot_cadence RENAME TO uq_intent_slot;

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
  -- 089: the slot expiry takes cadence rows only (#1413). A planned row
  -- that is due and unserved ends through the miss door
  -- (fn_planned_misses), which tells the bound chats why; ended here, it
  -- would end in silence.
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                   AND origin = 'cadence'
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

GRANT CREATE ON SCHEMA public TO svc_maintenance;

DROP FUNCTION fn_prompts_due(int);

CREATE FUNCTION fn_prompts_due(p_limit int, p_late interval DEFAULT NULL)
RETURNS TABLE (o_id uuid, o_state text, o_workspace_id uuid, o_schedule_slot_at timestamptz, o_file_name text, o_media_kind text, o_mime_type varchar, o_source_id uuid, o_provider_file_ref text, o_handle varchar, o_tz text, o_api_publishing_enabled boolean, o_origin text, o_scheduled_by_user_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
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
                                 AND l.media_item_id = i.media_item_id
                                 AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                 AND (l.expires_at IS NULL OR l.expires_at > now()))))
   ORDER BY i.schedule_slot_at LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_prompts_due(p_limit int, p_late interval) IS
  'The prompt sweep''s due stories across every workspace — the card''s columns, origin and scheduler for each scheduled, unflagged intent whose time has come in an active, unpaused workspace: every cadence row, and a planned row only while it can be served (its media available, its account active or awaiting reconnection, no live reject, unsupported, hold or seasonal lock) and within p_late of its time. A NULL p_late, the default, serves no planned row. fn_planned_misses lists the complement. A SECURITY DEFINER read owned by svc_maintenance; the prompting happens per workspace under that workspace''s tenant. EXECUTE for svc_worker (082, #751; 089, #1413).';

ALTER FUNCTION fn_prompts_due(p_limit int, p_late interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_prompts_due(p_limit int, p_late interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_prompts_due(p_limit int, p_late interval) TO svc_worker;

CREATE FUNCTION fn_planned_misses(p_limit int, p_late interval)
RETURNS TABLE (o_id uuid, o_workspace_id uuid, o_reason text, o_schedule_slot_at timestamptz, o_file_name text, o_handle varchar, o_tz text, o_scheduled_by_user_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT d.id, d.workspace_id, d.reason, d.schedule_slot_at, d.file_name, d.handle, d.tz,
         d.scheduled_by_user_id
    FROM (SELECT i.id, i.workspace_id, i.schedule_slot_at, i.scheduled_by_user_id,
                 m.file_name, a.handle, w.tz,
                 CASE
                   WHEN m.id IS NULL OR m.state = 'removed' THEN 'item_removed'
                   WHEN m.state = 'unsupported' THEN 'item_unsupported'
                   WHEN EXISTS (SELECT 1 FROM post_locks l
                                 WHERE l.workspace_id = i.workspace_id
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

COMMENT ON FUNCTION fn_planned_misses(p_limit int, p_late interval) IS
  'The planned stories that will not be served, across every workspace: each due, scheduled, unflagged planned intent that cannot be served (item_removed, item_unsupported, item_locked, account_removed, in that precedence) or whose p_late window has passed (paused when its workspace was not taking posts, else late) — the complement of fn_prompts_due''s planned rows. The worker expires each with its reason and tells the bound chats in the same transaction, per workspace under that workspace''s tenant. STRICT: a NULL window lists nothing. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_worker (089, #1413).';

ALTER FUNCTION fn_planned_misses(p_limit int, p_late interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_planned_misses(p_limit int, p_late interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_planned_misses(p_limit int, p_late interval) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
