-- Migration: 039_credential_refactor_backfill_auth_method.sql
-- Description: Backfill api_tokens.auth_method from instagram_accounts.auth_method
-- Created: 2026-06-02
-- Issue: #468, follows 038
--
-- Backfills the new api_tokens.auth_method from the legacy
-- instagram_accounts.auth_method column. After PR #462 (today's
-- production cleanup) prod has exactly one Instagram token row, and
-- that row's account.auth_method is 'instagram_login' — but this
-- migration is written defensively for any future deployment that
-- may have legacy fb_login rows or unset values.
--
-- Rows that don't join cleanly (no instagram_account_id FK, or the
-- account row has NULL auth_method) default to 'instagram_login'
-- because that's the only OAuth flow we still support for new
-- connections. Legacy fb_login tokens will need to be tagged via
-- explicit UPDATE if they ever resurface.

UPDATE api_tokens t
SET auth_method = COALESCE(a.auth_method, 'instagram_login')
FROM instagram_accounts a
WHERE t.instagram_account_id = a.id
  AND t.service_name = 'instagram'
  AND t.auth_method IS NULL;

-- Catch any orphan IG tokens with no account FK (legacy single-tenant
-- rows from before instagram_account_id existed). Same default.
UPDATE api_tokens
SET auth_method = 'instagram_login'
WHERE service_name = 'instagram'
  AND auth_method IS NULL;

INSERT INTO schema_version (version, description, applied_at)
VALUES (39, 'Backfill api_tokens.auth_method from instagram_accounts', NOW())
ON CONFLICT DO NOTHING;
