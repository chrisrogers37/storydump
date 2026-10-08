-- 108: oauth_states.encrypted_code_verifier — PKCE on the Drive connect flow (07 §51; RFC 7636).
-- Identical to the 07 §51 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT count(*) = 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'oauth_states' AND column_name = 'encrypted_code_verifier'

-- [§51 oauth_states: the PKCE code verifier, encrypted]
-- RFC 7636: the code verifier minted with a Drive connect state, stored as ciphertext under the
-- credential ring (§3) and never as itself. NULL on a state minted without one.
ALTER TABLE oauth_states ADD COLUMN encrypted_code_verifier TEXT NULL;
