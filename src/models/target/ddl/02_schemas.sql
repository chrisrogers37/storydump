-- Schema ownership and access.

ALTER SCHEMA public OWNER TO CURRENT_USER;

REVOKE ALL ON SCHEMA public FROM PUBLIC;

COMMENT ON SCHEMA public IS NULL;

GRANT USAGE ON SCHEMA public TO svc_ingress, svc_worker, svc_claim, svc_clock, svc_maintenance, svc_membership;

CREATE SCHEMA archive;

GRANT USAGE, CREATE ON SCHEMA archive TO svc_maintenance;
