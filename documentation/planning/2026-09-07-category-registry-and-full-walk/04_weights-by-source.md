---
title: Phase 2 (revised) — weights by connected folder
type: plan
status: draft
owner: chris
created: 2026-09-08
tags: [plan, scheduler, category-mix, api, web, phase-2]
---

# Phase 2 (revised) — weights by connected folder

## Summary

The connected folder (`media_sources` row) is the group. This phase keys the weighted draw and the mix service on `source_id` instead of the folder name, replaces the name-keyed Settings card from #1251 with one that lists connected folders, and keeps every 2026-09-06 draw property (weighted over groups with eligible media, least-recently-posted rotation, locks, whole-pool fallback). Unweighted sources post in proportion to their media, together capped at the smallest explicit weight on the final split. One PR after the walk (`01_registry-and-full-walk.md` §1a), plus a one-line follow-up that drops the transitional API keys once the web is live.

## Evidence

- `scripts/migrations/055_intent_ledger_tables.sql:79-101` — `category_post_case_mix (workspace_id, category TEXT NOT NULL, ratio NUMERIC(5,4), effective_from, effective_to)`; `:98` `uq_case_mix_current UNIQUE (workspace_id, category) WHERE effective_to IS NULL`. D23 keeps it row-shaped and SCD2.
- `scripts/migrations/054_accounts_sources_media_tables.sql` — `media_items.source_id` (composite FK to `media_sources`), `media_sources.config` (`folder_ref`, `folder_name`, `removed?`), `state`.
- `src/services/target/scheduler.py:387` mix read by name, `:419` weighted draw, `:430` whole-pool fallback, `:382` rotation order; per-account eligibility `:369-380`.
- `src/services/target/category_mix.py` — `normalize` (`:53`), `set_mix` (`:94`, advisory lock + supersede), `current_mix` (`:132`), `discovered_categories` (`:143`, deleted here); `src/api/routes/v1.py:627,642` the routes; `src/api/app.py` the `MixInvalid` handler (`reason = invalid_mix_<why>`).
- `src/services/target/workspaces.py::list_sources` — the source rows the Integrations tab renders (folder name, state, last sync); `provisioning.py:445-454` writes `config.folder_name`.
- Web: `landing/src/components/dashboard/settings/category-weights-card.tsx` (replaced), `landing/src/lib/category-mix.ts` (`percentRows`, `toMix` drops 0 rows at `:64`, `mixRefusalCopy`), `landing/src/app/api/workspaces/[id]/category-mix/route.ts` (`:26` emits `invalid_mix:not_a_list` with a colon — fix to the underscore form), `integrations-tab.tsx` (source list, Remove, Sync now), `setup-wizard.tsx:28` (legacy `settings/category-mix-card.tsx`), the marketing guide `landing/src/app/(marketing)/setup/media-organize/page.tsx:32-50`.
- Pins for the small migration: `tests/scripts/test_advertised_ddl.py:279` (28 → 29), `tests/scripts/test_lineage_lane.py` (+071), `src/models/target/intent_ledger.py:56,74-80` (the model and its by-name index); the tenancy lane handles `ADD COLUMN` and accepts `DROP INDEX`/`CREATE UNIQUE INDEX`.

## Implementation Plan

### Dependencies
`01_registry-and-full-walk.md` §1a merged (the walk, #1256), which added `media_items.folder_path TEXT NULL` as migration 070 (the current path under the connected folder, refreshed each walk).

### Blocks
The transitional-keys follow-up (step 6). #1220 step 3 follows this epic by S1.

### Steps

1. **Migration 071** (`scripts/migrations/071_case_mix_by_source.sql`, identical block as `07-security-model.md` `### §17.`, manifest ordinal 15, count pin 29, lineage list; 070 is the walk's `folder_path` column, #1256): `ALTER TABLE category_post_case_mix ADD COLUMN source_id UUID NULL;` `DROP INDEX uq_case_mix_current;` `CREATE UNIQUE INDEX uq_case_mix_current_by_source ON category_post_case_mix (workspace_id, source_id) WHERE effective_to IS NULL AND source_id IS NOT NULL;` — no FK (the lane refuses `ADD FOREIGN KEY`; sources are removed by flag, never deleted). Postconditions: the column exists; the old index is gone; the new one is present. Model: `source_id` and the new index on `CategoryPostCaseMix`; the by-name index removed. `category` stays NOT NULL and carries the source's `folder_name` as a label.
2. **Planner** (`scheduler.execute_plan_slot`): rows = the workspace's connected sources (`state = 'active'`, not `config.removed`) left-joined to current mix rows by `source_id`; eligible counts grouped by `m.source_id` (the existing eligibility); explicit rows (ratio > 0) and automatic rows (no mix row). Over the rows with eligible media: `share_auto` = automatic rows' share of eligible media; `r_min` = the smallest ratio among the remaining explicit rows, renormalised; `A = min(share_auto, r_min / (1 + r_min))`; automatic rows split `A` by media share; explicit rows split `1 − A` by ratio; no explicit rows → `A = 1` (proportional). Pick `WHERE m.source_id = :source_id`; keep the rotation order and the locks; `rng` injectable; fallback to the whole pool oldest-first when the weighted set is empty. Rows with `source_id IS NULL` (any name-keyed row set before this lands) are ignored and superseded by the first save.
3. **Service** (`category_mix.py`): `normalize([{source_id, ratio}])` — ratios > 0, sum-to-one, no duplicates; `set_mix` validates every id is a connected source of the workspace (`invalid_mix_unknown_source`), writes `source_id` and the source's `folder_name` into `category`, keeps the advisory lock and the supersede; `current_mix` by source; `mix_view(executor, *, workspace_id)` → `[{source_id, provider, name, state, media_count, ratio | null, effective}]` computed by the same function the planner uses, over workspace-wide eligibility (`state = 'available'`; the card says "approximate"). `discovered_categories` deleted; the docstring rewritten.
4. **Routes**: `GET /workspaces/{ws}/category-mix` → `{"rows": mix_view, "explicit_total"}` **plus** the v1 keys `mix`/`categories` in their current object shape for one release; `PUT {"rows": [{source_id, ratio}]}` (the v1 by-name body refused `invalid_mix_by_name`); the PUT response is the GET shape. Admin floor unchanged.
5. **Web**: the card becomes "Posting mix" on Settings › General: one row per connected folder — provider, `folder_name`, `N files`, "Your weight" (a percentage input or "Automatic") and "Posts about" (the API's effective %, sums to 100); footer total over explicit rows; "Split evenly" over all rows; "Automatic for all" clears the mix; copy: "Each connected folder is a group. Give folders a share of posts, or leave them automatic and they post in proportion to their files. Subfolders are just structure — to weight two subfolders, connect them as folders." Empty state: "Connect a Google Drive folder under Integrations." `toMixById` never drops a typed 0 silently: 0 is refused with copy "Remove the folder under Integrations to stop posting from it." BFF refusal reasons use the underscore form. The setup wizard's legacy card is replaced by a link to Settings › General; `category-mix-card.tsx` deleted. The marketing guide says: connect the folders you want as groups; any depth is walked; renames are safe.
6. **Follow-up PR** (after the Vercel deploy is live): remove the v1 keys and the by-name refusal branch; `test_v1_routes.py` updated.
7. **Docs**: `06` §3 media selection (weights by connected folder; the automatic rule; the fallback), `02` §2 (`category_post_case_mix.source_id`; `media_items.category` is a label; `folder_path`), `.claude/rules/scheduler.md:12-15`, `category_mix.py` and `category-mix.ts` docstrings, CHANGELOG, README Live status; `03` ruling marked built.

## Test Plan

- Unit (`tests/src/services/target/test_work_loop.py`): the draw over sources; the arithmetic (70/30 + an automatic 10-file source; a 50,000-file automatic source capped at 23 %; one explicit source at 100 + a large automatic one capped at 50 %; an explicit source with no eligible media dropped before the cap; no explicit rows → proportional); a removed source never drawn; the whole-pool fallback; locks and rotation preserved; name-keyed rows ignored.
- Unit (`test_category_mix.py`): validation by source id (`unknown_source`, duplicates, sum-to-one, 0 refused), `mix_view.effective` equals the planner's weights (one function, both called), v1 keys derived from the view.
- Unit (`tests/src/api/test_v1_routes.py`): GET carries `rows` and the v1 keys; PUT by source at the admin floor; the by-name body refused; the PUT response is the GET shape.
- Gate (`tests/scripts/test_scheduler_clock_gate.py`): weights by source on the real table (the by-name seed at `:976-985` moves to sources); the automatic share; a removed source excluded. Gates: advertised DDL (28), lineage, replay, adopt; tenancy unchanged (no new table).
- Web (`landing/src/lib/category-mix.test.ts` + a component test): rows, automatic vs explicit, sum over explicit rows, 0 refused, request and response shapes; `vitest`, `tsc`, `eslint`.

## Verification Checklist

- The unit, API and gate suites above are green; `pytest tests/scripts/test_advertised_ddl.py tests/scripts/test_lineage_lane.py tests/scripts/test_advertised_ddl_replay.py tests/scripts/test_migration_runner_adopt.py` green on postgres:15.
- Production: the owner removes `storydump-media`, connects `memes` and `merch` as two folders, sets 70 / 30; `GET /workspaces/<ws>/category-mix` returns two rows with `effective` 70 / 30; the next slots draw by source (read-only: `post_intents` joined to `media_items.source_id`).
- Renaming `memes` in Drive and syncing changes the row's label and nothing else; adding a subfolder under `memes` adds files to that row's count and nothing else.

## What NOT To Do

- Do not key anything on a folder name or path; the source id is the key, the name is a label.
- Do not add a registry, a lifecycle, a debut or a notice; connecting and removing a folder are the lifecycle, and the person doing it is in the UI.
- Do not make automatic sources silent (weight 0) or dominant (the cap).
- Do not compute the effective percentage in the web; one Python function feeds both the draw and the API.
- Do not remove the v1 keys in the same deploy as the web change (Railway and Vercel deploy separately).

## Context

area: scheduler · category-mix service · API · web · docs — effort: M — risk: low-medium (draw key changes; deploy-window compatibility) — priority: high
