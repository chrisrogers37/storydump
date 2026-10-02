-- Migration 095: every SECURITY DEFINER function pins its search_path with pg_temp last.
--
-- THE INVARIANT. A SECURITY DEFINER function runs with its owner's rights, so every name it resolves
-- must come from a schema it chose. The doors already pin `search_path`. From this file on, the pinned
-- path is `pg_catalog, public, pg_temp`: pg_temp named, and LAST, as PostgreSQL's documentation asks of
-- definer functions ("Writing SECURITY DEFINER Functions Safely"). Before this file 28 of the 29 doors
-- pinned `pg_catalog, public`, 068's fn_member_remove pinned `public` alone, and none named pg_temp.
--
-- THE FORM. One `ALTER FUNCTION ... SET search_path` per door, written out: the 29 SECURITY DEFINER
-- functions in `public` (signatures by type). No body is restated, so a door that a later file
-- re-creates keeps its own CREATE's path, and the order in which files merge does not matter.
-- An ALTER that cannot be applied aborts the file.
--
-- A door created AFTER this file names the path in its own CREATE. The census in
-- `tests/scripts/test_rls_runtime_harness.py` reads `pg_proc.proconfig` for every definer function in
-- `public` and fails if any one lacks it.
--
-- runner:postcondition SELECT count(*) >= 29 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prosecdef AND p.proconfig @> ARRAY['search_path=pg_catalog, public, pg_temp']

ALTER FUNCTION fn_auth_plane_sweep(interval, interval, interval, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_claim_job(text, text, interval, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_clock_tick(integer, interval, jsonb) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_extend_leases(uuid[], interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_group_member_seen(text, text, uuid) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_destinations() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_oldest_tenant_wait() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_oldest_tenant_wait_named() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_outbox_pending() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_posting_freshness() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_publish_attempts() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_ready_lanes() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_health_scheduling_lag() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_member_remove(uuid, uuid, uuid) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_memberships_for_caller() SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_meta_accounts_for_ref(text) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_offboard_finalize(uuid, interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_planned_misses(integer, interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_prompts_due(integer, interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_prompts_pending(integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_reaper_stale_approved(interval, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_reaper_sweep(integer, interval, interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_reconciler_sweep(integer, interval[], interval) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_resolve_binding(text, text) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_retention_batch(text, interval, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_sender_sweep(text, integer, numeric, numeric, integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_settled_cards(text[], integer) SET search_path = pg_catalog, public, pg_temp;

ALTER FUNCTION fn_stranded_sources(numeric, integer) SET search_path = pg_catalog, public, pg_temp;
