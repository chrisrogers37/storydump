-- Migration: 053_shared_trigger_functions.sql
-- Description: The two shared functions every later target-schema table
--   depends on (plan 02 §0). First file of the F.2 target lineage, and the
--   head of the advertised stream - so it lands before any table regardless
--   of how the table increments are eventually ordered.
--
--   trg_touch_updated_at() is the one updated_at rule. Every target table
--   carrying updated_at attaches a BEFORE UPDATE trigger to it rather than
--   restating now() per table, so the column cannot mean different things on
--   different tables.
--
--   fn_safe_tz() is the one time-zone gate, and it has two consumers with one
--   rule between them. The ck_*_tz_valid CHECKs make an unrecognized zone
--   unstorable at write time; fn_next_slot converts through it at read time so
--   a value that was valid when stored and is later withdrawn from tzdata
--   degrades THAT ROW to UTC instead of aborting the caller's whole set-based
--   statement. It is STABLE and deliberately not IMMUTABLE: the answer depends
--   on the server's tzdata, which changes.
--
--   ABOVE THE LINEAGE BOUNDARY. 051 renames the legacy schema out of public
--   and leaves an empty public behind; this file is the first thing created
--   into it. Nothing here touches the legacy lineage, and the running
--   application does not see it until the M.3 cutover.
--
--   No table, deliberately. That is what makes this increment independent of
--   the open question about whether a table may land before its policy - that
--   question governs the table increments, and there is no table here.
--
-- Rollback: DROP FUNCTION IF EXISTS fn_safe_tz(text);
--   DROP FUNCTION IF EXISTS trg_touch_updated_at();
--   Safe while the target lineage carries no dependants; once a table
--   attaches a trigger, the dependency is real and the drop must go with it.
-- Created: 2026-08-13
-- Issue: #806
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'trg_touch_updated_at')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_safe_tz')
-- runner:postcondition SELECT fn_safe_tz('America/New_York') = 'America/New_York'
-- runner:postcondition SELECT fn_safe_tz('Not/AZone') = 'UTC'

CREATE FUNCTION trg_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE FUNCTION fn_safe_tz(p_tz text) RETURNS text LANGUAGE plpgsql STABLE AS $$
BEGIN
  PERFORM now() AT TIME ZONE p_tz;
  RETURN p_tz;
EXCEPTION WHEN invalid_parameter_value THEN
  RETURN 'UTC';
END $$;
