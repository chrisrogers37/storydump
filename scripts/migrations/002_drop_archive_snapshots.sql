-- Drop the sixteen archive snapshots without touching retention exports.
-- The runner wraps the drops and its ledger row in one transaction.
-- runner:manual
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'archive' AND c.relname LIKE '%pre_cutover%')

DO $backup_guard$
BEGIN
  IF current_setting('app.archive_snapshot_backup_confirmed', true) IS DISTINCT FROM 'on' THEN
    RAISE EXCEPTION 'archive snapshot tables must be exported or backed up; rerun apply --manual 002 --confirm-archive-backup';
  END IF;
END $backup_guard$;

DROP TABLE IF EXISTS archive.api_tokens_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.audit_log_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.category_post_case_mix_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.chat_settings_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.instagram_accounts_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.media_items_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.media_posting_locks_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.onboarding_sessions_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.posting_history_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.posting_history_dedup_archive_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.posting_queue_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.schema_version_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.service_runs_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.user_chat_memberships_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.user_interactions_pre_cutover_20260917;
DROP TABLE IF EXISTS archive.users_pre_cutover_20260917;
