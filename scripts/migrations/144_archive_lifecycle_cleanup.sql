-- 144: deletion cleanup and offboarding obligations: the review hold, verified transit destruction, the reviewed redaction door and the finalize transit guard (#1644).
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'instagram_archive_generations' AND k.conname = 'ck_archive_generation_lifecycle' AND pg_get_constraintdef(k.oid) LIKE '%review_hold%')
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'instagram_artifacts' AND column_name = 'destroyed_at'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_archive_redact_generation_records' AND p.prosecdef AND r.rolname = 'svc_maintenance' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig) AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_offboard_finalize' AND position('transit cleanup obligations' IN p.prosrc) > 0)
-- A held generation is unavailable but not destroyed: admission and suitability already refuse every lifecycle but active.
ALTER TABLE instagram_archive_generations DROP CONSTRAINT ck_archive_generation_lifecycle;
ALTER TABLE instagram_archive_generations ADD CONSTRAINT ck_archive_generation_lifecycle CHECK (lifecycle IN ('active','superseded','review_hold','deletion_pending','deleted'));
-- Destruction is recorded only once a lookup proved the admitted asset absent.
ALTER TABLE instagram_artifacts ADD COLUMN destroyed_at TIMESTAMPTZ NULL;
ALTER TABLE instagram_artifacts ADD CONSTRAINT ck_archive_artifact_destroyed CHECK (admitted_asset_ref IS NULL OR ((cleanup_state = 'destroyed') = (destroyed_at IS NOT NULL)));

-- Provenance and artifact captures are immutable and the runtime logins cannot delete them;
-- the redaction door alone does, once nothing can still use them.
GRANT SELECT, DELETE ON instagram_operation_provenance, instagram_intent_provenance, instagram_artifacts TO svc_maintenance;
CREATE POLICY p_maint_redact_read ON instagram_operation_provenance FOR SELECT TO svc_maintenance USING (true);
CREATE POLICY p_maint_redact_delete ON instagram_operation_provenance FOR DELETE TO svc_maintenance USING (true);
CREATE POLICY p_maint_redact_delete ON instagram_intent_provenance FOR DELETE TO svc_maintenance USING (true);
CREATE POLICY p_maint_redact_read ON instagram_artifacts FOR SELECT TO svc_maintenance USING (true);
CREATE POLICY p_maint_redact_delete ON instagram_artifacts FOR DELETE TO svc_maintenance USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;
CREATE FUNCTION fn_archive_redact_generation_records(p_source uuid, p_generation_ids uuid[])
RETURNS TABLE (o_operation_rows int, o_intent_rows int, o_artifact_rows int)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_ws uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
  v_operations int;
  v_intents int;
  v_artifacts int;
BEGIN
  IF v_ws IS NULL OR p_source IS NULL OR p_generation_ids IS NULL OR cardinality(p_generation_ids) = 0
     OR array_position(p_generation_ids, NULL) IS NOT NULL
     OR (SELECT count(*) FROM instagram_archive_generations g
          WHERE g.workspace_id = v_ws AND g.source_id = p_source AND g.id = ANY (p_generation_ids)
            AND g.lifecycle IN ('deletion_pending','deleted'))
        <> (SELECT count(DISTINCT x) FROM unnest(p_generation_ids) x) THEN
    RAISE EXCEPTION 'archive redaction refused: scope_unavailable' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_operation_provenance p
               JOIN provider_operations o ON o.workspace_id = p.workspace_id AND o.id = p.operation_id
              WHERE p.workspace_id = v_ws AND p.data_generation_id = ANY (p_generation_ids)
                AND o.state IN ('permitted','ambiguous')) THEN
    RAISE EXCEPTION 'archive redaction refused: operation_unresolved' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_artifacts a
              WHERE a.workspace_id = v_ws AND a.data_generation_id = ANY (p_generation_ids) AND a.kind = 'transit'
                AND NOT (a.outcome = 'settled' AND a.cleanup_state = 'destroyed')) THEN
    RAISE EXCEPTION 'archive redaction refused: transit_unresolved' USING ERRCODE = 'P0001';
  END IF;
  IF EXISTS (SELECT 1 FROM instagram_intent_provenance p
               JOIN post_intents i ON i.workspace_id = p.workspace_id AND i.id = p.intent_id
              WHERE p.workspace_id = v_ws
                AND (p.capture_generation_id = ANY (p_generation_ids) OR p.staged_generation_id = ANY (p_generation_ids))
                AND i.state NOT IN ('posted','skipped','rejected','expired','failed','cancelled')) THEN
    RAISE EXCEPTION 'archive redaction refused: intent_live' USING ERRCODE = 'P0001';
  END IF;
  -- Dependency order: both provenance kinds reference the artifacts. A selection-only link stays.
  DELETE FROM instagram_operation_provenance
   WHERE workspace_id = v_ws AND data_generation_id = ANY (p_generation_ids);
  GET DIAGNOSTICS v_operations = ROW_COUNT;
  DELETE FROM instagram_intent_provenance
   WHERE workspace_id = v_ws
     AND (capture_generation_id = ANY (p_generation_ids) OR staged_generation_id = ANY (p_generation_ids));
  GET DIAGNOSTICS v_intents = ROW_COUNT;
  DELETE FROM instagram_artifacts
   WHERE workspace_id = v_ws AND data_generation_id = ANY (p_generation_ids);
  GET DIAGNOSTICS v_artifacts = ROW_COUNT;
  RETURN QUERY SELECT v_operations, v_intents, v_artifacts;
END $$;
COMMENT ON FUNCTION fn_archive_redact_generation_records(uuid, uuid[]) IS
  'Deletes the claimed tenant''s immutable records of fenced generations: operation provenance, then intent provenance captured or staged in the set, then artifacts. Refuses by name: scope_unavailable (no tenant, an empty set, or a generation not of this source or not deletion_pending/deleted), operation_unresolved (a permitted or ambiguous operation), transit_unresolved (a transit attempt not settled and destroyed), intent_live (a non-terminal intent captured or staged in the set). SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_worker (144, #1644).';
ALTER FUNCTION fn_archive_redact_generation_records(uuid, uuid[]) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_archive_redact_generation_records(uuid, uuid[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_archive_redact_generation_records(uuid, uuid[]) TO svc_worker;

-- The workspace's deletion must not orphan an upload nobody can destroy any more.
CREATE OR REPLACE FUNCTION fn_offboard_finalize(p_ws uuid, p_grace interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
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
  IF EXISTS (SELECT 1 FROM instagram_artifacts WHERE workspace_id = p_ws AND kind = 'transit'
              AND (outcome IN ('unsettled','uncertain') OR cleanup_state = 'pending')) THEN
    RAISE EXCEPTION 'workspace % still holds archive transit cleanup obligations', p_ws;
  END IF;
  DELETE FROM workspaces WHERE id = p_ws;
END $$;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
