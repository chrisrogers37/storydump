-- 142: verified Meta identity mappings: the (app, subject) a Meta callback names, bound to the credential and authorization it was verified for (#1644).
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'meta_identity_mappings' AND c.relkind = 'r' AND c.relrowsecurity)
-- runner:postcondition SELECT count(*) >= 3 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'meta_identity_mappings'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_meta_subject_mappings' AND p.prosecdef AND r.rolname = 'svc_maintenance' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig))
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_meta_subject_mappings' AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_ingress')
CREATE TABLE meta_identity_mappings (
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  ig_account_id UUID NOT NULL,
  credential_id UUID NOT NULL,
  authorization_id UUID NOT NULL,
  app_identity TEXT NOT NULL,
  subject_ref TEXT NOT NULL,
  account_ref TEXT NOT NULL,
  method TEXT NOT NULL,
  authorized_at TIMESTAMPTZ NULL,
  verified_at TIMESTAMPTZ NOT NULL,
  is_current BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, credential_id, authorization_id),
  CONSTRAINT ck_meta_mapping_app CHECK (app_identity ~ '^instagram:[0-9]{1,32}$'),
  CONSTRAINT ck_meta_mapping_refs CHECK (subject_ref ~ '^[0-9]{1,64}$' AND account_ref ~ '^[0-9]{1,64}$'),
  CONSTRAINT ck_meta_mapping_method CHECK ((method = 'oauth_exchange' AND authorized_at IS NOT NULL) OR (method = 'reconciled_read' AND authorized_at IS NULL)),
  CONSTRAINT fk_meta_mapping_credential FOREIGN KEY (workspace_id, ig_account_id, credential_id) REFERENCES oauth_credentials(workspace_id, ig_account_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_meta_mapping_current ON meta_identity_mappings (workspace_id, credential_id) WHERE is_current;
CREATE INDEX ix_meta_mapping_subject ON meta_identity_mappings (app_identity, subject_ref);

-- A mapping is evidence: only the current flag moves, and only from true to false.
CREATE FUNCTION trg_meta_mapping_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF (NEW.workspace_id, NEW.ig_account_id, NEW.credential_id, NEW.authorization_id, NEW.app_identity, NEW.subject_ref, NEW.account_ref, NEW.method, NEW.authorized_at, NEW.verified_at, NEW.created_at)
     IS DISTINCT FROM (OLD.workspace_id, OLD.ig_account_id, OLD.credential_id, OLD.authorization_id, OLD.app_identity, OLD.subject_ref, OLD.account_ref, OLD.method, OLD.authorized_at, OLD.verified_at, OLD.created_at)
     OR (NEW.is_current AND NOT OLD.is_current) THEN
    RAISE EXCEPTION 'meta identity mapping is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
REVOKE ALL ON FUNCTION trg_meta_mapping_immutable() FROM PUBLIC;
CREATE TRIGGER tg_meta_mapping_immutable BEFORE UPDATE ON meta_identity_mappings FOR EACH ROW EXECUTE FUNCTION trg_meta_mapping_immutable();

-- The runtime logins write their own tenant's rows (no DELETE: a credential's deletion cascades);
-- the deletion and reconciliation doors read across tenants.
ALTER TABLE meta_identity_mappings ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_tenant ON meta_identity_mappings FOR ALL TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE (is_current) ON meta_identity_mappings TO svc_ingress, svc_worker;
GRANT SELECT ON meta_identity_mappings TO svc_maintenance, svc_clock;
CREATE POLICY p_maint_meta_mappings ON meta_identity_mappings FOR SELECT TO svc_maintenance USING (true);
CREATE POLICY p_clock_meta_mappings ON meta_identity_mappings FOR SELECT TO svc_clock USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;
CREATE FUNCTION fn_meta_subject_mappings(p_app_identity text, p_subject text)
RETURNS TABLE (o_workspace_id uuid, o_ig_account_id uuid, o_credential_id uuid, o_authorization_id uuid,
  o_authorized_at timestamptz, o_verified_at timestamptz, o_is_current boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT workspace_id, ig_account_id, credential_id, authorization_id, authorized_at, verified_at, is_current
    FROM meta_identity_mappings
   WHERE app_identity = p_app_identity AND subject_ref = p_subject
   ORDER BY workspace_id, verified_at, authorization_id
$$;
COMMENT ON FUNCTION fn_meta_subject_mappings(text, text) IS
  'The verified mappings a Meta deauthorize callback''s (app identity, subject) names, across every workspace: the callback carries no tenant, so p_tenant would hide every row from the API''s login. An equality on the indexed pair, ordered by workspace and verification time. SECURITY DEFINER owned by svc_maintenance; EXECUTE for svc_ingress (142, #1644).';
ALTER FUNCTION fn_meta_subject_mappings(text, text) OWNER TO svc_maintenance;
REVOKE ALL ON FUNCTION fn_meta_subject_mappings(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_meta_subject_mappings(text, text) TO svc_ingress;
REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
