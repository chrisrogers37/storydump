---
title: Phase 3 — the Category mix card on the registry
type: plan
status: draft
owner: chris
created: 2026-09-07
tags: [plan, web, settings, phase-3]
---

# Phase 3 — The Category mix card on the registry

## Summary

Rebuild `CategoryWeightsCard` on `GET /workspaces/{ws}/category-mix`'s registry rows: every active folder with its current name and path, its media count, a percentage input for explicit rows and "auto (≈x %)" for the rest, gone rows shown with their weight so it can be moved, and copy that no longer warns about renames or depth. Saving sends ids.

## Evidence

- `landing/src/components/dashboard/settings/category-weights-card.tsx:22-140` — the name-keyed card; `landing/src/lib/category-mix.ts:24-140` — `percentRows`, `toMix`, `evenSplit`, `saveCategoryMix`, `mixRefusalCopy`; `landing/src/app/api/workspaces/[id]/category-mix/route.ts` — the `PUT` proxy; `landing/src/app/(dashboard)/dashboard/settings/page.tsx` — fetches `category-mix` and renders the card after the schedule.
- `landing/src/lib/category-mix.test.ts` — the current tests to rewrite.

## Implementation Plan

### Dependencies
Phase 2 (the `rows` shape and `PUT` by id).

### Blocks
None.

### Steps

1. `lib/category-mix.ts`: types `RegistryRow {category_id, name, path, state, media_count, ratio: number | null, effective: number}`; `cardRows(rows)` → editable rows (explicit percent or `auto` with the effective value); `toMixById(rows)` → `[{category_id, ratio}]` for explicit rows only, sum-to-100 over explicit rows (auto rows excluded), "set auto" = removing the row's ratio; `saveCategoryMix(ws, rows)` sends `{"rows": …}`; refusal copy for `invalid_mix_unknown_category` ("that folder is no longer here — reload").
2. `category-weights-card.tsx`: rows show name, muted path, `N files`, a badge for `gone` ("folder no longer found"), an input for explicit rows and a toggle "Set a weight" / "Back to auto"; the footer total counts explicit rows only; Split evenly applies to explicit rows; the intro copy says: "Each top-level subfolder of a synced Drive folder is a category. Folders you have not weighted post on auto, in proportion to how much media they hold. Renaming a folder in Drive changes nothing here."
3. BFF `PUT` forwards `rows`; the page passes the new response type.
4. Media page: none.

## Test Plan

- `landing/src/lib/category-mix.test.ts`: `cardRows` (auto vs explicit, gone rows kept), `toMixById` (sum over explicit rows; auto excluded; unknown id), `saveCategoryMix` request shape.
- `npx vitest run`, `npx tsc --noEmit`, `npx eslint .` green.

## Verification Checklist

- Settings › General › Category mix lists Unsorted, memes, merch with counts; setting memes 70 / merch 30 saves and reloads to the same numbers; Unsorted shows "auto (≈0 %)" with 0 files.
- Renaming `memes` in Drive and syncing changes the row's name and nothing else.

## What NOT To Do

- Do not let the card compute effective percentages; render the API's.
- Do not hide gone rows; a weight nobody can see is a weight nobody can move.

## Context

area: web (settings) — effort: M — risk: low — priority: high
