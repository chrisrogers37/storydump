-- Migration 115: an invited person sees their own invitations and can have the link sent again
-- (owner ruling 2026-10-11: invitations travel by email). Appended to the advertised stream as
-- `07` §58.
--
-- SIGNING IN WITHOUT THE LINK FOUND NOTHING. Someone invited by email who signed in from the front
-- door rather than the link was let in (fn_signup_admitted, 092 and 098) and then asked to name a
-- workspace. Nothing told them an invitation was waiting, and nothing could: an invitation is its
-- workspace's row (058's tenant policy), and they are not yet a member of it. The token is still
-- the only way in (D33). These doors show the invitation and mint a fresh link for its own
-- address; neither accepts one.
--
-- fn_invitations_for(p_user, p_email, p_limit) lists the invitations the accept door would still
-- honour for that address: email invitations, pending and unexpired, addressed to p_email (lower()
-- on both sides), from an active workspace whose inviter is still an owner or admin there with an
-- active account (098's standing), in a workspace p_user does not already belong to. Newest first,
-- at most p_limit. It answers each invitation's id, workspace name, role,
-- inviter and expiry, never its token hash. There is no removal clause: a removal revokes the
-- removed person's pending invitations to that workspace when it happens (098), so none is left
-- pending to list.
--
-- fn_invitation_reissue(p_invitation_id, p_email, p_token_hash) stores a fresh token's hash on one
-- invitation, in place, in one UPDATE whose WHERE holds every guard (pending, unexpired, email,
-- addressed to p_email), and answers whether it did. An accept or a revoke that commits first
-- therefore wins, and the rotation finds no row. created_at and expires_at are kept, so a later
-- removal still outranks the invitation and a re-send never extends it.
--
-- THE API SENDS FIRST AND ROTATES SECOND. It re-sends only an invitation the list shows the
-- caller (so standing is judged there), spends the day's budget, sends the email carrying the new
-- link, and calls this door only once the provider has taken it. A send that fails or times out
-- therefore leaves the old link working; the old link stops working when the rotation commits.
--
-- THE LIST IS INDEXED. Both pages that show it ask on every visit, and invitations are never
-- deleted (the reaper only marks them expired), so an unindexed lower(email) would scan every
-- invitation on the platform each time. ix_invite_pending_email covers lower(email) over pending
-- rows; fn_signup_admitted (098) reads the same predicate, and the index serves it too.
--
-- THE CALLER PROVES THE PERSON, as fn_invitation_accept's does: p_user is the session's user, and
-- p_email is that user's verified address, which svc_ingress reads from users (user-plane, open
-- to it). svc_membership, the doors' owner, already reads and updates workspace_invitations and
-- reads workspaces, workspace_members and users' id and state (057, 058, 098), so no grant changes.
-- workspace_invitations carries no audit trigger, so the reissue sets no actor. The CREATE bracket
-- is 062's.
--
-- Adoption evidence (#997): both doors by name, owned by svc_membership as SECURITY DEFINER,
-- svc_ingress's EXECUTE on each, and the index by name: catalog state this file creates. The probes join the catalogs by
-- name, so they read false without raising on a database that lacks a role or a door.
--
-- runner:postcondition SELECT count(*) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname IN ('fn_invitations_for', 'fn_invitation_reissue') AND p.prosecdef AND r.rolname = 'svc_membership'
-- runner:postcondition SELECT count(DISTINCT p.proname) = 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname IN ('fn_invitations_for', 'fn_invitation_reissue') AND r.rolname = 'svc_ingress' AND a.privilege_type = 'EXECUTE'
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'workspace_invitations' AND indexname = 'ix_invite_pending_email')

-- [§58 an invited person's own invitations: the ones the accept door would honour, and a fresh link to the invited address]
-- Two definer doors owned by svc_membership, EXECUTE for svc_ingress alone. The caller proves the
-- person: p_user is the session's user and p_email their verified address. Neither door accepts
-- an invitation or answers a token. The list is indexed on lower(email) over pending invitations.

CREATE INDEX ix_invite_pending_email ON workspace_invitations (lower(email))
  WHERE state = 'pending';

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_invitations_for(p_user uuid, p_email text, p_limit integer)
RETURNS TABLE (o_invitation_id uuid, o_workspace_name text, o_role text, o_invited_by uuid,
               o_expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.id, w.name, i.role, i.invited_by_user_id, i.expires_at
    FROM workspace_invitations i
    JOIN workspaces w ON w.id = i.workspace_id AND w.state = 'active'
    JOIN workspace_members a ON a.workspace_id = i.workspace_id
                            AND a.user_id = i.invited_by_user_id
                            AND a.role IN ('owner', 'admin')
    JOIN users u ON u.id = i.invited_by_user_id AND u.state = 'active'
   WHERE i.delivery_channel = 'email' AND lower(i.email) = lower(p_email)
     AND i.state = 'pending' AND i.expires_at > now()
     AND NOT EXISTS (SELECT 1 FROM workspace_members m
                      WHERE m.workspace_id = i.workspace_id AND m.user_id = p_user)
   ORDER BY i.created_at DESC, i.id
   LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_invitations_for(uuid, text, integer) IS
  'The invitations an invited person could still accept (115): email invitations, pending and '
  'unexpired, addressed to p_email (lower() on both sides), from an active workspace whose inviter '
  'is still its owner or an admin with an active account, in a workspace p_user does not belong '
  'to. Newest first, at most p_limit. Answers the id, workspace name, role, '
  'inviter and expiry, never the token hash. The caller proves the person. SECURITY DEFINER owned '
  'by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_invitations_for(uuid, text, integer) OWNER TO svc_membership;

CREATE FUNCTION fn_invitation_reissue(p_invitation_id uuid, p_email text, p_token_hash text)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  WITH rotated AS (
    UPDATE workspace_invitations i SET token_hash = p_token_hash
     WHERE i.id = p_invitation_id
       AND i.delivery_channel = 'email' AND lower(i.email) = lower(p_email)
       AND i.state = 'pending' AND i.expires_at > now()
    RETURNING i.id
  )
  SELECT EXISTS (SELECT 1 FROM rotated)
$$;

COMMENT ON FUNCTION fn_invitation_reissue(uuid, text, text) IS
  'Email me the link again (115): stores a fresh token hash on one invitation, in place, when it '
  'is a pending, unexpired email invitation addressed to p_email (lower() on both sides); true '
  'when it did. Every guard is in the UPDATE''s own WHERE, so an accept or a revoke that commits '
  'first wins. created_at and expires_at are kept. The API calls it only after the email carrying '
  'the new link was sent, and only for an invitation fn_invitations_for lists for the caller, so '
  'a failed send leaves the old link working. The caller proves the person. SECURITY DEFINER '
  'owned by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_invitation_reissue(uuid, text, text) OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_invitations_for(uuid, text, integer) FROM PUBLIC;

REVOKE ALL ON FUNCTION fn_invitation_reissue(uuid, text, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_invitations_for(uuid, text, integer) TO svc_ingress;

GRANT EXECUTE ON FUNCTION fn_invitation_reissue(uuid, text, text) TO svc_ingress;
