-- Migration: 049_inv1_delivered_requires_stamp.sql
-- Description: INV-1 — a 'delivered' row MUST carry a telegram_message_id.
--   'delivered' means the approval card is confirmed in the chat, and the
--   stamp is that confirmation, so the two are definitionally coupled; the
--   "delivered-but-unstamped" orphan shape (#687) becomes unrepresentable.
--   Mirrors the check_delivered_stamped constraint on the SQLAlchemy model.
--   Non-destructive, idempotent (DROP IF EXISTS + ADD). Immediate full-table
--   validate under a brief AccessExclusive lock is the house convention and
--   safe at posting_queue's single-digit row count.
-- Created: 2026-07-20
-- Issue: #687
--
-- ORDERING: apply AFTER 048 (backfill) with the delivered-on-stamp code live.
-- Precondition (assert 0 before apply):
--   SELECT count(*) FROM posting_queue
--   WHERE status = 'delivered' AND telegram_message_id IS NULL;

ALTER TABLE posting_queue DROP CONSTRAINT IF EXISTS check_delivered_stamped;
ALTER TABLE posting_queue ADD CONSTRAINT check_delivered_stamped
  CHECK (status <> 'delivered' OR telegram_message_id IS NOT NULL);

INSERT INTO schema_version (version, description, applied_at)
VALUES (49, 'INV-1: delivered requires telegram_message_id (#687)', NOW())
ON CONFLICT DO NOTHING;
