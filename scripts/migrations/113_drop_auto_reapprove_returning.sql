-- Migration 113: auto-approval's contract half, `workspaces.auto_reapprove_returning` dropped
-- (#1477; plan `documentation/planning/2026-10-11-auto-approval/02_serve-step.md` step 8; `07`
-- §56). Statements appended to the advertised stream.
--
-- 053 stored the boolean for "previously-posted media skips approval", and nothing ever acted on
-- it. 110 gave that meaning its home, the `returning` approval position, and the code it shipped
-- with stopped reading the column. That code was deployed everywhere before this file was
-- written, so no running reader is left: the services' SQL names the columns it reads, and the
-- target models describe the schema for parity, never for an ORM. No view, door, policy or index
-- reads the column, so the plain drop (RESTRICT) takes nothing with it.
--
-- The drop is catalog-only: PostgreSQL marks the column dropped without rewriting the table, so
-- `workspaces`' ACCESS EXCLUSIVE lock is held only for that and the commit.
--
-- Adoption: a drop's only state is an absence, and a probe must never read true on a database
-- that predates its file. So the postcondition pairs the absence with the table it is dropped
-- from (080's shape): a database before the target lineage has no `workspaces` and reads false,
-- one before this file still has the column and reads false, and the expression is one row
-- either way. It reads `pg_attribute`, which no privilege filters, rather than
-- `information_schema.columns`, which hides a column its reader holds no privilege on. No other
-- file's evidence reads the column (053's postconditions name its table, not its columns; 088's
-- adoption probe and 110's postconditions name neither), so no file needs an explicit probe.

-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'workspaces' AND t.relkind = 'r') AND NOT EXISTS (SELECT 1 FROM pg_attribute a JOIN pg_class t ON t.oid = a.attrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'workspaces' AND a.attname = 'auto_reapprove_returning' AND NOT a.attisdropped)

-- [§56 auto-approval's contract half: workspaces.auto_reapprove_returning dropped, its meaning now the returning position]
-- Retired by 110: the `returning` position carries what the column stored, and nothing reads it.
ALTER TABLE workspaces DROP COLUMN auto_reapprove_returning;
