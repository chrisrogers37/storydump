---
title: Phase 1 — the category registry and the full-depth walk
type: plan
status: draft
owner: chris
created: 2026-09-07
tags: [plan, drive, media-sync, migration, phase-1]
---

# Phase 1 — The category registry and the full-depth walk

## Summary

Add `media_categories` (keyed by Drive folder id), `media_items.category_id`, `media_sources.walk_seq` and `category_post_case_mix.category_id` in migration 070; walk the whole tree under a picked folder with the category = the top-level subfolder (the root's own files = "Unsorted"); maintain the registry's names and lifecycle from each completed walk. After this phase the planner and the card still work as in #1251 (name-keyed, unchanged), but every row carries the id the next phase switches to.

## Evidence

- Walk: `src/services/target/google_drive_adapter.py:231-362` (`list_changes`), `:364-409` (`_subfolders`), `:185` (`_subfolder_query`), `:104` (`FOLDER_LIST_CAP`); `src/services/target/drive_adapter.py:136` (`checkpoint_incomplete`).
- Sync: `src/services/target/media_sync.py:393-414` (upsert with `category`), `:311` (the stored cursor), `:338` (persistent-failure reset), `:427` (checkpoint write), `:438` (chunk chain), `:160` (re-arm).
- Schema: `054:278` (`media_items.category`), `054:295` (`uq_media_dedup`), `055:79-101` (`category_post_case_mix`), `057_grant_matrix_and_archive_schema.sql:96-110` (grant matrix — SELECT/INSERT/UPDATE to `svc_ingress`, `svc_worker`; no DELETE), `058_rls_and_policies.sql:145-147` (the `p_tenant` policy shape: `workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`; ENABLE only, no FORCE — the owner role bypasses by design).
- Pins: `tests/scripts/test_advertised_ddl.py:279` (27), `tests/scripts/test_tenancy_gate.py:377` (26 tables), `tests/scripts/test_lineage_lane.py` (ratified list ends at 069), `src/models/target/accounts_sources_media.py` (models), `tests/scripts/test_rls_runtime_harness.py` (policy census at `:150` — one `("p_tenant", <table>, "ALL", T)` row per tenant table; `_seed_tenant` at `:374`).
- Recipe: `07-security-model.md` §15 + `scripts/migrations/069_workspace_drive_grant.sql` + `scripts/advertised_ddl_manifest.json` (ordinal 13 for doc 07 is the last).

## Implementation Plan

### Dependencies
None.

### Blocks
Phase 2 (weights by id), Phase 3 (web card).

### Steps

1. **Migration `scripts/migrations/070_media_categories.sql`** and the identical block as `07-security-model.md` §16 ("The category registry — folders by id"), manifest entry (doc 07, ordinal 14, class normative), count pin 27 → 28, lineage list + 070, tenancy pin 26 → 27:
   ```sql
   CREATE TABLE media_categories (
     id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
     source_id       UUID NOT NULL,
     folder_ref      TEXT NOT NULL,           -- the Drive folder id; the picked root's id for "Unsorted"
     name            TEXT NOT NULL,           -- the folder's current Drive name, refreshed each walk
     path            TEXT NOT NULL,           -- "memes", "memes/2025" is never a category: top level only
     state           TEXT NOT NULL DEFAULT 'active'
                     CONSTRAINT ck_categories_state CHECK (state IN ('active','gone')),
     first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
     last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     last_seen_walk  BIGINT NOT NULL DEFAULT 0,
     announced_at    TIMESTAMPTZ NULL,        -- phase 2: the discovery notice, once
     created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
     updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
     CONSTRAINT uq_categories_ws_id UNIQUE (workspace_id, id),
     CONSTRAINT uq_categories_folder UNIQUE (workspace_id, source_id, folder_ref),
     CONSTRAINT fk_categories_source FOREIGN KEY (workspace_id, source_id)
       REFERENCES media_sources (workspace_id, id) ON DELETE CASCADE
   );
   CREATE TRIGGER tg_touch_media_categories BEFORE UPDATE ON media_categories
     FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
   CREATE INDEX ix_categories_active ON media_categories (workspace_id) WHERE state = 'active';
   ALTER TABLE media_categories ENABLE ROW LEVEL SECURITY;
   CREATE POLICY p_tenant ON media_categories FOR ALL TO svc_ingress, svc_worker
     USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
     WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
   GRANT SELECT, INSERT, UPDATE ON media_categories TO svc_ingress, svc_worker;
   ALTER TABLE media_items ADD COLUMN category_id UUID NULL;
   ALTER TABLE media_items ADD CONSTRAINT fk_media_category FOREIGN KEY (workspace_id, category_id)
     REFERENCES media_categories (workspace_id, id) ON DELETE SET NULL;
   CREATE INDEX ix_media_selection_by_category ON media_items (workspace_id, state, category_id);
   ALTER TABLE media_sources ADD COLUMN walk_seq BIGINT NOT NULL DEFAULT 0;
   ALTER TABLE category_post_case_mix ADD COLUMN category_id UUID NULL;
   ALTER TABLE category_post_case_mix ADD CONSTRAINT fk_case_mix_category FOREIGN KEY (workspace_id, category_id)
     REFERENCES media_categories (workspace_id, id) ON DELETE SET NULL;
   CREATE UNIQUE INDEX uq_case_mix_current_by_id ON category_post_case_mix (workspace_id, category_id)
     WHERE effective_to IS NULL AND category_id IS NOT NULL;
   UPDATE media_sources SET next_sync_at = now() WHERE provider = 'gdrive' AND state = 'active';  -- arm the first full walk
   ```
   The policy is copied from `058:145-147` (ENABLE only; `058` never uses FORCE); `ADD COLUMN` is handled by the tenancy lane (`scripts/tenancy_gate.py:274`); the `07` section is an `### §16.` heading like §10–§15. Postconditions: the table exists with RLS on; the three FKs exist; `walk_seq` exists. Models: `MediaCategory` in `src/models/target/accounts_sources_media.py`, `category_id` on `MediaItem`, `walk_seq` on `MediaSource`, `category_id` on `CategoryPostCaseMix` (`src/models/target/intent_ledger.py:56`).

2. **Adapter walk** (`google_drive_adapter.py`): replace `_subfolders` with `_folder_tree(root)` — breadth-first over `'<id>' in parents and mimeType = folder`, each entry `{"id", "name", "top": <top-level ancestor id or the root>, "path": "memes/2025"}`; bounded by `FOLDER_TREE_CAP = 2000` (said once); the repeated-token guard stays. `list_changes` start: `queue = tree`, `current = {"id": root, "name": "Unsorted", "top": root, "path": ""}`. Items gain `category_ref` (= `current["top"]`), `category_name` (the top folder's name, "Unsorted" for the root) and `category_path`. The rest of the cursor logic, the vanished-folder skip and the expired-token restart stay as in #1251.

3. **Sync** (`media_sync.py`): in phase 3 of `_run_sync`, before the item upserts, upsert the registry rows this page touched: `INSERT INTO media_categories (workspace_id, source_id, folder_ref, name, path, last_seen_walk, last_seen_at) VALUES (…, walk_seq + 1, now()) ON CONFLICT (workspace_id, source_id, folder_ref) DO UPDATE SET name = EXCLUDED.name, path = EXCLUDED.path, state = 'active', last_seen_walk = EXCLUDED.last_seen_walk, last_seen_at = now() RETURNING id, (xmax = 0) AS created`. Item upsert writes `category_id` and `category` (the name). When the walk completes (checkpoint `{"v":1}`): `UPDATE media_sources SET walk_seq = walk_seq + 1 …`; `UPDATE media_categories SET state = 'gone' WHERE source_id = :s AND last_seen_walk < <new walk_seq>`.

4. **Reads** (`category_mix.discovered_categories`, `workspaces.stats`): unchanged in this phase (name-keyed); add `category_mix.registry(executor, workspace_id) -> rows` for phase 2.

5. **Docs**: `02` §2 (the registry table, `media_items.category_id`, the checkpoint queue entry shape, `walk_seq`), `07` §16 block, `03` ruling (owner: registry by folder id; full-depth walk; Unsorted), CHANGELOG, README Live status.

## Test Plan

- Unit (`tests/src/services/target/test_google_drive_adapter.py`): a three-level tree resolves every file to its top-level folder; the root's files carry `category_ref` = root and name "Unsorted"; the tree cap is said and truncates; the cursor survives a paged nested folder; a vanished nested folder is skipped.
- Unit (a new `test_media_sync_registry.py` with scripted executors): the registry upsert SQL shape; the walk-completion stamps; the `gone` sweep runs only on a completed walk.
- Gate (`tests/scripts/test_w6_sync_gate.py`): a scripted tree → registry rows with names and paths; a rename between walks keeps the id and refreshes the name; a folder absent from a completed walk → `gone`, present again → `active`; `media_items.category_id` set and `category` text refreshed.
- Gates: replay, adopt, lineage, tenancy (27 tables), RLS runtime harness (seed one registry row per tenant), model parity.

## Verification Checklist

- `pytest tests/scripts/test_advertised_ddl.py` passes with the normative count at 28.
- `pytest tests/scripts/test_tenancy_gate.py tests/scripts/test_lineage_lane.py tests/scripts/test_advertised_ddl_replay.py tests/scripts/test_migration_runner_adopt.py tests/scripts/test_rls_runtime_harness.py` pass on postgres:15.
- `pytest tests/scripts/test_w6_sync_gate.py` passes, including the rename / gone scenarios.
- On production after deploy: `SELECT name, path, state FROM media_categories WHERE workspace_id = <TL Enterprises>` shows `Unsorted`, `memes`, `merch` active after the armed sync; `SELECT count(*) FROM media_items WHERE category_id IS NULL AND workspace_id = <ws>` is 0 after the walk.

## What NOT To Do

- Do not key anything new by folder name; the id is the key, the name is a label.
- Do not walk deeper than the registry's `top` mapping needs for category purposes; every folder is LISTED for media, but only top-level folders are categories (F2).
- Do not delete registry rows; `gone` is a state (the grant matrix has no DELETE for login roles).
- Do not change the planner or the card in this phase; both keep reading names until phase 2/3.
- Do not write any name-to-id adoption; F5 is locked as no carry-over.

## Context

area: sync · migration · adapter — effort: L — risk: medium (schema change + walk rewrite) — priority: high (supersedes #1251's caveats)
