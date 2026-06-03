-- Migration: 041_credential_refactor_drop_legacy_auth_method.sql
-- Description: Drop instagram_accounts.auth_method (credential refactor phase 5)
-- Created: 2026-06-03
-- Issue: #468 PR 5, closes the auth_method portion of #380 phase 5
--
-- After PR 4 the application reads auth_method from api_tokens, not
-- from instagram_accounts. PRs 2-4 made the column unused for any
-- read path; the OAuth callbacks (PR 3) also stop writing to it in
-- the corresponding code change. This migration removes the
-- redundant column.
--
-- Pre-flight: before applying, confirm zero application reads:
--   git grep -nE "instagram_account.*auth_method|account\.auth_method" src/
-- should match only OAuth callbacks (which this migration's code
-- change removes the write from) and tests.
--
-- Reversible: re-add via
--   ALTER TABLE instagram_accounts ADD COLUMN auth_method VARCHAR(20);
-- and re-backfill from api_tokens.auth_method if needed.

ALTER TABLE instagram_accounts
  DROP COLUMN IF EXISTS auth_method;

INSERT INTO schema_version (version, description, applied_at)
VALUES (41, 'Drop instagram_accounts.auth_method (now on api_tokens, #468 PR 5)', NOW())
ON CONFLICT DO NOTHING;
