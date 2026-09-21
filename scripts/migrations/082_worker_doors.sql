-- Migration 082: the worker's doors — the four tenant-less sweeps' reads as SECURITY DEFINER
-- functions owned by svc_maintenance, so the worker can run as svc_worker (#751 part 2; `07` §25;
-- documentation/planning/2026-09-21-worker-login-doors/00_PLAN.md). Statements appended to the
-- advertised stream.
--
-- MEASURED, NOT ASSUMED. The worker issues no direct DELETE and touches none of the four tables
-- svc_worker cannot see; what blocks its switch is four paths that open a session with an EMPTY
-- tenant and the system actor and then run SQL on policy-covered tables — the outbox sender sweep
-- (work_loop.ensure_sender_jobs), the prompt sweep (prompts.sweep_due_prompts), the stranded-source
-- alert (media_sync.alert_stranded_sources) and the reconciler's container poll (worker._poll_from).
-- Under the owner login they work because the owner bypasses RLS; under svc_worker the sender sweep
-- mints nothing (no card is ever delivered) and the prompt sweep finds nothing (nothing ever asks
-- for approval), and their writes fail p_tenant's WITH CHECK. tests/scripts/test_worker_login_gate.py
-- runs each as svc_worker and expected what the owner gets: 0 == 1 three times and None before this.
--
-- FOUR DOORS, THE READS ONLY. fn_sender_sweep is the worker's INSERT … SELECT verbatim, one
-- statement because its NOT EXISTS live-job check evaluated with the mint is its idempotence.
-- fn_prompts_due, fn_prompts_pending and fn_stranded_sources return rows WITH their workspace ids;
-- the prompting, the transitions and the alerted_at stamp happen per workspace under that
-- workspace's tenant and the system actor, so the ledger's triggers attribute every write as they
-- do a member's own. The reconciler's poll needs no door: its sweep row already names the workspace
-- and the poll claims it before its read. svc_maintenance gains SELECT and USING (true) on the three
-- tables it lacked and INSERT on jobs; EXECUTE for svc_worker on all four.
--
-- The CREATE bracket is 062's, for the reason it gave: ALTER FUNCTION … OWNER TO needs the incoming
-- owner to hold CREATE on the schema, and the steady state never leaves it there.
--
-- DEPLOY ORDER: the consumers call the doors unconditionally; every deploy's predeploy applies this
-- file before the code that reads through it starts. The owner login the worker still connects as
-- holds EXECUTE on every door through its memberships (081's header), so the deploy changes nothing
-- until the switch.
--
-- Adoption evidence + post-apply verification, catalog-only: the four doors by NAME (an applied file
-- is immutable), the EXECUTE rows as a floor, the three policies, the bracket closed.
-- runner:postcondition SELECT count(*) = 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname IN ('fn_sender_sweep', 'fn_prompts_due', 'fn_prompts_pending', 'fn_stranded_sources') AND r.rolname = 'svc_maintenance' AND p.prosecdef
-- runner:postcondition SELECT count(*) >= 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname IN ('fn_sender_sweep', 'fn_prompts_due', 'fn_prompts_pending', 'fn_stranded_sources') AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker'
-- runner:postcondition SELECT count(*) = 3 FROM pg_policies WHERE schemaname = 'public' AND policyname IN ('p_maint_bindings', 'p_maint_media', 'p_maint_sources')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND r.rolname = 'svc_maintenance' AND a.privilege_type = 'CREATE')

-- [§25 the worker's doors: the four tenant-less sweeps read the estate through svc_maintenance]
-- The worker's sender sweep, prompt sweep, stranded-source alert and the reconciler's container
-- poll opened a session with an empty tenant and ran SQL on policy-covered tables — the class that
-- blinded the health surfaces under svc_ingress (#751). Under svc_worker the sender sweep minted
-- nothing and the prompt sweep found nothing; their writes would have been refused. The reads
-- become doors owned by svc_maintenance, which lacked channel_bindings, media_items and
-- media_sources (SELECT) and INSERT on jobs; the writes stay under each workspace's own tenant.
-- The sender sweep is one door because its statement is one statement: the NOT EXISTS live-job
-- check evaluated with the mint is what makes it idempotent. The CREATE bracket is 062's.
GRANT SELECT ON channel_bindings, media_items, media_sources TO svc_maintenance;

GRANT INSERT ON jobs TO svc_maintenance;

CREATE POLICY p_maint_bindings ON channel_bindings FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_media ON media_items FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_sources ON media_sources FOR SELECT TO svc_maintenance USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int)
RETURNS int
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  WITH minted AS (
    INSERT INTO jobs (kind, workspace_id, lane, serialization_key,
                      run_at, max_attempts, deadline_at, payload)
    SELECT 'deliver_outbox', b.workspace_id, 'interactive',
           p_prefix || b.id, now(), p_attempts,
           now() + make_interval(secs => p_deadline),
           jsonb_build_object('v', 1, 'binding_id', b.id)
      FROM channel_bindings b
     WHERE b.state = 'active' AND b.channel LIKE 'telegram%'
       AND (EXISTS (SELECT 1 FROM channel_outbox o
                     WHERE o.binding_id = b.id AND o.state = 'pending')
            OR EXISTS (SELECT 1 FROM channel_outbox o
                        WHERE o.binding_id = b.id AND o.state = 'ambiguous'
                          AND o.updated_at <= now() - make_interval(secs => p_age)))
       AND NOT EXISTS (SELECT 1 FROM jobs j
                        WHERE j.serialization_key = p_prefix || b.id
                          AND j.state IN ('ready', 'leased'))
     LIMIT p_limit
    RETURNING 1)
  SELECT count(*)::int FROM minted
$$;

COMMENT ON FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) IS
  'The outbox sender sweep: one deliver_outbox job per pushable Telegram binding with pending (or aged ambiguous) outbox rows and no live sender job, every workspace''s, minted in the one statement whose NOT EXISTS is its idempotence. The worker''s former statement verbatim — the key prefix, the lane budget and the bound are its parameters, spelled once in Python; the binding predicate is bindings.push_binding_where, pinned to this body by a test. A SECURITY DEFINER door owned by svc_maintenance; EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) TO svc_worker;

CREATE FUNCTION fn_prompts_due(p_limit int)
RETURNS TABLE (o_id uuid, o_state text, o_workspace_id uuid, o_schedule_slot_at timestamptz, o_file_name text, o_media_kind text, o_mime_type varchar, o_source_id uuid, o_provider_file_ref text, o_handle varchar, o_tz text, o_api_publishing_enabled boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.id, i.state, i.workspace_id, i.schedule_slot_at,
         m.file_name, m.media_kind, m.mime_type,
         m.source_id, m.provider_file_ref, a.handle, w.tz,
         w.api_publishing_enabled
    FROM post_intents i
    JOIN workspaces w ON w.id = i.workspace_id
    JOIN media_items m ON m.id = i.media_item_id
     AND m.workspace_id = i.workspace_id
    LEFT JOIN ig_accounts a ON a.id = i.ig_account_id
     AND a.workspace_id = i.workspace_id
   WHERE i.state = 'scheduled' AND i.schedule_slot_at <= now()
     AND w.state = 'active' AND NOT w.is_paused
   ORDER BY i.schedule_slot_at LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_prompts_due(p_limit int) IS
  'The prompt sweep''s due stories across every workspace — the card''s columns for each scheduled intent whose slot has come in an active, unpaused workspace. A SECURITY DEFINER read owned by svc_maintenance; the prompting itself happens per workspace under that workspace''s tenant. EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_prompts_due(p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_prompts_due(p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_prompts_due(p_limit int) TO svc_worker;

CREATE FUNCTION fn_prompts_pending(p_limit int)
RETURNS TABLE (o_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT i.id, i.workspace_id FROM post_intents i
   WHERE i.state = 'prompt_pending'
   LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_prompts_pending(p_limit int) IS
  'The prompt sweep''s second phase across every workspace: intents whose card is out and whose state must advance to awaiting_approval, advanced per workspace under its tenant. Owned by svc_maintenance; EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_prompts_pending(p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_prompts_pending(p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_prompts_pending(p_limit int) TO svc_worker;

CREATE FUNCTION fn_stranded_sources(p_age numeric, p_limit int)
RETURNS TABLE (o_id uuid, o_workspace_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT id, workspace_id FROM media_sources
   WHERE state = 'error'
     AND (alerted_at IS NULL
          OR alerted_at < now() - make_interval(secs => p_age))
   ORDER BY alerted_at NULLS FIRST
   LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_stranded_sources(p_age numeric, p_limit int) IS
  'The stranded-source alert''s selection across every workspace: sources in error not alerted within the window. The alerted_at stamp and the card are written per workspace under its tenant. Owned by svc_maintenance; EXECUTE for svc_worker (082, #751).';

ALTER FUNCTION fn_stranded_sources(p_age numeric, p_limit int) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_stranded_sources(p_age numeric, p_limit int) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_stranded_sources(p_age numeric, p_limit int) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
