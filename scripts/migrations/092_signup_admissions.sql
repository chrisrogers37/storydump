-- Migration 092: a new account needs a way in (owner decision, 2026-10-02: "Limit sign-in to
-- emails you've let in from the waitlist"). Appended to the advertised stream as `07` §35.
--
-- ANY GOOGLE ACCOUNT COULD SIGN UP. The sign-in callback's identity upsert created a `users` row
-- for every verified Google subject it had not seen, and a user may create workspaces. A NEW
-- subject now creates its user only when fn_signup_admitted says its verified email may: the
-- owner admitted it (a row here), or a live invitation (pending, unexpired) is addressed to it.
-- A person who already has an identity signs in as before; the door is asked only before the
-- INSERT INTO users (`identity.upsert_google_identity`), and TARGET_SIGNUP_OPEN switches the ask
-- off for a local stack.
--
-- signup_admissions is global, not tenant-plane: it has no workspace. The owner admits someone
-- with one INSERT as the database owner, which owner-bypass lets through; RLS is on and the one
-- policy is svc_membership's read, so the runtime roles, which hold no grant here, read nothing,
-- and the door is the only reader. The CHECK keeps the stored address lower case, so an admission
-- typed in capitals is refused at the INSERT rather than silently never matching.
--
-- fn_signup_admitted reads two tables svc_membership already reads or is granted here: this one
-- and workspace_invitations (058's row-open p_member_invites). It answers one boolean about one
-- address, never which workspace invited it. Both sides compare lower(), as fn_invitation_accept
-- does. The CREATE bracket is 062's, as 090's is.
--
-- Adoption evidence (#997): the table with RLS enabled, the door owned by svc_membership and
-- granted to svc_ingress, and svc_membership's read — catalog state this file creates.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'signup_admissions' AND c.relrowsecurity)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_signup_admitted' AND p.prosecdef AND r.rolname = 'svc_membership')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_signup_admitted' AND r.rolname = 'svc_ingress' AND a.privilege_type = 'EXECUTE')
-- runner:postcondition SELECT has_table_privilege('svc_membership', 'signup_admissions', 'SELECT')

CREATE TABLE signup_admissions (
  email       text PRIMARY KEY CONSTRAINT ck_signup_admissions_lower CHECK (email = lower(email)),
  admitted_at timestamptz NOT NULL DEFAULT now(),
  note        text
);

ALTER TABLE signup_admissions ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON signup_admissions TO svc_membership;

CREATE POLICY p_member_admissions ON signup_admissions FOR SELECT TO svc_membership USING (true);

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_signup_admitted(p_email text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM signup_admissions a WHERE a.email = lower(p_email))
      OR EXISTS (SELECT 1 FROM workspace_invitations i
                  WHERE lower(i.email) = lower(p_email)
                    AND i.state = 'pending' AND i.expires_at > now())
$$;

COMMENT ON FUNCTION fn_signup_admitted(text) IS
  'May a new Google account with this verified email create its user (092)? True when the owner '
  'admitted the address (signup_admissions) or a pending, unexpired invitation is addressed to it; '
  'false for NULL. Compares lower() on both sides. Answers one boolean, never which workspace '
  'invited the address. SECURITY DEFINER owned by svc_membership with EXECUTE granted to '
  'svc_ingress.';

ALTER FUNCTION fn_signup_admitted(text) OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_signup_admitted(text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_signup_admitted(text) TO svc_ingress;
