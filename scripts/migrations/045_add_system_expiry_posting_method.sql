-- Migration: 045_add_system_expiry_posting_method.sql
-- Description: Add 'system_expiry' to the posting_history posting_method CHECK constraint
-- Created: 2026-07-19
-- Issue: #682 (expire-reap drain blocked: history write rejected by check_posting_method)
--
-- The reap lifecycle (#560/#561) writes terminal posting_history rows with
-- posting_method='system_expiry', but migration 004 constrained
-- posting_method to ('instagram_api', 'telegram_manual') and migration 042
-- widened only the sibling status constraint (check_history_status). The
-- system_expiry write path first actually executed in production once #683
-- fixed the reap's error classification — the insert failed with a
-- CheckViolation and aborted the sweep pass, so expired rows could not drain.
--
-- posting_method values written by code today: 'instagram_api'
-- (telegram_autopost), 'telegram_manual' (scheduler), 'system_expiry'
-- (queue_reap). The model now declares this constraint too, so fresh
-- installs match.

ALTER TABLE posting_history DROP CONSTRAINT IF EXISTS check_posting_method;
ALTER TABLE posting_history ADD CONSTRAINT check_posting_method CHECK (posting_method IN ('instagram_api', 'telegram_manual', 'system_expiry'));

INSERT INTO schema_version (version, description, applied_at)
VALUES (45, 'Add system_expiry to posting_history posting_method CHECK (#682 reap drain)', NOW())
ON CONFLICT DO NOTHING;
