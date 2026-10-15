-- 148: a resolved transit-review receipt completes when offboarding purges its last scope (#1644).
-- runner:postcondition SELECT COALESCE(position('r.state = ''needs_review'' AND r.review_reason = ''transit_unresolved''' in pg_get_functiondef(to_regprocedure('public.fn_meta_receipt_cleanup_jobs(integer)'))) > 0, false)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_meta_receipt_cleanup_jobs' AND p.prosecdef AND r.rolname = 'svc_clock' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig) AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.rolname='svc_clock' WHERE n.nspname='public' AND c.relname='meta_deletion_receipts' AND a.attname='review_reason' AND has_column_privilege(r.oid,c.oid,a.attnum,'UPDATE'))

GRANT UPDATE (review_reason) ON meta_deletion_receipts TO svc_clock;
GRANT CREATE ON SCHEMA public TO svc_clock;
CREATE OR REPLACE FUNCTION fn_meta_receipt_cleanup_jobs(p_limit int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE minted int;
BEGIN
 -- A purged workspace's cascade took every row a scope froze: nothing is left to clean.
 UPDATE meta_deletion_receipt_scopes s SET state = 'workspace_purged'
  WHERE s.state IN ('pending','cleaning','held')
    AND NOT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = s.workspace_id);
 -- Match the progress door: only a transit-only review can complete without
 -- manual scope verification, and only after every scope is completed or purged.
 UPDATE meta_deletion_receipts r
    SET state = 'completed', review_reason = NULL, completed_at = now()
  WHERE (r.state = 'pending' AND r.review_reason IS NULL
         OR r.state = 'needs_review' AND r.review_reason = 'transit_unresolved')
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
  'The lifecycle sweep''s deletion leg: marks scopes of purged workspaces workspace_purged; completes a receipt once every scope is completed or purged only if it is pending without review or its sole review reason is transit_unresolved; other review reasons remain held. Mints at most LEAST(p_limit, 25) meta_deletion_cleanup jobs, one per (receipt, workspace) with due work and no ready or leased job on its key. SECURITY DEFINER owned by svc_clock; EXECUTE for svc_worker (148, #1644).';
ALTER FUNCTION fn_meta_receipt_cleanup_jobs(integer) OWNER TO svc_clock;
REVOKE ALL ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_receipt_cleanup_jobs(integer) TO svc_worker;
REVOKE CREATE ON SCHEMA public FROM svc_clock;
