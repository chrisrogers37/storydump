---
title: Phase 3 — the Category mix card on the registry, and the close-out
type: plan
status: superseded
owner: chris
created: 2026-09-07
tags: [plan, web, settings, docs, phase-3]
---

> **Superseded 2026-09-08** by the sources-are-groups ruling (`03-decision-record.md`); the card is specified in `04_weights-by-source.md`. Kept as the record.

# Phase 3 — the Category mix card on the registry, and the close-out

## Summary

Rebuild `CategoryWeightsCard` on the GET's registry rows with two columns per row — "Your weight" (an input, Automatic, or Off) and "Posts about" (the API's effective %) — gone rows shown only while they hold a weight, Unsorted hidden while empty and unweighted, explicit states for no source / first walk pending / failed load, and copy that no longer warns about renames or depth. Saving sends ids. The same PR removes the v1 keys and by-name body from the API, retires the setup wizard's legacy card, and closes the documentation out.

## Evidence

- `landing/src/components/dashboard/settings/category-weights-card.tsx:22-150` — the name-keyed card (`:33` `percentRows(data, data)`, `:112-120` the empty copy, `:126` `flex-wrap`, `:146` input `aria-label`); `landing/src/lib/category-mix.ts:24-140` — `percentRows`, `toMix` (`:64` drops 0 rows), `evenSplit`, `saveCategoryMix` (`:99` reads `data.mix`), `mixRefusalCopy` (`:106` matches `invalid_mix_`); `landing/src/app/api/workspaces/[id]/category-mix/route.ts:26` — the BFF emits `invalid_mix:not_a_list` with a colon; `landing/src/app/(dashboard)/dashboard/settings/page.tsx:120,221-225` — fetch and render (disabled for non-admins).
- `landing/src/components/dashboard/setup-wizard.tsx:28,439` — imports the legacy card `landing/src/components/dashboard/settings/category-mix-card.tsx` (`:10-19` its legacy shape).
- `landing/src/app/(marketing)/setup/media-organize/page.tsx:32-50` — teaches one level and no loose files.
- `landing/src/components/ui/badge.tsx:13-18` — badge variants; `integrations-tab.tsx:560` — Sync now.
- `landing/src/lib/category-mix.test.ts` — the current tests to rewrite.
- Docs restating the one-level rule: `.claude/rules/scheduler.md:12-15`, `02-domain-model.md:654-659`, `06-product-lifecycles.md:45`, `landing/src/lib/category-mix.ts:4-9`.

## Implementation Plan

### Dependencies
Phase 2 (the `rows` shape and `PUT` by id).

### Blocks
None.

### Steps

1. `lib/category-mix.ts`: types `RegistryRow {category_id, source_id, source_name, name, is_root, state, media_count, ratio: number | null, effective: number}`; `cardRows(rows)` → editable rows (`mode: "explicit" | "automatic" | "off"`, the typed percent for explicit rows, the effective percent for every row); gone rows kept only when `ratio !== null`; Unsorted dropped when `media_count === 0 && ratio === null`; `toMixById(rows)` → `[{category_id, ratio}]` for explicit and Off rows (Off = ratio 0, kept, never dropped), sum-to-100 over explicit rows only; `saveCategoryMix(ws, rows)` sends `{"rows": …}` and reads the GET-shaped response; refusal copy for `invalid_mix_unknown_category` ("That folder is no longer here — reload the page") and `invalid_mix_ambiguous_name` never reaches the card (ids only). The docstring at `:4-9` is rewritten.
2. `category-weights-card.tsx`: per row — name, muted `source_name` when the workspace has more than one source, `N files`, a text badge "folder no longer found" (`secondary`, never `destructive`) for gone rows; column "Your weight": a percentage input for explicit rows, "Automatic" with a "Set a weight" button, or "Off"; a per-row menu "Automatic / Off / Set a weight" with `aria-label`s; column "Posts about": the effective % (sums to 100); footer "Total of your weights" over explicit rows only; "Split evenly" converts every active row to an explicit even split; gone rows show "Remove weight". Three states: no Drive source ("Connect Google Drive under Integrations"), no completed walk yet ("Folders appear after the first sync — or Sync now under Integrations"), failed load. Intro copy: "Each top-level subfolder of your synced Drive folder is a category. Folders you have not weighted post automatically, in proportion to their files. Rename folders freely — the weight follows the folder." Unsorted's muted line reads "Files directly in <folder_name>".
3. BFF `PUT` forwards `rows`; the refusal reason uses the underscore form (`invalid_mix_not_a_list`); the page passes the new response type.
4. API: remove `mix`/`categories` from the GET and the by-name `PUT` body (phase 2 step 3's transitional keys); `test_v1_routes.py` updated. This step is its **own small PR**, merged only after the phase-3 web deploy is live on Vercel — Railway deploying ahead of Vercel would otherwise make the still-live v1 card throw (`category-mix.ts:28`).
5. Setup wizard: replace the legacy `category-mix-card.tsx` with the new card (or a link to Settings › General), delete the legacy file and its tests.
6. Marketing guide `media-organize/page.tsx:32-50`: subfolders at any depth, loose files post as Unsorted, renames are safe.
7. Close-out: `.claude/rules/scheduler.md:12-15`, `02-domain-model.md:654-659`, `06-product-lifecycles.md:45` updated; `03-decision-record.md` ruling (already drafted in 1b/2) marked built; README Live status; CHANGELOG.

## Test Plan

- `landing/src/lib/category-mix.test.ts`: `cardRows` (automatic vs explicit vs off; gone rows kept only with a weight; empty Unsorted hidden), `toMixById` (sum over explicit rows; Off kept as 0; automatic excluded; unknown id), `saveCategoryMix` request and response shapes.
- Component test (vitest + testing-library, as the repo's other dashboard cards): the three states render; the effective column sums to 100 with an automatic row present.
- `npx vitest run`, `npx tsc --noEmit`, `npx eslint .` green; `pytest tests/src/api/test_v1_routes.py` green after step 4.

## Verification Checklist

- Settings › General › Category mix lists memes and merch with counts and Unsorted hidden (0 files, no weight); setting memes 70 / merch 30 saves and reloads to the same numbers; "Posts about" shows 70 / 30.
- Placing a new subfolder with files in Drive and syncing shows it as Automatic with a non-zero "Posts about" and a one-line group message; renaming `memes` in Drive and syncing changes the row's name and nothing else.
- Setting a folder to Off shows "Off" and its "Posts about" 0.
- The setup wizard no longer imports `category-mix-card.tsx`; `grep -rn "category-mix-card" landing/src` is empty.

## What NOT To Do

- Do not let the card compute effective percentages; render the API's.
- Do not hide a gone row that holds a weight; a weight nobody can see is a weight nobody can move — and do not show gone rows that hold none.
- Do not treat a typed 0 as "automatic"; 0 is Off.
- Do not leave the one-level rule standing anywhere in the docs, rules or docstrings listed under Evidence.

## Context

area: web (settings) · API cleanup · docs — effort: M — risk: low — priority: high
