-- Migration: 042_add_expired_history_status.sql
-- Description: Add 'expired' to the posting_history status CHECK constraint
-- Created: 2026-07-14
-- Issue: #560 (reap lifecycle — expire sent rows gracefully instead of orphaning buttons)
--
-- When a button-bearing queue row (already sent to Telegram, with live inline
-- buttons) ages out, the reapers now edit the card to "Expired", strip the
-- buttons, and write a terminal posting_history row with status='expired'
-- instead of hard-deleting the row and orphaning its buttons. That terminal
-- status must be permitted by the check_history_status constraint.
--
-- The 'expired' history row is also the tap-time fallback: a late tap on a
-- reaped card finds it and shows a friendly "Expired" caption rather than the
-- "Queue item not found" error.

ALTER TABLE posting_history DROP CONSTRAINT IF EXISTS check_history_status;
ALTER TABLE posting_history ADD CONSTRAINT check_history_status CHECK (status IN ('posted', 'failed', 'skipped', 'rejected', 'expired'));

INSERT INTO schema_version (version, description, applied_at)
VALUES (42, 'Add expired to posting_history status CHECK (Epic #560 reap lifecycle)', NOW())
ON CONFLICT DO NOTHING;
