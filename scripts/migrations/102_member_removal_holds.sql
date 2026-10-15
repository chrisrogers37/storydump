-- Migration 102: a removal holds. The remove door checks its own caller, the removal record is
-- the membership doors' alone, earlier removals are recorded, and the join door names pg_temp
-- last on its search_path. Appended to the advertised stream as `07` §45.
--
-- THE INVARIANT. A member is removed only by an owner or admin of their workspace, acting in that
-- workspace, and the removal record (`workspace_member_removals`, 090) is read and written by the
-- membership doors alone.
--
-- THE REMOVE DOOR'S OWN CHECK. `fn_member_remove` refuses, before anything else, a workspace that
-- is not the caller's claimed tenant (`app.tenant_id`) or a remover who is not an owner or admin
-- of it. The command port already holds its caller to both, and the door now checks them itself.
-- The refusal is a RAISE known by its message, as `fn_offboard_finalize`'s are: SQLSTATE 42501
-- stays the grant and policy denials' alone. CREATE OR REPLACE keeps the door's signature, its
-- owner (svc_membership) and its EXECUTE grant (svc_ingress); its comment is restated.
--
-- THE JOIN DOOR'S PATH. 068 and 090 pinned `fn_group_member_seen` to `pg_catalog, public`.
-- PostgreSQL searches pg_temp first for relations unless the path names it, so this file names it
-- last, as `fn_member_remove`'s path already does: every unqualified name in the door resolves in
-- pg_catalog or public. ALTER FUNCTION ... SET changes the setting alone and leaves 090's body as
-- it is.
--
-- THE RECORD IS THE DOORS' ALONE. 090 granted svc_ingress and svc_worker SELECT on the record
-- under a tenant read policy (`p_tenant_read`). Nothing outside the two doors reads it, and the
-- doors run as svc_membership, so this file revokes that SELECT and drops that policy:
-- svc_membership's grant and its row-open policy are the table's only access. RLS stays enabled.
--
-- EARLIER REMOVALS. The backfill reads the governance audit trail (entity_kind 'member', op
-- DELETE, with the workspace and the person). For everyone who is not a member again and whose
-- workspace and account still exist, it takes the latest removal and records it when another
-- person made it (actor_kind 'user', not the member themselves), naming the remover where that
-- account still exists. A record the door already wrote is kept.
--
-- THE INVITATIONS THEY OUTRANK. After the backfill, 098's one-time revoke statement runs again,
-- unchanged: a pending invitation whose inviter has a removal record in its workspace and is not
-- a member there again, and one addressed (by email or Telegram id) to a person removed from its
-- workspace after it was sent, are revoked. The doors refuse both (098); revoking them keeps the
-- invitation listing filtering on state alone. workspace_invitations carries no governance
-- trigger, so the UPDATE needs no actor.
--
-- OUTSIDE THIS FILE. `workspaces.remove_member` retires the removed person's live link states for
-- the workspace in the same unit of work (`oauth_states.retire_live_states`, by person and
-- workspace, whatever the provider).
--
-- The door changes sit inside a CREATE bracket on svc_membership, as 090's do.
--
-- DEPLOY ORDER: either. Nothing in the code reads the record directly, and the door changes need
-- no code.
--
-- Adoption evidence (#997): the remove door's body by the claim it now reads, and the join door's
-- pinned path, both catalog state this file creates. The revoked grant and the dropped policy
-- are absences, and the backfill and the invitation revoke are data, so none is probed.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_member_remove' AND position('app.tenant_id' IN p.prosrc) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_group_member_seen' AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig))

REVOKE SELECT ON workspace_member_removals FROM svc_ingress, svc_worker;

DROP POLICY p_tenant_read ON workspace_member_removals;

INSERT INTO workspace_member_removals (workspace_id, user_id, removed_by_user_id, removed_at)
SELECT latest.workspace_id, latest.entity_id, u.id, latest.created_at
  FROM (SELECT DISTINCT ON (e.workspace_id, e.entity_id)
               e.workspace_id, e.entity_id, e.actor_kind, e.actor_user_id, e.created_at
          FROM audit_events e
         WHERE e.entity_kind = 'member'
           AND e.detail ->> 'op' = 'DELETE'
         ORDER BY e.workspace_id, e.entity_id, e.created_at DESC, e.id DESC) latest
  LEFT JOIN users u ON u.id = latest.actor_user_id
 WHERE latest.actor_kind = 'user'
   AND latest.actor_user_id IS DISTINCT FROM latest.entity_id
   AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = latest.workspace_id)
   AND EXISTS (SELECT 1 FROM users p WHERE p.id = latest.entity_id)
   AND NOT EXISTS (SELECT 1 FROM workspace_members m
                    WHERE m.workspace_id = latest.workspace_id AND m.user_id = latest.entity_id)
ON CONFLICT (workspace_id, user_id) DO NOTHING;

UPDATE workspace_invitations i SET state = 'revoked'
 WHERE i.state = 'pending'
   AND (EXISTS (SELECT 1 FROM workspace_member_removals r
                 WHERE r.workspace_id = i.workspace_id AND r.user_id = i.invited_by_user_id
                   AND NOT EXISTS (SELECT 1 FROM workspace_members m
                                    WHERE m.workspace_id = i.workspace_id
                                      AND m.user_id = i.invited_by_user_id))
        OR EXISTS (SELECT 1 FROM workspace_member_removals r
                     JOIN users u ON u.id = r.user_id
                    WHERE r.workspace_id = i.workspace_id AND r.removed_at >= i.created_at
                      AND (lower(u.primary_email) = lower(i.email)
                           OR i.invited_tg_user_id::text IN
                              (SELECT x.external_id FROM user_identities x
                                WHERE x.user_id = r.user_id AND x.provider = 'telegram'))));

GRANT CREATE ON SCHEMA public TO svc_membership;

ALTER FUNCTION fn_group_member_seen(text, text, uuid)
  SET search_path = pg_catalog, public, pg_temp;

CREATE OR REPLACE FUNCTION fn_member_remove(p_workspace uuid, p_user uuid, p_by_user uuid)
RETURNS TABLE (o_outcome text, o_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_role text;
BEGIN
  IF p_workspace IS DISTINCT FROM NULLIF(current_setting('app.tenant_id', true), '')::uuid
     OR NOT EXISTS (SELECT 1 FROM workspace_members a
                     WHERE a.workspace_id = p_workspace AND a.user_id = p_by_user
                       AND a.role IN ('owner', 'admin')) THEN
    RAISE EXCEPTION 'member removal outside the caller''s admin scope';
  END IF;
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
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

COMMENT ON FUNCTION fn_member_remove(uuid, uuid, uuid) IS
  'The revoke for every join edge (06): an admin removes a member explicitly, and the removal is '
  'recorded so the Telegram join path cannot undo it (090). The one DELETE on workspace_members '
  'in the system lives here (057: no login role deletes). The door checks its caller itself '
  '(102): p_workspace must be the claimed tenant (app.tenant_id) and p_by_user an owner or admin '
  'of it, or it raises. Outcomes: removed, not_found, owner (never removable here), self (never '
  'through this door). SECURITY DEFINER owned by svc_membership with EXECUTE granted to '
  'svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;
