-- Migration 083: the clock's five legs mint jobs with a deadline (#1381).
--
-- THE SIBLING OF #1361, IN SQL. `jobs.enqueue` has written `deadline_at` since
-- #1288, and `budget_exhausted` (jobs.py) ends a job on EITHER its attempts or
-- its deadline. #1361 found two Python producers that hand-wrote their INSERT
-- and so minted jobs bounded by attempts alone; fn_clock_tick is the third and
-- last, and it mints five kinds that way — the recurring system singletons,
-- due account slots, due credential refreshes, due source syncs and reauth
-- prompts. Measured on main before this file: `deadline_at` appeared nowhere
-- in 063's definition.
--
-- THE DEADLINE IS MEASURED FROM run_at, NOT FROM now(), and that distinction
-- is the whole reason this is a considered change rather than a column added
-- in five places. #1361 paid for it at the offboarding site: a successor
-- minted thirty days before it is due, carrying a deadline anchored at mint,
-- is already spent when it first becomes claimable and dies on its first run.
-- Four of the five legs here mint at now(), where the two instants coincide.
-- The FIRST does not: a system singleton's run_at is its last completion plus
-- its cadence, which for a slow cadence is well ahead of this tick. Anchoring
-- that one at now() would have been the same defect, reintroduced.
--
-- WHY A DEADLINE HELPS RATHER THAN HARMS THE SINGLETONS. Leg 1 mints only when
-- NO ready/leased row holds the kind's key, so a job stuck `ready` blocks its
-- own successor for ever — attempts never advance, because nothing runs it.
-- The deadline is what lets the reaper end it and the next tick re-mint. For
-- legs 2-5 the cursor is advanced at mint (`next_slot_at`, `next_refresh_at`,
-- `next_sync_at`, `last_reauth_prompt_at`), so a job that exhausts its bound
-- is simply a missed beat, as it already is when its attempts run out.
--
-- THE ATTEMPT LITERALS ARE CARRIED FORWARD UNCHANGED, deliberately. Three legs
-- write 3 and two write 5, while LANE_BUDGETS['bulk'][0] is 5. Unifying them
-- would change how many times a failing job is retried, which is a behaviour
-- change nobody has ruled on, and it is not what this file is for. #1381
-- records the question; this file answers only the deadline half of it.
--
-- The six hours are `LANE_BUDGETS['bulk'][1]`, copied because SQL cannot read
-- a Python constant, and pinned to it by `tests/scripts/test_clock_deadlines.py`
-- the way `vocabulary.py`'s CHECK lists are pinned to their migrations.
--
-- The rest of fn_clock_tick is 063's, carried forward byte-for-byte: the
-- provider guard on the refresh leg, the shared p_max budget, the five legs'
-- order and their cursor advances.
--
-- runner:postcondition SELECT (length(prosrc) - length(replace(prosrc, 'deadline_at', ''))) / length('deadline_at') >= 5 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_clock_tick'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_clock_tick' AND r.rolname = 'svc_clock')

-- The CREATE bracket is 062's, carried forward for the reason 063 gave:
-- `ALTER FUNCTION … OWNER TO` needs the incoming owner to hold CREATE on the
-- schema, and the steady-state grant matrix never leaves CREATE with a door
-- owner. Granted here, revoked below.
GRANT CREATE ON SCHEMA public TO svc_clock;

DROP FUNCTION fn_clock_tick(int, interval, jsonb);

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
        due_at timestamptz;   -- 083: the singleton's run_at, named so its
                              -- deadline can be measured FROM it
        n1 int := 0; n2 int := 0; n3 int := 0; n4 int := 0; n5 int := 0;
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
     WHERE a.state = 'active' AND a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()
       AND w.state = 'active' AND NOT w.is_paused
     ORDER BY a.next_slot_at LIMIT rem
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
     WHERE state = 'active' AND next_refresh_at IS NOT NULL AND next_refresh_at <= now()
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
     LIMIT rem
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
  WITH due AS (
    SELECT id, workspace_id FROM media_sources
     WHERE state = 'active' AND next_sync_at IS NOT NULL AND next_sync_at <= now()
     LIMIT rem
  ), ins AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
    SELECT 'sync_media_source', d.workspace_id, 'bulk', 'src:' || d.id, now(), 5, now() + interval '6 hours',
           jsonb_build_object('v', 1, 'source_id', d.id, 'reason', 'baseline')
      FROM due d
  )
  UPDATE media_sources s SET next_sync_at = NULL                   -- the sync executor re-arms it
    FROM due d WHERE s.id = d.id;
  GET DIAGNOSTICS n3 = ROW_COUNT;
  rem := rem - n3;
  -- (5) reauth prompts for accounts sitting reauth_required (`02` §5 :1165; `05`: 1/week).
  -- Marker stamped at MINT, symmetric with legs 2-4's re-arm-at-mint shape; the NOT EXISTS
  -- guards a still-open prompt job so a slow executor cannot pile up prompts for one account.
  -- ix_ig_accounts_reauth_due serves the scan:
  WITH due AS (
    SELECT a.id, a.workspace_id, a.provider_account_ref
      FROM ig_accounts a
     WHERE a.state = 'reauth_required'
       AND (a.last_reauth_prompt_at IS NULL
            OR a.last_reauth_prompt_at <= now() - interval '7 days')
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.kind = 'reauth_prompt'
                          AND j.serialization_key = 'ig:' || a.provider_account_ref
                          AND j.state IN ('ready','leased'))
     LIMIT rem
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

COMMENT ON FUNCTION fn_clock_tick(int, interval, jsonb) IS
  'The scheduled clock tick: a SECURITY DEFINER producer of due work, owned by '
  'svc_clock with EXECUTE granted to svc_worker, and pinned to '
  'search_path = pg_catalog, public. One call runs five legs in order - '
  'recurring system singletons, due account slots, due credential refreshes, '
  'due source syncs, and reauth prompts. The legs share one budget: p_max caps '
  'the first, and each later leg draws only on what the ones before it left, so '
  'one call mints at most p_max rows. It is NOT the only writer of jobs - '
  'application services enqueue directly as well. The refresh leg is scoped to '
  'provider ig_login and fails closed: a provider with no refresh door of its '
  'own is skipped rather than minted. Every leg mints with a deadline measured '
  'from the job''s own run_at, so a job the worker never completes is ended by '
  'the reaper rather than held for ever.';

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
