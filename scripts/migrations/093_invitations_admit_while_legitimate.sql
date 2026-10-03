-- Migration 093: an invitation admits only while it is legitimate (owner decision:
-- "removed people stay removed"; PR #1560 security review, finding 1). Appended to the
-- advertised stream as `07` §36.
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
-- fn_member_remove (090) now also revokes the removed member's pending invitations in that
-- workspace (the ones they sent), in the same transaction as the delete, so the accept door
-- (059) refuses them as well. A demoted admin's invitations stay pending and acceptable by an
-- existing account; they no longer admit a new one, because the door reads the inviter's role
-- live. fn_invitation_accept is left as it is: a service identity's invitation records no
-- inviter, so holding the accept door to a live inviter would refuse every one of them.
--
-- No data is written here: the two doors are replaced in place (CREATE OR REPLACE keeps their
-- owner and grants), inside 062's CREATE bracket, as 090's and 092's are. svc_membership already
-- holds what the new bodies need (057: SELECT on workspaces, SELECT and UPDATE on
-- workspace_invitations, SELECT on workspace_members; 058's row-open policies on all three).
--
-- Adoption evidence (#997): the two doors' new text — catalog state this file creates.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_signup_admitted' AND position('invited_by_user_id' IN p.prosrc) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_member_remove' AND position('workspace_invitations' IN p.prosrc) > 0)

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
  'from an active workspace whose owner or admin it still was sent by (093); false for NULL. '
  'Compares lower() on both sides. Answers one boolean, never which workspace invited the '
  'address. SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

CREATE OR REPLACE FUNCTION fn_member_remove(p_workspace uuid, p_user uuid, p_by_user uuid)
RETURNS TABLE (o_outcome text, o_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_role text;
BEGIN
  IF p_user = p_by_user THEN
    RETURN QUERY SELECT 'self'::text, NULL::text; RETURN;
  END IF;
  SELECT m.role INTO v_role FROM workspace_members m
   WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  IF v_role IS NULL THEN
    RETURN QUERY SELECT 'not_found'::text, NULL::text; RETURN;
  END IF;
  IF v_role = 'owner' THEN
    RETURN QUERY SELECT 'owner'::text, v_role; RETURN;
  END IF;
  DELETE FROM workspace_members m WHERE m.workspace_id = p_workspace AND m.user_id = p_user;
  INSERT INTO workspace_member_removals (workspace_id, user_id, removed_by_user_id)
  VALUES (p_workspace, p_user, p_by_user)
  ON CONFLICT (workspace_id, user_id)
  DO UPDATE SET removed_by_user_id = EXCLUDED.removed_by_user_id, removed_at = now();
  UPDATE workspace_invitations i SET state = 'revoked'
   WHERE i.workspace_id = p_workspace AND i.invited_by_user_id = p_user AND i.state = 'pending';
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

COMMENT ON FUNCTION fn_member_remove(uuid, uuid, uuid) IS
  'The revoke for every join edge (06): an admin removes a member explicitly, and the removal is '
  'recorded so the Telegram join path cannot undo it (090); the pending invitations the removed '
  'member sent in the workspace are revoked with it (093). The one DELETE on workspace_members '
  'in the system lives here (057: no login role deletes). p_by_user is the command port''s actor, '
  'already held to the admin floor — the caller proves the admin. Outcomes: removed, not_found, '
  'owner (never removable here), self (never through this door). SECURITY DEFINER owned by '
  'svc_membership with EXECUTE granted to svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;
