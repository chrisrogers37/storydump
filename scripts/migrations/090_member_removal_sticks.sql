-- Migration 090: a removal sticks, and a removed admin's tokens die with the membership
-- (real-user readiness review, 2026-10-02). Appended to the advertised stream as `07` §33.
--
-- A REMOVAL WAS UNDONE BY THE NEXT GROUP MESSAGE. fn_member_remove (068) deletes the
-- membership row, and fn_group_member_seen (068) inserts a member for any linked user seen in
-- the bound group. Nothing told the second door about the first, so a person an admin removed,
-- who was still in the group, was a member again the next time they spoke there, with the web,
-- token and tap access that comes with it. workspace_member_removals is the record of the
-- removal: fn_member_remove writes it beside the delete, and fn_group_member_seen refuses to
-- join a removed person by name (`removed`). An invitation is the way back in: the accept door
-- is untouched, and once the person is a member again the record is inert (the join door reads
-- it only for a non-member, and a second removal stamps it again).
--
-- A REMOVED ADMIN'S SERVICE TOKENS OUTLIVED THEM. A workspace service identity carried no
-- minter, so a removal could not find the tokens the removed person made, and nobody could
-- tell whose a token was. service_tokens.created_by_user_id records the person who minted a
-- workspace-subject token (NULL for every token minted before this file, and for a
-- person-bound token, whose user_id already says it); the removal revokes the minter's live
-- workspace tokens in the same transaction (`workspaces.remove_member`).
--
-- The table is tenant-plane: RLS on, a tenant read policy for the two runtime roles (the
-- runtime never writes it), and SELECT-INSERT-UPDATE for svc_membership, the owner of both
-- doors, under a row-open policy as 068 gave it on workspace_members. Both doors are replaced
-- in place (CREATE OR REPLACE keeps their owner and grants), inside 062's CREATE bracket, and
-- fn_member_remove takes `pg_temp` last on its search_path as fn_group_member_seen already did.
--
-- Adoption evidence (#997): the table with RLS enabled, the column, the two doors' new text,
-- and svc_membership's write grant — catalog state this file creates.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'workspace_member_removals' AND c.relrowsecurity)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'service_tokens' AND column_name = 'created_by_user_id')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_group_member_seen' AND position('workspace_member_removals' IN p.prosrc) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_member_remove' AND position('workspace_member_removals' IN p.prosrc) > 0)
-- runner:postcondition SELECT has_table_privilege('svc_membership', 'workspace_member_removals', 'INSERT') AND NOT has_table_privilege('svc_ingress', 'workspace_member_removals', 'INSERT')

CREATE TABLE workspace_member_removals (
  workspace_id       uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  removed_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  removed_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);

ALTER TABLE workspace_member_removals ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON workspace_member_removals TO svc_ingress, svc_worker;
GRANT SELECT, INSERT, UPDATE ON workspace_member_removals TO svc_membership;

CREATE POLICY p_tenant_read ON workspace_member_removals FOR SELECT TO svc_ingress, svc_worker
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
CREATE POLICY p_member_removals ON workspace_member_removals FOR ALL TO svc_membership
  USING (true) WITH CHECK (true);

ALTER TABLE service_tokens
  ADD COLUMN created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL;

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE OR REPLACE FUNCTION fn_group_member_seen(p_channel text, p_external_ref text, p_user uuid)
RETURNS TABLE (o_workspace_id uuid, o_outcome text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
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
  IF EXISTS (SELECT 1 FROM workspace_member_removals r
              WHERE r.workspace_id = v_ws AND r.user_id = p_user)
     AND NOT EXISTS (SELECT 1 FROM workspace_members m
                      WHERE m.workspace_id = v_ws AND m.user_id = p_user) THEN
    RETURN QUERY SELECT v_ws, 'removed'::text; RETURN;
  END IF;
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', 'telegram', true);
  BEGIN
    INSERT INTO workspace_members (workspace_id, user_id, role)
    VALUES (v_ws, p_user, 'member')
    ON CONFLICT (workspace_id, user_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
  EXCEPTION WHEN foreign_key_violation THEN
    RETURN QUERY SELECT v_ws, 'unknown_user'::text; RETURN;
  END;
  RETURN QUERY SELECT v_ws, CASE WHEN v_inserted > 0 THEN 'joined' ELSE 'already_member' END;
END $$;

COMMENT ON FUNCTION fn_group_member_seen(text, text, uuid) IS
  'The 06 Telegram join path: a person seen in a bound, active group becomes a member (role '
  'member; never a downgrade), unless an admin removed them and they have not been invited back '
  '(090). p_user is the caller''s resolution of the sender''s linked Telegram identity — the '
  'caller proves the person, this door trusts it. Sets the actor GUCs transaction-locally, so it '
  'must be the last governance write in its transaction. SECURITY DEFINER owned by '
  'svc_membership with EXECUTE granted to svc_ingress. Outcomes: joined, already_member, '
  'removed, unbound_chat, revoked_chat, workspace_inactive, unknown_user.';

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
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

COMMENT ON FUNCTION fn_member_remove(uuid, uuid, uuid) IS
  'The revoke for every join edge (06): an admin removes a member explicitly, and the removal is '
  'recorded so the Telegram join path cannot undo it (090). The one DELETE on workspace_members '
  'in the system lives here (057: no login role deletes). p_by_user is the command port''s actor, '
  'already held to the admin floor — the caller proves the admin. Outcomes: removed, not_found, '
  'owner (never removable here), self (never through this door). SECURITY DEFINER owned by '
  'svc_membership with EXECUTE granted to svc_ingress.';

REVOKE CREATE ON SCHEMA public FROM svc_membership;
