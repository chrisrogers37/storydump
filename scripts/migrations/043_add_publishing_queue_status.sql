-- Migration: 043_add_publishing_queue_status.sql
-- Description: Add 'publishing' to the posting_queue status CHECK constraint
--              and a posting_queue.instagram_container_id anchor column.
-- Created: 2026-07-14
-- Issue: #549 (claim-before-publish — a crash/redeploy after an Instagram
--        publish but before the DB bookkeeping must never re-serve the same
--        media and duplicate the story on the live IG account)
--
-- The auto-post paths (scheduler auto-approve + the Telegram autopost button)
-- publish to Instagram BEFORE writing any idempotency signal. The IG Graph
-- publish (create-container -> poll -> publish-container) carries NO client
-- idempotency key — the only anchor is the container_id. To make the publish
-- recoverable we claim the queue row into a new 'publishing' status and persist
-- the container_id as soon as the container is created, BEFORE the publish.
--
--   * status='publishing' + a persisted instagram_container_id => the container
--     was created and the publish outcome is unknown. The row is excluded from
--     every stale-sweep/reaper, so it persists and keeps the media out of
--     reselection (the "not already queued" selection filter) — no duplicate.
--   * A 'publishing' row also counts toward the daily cap, so a crashed
--     mid-publish story is counted exactly once (no under-count that would let
--     an over-cap post slip through).

ALTER TABLE posting_queue DROP CONSTRAINT IF EXISTS check_status;
ALTER TABLE posting_queue ADD CONSTRAINT check_status CHECK (status IN ('pending', 'processing', 'failed', 'publishing'));

-- Anchor for a claimed-but-unconfirmed publish. NULL until the IG media
-- container is created; set (together with status='publishing') immediately
-- after creation and before the publish call.
ALTER TABLE posting_queue ADD COLUMN IF NOT EXISTS instagram_container_id VARCHAR(255);

INSERT INTO schema_version (version, description, applied_at)
VALUES (43, 'Add publishing status + instagram_container_id anchor to posting_queue (#549 claim-before-publish)', NOW())
ON CONFLICT DO NOTHING;
