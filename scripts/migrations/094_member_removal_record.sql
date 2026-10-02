-- Migration 094: a removal holds. The join door honours a removal record, and the remove
-- door checks its own caller. Appended to the advertised stream as `07` §37.
--
-- THE INVARIANT. A person an admin removed returns only through an invitation: activity in
-- a bound group does not add them back.
--
-- THE RECORD. `workspace_member_removals` holds one row per (workspace, person) an admin has
-- removed, written by `fn_member_remove` beside the delete; a later removal of the same person
-- updates its remover. It is never cleared: a membership row outranks it, so an accepted
-- invitation (`fn_invitation_accept`, unchanged) makes the person a member again, and the
-- record matters again only once they are removed again. svc_membership, the doors' owner, is the one role with a grant on it. The
-- remover's foreign key is ON DELETE SET NULL, so deleting the remover's account keeps the
-- record; the workspace's and the person's cascade with them.
--
-- THE JOIN DOOR. `fn_group_member_seen` answers a member `already_member` first, however they
-- came back, and otherwise inserts only where no removal record exists for the workspace and
-- the person. When it inserts nothing for a non-member, it answers `removed_by_admin` and
-- writes nothing.
--
-- THE REMOVE DOOR'S OWN CHECK. `fn_member_remove` refuses, before anything else, a workspace
-- that is not the caller's claimed tenant (`app.tenant_id`) or a remover who is not an owner
-- or admin of it. The command port already holds its caller to both, and the door now checks
-- them itself. The refusal is a RAISE known by its message, as `fn_offboard_finalize`'s are:
-- SQLSTATE 42501 stays the grant and policy denials' alone.
--
-- Both doors keep their signatures, their owner (svc_membership) and their EXECUTE grant
-- (svc_ingress), which CREATE OR REPLACE carries. Both now pin search_path to
-- pg_catalog, public, pg_temp.
--
-- EARLIER REMOVALS. A removal made before this file left only its governance audit row
-- (entity_kind 'member', op DELETE, with the workspace and the person: 055, 085). The
-- backfill records the latest removal made by a person (actor_kind 'user') for everyone who
-- is not a member again and whose workspace and account still exist. A removal whose audit
-- row retention has already deleted (`fn_retention_batch`) leaves nothing to read, so it
-- stays unrecorded.
--
-- DEPLOY ORDER: either. The code that reads `removed_by_admin` handles it whenever it
-- appears, and the door changes need no code.
--
-- Adoption evidence (#997): the table by name with RLS on, svc_membership's UPDATE grant on
-- it, the remover's SET NULL key, each door's body by the text this file introduces, and the
-- two doors' pinned search_path.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'workspace_member_removals' AND c.relrowsecurity)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND c.relname = 'workspace_member_removals' AND r.rolname = 'svc_membership' AND a.privilege_type = 'UPDATE')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'workspace_member_removals' AND k.contype = 'f' AND k.confdeltype = 'n')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_group_member_seen' AND position('workspace_member_removals' IN p.prosrc) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_member_remove' AND position('app.tenant_id' IN p.prosrc) > 0 AND position('workspace_member_removals' IN p.prosrc) > 0)
-- runner:postcondition SELECT count(*) >= 2 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname IN ('fn_group_member_seen', 'fn_member_remove') AND 'search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig)

CREATE TABLE workspace_member_removals (
  workspace_id       UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id            UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  removed_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);

CREATE TRIGGER tg_touch_workspace_member_removals BEFORE UPDATE ON workspace_member_removals
  FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();

COMMENT ON TABLE workspace_member_removals IS
  'One row per (workspace, person) an admin removed, written by fn_member_remove; a later '
  'removal updates its remover. fn_group_member_seen adds nobody it names, but a membership row '
  'outranks it, so an accepted invitation re-admits the person. Never cleared.';

ALTER TABLE workspace_member_removals ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE ON workspace_member_removals TO svc_membership;

CREATE POLICY p_member_removals ON workspace_member_removals FOR ALL TO svc_membership
  USING (true) WITH CHECK (true);

INSERT INTO workspace_member_removals
  (workspace_id, user_id, removed_by_user_id, created_at, updated_at)
SELECT DISTINCT ON (e.workspace_id, e.entity_id)
       e.workspace_id, e.entity_id, u.id, e.created_at, e.created_at
  FROM audit_events e
  LEFT JOIN users u ON u.id = e.actor_user_id
 WHERE e.entity_kind = 'member'
   AND e.detail ->> 'op' = 'DELETE'
   AND e.actor_kind = 'user'
   AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = e.workspace_id)
   AND EXISTS (SELECT 1 FROM users p WHERE p.id = e.entity_id)
   AND NOT EXISTS (SELECT 1 FROM workspace_members m
                    WHERE m.workspace_id = e.workspace_id AND m.user_id = e.entity_id)
 ORDER BY e.workspace_id, e.entity_id, e.created_at DESC;

CREATE OR REPLACE FUNCTION fn_group_member_seen(p_channel text, p_external_ref text, p_user uuid)
RETURNS TABLE (o_workspace_id uuid, o_outcome text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_ws uuid;
  v_state text;
  v_ws_state text;
  v_inserted int;
BEGIN
  SELECT b.workspace_id, b.state INTO v_ws, v_state
    FROM channel_bindings b
   WHERE b.channel = p_channel AND b.external_ref = p_external_ref;
  IF v_ws IS NULL THEN
    RETURN QUERY SELECT NULL::uuid, 'unbound_chat'::text; RETURN;
  END IF;
  IF v_state <> 'active' THEN
    RETURN QUERY SELECT v_ws, 'revoked_chat'::text; RETURN;
  END IF;
  SELECT w.state INTO v_ws_state FROM workspaces w WHERE w.id = v_ws;
  IF v_ws_state IS DISTINCT FROM 'active' THEN
    RETURN QUERY SELECT v_ws, 'workspace_inactive'::text; RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM workspace_members m
              WHERE m.workspace_id = v_ws AND m.user_id = p_user) THEN
    RETURN QUERY SELECT v_ws, 'already_member'::text; RETURN;
  END IF;
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', 'telegram', true);
  BEGIN
    INSERT INTO workspace_members (workspace_id, user_id, role)
    SELECT v_ws, p_user, 'member'
     WHERE NOT EXISTS (SELECT 1 FROM workspace_member_removals r
                        WHERE r.workspace_id = v_ws AND r.user_id = p_user)
    ON CONFLICT (workspace_id, user_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
  EXCEPTION WHEN foreign_key_violation THEN
    RETURN QUERY SELECT v_ws, 'unknown_user'::text; RETURN;
  END;
  RETURN QUERY SELECT v_ws, CASE
    WHEN v_inserted > 0 THEN 'joined'
    WHEN EXISTS (SELECT 1 FROM workspace_members m
                  WHERE m.workspace_id = v_ws AND m.user_id = p_user) THEN 'already_member'
    ELSE 'removed_by_admin'
  END;
END $$;

COMMENT ON FUNCTION fn_group_member_seen(text, text, uuid) IS
  'The 06 Telegram join path: a person seen in a bound, active group becomes a member (role '
  'member; never a downgrade), unless an admin removed them and no invitation has made them a '
  'member since (workspace_member_removals). p_user is the caller''s resolution of the sender''s '
  'linked Telegram identity — the caller proves the person, this door trusts it. Sets the actor '
  'GUCs transaction-locally, so it must be the last governance write in its transaction. '
  'SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress. Outcomes: '
  'joined, already_member, removed_by_admin, unbound_chat, revoked_chat, workspace_inactive, '
  'unknown_user.';

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
  DO UPDATE SET removed_by_user_id = EXCLUDED.removed_by_user_id;
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

COMMENT ON FUNCTION fn_member_remove(uuid, uuid, uuid) IS
  'The revoke for every join edge (06): an admin removes a member explicitly, and the removal '
  'holds against the join path until an invitation re-admits them (workspace_member_removals, '
  'written here). The one DELETE on workspace_members in the system lives here (057: no login '
  'role deletes). The command port holds its actor to the admin floor, and this door checks it '
  'again: p_workspace must be the claimed tenant and p_by_user an owner or admin of it, or it '
  'raises. Outcomes: removed, not_found, owner (never removable here), self (never through this '
  'door). SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';
