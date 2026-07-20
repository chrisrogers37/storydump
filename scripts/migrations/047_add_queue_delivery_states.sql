-- Migration: 047_add_queue_delivery_states.sql
-- Description: Widen the posting_queue check_status constraint to add the two
--   delivery-tracking states 'sent_unconfirmed' and 'delivered'. Allowed values
--   are single-sourced in the QueueStatus enum (src/models/enums.py); the model
--   constraint is derived from that enum and a CI parity gate
--   (tests/src/models/test_enum_ssot_parity.py) asserts the model AND this
--   migration both match it — so a future value that lands in code without the
--   migration fails a test instead of aborting a production scheduler sweep.
-- Created: 2026-07-20
-- Issue: #684 (delivery-state machine — split the (processing, msg_id NULL) limbo)
--
-- 'sent_unconfirmed' is written when a Telegram send is dispatched but its
-- delivery cannot be confirmed (AmbiguousDeliveryError), replacing the ambiguous
-- (status='processing', telegram_message_id NULL) limbo. 'delivered' is written
-- when a telegram_message_id is stamped on the row (INV-1: delivered iff
-- stamped). Non-destructive (constraint widen), idempotent. The #510
-- ready/claimed lease rename is deferred to the lease increment (PR-L).

ALTER TABLE posting_queue DROP CONSTRAINT IF EXISTS check_status;
ALTER TABLE posting_queue ADD CONSTRAINT check_status CHECK (status IN ('pending', 'processing', 'failed', 'publishing', 'sent_unconfirmed', 'delivered'));

INSERT INTO schema_version (version, description, applied_at)
VALUES (47, 'Widen posting_queue check_status: add sent_unconfirmed + delivered delivery states (#684)', NOW())
ON CONFLICT DO NOTHING;
