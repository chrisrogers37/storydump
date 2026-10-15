-- The history choice an Instagram connect carries in its one-shot state, and the applied result its callback records (#1644).
-- runner:postcondition SELECT count(*) = 4 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'oauth_states' AND column_name IN ('archive_entry','archive_use_history','archive_expected_mix_revision','archive_preview_version')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relname = 'oauth_connection_results' AND c.relkind = 'r' AND c.relrowsecurity)
-- runner:postcondition SELECT count(*) >= 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'oauth_connection_results'
ALTER TABLE oauth_states ADD COLUMN archive_entry TEXT NULL;
ALTER TABLE oauth_states ADD COLUMN archive_use_history BOOLEAN NULL;
ALTER TABLE oauth_states ADD COLUMN archive_expected_mix_revision BIGINT NULL;
ALTER TABLE oauth_states ADD COLUMN archive_preview_version INTEGER NULL;
ALTER TABLE oauth_states ADD CONSTRAINT ck_oauth_state_archive_values CHECK (archive_entry IN ('add','first_connect','reconnect') AND archive_expected_mix_revision >= 0 AND archive_preview_version = 1);
-- A choice is all three facts or none, a reconnect carries none, and the entry names the start: Add pins no destination, a row start pins one.
ALTER TABLE oauth_states ADD CONSTRAINT ck_oauth_state_archive_choice CHECK ((archive_use_history IS NULL) = (archive_expected_mix_revision IS NULL) AND (archive_use_history IS NULL) = (archive_preview_version IS NULL) AND (archive_use_history IS NULL OR archive_entry IN ('add','first_connect')) IS TRUE AND (archive_entry IS NULL OR (provider = 'ig_login' AND (archive_entry = 'add') = (reconnect_target IS NULL))));

CREATE TABLE oauth_connection_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id UUID NOT NULL REFERENCES session_tokens(id) ON DELETE CASCADE,
  ig_account_id UUID NOT NULL,
  source_id UUID NULL,
  outcome TEXT NOT NULL,
  history_enabled BOOLEAN NOT NULL,
  mix_revision BIGINT NOT NULL,
  mix_reset_applied BOOLEAN NOT NULL,
  next_action TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT ck_connection_result_outcome CHECK (outcome IN ('connected_history_on','connected_history_off','connected_history_off_mix_conflict','reconnected_preserved')),
  CONSTRAINT ck_connection_result_next_action CHECK (next_action IS NOT DISTINCT FROM CASE WHEN outcome = 'connected_history_off_mix_conflict' THEN 'review_mix' END),
  CONSTRAINT ck_connection_result_revision CHECK (mix_revision >= 0),
  CONSTRAINT fk_connection_result_account FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts(workspace_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_connection_result_source FOREIGN KEY (workspace_id, source_id, ig_account_id) REFERENCES media_sources(workspace_id, id, origin_ig_account_id) ON DELETE CASCADE
);

-- The API's alone: the callback's tenant unit of work writes a result once, and the initiating member session reads it.
ALTER TABLE oauth_connection_results ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_ingress_connection_results ON oauth_connection_results FOR ALL TO svc_ingress
  USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
GRANT SELECT, INSERT ON oauth_connection_results TO svc_ingress;
