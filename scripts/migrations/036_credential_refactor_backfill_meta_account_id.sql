-- Migration 036: Backfill api_tokens.meta_account_id from instagram_accounts
-- Phase 3 of Instagram credential refactor (#380).
-- Populates the new column for all existing tokens that were created
-- before the dual-write (Phase 2) was deployed.

BEGIN;

UPDATE api_tokens t
SET meta_account_id = ia.instagram_account_id
FROM instagram_accounts ia
WHERE t.instagram_account_id = ia.id
  AND t.meta_account_id IS NULL
  AND ia.instagram_account_id IS NOT NULL;

INSERT INTO schema_version (version, description)
VALUES (36, 'Backfill api_tokens.meta_account_id from instagram_accounts');

COMMIT;
