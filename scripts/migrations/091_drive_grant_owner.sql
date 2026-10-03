-- Migration 091: the workspace's Drive grant records who granted it (real-user readiness
-- review, 2026-10-02). Appended to the advertised stream as `07` §34.
--
-- ANY ADMIN COULD BROWSE ANOTHER PERSON'S DRIVE. The `gdrive` credential is the WORKSPACE's
-- (069, `07` §15), but what it carries is `drive.readonly` over the whole Drive of the person
-- who connected it, Shared with me included. The folder browser and the folder pick sat at the
-- admin floor, so every admin could walk that person's Drive and connect any folder in it.
-- oauth_credentials.granted_by_user_id names the person whose Google account the grant is:
-- the Drive connect callback writes it (the state's user, whom the callback has already
-- checked is the returning browser), a reconnect replaces it, and the browser and the pick
-- admit that person alone (`workspaces.may_browse_drive`). Every other admin still reads the
-- grant's status, the connected folders and their sync.
--
-- NULL for an `ig_login` credential. A `gdrive` grant made before this file takes its granter
-- from the audit trail (below); one the trail cannot name stays NULL, and a NULL-granter Drive
-- grant is browsable by the workspace's owner only, until a reconnect records a granter. ON DELETE SET NULL, so a deleted user's grant falls back to that rule
-- rather than blocking the delete. The column rides the table's existing grants and policies
-- (057, 058): the runtime roles already write the row, and svc_clock's column-level SELECT
-- does not name it.
--
-- Adoption evidence (#997): the column, and its reference to users — catalog state this file
-- creates.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'oauth_credentials' AND column_name = 'granted_by_user_id' AND data_type = 'uuid')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'public.oauth_credentials'::regclass AND contype = 'f' AND confrelid = 'public.users'::regclass AND confdeltype = 'n')

ALTER TABLE oauth_credentials
  ADD COLUMN granted_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL;

-- Name the granter of every grant made before this file from the audit trail (055's
-- tg_audit_oauth_credentials, 085): the latest write a person made that left the grant active
-- is the connect or reconnect, by the person whose Google account it is. Refreshes and the
-- read door write as `system`, a disconnect leaves it `revoked`, and this UPDATE changes no
-- audited column, so it writes no audit row of its own. The audit trail outlives a deleted
-- user, so only an actor who still exists is named; a grant with no such row stays NULL.
UPDATE oauth_credentials c
   SET granted_by_user_id = (
         SELECT e.actor_user_id FROM audit_events e
          WHERE e.entity_kind = 'credential' AND e.entity_id = c.id
            AND e.actor_kind = 'user' AND e.to_state = 'active'
            AND EXISTS (SELECT 1 FROM users u WHERE u.id = e.actor_user_id)
          ORDER BY e.id DESC
          LIMIT 1)
 WHERE c.provider = 'gdrive' AND c.granted_by_user_id IS NULL;
