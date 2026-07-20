-- Migration: 048_backfill_queue_delivery_states.sql
-- Description: Map legacy 'processing' rows to the delivery-state vocabulary
--   introduced by migration 047 (#684). Stamped rows are confirmed-delivered
--   cards that predate the delivered-on-stamp code path; unstamped rows are
--   FAIL-SAFE parked as 'sent_unconfirmed' — a NULL telegram_message_id is
--   not proof the send never happened (#679/#680), so a possibly-delivered
--   card must never be requeued or raw-deleted. 'pending' is untouched (it
--   remains the live pre-claim state; the #510 ready/claimed rename ships
--   with the lease increment). Idempotent, WHERE-scoped, safe to re-run.
-- Created: 2026-07-20
-- Issue: #680 #687
--
-- ORDERING: apply only AFTER the delivered-on-stamp code (PR3) is deployed —
-- expand (047) -> code cutover -> backfill (this) -> invariant (049).
--
-- Precondition (run before apply; PAUSE AND INVESTIGATE on large deviation):
--   SELECT status, (telegram_message_id IS NOT NULL) AS stamped, count(*)
--   FROM posting_queue GROUP BY 1, 2 ORDER BY 1, 2;
-- Snapshot at planning (prod 2026-07-19): 9 rows total —
--   2 (processing, stamped)   -> become 'delivered' here
--   0 (processing, unstamped) -> would become 'sent_unconfirmed'
--   7 (failed, unstamped)     -> correctly need NO update: 'failed' is valid
--                                in both the legacy and target vocabularies,
--                                so a nonzero 'failed' count is not a gap.
-- Neon-branch dry-run (prod copy 2026-07-20): 20 (processing, stamped),
--   0 unstamped, 0 failed — the stuck stamped-processing class had grown 10x
--   in a day (reported to ops with the dry-run). All 20 map to 'delivered',
--   where the existing stamped-row 24h reap ages them out.
-- A markedly larger 'processing' population than the latest count above
-- means stuck rows accumulated since — investigate before mapping them.

UPDATE posting_queue SET status = 'delivered'
  WHERE status = 'processing' AND telegram_message_id IS NOT NULL;

UPDATE posting_queue SET status = 'sent_unconfirmed'
  WHERE status = 'processing' AND telegram_message_id IS NULL;

INSERT INTO schema_version (version, description, applied_at)
VALUES (48, 'Backfill legacy processing rows to delivery states (#680/#687)', NOW())
ON CONFLICT DO NOTHING;
