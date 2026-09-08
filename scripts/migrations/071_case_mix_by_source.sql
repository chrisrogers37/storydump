-- 071: category_post_case_mix.source_id — the mix keyed on the connected folder (07 §17; owner ruling 2026-09-08).
-- Identical to the 07 §17 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'category_post_case_mix' AND column_name = 'source_id' AND is_nullable = 'YES')
-- runner:postcondition SELECT NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_case_mix_current') AND EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'uq_case_mix_current_by_source')

-- The posting mix keyed on the CONNECTED FOLDER (owner ruling 2026-09-08 — sources are the
-- groups): a weight belongs to a media_sources row, never to a folder name. `category` stays as
-- the label the card shows (the source's folder_name at save time). Rows written before this
-- migration carry no source_id: the planner ignores them and the first save supersedes them.
-- No FK: the tenancy lane refuses ADD FOREIGN KEY, and a source is removed by flag, never deleted.
ALTER TABLE category_post_case_mix ADD COLUMN source_id UUID NULL;

DROP INDEX uq_case_mix_current;

CREATE UNIQUE INDEX uq_case_mix_current_by_source ON category_post_case_mix (workspace_id, source_id)
  WHERE effective_to IS NULL AND source_id IS NOT NULL;
