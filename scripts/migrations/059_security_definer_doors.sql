-- Migration: 059_security_definer_doors.sql
-- Description: F.2.8 — plan 02 §7-DDL's nine SECURITY DEFINER doors, with
--   their owner/revoke/grant cycles and the transient CREATE bracket that
--   installing them requires. NO TABLES, NO INDEXES.
--
--   A CONTIGUOUS PREFIX OF THE ADVERTISED STREAM, and that is the whole
--   contract this file is under. `04` §0.2 arm (b) diffs the concatenated
--   target lineage against the expanded 02+07 stream as an ORDERED PREFIX, so
--   this file is statements 202..240 of that stream in that order, continuing
--   058's 126..201. Nothing here is authored — the body is the plan's own SQL,
--   extracted from the stream rather than transcribed.
--
--   TEN CREATE FUNCTIONs, NINE DOORS, and the difference is not a discrepancy.
--   `fn_next_slot` is a pure `STABLE` helper called by `fn_clock_tick`, not a
--   door: it takes scalars, returns a timestamptz, is not SECURITY DEFINER,
--   and so correctly receives no `ALTER … OWNER TO` and no `GRANT EXECUTE`.
--   Anyone counting `CREATE FUNCTION` to check the split's "nine" will find
--   ten; the nine are the ones carrying the three-statement cycle.
--
--   DOES NOT CROSS INTO `07`, and it is the last increment that does not.
--   Measured: `02-domain-model.md` contributes stream indices 0..240 and
--   `07-security-model.md` begins at 241, so this file ends exactly ONE
--   statement short of the boundary. F.2.9 is the crossing, and the auth
--   plane's shape is different — three tables arriving WITH their RLS and
--   policies, unlike every `02` table increment.
--
--   NO TARGET MODELS, structurally. `CREATE FUNCTION`, `ALTER FUNCTION …
--   OWNER TO`, `REVOKE`, and `GRANT EXECUTE` have no declarative SQLAlchemy
--   representation — `create_all` emits none of them under any model
--   definition — so there is nothing a model could mirror. Zero relations are
--   created; the 23 tables were modelled in F.2.2-F.2.5.
--
--   THE TRANSIENT CREATE BRACKET IS THIS FILE'S SHARPEST DETAIL. `ALTER
--   FUNCTION … OWNER TO` requires the executor to be a member of the new
--   owning role AND the new owner to hold CREATE on the schema — membership
--   alone fails with "permission denied for schema public" (pass 7, R6's P0,
--   empirically forced). So the schema owner grants CREATE, the nine doors are
--   installed and re-owned, and the grant is revoked as this section's LAST
--   statement. The bracket opens and closes inside this one file; a partial
--   application would leave a door-owner role holding CREATE on public, which
--   is exactly what `advertised_ddl_replay` asserts against.
--
--   FOUR OWNERS FOR NINE DOORS: svc_claim, svc_clock, svc_maintenance,
--   svc_membership. Cross-checked against `DOOR_OWNER_ROLES` in
--   `tests/scripts/test_advertised_ddl_replay.py`, which is an independently
--   written list and matches exactly.
--
--   DEPENDS ON 053-058. The door bodies read and write the tables 053-056
--   created, under the RLS 058 enabled; the roles come from the `04`
--   window-bootstrap, not from here.
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public;
--   these objects are created into the empty public it leaves behind. The
--   running application does not see them until the M.3 cutover.
--
-- Rollback: DROP FUNCTION IF EXISTS fn_claim_job, fn_extend_leases,
--   fn_next_slot, fn_clock_tick, fn_reconciler_sweep, fn_reaper_sweep,
--   fn_retention_batch, fn_offboard_finalize, fn_auth_plane_sweep,
--   fn_invitation_accept CASCADE;
--   The EXECUTE grants go with the functions. Nothing needs un-revoking: the
--   CREATE bracket is already closed by this file's own last statement.
-- Created: 2026-08-19
-- Issue: #806
-- EVERY POSTCONDITION BELOW MUST STAY TRUE FOREVER, not merely at the end of
--   this file's run. `migration_runner.py` derives a file's permanent ADOPTION
--   PROBE from its postconditions when the adoption manifest carries no entry,
--   so each line answers two questions: "did this file do its job just now" and
--   "has this file ever been applied to this database".
--
--   ONE OF THESE IS A NEGATIVE, DELIBERATELY, AND IT IS A DIFFERENT CATEGORY
--   FROM THE ONES 053 BANNED. The rule those established is: never assert the
--   absence of state a LATER increment adds, because the probe then reads
--   unapplied once that increment lands. The CREATE-bracket check is not that.
--   It asserts the TERMINAL state THIS FILE ITSELF establishes with its own
--   last statement, and no later increment re-grants CREATE on public to a
--   door owner — `advertised_ddl_replay` asserts the same absence permanently,
--   independently of this file. A door owner holding CREATE afterwards means
--   this migration applied only partially, which is precisely what an adoption
--   probe should catch. Counts use `>=` where F.2.9 may add more.
-- runner:postcondition SELECT count(*) >= 9 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prosecdef
-- runner:postcondition SELECT count(DISTINCT r.rolname) >= 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.prosecdef AND r.rolname IN ('svc_claim','svc_clock','svc_maintenance','svc_membership')
-- runner:postcondition SELECT NOT has_schema_privilege('svc_claim', 'public', 'CREATE') AND NOT has_schema_privilege('svc_clock', 'public', 'CREATE') AND NOT has_schema_privilege('svc_maintenance', 'public', 'CREATE') AND NOT has_schema_privilege('svc_membership', 'public', 'CREATE')
-- runner:postcondition SELECT has_function_privilege('svc_worker', 'fn_claim_job(text, text, interval, int)', 'EXECUTE')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_next_slot' AND NOT p.prosecdef)

-- [§7-DDL nine SECURITY DEFINER doors]
-- TRANSIENT, BRACKETED (pass 7 — R6's P0, empirically forced): ALTER FUNCTION … OWNER TO
-- requires the executor to be a member of the new owning role (bootstrap grants svc_migration
-- those memberships for the window, 04) AND the new owner to hold CREATE on the schema —
-- membership alone fails with "permission denied for schema public". Granted here by the
-- schema's owner, revoked as this section's last statement: the steady-state grant matrix
-- never carries CREATE for a door owner, and the stream is self-cleaning either way.
GRANT CREATE ON SCHEMA public TO svc_claim, svc_clock, svc_maintenance, svc_membership;

-- 1/9 fn_claim_job — the §5 claim, verbatim, as the function it always claimed to be.
CREATE FUNCTION fn_claim_job(p_lane text, p_worker text, p_lease interval, p_ws_lane_cap int)
RETURNS SETOF jobs LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  RETURN QUERY
  WITH candidate AS (
    SELECT j.id FROM jobs j
    WHERE j.state = 'ready' AND j.lane = p_lane AND j.run_at <= now()
      AND NOT EXISTS (SELECT 1 FROM jobs h
                      WHERE h.serialization_key = j.serialization_key AND h.state = 'leased')
      AND NOT EXISTS (SELECT 1 FROM provider_quarantine q
                      WHERE q.workspace_id = j.workspace_id
                        AND q.scope_ref = j.serialization_key
                        AND q.quarantined_until > now())
      AND (j.workspace_id IS NULL OR
           (SELECT count(*) FROM jobs w
            WHERE w.state = 'leased' AND w.lane = j.lane
              AND w.workspace_id = j.workspace_id) < p_ws_lane_cap)
    ORDER BY j.run_at
    LIMIT 1 FOR UPDATE OF j SKIP LOCKED
  )
  UPDATE jobs SET state = 'leased', locked_by = p_worker, lease_token = gen_random_uuid(),
                  locked_until = now() + p_lease, attempts = attempts + 1
    FROM candidate WHERE jobs.id = candidate.id
  RETURNING jobs.*;
END $$;

ALTER FUNCTION fn_claim_job(text, text, interval, int) OWNER TO svc_claim;

REVOKE ALL ON FUNCTION fn_claim_job(text, text, interval, int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_claim_job(text, text, interval, int) TO svc_worker;

-- 2/9 fn_extend_leases — the heartbeat. Extends only live leases matching the caller's tokens;
-- an expired or reassigned lease is silently not extended (the count return lets the worker see it).
CREATE FUNCTION fn_extend_leases(p_tokens uuid[], p_lease interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int;
BEGIN
  UPDATE jobs SET locked_until = now() + p_lease
   WHERE lease_token = ANY (p_tokens) AND state = 'leased' AND locked_until > now();
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

ALTER FUNCTION fn_extend_leases(uuid[], interval) OWNER TO svc_claim;

REVOKE ALL ON FUNCTION fn_extend_leases(uuid[], interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_extend_leases(uuid[], interval) TO svc_worker;

-- Slot arithmetic (the tick's helper; v1 policy = uniform spacing inside the account-local
-- posting window, the legacy create-schedule semantics carried forward; start = end ⇒ 24 h window,
-- start > end ⇒ the window wraps midnight, both current semantics).
-- REWRITTEN AT PASS 6 (R5: 7,344 of the 28,800 legal (start,end,ppd) combinations produced an
-- extra boundary slot — reproduced exactly). The defect class: accumulate a fractional interval,
-- then test the boundary on hour+minute — microsecond rounding lands 03:59:59.999998 "inside" a
-- window that ends 04:00. The rewrite makes the per-cycle count structural: slots are computed
-- BY INDEX on the cycle grid (slot i = cycle_start + i·window/ppd, i ∈ [0, ppd)), so a cycle
-- holds exactly ppd slots by integer arithmetic and no time-precision boundary test exists to
-- get wrong; rounding cannot accumulate because every slot derives from cycle_start, not from
-- its predecessor. round() (not floor) recovers the index: a slot re-read from µs-rounded
-- storage sits within ±1 µs of its grid point and nearest-index snapping absorbs both
-- directions — floor would step a µs-early value back one slot and re-mint it immediately,
-- 1 µs apart, which uq_intent_slot (exact-timestamp key) would NOT dedup.
-- STABLE, not IMMUTABLE: the result depends on the tz database via AT TIME ZONE.
CREATE FUNCTION fn_next_slot(p_after timestamptz, p_tz text,
                             p_start_h int, p_end_h int, p_ppd int)
RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
DECLARE
  tz         text      := fn_safe_tz(p_tz);     -- §0 tz rule: a decayed stored zone degrades
                                                -- this row to UTC, never aborts the caller's
                                                -- set-based tick statement (R5 regression note)
  win_hours  numeric   := CASE WHEN p_end_h = p_start_h THEN 24
                               ELSE ((p_end_h - p_start_h + 24) % 24) END;
  aft_local  timestamp := p_after AT TIME ZONE tz;
  anchor     timestamp := date_trunc('day', aft_local) + p_start_h * interval '1 hour';
  cyc_start  timestamp;                         -- latest window start at or before p_after
  idx        int;
BEGIN
  cyc_start := CASE WHEN aft_local >= anchor THEN anchor ELSE anchor - interval '1 day' END;
  idx := round(EXTRACT(epoch FROM (aft_local - cyc_start)) * p_ppd / (win_hours * 3600.0));
  IF idx + 1 >= p_ppd THEN
    RETURN (cyc_start + interval '1 day') AT TIME ZONE tz;        -- next cycle's first slot
  END IF;
  RETURN (cyc_start + ((idx + 1) * win_hours / p_ppd) * interval '1 hour') AT TIME ZONE tz;
END $$;

-- 3/9 fn_clock_tick — one indexed pass per concern; every heavy step becomes a job. The tick
-- INSERTS plan_slot jobs (never intents — §5 registry, pass 5), schedules refreshes off the
-- payload-free column grant, schedules due syncs, and keeps the recurring system singletons alive.
-- BUDGET IS STRUCTURAL (pass 6 — R5: four independent LIMIT p_max legs permitted 3×p_max +
-- recurring inserts against 05's "≤ 500 inserts/tick" promise): p_max is the tick's TOTAL
-- insert budget, enforced per the §7 door bound rule — one running remainder, every leg
-- LIMITed by it, so the sum cannot exceed p_max under any configuration. Priority order —
-- recurring singletons (they keep the reconciler/reapers/retention alive and are bounded by
-- the registry's kind count), then plan_slot (the product's visible output), then credential
-- refreshes, then baseline syncs (both deferrable-by-ticks at their 05 cadences). A starved
-- class drains on later ticks (the 05 tick cadence). This comment is the priority order's one
-- home; the 05 rows cite it.
CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int, o_recurring_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0;
BEGIN
  PERFORM set_config('app.actor_kind', 'clock', true);
  -- (1) recurring system singletons: if no ready/leased row holds the kind's singleton key,
  -- insert the next run at last-completion + cadence (or now, whichever is later):
  FOR k, cadence IN
    SELECT key, (value::text)::numeric * interval '1 second'
      FROM jsonb_each(p_recurring) WHERE key <> 'v'
  LOOP
    EXIT WHEN n4 >= p_max;
    IF NOT EXISTS (SELECT 1 FROM jobs
                   WHERE kind = k AND state IN ('ready','leased')) THEN
      SELECT max(updated_at) INTO last_done FROM jobs
       WHERE kind = k AND state = 'succeeded';
      INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
      VALUES (k, NULL, 'bulk', k,            -- system singletons key on their kind (§5 registry)
              GREATEST(now(), COALESCE(last_done + cadence, now())), 3,
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
     WHERE a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'plan_slot', d.workspace_id, 'bulk', 'acct:' || d.id, now(), 3,
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
     WHERE state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'refresh_credential', d.workspace_id, 'bulk', 'cred:' || d.id, now(), 5,
           jsonb_build_object('v', 1, 'credential_id', d.id)
      FROM due d
  )
  UPDATE oauth_credentials c SET next_refresh_at = now() + p_refresh_cadence
    FROM due d WHERE c.id = d.id;
  GET DIAGNOSTICS n2 = ROW_COUNT;
  rem := rem - n2;
  -- (4) due source syncs — same shape (H4's slow jittered baseline; pre-slot/demand syncs are
  -- produced by their own sites — the tick owns only the baseline). ix_sources_sync_due serves it:
  WITH due AS (
    SELECT id, workspace_id FROM media_sources
     WHERE state = 'active' AND next_sync_at IS NOT NULL AND next_sync_at <= now()
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'sync_media_source', d.workspace_id, 'bulk', 'src:' || d.id, now(), 5,
           jsonb_build_object('v', 1, 'source_id', d.id, 'reason', 'baseline')
      FROM due d
  )
  UPDATE media_sources s SET next_sync_at = NULL                   -- the sync executor re-arms it
    FROM due d WHERE s.id = d.id;
  GET DIAGNOSTICS n3 = ROW_COUNT;
  RETURN QUERY SELECT n1, n2, n3, n4;
END $$;

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;

-- 4/9 fn_reconciler_sweep — the cross-tenant READ half of reconciliation (§6): returns the
-- ladder-due ambiguous intents plus the parked review_required rows whose customer-notification
-- window has passed; every verdict/notification WRITE then runs tenant-scoped as svc_worker.
-- BUDGET IS STRUCTURAL (pass 6 — R5: LIMIT p_lim on each UNION branch permitted 2×p_lim rows
-- against 05's "LIMIT 50" promise): p_lim bounds the whole sweep; ladder-due rows take
-- priority — an unresolved ambiguity blocks its real account's next publish via
-- uq_publish_exclusive, while a notify-window row is informational — and notify-window rows
-- fill only the remaining budget.
CREATE FUNCTION fn_reconciler_sweep(p_lim int, p_rungs interval[], p_notify_after interval)
RETURNS TABLE (o_intent_id uuid, o_workspace_id uuid, o_reason text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
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

ALTER FUNCTION fn_reconciler_sweep(int, interval[], interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_reconciler_sweep(int, interval[], interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reconciler_sweep(int, interval[], interval) TO svc_worker;

-- 5/9 fn_reaper_sweep — the tenant-table expiry classes, one bounded pass per class. Intent
-- expiry TTLs: slot lapse for scheduled/prompt_pending (the slot passed unclaimed/undelivered),
-- the workspace's approval TTL (else p_approval_ttl) for awaiting_approval, p_approved_ttl for
-- stale approvals. Every intent flip fires the §4 guard + audit triggers as this owner.
-- BUDGET IS STRUCTURAL (pass 7 — R6: seven independent LIMIT p_lim legs permitted 7×p_lim
-- against the §7 bound rule's TOTAL default): p_lim is the sweep's TOTAL row budget — one
-- running remainder, every leg's LIMIT draws it down, sum cannot exceed p_lim (the `05` row).
-- Priority order is liveness-first: the jobs expired-lease re-ready leg runs FIRST — a crashed
-- worker's leased jobs are invisible to claiming until re-readied, so this leg gates all lane
-- throughput; every other leg is expiry bookkeeping whose latency only lengthens a TTL. Then
-- intent slot-lapse (a lapsed slot must not fire late), the two intent age TTLs, then the
-- small-table classes. A leg starved at p_lim carries to the next sweep on the recurring
-- reap_expired schedule — residue is latency, never loss.
CREATE FUNCTION fn_reaper_sweep(p_lim int, p_approval_ttl interval, p_approved_ttl interval)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; c int; rem int := GREATEST(p_lim, 0);
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
   WHERE id IN (SELECT id FROM jobs
                 WHERE state = 'leased' AND locked_until < now() LIMIT rem);
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;   -- ix_jobs_lease_expiry
  -- Intent expiry legs ride the §3 partial reap indexes: each scan touches only live working
  -- rows (small at any instant), never the kept-forever terminal ledger. Fixed-TTL predicates
  -- are written sargable (col < now() - ttl); the per-workspace-TTL leg's bound varies per row,
  -- so its cost bound is the partial index itself.
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                 LIMIT rem);                                    -- ix_intents_reap_slot
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents i SET state = 'expired'
   WHERE i.id IN (
     SELECT i2.id FROM post_intents i2 JOIN workspaces w ON w.id = i2.workspace_id
      WHERE i2.state = 'awaiting_approval'
        AND i2.entered_state_at
            < now() - COALESCE(w.approval_ttl_minutes * interval '1 minute', p_approval_ttl)
      LIMIT rem);                                               -- ix_intents_reap_age
  GET DIAGNOSTICS c = ROW_COUNT; n := n + c; rem := rem - c;
  UPDATE post_intents SET state = 'expired'
   WHERE id IN (SELECT id FROM post_intents
                 WHERE state = 'approved' AND entered_state_at < now() - p_approved_ttl
                 LIMIT rem);                                    -- ix_intents_reap_age
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

ALTER FUNCTION fn_reaper_sweep(int, interval, interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_reaper_sweep(int, interval, interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_reaper_sweep(int, interval, interval) TO svc_worker;

-- 6/9 fn_retention_batch — one 05 retention class per call; the audit class exports before it
-- deletes and ABORTS if the export fails (raise propagates, transaction rolls back — 07 §4).
-- Auth-plane classes are NOT here — they sweep through fn_auth_plane_sweep on the same schedule.
CREATE FUNCTION fn_retention_batch(p_class text, p_keep interval, p_batch int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE n int := 0; tbl text;
BEGIN
  PERFORM set_config('app.actor_kind', 'reaper', true);
  IF p_class = 'audit_events' THEN
    -- clock_timestamp(), never now(): now() is transaction-stable, so two batches in one
    -- transaction would mint the same name and collide (R5). Microsecond suffix keeps
    -- same-second batches distinct; the archive_audit age test reads only the first 8 digits.
    tbl := 'audit_export_' || to_char(clock_timestamp(), 'YYYYMMDD_HH24MISS_US');
    EXECUTE format(
      'CREATE TABLE archive.%I AS SELECT * FROM audit_events
        WHERE created_at < now() - %L::interval ORDER BY id LIMIT %s',
      tbl, p_keep, p_batch);                    -- export-or-abort: failure raises out
    EXECUTE format(
      'DELETE FROM audit_events WHERE id IN (SELECT id FROM archive.%I)', tbl);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'jobs_ok' THEN
    DELETE FROM jobs WHERE id IN (SELECT id FROM jobs
      WHERE state IN ('succeeded','cancelled') AND updated_at < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'jobs_bad' THEN
    DELETE FROM jobs WHERE id IN (SELECT id FROM jobs
      WHERE state IN ('failed','review_required') AND updated_at < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'provider_operations' THEN
    DELETE FROM provider_operations WHERE id IN (SELECT id FROM provider_operations
      WHERE state IN ('succeeded','failed') AND updated_at < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'channel_outbox' THEN
    DELETE FROM channel_outbox WHERE id IN (SELECT id FROM channel_outbox
      WHERE state IN ('sent','superseded','failed','ambiguous')
        AND updated_at < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'daily_post_counts' THEN
    DELETE FROM daily_post_counts WHERE (workspace_id, ig_account_id, local_date) IN (
      SELECT workspace_id, ig_account_id, local_date FROM daily_post_counts
       WHERE local_date < (now() - p_keep)::date LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class = 'rate_counters' THEN
    DELETE FROM rate_counters WHERE (scope, key, window_start) IN (
      SELECT scope, key, window_start FROM rate_counters
       WHERE window_start < now() - p_keep LIMIT p_batch);
    GET DIAGNOSTICS n = ROW_COUNT;
  ELSIF p_class IN ('archive_audit', 'archive_snapshots') THEN
    -- Every archive table's name encodes its date (audit_export_<ts>; <table>_pre_cutover_<ymd>
    -- — the 04 snapshot naming rule exists exactly so this one mechanism covers both families),
    -- so age is a name comparison and no second drop mechanism is needed. p_batch bounds the
    -- drops per call like every other class (pass 7 — R6: the loop ignored it), oldest first
    -- so a starved backlog converges from the aged end; relname tiebreak keeps order total.
    FOR tbl IN SELECT c.relname FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
                WHERE ns.nspname = 'archive' AND c.relkind = 'r'
                  AND c.relname LIKE CASE p_class WHEN 'archive_audit' THEN 'audit\_export\_%'
                                                  ELSE '%\_pre\_cutover\_%' END
                  AND substring(c.relname FROM '[0-9]{8}')
                      < to_char(now() - p_keep, 'YYYYMMDD')
                ORDER BY substring(c.relname FROM '[0-9]{8}'), c.relname
                LIMIT p_batch
    LOOP
      EXECUTE format('DROP TABLE archive.%I', tbl); n := n + 1;
    END LOOP;
  ELSE
    RAISE EXCEPTION 'unknown retention class %', p_class;
  END IF;
  RETURN n;
END $$;

ALTER FUNCTION fn_retention_batch(text, interval, int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_retention_batch(text, interval, int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_retention_batch(text, interval, int) TO svc_worker;

-- 7/9 fn_offboard_finalize — the final-deletion leg of 06 §1, guards INSIDE the body: the
-- workspace is offboarding, the grace window (anchored on workspaces.offboarding_at) has
-- elapsed, and zero live intents remain. The §0 cascade does the rest; audit_events survives.
CREATE FUNCTION fn_offboard_finalize(p_ws uuid, p_grace interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
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
  DELETE FROM workspaces WHERE id = p_ws;
END $$;

ALTER FUNCTION fn_offboard_finalize(uuid, interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_offboard_finalize(uuid, interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_offboard_finalize(uuid, interval) TO svc_worker;

-- 8/9 fn_auth_plane_sweep — expiry/retention deletes on the auth-plane trio. svc_maintenance
-- carries exactly the enumerated auth-plane policies this body needs (07 prints the two for its
-- tables; command_dedup's is above) — the R3 §6.7 resolution, kept.
CREATE FUNCTION fn_auth_plane_sweep(p_state_slack interval, p_session_keep interval,
                                    p_dedup_keep interval, p_batch int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
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

ALTER FUNCTION fn_auth_plane_sweep(interval, interval, interval, int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_auth_plane_sweep(interval, interval, interval, int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_auth_plane_sweep(interval, interval, interval, int) TO svc_worker;

-- 9/9 fn_invitation_accept — the pre-membership door (pass 5, R4 finding 7: the acceptor is
-- authenticated but not yet a member; tenant RLS structurally cannot resolve token → workspace
-- for them, and the central membership gate would refuse them). The D33 match fact is computed
-- HERE, from raw inputs — google: the verified id_token email; telegram: the tapping identity's
-- immutable numeric id vs invited_tg_user_id (never the hint column) — so the authorization
-- fact is computed where it is consumed. Caller (ingress) rate-limits via the preauth_ip scope.
CREATE FUNCTION fn_invitation_accept(p_token_hash text, p_user uuid, p_provider text,
                                     p_verified_email text, p_tg_user_id bigint, p_channel text)
RETURNS TABLE (o_workspace_id uuid, o_granted_role text, o_matched boolean)
-- o_ outputs per the §7 output-name rule (R5's reproduction was this door's ON CONFLICT list)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
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
  -- D33 per-provider acceptance constraint, evaluated in-body:
  IF p_provider = 'google' THEN
    IF inv.email IS NOT NULL THEN
      IF lower(p_verified_email) IS DISTINCT FROM lower(inv.email) THEN
        RAISE EXCEPTION 'identity proof mismatch' USING ERRCODE = 'check_violation';
      END IF;
      m := true;
    ELSE m := false;                             -- no comparable proof: recorded skip
    END IF;
  ELSIF p_provider = 'telegram' THEN
    IF inv.invited_tg_user_id IS NOT NULL THEN
      IF p_tg_user_id IS DISTINCT FROM inv.invited_tg_user_id THEN
        RAISE EXCEPTION 'identity proof mismatch' USING ERRCODE = 'check_violation';
      END IF;
      m := true;
    ELSE m := false;                             -- hint-only or bare token: recorded skip (D36)
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
  ON CONFLICT (workspace_id, user_id) DO NOTHING;  -- already a member: invite consumed, the
                                                    -- existing role stands (role changes go
                                                    -- through the 06 §2 gate)
  IF inv.role = 'admin' AND NOT m THEN             -- D36 elevation-pending, same transaction
    SELECT b.id INTO bind FROM channel_bindings b
     WHERE b.workspace_id = inv.workspace_id AND b.state = 'active'
     ORDER BY b.created_at LIMIT 1;
    IF bind IS NOT NULL THEN
      INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload)
      VALUES (inv.workspace_id, bind, 'notification',
              jsonb_build_object('v', 1, 'template', 'elevation_pending',
                                 'invitation_id', inv.id, 'accepted_by', p_user));
    END IF;                                        -- zero-binding workspace: the pending
  END IF;                                          -- elevation is visible on the web surface
  RETURN QUERY SELECT inv.workspace_id, grant_role, m;
END $$;

ALTER FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) OWNER TO svc_membership;

REVOKE ALL ON FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) TO svc_ingress;

-- Close the transient bracket opened before door 1/9: every ownership transfer above is done,
-- and no door owner keeps CREATE. The replay gate asserts this end state (04 0.2).
REVOKE CREATE ON SCHEMA public FROM svc_claim, svc_clock, svc_maintenance, svc_membership;
