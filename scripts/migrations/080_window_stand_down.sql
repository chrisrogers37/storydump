-- 080: M.3 step 8, the window's stand-down — the SUCCESS variant, PARTIAL by
-- fork F8 (a) (the legacy tear-out, phase 04; forks F6, F7, F8; #1216).
-- GATED like 079: the deploy owes it; the owner applies it, after 079, with
--   python -m scripts.migration_runner apply --manual 80
-- from documentation/operations/legacy-window-close.md.
--
-- WHAT IT DOES: the subject-identity guard first (04:213's R8 mirror — the
-- target's marker table present AND `legacy` absent AND 079 recorded in the
-- ledger, else RAISE: run early, a stand-down would certify a half-done
-- window, and on a database that never held `legacy` — a fresh target-only
-- one — the first two facts hold without any window; the ledger row is what
-- says 3g happened HERE); then DROP the #787 definer door (`window_ddl` is
-- not `legacy`, so 3g does not take it — in production the door was never
-- created, measured 2026-09-18, so this is a no-op there and the gate world's
-- exercise of it is CI's); then REVOKE the window's CREATE ON DATABASE from
-- svc_migration (held in production, measured the same day; the 3c re-create
-- of `public` needed it; nothing does now).
--
-- WHAT IT KEEPS, and why this is F8 (a) rather than the stand-down as
-- printed (04:224-226 revoked the four svc_* memberships from svc_migration
-- and svc_migration from the owner login): door files after the window hand
-- SECURITY DEFINER functions to service roles with `ALTER FUNCTION … OWNER
-- TO svc_*` (059, 062, 063, 064, 068, 076 — and every future door), and on
-- PG16+ that needs the executor to hold SET on the receiving role. The
-- executor is the database owner (production's runner login), whose creator
-- auto-grant on each svc_* role carries ADMIN but NO SET (0.2's Login
-- contract; D40's amendment); its SET runs THROUGH the window's memberships —
-- the owner → svc_migration (the bootstrap's explicit grant) → the four
-- NOLOGIN door owners — measured in production on 2026-09-17:
-- pg_has_role(current_user, 'svc_maintenance', 'SET') = true. Revoking any
-- link of that chain makes the next door file abort both services' deploys.
-- So every membership stays. The amendment beside D40's in
-- documentation/planning/2026-08-02-consolidated-design-plan/03-decision-record.md
-- records it; the full stand-down is the F.4 posture increment's (#751),
-- where the runner's login changes or door files bracket their own grants.
--
-- THE GATE, run as printed after this file (the PG16+ arms; production is
-- 17.x). The answer each line gives is on its right: F8 (a)'s shape and
-- production's measured one — `public` belongs to the owner login, not to
-- svc_migration; D40's design fact is not the production state (03:189).
-- tests/scripts/test_window_close.py runs every line on phase 03's world
-- and asserts these answers.
--   SELECT to_regclass('public.jobs') IS NOT NULL
--      AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy')
--      AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'window_ddl');            -- t
--   SELECT NOT has_database_privilege('svc_migration', current_database(), 'CREATE');      -- t
--   SELECT array_agg(r.rolname ORDER BY r.rolname) FROM pg_auth_members m
--     JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles g ON g.oid = m.member
--    WHERE g.rolname = 'svc_migration';   -- {svc_claim,svc_clock,svc_maintenance,svc_membership}, kept
--   SELECT count(*) = 0 FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.member
--    WHERE g.rolname LIKE 'svc\_%' AND g.rolname <> 'svc_migration';                         -- t
--   SELECT pg_has_role(current_user, 'svc_migration', 'MEMBER');                            -- t, kept
--   SELECT pg_has_role(current_user, 'svc_maintenance', 'SET');    -- t, the chain door files need (PG16+; on 15 read 'MEMBER')
--   SELECT count(*) = 0 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
--    WHERE r.rolname LIKE 'svc\_%'
--      AND NOT (m.member = current_user::regrole
--               AND (r.rolname = 'svc_migration' OR m.admin_option))   -- the bootstrap's explicit grant, or the creator auto-grant (16+)
--      AND NOT (m.member = 'svc_migration'::regrole
--               AND r.rolname IN ('svc_claim','svc_clock','svc_maintenance','svc_membership')); -- t
--   SELECT bool_and(NOT has_schema_privilege(r, 'public', 'CREATE')) FROM unnest(ARRAY[
--     'svc_claim','svc_clock','svc_maintenance','svc_membership','svc_ingress','svc_worker']) r;  -- t
--   SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public';             -- the owner login
--   SELECT count(*) = 0 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--    WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'public');    -- t
--   SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--    WHERE n.nspname = 'archive' AND c.relkind = 'r' AND c.relname LIKE '%\_pre\_cutover\_%';    -- 16
-- The positive control, after the gate (F8): 076's hand-off as the owner —
-- `GRANT CREATE ON SCHEMA public TO svc_maintenance; ALTER FUNCTION
-- fn_reaper_stale_approved(interval, int) OWNER TO svc_maintenance; REVOKE
-- CREATE ON SCHEMA public FROM svc_maintenance` (every door file brackets the
-- schema half itself; the membership half is what this file keeps) — still
-- succeeds.
-- runner:manual
-- runner:unadvertised
-- runner:postcondition SELECT to_regclass('public.jobs') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy') AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'window_ddl')
-- runner:postcondition SELECT NOT has_database_privilege('svc_migration', current_database(), 'CREATE')
DO $$
BEGIN
  -- SUBJECT-IDENTITY GUARD (04:213, the R8 mirror): this file closes a
  -- COMPLETED window. Run early, its gate would certify "window closed" over
  -- a half-done window, and the door it drops might still be needed.
  IF to_regclass('public.jobs') IS NULL
     OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy')
     OR NOT EXISTS (SELECT 1 FROM runner.schema_migrations
                     WHERE version = 79 AND status IN ('applied', 'repaired')) THEN
    RAISE EXCEPTION 'success variant refused: the window has not completed 3g (target marker present: %, legacy schema present: %, 079 recorded: %)',
      to_regclass('public.jobs') IS NOT NULL,
      EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy'),
      EXISTS (SELECT 1 FROM runner.schema_migrations WHERE version = 79 AND status IN ('applied', 'repaired'));
  END IF;
END $$;
DROP SCHEMA IF EXISTS window_ddl CASCADE;
DO $$
BEGIN
  EXECUTE format('REVOKE CREATE ON DATABASE %I FROM svc_migration', current_database());
END $$;
