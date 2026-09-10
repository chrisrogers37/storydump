-- 073: dry-run posts carry their own published_via (07 §19; Dry Run Mode, owner ruling 2026-09-10).
-- Identical to the 07 §19 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_intent_via' AND pg_get_constraintdef(oid) LIKE '%dry_run%')

-- Dry Run Mode (Settings › General, owner ruling 2026-09-10): an approved post completes as if
-- published — the cap debit, the rotation, the card's line — and nothing reaches Instagram. The
-- row says so in its own column: `published_via = 'dry_run'`, no container, no media id from Meta.
-- The two checks that pin what a posted row must carry gain the branch; nothing else moves.
ALTER TABLE post_intents DROP CONSTRAINT ck_intent_via;
ALTER TABLE post_intents ADD CONSTRAINT ck_intent_via
  CHECK (published_via IN ('api','manual','legacy_backfill','dry_run'));
ALTER TABLE post_intents DROP CONSTRAINT ck_posted_complete;
ALTER TABLE post_intents ADD CONSTRAINT ck_posted_complete CHECK (
  state <> 'posted'
  OR published_via = 'legacy_backfill'
  OR (published_via = 'manual' AND cap_consumed_on IS NOT NULL)
  OR (published_via = 'dry_run' AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL)
  OR (published_via = 'api' AND ig_container_id IS NOT NULL
      AND publish_step = 'effect_confirmed' AND cap_consumed_on IS NOT NULL));
