-- Migration 081: the fleet-health doors — the seven reads behind /health/scheduling and
-- /health/posting as SECURITY DEFINER functions owned by svc_maintenance (#751; `07` §24).
-- Statements appended to the advertised stream.
--
-- THE GAP, MET IN PRODUCTION. Both surfaces count across every workspace — stalled cursors, active
-- destinations, landings, the debited cap ledger, the ready lanes, the pending outbox — and both
-- modules documented that their reach rested on the owner login's BYPASSRLS, naming #751 as the
-- place a door would have to close it. The first switch of the API to svc_ingress (2026-09-20)
-- proved them right: with no tenant set every policy-covered table read empty, /health/posting
-- answered never-posted for an estate with 104 landings and /health/scheduling no-signal for two
-- active accounts, and the fleet monitors — whose whole design is that a permanent no-signal must
-- not excuse an outage — went blind while nothing else changed. The API was rolled back the same
-- hour; this file is what lets the switch be repeated.
--
-- SEVEN DOORS, ONE PER READ, EACH THE MODULE'S OWN QUERY MOVED VERBATIM. Owned by svc_maintenance,
-- which already holds USING (true) on post_intents, jobs, channel_outbox, daily_post_counts and
-- rate_counters (058) and SELECT on each of them (057); ig_accounts is the one table it lacked, so
-- this file grants that SELECT and that policy. EXECUTE goes to svc_ingress on all seven, and on
-- the three backpressure reads to svc_worker as well (the worker's own status line renders the
-- same snapshot); the four posting and lag reads are the API's alone. The reads a
-- policy already admits without a tenant — the system lane's jobs (workspace_id IS NULL, p_jobs)
-- and the tg_global rate counter (p_rate USING (true)) — stay direct: a door for a read the policy
-- answers would be a second authority. Nothing identifying leaves a door but
-- fn_health_oldest_tenant_wait's workspace id, which the worker's log renders and the
-- unauthenticated surface never does (backpressure.py's `identify` gate, unchanged).
--
-- The CREATE bracket is 062's, for the reason it gave: ALTER FUNCTION … OWNER TO needs the incoming
-- owner to hold CREATE on the schema, and the steady state never leaves it there.
--
-- DEPLOY ORDER: the consumers call the doors unconditionally. Every deploy's predeploy applies this
-- file before the code that reads through it starts — the ordinary order, nothing to schedule.
--
-- Adoption evidence + post-apply verification. Catalog-only: has_*_privilege probes RAISE when their
-- role is absent and the runner treats a raising probe as a hard failure. Four reads, one per
-- structural thing this file does — the seven doors by NAME (a later fn_health_* door must not flip
-- this file's probe: an applied file is immutable) and the EXECUTE rows as a floor a later grant
-- may raise.
-- runner:postcondition SELECT count(*) = 7 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname IN ('fn_health_posting_freshness', 'fn_health_publish_attempts', 'fn_health_destinations', 'fn_health_scheduling_lag', 'fn_health_ready_lanes', 'fn_health_outbox_pending', 'fn_health_oldest_tenant_wait') AND r.rolname = 'svc_maintenance' AND p.prosecdef
-- runner:postcondition SELECT count(*) >= 10 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname IN ('fn_health_posting_freshness', 'fn_health_publish_attempts', 'fn_health_destinations', 'fn_health_scheduling_lag', 'fn_health_ready_lanes', 'fn_health_outbox_pending', 'fn_health_oldest_tenant_wait') AND a.privilege_type = 'EXECUTE' AND g.rolname IN ('svc_ingress', 'svc_worker')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'ig_accounts' AND policyname = 'p_maint_accts')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND r.rolname = 'svc_maintenance' AND a.privilege_type = 'CREATE')

-- [§24 the fleet-health doors: the two health surfaces read the estate through svc_maintenance]
-- /health/scheduling and /health/posting count across every workspace, and both rested on the
-- owner login's BYPASSRLS: under svc_ingress with no tenant set every policy-covered table reads
-- empty, the surfaces answer never-posted / no-signal for a live estate, and the fleet monitors go
-- blind (met in production on 2026-09-20, #751). Seven definer doors, one per read, each the
-- module's own query moved verbatim; owned by svc_maintenance, which held USING (true) on every
-- table but ig_accounts. EXECUTE for svc_ingress on all seven; the three backpressure reads also
-- for svc_worker, whose status line renders the same snapshot — the four posting and lag reads
-- are the API's alone.
-- The CREATE bracket is 062's, for the reason it gave.
GRANT SELECT ON ig_accounts TO svc_maintenance;

CREATE POLICY p_maint_accts ON ig_accounts FOR SELECT TO svc_maintenance USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_health_posting_freshness()
RETURNS TABLE (o_posted_ever bigint, o_last_post_age_seconds numeric, o_intents_ever bigint, o_oldest_intent_age_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*) FILTER (WHERE state = 'posted' AND published_via NOT IN ('legacy_backfill', 'dry_run')),
         min(EXTRACT(EPOCH FROM now() - entered_state_at))
           FILTER (WHERE state = 'posted' AND published_via NOT IN ('legacy_backfill', 'dry_run')),
         count(*),
         max(EXTRACT(EPOCH FROM now() - created_at))
    FROM post_intents
$$;

COMMENT ON FUNCTION fn_health_posting_freshness() IS
  'Landings and their freshness, estate-wide, for /health/posting: confirmed posts (never a legacy_backfill or dry_run row), the age of the freshest, every intent ever and the age of the oldest. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_ingress, the surface''s login. Exists because p_tenant hides every row from a tenant-less read (081, #751).';

ALTER FUNCTION fn_health_posting_freshness() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_posting_freshness() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_posting_freshness() TO svc_ingress;

CREATE FUNCTION fn_health_publish_attempts()
RETURNS TABLE (o_debited_total bigint, o_ledger_days bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT coalesce(sum(count), 0), count(*)
    FROM daily_post_counts
$$;

COMMENT ON FUNCTION fn_health_publish_attempts() IS
  'The cap ledger''s attempt record for /health/posting: what was debited in total and how many (workspace, account, day) buckets ever debited. Owned by svc_maintenance; EXECUTE for svc_ingress (081, #751).';

ALTER FUNCTION fn_health_publish_attempts() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_publish_attempts() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_publish_attempts() TO svc_ingress;

CREATE FUNCTION fn_health_destinations()
RETURNS TABLE (o_accounts_active bigint, o_oldest_active_destination_age_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*), max(EXTRACT(EPOCH FROM now() - created_at))
    FROM ig_accounts WHERE state = 'active'
$$;

COMMENT ON FUNCTION fn_health_destinations() IS
  'The destinations that could receive a post, for /health/posting: a count and the age of the oldest. Owned by svc_maintenance; EXECUTE for svc_ingress (081, #751).';

ALTER FUNCTION fn_health_destinations() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_destinations() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_destinations() TO svc_ingress;

CREATE FUNCTION fn_health_scheduling_lag()
RETURNS TABLE (o_stalled bigint, o_accounts_active bigint, o_max_lag_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*) FILTER (WHERE next_slot_at IS NOT NULL AND next_slot_at <= now()),
         count(*),
         max(EXTRACT(EPOCH FROM now() - next_slot_at))
           FILTER (WHERE next_slot_at IS NOT NULL AND next_slot_at <= now())
    FROM ig_accounts WHERE state = 'active'
$$;

COMMENT ON FUNCTION fn_health_scheduling_lag() IS
  'The cursor lag for /health/scheduling: active accounts whose slot is due and unadvanced, every active account, and the worst lag. Owned by svc_maintenance; EXECUTE for svc_ingress (081, #751).';

ALTER FUNCTION fn_health_scheduling_lag() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_scheduling_lag() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_scheduling_lag() TO svc_ingress;

CREATE FUNCTION fn_health_ready_lanes()
RETURNS TABLE (o_lane text, o_ready bigint, o_oldest_age numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT lane, count(*), EXTRACT(EPOCH FROM max(now() - run_at))
    FROM jobs WHERE state = 'ready' AND run_at <= now()
   GROUP BY lane
$$;

COMMENT ON FUNCTION fn_health_ready_lanes() IS
  'The backpressure signal''s ready lanes: due, unclaimed jobs per lane and the oldest wait, every tenant''s included. Owned by svc_maintenance; EXECUTE for svc_ingress (/health/scheduling) and svc_worker (its own status line) (081, #751).';

ALTER FUNCTION fn_health_ready_lanes() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_ready_lanes() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_ready_lanes() TO svc_ingress, svc_worker;

CREATE FUNCTION fn_health_outbox_pending()
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT count(*) FROM channel_outbox WHERE state = 'pending'
$$;

COMMENT ON FUNCTION fn_health_outbox_pending() IS
  'The backpressure signal''s pending outbox rows, every tenant''s included. Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker (081, #751).';

ALTER FUNCTION fn_health_outbox_pending() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_outbox_pending() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_outbox_pending() TO svc_ingress, svc_worker;

CREATE FUNCTION fn_health_oldest_tenant_wait()
RETURNS TABLE (o_workspace_id uuid, o_wait numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT workspace_id, EXTRACT(EPOCH FROM now() - min(run_at))
    FROM jobs WHERE state = 'ready' AND run_at <= now() AND workspace_id IS NOT NULL
   GROUP BY workspace_id ORDER BY 2 DESC LIMIT 1
$$;

COMMENT ON FUNCTION fn_health_oldest_tenant_wait() IS
  'The backpressure signal''s longest-waiting tenant: the workspace and its wait. The worker''s log names the workspace; the unauthenticated surface never does (backpressure.py''s identify gate). Owned by svc_maintenance; EXECUTE for svc_ingress and svc_worker (081, #751).';

ALTER FUNCTION fn_health_oldest_tenant_wait() OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_health_oldest_tenant_wait() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_health_oldest_tenant_wait() TO svc_ingress, svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
