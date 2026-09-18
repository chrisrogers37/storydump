-- 079: M.3 step 3g — DROP SCHEMA legacy CASCADE (the legacy tear-out, phase
-- 04; forks F6 and F7; #1216). GATED: the deploy owes this file and never runs
-- it. The `manual` directive below is the runner's contract — `apply` skips
-- the file where it stands, reports it as owed and exits 0; only
--   python -m scripts.migration_runner apply --manual 79
-- applies it, run by the owner from
-- documentation/operations/legacy-window-close.md after the rehearsal on a
-- Neon PITR branch. It is exempt from the below-head rule in both doors, so
-- ordinary files numbered above it deploy freely while it waits.
--
-- WHAT IT NEEDS: 078 applied — every one of the sixteen snapshots present in
-- `archive` — and nothing written to `legacy` since (the tier that wrote there
-- was deleted in phase 01; production measured n_tup_ins/upd/del = 0 on all
-- sixteen on 2026-09-17). The DO block is the precondition, in-file: it
-- refuses the drop for a missing snapshot or a count that no longer matches
-- its source — an inventory error or a writer since 078 is data the drop
-- would destroy — and a refusal leaves `legacy` intact (one transaction: the
-- file is `wrapped`). The sixteen names are LEGACY_TABLES written out; the
-- date is 078's; tests/scripts/test_window_close.py pins both.
--
-- WHAT GOES WITH THE SCHEMA: the sixteen tables (42 MB), their indexes and
-- constraints, and the uuid-ossp extension that rode into `legacy` with the
-- 051 move (051's own note: the target uses gen_random_uuid() only). NOT
-- taken: `window_ddl` (080's), `archive` (the snapshots — 90 days, fork F9),
-- `runner`, `public`.
--
-- THE ACTOR: the database owner (production's runner login; `applied_by =
-- neondb_owner` on every ledger row since 066), which owns the legacy tables.
-- The runbook stops the worker first and takes the PITR marker before this
-- file: the backout for a drop that succeeded and broke the target is a
-- restore to that marker, which discards every write after it.
--
-- #1202 ("3g has no guard against running before #410's demo videos exist"):
-- ruled met on its "or" leg by the owner on 2026-09-16 — the target tier is
-- armed and serving with a connected destination. This directive guards
-- against an ACCIDENTAL deploy-time drop; it is not that guard.
-- runner:manual
-- runner:unadvertised
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy')
-- runner:postcondition SELECT count(*) = 16 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'archive' AND c.relkind = 'r' AND c.relname LIKE '%\_pre\_cutover\_20260917'
DO $$
DECLARE
  t text;
  src bigint;
  snap bigint;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'api_tokens', 'audit_log', 'category_post_case_mix', 'chat_settings',
    'instagram_accounts', 'media_items', 'media_posting_locks',
    'onboarding_sessions', 'posting_history', 'posting_history_dedup_archive',
    'posting_queue', 'schema_version', 'service_runs', 'user_chat_memberships',
    'user_interactions', 'users'
  ] LOOP
    IF to_regclass(format('archive.%I', t || '_pre_cutover_20260917')) IS NULL THEN
      RAISE EXCEPTION '3g refused: no snapshot for legacy.%', t;
    END IF;
    IF to_regclass(format('legacy.%I', t)) IS NULL THEN
      RAISE EXCEPTION '3g refused: legacy.% is not present to compare against its snapshot', t;
    END IF;
    EXECUTE format('SELECT count(*) FROM legacy.%I', t) INTO src;
    EXECUTE format('SELECT count(*) FROM archive.%I', t || '_pre_cutover_20260917') INTO snap;
    IF src <> snap THEN
      RAISE EXCEPTION '3g refused: legacy.% holds % rows but its snapshot holds % — a writer since 078; snapshot again before dropping', t, src, snap;
    END IF;
  END LOOP;
END $$;
DROP SCHEMA legacy CASCADE;
