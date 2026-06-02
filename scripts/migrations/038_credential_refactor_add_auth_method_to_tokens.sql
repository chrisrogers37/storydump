-- Migration: 038_credential_refactor_add_auth_method_to_tokens.sql
-- Description: Add auth_method + issuing_app_id columns to api_tokens
-- Created: 2026-06-02
-- Issue: #468, closes phase 4 of #380
--
-- Why this exists
-- ----------------
-- The IG host-routing bug fixed in PR #462 (today) revealed that
-- credential provenance lives on the wrong table. Today the posting
-- code has to JOIN instagram_accounts to discover which OAuth flow
-- issued each token; this PR migrates the discriminator onto the
-- credential row itself so the token becomes self-describing.
--
-- After this migration + the dual-write + read-switch + drop-legacy
-- PRs that follow, instagram_accounts becomes pure identity (username
-- + display_name + is_active) and api_tokens carries everything about
-- *the credential*: its OAuth flow (auth_method), its issuing app
-- (issuing_app_id), and the Meta-side account ID it returned
-- (meta_account_id, added in migration 035).
--
-- Additive only — no behavior changes, no data deleted. Backfill is
-- in migration 039 so this one is fully reversible.

ALTER TABLE api_tokens
  ADD COLUMN IF NOT EXISTS auth_method VARCHAR(50),
  ADD COLUMN IF NOT EXISTS issuing_app_id VARCHAR(100);

-- Partial index so the auth_method lookup is fast for IG tokens
-- without indexing the NULL google_drive / shopify rows.
CREATE INDEX IF NOT EXISTS api_tokens_auth_method_idx
  ON api_tokens (auth_method)
  WHERE auth_method IS NOT NULL;

INSERT INTO schema_version (version, description, applied_at)
VALUES (38, 'Add api_tokens.auth_method + issuing_app_id (credential refactor phase 4)', NOW())
ON CONFLICT DO NOTHING;
