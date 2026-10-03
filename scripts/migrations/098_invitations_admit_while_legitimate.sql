-- Migration 098: an invitation admits only while it is legitimate (owner decision:
-- "removed people stay removed"; PR #1560 security review, finding 1; PR #1574 security review,
-- findings 1, 2 and 4). Appended to the advertised stream as `07` §41.
--
-- A REMOVED ADMIN'S INVITATIONS OUTLIVED THEM. fn_signup_admitted (092) counted any pending,
-- unexpired invitation addressed to an email, and fn_invitation_accept (059) checked only the
-- token, its state and expiry and the identity proof. An admin could mint an invitation for a
-- second address of their own, be removed, and still bring that address in, as an admin, with a
-- new account or an existing one. A suspended workspace's invitations still let people in, and an
-- invitation addressed to a member who was later removed brought them back.
--
-- AN INVITATION IS LEGITIMATE while its workspace is 'active' and its inviter is still an owner or
-- admin member of that workspace with an active account. Both doors now read that live:
-- fn_signup_admitted counts only a legitimate invitation, and fn_invitation_accept refuses any
-- other with the same no_data_found it answers for a used, revoked or expired one. A removed,
-- demoted or disabled inviter, a suspended or offboarding workspace, and an invitation with no
-- recorded inviter (a deleted inviter's) let nobody in. Workspace service identities never write
-- in this release (07 §1, F10), so no live invitation lacks an inviter by design.
--
-- A REMOVAL OUTRANKS AN EARLIER INVITATION. fn_invitation_accept also refuses a person whose
-- removal from the workspace (workspace_member_removals, 090) is newer than the invitation, so an
-- invitation sent before the removal cannot undo it; a fresh invitation after it still can.
--
-- The removal also revokes the pending invitations the removed member sent or was sent, in the
-- removal's transaction (`invitations.revoke_on_removal`, called by `workspaces.remove_member`), so
-- fn_member_remove's body is left as it is. Invitations sent by people removed before this file
-- are revoked here once: a pending invitation whose inviter has a removal record in its workspace
-- and is not a member there again. workspace_invitations carries no governance trigger (only
-- 053's touch trigger), so the UPDATE needs no actor.
--
-- svc_membership, the doors' owner, reads users' id and state (a column grant under a SELECT
-- policy) to see an inviter's account state; it already holds the rest (057: workspaces,
-- workspace_invitations, workspace_members; 090: workspace_member_removals). The doors are
-- replaced in place (CREATE OR REPLACE keeps their owner and grants) inside 062's CREATE bracket,
-- as 090's and 092's are.
--
-- Adoption evidence (#997): the doors' new text and the grant — catalog state this file creates.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_signup_admitted' AND position('invited_by_user_id' IN p.prosrc) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_invitation_accept' AND position('workspace_member_removals' IN p.prosrc) > 0)
-- runner:postcondition SELECT has_column_privilege('svc_membership', 'users', 'state', 'SELECT')

GRANT SELECT (id, state) ON users TO svc_membership;

CREATE POLICY p_member_users ON users FOR SELECT TO svc_membership USING (true);

UPDATE workspace_invitations i SET state = 'revoked'
  FROM workspace_member_removals r
 WHERE r.workspace_id = i.workspace_id AND r.user_id = i.invited_by_user_id
   AND i.state = 'pending'
   AND NOT EXISTS (SELECT 1 FROM workspace_members m
                    WHERE m.workspace_id = i.workspace_id AND m.user_id = i.invited_by_user_id);

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
                   JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'
                  WHERE lower(i.email) = lower(p_email)
                    AND i.state = 'pending' AND i.expires_at > now())
$$;

COMMENT ON FUNCTION fn_signup_admitted(text) IS
  'May a new Google account with this verified email create its user (092)? True when the owner '
  'admitted the address (signup_admissions) or a pending, unexpired invitation is addressed to it '
  'from an active workspace by someone still its owner or an admin, with an active account (098); '
  'false for NULL. Compares lower() on both sides. Answers one boolean, never which workspace '
  'invited the address. SECURITY DEFINER owned by svc_membership with EXECUTE granted to '
  'svc_ingress.';

CREATE OR REPLACE FUNCTION fn_invitation_accept(p_token_hash text, p_user uuid, p_provider text,
                                                p_verified_email text, p_tg_user_id bigint,
                                                p_channel text)
RETURNS TABLE (o_workspace_id uuid, o_granted_role text, o_matched boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE inv record; m boolean; grant_role text; bind uuid;
BEGIN
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', COALESCE(p_channel, 'web'), true);
  SELECT * INTO inv FROM workspace_invitations i
   WHERE i.token_hash = p_token_hash AND i.state = 'pending' AND i.expires_at > now()
   FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'invitation not acceptable (used, revoked, expired, or unknown)'
      USING ERRCODE = 'no_data_found';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM workspaces w
                     WHERE w.id = inv.workspace_id AND w.state = 'active')
     OR NOT EXISTS (SELECT 1 FROM workspace_members a
                      JOIN users u ON u.id = a.user_id AND u.state = 'active'
                     WHERE a.workspace_id = inv.workspace_id
                       AND a.user_id = inv.invited_by_user_id
                       AND a.role IN ('owner', 'admin'))
     OR EXISTS (SELECT 1 FROM workspace_member_removals r
                 WHERE r.workspace_id = inv.workspace_id AND r.user_id = p_user
                   AND r.removed_at >= inv.created_at) THEN
    RAISE EXCEPTION 'invitation not acceptable (no longer legitimate)'
      USING ERRCODE = 'no_data_found';
  END IF;
  -- D33 per-provider acceptance constraint, evaluated in-body:
  IF p_provider = 'google' THEN
    IF inv.email IS NOT NULL THEN
      IF lower(p_verified_email) IS DISTINCT FROM lower(inv.email) THEN
        RAISE EXCEPTION 'identity proof mismatch' USING ERRCODE = 'check_violation';
      END IF;
      m := true;
    ELSE m := false;                             -- no comparable proof: recorded skip
    END IF;
  ELSIF p_provider = 'telegram' THEN
    IF inv.invited_tg_user_id IS NOT NULL THEN
      IF p_tg_user_id IS DISTINCT FROM inv.invited_tg_user_id THEN
        RAISE EXCEPTION 'identity proof mismatch' USING ERRCODE = 'check_violation';
      END IF;
      m := true;
    ELSE m := false;                             -- hint-only or bare token: recorded skip (D36)
    END IF;
  ELSE
    RAISE EXCEPTION 'unknown acceptance provider %', p_provider;
  END IF;
  UPDATE workspace_invitations
     SET state = 'accepted', accepted_by_user_id = p_user, accepted_email_matched = m
   WHERE id = inv.id;
  grant_role := CASE WHEN inv.role = 'admin' AND m THEN 'admin' ELSE 'member' END;
  INSERT INTO workspace_members (workspace_id, user_id, role, added_by_user_id)
  VALUES (inv.workspace_id, p_user, grant_role, inv.invited_by_user_id)
  ON CONFLICT (workspace_id, user_id) DO NOTHING;  -- already a member: invite consumed, the
                                                    -- existing role stands (role changes go
                                                    -- through the 06 §2 gate)
  IF inv.role = 'admin' AND NOT m THEN             -- D36 elevation-pending, same transaction
    SELECT b.id INTO bind FROM channel_bindings b
     WHERE b.workspace_id = inv.workspace_id AND b.state = 'active'
     ORDER BY b.created_at LIMIT 1;
    IF bind IS NOT NULL THEN
      INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload)
      VALUES (inv.workspace_id, bind, 'notification',
              jsonb_build_object('v', 1, 'template', 'elevation_pending',
                                 'invitation_id', inv.id, 'accepted_by', p_user));
    END IF;                                        -- zero-binding workspace: the pending
  END IF;                                          -- elevation is visible on the web surface
  RETURN QUERY SELECT inv.workspace_id, grant_role, m;
END $$;

COMMENT ON FUNCTION fn_invitation_accept(text, uuid, text, text, bigint, text) IS
  'The pre-membership door (059): accept a pending, unexpired invitation by its token hash, with '
  'the D33 identity proof computed here. Refuses (no_data_found) an invitation that is no longer '
  'legitimate (098): its workspace is not active, its inviter is no longer an owner or admin '
  'there with an active account, or the acceptor was removed from the workspace after it was '
  'sent. SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;
