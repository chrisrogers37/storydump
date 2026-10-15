-- Migration 099: a person can unlink their own Telegram identity. Appended to the advertised
-- stream as `07` §42.
--
-- THERE WAS NO WAY BACK FROM A LINK. A user links Telegram from Settings (the `link` state, `07`
-- §2), and uq_user_provider then holds one Telegram identity per user, so a person who linked
-- the wrong Telegram account, or stopped using one, could neither replace nor remove it: the
-- grant matrix gives no runtime role DELETE on user_identities (057), and linking refuses a
-- second Telegram account by name. fn_identity_unlink is the one delete: it removes the user's
-- Telegram identity, and only while the user keeps another identity, so an account is never
-- left with no way to sign in. A Google identity is the sign-in identity and is never removed
-- here; the door refuses any provider but 'telegram' by raising.
--
-- What unlinking does not touch: memberships. A workspace joined from a Telegram group stays
-- joined (unlinking an identity is not leaving a workspace; removal is fn_member_remove's). What
-- it does change: that Telegram account now resolves to no Storydump user, so its card taps are
-- refused as unlinked and its group messages join nobody, until the person links again.
--
-- THE CALLER PROVES THE PERSON: p_user is the session's user (DELETE /api/v1/me/telegram), as
-- fn_group_member_seen trusts its caller's resolution. Outcomes: unlinked, not_linked (no
-- Telegram identity to remove), last_identity (it is the user's only identity; nothing is
-- removed). user_identities is user-plane and carries no audit trigger, so the door sets no
-- actor. svc_membership receives SELECT and DELETE on user_identities under a row-open policy,
-- as 068 gave it DELETE on workspace_members for fn_member_remove; the CREATE bracket is 062's.
--
-- Adoption evidence (#997): the door owned by svc_membership and granted to svc_ingress, and
-- svc_membership's delete — catalog state this file creates.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner WHERE n.nspname = 'public' AND p.proname = 'fn_identity_unlink' AND p.prosecdef AND r.rolname = 'svc_membership')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, aclexplode(p.proacl) a JOIN pg_roles r ON r.oid = a.grantee WHERE n.nspname = 'public' AND p.proname = 'fn_identity_unlink' AND r.rolname = 'svc_ingress' AND a.privilege_type = 'EXECUTE')
-- runner:postcondition SELECT has_table_privilege('svc_membership', 'user_identities', 'DELETE')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'user_identities' AND policyname = 'p_member_identities')

GRANT SELECT, DELETE ON user_identities TO svc_membership;

CREATE POLICY p_member_identities ON user_identities FOR ALL TO svc_membership
  USING (true) WITH CHECK (true);

GRANT CREATE ON SCHEMA public TO svc_membership;

CREATE FUNCTION fn_identity_unlink(p_user uuid, p_provider text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v_deleted int;
BEGIN
  IF p_provider IS DISTINCT FROM 'telegram' THEN
    RAISE EXCEPTION 'only a telegram identity is unlinked here, not %', p_provider
      USING ERRCODE = 'invalid_parameter_value';
  END IF;
  DELETE FROM user_identities i
   WHERE i.user_id = p_user AND i.provider = p_provider
     AND EXISTS (SELECT 1 FROM user_identities o
                  WHERE o.user_id = p_user AND o.provider <> p_provider);
  GET DIAGNOSTICS v_deleted = ROW_COUNT;
  IF v_deleted > 0 THEN
    RETURN 'unlinked';
  END IF;
  IF EXISTS (SELECT 1 FROM user_identities i
              WHERE i.user_id = p_user AND i.provider = p_provider) THEN
    RETURN 'last_identity';
  END IF;
  RETURN 'not_linked';
END $$;

COMMENT ON FUNCTION fn_identity_unlink(uuid, text) IS
  'A person unlinks their own Telegram identity (099). Removes the user''s telegram row in '
  'user_identities only while the user keeps another identity; refuses any other provider by '
  'raising. Memberships are untouched. p_user is the caller''s session user — the caller proves '
  'the person, this door trusts it. Outcomes: unlinked, not_linked, last_identity. SECURITY '
  'DEFINER owned by svc_membership with EXECUTE granted to svc_ingress.';

ALTER FUNCTION fn_identity_unlink(uuid, text) OWNER TO svc_membership;

REVOKE CREATE ON SCHEMA public FROM svc_membership;

REVOKE ALL ON FUNCTION fn_identity_unlink(uuid, text) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION fn_identity_unlink(uuid, text) TO svc_ingress;
