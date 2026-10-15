-- Migration 068: the chat-inbound resolver door and the group-membership door (#854 (a),
-- #1242; the eleventh and twelfth `02` §7 doors). Appended to the advertised stream as
-- `07` §14.
--
-- fn_resolve_binding closes #854: svc_ingress has no pre-context read of channel_bindings
-- (p_tenant is workspace_id = app.tenant_id), so "which workspace is this chat" refused as
-- unknown_binding by measurement. A definer read exposing ONE row, not a lookup policy over
-- the whole table.
--
-- fn_group_member_seen is the `06` Telegram join path: a linked user seen in a bound, active
-- group becomes a member at role 'member' — never a downgrade, never a write for an unbound
-- chat or an inactive workspace. The actor GUCs are set inside the door so the governance
-- audit trigger on workspace_members attributes the row.
--
-- DEPLOY ORDER: the dispatcher's membership step and tenant_resolution.resolve_chat call
-- these doors unconditionally — apply this file BEFORE deploying the code that reads through
-- them.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_resolve_binding' AND p.prosecdef AND r.rolname = 'svc_membership')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_group_member_seen' AND p.prosecdef AND r.rolname = 'svc_membership')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_group_member_seen' AND r.rolname = 'svc_ingress' AND a.privilege_type = 'EXECUTE')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_resolve_binding' AND r.rolname = 'svc_ingress' AND a.privilege_type = 'EXECUTE')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(n.nspacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND r.rolname = 'svc_membership' AND a.privilege_type = 'CREATE')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_member_remove' AND p.prosecdef AND r.rolname = 'svc_membership')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_member_remove' AND r.rolname = 'svc_ingress' AND a.privilege_type = 'EXECUTE')
-- runner:postcondition SELECT has_table_privilege('svc_membership', 'workspace_members', 'DELETE') AND NOT has_table_privilege('svc_ingress', 'workspace_members', 'DELETE') AND NOT has_table_privilege('svc_worker', 'workspace_members', 'DELETE')

-- 11/13 fn_resolve_binding — the chat-inbound resolver door (#854 (a); #1242). p_tenant on
-- channel_bindings is workspace_id = app.tenant_id, so "which workspace is this chat" has no
-- pre-context read path for svc_ingress: the row exists and the read comes back EMPTY, and
-- resolve_chat refused as unknown_binding by measurement (the harness pinned it). A definer
-- read owned by svc_membership (already SELECT-true on channel_bindings) that exposes exactly
-- ONE row — the named chat's — where a lookup policy would expose every workspace's topology.
-- The CREATE bracket is 062's, for the reason it gave.
GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_resolve_binding(p_channel text, p_external_ref text)
RETURNS TABLE (o_workspace_id uuid, o_binding_id uuid, o_state text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
  SELECT b.workspace_id, b.id, b.state
    FROM channel_bindings b
   WHERE b.channel = p_channel AND b.external_ref = p_external_ref
$$;

COMMENT ON FUNCTION fn_resolve_binding(text, text) IS
  'Chat inbound -> workspace: the one row for a (channel, external_ref), any state, so the '
  'caller can tell revoked from never-bound. SECURITY DEFINER owned by svc_membership with '
  'EXECUTE granted to svc_ingress; exists because p_tenant on channel_bindings cannot answer a '
  'pre-context question about one chat.';

-- 12/13 fn_group_member_seen — the 06 Telegram join path (#1242): a person seen in a BOUND
-- group becomes a member at role ''member''. THE CALLER PROVES THE PERSON: p_user is the user
-- the ingress adapter resolved from the sender''s linked Telegram identity (user_identities is
-- role-open to it), and this door trusts that resolution the way the worker trusts
-- job.workspace_id — the contract is stated here rather than re-checked in-body. Never a
-- downgrade (ON CONFLICT DO NOTHING: an owner or admin stays what they are); never a write for
-- an unbound or revoked chat, an inactive workspace, or a user that vanished between
-- resolution and insert (a disabled user is the caller's refusal: users is user-plane to it). The actor GUCs are set HERE (transaction-LOCAL: this must be
-- the last governance write in its transaction) so the audit trigger attributes the row to the
-- joiner as ''user'' — the same convention fn_invitation_accept records for an acceptor.
CREATE FUNCTION fn_group_member_seen(p_channel text, p_external_ref text, p_user uuid)
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
  PERFORM set_config('app.actor_kind', 'user', true);
  PERFORM set_config('app.actor_user_id', p_user::text, true);
  PERFORM set_config('app.channel', 'telegram', true);
  BEGIN
    INSERT INTO workspace_members (workspace_id, user_id, role)
    VALUES (v_ws, p_user, 'member')
    ON CONFLICT (workspace_id, user_id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
  EXCEPTION WHEN foreign_key_violation THEN
    -- The user vanished between the caller's resolution and this insert:
    -- a named outcome, never a raise the ingress route would turn into a
    -- redelivery loop.
    RETURN QUERY SELECT v_ws, 'unknown_user'::text; RETURN;
  END;
  RETURN QUERY SELECT v_ws, CASE WHEN v_inserted > 0 THEN 'joined' ELSE 'already_member' END;
END $$;

COMMENT ON FUNCTION fn_group_member_seen(text, text, uuid) IS
  'The 06 Telegram join path: a person seen in a bound, active group becomes a member (role '
  'member; never a downgrade). p_user is the caller''s resolution of the sender''s linked '
  'Telegram identity — the caller proves the person, this door trusts it. Sets the actor GUCs '
  'transaction-locally, so it must be the last governance write in its transaction. SECURITY '
  'DEFINER owned by svc_membership with EXECUTE granted to svc_ingress. Outcomes: joined, '
  'already_member, unbound_chat, revoked_chat, workspace_inactive, unknown_user.';

-- 13/13 fn_member_remove — the revoke for every join edge (06: "an admin removes membership
-- explicitly"; #1242 review). The 057 grant matrix is structural about this: NO login role
-- deletes, a delete lives only inside a door body as its NOLOGIN owner — so the one DELETE
-- svc_membership receives here backs exactly this body. THE CALLER PROVES THE ADMIN: p_by_user
-- is the command port's actor, already held to the admin floor; the door refuses by name what
-- no admin may do — remove the owner (transfer_ownership is that edge) or remove themselves —
-- and answers not_found for a non-member. The actor GUCs are the caller's (a web command's
-- unit of work sets them), so the governance audit row names the admin who did it.
GRANT DELETE ON workspace_members TO svc_membership;

CREATE FUNCTION fn_member_remove(p_workspace uuid, p_user uuid, p_by_user uuid)
RETURNS TABLE (o_outcome text, o_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
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
  RETURN QUERY SELECT 'removed'::text, v_role;
END $$;

COMMENT ON FUNCTION fn_member_remove(uuid, uuid, uuid) IS
  'The revoke for every join edge (06): an admin removes a member explicitly. The one DELETE on '
  'workspace_members in the system lives here (057: no login role deletes). p_by_user is the '
  'command port''s actor, already held to the admin floor — the caller proves the admin. '
  'Outcomes: removed, not_found, owner (never removable here), self (never through this door). '
  'SECURITY DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_resolve_binding(text, text) OWNER TO svc_membership;
ALTER FUNCTION fn_group_member_seen(text, text, uuid) OWNER TO svc_membership;
ALTER FUNCTION fn_member_remove(uuid, uuid, uuid) OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_resolve_binding(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fn_group_member_seen(text, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION fn_member_remove(uuid, uuid, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_resolve_binding(text, text) TO svc_ingress;
GRANT EXECUTE ON FUNCTION fn_group_member_seen(text, text, uuid) TO svc_ingress;
GRANT EXECUTE ON FUNCTION fn_member_remove(uuid, uuid, uuid) TO svc_ingress;
