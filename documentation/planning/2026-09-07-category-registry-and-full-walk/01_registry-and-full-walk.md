---
title: Phase 1 — the lazy full-depth walk (1a) and the category registry (1b)
type: plan
status: draft
owner: chris
created: 2026-09-07
tags: [plan, drive, media-sync, migration, phase-1]
---

# Phase 1 — the lazy full-depth walk (1a) and the category registry (1b)

## Summary

Two PRs. **1a** rewrites `list_changes` to walk the whole tree under a picked folder lazily with a v2 cursor, so nested media syncs; no schema change; categories stay name-keyed. **1b** adds `media_categories` (keyed by Drive folder id), `media_items.category_id`, `media_sources.walk_seq` and `category_post_case_mix.category_id` in migration 070, feeds the registry from the folder listing, maintains names and the active/gone lifecycle from each completed walk, and tags every media row with its category id. After 1b the planner and the card still work as in #1251 (name-keyed), but every row carries the id phase 2 switches to.

## Evidence

- Walk: `src/services/target/google_drive_adapter.py:231-362` (`list_changes`; `:247-250` queue entries `id`/`name`; `:273` the bare `page_token` branch; `:325-336` folder restart), `:364-409` (`_subfolders`), `:185` (`_subfolder_query`), `:104` (`FOLDER_LIST_CAP`); `src/services/target/drive_adapter.py:136` (`checkpoint_incomplete`).
- Sync: `src/services/target/media_sync.py:311` (stored cursor resumed as-is), `:338` (persistent-failure reset to `{"v":1}`), `:350-370` (the sync's binding notice), `:393-414` (upsert; `:403` the `category IS DISTINCT FROM` guard; `:406` `RETURNING (xmax = 0)`), `:427` (checkpoint write), `:438` (chunk chain with `serialization_key`), `:160` (re-arm nulls the cursor), `:467` (summary log line).
- Lease and restarts: `src/services/target/work_loop.py:68,99` (90 s lease, heartbeat); `05-operational-numbers.md:36` (a lease covers one checkpointed step).
- Schema: `054:278` (`media_items.category`), `054:295` (`uq_media_dedup`), `055:79-101` (`category_post_case_mix`; `:98` `uq_case_mix_current`), `057_grant_matrix_and_archive_schema.sql:96-110` (grants; no DELETE), `058_rls_and_policies.sql:145-147` (`p_tenant` shape: `workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`; ENABLE only, no FORCE), `058:149-153` (`policy_expansion.tenant_plane` is "the normative enumeration" for the 058 tables).
- Lane: `scripts/tenancy_gate.py:81-101` (`_TENANCY_IRRELEVANT`, no `UPDATE`), `:274` (`ADD COLUMN` handled), `:309-323` (constraints: `DROP CONSTRAINT` and `ADD CONSTRAINT … CHECK` only; FOREIGN KEY refused by design), `DROP INDEX` accepted (#1246).
- Pins: `tests/scripts/test_advertised_ddl.py:279` (27) and `:289` (`CREATE POLICY p_tenant ON` count `13 + 1`), `tests/scripts/test_tenancy_gate.py:377` (26 tables) and `:378` (19 tenant-keyed), `tests/scripts/test_rls_runtime_harness.py:150` (policy census, one `("p_tenant", <table>, "ALL", T)` row per tenant table), `:374` (`_seed_tenant`), `:704` (`matrix == 16`), `tests/scripts/test_lineage_lane.py` (ratified list ends at 069), `src/models/target/accounts_sources_media.py:250` (`MediaItem`), `src/models/target/intent_ledger.py:56,73` (`CategoryPostCaseMix`, its by-name index), `tests/src/models/test_enum_ssot_parity.py` (enum parity; there is no DDL↔model gate — model changes are reviewed by hand).
- Recipe: `07-security-model.md` §15 + `scripts/migrations/069_workspace_drive_grant.sql` + `scripts/advertised_ddl_manifest.json` (doc 07's last ordinal is 13).
- Existing gate to extend: `tests/scripts/test_w6_sync_gate.py:1119-1157` (the moved-file scenario), `TestSubfoldersAreCategories`.

## Implementation Plan

### Dependencies
None.

### Blocks
1b blocks phase 2; 1a blocks 1b.

### Steps

**1a — the lazy full-depth walk (one PR, no migration)**

1. `google_drive_adapter.py`: cursor v2 `{"v":2,"walk":<token>,"current":{"id","name","top","top_name","path"},"queue":[…],"page_token"?}`. `list_changes(config, checkpoint, …)`: a stored cursor that is not `v == 2` with a `walk` is **ignored** (start over). Start: list the root's immediate subfolders (`_subfolders`, `FOLDER_LIST_CAP`, name order) → queue entries with `top = <own id>`, `top_name = <own name>`, `path = <name>`; `current` = the root (`top` = root id, `top_name = None`, `path = ""`). When a folder is popped it lists **its** subfolders once (same helper) and appends them with the inherited `top`/`top_name` and `path = parent.path + "/" + name`, then pages its media. `FOLDER_WALK_CAP = 2000` folders seen per walk, counted in the cursor (`seen`); past it, no more subfolders are queued and `truncated = true` is carried to completion and logged once. Items gain `category_ref` (= `current["top"]`) and `category_path`; `category` = `current["top_name"]` (NULL for the root's own files, as today). The vanished-folder skip and the expired-token restart stand; the bare `page_token` shape (`:350-353`) is never emitted — a chunk carries the whole cursor.
2. `media_sync.py`: the chunk job's payload carries the cursor's `walk` token; on resume, if the stored cursor is missing or its `walk` differs, the chunk starts a fresh walk instead of resuming a foreign page. The summary line (`:467`) adds `folders_seen` and `truncated`. The persistent-failure reset writes `{"v":2}`.
3. `drive_adapter.checkpoint_incomplete` unchanged (truthy `current`/`queue`/`page_token`). `02` §2 documents the v2 cursor; CHANGELOG entry.

**1b — the registry (one PR, migration 070)**

4. **Migration `scripts/migrations/070_media_categories.sql`** and the identical block as `07-security-model.md` `### §16.` ("The category registry — folders by id"), manifest entry (doc 07, ordinal 14, class normative), advertised count 27 → 28, `p_tenant` literal count `13 + 1` → `13 + 2` (the policy is literal in §16, as §15 is; `policy_expansion.tenant_plane` stays the 058 enumeration — say so in the block comment), tenancy pins 26 → 27 and 19 → 20, harness census + `_seed_tenant` row and `matrix == 17`, lineage list + 070:
   ```sql
   CREATE TABLE media_categories (
     id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
     source_id       UUID NOT NULL,
     folder_ref      TEXT NOT NULL,           -- the Drive folder id; the picked root's own id when is_root
     name            TEXT NOT NULL,           -- the folder's current Drive name; "Unsorted" for the root row
     is_root         BOOLEAN NOT NULL DEFAULT false,
     state           TEXT NOT NULL DEFAULT 'active'
                     CONSTRAINT ck_categories_state CHECK (state IN ('active','gone')),
     first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
     last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     last_seen_walk  BIGINT NOT NULL DEFAULT 0,
     announced_at    TIMESTAMPTZ NULL,        -- phase 2: said to the bindings, once
     debuted_at      TIMESTAMPTZ NULL,        -- phase 2: the debut post drawn, once
     created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
     updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
     CONSTRAINT uq_categories_ws_id UNIQUE (workspace_id, id),
     CONSTRAINT uq_categories_folder UNIQUE (workspace_id, source_id, folder_ref),
     CONSTRAINT fk_categories_source FOREIGN KEY (workspace_id, source_id)
       REFERENCES media_sources (workspace_id, id) ON DELETE CASCADE
   );
   CREATE TRIGGER tg_touch_media_categories BEFORE UPDATE ON media_categories
     FOR EACH ROW EXECUTE FUNCTION trg_touch_updated_at();
   CREATE INDEX ix_categories_active ON media_categories (workspace_id, source_id) WHERE state = 'active';
   ALTER TABLE media_categories ENABLE ROW LEVEL SECURITY;
   CREATE POLICY p_tenant ON media_categories FOR ALL TO svc_ingress, svc_worker
     USING      (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
     WITH CHECK (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
   GRANT SELECT, INSERT, UPDATE ON media_categories TO svc_ingress, svc_worker;
   ALTER TABLE media_items ADD COLUMN category_id UUID NULL;               -- no FK: F8
   CREATE INDEX ix_media_selection_by_category ON media_items (workspace_id, state, category_id);
   ALTER TABLE media_sources ADD COLUMN walk_seq BIGINT NOT NULL DEFAULT 0;
   ALTER TABLE category_post_case_mix ADD COLUMN category_id UUID NULL;   -- no FK: F8
   DROP INDEX uq_case_mix_current;                                        -- the name is a label, not a key
   CREATE UNIQUE INDEX uq_case_mix_current_by_id ON category_post_case_mix (workspace_id, category_id)
     WHERE effective_to IS NULL AND category_id IS NOT NULL;
   ```
   No `UPDATE`, no `ALTER … ADD FOREIGN KEY` (the lane refuses both; F8). `-- runner:postcondition`s: the table exists with RLS enabled and the policy present; `media_items.category_id`, `media_sources.walk_seq`, `category_post_case_mix.category_id` exist; `uq_case_mix_current` is gone and `uq_case_mix_current_by_id` present. Models: `MediaCategory` in `src/models/target/accounts_sources_media.py`; `category_id` on `MediaItem`; `walk_seq` on `MediaSource`; `category_id` on `CategoryPostCaseMix` and its by-name index removed (`intent_ledger.py:73`) — reviewed against the block by hand (no DDL↔model gate exists).

5. **Sync — the registry from the listing** (`media_sync.py`): on the walk's first chunk (the cursor's `walk` token is `walk_seq + 1`, read from the source row) the adapter returns the root's top-level listing alongside the page; the sync upserts one registry row per listed folder plus the root row (`is_root`, name "Unsorted"): `INSERT … ON CONFLICT (workspace_id, source_id, folder_ref) DO UPDATE SET name = EXCLUDED.name, state = 'active', last_seen_walk = EXCLUDED.last_seen_walk, last_seen_at = now() RETURNING id, (xmax = 0) AS created`, always with `workspace_id` in the predicate (BYPASSRLS in production). Rows are upserted **before** items and independent of them, so empty folders have rows and 0-file Unsorted exists.
6. **Sync — items**: the upsert sets `category_id` (looked up from `category_ref` in the walk's registry map) and `category` (the name); the guard becomes `WHERE media_items.category IS DISTINCT FROM EXCLUDED.category OR media_items.category_id IS DISTINCT FROM EXCLUDED.category_id`, keeping the same-file conditions from #1251 — so every pre-070 row gains its id on the first walk.
7. **Sync — completion**: when the walk completes and the cursor's `walk` equals `walk_seq + 1` of the source row at that moment: `UPDATE media_sources SET walk_seq = :walk WHERE id = :s AND workspace_id = :ws`; `UPDATE media_categories SET state = 'gone' WHERE workspace_id = :ws AND source_id = :s AND last_seen_walk < :walk AND state = 'active'`. A completing cursor with a stale token completes without sweeping (a re-pick or a persistent-failure reset happened mid-walk; the next walk heals it). The summary line adds `categories_seen` and `gone`.
8. **Reads**: `category_mix.discovered_categories` and `workspaces.stats` unchanged in this phase (name-keyed); add `category_mix.registry(executor, *, workspace_id) -> rows` (id, source_id, folder_name from `config.folder_name`, name, is_root, state, media_count over `state = 'available'`) for phase 2.
9. **Docs**: `02` §2 (the registry table, `media_items.category_id`, `walk_seq`, the v2 cursor), `07` §16 block, `03` ruling in "Supersedes (the 2026-09-06 one-level ruling) / Kept / What it costs" form, recording the schema change as the owner's F1 approval; CHANGELOG; README Live status.

## Test Plan

- 1a unit (`tests/src/services/target/test_google_drive_adapter.py`): a three-level tree resolves every file to its top-level folder (`category_ref`, `category`, `category_path`); the root's own files carry `category_ref` = root and `category` NULL; a folder is listed for subfolders exactly once per walk; a #1251-shape cursor is ignored and the walk starts over; the cap stops queuing and `truncated` reaches completion; the cursor survives a paged nested folder; a vanished nested folder is skipped; the bare `page_token` shape is never emitted.
- 1a unit (`test_media_sync_walk.py`, scripted executors): a chunk whose `walk` differs from the stored cursor starts a fresh walk.
- 1a gate (`test_w6_sync_gate.py`): a scripted tree with a file two levels deep lands in `media_items` with `category` = its top-level folder's name (the epic's second headline problem, end to end).
- 1b unit (`test_media_sync_registry.py`): the registry upsert SQL shape carries `workspace_id`; the item guard fires on an unchanged name with a changed id; the completion stamps; the sweep is skipped on a stale token.
- 1b gate (`test_w6_sync_gate.py`): registry rows for the root and each top-level folder including an empty one; a rename between walks keeps the id and refreshes the name; a folder absent from a completed walk → `gone`, present again → `active`; a pre-070 row (seeded with `category` set and `category_id` NULL) gains its id on the next walk; a re-pick mid-walk does not sweep; `media_items.category_id` set and `category` text refreshed.
- 1b gates: replay, adopt, lineage, tenancy (27 tables, 20 tenant-keyed), RLS runtime harness (census row, seed, `matrix == 17`), advertised DDL (28, `13 + 2`).

## Verification Checklist

- 1a: `pytest tests/src/services/target/test_google_drive_adapter.py tests/scripts/test_w6_sync_gate.py` green; production after deploy: a file the owner places two levels deep under `storydump-media` appears in the library after Sync now.
- 1b: `pytest tests/scripts/test_advertised_ddl.py tests/scripts/test_tenancy_gate.py tests/scripts/test_lineage_lane.py tests/scripts/test_advertised_ddl_replay.py tests/scripts/test_migration_runner_adopt.py tests/scripts/test_rls_runtime_harness.py tests/scripts/test_w6_sync_gate.py` green on postgres:15.
- 1b production (read-only): `SELECT name, is_root, state FROM media_categories WHERE workspace_id = <TL Enterprises>` shows `Unsorted` (root), `memes`, `merch` active after the next completed walk; `SELECT count(*) FROM media_items WHERE category_id IS NULL AND workspace_id = <ws>` is 0.

## What NOT To Do

- Do not key anything new by folder name; the id is the key, the name is a label.
- Do not feed the registry from items; it comes from the folder listing, so empty folders have rows.
- Do not delete registry rows; `gone` is a state (no login role holds DELETE).
- Do not change the planner or the card in this phase; both keep reading names until phase 2/3.
- Do not write any name-to-id adoption (F5) and do not put an `UPDATE` or an `ADD FOREIGN KEY` in 070 (F8).
- Do not enumerate the tree up front; discovery is one listing per popped folder (the cycle-1 counter-plan).

## Context

area: adapter · sync · migration — effort: 1a M, 1b L — risk: medium (walk rewrite + schema change) — priority: high (1a fixes the failure observed in production on 2026-09-06)
