-- 149: bound prior-revision reads to one archive item during redaction (#1644).
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_index WHERE indexrelid=to_regclass('public.ix_archive_tombstone_item_revision') AND indrelid=to_regclass('public.instagram_item_tombstones') AND indisvalid AND indisready AND indnkeyatts=4 AND indnatts=4 AND pg_get_indexdef(indexrelid,1,true)='workspace_id' AND pg_get_indexdef(indexrelid,2,true)='source_id' AND pg_get_indexdef(indexrelid,3,true)='media_item_id' AND pg_get_indexdef(indexrelid,4,true)='observation_revision')

CREATE INDEX ix_archive_tombstone_item_revision ON instagram_item_tombstones (workspace_id, source_id, media_item_id, observation_revision);
