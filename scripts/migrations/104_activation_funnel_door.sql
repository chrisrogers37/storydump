-- Migration 104: the activation funnel door — how far the people who signed up got through
-- onboarding, counted across every workspace by a SECURITY DEFINER read owned by svc_maintenance
-- (#1481; `07` §47). Statements appended to the advertised stream.
--
-- THE QUESTION HAS NO TENANT. "Of the people who signed up since a date, how many created a
-- workspace, connected Instagram, added a folder and approved a first story, and where did the rest
-- stop?" spans every workspace. The tenant-scoped tables it joins show no runtime login another
-- tenant's rows, and users, the one user-plane table, is open to the runtime logins row by row for
-- sign-in, not as a report. A door answers it, in counts only.
--
-- ONE DOOR, FIVE ROWS, COUNTS ONLY. fn_activation_funnel(p_since, p_stall) returns one row per
-- stage — signed in, workspace created, Instagram connected, folder added, first approval — with
-- how many people reached it and how many stalled there; no id, name or email leaves it. The cohort
-- is the active users created at or after p_since. A person's workspaces are the active ones they
-- own (a workspace_members row with role 'owner'), and each stage is the EARLIEST matching row:
-- the sign-up itself, the first owned workspace, the first ig_accounts row and the first
-- media_sources row in one, in any state (a first connect counts though the account was removed
-- later), and the first approval — an audit row the intent trigger wrote (detail IS NULL; a row a
-- service writes by hand always carries a detail) moving a post_intent from awaiting_approval to
-- approved, or to posted, which is a person's "Posted myself" in manual mode. Each stage is counted
-- on its own: a folder added before Instagram counts for both.
--
-- STALLED. A person's first missing stage is the earliest of stages 2-5 they have not reached, and
-- their latest activity is the latest stage they have. They are stalled when a stage is missing
-- and nothing has happened for p_stall (72 hours by default), and they are counted against the
-- stage before the missing one, so the first approval's row never counts a stall. A person who
-- owns no active workspace but belongs to someone else's joined a team rather than stopping: signed
-- in, never stalled.
--
-- THE READS IT NEEDS. svc_maintenance already reads workspaces and audit_events (057, 058),
-- ig_accounts (081) and media_sources (082) under USING (true) policies; users and
-- workspace_members are the two it lacked, so this file grants those SELECTs and their policies.
--
-- EXECUTE FOR svc_worker ALONE. The worker's login is the one the psql escape hatch connects as
-- (documentation/operations/reading-the-ledger.md); svc_ingress never holds it, so no API route or
-- CLI verb can reach the door until an operator principal exists to stand behind one (#1124).
--
-- The CREATE bracket is 062's, for the reason it gave: ALTER FUNCTION … OWNER TO needs the incoming
-- owner to hold CREATE on the schema, and the steady state never leaves it there.
--
-- SEARCH PATH: pinned to pg_catalog, public, pg_temp, with pg_temp LAST. Unlisted, pg_temp is
-- searched first, so a temporary object could shadow a name the door resolves while it runs as
-- svc_maintenance.
--
-- DEPLOY ORDER: nothing calls the door. The predeploy applies this file and the services read
-- nothing new.
--
-- Adoption evidence (#997): the door by name and owner (not arity, so a later file may change its
-- arguments without falsifying this evidence), svc_worker's EXECUTE row as a floor, the two
-- policies by name and the two SELECT grants — catalog state this file alone creates. Each probe
-- joins the catalogs by name, never has_*_privilege or a regclass cast, so it reads false without
-- raising on a database that has none of these roles, tables or objects. The closed CREATE bracket
-- is not probed: an absence reads true on a database this file has never touched.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_activation_funnel' AND r.rolname = 'svc_maintenance' AND p.prosecdef)
-- runner:postcondition SELECT count(*) >= 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_activation_funnel' AND a.privilege_type = 'EXECUTE' AND g.rolname = 'svc_worker'
-- runner:postcondition SELECT count(*) = 2 FROM pg_policies WHERE schemaname = 'public' AND ((tablename = 'users' AND policyname = 'p_maint_users') OR (tablename = 'workspace_members' AND policyname = 'p_maint_members'))
-- runner:postcondition SELECT count(DISTINCT c.relname) = 2 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a JOIN pg_roles g ON g.oid = a.grantee WHERE n.nspname = 'public' AND c.relname IN ('users', 'workspace_members') AND a.privilege_type = 'SELECT' AND g.rolname = 'svc_maintenance'

-- [§47 the activation funnel door: how far the people who signed up got, counted across every workspace through svc_maintenance]
-- How many people who signed up since p_since created a workspace, connected Instagram, added a
-- folder and approved a first story, and how many stalled at each stage: a read across every
-- workspace that no runtime login can make: the tenant-scoped tables it joins hide other tenants' rows.
-- One definer door, counts only. svc_maintenance lacked users and workspace_members (SELECT, and a
-- USING (true) policy each); EXECUTE for svc_worker alone, the login the psql escape hatch connects
-- as, and for no API principal until an operator principal exists (#1124). The CREATE bracket is
-- 062's.
GRANT SELECT ON users TO svc_maintenance;

GRANT SELECT ON workspace_members TO svc_maintenance;

CREATE POLICY p_maint_users ON users FOR SELECT TO svc_maintenance USING (true);

CREATE POLICY p_maint_members ON workspace_members FOR SELECT TO svc_maintenance USING (true);

GRANT CREATE ON SCHEMA public TO svc_maintenance;

CREATE FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval DEFAULT interval '72 hours')
RETURNS TABLE (o_ordinal int, o_stage text, o_reached bigint, o_stalled bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH owned_ws AS (
    SELECT m.user_id, w.id AS workspace_id, w.created_at
      FROM workspace_members m
      JOIN workspaces w ON w.id = m.workspace_id
     WHERE m.role = 'owner' AND w.state = 'active'
  ), stages AS (
    SELECT u.created_at AS s1,
           (SELECT min(o.created_at) FROM owned_ws o WHERE o.user_id = u.id) AS s2,
           (SELECT min(a.created_at) FROM owned_ws o
              JOIN ig_accounts a ON a.workspace_id = o.workspace_id
             WHERE o.user_id = u.id) AS s3,
           (SELECT min(src.created_at) FROM owned_ws o
              JOIN media_sources src ON src.workspace_id = o.workspace_id
             WHERE o.user_id = u.id) AS s4,
           (SELECT min(e.created_at) FROM owned_ws o
              JOIN audit_events e ON e.workspace_id = o.workspace_id
             WHERE o.user_id = u.id
               AND e.entity_kind = 'post_intent' AND e.detail IS NULL
               AND e.from_state = 'awaiting_approval'
               AND e.to_state IN ('approved', 'posted')) AS s5,
           EXISTS (SELECT 1 FROM workspace_members m
                     JOIN workspaces w ON w.id = m.workspace_id
                    WHERE m.user_id = u.id AND m.role <> 'owner'
                      AND w.state = 'active') AS joined
      FROM users u
     WHERE u.state = 'active' AND u.created_at >= p_since
  ), placed AS (
    SELECT s2, s3, s4, s5,
           CASE WHEN s2 IS NULL AND joined THEN NULL
                WHEN s2 IS NULL THEN 2
                WHEN s3 IS NULL THEN 3
                WHEN s4 IS NULL THEN 4
                WHEN s5 IS NULL THEN 5
           END AS first_missing,
           GREATEST(s1, s2, s3, s4, s5) < now() - p_stall AS idle
      FROM stages
  ), totals AS (
    SELECT count(*) AS r1, count(s2) AS r2, count(s3) AS r3, count(s4) AS r4, count(s5) AS r5,
           count(*) FILTER (WHERE first_missing = 2 AND idle) AS t1,
           count(*) FILTER (WHERE first_missing = 3 AND idle) AS t2,
           count(*) FILTER (WHERE first_missing = 4 AND idle) AS t3,
           count(*) FILTER (WHERE first_missing = 5 AND idle) AS t4
      FROM placed
  )
  SELECT k.ordinal, k.stage, k.reached, k.stalled
    FROM totals t
   CROSS JOIN LATERAL (VALUES (1, 'signed in', t.r1, t.t1),
                              (2, 'workspace created', t.r2, t.t2),
                              (3, 'Instagram connected', t.r3, t.t3),
                              (4, 'folder added', t.r4, t.t4),
                              (5, 'first approval', t.r5, 0::bigint))
         AS k (ordinal, stage, reached, stalled)
   ORDER BY k.ordinal
$$;

COMMENT ON FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval) IS
  'The activation funnel across every workspace: one row per stage (signed in, workspace created, Instagram connected, folder added, first approval) with how many people who signed up since p_since reached it, each stage counted on its own, and how many stalled there: a later stage missing and nothing new from them in p_stall, counted against the stage before the missing one. A person who owns no active workspace but belongs to someone else''s counts as signed in and never as stalled. Counts only: no id, name or email leaves it. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_worker alone, the login the psql escape hatch connects as, and for no API principal until an operator principal exists (#1124) (104, #1481).';

ALTER FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval) OWNER TO svc_maintenance;

REVOKE ALL ON FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_activation_funnel(p_since timestamptz, p_stall interval) TO svc_worker;

REVOKE CREATE ON SCHEMA public FROM svc_maintenance;
