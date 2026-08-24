-- PROPOSED — NOT A CORPUS MIGRATION, AND DELIBERATELY UNNUMBERED.
--
-- Web signup (#1015) needs two NOT NULL constraints dropped on the LEGACY
-- lineage — the schema the running application uses today. This file holds that
-- SQL. It is NOT in the numbered corpus because THERE IS NO SLOT FOR IT, and
-- that needs a ruling rather than a guess.
--
-- WHY NOT 064. `051_schema_move_public_to_legacy.sql` carries
-- `-- runner:schema-move` and is the corpus's LINEAGE BOUNDARY: below it is the
-- legacy lineage (001-050, what production runs), above it the target schema
-- built into the fresh `public` that 051 leaves behind. A file numbered 064
-- therefore executes AFTER the cutover, against the TARGET `users` — which
-- carries no telegram columns at all (053:83, "Platform-neutral human (FC-1.3):
-- NO telegram columns"). It would not merely target the wrong schema; the
-- ALTER would ERROR on a column that does not exist.
--
-- WHY NOT A LEGACY NUMBER. `legacy_lineage_max` derives the boundary from the
-- marker, so the legacy lineage ends at 050 and every slot 001-050 is taken.
-- `_FILENAME_RE` is `^(\d+)_.+\.sql$` — integers only, so there is no 050a.
-- Renumbering 051+ is not available either: 052-063 are each under a
-- "contiguous prefix of the advertised stream" contract (053's header), which
-- renumbering would break.
--
-- AND THE MECHANISM IS ALREADY TAKEN. Inserting into the legacy band is not
-- impossible -- PR #840 does exactly it, adding a new 050 and renumbering the
-- whole band upward (old 050 -> 051, the schema move 051 -> 052, 053 -> 054,
-- 060 -> 061). So the operation this file needs is a FULL-BAND RENUMBER, and
-- #840 is holding that lock: it is a DRAFT, reads CONFLICTING, and last moved
-- 2026-08-22. Two concurrent renumbers of one band is a hard lineage failure,
-- not a merge conflict anyone can eyeball -- which is the second and
-- independent reason this file carries no number.
--
-- SO THE RULING NEEDED IS: how does a NEW legacy-lineage migration get applied
-- to a production database that sits at 045 and must not be cut over yet? That
-- is a corpus-owner decision, not this PR's to make. Until it lands this file
-- is inert: `discover_migrations` ignores anything not matching `NNN_*.sql`,
-- and this lives in a subdirectory besides.
--
-- HAS NOT BEEN RUN AGAINST ANY DATABASE. The behaviour below was verified on a
-- throwaway PostgreSQL 16 container built from 006's DDL — see the PR body.

BEGIN;

-- users.telegram_user_id (setup_database.sql:12)
--   A user who signed up on the web has no Telegram identity. `users.id` is and
--   always was the primary key; this column is one provider's external id.
ALTER TABLE users ALTER COLUMN telegram_user_id DROP NOT NULL;

-- chat_settings.telegram_chat_id (006_chat_settings.sql:17)
--   Demotes the column from the tenant's IDENTITY to an OPTIONAL BINDING
--   ATTRIBUTE. `chat_settings.id` is the tenant key and every content FK
--   already points at it.
ALTER TABLE chat_settings ALTER COLUMN telegram_chat_id DROP NOT NULL;

-- UNIQUE IS DELIBERATELY KEPT ON BOTH. PostgreSQL UNIQUE is NULLS DISTINCT, so
-- many unbound rows coexist while a real telegram id still collides. Measured
-- on PG16 against 006's DDL: three NULL rows inserted and retained, a duplicate
-- real chat id still rejected, and BOTH indexes (the UNIQUE constraint's and
-- the redundant plain `idx_chat_settings_telegram_id` at 006:43) byte-identical
-- before and after — so this is a catalog-only change with no table rewrite.
--
-- THE GUARD SHIPS WITH THIS, NOT AFTER IT. Dropping NOT NULL arms a latent
-- defect in ChatSettingsRepository.get_by_chat_id: the ORM compiles
-- `column == None` to `IS NULL`, which matches every unbound row. See the
-- docstring there — it is the half of this change that is not SQL.

COMMIT;
