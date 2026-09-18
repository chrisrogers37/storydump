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
-- sixteen on 2026-09-17). The DO block is the precondition, in-file, and it
-- refuses the drop when `legacy` holds any relation but the sixteen (a table
-- added since 078 has no snapshot and would go with the schema unseen), for
-- a missing snapshot, for a legacy table that is not there to compare, for a
-- row count that no longer matches its source, and for CONTENT that differs
-- from its snapshot — every row hashed (`md5(row::text)`), the multiset
-- difference taken, so an update in place, a delete-and-insert or a column
-- added since 078 refuses too — and when anything OUTSIDE `legacy` depends
-- on a legacy relation, row type or function (a view, a foreign key, a
-- default, a function signature: what CASCADE would take with the schema,
-- unseen), naming it; measured in production on 2026-09-18: nothing does.
-- A refusal leaves `legacy` intact (one transaction: the file is `wrapped`).
-- What it cannot see and the drop takes anyway: the legacy tables' own
-- indexes (77 in production), constraints, defaults and sequence values — a
-- snapshot is rows only (078's header). The sixteen names are LEGACY_TABLES
-- written out; the date is 078's; tests/scripts/test_window_close.py pins
-- both and drives every refusal.
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
  n bigint;
  src bigint;
  snap bigint;
  diff bigint;
  deps text;
BEGIN
  SELECT count(*) INTO n FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = 'legacy' AND c.relkind IN ('r', 'p', 'v', 'm', 'f');
  IF n <> 16 THEN
    RAISE EXCEPTION '3g refused: legacy holds % relations, not the sixteen of the inventory — something has no snapshot', n;
  END IF;
  -- what CASCADE would take from OUTSIDE the schema: any dependent of a legacy
  -- relation, row type or function whose own schema is not `legacy` (a view,
  -- a foreign key, a default, a function signature); an object class this
  -- CASE does not name counts too — a refusal to read, never a drop
  SELECT count(DISTINCT (d.classid, d.objid)),
         string_agg(DISTINCT pg_describe_object(d.classid, d.objid, d.objsubid), '; ')
    INTO n, deps
    FROM pg_depend d, pg_namespace rn
   WHERE rn.nspname = 'legacy'
     AND d.deptype IN ('n', 'a')
     AND ((d.refclassid = 'pg_class'::regclass AND EXISTS (SELECT 1 FROM pg_class x WHERE x.oid = d.refobjid AND x.relnamespace = rn.oid))
       OR (d.refclassid = 'pg_type'::regclass AND EXISTS (SELECT 1 FROM pg_type x WHERE x.oid = d.refobjid AND x.typnamespace = rn.oid))
       OR (d.refclassid = 'pg_proc'::regclass AND EXISTS (SELECT 1 FROM pg_proc x WHERE x.oid = d.refobjid AND x.pronamespace = rn.oid)))
     AND coalesce(CASE d.classid
           WHEN 'pg_class'::regclass THEN (SELECT x.relnamespace FROM pg_class x WHERE x.oid = d.objid)
           WHEN 'pg_constraint'::regclass THEN (SELECT x.connamespace FROM pg_constraint x WHERE x.oid = d.objid)
           WHEN 'pg_rewrite'::regclass THEN (SELECT v.relnamespace FROM pg_rewrite x JOIN pg_class v ON v.oid = x.ev_class WHERE x.oid = d.objid)
           WHEN 'pg_trigger'::regclass THEN (SELECT v.relnamespace FROM pg_trigger x JOIN pg_class v ON v.oid = x.tgrelid WHERE x.oid = d.objid)
           WHEN 'pg_attrdef'::regclass THEN (SELECT v.relnamespace FROM pg_attrdef x JOIN pg_class v ON v.oid = x.adrelid WHERE x.oid = d.objid)
           WHEN 'pg_type'::regclass THEN (SELECT x.typnamespace FROM pg_type x WHERE x.oid = d.objid)
           WHEN 'pg_proc'::regclass THEN (SELECT x.pronamespace FROM pg_proc x WHERE x.oid = d.objid)
         END, 0) <> rn.oid;
  IF n <> 0 THEN
    RAISE EXCEPTION '3g refused: % object(s) outside legacy depend on it and CASCADE would take them unseen: %', n, deps;
  END IF;
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
    EXECUTE format(
      'SELECT count(*) FROM ((SELECT md5(x::text) FROM legacy.%I x) EXCEPT ALL (SELECT md5(y::text) FROM archive.%I y)) d',
      t, t || '_pre_cutover_20260917') INTO diff;
    IF diff <> 0 THEN
      RAISE EXCEPTION '3g refused: legacy.% differs from its snapshot in % row(s) — content changed since 078; snapshot again before dropping', t, diff;
    END IF;
  END LOOP;
END $$;
DROP SCHEMA legacy CASCADE;
