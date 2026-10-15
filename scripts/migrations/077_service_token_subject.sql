-- 077: a token's subject — the person-bound token beside the workspace service identity
-- (07 §23; plan 2026-09-15-cli-v2, phase 01).
-- Identical to the 07 §23 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'service_tokens' AND column_name = 'user_id'
-- runner:postcondition SELECT count(*) = 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'service_tokens' AND c.conname = 'ck_service_token_subject' AND c.contype = 'c'
-- runner:postcondition SELECT count(*) = 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND t.relname = 'service_tokens' AND c.relname = 'ix_service_tokens_user' AND i.indpred IS NOT NULL

-- [§23 service_tokens: the person-bound subject beside the workspace one]
-- A token acts for exactly one subject: a person (user_id set, workspace_id NULL — acts as that
-- person across their memberships, never above the membership role) or a workspace (a service
-- identity: workspace_id set, user_id NULL — reads its one workspace under its own name). The 060
-- reading of "workspace_id NULL = all workspaces (operator)" is retired: a token with no
-- workspace is a person's, and nothing acts across every workspace.
ALTER TABLE service_tokens ADD COLUMN user_id UUID NULL REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE service_tokens ADD CONSTRAINT ck_service_token_subject CHECK ((user_id IS NULL) <> (workspace_id IS NULL));
CREATE INDEX ix_service_tokens_user ON service_tokens (user_id) WHERE user_id IS NOT NULL;
