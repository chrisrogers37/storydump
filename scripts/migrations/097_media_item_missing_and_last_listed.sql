-- 097: the sync retires what the publish can never fetch (07 §40; #1545).
-- Identical to the 07 §40 block; the advertised-DDL manifest pins the two together.
--
-- A Drive file the publish can never fetch stalled the folder it lives in: one deleted from Drive
-- after it synced, or one past the publish's byte cap (`vocabulary.PUBLISH_MAX_BYTES`). Every slot
-- for that folder redrew it until someone tapped Skip. The sync, where Drive's state enters, now:
--   * writes the listing's size to `file_size` (054's column, which nothing wrote) and lands a file
--     past the cap as `unsupported`, judged again each time a walk lists it;
--   * stamps `last_listed_at` on every row a walk lists. A walk spans chunks, each one page in its
--     own transaction, so the rows are where the walk's listing is kept. The last page of a walk
--     that saw the whole tree moves the source's `available` and `unsupported` rows that it did not
--     list, and that were created and last listed before the walk began, to `missing`.
-- `missing` is not `removed`. `removed` is the folder's retirement, which a re-pick undoes
-- (`rearm_after_connect`); `missing` is the file's own absence, which only a listing undoes. So
-- ck_media_state learns the value, and the miss door (fn_planned_misses, 089) names it
-- `item_missing`, after `item_unsupported` and before the locks; the draw takes `available` items
-- only, and the serve door does for a planned story (a cadence row is served whatever its media's
-- state, as before). CREATE OR REPLACE keeps the door's owner (svc_maintenance) and its EXECUTE
-- grant, and its search_path ends in pg_temp.
--
-- Adoption evidence (#997): the column, the CHECK naming the value and the door naming its reason,
-- catalog state this file creates. Each probe reads false, without raising, on a database without
-- them.
--
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'media_items' AND column_name = 'last_listed_at' AND data_type = 'timestamp with time zone' AND is_nullable = 'YES')
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = 'public' AND t.relname = 'media_items' AND c.conname = 'ck_media_state' AND position('missing' IN pg_get_constraintdef(c.oid)) > 0)
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = 'fn_planned_misses' AND position('item_missing' IN p.prosrc) > 0)

ALTER TABLE media_items ADD COLUMN last_listed_at TIMESTAMPTZ NULL;

ALTER TABLE media_items DROP CONSTRAINT ck_media_state;

ALTER TABLE media_items ADD CONSTRAINT ck_media_state
  CHECK (state IN ('available','unsupported','removed','missing'));

CREATE OR REPLACE FUNCTION fn_planned_misses(p_limit int, p_late interval)
RETURNS TABLE (o_id uuid, o_workspace_id uuid, o_reason text, o_schedule_slot_at timestamptz, o_file_name text, o_handle varchar, o_tz text, o_scheduled_by_user_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT d.id, d.workspace_id, d.reason, d.schedule_slot_at, d.file_name, d.handle, d.tz,
         d.scheduled_by_user_id
    FROM (SELECT i.id, i.workspace_id, i.schedule_slot_at, i.scheduled_by_user_id,
                 m.file_name, a.handle, w.tz,
                 CASE
                   WHEN m.id IS NULL OR m.state = 'removed' THEN 'item_removed'
                   WHEN m.state = 'unsupported' THEN 'item_unsupported'
                   WHEN m.state = 'missing' THEN 'item_missing'
                   WHEN EXISTS (SELECT 1 FROM post_locks l
                                 WHERE l.workspace_id = i.workspace_id
                                   AND l.ig_account_id IS NULL
                                   AND l.media_item_id = i.media_item_id
                                   AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                   AND (l.expires_at IS NULL OR l.expires_at > now()))
                     THEN 'item_locked'
                   WHEN a.state IS NULL OR a.state NOT IN ('active', 'reauth_required')
                     THEN 'account_removed'
                   WHEN i.schedule_slot_at > now() - p_late THEN NULL
                   WHEN w.state <> 'active' OR w.is_paused THEN 'paused'
                   ELSE 'late'
                 END AS reason
            FROM post_intents i
            JOIN workspaces w ON w.id = i.workspace_id
            LEFT JOIN media_items m ON m.id = i.media_item_id
             AND m.workspace_id = i.workspace_id
            LEFT JOIN ig_accounts a ON a.id = i.ig_account_id
             AND a.workspace_id = i.workspace_id
           WHERE i.state = 'scheduled' AND i.origin = 'planned'
             AND i.schedule_slot_at <= now() AND NOT i.cancel_requested) d
   WHERE d.reason IS NOT NULL
   ORDER BY d.schedule_slot_at LIMIT p_limit
$$;

COMMENT ON FUNCTION fn_planned_misses(p_limit int, p_late interval) IS
  'The planned stories that will not be served, across every workspace: each due, scheduled, unflagged planned intent that cannot be served (item_removed, item_unsupported, item_missing, item_locked, account_removed, in that precedence) or whose p_late window has passed (paused when its workspace was not taking posts, else late) — the complement of fn_prompts_due''s planned rows. The worker expires each with its reason and tells the bound chats in the same transaction, per workspace under that workspace''s tenant. STRICT: a NULL window lists nothing. A SECURITY DEFINER read owned by svc_maintenance; EXECUTE for svc_worker (089, #1413; 097, #1545).';
