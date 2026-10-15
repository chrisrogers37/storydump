-- 143: durable Meta deletion receipts: tenant-free episodes, the generations each scope froze, and status capabilities (#1644).
-- runner:postcondition SELECT count(*) = 4 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname IN ('meta_deletion_receipts','meta_deletion_receipt_scopes','meta_deletion_receipt_generations','meta_deletion_receipt_capabilities') AND c.relkind = 'r' AND c.relrowsecurity
-- runner:postcondition SELECT count(*) = 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname IN ('fn_meta_deletion_receive','fn_meta_deletion_status','fn_meta_receipt_scope','fn_meta_receipt_progress') AND p.prosecdef AND r.rolname = 'svc_maintenance' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig)
-- runner:postcondition SELECT count(*) = 4 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND a.privilege_type = 'EXECUTE' AND ((p.proname IN ('fn_meta_deletion_receive','fn_meta_deletion_status') AND g.rolname = 'svc_ingress') OR (p.proname IN ('fn_meta_receipt_scope','fn_meta_receipt_progress') AND g.rolname = 'svc_worker'))
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(a.attacl) x JOIN pg_roles r ON r.oid = x.grantee WHERE n.nspname = 'public' AND c.relname = 'instagram_archive_generations' AND a.attname = 'authorization_id' AND r.rolname = 'svc_maintenance' AND x.privilege_type = 'SELECT')
-- No foreign key to workspaces: a receipt outlives the workspace it scoped, so offboarding never erases the proof.
CREATE TABLE meta_deletion_receipts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  app_identity TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  fingerprint_key_version INTEGER NOT NULL,
  issued_at TIMESTAMPTZ NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  state TEXT NOT NULL DEFAULT 'pending',
  review_reason TEXT NULL,
  completed_at TIMESTAMPTZ NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_meta_receipt_fingerprint UNIQUE (app_identity, request_fingerprint),
  CONSTRAINT ck_meta_receipt_app CHECK (app_identity ~ '^(instagram:[0-9]{1,32}|instagram:unconfigured|facebook:legacy)$'),
  CONSTRAINT ck_meta_receipt_fingerprint CHECK (request_fingerprint ~ '^[0-9a-f]{64}$' AND fingerprint_key_version > 0),
  CONSTRAINT ck_meta_receipt_state CHECK (state IN ('pending','needs_review','completed')),
  CONSTRAINT ck_meta_receipt_review_reason CHECK (review_reason IN ('ordering_ambiguous','mapping_incomplete','app_unrecognized','transit_unresolved')),
  CONSTRAINT ck_meta_receipt_review CHECK ((state = 'needs_review') = (review_reason IS NOT NULL)),
  CONSTRAINT ck_meta_receipt_completed CHECK ((state = 'completed') = (completed_at IS NOT NULL))
);

CREATE TABLE meta_deletion_receipt_scopes (
  receipt_id UUID NOT NULL REFERENCES meta_deletion_receipts(id) ON DELETE RESTRICT,
  workspace_id UUID NOT NULL,
  ig_account_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  authorization_id UUID NOT NULL,
  disposition TEXT NOT NULL,
  state TEXT NOT NULL,
  results JSONB NOT NULL DEFAULT '{}'::jsonb,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (receipt_id, workspace_id, credential_id, authorization_id),
  CONSTRAINT ck_meta_scope_disposition CHECK (disposition IN ('in_scope','held')),
  CONSTRAINT ck_meta_scope_state CHECK (state IN ('pending','cleaning','held','completed','workspace_purged')),
  CONSTRAINT ck_meta_scope_held CHECK (state = 'workspace_purged' OR (disposition = 'held') = (state = 'held')),
  CONSTRAINT ck_meta_scope_results CHECK (jsonb_typeof(results) = 'object' AND octet_length(results::text) <= 4096)
);
CREATE INDEX ix_meta_scope_due ON meta_deletion_receipt_scopes (next_attempt_at) WHERE state IN ('pending','cleaning');

CREATE TABLE meta_deletion_receipt_generations (
  receipt_id UUID NOT NULL,
  workspace_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  authorization_id UUID NOT NULL,
  source_id UUID NOT NULL,
  generation_id UUID NOT NULL,
  PRIMARY KEY (receipt_id, workspace_id, generation_id),
  CONSTRAINT fk_meta_receipt_generation_scope FOREIGN KEY (receipt_id, workspace_id, credential_id, authorization_id) REFERENCES meta_deletion_receipt_scopes(receipt_id, workspace_id, credential_id, authorization_id) ON DELETE RESTRICT
);

CREATE TABLE meta_deletion_receipt_capabilities (
  capability_hash TEXT PRIMARY KEY,
  receipt_id UUID NOT NULL REFERENCES meta_deletion_receipts(id) ON DELETE RESTRICT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_meta_capability_hash CHECK (capability_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX ix_meta_capability_receipt ON meta_deletion_receipt_capabilities (receipt_id);

CREATE TRIGGER tg_touch_meta_deletion_receipts BEFORE UPDATE ON meta_deletion_receipts FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
CREATE TRIGGER tg_touch_meta_deletion_receipt_scopes BEFORE UPDATE ON meta_deletion_receipt_scopes FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

-- The doors' alone: the runtime logins hold no grant on any of the four.
ALTER TABLE meta_deletion_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_deletion_receipt_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_deletion_receipt_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE meta_deletion_receipt_capabilities ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_maint_meta_receipts ON meta_deletion_receipts FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_meta_scopes ON meta_deletion_receipt_scopes FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_meta_generations ON meta_deletion_receipt_generations FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
CREATE POLICY p_maint_meta_capabilities ON meta_deletion_receipt_capabilities FOR ALL TO svc_maintenance USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE ON meta_deletion_receipts, meta_deletion_receipt_scopes TO svc_maintenance;
GRANT SELECT, INSERT ON meta_deletion_receipt_generations, meta_deletion_receipt_capabilities TO svc_maintenance;
-- The clock's cleanup sweep (145) purges scopes, completes receipts and reads the frozen generations.
CREATE POLICY p_clock_meta_receipts_s ON meta_deletion_receipts FOR SELECT TO svc_clock USING (true);
CREATE POLICY p_clock_meta_receipts_u ON meta_deletion_receipts FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);
CREATE POLICY p_clock_meta_scopes_s ON meta_deletion_receipt_scopes FOR SELECT TO svc_clock USING (true);
CREATE POLICY p_clock_meta_scopes_u ON meta_deletion_receipt_scopes FOR UPDATE TO svc_clock USING (true) WITH CHECK (true);
CREATE POLICY p_clock_meta_generations ON meta_deletion_receipt_generations FOR SELECT TO svc_clock USING (true);
GRANT SELECT, UPDATE (state, completed_at, updated_at) ON meta_deletion_receipts TO svc_clock;
GRANT SELECT, UPDATE (state, next_attempt_at, updated_at) ON meta_deletion_receipt_scopes TO svc_clock;
GRANT SELECT ON meta_deletion_receipt_generations TO svc_clock;
-- 135 granted the other columns and the row policy; the receive door freezes by authorization.
GRANT SELECT (authorization_id) ON instagram_archive_generations TO svc_maintenance;

GRANT CREATE ON SCHEMA public TO svc_maintenance;
-- p_fingerprints[1] is the request's fingerprint under the current key (p_key_version); later
-- elements are older key versions, matched for replays only.
CREATE FUNCTION fn_meta_deletion_receive(p_app_identity text, p_fingerprints text[], p_key_version int,
  p_subject text, p_issued_at timestamptz, p_capability_hash text, p_skew_seconds int)
RETURNS TABLE (o_receipt_id uuid, o_replay boolean, o_state text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_receipt uuid;
  v_state text;
  v_reason text;
  v_issued timestamptz;
  v_skew interval;
  v_ws uuid;
  v_disposition text;
  m record;
BEGIN
  IF p_app_identity IS NULL OR p_app_identity !~ '^(instagram:[0-9]{1,32}|instagram:unconfigured|facebook:legacy)$'
     OR p_fingerprints IS NULL OR array_ndims(p_fingerprints) IS DISTINCT FROM 1
     OR array_lower(p_fingerprints, 1) <> 1 OR cardinality(p_fingerprints) > 16
     OR array_position(p_fingerprints, NULL) IS NOT NULL
     OR EXISTS (SELECT 1 FROM unnest(p_fingerprints) f WHERE f !~ '^[0-9a-f]{64}$')
     OR p_key_version IS NULL OR p_key_version < 1
     OR p_subject IS NULL OR p_subject !~ '^[0-9]{1,64}$'
     OR p_capability_hash IS NULL OR p_capability_hash !~ '^[0-9a-f]{64}$'
     OR p_skew_seconds IS NULL OR p_skew_seconds NOT BETWEEN 0 AND 3600 THEN
    RAISE EXCEPTION 'meta receipt refused: invalid input' USING ERRCODE = 'P0001';
  END IF;
  v_skew := make_interval(secs => p_skew_seconds);
  -- Order evidence from beyond the skew is no evidence: the door owns the clock.
  v_issued := CASE WHEN p_issued_at <= now() + v_skew THEN p_issued_at END;
  PERFORM pg_advisory_xact_lock(hashtextextended('meta_receipt:' || p_app_identity || ':' || p_fingerprints[1], 0));
  SELECT r.id, r.state INTO v_receipt, v_state FROM meta_deletion_receipts r
   WHERE r.app_identity = p_app_identity AND r.request_fingerprint = ANY (p_fingerprints)
   ORDER BY r.received_at, r.id LIMIT 1;
  IF FOUND THEN
    INSERT INTO meta_deletion_receipt_capabilities (capability_hash, receipt_id) VALUES (p_capability_hash, v_receipt);
    RETURN QUERY SELECT v_receipt, true, v_state;
    RETURN;
  END IF;
  INSERT INTO meta_deletion_receipts (app_identity, request_fingerprint, fingerprint_key_version, issued_at)
  VALUES (p_app_identity, p_fingerprints[1], p_key_version, v_issued)
  RETURNING id INTO v_receipt;
  FOR v_ws IN SELECT DISTINCT workspace_id FROM meta_identity_mappings
               WHERE app_identity = p_app_identity AND subject_ref = p_subject ORDER BY workspace_id LOOP
    PERFORM pg_advisory_xact_lock(hashtextextended('sources:' || v_ws, 0));
    FOR m IN SELECT * FROM meta_identity_mappings
              WHERE workspace_id = v_ws AND app_identity = p_app_identity AND subject_ref = p_subject
              ORDER BY ig_account_id, verified_at, authorization_id LOOP
      IF v_issued IS NULL THEN
        -- No order evidence: in scope only while this is the account's one grant for the subject and its
        -- one episode. With a second grant (a reauthorization) or an earlier receipt, nothing says which a
        -- request reached, and a boundary never widens to a newer grant: held, so the receipt needs review.
        v_disposition := CASE WHEN (
            SELECT count(*) FROM meta_identity_mappings o
             WHERE o.workspace_id = v_ws AND o.ig_account_id = m.ig_account_id
               AND o.app_identity = p_app_identity AND o.subject_ref = p_subject) > 1
          OR EXISTS (
            SELECT 1 FROM meta_deletion_receipt_scopes ps
              JOIN meta_identity_mappings pm ON pm.workspace_id = ps.workspace_id
               AND pm.credential_id = ps.credential_id AND pm.authorization_id = ps.authorization_id
             WHERE ps.workspace_id = v_ws AND ps.ig_account_id = m.ig_account_id
               AND ps.receipt_id <> v_receipt
               AND pm.app_identity = p_app_identity AND pm.subject_ref = p_subject)
          THEN 'held' ELSE 'in_scope' END;
      ELSIF COALESCE(m.authorized_at, m.verified_at) <= v_issued - v_skew THEN
        v_disposition := 'in_scope';
      ELSIF m.authorized_at >= v_issued + v_skew THEN
        v_disposition := 'excluded';
      ELSE
        v_disposition := 'held';
      END IF;
      CONTINUE WHEN v_disposition = 'excluded';
      INSERT INTO meta_deletion_receipt_scopes (receipt_id, workspace_id, ig_account_id, credential_id,
        authorization_id, disposition, state)
      VALUES (v_receipt, v_ws, m.ig_account_id, m.credential_id, m.authorization_id, v_disposition,
        CASE WHEN v_disposition = 'held' THEN 'held' ELSE 'pending' END);
      -- Frozen now: a grant made later never joins this scope.
      INSERT INTO meta_deletion_receipt_generations (receipt_id, workspace_id, credential_id,
        authorization_id, source_id, generation_id)
      SELECT v_receipt, v_ws, m.credential_id, m.authorization_id, g.source_id, g.id
        FROM instagram_archive_generations g
        JOIN media_sources s ON s.workspace_id = g.workspace_id AND s.id = g.source_id
       WHERE g.workspace_id = v_ws AND g.authorization_id = m.authorization_id
         AND s.provider = 'instagram_archive' AND s.origin_ig_account_id = m.ig_account_id
         AND g.lifecycle <> 'deleted';
    END LOOP;
  END LOOP;
  v_reason := CASE
    WHEN p_app_identity !~ '^instagram:[0-9]{1,32}$' THEN 'app_unrecognized'
    WHEN EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes
                  WHERE receipt_id = v_receipt AND disposition = 'held') THEN 'ordering_ambiguous'
  END;
  v_state := CASE
    WHEN v_reason IS NOT NULL THEN 'needs_review'
    WHEN NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes WHERE receipt_id = v_receipt) THEN 'completed'
    ELSE 'pending'
  END;
  UPDATE meta_deletion_receipts
     SET state = v_state, review_reason = v_reason,
         completed_at = CASE WHEN v_state = 'completed' THEN now() END
   WHERE id = v_receipt;
  INSERT INTO meta_deletion_receipt_capabilities (capability_hash, receipt_id) VALUES (p_capability_hash, v_receipt);
  RETURN QUERY SELECT v_receipt, false, v_state;
END $$;
COMMENT ON FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamptz, text, integer) IS
  'A Meta data-deletion request, tenant-free. A fingerprint any key version matches is a replay: the same receipt and a new capability, no new cleanup. Otherwise a new receipt: per mapped workspace, ascending, under the sources advisory lock, each mapping of (app identity, subject) is classified by its authorization against issued_at with the skew (in_scope, held, or excluded and never frozen), and in_scope and held scopes freeze the non-deleted generations of that authorization on that origin''s source. Without a credible issued_at a mapping is in_scope only when it is the account''s one mapping for the subject and no earlier receipt scoped the account, and held otherwise, so a newer grant is never reached for want of order evidence. Review reasons: app_unrecognized, ordering_ambiguous. Stores no raw payload, subject or secret. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_ingress (143, #1644).';
ALTER FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamptz, text, integer) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamptz, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_deletion_receive(text, text[], integer, text, timestamptz, text, integer) TO svc_ingress;

CREATE FUNCTION fn_meta_deletion_status(p_capability_hash text)
RETURNS TABLE (o_state text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE WHEN r.state = 'completed' THEN 'completed' ELSE 'in_progress' END
    FROM meta_deletion_receipt_capabilities c
    JOIN meta_deletion_receipts r ON r.id = c.receipt_id
   WHERE c.capability_hash = p_capability_hash
$$;
COMMENT ON FUNCTION fn_meta_deletion_status(text) IS
  'The public status of the receipt a capability names: completed or in_progress, nothing else; an unknown capability returns no row. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_ingress (143, #1644).';
ALTER FUNCTION fn_meta_deletion_status(text) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_deletion_status(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_deletion_status(text) TO svc_ingress;

CREATE FUNCTION fn_meta_receipt_scope(p_receipt uuid)
RETURNS TABLE (o_ig_account_id uuid, o_credential_id uuid, o_authorization_id uuid, o_disposition text,
  o_state text, o_results jsonb, o_source_id uuid, o_generation_ids uuid[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT s.ig_account_id, s.credential_id, s.authorization_id, s.disposition, s.state, s.results, g.source_id,
         COALESCE(array_agg(g.generation_id ORDER BY g.generation_id) FILTER (WHERE g.generation_id IS NOT NULL), '{}')
    FROM meta_deletion_receipt_scopes s
    LEFT JOIN meta_deletion_receipt_generations g ON g.receipt_id = s.receipt_id AND g.workspace_id = s.workspace_id
     AND g.credential_id = s.credential_id AND g.authorization_id = s.authorization_id
   WHERE s.receipt_id = p_receipt
     AND s.workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
   GROUP BY s.receipt_id, s.workspace_id, s.credential_id, s.authorization_id, g.source_id
   ORDER BY s.ig_account_id, s.authorization_id, g.source_id
$$;
COMMENT ON FUNCTION fn_meta_receipt_scope(uuid) IS
  'The claimed tenant''s scopes of one receipt, each with its source and the generation ids it froze; no tenant reads nothing. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_worker (143, #1644).';
ALTER FUNCTION fn_meta_receipt_scope(uuid) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_receipt_scope(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_receipt_scope(uuid) TO svc_worker;

CREATE FUNCTION fn_meta_receipt_progress(p_receipt uuid, p_credential uuid, p_authorization uuid,
  p_state text, p_results jsonb, p_review_reason text, p_retry_after_seconds int)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_ws uuid := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
  v_from text;
  v_state text;
  v_reason text;
BEGIN
  IF p_state IS NULL OR p_state NOT IN ('cleaning','completed')
     OR p_results IS NULL OR jsonb_typeof(p_results) <> 'object' OR octet_length(p_results::text) > 4096
     OR (p_review_reason IS NOT NULL AND (p_review_reason <> 'transit_unresolved' OR p_state = 'completed')) THEN
    RAISE EXCEPTION 'meta receipt progress refused: invalid input' USING ERRCODE = 'P0001';
  END IF;
  SELECT s.state INTO v_from FROM meta_deletion_receipt_scopes s
   WHERE s.receipt_id = p_receipt AND s.workspace_id = v_ws
     AND s.credential_id = p_credential AND s.authorization_id = p_authorization
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'meta receipt progress refused: scope_unavailable' USING ERRCODE = 'P0001';
  END IF;
  IF v_from NOT IN ('pending','cleaning') THEN
    RAISE EXCEPTION 'meta receipt progress refused: scope_%', v_from USING ERRCODE = 'P0001';
  END IF;
  IF p_state = 'completed' THEN
    IF EXISTS (SELECT 1 FROM meta_deletion_receipt_generations x
                 LEFT JOIN instagram_archive_generations g ON g.workspace_id = x.workspace_id
                  AND g.source_id = x.source_id AND g.id = x.generation_id
                WHERE x.receipt_id = p_receipt AND x.workspace_id = v_ws
                  AND x.credential_id = p_credential AND x.authorization_id = p_authorization
                  AND g.lifecycle IS DISTINCT FROM 'deleted') THEN
      RAISE EXCEPTION 'meta receipt progress refused: generations_not_deleted' USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM instagram_artifacts a
                WHERE a.workspace_id = v_ws AND a.kind = 'transit'
                  AND a.data_generation_id IN (SELECT x.generation_id FROM meta_deletion_receipt_generations x
                        WHERE x.receipt_id = p_receipt AND x.workspace_id = v_ws
                          AND x.credential_id = p_credential AND x.authorization_id = p_authorization)
                  AND NOT (a.outcome = 'settled' AND a.cleanup_state = 'destroyed')) THEN
      RAISE EXCEPTION 'meta receipt progress refused: transit_unresolved' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  UPDATE meta_deletion_receipt_scopes
     SET state = p_state, results = p_results,
         next_attempt_at = now() + make_interval(secs => LEAST(GREATEST(COALESCE(p_retry_after_seconds, 60), 60), 3600))
   WHERE receipt_id = p_receipt AND workspace_id = v_ws
     AND credential_id = p_credential AND authorization_id = p_authorization;
  SELECT r.state, r.review_reason INTO v_state, v_reason FROM meta_deletion_receipts r WHERE r.id = p_receipt FOR UPDATE;
  IF p_review_reason IS NOT NULL AND v_state <> 'needs_review' THEN
    UPDATE meta_deletion_receipts SET state = 'needs_review', review_reason = p_review_reason WHERE id = p_receipt;
    v_state := 'needs_review';
  ELSIF (v_state = 'pending' OR (v_state = 'needs_review' AND v_reason = 'transit_unresolved'))
        AND NOT EXISTS (SELECT 1 FROM meta_deletion_receipt_scopes s
         WHERE s.receipt_id = p_receipt AND s.state NOT IN ('completed','workspace_purged')) THEN
    UPDATE meta_deletion_receipts SET state = 'completed', review_reason = NULL, completed_at = now() WHERE id = p_receipt;
    v_state := 'completed';
  END IF;
  RETURN v_state;
END $$;
COMMENT ON FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) IS
  'Reports one of the claimed tenant''s scopes: pending to cleaning or completed, cleaning to cleaning or completed; a held, completed or purged scope refuses by name. Completion is structural and refuses by name (generations_not_deleted, transit_unresolved) unless every generation the scope froze is deleted and no transit upload of them is unresolved. Due again after the retry, clamped to 60..3600 seconds. A transit_unresolved reason moves a pending receipt to needs_review; the receipt completes once every scope is completed or purged, from pending or from needs_review for transit_unresolved alone, and never from any other review reason. Returns the receipt state. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_worker (143, #1644).';
ALTER FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_receipt_progress(uuid, uuid, uuid, text, jsonb, text, integer) TO svc_worker;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
