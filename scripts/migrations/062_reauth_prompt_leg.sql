-- Migration 062: the reauth-prompt clock leg + the marker it keys on (W5e half
-- of the credential lifecycle; `02` §5 :1165 declared the leg, nothing
-- implemented it — reauth_prompt had NO producer anywhere).
--
-- Also the arming index note: `store_credential` now arms `next_refresh_at`
-- at store time (Python side, same change set) — without that, a credential
-- stored at reconnect is invisible to the refresh leg forever, which is the
-- second pin of the silent-death landmine this change set pulls.
--
-- The cadence is `05`'s operational number (reauth-prompt cadence: 1 prompt /
-- account / week) and is deliberately NOT a parameter: the tick's signature
-- stays stable, and the number's home is `05`, not a config knob.

-- Adoption evidence + post-apply verification (#997). Three catalog reads, one
-- per structural thing this file does, so the probe derives from the DDL rather
-- than from a judgment about it. Catalog-only and deliberately so: a
-- has_*_privilege probe RAISES when its role is absent, and the runner treats a
-- raising probe as a hard failure rather than as false -- which on a database
-- that predates the service roles is every probe of that shape.
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'ig_accounts' AND column_name = 'last_reauth_prompt_at'
-- runner:postcondition SELECT count(*) = 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND t.relname = 'ig_accounts' AND c.relname = 'ix_ig_accounts_reauth_due' AND i.indpred IS NOT NULL
-- runner:postcondition SELECT count(*) = 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_clock_tick' AND 'o_reauth_jobs' = ANY(p.proargnames)

ALTER TABLE ig_accounts
  ADD COLUMN last_reauth_prompt_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN ig_accounts.last_reauth_prompt_at IS
  'When the reauth-prompt clock leg last minted a prompt for this account. '
  'NULL = never prompted. Stamped at MINT (not delivery), symmetric with the '
  'refresh leg re-arming next_refresh_at at mint.';

CREATE INDEX ix_ig_accounts_reauth_due
  ON ig_accounts (last_reauth_prompt_at)
  WHERE state = 'reauth_required';

-- 057's matrix gives svc_clock full-table SELECT on ig_accounts and column
-- UPDATE on exactly next_slot_at (:129); the new marker needs the same
-- column-scoped shape:
GRANT UPDATE (last_reauth_prompt_at) ON ig_accounts TO svc_clock;

-- The return shape gains o_reauth_jobs, so this is DROP + CREATE (CREATE OR
-- REPLACE cannot change a RETURNS TABLE shape). Owner/grants re-established
-- below, matching 059's posture exactly — including its TRANSIENT, BRACKETED
-- CREATE grant: ALTER FUNCTION … OWNER TO needs the new owner to hold CREATE
-- on the schema (membership alone fails with "permission denied for schema
-- public" — 059's own words, re-confirmed by this migration failing exactly
-- that way without the bracket). Granted here, revoked below; the
-- steady-state matrix never carries CREATE for a door owner.
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

ALTER FUNCTION fn_clock_tick(int, interval, jsonb) OWNER TO svc_clock;

REVOKE CREATE ON SCHEMA public FROM svc_clock;

REVOKE ALL ON FUNCTION fn_clock_tick(int, interval, jsonb) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_clock_tick(int, interval, jsonb) TO svc_worker;
