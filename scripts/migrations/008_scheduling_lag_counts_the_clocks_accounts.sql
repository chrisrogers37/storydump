-- The scheduling-lag door counts only the accounts the clock advances: an
-- active account of an active, unpaused workspace, fn_clock_tick's own
-- population, so a paused workspace's due cursor no longer reads as stalled.
-- Same signature, owner and grants.
-- The runner wraps this file and its ledger row in one transaction.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc WHERE oid = to_regprocedure('public.fn_health_scheduling_lag()') AND prosrc LIKE '%NOT w.is_paused%')

SET LOCAL search_path = public, pg_temp;

CREATE OR REPLACE FUNCTION fn_health_scheduling_lag()
RETURNS TABLE (o_stalled bigint, o_accounts_active bigint, o_max_lag_seconds numeric)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT count(*) FILTER (WHERE a.next_slot_at IS NOT NULL AND a.next_slot_at <= now()),
         count(*),
         max(EXTRACT(EPOCH FROM now() - a.next_slot_at))
           FILTER (WHERE a.next_slot_at IS NOT NULL AND a.next_slot_at <= now())
    FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id
   WHERE a.state = 'active' AND w.state = 'active' AND NOT w.is_paused
$$;
