-- M.3 step-0 window legacy-DDL door (plan 04, printed in full there; this file is
-- its transcription, not a new design). Applied by the owner actor IMMEDIATELY
-- AFTER step0_bootstrap.sql, and by the CI lineage stand-up before any world
-- that replays 050.
--
-- THIS IS NOT A RUNNER MIGRATION. It is the #787 ruling's mechanism: option (d),
-- a SECURITY DEFINER function owned by the database-owner actor.
--
-- WHY IT EXISTS. Window step 3b applies 050 as svc_migration. 050 ALTERs two
-- owner-owned legacy tables, ALTER requires table ownership or membership in the
-- owning role, and NO GRANT CAN PRODUCE IT -- ALTER is not a grantable
-- privilege. Postgres also checks ownership BEFORE it determines a statement is
-- a no-op, so 050's idempotency buys nothing at the privilege layer: as
-- svc_migration both statements fail `must be owner of table` even against a
-- fully-normalized database. A definer door is the only mechanism that scopes
-- the elevation to STATEMENTS rather than to a ROLE.
--
-- THE BOUND IS THE STATIC BODY, AND NOTHING ELSE. This door can alter exactly
-- the two tables its body names; the same role is refused on any other table,
-- measured. That bound rests on ONE property: the body is a fixed statement
-- list. One `EXECUTE format(...)` would turn it into a general-purpose DDL
-- executor running as the database owner -- a no-privilege role was
-- demonstrated altering a table the author never named through exactly that
-- edit. The door is therefore parameterless AND free of dynamic SQL, and BOTH
-- are gated (tests/scripts/test_window_legacy_ddl_door.py), because the
-- widening edit does not look dangerous: parameterising two near-identical
-- doors into one is an ordinary-looking refactor.
--
-- THE REVOKE IS THE ENTIRE ACCESS CONTROL. A newly created function has
-- `proacl IS NULL`, which means EXECUTE to PUBLIC -- a role holding no grant of
-- any kind was demonstrated performing owner DDL through an unrevoked definer
-- function. The REVOKE below is not hygiene; omitting it hands owner-privileged
-- DDL to every role in the cluster. It runs in the SAME transaction as the
-- CREATE so no window exists between them.
--
-- ITS OWN SCHEMA, DELIBERATELY. `window_ddl` keeps the door out of `public`'s
-- runtime definer-door census (which is a drift detector and should stay one),
-- survives the 3c rename the way `runner` does so teardown never depends on
-- rename timing, and makes stand-down a single complete statement rather than
-- an enumeration.
--
-- CLOSURE: this door is a WINDOW TRANSIENT. Plan 04 step 8 drops the schema in
-- BOTH variants -- success and abandon. It is not dropped by 3g: `window_ddl` is
-- not `legacy`, so the success path must drop it explicitly. An abort that
-- reaches neither variant leaves it standing; that residue is bounded (two named
-- ALTERs), enumerable (`pg_proc` joined to `pg_namespace`), and removed by one
-- statement. That trade is the ruling's condition 3, chosen deliberately -- see
-- the door's section in plan 04.
--
-- ACTOR: the database-owner actor -- the role that owns the database and the
-- legacy lineage. SECURITY DEFINER runs the body as THAT role, which is the
-- whole mechanism; applying this file as any other role produces a door that
-- confers nothing.

BEGIN;

CREATE SCHEMA IF NOT EXISTS window_ddl;
REVOKE ALL ON SCHEMA window_ddl FROM PUBLIC;

-- 050's two statements, verbatim, fully schema-qualified. Qualification is not
-- style: `search_path` is pinned to pg_catalog, and the legacy lineage sits in
-- `public` for the whole life of this door (3b runs before the 3c move).
CREATE OR REPLACE FUNCTION window_ddl.fn_050_chain_reconciliation()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $fn$
BEGIN
  ALTER TABLE public.api_tokens
    DROP CONSTRAINT IF EXISTS api_tokens_service_name_token_type_key;
  ALTER TABLE public.chat_settings
    ALTER COLUMN caption_style TYPE TEXT;
END
$fn$;

-- THE access control. Same transaction as the CREATE above.
REVOKE ALL ON FUNCTION window_ddl.fn_050_chain_reconciliation() FROM PUBLIC;

-- The window actor is the only grantee. Guarded because this file is also
-- applied to CI owner-worlds that stand the lineage up without the bootstrap,
-- where the service roles do not exist.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_migration') THEN
    GRANT USAGE ON SCHEMA window_ddl TO svc_migration;
    GRANT EXECUTE ON FUNCTION window_ddl.fn_050_chain_reconciliation()
      TO svc_migration;
  END IF;
END $$;

COMMIT;
