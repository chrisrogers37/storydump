-- 145: the archive lifecycle sweep, deletion cleanup jobs and existing-account reconciliation jobs (#1644).
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'jobs' AND k.conname = 'ck_jobs_kind' AND pg_get_constraintdef(k.oid) LIKE '%reconcile_archive_accounts%')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'jobs' AND k.conname = 'ck_jobs_system_kinds' AND pg_get_constraintdef(k.oid) LIKE '%archive_lifecycle_sweep%')
-- runner:postcondition SELECT count(*) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname IN ('fn_meta_receipt_cleanup_jobs','fn_archive_reconcile_jobs') AND p.prosecdef AND r.rolname = 'svc_clock' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig) AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker'
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_kind;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_kind CHECK (kind IN (
 'plan_slot','publish_pipeline','deliver_outbox','sync_media_source',
 'first_ingest_chunk','refresh_credential','offboard_workspace',
 'revoke_workspace_credentials','reauth_prompt','reconcile_ambiguous',
 'reap_expired','reap_transit_assets','retention_sweep','reencrypt_credentials',
 'send_email','alert_stranded_sources','activation_nudge_sweep','convert_source_mix',
 'archive_lifecycle_sweep','meta_deletion_cleanup','reconcile_archive_accounts'));
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_system_kinds;
ALTER TABLE jobs ADD CONSTRAINT ck_jobs_system_kinds CHECK (
  (workspace_id IS NULL) = (kind IN
    ('reconcile_ambiguous','reap_expired','reap_transit_assets','retention_sweep',
     'reencrypt_credentials','send_email','alert_stranded_sources',
     'activation_nudge_sweep','archive_lifecycle_sweep')));

GRANT CREATE ON SCHEMA public TO svc_clock;
CREATE FUNCTION fn_meta_receipt_cleanup_jobs(p_limit int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE minted int;
BEGIN
 -- A purged workspace's cascade took every row a scope froze: nothing is left to clean.
 UPDATE meta_deletion_receipt_scopes s SET state = 'workspace_purged'
  WHERE s.state IN ('pending','cleaning','held')
    AND NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id);
 UPDATE meta_deletion_receipts r SET state = 'completed', completed_at = now()
  WHERE r.state = 'pending' AND r.review_reason IS NULL
    AND NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes s
                     WHERE s.receipt_id = r.id AND s.state NOT IN ('completed','workspace_purged'));
 -- Due work is a pending or cleaning scope, or a held scope whose frozen generations are not yet fenced.
 WITH due AS (
  SELECT s.receipt_id, s.workspace_id FROM meta_deletion_receipt_scopes s
   WHERE s.next_attempt_at <= now()
     AND (s.state IN ('pending','cleaning')
      OR (s.state = 'held' AND EXISTS (SELECT 1 FROM meta_deletion_receipt_generations x
           JOIN instagram_archive_generations g ON g.workspace_id = x.workspace_id
            AND g.source_id = x.source_id AND g.id = x.generation_id
          WHERE x.receipt_id = s.receipt_id AND x.workspace_id = s.workspace_id
            AND x.credential_id = s.credential_id AND x.authorization_id = s.authorization_id
            AND g.lifecycle IN ('active','superseded'))))
     AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id)
     AND NOT EXISTS (SELECT 1 FROM jobs j
                      WHERE j.serialization_key = 'receipt:' || s.receipt_id || ':' || s.workspace_id
                        AND j.state IN ('ready','leased'))
   GROUP BY s.receipt_id, s.workspace_id
   ORDER BY min(s.next_attempt_at), s.receipt_id, s.workspace_id
   LIMIT LEAST(GREATEST(p_limit, 0), 25)
 )
 INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
 SELECT 'meta_deletion_cleanup', workspace_id, 'bulk', 'receipt:' || receipt_id || ':' || workspace_id, now(), 5,
  now() + interval '6 hours', jsonb_build_object('v', 1, 'receipt_id', receipt_id)
 FROM due;
 GET DIAGNOSTICS minted = ROW_COUNT;
 RETURN minted;
END $$;
COMMENT ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) IS
  'The lifecycle sweep''s deletion leg: marks scopes of purged workspaces workspace_purged, completes pending receipts with nothing left, and mints at most LEAST(p_limit, 25) meta_deletion_cleanup jobs, one per (receipt, workspace) with due work and no ready or leased job on its key. SECURITY DEFINER owned by svc_clock; EXECUTE for svc_worker (145, #1644).';
ALTER FUNCTION fn_meta_receipt_cleanup_jobs(integer) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) TO svc_worker;

CREATE FUNCTION fn_archive_reconcile_jobs(p_limit int, p_run_id text, p_run_digest text)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE minted int;
BEGIN
 IF p_run_id IS NULL OR p_run_id !~ '^[a-zA-Z0-9_.-]{1,128}$'
  OR p_run_digest IS NULL OR p_run_digest !~ '^[0-9a-f]{64}$' THEN RETURN 0; END IF;
 -- Discovery is a hint only; the tenant executor rereads under the canonical locks.
 -- One job per workspace per activation digest: a repeated deploy resets nothing.
 WITH pending AS (
  SELECT w.id FROM workspaces w
  WHERE w.state = 'active'
   AND w.source_mix_converted_at IS NOT NULL AND w.source_mix_revision IS NOT NULL
   AND w.source_mix_mode IN ('equal','custom') AND w.source_membership_policy = 'reset_equal'
   AND EXISTS (SELECT 1 FROM ig_accounts a
     JOIN oauth_credentials c ON c.workspace_id = a.workspace_id AND c.ig_account_id = a.id
    WHERE a.workspace_id = w.id AND a.state = 'active' AND a.provider_account_ref NOT LIKE 'manual:%'
     AND c.provider = 'ig_login' AND c.state = 'active'
     AND (NOT EXISTS (SELECT 1 FROM meta_identity_mappings m
           WHERE m.workspace_id = c.workspace_id AND m.credential_id = c.id AND m.is_current)
      OR NOT EXISTS (SELECT 1 FROM media_sources s
           WHERE s.workspace_id = a.workspace_id AND s.provider = 'instagram_archive'
            AND s.origin_ig_account_id = a.id)))
   AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.workspace_id = w.id
    AND j.kind = 'reconcile_archive_accounts' AND j.payload->>'run_digest' = p_run_digest)
  ORDER BY w.id LIMIT LEAST(GREATEST(p_limit, 0), 5)
 )
 INSERT INTO jobs (kind, workspace_id, lane, serialization_key, run_at, max_attempts, deadline_at, payload)
 SELECT 'reconcile_archive_accounts', id, 'bulk', 'archive-reconcile:' || id, now(), 5, now() + interval '6 hours',
  jsonb_build_object('v', 1, 'run_id', p_run_id, 'run_digest', p_run_digest)
 FROM pending;
 GET DIAGNOSTICS minted = ROW_COUNT;
 RETURN minted;
END $$;
COMMENT ON FUNCTION fn_archive_reconcile_jobs(integer, text, text) IS
  'The lifecycle sweep''s reconciliation leg, called only with activation evidence: at most LEAST(p_limit, 5) reconcile_archive_accounts jobs for active, converted workspaces holding an active non-manual account whose active ig_login credential lacks a current mapping or whose account lacks an archive source, never twice for one run digest. SECURITY DEFINER owned by svc_clock; EXECUTE for svc_worker (145, #1644).';
ALTER FUNCTION fn_archive_reconcile_jobs(integer, text, text) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_archive_reconcile_jobs(integer, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_archive_reconcile_jobs(integer, text, text) TO svc_worker;
REVOKE CREATE ON SCHEMA public FROM svc_clock;
