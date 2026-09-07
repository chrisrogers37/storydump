---
title: Category registry keyed by folder id, and a full-depth Drive walk (epic)
type: plan
status: draft
owner: chris
created: 2026-09-07
tags: [plan, drive, media-sync, categories, scheduler, web, epic]
---

# Category registry keyed by folder id, and a full-depth Drive walk

## Summary

#1251 (merged 2026-09-06) made a picked Drive folder's immediate subfolders into categories, keyed by the folder's **name**, and weighted how often each posts. Two properties of that build will glitch across a wide user base: a renamed folder strands its weight under the old name, and folders nested deeper than one level are never walked, so their media silently never syncs. This epic replaces name-keyed categories with a per-workspace **registry keyed by Drive folder id** (names refreshed on every sync; a lifecycle of active/gone), walks the **whole tree** under a picked folder (the category is the top-level subfolder; files directly in the picked folder are an **Unsorted** category that can be weighted), gives folders discovered after the mix was set an **auto** weight so they post without anyone touching Settings, and rebuilds the Settings card on the registry. The weighted draw from #1251 (weighted over categories with eligible media, least-recently-posted rotation, locks) is kept.

Mission alignment (`PROJECT_MISSION.md`): *zero-friction content automation* and *simple over powerful* — a folder rename or a new subfolder must never require a settings change or produce silent non-posting; the registry makes folder structure a fact the product tracks rather than a string a person maintains.

## Evidence (current state, verified 2026-09-07)

- `scripts/migrations/054_accounts_sources_media_tables.sql:278` — `media_items.category TEXT NULL`; `:295` `uq_media_dedup UNIQUE (workspace_id, content_hash)`.
- `scripts/migrations/055_intent_ledger_tables.sql:79-101` — `category_post_case_mix` keyed by `(workspace_id, category TEXT)` with `uq_case_mix_current` partial on `effective_to IS NULL`; D23 (`03-decision-record.md`) keeps it row-shaped, sum-to-one service-enforced.
- `src/services/target/google_drive_adapter.py:231` `list_changes` — one-level walk with cursor `current`/`queue`/`page_token`; `:364` `_subfolders` (name order, `FOLDER_LIST_CAP = 500` at `:104`); `src/services/target/drive_adapter.py:136` `checkpoint_incomplete`.
- `src/services/target/media_sync.py:393-414` — the upsert writes `category` (name) and updates it only for the same file moving.
- `src/services/target/scheduler.py:306` `execute_plan_slot` — `:387` reads `category, ratio FROM category_post_case_mix … effective_to IS NULL`; `:419` weighted draw over categories with eligible media; `:382` order `last_posted_at NULLS FIRST, created_at`.
- `src/services/target/category_mix.py` — `normalize`, `set_mix` (advisory lock, supersede, inserts), `current_mix`, `discovered_categories` (by name).
- `src/api/routes/v1.py:627,642` — `GET`/`PUT /workspaces/{ws}/category-mix` by name.
- `landing/src/lib/category-mix.ts:24,53` `percentRows`/`toMix`; `landing/src/components/dashboard/settings/category-weights-card.tsx:22` `CategoryWeightsCard`.
- `src/services/target/workspaces.py:488` `stats.media_by_category` groups by the name.
- Legacy reference only: `src/services/media_sources/google_drive_provider.py:162-182` (one-level walk, subfolder name = category).
- Pins that move with a schema change: `tests/scripts/test_advertised_ddl.py:279` (27 normative blocks), `tests/scripts/test_tenancy_gate.py:377` (26 tenant tables), `tests/scripts/test_lineage_lane.py` ratified list, `src/models/target/accounts_sources_media.py` (model CHECKs), `tests/scripts/test_rls_runtime_harness.py` (seeds every tenant table).

## Architecture

**Registry.** `media_categories` — one row per (workspace, source, Drive folder id) for the picked root's top-level subfolders plus one row for the root itself (`folder_ref` = the picked folder's id, `name` = "Unsorted"). Columns: `id`, `workspace_id`, `source_id`, `folder_ref`, `name` (current Drive name, refreshed each sync), `path`, `state` (`active` | `gone`), `first_seen_at`, `last_seen_at`, `last_seen_walk`, `announced_at`, timestamps. Tenant-keyed, RLS `p_tenant` for `svc_ingress`/`svc_worker`, grants SELECT/INSERT/UPDATE (never DELETE — gone is a state).

**Media.** `media_items.category_id` (composite FK to the registry, `ON DELETE SET NULL`), beside the existing `category` text kept as the denormalized display name (stats and the library keep working unchanged). The sync writes both.

**Walk.** `list_changes` first enumerates the folder TREE under the picked root breadth-first (bounded at `FOLDER_TREE_CAP`, the cut said), recording for each folder its top-level ancestor; then lists media one folder page per call as today. Items carry `category_ref` (the top-level folder id, or the root's id for its own files), `category_name`, `category_path`. The cursor shape stays `current`/`queue`/`page_token`; queue entries gain `top` and `path`.

**Lifecycle.** `media_sources.walk_seq` counts completed walks. Each walk stamps the registry rows it sees with `last_seen_walk = walk_seq + 1`; when the walk completes, rows of that source with `last_seen_walk < walk_seq` become `gone`; a gone folder seen again becomes `active` with its name refreshed. Renames change `name` only.

**Weights.** `category_post_case_mix.category_id` (composite FK to the registry; nullable only so rows written before phase 2 remain valid history) becomes the key; `category` text stays as a label. Rows without an id are ignored by the phase-2 planner and superseded by the first save from the new card (F5). The planner joins mix rows to active registry rows by id and counts eligible media by `category_id`. **Auto weights (F4):** an active registry row with eligible media and no current mix row is drawn as if it had weight = (its share of the workspace's eligible media) × (the mean explicit weight), so a newly discovered folder posts at a sensible rate without silently taking over or staying silent. Discovery is said once per registry row to the workspace's bindings (`announced_at`).

**Web.** The card lists registry rows: name (path muted), media count, a badge for gone rows, a percentage input for explicit rows and "auto (≈x %)" for the rest; explicit percentages must add up to 100; a gone row keeps its weight visible so it can be moved. Renames are invisible.

## Decision Forks

All seven forks were served one at a time and locked by the owner on 2026-09-07; the leans stood except F5, which the production check reframed.

**F1 — Registry shape.** Context: where does a category's identity live?
Options: (a) a new tenant table `media_categories` with `media_items.category_id`; (b) reuse `media_items.category` as the folder id and add a names table; (c) a JSON registry inside `media_sources.config`.
Lean: **(a)** — a row per folder gives lifecycle, names, counts and FKs; (b) breaks every reader of `category` as a name; (c) cannot be joined or constrained.
Ratifier: owner. Status: **locked** 2026-09-07 (owner, served one fork at a time in session; lean taken).

**F2 — Category depth.** Context: a walk of any depth must map each file to one category.
Options: (a) the top-level subfolder under the picked root, nested folders inherit; (b) the deepest folder; (c) a per-source `category_depth` setting.
Lean: **(a)** now — the grouping people think in; the registry row's `path` keeps (c) possible without a data-model change.
Ratifier: owner. Status: **locked** 2026-09-07 (owner, served one fork at a time in session; lean taken).

**F3 — Files directly in the picked folder.** Options: (a) a real registry row "Unsorted" (`folder_ref` = the root's id) that can be weighted like any other; (b) uncategorized (`NULL`), posting only when no weighted category has media (today).
Lean: **(a)** — one rule for every file; "post only as a fallback" is the kind of hidden behaviour that produces support questions.
Ratifier: owner. Status: **locked** 2026-09-07 (owner, served one fork at a time in session; lean taken).

**F4 — Folders discovered after the mix was set.** Options: (a) auto = share of the unassigned remainder, proportional to media; when explicit weights already sum to 100 the new folder gets 0 and the card and a one-time group notice say so; (b) auto = media share × the mean explicit weight, renormalized with the explicit weights, so a new folder always posts at a sensible rate; (c) equal share with every explicit row.
Lean: **(b)** — the only option under which "add a folder" never silently changes to "and now set a weight"; the card shows the effective percentage so nothing is hidden.
Ratifier: owner. Status: **locked** 2026-09-07 (owner, served one fork at a time in session; lean taken).

**F5 — Transition of name-keyed weight rows.** Context: production held zero `category_post_case_mix` rows on 2026-09-07 (read-only check; media counts memes 3,477 / merch 1,077). Options: (a) no carry-over — the new planner and API know only ids; a name-keyed row set before phase 2 is ignored by the planner and superseded by the first save from the new card; (b) adopt-by-name on the first walk; (c) a migration-time backfill (impossible: ids exist only after a walk).
Lean: **(a)** — nothing to carry, so no matching rule to test or explain.
Ratifier: owner. Status: **locked** 2026-09-07 (owner: "New system with newly synced items should have the ID approach"; the legacy system's rows are not involved).

**F6 — `media_items.category` text.** Options: (a) keep as the denormalized display name refreshed by the sync, add `category_id`; (b) drop it and join everywhere.
Lean: **(a)** — `stats.media_by_category`, the library and the case-mix history read a name today; the id is the key, the text is the label.
Ratifier: owner. Status: **locked** 2026-09-07 (owner, served one fork at a time in session; lean taken).

**F7 — Phasing.** Options: (a) three PRs in sequence (registry + walk; weights by id + auto; web card); (b) one PR.
Lean: **(a)** — each lands green and reviewable on its own; (b) is a 2,000-line review.
Ratifier: owner. Status: **locked** 2026-09-07 (owner, served one fork at a time in session; lean taken).

## Implementation Plan

### Dependencies
None — builds on #1251 as merged (5b3437d).

### Blocks
The Instagram publish leg (#1220 step 3) does not depend on this; the setup wizard's legacy category card (`category-mix-card.tsx`) is retired by phase 3.

### Steps
1. Phase 1 — `01_registry-and-full-walk.md`: migration 070, the registry, the full-depth walk, the lifecycle. One PR.
2. Phase 2 — `02_weights-by-id-and-auto.md`: the draw and the mix service by id, auto weights, API v2, the discovery notice. One PR.
3. Phase 3 — `03_web-card-on-the-registry.md`: the Settings card on the registry. One PR.
4. Close the loop: `03-decision-record.md` ruling, README Live status, CHANGELOG, and a production read-only check that the TL Enterprises registry shows Unsorted / memes / merch and the 70/30 saved from the new card carries ids.

## Test Plan

Per phase (see each doc). Tiers: unit (`tests/src/…`), gate on postgres:15 (`tests/scripts/…`), web (`vitest`, `tsc`, `eslint`). Tests are written first at each tier.

## Verification Checklist

- Every phase doc's checklist is green.
- `pytest tests/scripts/test_advertised_ddl.py tests/scripts/test_tenancy_gate.py tests/scripts/test_lineage_lane.py tests/scripts/test_rls_runtime_harness.py` green after phase 1 (pins 28 / 27).
- Production after phase 3: renaming a Drive subfolder and syncing leaves the weight on the row; a new subfolder appears in the card as auto and posts within its next eligible slot.

## What NOT To Do

- Do not build phases 2 or 3 before phase 1 is merged; the ids they key on come from the walk.
- Do not touch the legacy provider in `src/services/media_sources/`; it is reference only.
- Do not fold the picker counts (#1253) or shared drives (#1248) into this epic.

## Context

area: sync · scheduler · api · web · docs — effort: L + M + M — risk: medium — priority: high (the owner's stated concern about renames and nested folders)

## Companion Plans

- `documentation/planning/2026-08-02-consolidated-design-plan/02-domain-model.md` §2 (`media_items`, `media_sources.config`/`sync_checkpoint` contract, `category_post_case_mix`), §7 (tenant tables, RLS), `03-decision-record.md` (D23; the 2026-09-05 and 2026-09-06 rulings), `06-product-lifecycles.md` §3 (media selection), `07-security-model.md` (§14/§15 as the block-and-manifest recipe to follow).
- #1251 (the build this supersedes in part), #1252 (notice timer), #1253 (folder counts in the picker), #1248 (shared drives in the picker).
- `documentation/planning/investigations/2026-09-06-empty-library-after-first-sync/00_INVESTIGATION.md`.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| A full tree walk lists every folder every sync (6 h cadence); a large Drive tree costs a Drive request per folder | Quota pressure, slow first walks | `FOLDER_TREE_CAP` (2,000 folders per source, the cut said once); the tree enumeration is one breadth-first pass per walk; media pages are unchanged |
| The cursor grows with the tree (queue entries in `sync_checkpoint` JSONB) | Large row writes per chunk | Queue entries carry only `id`, `name`, `top`, `path`; 2,000 entries ≈ 250 KB, within JSONB comfort; measured in the gate with a 2,000-folder scripted tree |
| Between phase 2's deploy and the first save from the new card, no id-keyed weights exist | Slots draw from the whole pool, proportional to media (every category on auto) | Phase 3 ships in the same train; the owner enters 70/30 once on the new card; stated in the PR and the CHANGELOG |
| Same content hash in two folders shares one row (`uq_media_dedup`) | One of the two folders "loses" the file for weighting | Unchanged from #1251; the same-file guard on the update stands; documented |
| Two walks of the same source overlapping (a demand sync during a baseline) | Registry stamps from two `walk_seq` generations | Sync jobs serialize on `src:<id>` today (`jobs.serialization_key`); `walk_seq` increments only on a completed walk |
| Gate fixtures seed `media_items` without a registry row | Existing gates break on the FK | `category_id` is nullable; existing seeds keep working; new gates seed rows |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 01 Registry, migration 070, full-depth walk, lifecycle | L | — | — |
| 02 Weights by id, auto weights, API v2, discovery notice | M | 01 | — |
| 03 Web card on the registry | M | 02 | — |

Critical path: 01 → 02 → 03. Each phase is one PR, reviewed with the two-lens round the repo uses, tests first at unit, gate and web tiers.
