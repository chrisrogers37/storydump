-- 078: the M.3 step-3f snapshots — every legacy table copied into `archive`, owned by
-- svc_maintenance (04 3f; 00 FC-7 §6; the legacy tear-out, phase 03; fork F4: all sixteen —
-- the fourteen of 02 §9, schema_version, and posting_history_dedup_archive, which was made by
-- hand in production and is declared by no migration). Owed since the window ran by hand
-- (2026-08-24/26): the drop of `legacy` (079, gated) follows only once these exist.
--
-- NOT advertised DDL: the stream replays from an empty database that holds no `legacy`, so
-- this file is above the 051 move but outside the F.2 prefix — the marker below is how
-- scripts/advertised_ddl.py::target_lineage_files leaves it out. No 07 block, no manifest row;
-- the postconditions are its adoption evidence (until 3g drops `legacy`, after which an
-- unbounded `runner adopt` on a database lacking this row cannot probe it — phase 04 bounds
-- any adopt it needs).
--
-- THE ACTOR AND ITS RIGHTS, measured on 2026-09-17: every ledger row since the runner was
-- armed (066–077) was applied by neondb_owner, the same login as TARGET_DATABASE_URL; it owns
-- the legacy tables, CREATEs on `archive` (its own schema), and holds svc_maintenance with
-- SET (pg_has_role … 'SET' = true) through the window bootstrap's memberships — the ones
-- fork F8's partial stand-down keeps, and which any stand-down must in any case follow this
-- file by phase order. CI's other actor, svc_migration, holds the same through the bootstrap.
-- No bracket is needed for either.
--
-- ONE TRANSACTION, no writers: the file is `wrapped` (no BEGIN, no no-transaction marker), so
-- the sixteen copies, the thirty-two postconditions and the ledger row commit together or not
-- at all — a failure anywhere leaves no partial archive and aborts both services' deploys with
-- the old version serving. The row-count postconditions compare each copy to its source in the
-- same transaction; that is exact only because nothing writes to `legacy` any more (measured:
-- n_tup_ins/upd/del = 0 on all sixteen since the stats' last reset; the legacy tier's code was
-- deleted in phase 01). statement_timeout is 0 on the runner's session; the sixteen tables
-- total 42 MB.
--
-- THE SNAPSHOTS are read-only copies: CREATE TABLE … AS copies rows, not constraints, indexes,
-- defaults or sequences, and the legacy lineage declares no type a later DROP SCHEMA … CASCADE
-- could reach into `archive` through (only uuid-ossp rides into `legacy`, referenced by no
-- copied column). They are readable by svc_maintenance's members only — the owner, and
-- svc_migration until the stand-down — with no grant to anything else. They carry the ratified
-- `archive_snapshots` retention class (05 §Retention: 90 days, fork F9): 059's sweep reads the
-- date from the NAME, so the clock runs from 2026-09-17 and the snapshots become eligible on
-- 2026-12-16 whatever day 079 runs; an owner who wants them longer exports first
-- (`pg_dump -n archive`). The date in the names is FROZEN at merge: the runner checksums the
-- file's bytes, so an edit after the first predeploy breaks every later apply until `repair`.
--
-- It names all sixteen tables unconditionally: an inventory error fails the file whole and the
-- deploy with it, never a partial archive. A database built from the tree rather than cloned
-- from production lacks the hand-made table — pre-create it from
-- tests/scripts/fixtures/legacy_by_hand.sql before applying, as the lineage lane does.
-- runner:unadvertised
-- runner:postcondition SELECT to_regclass('archive.api_tokens_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.api_tokens_pre_cutover_20260917) = (SELECT count(*) FROM legacy.api_tokens)
-- runner:postcondition SELECT to_regclass('archive.audit_log_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.audit_log_pre_cutover_20260917) = (SELECT count(*) FROM legacy.audit_log)
-- runner:postcondition SELECT to_regclass('archive.category_post_case_mix_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.category_post_case_mix_pre_cutover_20260917) = (SELECT count(*) FROM legacy.category_post_case_mix)
-- runner:postcondition SELECT to_regclass('archive.chat_settings_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.chat_settings_pre_cutover_20260917) = (SELECT count(*) FROM legacy.chat_settings)
-- runner:postcondition SELECT to_regclass('archive.instagram_accounts_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.instagram_accounts_pre_cutover_20260917) = (SELECT count(*) FROM legacy.instagram_accounts)
-- runner:postcondition SELECT to_regclass('archive.media_items_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.media_items_pre_cutover_20260917) = (SELECT count(*) FROM legacy.media_items)
-- runner:postcondition SELECT to_regclass('archive.media_posting_locks_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.media_posting_locks_pre_cutover_20260917) = (SELECT count(*) FROM legacy.media_posting_locks)
-- runner:postcondition SELECT to_regclass('archive.onboarding_sessions_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.onboarding_sessions_pre_cutover_20260917) = (SELECT count(*) FROM legacy.onboarding_sessions)
-- runner:postcondition SELECT to_regclass('archive.posting_history_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.posting_history_pre_cutover_20260917) = (SELECT count(*) FROM legacy.posting_history)
-- runner:postcondition SELECT to_regclass('archive.posting_history_dedup_archive_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.posting_history_dedup_archive_pre_cutover_20260917) = (SELECT count(*) FROM legacy.posting_history_dedup_archive)
-- runner:postcondition SELECT to_regclass('archive.posting_queue_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.posting_queue_pre_cutover_20260917) = (SELECT count(*) FROM legacy.posting_queue)
-- runner:postcondition SELECT to_regclass('archive.schema_version_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.schema_version_pre_cutover_20260917) = (SELECT count(*) FROM legacy.schema_version)
-- runner:postcondition SELECT to_regclass('archive.service_runs_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.service_runs_pre_cutover_20260917) = (SELECT count(*) FROM legacy.service_runs)
-- runner:postcondition SELECT to_regclass('archive.user_chat_memberships_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.user_chat_memberships_pre_cutover_20260917) = (SELECT count(*) FROM legacy.user_chat_memberships)
-- runner:postcondition SELECT to_regclass('archive.user_interactions_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.user_interactions_pre_cutover_20260917) = (SELECT count(*) FROM legacy.user_interactions)
-- runner:postcondition SELECT to_regclass('archive.users_pre_cutover_20260917') IS NOT NULL
-- runner:postcondition SELECT (SELECT count(*) FROM archive.users_pre_cutover_20260917) = (SELECT count(*) FROM legacy.users)

CREATE TABLE archive.api_tokens_pre_cutover_20260917 AS TABLE legacy.api_tokens;
ALTER TABLE archive.api_tokens_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.audit_log_pre_cutover_20260917 AS TABLE legacy.audit_log;
ALTER TABLE archive.audit_log_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.category_post_case_mix_pre_cutover_20260917 AS TABLE legacy.category_post_case_mix;
ALTER TABLE archive.category_post_case_mix_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.chat_settings_pre_cutover_20260917 AS TABLE legacy.chat_settings;
ALTER TABLE archive.chat_settings_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.instagram_accounts_pre_cutover_20260917 AS TABLE legacy.instagram_accounts;
ALTER TABLE archive.instagram_accounts_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.media_items_pre_cutover_20260917 AS TABLE legacy.media_items;
ALTER TABLE archive.media_items_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.media_posting_locks_pre_cutover_20260917 AS TABLE legacy.media_posting_locks;
ALTER TABLE archive.media_posting_locks_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.onboarding_sessions_pre_cutover_20260917 AS TABLE legacy.onboarding_sessions;
ALTER TABLE archive.onboarding_sessions_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.posting_history_pre_cutover_20260917 AS TABLE legacy.posting_history;
ALTER TABLE archive.posting_history_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.posting_history_dedup_archive_pre_cutover_20260917 AS TABLE legacy.posting_history_dedup_archive;
ALTER TABLE archive.posting_history_dedup_archive_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.posting_queue_pre_cutover_20260917 AS TABLE legacy.posting_queue;
ALTER TABLE archive.posting_queue_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.schema_version_pre_cutover_20260917 AS TABLE legacy.schema_version;
ALTER TABLE archive.schema_version_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.service_runs_pre_cutover_20260917 AS TABLE legacy.service_runs;
ALTER TABLE archive.service_runs_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.user_chat_memberships_pre_cutover_20260917 AS TABLE legacy.user_chat_memberships;
ALTER TABLE archive.user_chat_memberships_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.user_interactions_pre_cutover_20260917 AS TABLE legacy.user_interactions;
ALTER TABLE archive.user_interactions_pre_cutover_20260917 OWNER TO svc_maintenance;

CREATE TABLE archive.users_pre_cutover_20260917 AS TABLE legacy.users;
ALTER TABLE archive.users_pre_cutover_20260917 OWNER TO svc_maintenance;
