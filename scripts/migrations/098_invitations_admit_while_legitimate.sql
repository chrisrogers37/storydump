-- Migration 098: an invitation admits only while it is legitimate (owner decision:
-- "removed people stay removed"; PR #1560 security review, finding 1). Appended to the
-- advertised stream as `07` §41.
--
-- A REMOVED ADMIN'S INVITATIONS OUTLIVED THEM. fn_signup_admitted (092) counted any pending,
-- unexpired invitation addressed to an email, so an admin could mint an invitation for a second
-- address of their own, be removed, and still sign up a brand-new account with it, then accept it
-- back into the workspace. A suspended workspace's invitations admitted too. The door now counts
-- an invitation only while its workspace is 'active' and its inviter is still an owner or admin
-- member of that workspace: a removed or demoted inviter, a suspended or offboarding workspace,
-- and an invitation with no recorded inviter (a workspace service identity's, or one whose
-- inviter's account was deleted) admit nobody new. The owner's admissions are untouched.
--
-- A removal also revokes the pending invitations the removed member sent in that workspace, so the
-- accept door (059) refuses them as well. That UPDATE is the application's
-- (`invitations.revoke_sent_by`, in the removal's transaction; svc_ingress holds UPDATE on
-- workspace_invitations under the tenant policy), so fn_member_remove's body is left as it is. A
-- demoted admin's invitations stay pending and acceptable by an existing account; they no longer
-- admit a new one, because the door reads the inviter's role live. fn_invitation_accept is left
-- as it is: a service identity's invitation records no inviter, so holding the accept door to a
-- live inviter would refuse every one of them.
--
-- No data is written here: the door is replaced in place (CREATE OR REPLACE keeps its owner and
-- grants), inside 062's CREATE bracket, as 092's is. svc_membership already holds what the new
-- body needs (057: SELECT on workspaces, workspace_invitations and workspace_members; 058's
-- row-open policies on all three).
--
-- Adoption evidence (#997): the door's new text — catalog state this file creates.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_signup_admitted' AND position('invited_by_user_id' IN p.prosrc) > 0)

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE OR REPLACE FUNCTION fn_signup_admitted(p_email text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM signup_admissions a WHERE a.email = lower(p_email))
      OR EXISTS (SELECT 1 FROM workspace_invitations i
                   JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'
                   JOIN workspace_members m ON m.workspace_id = i.workspace_id
                                           AND m.user_id = i.invited_by_user_id
                                           AND m.role IN ('owner', 'admin')
                  WHERE lower(i.email) = lower(p_email)
                    AND i.state = 'pending' AND i.expires_at > now())
$$;

COMMENT ON FUNCTION fn_signup_admitted(text) IS
  'May a new Google account with this verified email create its user (092)? True when the owner '
  'admitted the address (signup_admissions) or a pending, unexpired invitation is addressed to it '
  'from an active workspace whose owner or admin it still was sent by (098); false for NULL. '
  'Compares lower() on both sides. Answers one boolean, never which workspace invited the '
  'address. SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;
