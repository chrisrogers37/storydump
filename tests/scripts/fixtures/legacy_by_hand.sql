-- The one legacy table no migration creates: made by hand in production during #941 (a dedup
-- archive of posting_history rows), and measured there on 2026-09-17 by the tear-out's read-only
-- probe (phase 03 step 1): 19 columns, no constraints, no indexes. Applied beside the legacy seed
-- (`scripts/setup_database.sql`) INTO `public`, before the 051 schema move, so it rides into
-- `legacy` the way production's did — then the 3f snapshot file (078) copies it by name like the
-- other fifteen. `tests/scripts/legacy_inventory.py::HAND_MADE` names it; the two are pinned equal.
CREATE TABLE posting_history_dedup_archive (
    id uuid,
    media_item_id uuid,
    queue_item_id uuid,
    queue_created_at timestamp without time zone,
    queue_deleted_at timestamp without time zone,
    scheduled_for timestamp without time zone,
    posted_at timestamp without time zone,
    status character varying(50),
    success boolean,
    instagram_media_id text,
    instagram_permalink text,
    posted_by_user_id uuid,
    posted_by_telegram_username text,
    created_at timestamp without time zone,
    instagram_story_id text,
    posting_method character varying(20),
    chat_settings_id uuid,
    error_message text,
    archived_at timestamp with time zone
);
