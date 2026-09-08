-- 070: media_items.folder_path — the full-depth walk (07 §16; owner ruling 2026-09-08 — sources are the groups).
-- Identical to the 07 §16 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'media_items' AND column_name = 'folder_path' AND data_type = 'text' AND is_nullable = 'YES')

-- The folder path of a media item under its connected folder (owner ruling 2026-09-08 — sources
-- are the groups; every folder under a connected folder is walked, to any depth, lazily). "" for
-- a file directly in the connected folder; NULL for an adapter that has no notion of folders. A
-- label the sync refreshes on every walk beside `category` (the TOP-LEVEL folder's name): nothing
-- is keyed by either — the group that carries a weight is the source (`media_sources`), next PR.
ALTER TABLE media_items ADD COLUMN folder_path TEXT NULL;
