-- Migration: 044_backfill_media_items_chat_settings_id.sql
-- Description: Stamp ownership on any NULL-owned media_items row by deriving the
--              sole active group tenant. Insurance for the #412 tenant-ownership
--              enabler: sync now stamps chat_settings_id at index time, and this
--              backfill closes any pre-existing NULL-owned rows so #542's
--              tenant-scoped selection cannot silently drop them from the pool.
-- Issue: #412 (unblocks #542)
--
-- Ownership derivation: media_items records no tenant key other than
-- chat_settings_id, so per-row attribution is impossible — the only sound rule
-- is a single blanket assignment (as migration 018 did for posting_queue). That
-- is unambiguous only while exactly one real (group) tenant owns data. The
-- subquery below DERIVES that tenant from its known group chat rather than
-- hard-coding the UUID, so the value stays truthful if the tenant row is
-- rebuilt. Correct as long as a single active group tenant exists; re-verify
-- before a second real group tenant onboards media, after which NULL-owned rows
-- become unattributable and this rule must be revisited.
--
-- Idempotent and self-guarding: the WHERE clause only touches NULL-owned rows,
-- and a derivation that matches no tenant resolves to NULL and no-ops (a stale
-- predicate fails safe, never mis-stamps). Ships as defensive insurance.
--
-- DO NOT APPLY WITHOUT RATIFICATION. This is a data migration against production
-- media ownership; apply only on explicit sign-off, never via CI or on merge.

BEGIN;

UPDATE media_items
SET chat_settings_id = (
    SELECT id FROM chat_settings
    WHERE telegram_chat_id = -1003688539654
)
WHERE chat_settings_id IS NULL;

INSERT INTO schema_version (version, description, applied_at)
VALUES (
    44,
    'Backfill media_items.chat_settings_id from the sole active group tenant (#412)',
    NOW()
)
ON CONFLICT DO NOTHING;

COMMIT;
