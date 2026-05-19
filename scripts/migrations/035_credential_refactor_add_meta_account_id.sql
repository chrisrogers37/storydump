-- Migration 035: Add meta_account_id to api_tokens
-- Phase 1 of Instagram credential refactor (#380).
-- Additive only — no columns removed, no behavior changed.
--
-- api_tokens.meta_account_id stores the Meta-side identifier
-- (Business Account ID or Instagram User ID) that issued
-- the OAuth token. Moves identity out of token_metadata JSONB
-- into a proper indexed column for direct lookup.

BEGIN;

ALTER TABLE api_tokens
ADD COLUMN IF NOT EXISTS meta_account_id TEXT;

CREATE INDEX IF NOT EXISTS api_tokens_meta_account_id_idx
    ON api_tokens (meta_account_id)
    WHERE meta_account_id IS NOT NULL;

INSERT INTO schema_version (version, description)
VALUES (35, 'Add api_tokens.meta_account_id for credential refactor phase 1');

COMMIT;
