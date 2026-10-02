---
title: "Device-native inbound — phase 01: a system-created source enters the posting mix at a default share (PR 1)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, posting-mix, services]
links: []
---

> Phase 01 of [`00_EPIC.md`](00_EPIC.md), ratified 2026-09-20 at the forge gate and folded after ironclad cycle 1 (2026-09-25). **F12 ruled (b), 2026-09-28; rewritten 2026-10-01** from [the review](review-2026-10-01.md): this phase is now a reserved share inside `weights()`, and the F12 (a) text it replaces — `admit_source` and its freeze — is dropped (the RUN_LOG keeps its history). It ships last on the drops path: under (b) nothing is written to the mix when a drop source is created, so phase 03 does not wait on it, but customers get drops only after it. Cycle 2 re-cites the Steps against #1507, which moves the draw's folder read into `category_mix.pool_folders`, and F16 decides what the reservation counts. Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md); its entry for this phase records where the build departs from this text.

## Summary

`weights()` gives a source the system created — today only the Telegram drops folder, marked by `role` in its config — a reserved share of 20% while it is left on Automatic and has media to post; every other source keeps today's rule over the remaining 80%, and the card's Weight and Off still override the reservation. Nothing is written to `category_post_case_mix` when a system source is created, so the team's weights are never rewritten and no history rows appear that nobody typed. The role reaches `weights()` through the draw's folder read and `mix_view`; both mix cards — Settings' weights card and, since #1490, the overview's Posting mix card — label the row "Default 20%" while its mode stays Automatic. The design record's 2026-09-08 ruling is amended in place. What "has media to post" means is F16: any eligible file as ruled, or (the lean) only never-posted ones, so a few old drops do not take a fifth of the slots every month.

## Evidence

Cited on `main` at `29acea2e` (`728b087d`); `category_mix.py` is unchanged on `9be8daf6`, and cycle 2 re-cites the draw against #1507.

- `src/services/target/category_mix.py:41` `SUM_TOLERANCE = 0.001`; `:43` `MAX_SOURCES = 500`; `:47` `_LABEL` (`COALESCE(folder_name, folder_ref, 'folder')`); `:50-56` `MixInvalid` with the reason vocabulary `not_a_list · empty_source · duplicate_source · bad_ratio · sum_not_one · too_many_sources · unknown_source · all_off` (`ambiguous_name` went with the v1 compat, #1391); `:59-71` `_ratio` (NaN guard, four places, [0, 1]); `:74-78` `_sum_to_one`; `:81-103` `normalize`.
- `:106-145` `weights` — the one draw function: `:134` `pool = 1.0` with no explicit rows, `:136` `pool = 0.0` with no automatic rows, `:138-140` `r_min`, `share_auto`, `pool = min(share_auto, r_min / (1 + r_min))`, `:141-142` automatic split by media, `:144` explicit `(1 - pool) * ratio / total_ratio`.
- `:148-155` `_connected` (id, label for connected folders via `CONNECTED_SQL`, `workspaces.py:334`); `:158-210` `set_mix` — `unknown_source` before any write `:173`, the `all_off` rule `:178-179`, the advisory lock `:183-186` (`pg_advisory_xact_lock(hashtextextended('case_mix:<ws>', 0))`), the supersede UPDATE `:187-193`, the INSERT per row `:194-209` with `(workspace_id, source_id, category, ratio, created_by_user_id)`.
- `:213-261` `mix_view` (per-source `media_count`, current `ratio`, `effective` from `weights`; an `error` source counts `n = 0` at `:254`).
- Callers: `src/api/routes/v1.py:705-718` `get_category_mix` (the member floor is `principal.member_session`, `src/api/principal.py:359`, called at `v1.py:716`), `:728-748` `put_category_mix` (`principal.admin_session`, `principal.py:369`, called at `:740`; `set_mix` at `:744`; since #1391 the route reads only `rows`); `src/services/target/scheduler.py:427` `share = category_mix.weights(shaped)` in `execute_plan_slot`; `src/api/app.py:358-366` maps `MixInvalid` to 400 `invalid_mix_<reason>`.
- Model `src/models/target/intent_ledger.py:57-90` `CategoryPostCaseMix`: `ratio Numeric(5,4)` `:70`, `effective_from/to` `:71-72`, `created_by_user_id` nullable `:73` — written only at `category_mix.py:194-209`, never read back. Index `uq_case_mix_current_by_source` (`scripts/migrations/071_case_mix_by_source.sql:15-16`).
- Card: `landing/src/components/dashboard/settings/category-weights-card.tsx:123-128` body copy, `:168` the Automatic option, `:207`/`:218` the all-automatic copy; `landing/src/lib/category-mix.ts:70-100` `toMixBySource`, `:103-109` `evenSplit`, `:119-134` `saveCategoryMix`.
- Tests: `tests/src/services/target/test_category_mix.py` — `_Exec` scripted executor `:16-37`; `TestValidation:51`, `TestTheAutomaticRule:102` (the cap at `:144`), `TestSetMixIsOneSupersedeThenInserts:193`, `TestReads:271`. API `tests/src/api/test_v1_routes.py:1173` `TestCategoryMix`. Gate `tests/scripts/test_scheduler_clock_gate.py:1134` `TestTheCategoryMixShapesTheDraw` (its `_set_mix` seeding helper at `:1106-1131`, `clock_db`).
- The web derives a row's mode from its ratio alone (`landing/src/lib/category-mix.ts:50`), and `toMixBySource` writes every row that is not Automatic, Off as 0 and anything else as its percent (`:93-98`), so a fourth mode would be saved as Off. The overview's Posting mix card prints `effective` as "N% auto" (`landing/src/components/dashboard/posting-mix-card.tsx`), and its Posted column counts cadence posts only (#1490).
- Draft #1507 moves the draw's folder read into `category_mix.pool_folders` and its rows pass through as `{**row}`, so after it a role reaches `weights()` with no scheduler edit (on its branch at `d6ca6432`, 2026-10-01).
- Nothing named `admit_source` or `DEFAULT_ADMIT_RATIO` exists in `src/`, `tests/` or `landing/` (grep, 2026-09-20), and nothing will: F12 (b) dropped it.

## Implementation Plan

### Dependencies

F12 (b), locked 2026-09-28; F16; #1507 merged; the stall fix (#1545; [the review](review-2026-10-01.md) §4), so a reserved share never concentrates a file the publish cannot fetch.

### Blocks

Customers' drops: phase 03 merges dark without this phase, and the switch is offered to customers only after it ships. Phase 06, if a named need ever reopens the album.

### Steps

1. **`weights()`** (`category_mix.py:106-145`). A row marked `reserved` and left on Automatic takes `RESERVED_SHARE = 0.2` off the top while its count is above zero; today's rule then runs unchanged over the remaining `1 - 0.2` — explicit rows by ratio, the automatic pool by file count and capped as today. When the reserved source is the only one with a count, it takes everything. An explicit Weight or Off on the reserved row overrides the reservation, as for any row. `RESERVED_SHARE` sits beside `SUM_TOLERANCE` with a comment naming the decision doc and F12.
2. **The role reaches the draw.** The draw's folder read (`category_mix.pool_folders` once #1507 merges; the SELECT in `scheduler.py` before it) and `mix_view` select `config->>'role' = 'telegram_drops'` as `reserved`. The count the reservation uses is F16's: every eligible file under (a), or `FILTER (WHERE m.last_posted_at IS NULL)` under (b), the lean.
3. **The cards.** `mix_view`'s rows answer `reserved`; `cardRows` keeps the mode `automatic`, and both mix cards label the row "Default 20%". The weights card's rule copy and its all-automatic copy (`category-weights-card.tsx:123-129`, `:207`) gain one sentence: a folder Storydump creates for the workspace, such as Telegram drops, draws 20% while it has media to post (under F16 (b), new media), and Weight or Off on its row overrides that.
4. **The design record.** Amend the 2026-09-08 ruling in place (`documentation/planning/2026-08-02-consolidated-design-plan/03-decision-record.md:205`) with the reserved share, dated 2026-09-28, citing the decision doc and F12; no new ruling id (`:185`). The advertised-DDL pin is unaffected: no SQL fence changes.
5. **CHANGELOG** under Unreleased.

## Test Plan

- Unit, `tests/src/services/target/test_category_mix.py`, a new `TestTheReservedShare`: a reserved Automatic source with media beside Automatic folders of 300 and 50 files → 20%, and the other 80% split by file count under today's cap; beside 70/30 explicit → 20/56/24; reserved with nothing to count → 0, the rest renormalized; reserved and alone → 100%; the reserved row given a Weight → its ratio, no reservation; given Off → 0; under F16 (b), a source whose files have all been posted falls back to today's rule.
- Gate, `tests/scripts/test_scheduler_clock_gate.py` beside `TestTheCategoryMixShapesTheDraw`: a role-marked source draws its 20% on the gate's shaped rows, and no row is written to `category_post_case_mix`.
- Web: a reserved row reads as Automatic in `cardRows` and round-trips unchanged through `toMixBySource`.
- Mutation battery `tests/mutations/device_native_01.sh` per `.claude/rules/testing.md:171-197`: one named mutation each for the off-the-top reservation, the renormalization when alone, the override by Weight or Off, and, under F16 (b), its filter.

## Verification Checklist

- [ ] `.venv/bin/pytest tests/src/services/target/test_category_mix.py --no-cov -q` green, including the new class.
- [ ] The clock gate's mix class green with the reserved-share tests, on the real database.
- [ ] `tests/mutations/device_native_01.sh` ends `ran N of N mutations` with every mutation killed.
- [ ] On a dev workspace with a drop source, both mix cards show "Default 20%" and `category_post_case_mix` has no row nobody typed.
- [ ] The design record's 2026-09-08 ruling carries the amendment; the docs guard battery green.

## What NOT To Do

Do not write anything to `category_post_case_mix` when a system source is created. Do not add a fourth web mode. Do not reserve a share for a folder a person picked (picked folders keep Automatic, by ruling). Do not change the automatic rule itself (M3 rejected).

## Context

Area: services (`category_mix`), web (both mix cards), design record · Effort: S–M · Risk: low-medium (the draw is a hot path) · Priority: high, and last on the drops path.
