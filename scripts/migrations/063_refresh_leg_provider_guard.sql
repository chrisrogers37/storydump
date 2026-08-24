-- Migration 063: the refresh leg gets a provider guard (#982 prerequisite, #978
-- disclosure). One clause; the rest of fn_clock_tick is carried forward from 062
-- unchanged.
--
-- THE DEFECT IS LATENT, NOT BROKEN — and it goes live on someone else's commit.
-- 062's refresh leg selects due credentials on `state`/`next_refresh_at` with NO
-- provider filter, and `ig_refresh` (credential_lifecycle.py) then builds
-- IG-shaped params against graph.instagram.com unconditionally. Today that is
-- safe BY CONSTRUCTION ONLY: the sole writer into oauth_credentials is
-- `store_credential`, which takes no provider argument and binds the module
-- constant `PROVIDER = "ig_login"` (verified 2026-08-22 — one INSERT site in the
-- whole tree).
--
-- But `ck_credentials_provider` already admits 'gdrive' (054:198), so the row is
-- INSERTABLE the moment a gdrive credential writer lands. An armed gdrive row
-- would have its token posted to Instagram's host, draw a definitive 400, and be
-- wrongly `mark_dead`-ed — both D31 flips, permanent until reconnect. The person
-- who lands that writer must find this ALREADY DONE, which is why it ships ahead
-- of the adapter rather than beside it.
--
-- WHY A NEW FILE RATHER THAN AN EDIT TO 062. The runner keys on SHA256 of file
-- bytes: "an applied file that no longer matches its recorded checksum is a hard
-- failure everywhere: fix forward" (operations/migration-runner.md). Production
-- carries neither fn_clock_tick nor oauth_credentials today, so an edit would be
-- safe HERE — but "safe in the environments I can see" is not the rule, and 062
-- set the precedent by dropping and recreating the function 059 created. Rule and
-- precedent agree.
--
-- The guard makes the coupling EXPLICIT IN SQL and fails closed: a provider whose
-- refresh door does not exist yet is simply never minted, rather than minted and
-- mishandled. Removing the clause is a deliberate act, and
-- tests/scripts/test_w5de_credential_lifecycle.py::
-- TestTheRefreshLegIsProviderGuarded turns it red.

-- ADOPTION EVIDENCE (#997, ruled on #942). This file's SQL delta over 062 is one
-- clause INSIDE the function body, and a plpgsql body is stored by Postgres as
-- opaque text with no parsed catalog form - so there is no semantic surface to
-- probe and a `prosrc` predicate could only ever match a FORM. The comment below
-- is what makes the delta catalog-visible, and it is warranted on its own merits
-- rather than as a probe target: fn_clock_tick is SECURITY DEFINER, owned by
-- svc_clock with EXECUTE granted to svc_worker, and runs the five scheduled legs
-- that produce due work, and it carried no comment while a single nullable
-- timestamp column (062) carries one. Fix the documentation gap; the probe is a
-- beneficiary, not the reason.
--
-- A schema comment outlives everyone who remembers writing it, so this one
-- states only what was checked against the catalog and the source, and claims no
-- exclusivity it does not have: fn_clock_tick is NOT the only writer of `jobs`
-- (two services INSERT directly - media_sync chaining an ingest page, and
-- work_loop re-minting an outbox sender; a naive grep also hits scheduler.py,
-- but that one is a DOCSTRING quoting this very function's recurring leg), and
-- the EXECUTE grant is a grant, not a guarantee that nothing else can call it.
--
-- The probe therefore tests the comment's PRESENCE, never its text. Matching the
-- wording would re-create inside this option the form-matching that disqualified
-- probing `prosrc`, and would make a later reword break adoption silently.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_clock_tick' AND obj_description(p.oid, 'pg_proc') IS NOT NULL)

-- The CREATE bracket is 062's, carried forward for the same reason it gave:
-- `ALTER FUNCTION … OWNER TO` needs the incoming owner to hold CREATE on the
-- schema, and the steady-state grant matrix never leaves CREATE with a door
-- owner. Granted here, revoked below. Dropping this bracket makes the migration
-- fail at the ALTER — 062 records having failed exactly that way.
GRANT CREATE ON SCHEMA public TO svc_clock;

DROP FUNCTION fn_clock_tick(int, interval, jsonb);

CREATE FUNCTION fn_clock_tick(p_max int, p_refresh_cadence interval,
                              p_recurring jsonb)  -- {v:1, "<kind>": seconds, …} (05 seam)
RETURNS TABLE (o_slot_jobs int, o_refresh_jobs int, o_sync_jobs int,
               o_recurring_jobs int, o_reauth_jobs int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE k text; cadence interval; last_done timestamptz; rem int;
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
       AND provider = 'ig_login'   -- 063: see the header. Fails CLOSED for any new provider.
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
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, payload)
    SELECT 'reauth_prompt', d.workspace_id, 'bulk', 'ig:' || d.provider_account_ref, now(), 3,
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
  'own is skipped rather than minted.';

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
