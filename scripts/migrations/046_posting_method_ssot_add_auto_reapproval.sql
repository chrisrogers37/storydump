-- Migration: 046_posting_method_ssot_add_auto_reapproval.sql
-- Description: Add 'auto_reapproval' to the posting_history posting_method CHECK
--   constraint, and make this the LAST hand-authored widen of it. Allowed
--   values are now single-sourced in the PostingMethod enum
--   (src/models/enums.py); the model constraint is derived from that enum and a
--   CI parity gate (tests/src/models/test_enum_ssot_parity.py) asserts the model
--   AND this migration both match it — so a future value that lands in code
--   without the migration fails a test instead of aborting a production sweep.
-- Created: 2026-07-20
-- Issue: #685 (auto_reapproval CheckViolation would abort the scheduler repost path)
--
-- posting_method values written by code: 'instagram_api' (telegram_autopost),
-- 'telegram_manual' (scheduler), 'system_expiry' (queue_reap), 'auto_reapproval'
-- (scheduler default-config repost path). Migration 045 widened to the first
-- three; auto_reapproval is a live code path still missing from the constraint
-- (#685). Non-destructive (constraint widen), idempotent.

ALTER TABLE posting_history DROP CONSTRAINT IF EXISTS check_posting_method;
ALTER TABLE posting_history ADD CONSTRAINT check_posting_method CHECK (posting_method IN ('instagram_api', 'telegram_manual', 'system_expiry', 'auto_reapproval'));

INSERT INTO schema_version (version, description, applied_at)
VALUES (46, 'Add auto_reapproval to posting_history posting_method CHECK; posting_method SSOT (#685)', NOW())
ON CONFLICT DO NOTHING;
