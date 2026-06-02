-- Migration: 040_credential_refactor_expand_unique_constraint.sql
-- Description: Expand api_tokens UNIQUE constraint to include auth_method
-- Created: 2026-06-02
-- Issue: #468, #380 acceptance criteria
--
-- Today's UNIQUE (service_name, token_type, instagram_account_id)
-- prevents an account from holding both an instagram_login token AND
-- an fb_login token simultaneously. Issue #380's acceptance criteria
-- explicitly call this out: "An account can have a fb_login token
-- AND an instagram_login token simultaneously."
--
-- After this constraint expands to include auth_method, the consumer
-- services (InstagramAPIService etc.) can pick the right token by
-- (account, auth_method) without ambiguity. The old constraint name
-- is preserved logically by the new one — same role, more selective
-- key.
--
-- Safe to run: no existing rows can violate the new constraint
-- (every IG token had a unique (service, type, account) under the
-- old rule, so adding auth_method to the tuple cannot create
-- duplicates).

ALTER TABLE api_tokens
  DROP CONSTRAINT IF EXISTS unique_service_token_type_account;

ALTER TABLE api_tokens
  ADD CONSTRAINT unique_credential_per_account
  UNIQUE (service_name, token_type, instagram_account_id, auth_method);

INSERT INTO schema_version (version, description, applied_at)
VALUES (40, 'Expand api_tokens UNIQUE to include auth_method', NOW())
ON CONFLICT DO NOTHING;
