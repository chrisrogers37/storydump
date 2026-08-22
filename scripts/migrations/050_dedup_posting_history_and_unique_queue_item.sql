-- Migration: 050_dedup_posting_history_and_unique_queue_item.sql
-- Description: #695 — archive the duplicate posting_history groups, reduce each
--   to one row, and enforce one history row per queue item from here on.
--
--   ONE TRANSACTION, AND THE ORDER INSIDE IT IS THE CONTRACT. The archive is
--   written BEFORE the delete. posting_history is a permanent audit log with no
--   soft-delete and no second copy, and this database's point-in-time window is
--   24 hours — so without the archive step the removed rows are recoverable for
--   one day and then not at all. Archiving inside the same transaction makes the
--   evidence outlive the delete unconditionally: either both land or neither
--   does.
--
--   WHAT THE ARCHIVED ROWS ARE. Not all of them are redundant bookkeeping.
--   Three of the seven removed rows carry their own distinct
--   instagram_story_id, meaning the story really did publish a second and a
--   third time; media_items.times_posted agrees independently. Those rows are
--   the only surviving record that it happened, which is the reason this file
--   archives rather than simply deleting.
--
--   KEEP RULE: earliest posted_at wins, ties broken by created_at then id so the
--   rank is total and the result is deterministic on replay. The alternative
--   rule — prefer the row carrying an Instagram story id — selects the same row
--   in every affected group, so the two are indistinguishable on this data and
--   the simpler, total one is used.
--
--   WHY THE INDEX IS PARTIAL. queue_item_id is nullable by contract: history
--   rows outlive the queue rows they came from, and a row whose queue link has
--   been cleaned up carries NULL. Multiple NULLs must stay legal, so the
--   uniqueness applies only WHERE queue_item_id IS NOT NULL. This mirrors
--   idx_posting_history_chat_settings_id, which is partial for the same reason.
--
--   NOT CONCURRENTLY, DELIBERATELY. The table is ~1.7 MB / ~4.3k rows and the
--   index builds in tens of milliseconds, so the brief ACCESS EXCLUSIVE lock
--   costs nothing. CONCURRENTLY would force this file out of one-transaction
--   execution, where a failure can leave a partially applied file and an INVALID
--   index behind. Keeping it in a single transaction is worth more than the lock
--   is worth avoiding.
--
--   APPLICATION-LEVEL IDEMPOTENCY STILL EXISTS AND IS STILL FIRST.
--   HistoryRepository.create_idempotent is read-then-insert, so a sufficiently
--   tight replay can still race it; this index is the backstop that turns that
--   race into a rejected write instead of a duplicate row. The rejection surfaces
--   as an IntegrityError, is rolled back with the rest of its finalize by
--   atomic_session, and is caught and logged one frame up — no unhandled path.
--
-- Related: #695, #680, #684, #551, PR #694.

-- runner:postcondition SELECT to_regclass('public.posting_history_dedup_archive') IS NOT NULL
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_posting_history_queue_item_id')

-- 1. ARCHIVE. Every row of every duplicated group, not just the rows about to be
--    removed, so a reader of the archive can see the whole group as it stood and
--    tell which row survived.
CREATE TABLE posting_history_dedup_archive AS
SELECT h.*, now() AS archived_at
FROM posting_history h
WHERE h.queue_item_id IN (
    SELECT queue_item_id
    FROM posting_history
    WHERE queue_item_id IS NOT NULL
    GROUP BY queue_item_id
    HAVING count(*) > 1
);

COMMENT ON TABLE posting_history_dedup_archive IS
  'Verbatim copy of every posting_history row that belonged to a duplicated '
  'queue_item_id group, captured immediately before the surplus rows were '
  'removed. Retained indefinitely: for three of these rows a distinct '
  'instagram_story_id is the only evidence that a story published more than '
  'once. Read-only by convention; nothing writes here after the reduction.';

-- 2. REDUCE. Keep rank 1 per queue item, delete the rest.
DELETE FROM posting_history ph
USING (
    SELECT id
    FROM (
        SELECT id,
               row_number() OVER (
                   PARTITION BY queue_item_id
                   ORDER BY posted_at, created_at, id
               ) AS rn
        FROM posting_history
        WHERE queue_item_id IS NOT NULL
    ) ranked
    WHERE ranked.rn > 1
) surplus
WHERE ph.id = surplus.id;

-- 3. ENFORCE.
CREATE UNIQUE INDEX uq_posting_history_queue_item_id
    ON posting_history (queue_item_id)
    WHERE queue_item_id IS NOT NULL;

INSERT INTO schema_version (version, description, applied_at)
VALUES (50, 'Dedup posting_history + UNIQUE(queue_item_id) partial index (#695)', NOW())
ON CONFLICT DO NOTHING;
