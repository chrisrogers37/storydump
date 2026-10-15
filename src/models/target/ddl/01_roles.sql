-- The service roles and their memberships. Roles are cluster-wide: the block
-- creates the missing ones and leaves existing ones as they are.

DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['svc_ingress', 'svc_worker', 'svc_migration'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I LOGIN', r);
    END IF;
  END LOOP;
  FOREACH r IN ARRAY ARRAY['svc_claim', 'svc_clock', 'svc_maintenance', 'svc_membership'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END $$;

GRANT svc_claim, svc_clock, svc_maintenance, svc_membership TO svc_migration;

DO $$
BEGIN
  EXECUTE format('GRANT svc_migration TO %I', current_user);
END $$;
