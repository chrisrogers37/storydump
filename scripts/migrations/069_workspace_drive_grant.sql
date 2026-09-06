-- 069: the workspace-level Drive grant (07 §15; owner ruling 2026-09-05; #1165 lean (b)).
-- Identical to the 07 §15 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_credentials_one_owner' AND conrelid = 'public.oauth_credentials'::regclass AND pg_get_constraintdef(oid) LIKE '%ig_login%')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_credential_per_workspace') AND NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_credential_per_source')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM oauth_credentials WHERE provider = 'gdrive' AND (media_source_id IS NOT NULL OR ig_account_id IS NOT NULL))

-- The workspace-level Drive grant (owner ruling 2026-09-05; #1165's lean (b) — the same
-- drive.readonly scope, the order inverted). ONE Google grant per workspace, reused by every
-- folder it picks: a gdrive credential names NO owner column — the workspace is its owner — so
-- the owner XOR becomes provider-conditional, and the per-source unique key becomes a
-- per-workspace one. media_source_id stays (nullable, its FK intact) with no writer: every
-- gdrive row is a workspace row from here on. The ADD CONSTRAINT validates the whole table, so
-- a per-source gdrive row anywhere would abort this file (DROP and ADD roll back together);
-- none exists — the target tier never completed a production Drive connect before this ruling.
ALTER TABLE oauth_credentials DROP CONSTRAINT ck_credentials_one_owner;

ALTER TABLE oauth_credentials ADD CONSTRAINT ck_credentials_one_owner CHECK (
  CASE provider
    WHEN 'ig_login' THEN ig_account_id IS NOT NULL AND media_source_id IS NULL
    ELSE                 ig_account_id IS NULL     AND media_source_id IS NULL
  END);

DROP INDEX uq_credential_per_source;

CREATE UNIQUE INDEX uq_credential_per_workspace ON oauth_credentials (workspace_id, provider)
  WHERE ig_account_id IS NULL AND media_source_id IS NULL;
