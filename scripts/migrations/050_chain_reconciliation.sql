-- Migration: 050_chain_reconciliation.sql
-- Description: Chain reconciliation (#712, plan 0.2 fix-forward) - align the
--   two known divergences between replay-from-empty and a long-lived
--   production database, after which the CI replay gate and production
--   describe the same schema.
--   (1) Drop the 004/008-era orphaned unique on api_tokens if present:
--       004 declares UNIQUE (service_name, token_type) inline, which
--       PostgreSQL auto-names api_tokens_service_name_token_type_key; 008
--       replaced the constraint but dropped it under the name
--       unique_service_token_type, so on replayed installs the auto-named
--       original survives alongside 040's unique_credential_per_account.
--   (2) Normalize chat_settings.caption_style to TEXT: the model declares
--       String(20) while migration 030 created TEXT - the live type depends
--       on whether an install ran init_db or the migration first, which is
--       exactly why this normalizes rather than assumes.
-- Both statements are idempotent (IF EXISTS; TYPE TEXT is a no-op on TEXT).
-- Gated ops checkbox before the first PRODUCTION run of this file: confirm
-- the residue analysis with \d api_tokens and \d media_posting_locks against
-- production (plan 0.2 precondition - unobservable from CI).
-- Created: 2026-08-11
-- Issue: #712
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'api_tokens_service_name_token_type_key')
-- runner:postcondition SELECT (SELECT data_type FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'chat_settings' AND column_name = 'caption_style') = 'text'

ALTER TABLE api_tokens DROP CONSTRAINT IF EXISTS api_tokens_service_name_token_type_key;

ALTER TABLE chat_settings ALTER COLUMN caption_style TYPE TEXT;

INSERT INTO schema_version (version, description, applied_at)
VALUES (50, 'Chain reconciliation: drop 004/008 orphaned unique; caption_style to TEXT (#712)', NOW())
ON CONFLICT DO NOTHING;
