-- Migration 064: the memberships door — fn_memberships_for_caller() + ix_members_user
-- (#1037; the tenth `02` §7 door). Statements 260-266, appended to the advertised stream as
-- `07` §10.
--
-- THE GAP, FOUND TWICE. The web surface's first read after sign-in is "which workspaces am I
-- in". p_tenant on workspace_members is workspace_id = app.tenant_id: with no tenant claimed
-- the table reads EMPTY, and an empty list is indistinguishable from the greenfield's normal
-- signed-in-with-no-workspace state — a three-workspace owner would be sent to first-run
-- onboarding. alex hit it from the writer side (#1031) and astrid from the router side
-- (#1035); rajan confirmed one gap. Until this file both lanes REFUSED by name
-- (membership_list_unreadable) rather than answer []. This is what the door replaces: a
-- disclosed inability becomes an answer, and the refusal branches are deleted with it.
--
-- THE CALLER IS THE GUC, NEVER A PARAMETER (rajan's refinement on #1015). A p_user parameter
-- would be a SECOND place the app asserts who is calling; app.actor_user_id is the one it
-- already has to get right, because trg_governance_audit attributes every governance write
-- to it. NULLIF(current_setting(..., true), '') makes an unset GUC read as NULL, and NULL
-- matches no row: a caller that forgot the claim reads "no memberships", never another
-- user's. A definer function reading a GUC to bound its own result grants nothing to any
-- other role, which is why this is a door and not a user-plane policy on the table.
--
-- OWNED BY svc_membership, which already holds USING (true) on workspace_members and SELECT
-- on workspaces (058) — no new grant on any table. The CREATE bracket is 062's, for the reason
-- it gave: ALTER FUNCTION … OWNER TO needs the incoming owner to hold CREATE on the schema,
-- and the steady-state grant matrix never leaves CREATE with a door owner.
--
-- THE INDEX: workspace_members' primary key is (workspace_id, user_id), so the by-user read
-- this door serves was a sequential scan per /me.
--
-- DEPLOY ORDER: the consumer (workspaces.list_for_user, #1035's async lane) calls the door
-- unconditionally — apply this file BEFORE deploying the code that reads through it.

-- Adoption evidence + post-apply verification (#997). Catalog-only, deliberately: has_*_privilege
-- probes RAISE when their role is absent and the runner treats a raising probe as a hard
-- failure. Four reads, one per structural thing this file does.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_memberships_for_caller' AND p.prosecdef AND r.rolname = 'svc_membership')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_memberships_for_caller' AND r.rolname = 'svc_ingress' AND a.privilege_type = 'EXECUTE')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'workspace_members' AND indexname = 'ix_members_user')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND r.rolname = 'svc_membership' AND a.privilege_type = 'CREATE')

-- 10/10 fn_memberships_for_caller — the user-plane door the web surface reads its workspace
-- list through (#1037; the gap was found twice — #1031 writer-side, #1035 router-side — and
-- confirmed one gap). p_tenant on workspace_members is workspace_id = app.tenant_id, so "which
-- workspaces does this user belong to" has no pre-context read path for svc_ingress: with no
-- tenant set the table reads EMPTY, and an empty list is indistinguishable from the greenfield's
-- normal signed-in-with-no-workspace state. Both lanes therefore REFUSED
-- (membership_list_unreadable) until this door existed. The caller is read from
-- app.actor_user_id INTERNALLY, never taken as a parameter: one trust point — the GUC the
-- audit triggers already attribute every governance write to — and an unset GUC fails closed
-- to zero rows. Owned by svc_membership (already USING (true) on workspace_members and SELECT
-- on workspaces); it returns exactly the four columns the surface renders, never a widened read.
-- The CREATE bracket is 062's, for the reason it gave: ALTER FUNCTION … OWNER TO needs the
-- incoming owner to hold CREATE on the schema, and the steady state never leaves it there.
GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_memberships_for_caller()
RETURNS TABLE (o_workspace_id uuid, o_name varchar, o_state text, o_role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT m.workspace_id, w.name, w.state, m.role
    FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
   WHERE m.user_id = NULLIF(current_setting('app.actor_user_id', true), '')::uuid
   ORDER BY w.created_at, w.id
$$;

COMMENT ON FUNCTION fn_memberships_for_caller() IS
  'The calling user''s workspace memberships: a SECURITY DEFINER user-plane read owned by '
  'svc_membership with EXECUTE granted to svc_ingress. The caller is app.actor_user_id, read '
  'internally and never a parameter; unset, it returns no rows. Exists because p_tenant on '
  'workspace_members cannot answer a cross-tenant question for one user.';

ALTER FUNCTION fn_memberships_for_caller() OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_memberships_for_caller() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_memberships_for_caller() TO svc_ingress;

-- The read the door serves had no index: workspace_members' primary key is (workspace_id,
-- user_id), so a by-user lookup was a sequential scan.
CREATE INDEX ix_members_user ON workspace_members (user_id);
