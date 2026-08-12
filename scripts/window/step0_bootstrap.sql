-- M.3 step-0 window bootstrap (plan 04, printed in full there; this file is its
-- transcription, not a new design).
--
-- THIS IS NOT A RUNNER MIGRATION, and the distinction is the whole point.
--
-- 02 section 7-DDL opens with "ROLES ARE NOT CREATED HERE". The advertised DDL
-- stream executes as svc_migration, which must not hold role DDL's cluster
-- privileges -- a runner login carrying CREATEROLE is a standing
-- self-escalation path on PostgreSQL 15 (D40, "Rejected: svc_migration gets
-- CREATEROLE"). So the seven service roles are provisioned HERE, by the
-- database-owner actor, before the stream runs.
--
-- It is also the one artifact where guarded/conditional DDL is legal, because
-- it is not the byte-parity stream: the stream must stay byte-fixed so the
-- advertised text and the migration files can be held equal by diff. This file
-- has no such obligation and is idempotent instead.
--
-- ACTOR: the database-owner actor -- the role that owns the database and the
-- legacy lineage; on Neon, the project's database owner. Plan 04 0.2 states its
-- shape: non-superuser, CREATEROLE. It is also svc_migration's creator, which
-- is what keeps the self-grant below legal on PostgreSQL 16+ (the creator
-- receives ADMIN automatically; on the pinned PG15 CREATEROLE alone suffices).
--
-- Passwords and connection strings are deployment env, never DDL.
--
-- Closure: legs 2, 3 and 4 below are WINDOW TRANSIENTS. They are revoked by
-- plan 04 step 8, which has two variants (success and abandon) because on the
-- abandon path the objects survive the un-rename. Neither variant is
-- transcribed yet -- they belong with the M.3 filing, not with F.2.

DO $$
DECLARE r text; own text;
BEGIN
  -- 1. Provision absent service roles (inventory: 02 section 7; passwords are
  --    deployment env, set out-of-band -- never DDL). Guarded: pre-existing
  --    roles (svc_migration has been the runner's login since 0.2) pass
  --    through untouched.
  FOREACH r IN ARRAY ARRAY['svc_ingress','svc_worker','svc_migration'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I LOGIN', r);
    END IF;
  END LOOP;
  FOREACH r IN ARRAY ARRAY['svc_claim','svc_clock','svc_maintenance','svc_membership'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
  -- 2. Transient window memberships (the 02 section 7 invariant, its one
  --    bounded exception, and the OWNER TO mechanics that force it; revoked at
  --    step 8).
  GRANT svc_claim, svc_clock, svc_maintenance, svc_membership TO svc_migration;
  -- 3. Legacy reads for the window: 3e transforms and 3f snapshots read
  --    legacy.* as svc_migration, and schema ownership does not confer table
  --    access (R6). Granted while the lineage still sits in public -- grants
  --    attach to tables and ride the 3c rename. Closure, both exits (D40): on
  --    the success path they die with the tables at 3g; on the abandon path
  --    they survive the un-rename, and step 8's abandon variant revokes them.
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO svc_migration;
  -- 4. The 3c privilege pair: the rename requires schema ownership, the
  --    re-create requires database CREATE. PG15 mechanics make the ALTER itself
  --    membership-gated, so the owner actor self-grants first (revoked at step
  --    8; legal on PG16+ because the 0.2 login contract makes this actor
  --    svc_migration's creator, hence ADMIN holder). The ALTER is guarded for
  --    the same reason the role creation is: on a re-run the schema is already
  --    svc_migration's, and the actor would no longer own it.
  EXECUTE format('GRANT svc_migration TO %I', current_user);
  -- Precondition, not assumption (pass 8 -- R7's shape one level up: the
  -- abandon variant restores ownership to pg_database_owner, and a gate written
  -- from a constant cannot catch the constant being wrong). A database whose
  -- public owner was ever customized halts HERE -- at window prep, on the M.2
  -- branch -- never mis-restores later.
  SELECT nspowner::regrole::text INTO own FROM pg_namespace WHERE nspname = 'public';
  IF own NOT IN ('pg_database_owner', 'svc_migration') THEN
    RAISE EXCEPTION 'public owner is % - not the pg_database_owner the abandon variant '
                    'restores; record the real owner and adapt before opening the window', own;
  END IF;
  IF own IS DISTINCT FROM 'svc_migration' THEN
    ALTER SCHEMA public OWNER TO svc_migration;
  END IF;
  EXECUTE format('GRANT CREATE ON DATABASE %I TO svc_migration', current_database());
END $$;
