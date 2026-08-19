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
-- IDEMPOTENT IS NOT PRIVILEGE-FREE, and conflating the two is what #787 was
-- about: PostgreSQL checks table ownership BEFORE it decides a statement has
-- nothing to do, so as svc_migration both statements fail `must be owner of
-- table` even against an already-normalized database. Measured, not reasoned.
--
-- WHY THE BODY IS A CALL AND NOT THE TWO ALTERs (#787, ruling option (d)).
-- Window step 3b applies this file as svc_migration. ALTER requires table
-- ownership or membership in the owning role and is NOT a grantable privilege,
-- so no bootstrap GRANT can make the raw statements legal for that actor. The
-- ratified mechanism is a SECURITY DEFINER function owned by the database-owner
-- actor, which scopes the elevation to these two statements instead of to a
-- role. The statements themselves are unchanged and live, verbatim and
-- schema-qualified, in the door's body.
--
-- THIS FILE THEREFORE DEPENDS ON A STEP-0 ARTIFACT, which is a real cost and is
-- stated rather than buried: scripts/window/step0_legacy_ddl_door.sql must be
-- applied by the owner actor before this file runs, in EVERY world that runs it
-- -- the window (via the step-0 bootstrap) and CI's owner-world replays alike.
-- A bare `psql -f` of this file against a database without the door fails on an
-- undefined function. It is the only file in the legacy lineage that is not
-- self-contained.
--
-- IN THE EXPECTED WORLD THIS FILE NEVER EXECUTES AT ALL. Its two postconditions
-- below are its adoption probe, and a production that already satisfies them is
-- ledgered `adopted` at first contact with no SQL sent -- so the door exists for
-- the world where 050 is genuinely pending, not for the one production is
-- believed to be in.
--
-- Gated ops checkbox before the first PRODUCTION run of this file: confirm
-- the residue analysis with \d api_tokens and \d media_posting_locks against
-- production (plan 0.2 precondition - unobservable from CI).
-- Created: 2026-08-11
-- Issue: #712
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'api_tokens_service_name_token_type_key')
-- runner:postcondition SELECT (SELECT data_type FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'chat_settings' AND column_name = 'caption_style') = 'text'

SELECT window_ddl.fn_050_chain_reconciliation();

INSERT INTO schema_version (version, description, applied_at)
VALUES (50, 'Chain reconciliation: drop 004/008 orphaned unique; caption_style to TEXT (#712)', NOW())
ON CONFLICT DO NOTHING;
