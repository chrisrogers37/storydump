---
title: Category registry keyed by folder id, and a full-depth Drive walk (epic)
type: plan
status: completed
owner: chris
created: 2026-09-07
tags: [plan, drive, media-sync, categories, scheduler, web, epic]
links:
  - https://github.com/chrisrogers37/storydump/pull/1254
---

> **ARCHIVED 2026-09-08 — built.** Phase 1a landed as #1256 (the lazy full-depth walk, migration 070); phases 1b, 2 and 3 were superseded by the 2026-09-08 ruling *sources are the groups* and replaced by `04_weights-by-source.md`, built as #1262 (migration 071). One follow-up remains: removing the v1 category-mix keys after the Vercel deploy (#1263). Kept as the record of the hardened registry design a customer with an unsplittable folder could still call for.

> **Ruling 2026-09-08 — sources are the groups** (`03-decision-record.md`, post-ratification rulings). After an adversarial weigh of this plan the owner ruled that the **connected folder** (`media_sources` row) is the unit that carries a weight, not a subfolder. **Phase 1a (the lazy full-depth walk) stands and is built first.** Phases 1b, 2 and 3 — the registry keyed by folder id, weights per folder, the automatic rule with a debut, and the per-folder card — are **superseded** by `04_weights-by-source.md`. Forks F1–F4, F8 and F9 are superseded by the ruling; F5, F6, F7 and S1 stand. The superseded text is kept as the record of a hardened design that a customer with an unsplittable folder could still call for.

# Category registry keyed by folder id, and a full-depth Drive walk

## Summary

#1251 (merged 2026-09-06) made a picked Drive folder's immediate subfolders into categories, keyed by the folder's **name**, and weighted how often each posts. Two properties of that build will glitch across a wide user base: a renamed folder strands its weight under the old name, and folders nested deeper than one level are never walked, so their media silently never syncs. This epic replaces name-keyed categories with a per-workspace **registry keyed by Drive folder id** (names refreshed on every sync; a lifecycle of active/gone), walks the **whole tree** under a picked folder lazily (the category is the top-level subfolder; files directly in the picked folder are an **Unsorted** category that can be weighted), gives folders discovered after the mix was set an **automatic** weight so they post without anyone touching Settings, and rebuilds the Settings card on the registry. The weighted draw from #1251 (weighted over categories with eligible media, least-recently-posted rotation, locks, whole-pool fallback) is kept.

Mission alignment (`PROJECT_MISSION.md`): *zero-friction content automation* and *simple over powerful* — a folder rename or a new subfolder must never require a settings change or produce silent non-posting; the registry makes folder structure a fact the product tracks rather than a string a person maintains.

**Relation to #1220.** The target tier has not yet published a post (#1220 step 3 is the next program build). The owner ruled (S1, 2026-09-07) that all four phases of this epic land first and #1220 step 3 follows; phase 1a (the walk) still lands first within the epic because it removes the one failure observed in production.

## Evidence (current state, verified 2026-09-07 on `main` at 40b8b7f)

- `scripts/migrations/054_accounts_sources_media_tables.sql:278` — `media_items.category TEXT NULL`; `:295` `uq_media_dedup UNIQUE (workspace_id, content_hash)`; `:156` `media_sources` has no per-workspace uniqueness (two picked roots are legal).
- `scripts/migrations/055_intent_ledger_tables.sql:79-101` — `category_post_case_mix` keyed by `(workspace_id, category TEXT)` with `uq_case_mix_current` partial on `effective_to IS NULL` (`:98`); D23 (`03-decision-record.md`) keeps it row-shaped, sum-to-one service-enforced.
- `src/services/target/google_drive_adapter.py:231` `list_changes` — one-level walk with cursor `current`/`queue`/`page_token` (`:247-250` entries carry only `id`, `name`; `:273` the bare `page_token` branch; `:325-336` the folder restart); `:364` `_subfolders` (name order, `FOLDER_LIST_CAP = 500` at `:104`); `src/services/target/drive_adapter.py:136` `checkpoint_incomplete`.
- `src/services/target/media_sync.py:311` the stored cursor is resumed as-is; `:338` persistent failure writes `{"v":1}`; `:393-414` the upsert writes `category` guarded by `WHERE media_items.category IS DISTINCT FROM EXCLUDED.category` (`:403`); `:427` checkpoint write; `:438` chunk chain; `:160` re-arm nulls the cursor; `:350-370` the sync's own binding notice.
- `src/services/target/scheduler.py:306` `execute_plan_slot` — `:387` reads `category, ratio FROM category_post_case_mix … effective_to IS NULL`; `:419` weighted draw; `:382` order `last_posted_at NULLS FIRST, created_at`; `:430` whole-pool fallback; `:213` `_notice_no_media`.
- `src/services/target/category_mix.py` — `normalize` (`:53`), `set_mix` (`:94`, advisory lock, supersede, inserts), `current_mix` (`:132`), `discovered_categories` (`:143`).
- `src/api/routes/v1.py:627,642` — `GET`/`PUT /workspaces/{ws}/category-mix` by name; `src/api/app.py:232-234` the unmapped-exception 500.
- `landing/src/lib/category-mix.ts:24,53,64` `percentRows`/`toMix` (drops 0 rows); `landing/src/components/dashboard/settings/category-weights-card.tsx:22,33` `CategoryWeightsCard` reads `data.mix`; `landing/src/app/(dashboard)/dashboard/settings/page.tsx:120,221-225` server-renders it from the GET; `landing/src/components/dashboard/setup-wizard.tsx:28` still imports the legacy `category-mix-card.tsx`.
- `src/services/target/workspaces.py:488` `stats.media_by_category` groups by the name; `provisioning.py:445-454` writes `config.folder_name` for a picked root.
- `scripts/tenancy_gate.py:81-101` `_TENANCY_IRRELEVANT` (no `UPDATE`); `:274` `ADD COLUMN` handled; `:309-323` constraints: only `DROP CONSTRAINT` and `ADD CONSTRAINT … CHECK` pass, FOREIGN KEY is "a review event"; `DROP INDEX` accepted since #1246.
- Pins that move with a schema change: `tests/scripts/test_advertised_ddl.py:279` (27 normative blocks) and `:289` (`CREATE POLICY p_tenant ON` count `13 + 1`), `tests/scripts/test_tenancy_gate.py:377` (26 tenant tables) and `:378` (19 tenant-keyed), `tests/scripts/test_rls_runtime_harness.py:150` (policy census) and `:704` (`matrix == 16`), `tests/scripts/test_lineage_lane.py` ratified list, `src/models/target/accounts_sources_media.py` and `src/models/target/intent_ledger.py:56,74-80` (the model and its by-name index).
- Legacy reference only: `src/services/media_sources/google_drive_provider.py:162-182` (one-level walk, subfolder name = category).

## Architecture

**Walk (phase 1a, no schema change).** `list_changes` walks the whole tree under the picked root **lazily**: the first call lists the root's immediate subfolders (as today, `FOLDER_LIST_CAP`) and queues them; when a folder is popped it lists its own subfolders once and appends them with the inherited top-level ancestor; then it pages that folder's media. The cursor becomes `{"v":2,"walk":<token>,"seen":N,"truncated":bool,"current":{id,name,top,top_name,path,listed},"queue":[…],"page_token"?}`. The **sync mints the token** (1a: an opaque uuid; 1b: `walk_seq + 1`, claimed atomically) and passes `{"v":2,"walk":T}` as the start cursor whenever the stored cursor is absent or complete; the adapter carries `walk` through unchanged. A stored cursor without `walk` (the #1251 shape) is ignored and the walk starts over; a chained chunk whose `walk` differs from the stored cursor's starts over rather than resuming a foreign page; `listed` marks a folder whose subfolders are already queued so the expired-token restart never re-lists it. Items carry `category` (= `top_name`, unchanged semantics: NULL for the root's own files until 1b), `category_ref` (the top-level folder id, or the root's id) and `category_path`. `FOLDER_WALK_CAP = 2000` folders seen per walk; beyond it the rest is skipped and said once (log now; the bindings message once 1b exists). The vanished-folder skip and expired-token restart from #1251 stand.

**Registry (phase 1b, migration 070).** `media_categories` — one row per (workspace, source, Drive folder id) for the picked root's top-level subfolders plus one row for the root itself (`folder_ref` = the picked folder's id, `is_root = true`, `name` = "Unsorted"). Columns: `id`, `workspace_id`, `source_id`, `folder_ref`, `name` (current Drive name, refreshed each walk), `is_root`, `state` (`active` | `gone`), `first_seen_at`, `last_seen_at`, `last_seen_walk`, `announced_at`, `debuted_at`, timestamps. Tenant-keyed, RLS `p_tenant` for `svc_ingress`/`svc_worker`, grants SELECT/INSERT/UPDATE (never DELETE — gone is a state; rows leave only by the workspace/source cascades). The registry is fed from the **folder listing** (root + top-level folders on the walk's first call), never from items, so empty folders have rows.

**Media.** `media_items.category_id UUID NULL` (indexed; no FK — see F8) beside the existing `category` text kept as the denormalized display name (stats and the library keep working unchanged). The sync writes both, and the upsert guard fires when either differs.

**Lifecycle.** `media_sources.walk_seq` is the last token **minted**: at each walk's start the sync claims `UPDATE media_sources SET walk_seq = walk_seq + 1 … RETURNING walk_seq` (with `workspace_id` in the predicate) and the cursor carries it (`reg: true` marks a 1b cursor; 1b starts over on any cursor without it). Each walk stamps the registry rows it lists with `last_seen_walk = walk`; every start mints a strictly larger token, so an aborted walk's stamps are always below the next walk's. A walk that completes sweeps rows of the source with `last_seen_walk < walk` to `gone` — unless its root listing was cut at `FOLDER_LIST_CAP`, in which case it completes without sweeping and says so. A chunk carrying a token other than the stored cursor's starts over, so no walk completes on a stale token. A gone folder seen again becomes `active` with its name refreshed. Renames change `name` only.

**Weights (phase 2).** `category_post_case_mix.category_id UUID NULL` (indexed, partial-unique among current rows; no FK) becomes the key; `category` text stays as a label; `uq_case_mix_current` (the name key) is dropped. The planner joins current mix rows to active registry rows by id and counts eligible media by `category_id`. An explicit ratio of 0 means **Off** (never drawn, excluded from automatic maths). **Automatic weights (F4, locked):** over the rows that have eligible media (automatic and explicit; Off, gone and id-less media excluded), automatic rows together take `A = min(share_auto, r_min / (1 + r_min))` of the draws, where `share_auto` is their share of that eligible media and `r_min` the smallest explicit ratio among the rows that remain — the cap is the smallest explicit weight measured on the final split (70/30 → ≤ 23 %; a single row at 100 → ≤ 50 %); automatic rows split `A` by media share; explicit rows share `1 − A` by their ratios; with no explicit rows `A = 1`. A newly found folder with media takes the first eligible slot after discovery (`debuted_at`, claimed atomically), then follows the rule; the card shows the effective percentage. When the weighted set has no eligible media the slot falls back to the pool **minus media in Off rows**, oldest first (id-less and gone-row media stay in); all-Off is legal and posts nothing. Discovery is said once per walk at walk completion (F9).

**Web (phase 3).** The card lists registry rows with two columns: "Your weight" (an input, "Automatic", or "Off") and "Posts about" (the API's effective %, which sums to 100). Gone rows appear only while they hold a weight, with "Remove weight". Unsorted is hidden at 0 files and no weight. Renames are invisible.

## Decision Forks

All seven original forks were served one at a time and locked by the owner on 2026-09-07; the ironclad cycle-1 review (PR #1254 comment) reopened F4 on a defect in the ratified formula and added F8, F9 and S1; F4 and S1 were re-served and locked the same day.

**F1 — Registry shape.** Context: where does a category's identity live? Options: (a) a new tenant table `media_categories` with `media_items.category_id`; (b) reuse `media_items.category` as the folder id and add a names table; (c) a JSON registry inside `media_sources.config`. Lean: **(a)**. Ratifier: owner. Status: **locked** 2026-09-07.

**F2 — Category depth.** Context: a walk of any depth must map each file to one category. Options: (a) the top-level subfolder under the picked root, nested folders inherit; (b) the deepest folder; (c) a per-source `category_depth` setting. Lean: **(a)**; items keep `category_path` so (c) stays possible. Ratifier: owner. Status: **locked** 2026-09-07.

**F3 — Files directly in the picked folder.** Context: today they have no category and post only as a fallback. Options: (a) a real registry row "Unsorted" (`is_root`) that can be weighted like any other; (b) uncategorized (`NULL`). Lean: **(a)**; the card hides the row at 0 files and no weight. Ratifier: owner. Status: **locked** 2026-09-07.

**F4 — Folders discovered after the mix was set.** Context: the owner sets memes 70 / merch 30; a folder "events" appears later. The ratified intent (2026-09-07) was "a sensible rate, never takes over, never goes silent"; the ratified formula (media share × mean explicit weight) does not deliver it — explicit weights sum to one, so the mean is `1/n` and a 10-file folder draws at ≈0.1 % while a 50,000-file archive takes ≈31 %. Options: (a) **proportional pool, capped, with a debut**: automatic folders together take the smaller of their combined media share and the smallest explicit weight (measured on the final split, so one explicit row at 100 caps them at 50 %), split among themselves by media share; explicit rows share the rest by their ratios; a newly discovered folder with media takes the first eligible slot after discovery (`debuted_at`), then follows the rule; (b) a floor: each automatic folder draws at least half the smallest explicit weight; (c) every automatic folder is one more equal row. Lean: **(a)** — sayable ("new folders post in proportion to their files, together never more than your smallest weight, and each gets one post right away"), no takeover, no silence, and no dependence on how many categories exist. Ratifier: owner. Status: **locked** 2026-09-07 (re-locked by the owner after ironclad cycle 1: "Proportional, capped, with a debut").

**F5 — Transition of name-keyed weight rows.** Context: production held zero `category_post_case_mix` rows on 2026-09-07 (read-only check; media counts memes 3,477 / merch 1,077). Options: (a) no carry-over — ids only; a name-keyed row set before phase 2 is ignored by the planner and superseded by the first save from the new card; (b) adopt-by-name on the first walk; (c) a migration-time backfill (impossible). Lean: **(a)**. Ratifier: owner. Status: **locked** 2026-09-07 (owner: "New system with newly synced items should have the ID approach").

**F6 — `media_items.category` text.** Context: stats, the library and the case-mix history read a name today. Options: (a) keep as the denormalized display name refreshed by the sync, add `category_id`; (b) drop it and join everywhere. Lean: **(a)**. Ratifier: owner. Status: **locked** 2026-09-07.

**F7 — Phasing.** Context: the work spans a migration, the adapter, the planner, the API and the web. Options: (a) sequenced PRs, each green on its own; (b) one PR. Lean: **(a)** as four PRs: 1a walk, 1b registry, 2 weights, 3 card — the cycle-1 review showed the walk fix is separable and is the only failure observed in production. Ratifier: owner. Status: **locked** 2026-09-07 as sequenced PRs; the 1a/1b split (four PRs) is the refinement recorded in S1's lock and the README row.

**F8 — Foreign keys on the two altered tables.** Context: the tenancy lane refuses `ALTER TABLE … ADD CONSTRAINT … FOREIGN KEY` (`scripts/tenancy_gate.py:309-323`, a review event by design) and a two-column `ON DELETE SET NULL` would null `workspace_id`. Options: (a) no FK on `media_items.category_id` and `category_post_case_mix.category_id` — an index only; registry rows are never deleted except by the same cascades that delete the referencing rows; (b) extend the lane with a reviewed FOREIGN KEY branch and add the FKs with single-column `SET NULL (category_id)` (PG 15). Lean: **(a)** — smaller, and the integrity argument holds without the constraint. Ratifier: owner. Status: locked 2026-09-07 by the plan author under F1's approval, ratified by the owner at the merge of #1254; the owner may reopen.

**F9 — The discovery notice.** Context: the ratified auto rule already makes a new folder post; the notice exists so people know it did. Options: (a) one message per walk at walk completion — the first walk lists the folders found; later walks list new folders with counts and effective %, 0-file folders skipped, `announced_at` per row; (b) one message per registry row at creation (count 0 at that moment); (c) no notice. Lean: **(a)**. Ratifier: owner. Status: locked 2026-09-07 by the plan author, ratified by the owner at the merge of #1254; the owner may reopen.

**S1 — Sequencing against #1220 (owner's call, not a design fork).** Context: the target tier has never published a post; #1220 step 3 is the next program build and this epic is four PRs. Options: (a) phase 1a now; 1b–3 after the first real Instagram post (#1220 step 3); (b) all phases now, #1220 after. Lean: none — product priority. Ratifier: owner. Status: **locked** 2026-09-07 (owner: all four phases now, #1220 after).

## Implementation Plan

### Dependencies
None — builds on #1251 as merged (5b3437d).

### Blocks
Nothing technically; by S1 the Instagram publish leg (#1220 step 3) is sequenced after this epic. Phase 3 retires the setup wizard's legacy `landing/src/components/dashboard/settings/category-mix-card.tsx`.

### Steps
1. Phase 1a — `01_registry-and-full-walk.md` §1a: the lazy full-depth walk with the v2 cursor; no migration. One PR.
2. Phase 1b — `01_registry-and-full-walk.md` §1b: migration 070, the registry, the lifecycle, `category_id` on media. One PR.
3. Phase 2 — `02_weights-by-id-and-auto.md`: the draw and the mix service by id, the F4 rule, API v2 beside v1, the walk-completion notice. One PR.
4. Phase 3 — `03_web-card-on-the-registry.md`: the Settings card on the registry, the setup-wizard card retired, the docs close-out (`03` ruling, README Live status, CHANGELOG, the stale docstrings and rules). One PR.

## Test Plan

Per phase (see each doc). Tiers: unit (`tests/src/…`), gate on postgres:15 (`tests/scripts/…`), web (`vitest`, `tsc`, `eslint`). Tests are written first at each tier.

## Verification Checklist

- Every phase doc's checklist is green.
- `pytest tests/scripts/test_advertised_ddl.py tests/scripts/test_tenancy_gate.py tests/scripts/test_lineage_lane.py tests/scripts/test_rls_runtime_harness.py` green after phase 1b (pins 28 / 27 / 20 / 17 / `13 + 2`).
- Production after phase 1a: a file two levels deep in the production Drive (the owner places one) appears in the library under its top-level folder's name.
- Production after phase 3: renaming a Drive subfolder and syncing leaves the weight on the row; a new subfolder with files appears in the card as Automatic and posts at the first eligible slot after discovery.

## What NOT To Do

- Do not build phases 1b, 2 or 3 before the preceding one is merged; the ids they key on come from the walk and the registry.
- Do not touch the legacy provider in `src/services/media_sources/`; it is reference only.
- Do not fold the picker counts (#1253) or shared drives (#1248) into this epic; the lazy walk is the shape #1248 slots into later.
- Do not put an `UPDATE` or an `ALTER … ADD FOREIGN KEY` into migration 070; the tenancy lane refuses both (F8), and the first full walk arms itself at the next scheduled sync.

## Context

area: sync · scheduler · api · web · docs — effort: M + L + M + M — risk: medium — priority: high (the owner's stated concern about renames and nested folders)

## Companion Plans

- Phase docs: `01_registry-and-full-walk.md` (1a walk, 1b registry), `02_weights-by-id-and-auto.md`, `03_web-card-on-the-registry.md`.
- `documentation/planning/2026-08-02-consolidated-design-plan/02-domain-model.md` §2 (`media_items`, `media_sources.config`/`sync_checkpoint` contract, `category_post_case_mix`; `:654-659` states the one-level rule this epic supersedes), §7 (tenant tables, RLS), `03-decision-record.md` (D23; the 2026-09-05 and 2026-09-06 rulings, the latter superseded here), `06-product-lifecycles.md` §3 (media selection; `:45` one level), `07-security-model.md` (§14/§15 as the block-and-manifest recipe; §16 is this epic's block), `05-operational-numbers.md:36` (a lease covers one checkpointed step).
- `.claude/rules/scheduler.md:12-15` (stale `_pick_category_for_slot`), docstrings `src/services/target/category_mix.py:19-20` and `landing/src/lib/category-mix.ts:4-9`, the marketing guide `landing/src/app/(marketing)/setup/media-organize/page.tsx:32-50` — all restate the one-level rule and are updated in phase 3.
- #1251 (superseded in part), #1252 (notice timer), #1253 (picker counts), #1248 (shared drives), #1220 (publish leg); the ironclad cycle-1 review on PR #1254.
- `documentation/planning/investigations/2026-09-06-empty-library-after-first-sync/00_INVESTIGATION.md` (finding 1: nested media never synced).

## Risks

| Risk | Impact | Level | Mitigation |
|---|---|---|---|
| A full walk lists every folder every sync (6 h cadence): one Drive request per folder | Quota pressure, slow walks on large trees | medium | Lazy discovery: one listing per folder when it is popped, resumable by construction; `FOLDER_WALK_CAP = 2000` folders seen per walk, the cut said once to the bindings |
| Between phase 2's deploy and the first save from the new card, no id-keyed weights exist | Slots draw by the automatic rule (proportional), never from nothing | low | Phase 3 in the same train; the owner enters 70/30 once on the new card; stated in the PR and the CHANGELOG |
| Phase 2 and phase 3 deploy separately (Railway vs Vercel) | The live card would break on a changed GET shape | high if ignored | Phase 2 keeps the v1 keys (`mix`, `categories`) and accepts both PUT bodies; phase 3 removes them |
| A #1251-shape cursor is stored at deploy | A resumed walk reads fields that do not exist | high if ignored | A cursor without `walk` is ignored and the walk starts over; no `UPDATE` in 070 |
| A re-pick mid-walk while a chained chunk is queued | A "completed" empty walk sweeps every category to gone | high if ignored | The chunk's `walk` token must match the stored cursor's, and the sweep runs only when the completing token is the one the walk started with |
| Same content hash in two folders shares one row (`uq_media_dedup`) | One of the two folders "loses" the file for weighting | low | Unchanged from #1251; the same-file guard on the update stands; documented |
| Two picked roots in one workspace | Two "Unsorted" rows and possibly two "memes" rows on the card | low | The view carries `source_id` and the picked folder's `config.folder_name`; the card groups by source when there is more than one |
| Gate fixtures seed `media_items` without a registry row | Existing gates break | low | `category_id` is nullable; existing seeds keep working; new gates seed rows |

## Complexity and Sequencing

| Phase | Size | Depends on | Parallel with |
|---|---|---|---|
| 1a Lazy full-depth walk (v2 cursor) | M | — | — |
| 1b Registry, migration 070, lifecycle, `category_id` on media | L | 1a | — |
| 2 Weights by id, the F4 rule, API v2 beside v1, walk-completion notice | M | 1b | — |
| 3 Web card on the registry, wizard card retired, docs close-out | M | 2 | — |

Critical path: 1a → 1b → 2 → 3. Each phase is one PR, reviewed with the two-lens round the repo uses, tests first at unit, gate and web tiers.
