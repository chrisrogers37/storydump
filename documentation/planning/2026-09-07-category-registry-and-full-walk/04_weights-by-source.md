---
title: Phase 2 (revised) — weights by connected folder
type: plan
status: completed
owner: chris
created: 2026-09-08
tags: [plan, scheduler, category-mix, api, web, phase-2]
---

# Phase 2 (revised) — weights by connected folder

> **Status:** COMPLETE — built 2026-09-08 as PR #1262 (`/build`; challenge round folded the same day: a typed 0 is Off, stats tiles unchanged, nested picks refused in the same PR). Step 7 (removing the v1 keys after the Vercel deploy) is the one follow-up.

## Summary

The connected folder (`media_sources` row) is the group. This phase keys the weighted draw and the mix service on `source_id` instead of the folder name, replaces the name-keyed Settings card from #1251 with one that lists connected folders, and keeps every 2026-09-06 draw property (weighted over groups with eligible media, least-recently-posted rotation, locks, whole-pool fallback). Unweighted sources post in proportion to their media, together capped at the smallest explicit weight on the final split. One PR after the walk (`01_registry-and-full-walk.md` §1a), plus a one-line follow-up that drops the transitional API keys once the web is live.

## Evidence

- `scripts/migrations/055_intent_ledger_tables.sql:79-101` — `category_post_case_mix (workspace_id, category TEXT NOT NULL, ratio NUMERIC(5,4), effective_from, effective_to)`; `:98` `uq_case_mix_current UNIQUE (workspace_id, category) WHERE effective_to IS NULL`. D23 keeps it row-shaped and SCD2.
- `scripts/migrations/054_accounts_sources_media_tables.sql` — `media_items.source_id` (composite FK to `media_sources`), `media_sources.config` (`folder_ref`, `folder_name`, `removed?`), `state`.
- `src/services/target/scheduler.py:387` mix read by name, `:419` weighted draw, `:426-431` whole-pool fallback, `:382` rotation order; per-account eligibility `:368-381`.
- `src/services/target/category_mix.py` — `normalize` (`:53`), `set_mix` (`:94`, advisory lock + supersede), `current_mix` (`:132`), `discovered_categories` (`:143`, deleted here); `src/api/routes/v1.py:627,642` the routes; `src/api/app.py` the `MixInvalid` handler (`reason = invalid_mix_<why>`).
- `src/services/target/workspaces.py:317-331` `list_sources` — the source rows the Integrations tab renders (id, provider, state, sync stamps, folder_ref, folder_name) — **no media count and no `removed` flag**, so the mix view runs its own query; `provisioning.py:446-456` (update) and `:461-462` (insert) write `config.folder_name`; `media_sync.py:145,156` write `config.removed`.
- Web: `landing/src/components/dashboard/settings/category-weights-card.tsx` (replaced), `landing/src/lib/category-mix.ts` (`percentRows`, `toMix` drops 0 rows at `:64`, `mixRefusalCopy` `:102`), `landing/src/app/api/workspaces/[id]/category-mix/route.ts` (`:9,:26` emit `invalid_mix:not_a_list` with a colon — fix to the underscore form), `integrations-tab.tsx` (`enterFolder:222`, `pickFolder:240`, Remove, Sync now), `landing/src/lib/drive.ts:196` `addDriveFolder` / `:222` `addFolderRefusalCopy` (no `source_nested` case yet), `settings/page.tsx:115-120,222,262` (fetches sources and the mix; renders the card and the tab), `landing/src/lib/dashboard-payloads.ts:122-135` `SourceRow`, the marketing guide `landing/src/app/(marketing)/setup/media-organize/page.tsx:29-52` (the whole "subfolder = category" tree, not only the prose). The setup wizard and the legacy `settings/category-mix-card.tsx` were deleted in #1258.
- Pins for the small migration: `tests/scripts/test_advertised_ddl.py:281` (28 → 29), `tests/scripts/test_lineage_lane.py:347-355` (+071, appended), `src/models/target/intent_ledger.py:56,74-80` (the model and its by-name index); the tenancy lane handles `ADD COLUMN` and accepts `DROP INDEX`/`CREATE UNIQUE INDEX`, refuses `ADD FOREIGN KEY` (`test_tenancy_gate.py:576-578`). `07` §16 is the last heading, so 071 is `### §17.`; the manifest's last ordinal for doc 07 is 14.
- Nested picks: `POST /workspaces/{ws}/sources` is `src/api/routes/v1.py:564-605` (no nesting check); the Drive adapter's `list_folders` (`google_drive_adapter.py:664-690`) asks `files(id,name)` only and **nothing in the target tier reads a folder's `parents`** — step 6 adds that read.
- `workspaces.stats:485-495` groups media and posted tiles by the name label — unchanged by this phase (challenge round, 2026-09-08).
- `tests/src/services/target/test_category_mix.py:174` still tests `discovered_categories` (deleted in step 3); `tests/src/services/target/test_work_loop.py:750+` fakes the session positionally `[mix, counts, pick, intent]` and pins `m.category = :category` in the pick SQL.

## Implementation Plan

### Dependencies
`01_registry-and-full-walk.md` §1a merged (the walk, #1256), which added `media_items.folder_path TEXT NULL` as migration 070 (the current path under the connected folder, refreshed each walk).

### Blocks
The transitional-keys follow-up (step 6). #1220 step 3 follows this epic by S1.

### Steps

1. **Migration 071** (`scripts/migrations/071_case_mix_by_source.sql`, identical block as `07-security-model.md` `### §17.`, manifest ordinal 15, count pin 29, lineage list; 070 is the walk's `folder_path` column, #1256): `ALTER TABLE category_post_case_mix ADD COLUMN source_id UUID NULL;` `DROP INDEX uq_case_mix_current;` `CREATE UNIQUE INDEX uq_case_mix_current_by_source ON category_post_case_mix (workspace_id, source_id) WHERE effective_to IS NULL AND source_id IS NOT NULL;` — no FK (the lane refuses `ADD FOREIGN KEY`; sources are removed by flag, never deleted). Postconditions: the column exists; the old index is gone; the new one is present. Model: `source_id` and the new index on `CategoryPostCaseMix`; the by-name index removed. `category` stays NOT NULL and carries the source's `folder_name` as a label.
2. **Planner** (`scheduler.execute_plan_slot`): rows = the workspace's connected sources (`state = 'active'`, not `config.removed`) left-joined to current mix rows by `source_id`; eligible counts grouped by `m.source_id` (the existing eligibility); explicit rows (ratio > 0), **Off** rows (ratio = 0: never drawn, excluded from the automatic maths and from `r_min`) and automatic rows (no mix row). Over the rows with eligible media: `share_auto` = automatic rows' share of eligible media; `r_min` = the smallest ratio among the remaining explicit rows, renormalised; `A = min(share_auto, r_min / (1 + r_min))`; automatic rows split `A` by media share; explicit rows split `1 − A` by ratio; no explicit rows → `A = 1` (proportional). Pick `WHERE m.source_id = :source_id`; keep the rotation order and the locks; `rng` injectable; fallback to the whole pool oldest-first when the weighted set is empty. Rows with `source_id IS NULL` (any name-keyed row set before this lands) are ignored and superseded by the first save.
3. **Service** (`category_mix.py`): `normalize([{source_id, ratio}])` — ratios ≥ 0 (0 = Off), sum-to-one over the ratios > 0, no duplicates, at least one ratio > 0 when any row is given; `set_mix` validates every id is a connected source of the workspace (`invalid_mix_unknown_source`), writes `source_id` and the source's `folder_name` into `category`, keeps the advisory lock and the supersede; `current_mix` by source; `mix_view(executor, *, workspace_id)` → `[{source_id, provider, name, state, media_count, ratio | null, effective}]` (its own query over `media_sources` + `media_items`, since `list_sources` carries neither the count nor the removed flag) computed by the same function the planner uses, over workspace-wide eligibility (`state = 'available'`; the card says "approximate"); an Off row has `ratio: 0` and `effective: 0`. `discovered_categories` deleted; the docstring rewritten.
4. **Routes**: `GET /workspaces/{ws}/category-mix` → `{"rows": mix_view, "explicit_total"}` **plus** the v1 keys `mix`/`categories` in their current object shape for one release; `PUT {"rows": [{source_id, ratio}]}` (the v1 by-name body refused `invalid_mix_by_name`); the PUT response is the GET shape. Admin floor unchanged.
5. **Web**: the card becomes "Posting mix" on Settings › General: one row per connected folder — provider, `folder_name`, `N files`, "Your weight" (a percentage input or "Automatic") and "Posts about" (the API's effective %, sums to 100); footer total over explicit rows; "Split evenly" over all rows; "Automatic for all" clears the mix; copy: "Each connected folder is a group. Give folders a share of posts, or leave them automatic and they post in proportion to their files. Subfolders are just structure — to weight two subfolders, connect them as folders." Empty state: "Connect a Google Drive folder under Integrations." "Your weight" offers a percentage, Automatic, or **Off** (a typed 0 is Off: the folder stays connected and synced, never posts, and is excluded from the automatic maths; the row shows "Off" with a way back). `toMixById` keeps a 0 as Off, never drops it. BFF refusal reasons use the underscore form. The marketing guide's "subfolder = category" tree becomes: connect the folders you want as groups; any depth is walked; renames are safe.
6. **Connected folders are disjoint.** A folder inside a connected folder is already synced by its parent, so a nested pick would make two sources upsert the same rows and freeze attribution to whichever listed first (review of #1256). The adapter gains `folder_ancestors(*, workspace_id, folder_ref) -> list[str]` — the chain of parent ids up to the Drive root via `files/{id}?fields=parents` under the grant, bounded at 32 levels, a shared-drive root or a folder with no parent ending the chain. `POST /workspaces/{ws}/sources` refuses (`source_nested`, the `detail` naming the folder) when the candidate's chain contains an active source's `folder_ref`, or when any active source's chain contains the candidate (the ancestor case — one chain per existing source, N small). Re-picking a removed folder is allowed as today. The picker greys out folders that ARE already sources (exact id match, no Drive read) with "already connected"; a nested pick surfaces the API's refusal copy (`addFolderRefusalCopy` gains `source_nested`).
7. **Follow-up PR** (after the Vercel deploy is live): remove the v1 keys and the by-name refusal branch; `test_v1_routes.py` updated.
8. **Docs**: `06` §3 media selection (weights by connected folder; the automatic rule; the fallback), `02` §2 (`category_post_case_mix.source_id`; `media_items.category` is a label; `folder_path`), `.claude/rules/scheduler.md:12-15`, `category_mix.py` and `category-mix.ts` docstrings, CHANGELOG, README Live status; `03` ruling marked built.

## Test Plan

- Unit (`tests/src/services/target/test_work_loop.py`): the draw over sources; the arithmetic (70/30 + an automatic 10-file source; a 50,000-file automatic source capped at 23 %; one explicit source at 100 + a large automatic one capped at 50 %; an explicit source with no eligible media dropped before the cap; no explicit rows → proportional); a removed source never drawn; the whole-pool fallback; locks and rotation preserved; name-keyed rows ignored.
- Unit (`test_category_mix.py`): validation by source id (`unknown_source`, duplicates, sum-to-one over the ratios > 0, 0 kept as Off, all-zero refused), `mix_view.effective` equals the planner's weights (one function, both called), v1 keys derived from the view; the `discovered_categories` test removed with the function.
- Unit (`test_google_drive_adapter.py`): `folder_ancestors` walks `parents` to the root, stops at a shared-drive root or a missing parent, is bounded, refuses a non-id ref; (`tests/src/api/test_v1_routes.py`): a pick inside an active source and a pick that contains one are refused `source_nested` naming the folder; a removed source does not block.
- Unit (`tests/src/api/test_v1_routes.py`): GET carries `rows` and the v1 keys; PUT by source at the admin floor; the by-name body refused; the PUT response is the GET shape.
- Gate (`tests/scripts/test_scheduler_clock_gate.py`): weights by source on the real table (the by-name seed at `:976-985` moves to sources); the automatic share; a removed source excluded. Gates: advertised DDL (29), lineage, replay, adopt; tenancy unchanged (no new table).
- Web (`landing/src/lib/category-mix.test.ts` + a component test): rows, automatic vs explicit vs Off, sum over explicit rows, 0 kept as Off, request and response shapes; `drive.ts` refusal copy for `source_nested`; the picker greys an already-connected folder; `vitest`, `tsc`, `eslint`.

## Verification Checklist

- The unit, API and gate suites above are green; `pytest tests/scripts/test_advertised_ddl.py tests/scripts/test_lineage_lane.py tests/scripts/test_advertised_ddl_replay.py tests/scripts/test_migration_runner_adopt.py` green on postgres:15.
- Production: the owner removes `storydump-media`, connects `memes` and `merch` as two folders, sets 70 / 30; `GET /workspaces/<ws>/category-mix` returns two rows with `effective` 70 / 30; the next slots draw by source (read-only: `post_intents` joined to `media_items.source_id`).
- Renaming `memes` in Drive and syncing changes the row's label and nothing else; adding a subfolder under `memes` adds files to that row's count and nothing else.

## What NOT To Do

- Do not key anything on a folder name or path; the source id is the key, the name is a label.
- Do not add a registry, a lifecycle, a debut or a notice; connecting and removing a folder are the lifecycle, and the person doing it is in the UI.
- Do not make automatic sources silent or dominant (the cap); Off is the person's explicit 0, never the rule's.
- Do not compute the effective percentage in the web; one Python function feeds both the draw and the API.
- Do not remove the v1 keys in the same deploy as the web change (Railway and Vercel deploy separately).

## Context

area: scheduler · category-mix service · API · web · docs — effort: M — risk: low-medium (draw key changes; deploy-window compatibility) — priority: high
