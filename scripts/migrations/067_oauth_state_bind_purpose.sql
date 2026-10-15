-- Migration 067: the `bind` purpose on `oauth_states` (#1175 D-3 / D-4, owner
-- ruling 2026-09-05; `07` §13).
--
-- A Telegram group is bound to a workspace by a one-shot link an admin mints
-- from Settings: `t.me/<bot>?startgroup=bind-<state>` opens Telegram's group
-- picker, Telegram adds the bot and sends `/start bind-<state>` in the chosen
-- group, and the `/start` door consumes the state and writes the
-- `channel_bindings` row for THAT chat and the pinned workspace. The same
-- flow binds the second and the tenth group (D13's 0..n).
--
-- The state row pins both the user who minted it and the workspace — the
-- ELSE branch of `ck_oauth_state_context` already demands both for every
-- purpose that is not signin or link, so only the purpose vocabulary widens.
--
-- WIDENING A CHECK IS SAFE ON EXISTING ROWS by construction.

-- The bind purpose (#1175 D-3, owner ruling 2026-09-05): an admin's one-shot
-- `startgroup` link binds the group it is opened in to the pinned workspace.
-- `ck_oauth_state_context`'s ELSE branch already requires BOTH user_id and
-- workspace_id for any purpose that is not signin or link, which is exactly
-- what a bind state must pin. Drop-and-add is the repo's shape for a CHECK
-- edit (042, 045, 046, 049, 065).
ALTER TABLE oauth_states DROP CONSTRAINT ck_oauth_state_purpose;
ALTER TABLE oauth_states ADD CONSTRAINT ck_oauth_state_purpose
  CHECK (purpose IN ('connect','reconnect','signin','link','bind'));

-- runner:postcondition SELECT pg_get_constraintdef(oid) LIKE '%bind%' FROM pg_constraint WHERE conname = 'ck_oauth_state_purpose'
