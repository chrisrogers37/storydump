---
title: "Device-native inbound — phase 01: a system-created source enters the posting mix at a default share (PR 1)"
type: plan
status: active
owner: chris
created: 2026-09-20
tags: [plan, posting-mix, services]
links: []
---

> Phase 01 of [`00_EPIC.md`](00_EPIC.md), ratified 2026-09-20 at the forge gate and folded after ironclad cycle 1 (2026-09-25). **Waits on F12 (open):** this text implements F12(a), M1 with its freeze as cycle 1 corrected it; under F12(b) the phase is rewritten as a reserved share inside `weights()`. Spec: [`2026-09-20-device-native-inbound-spec.md`](../2026-09-20-device-native-inbound-spec.md). The ledger is [`RUN_LOG.md`](RUN_LOG.md); its entry for this phase records where the build departs from this text.

## Summary

One new service function beside the mix's existing writer: `category_mix.admit_source`. Given a source the system created, it enters the workspace's mix at an explicit 20% and every existing explicit share is scaled by 0.8; a workspace with no explicit mix first has its current automatic shares frozen into explicit rows, so the newcomer has something to take room from. Same per-workspace lock, same supersede-then-insert, same SCD history; `weights()` is untouched. Nothing calls it yet: phase 03 (the drop folder) and phase 06 (an album, if F10 keeps it) do. The rule is ratified as M1 in the decision doc; this phase builds it and records it in the design record. Cycle 1 corrected the freeze twice: it keeps Off rows Off, and a frozen share never rounds down to Off. Its larger finding — that the freeze moves the starvation to folders picked later — is F12, the owner's.

## Evidence

- `src/services/target/category_mix.py:41` `SUM_TOLERANCE = 0.001`; `:43` `MAX_SOURCES = 500`; `:47` `_LABEL` (`COALESCE(folder_name, folder_ref, 'folder')`); `:50-56` `MixInvalid` with the reason vocabulary `not_a_list · empty_source · duplicate_source · bad_ratio · sum_not_one · too_many_sources · unknown_source · all_off` (`ambiguous_name` went with the v1 compat, #1391); `:59-71` `_ratio` (NaN guard, four places, [0, 1]); `:74-78` `_sum_to_one`; `:81-103` `normalize`.
- `:106-145` `weights` — the one draw function: `:134` `pool = 1.0` with no explicit rows, `:136` `pool = 0.0` with no automatic rows, `:138-140` `r_min`, `share_auto`, `pool = min(share_auto, r_min / (1 + r_min))`, `:141-142` automatic split by media, `:144` explicit `(1 - pool) * ratio / total_ratio`.
- `:148-155` `_connected` (id, label for connected folders via `CONNECTED_SQL`, `workspaces.py:334`); `:158-210` `set_mix` — `unknown_source` before any write `:173`, the `all_off` rule `:178-179`, the advisory lock `:183-186` (`pg_advisory_xact_lock(hashtextextended('case_mix:<ws>', 0))`), the supersede UPDATE `:187-193`, the INSERT per row `:194-209` with `(workspace_id, source_id, category, ratio, created_by_user_id)`.
- `:213-261` `mix_view` (per-source `media_count`, current `ratio`, `effective` from `weights`; an `error` source counts `n = 0` at `:254`).
- Callers: `src/api/routes/v1.py:705-718` `get_category_mix` (the member floor is `principal.member_session`, `src/api/principal.py:359`, called at `v1.py:716`), `:728-748` `put_category_mix` (`principal.admin_session`, `principal.py:369`, called at `:740`; `set_mix` at `:744`; since #1391 the route reads only `rows`); `src/services/target/scheduler.py:427` `share = category_mix.weights(shaped)` in `execute_plan_slot`; `src/api/app.py:358-366` maps `MixInvalid` to 400 `invalid_mix_<reason>`.
- Model `src/models/target/intent_ledger.py:57-90` `CategoryPostCaseMix`: `ratio Numeric(5,4)` `:70`, `effective_from/to` `:71-72`, `created_by_user_id` nullable `:73` — written only at `category_mix.py:194-209`, never read back. Index `uq_case_mix_current_by_source` (`scripts/migrations/071_case_mix_by_source.sql:15-16`).
- Card: `landing/src/components/dashboard/settings/category-weights-card.tsx:123-128` body copy, `:168` the Automatic option, `:207`/`:218` the all-automatic copy; `landing/src/lib/category-mix.ts:70-100` `toMixBySource`, `:103-109` `evenSplit`, `:119-134` `saveCategoryMix`.
- Tests: `tests/src/services/target/test_category_mix.py` — `_Exec` scripted executor `:16-37`; `TestValidation:51`, `TestTheAutomaticRule:102` (the cap at `:144`), `TestSetMixIsOneSupersedeThenInserts:193`, `TestReads:271`. API `tests/src/api/test_v1_routes.py:1173` `TestCategoryMix`. Gate `tests/scripts/test_scheduler_clock_gate.py:1134` `TestTheCategoryMixShapesTheDraw` (its `_set_mix` seeding helper at `:1106-1131`, `clock_db`).
- Nothing named `admit_source` or `DEFAULT_ADMIT_RATIO` exists in `src/`, `tests/` or `landing/` (grep, 2026-09-20).

## Implementation Plan

### Dependencies

F12 locked as (a). Otherwise none.

### Blocks

Phase 03 (the relay job admits the drop source), and phase 06 if F10 keeps the album (its connect route admits it).

### Steps

1. **`src/services/target/category_mix.py`.** Add `DEFAULT_ADMIT_RATIO = 0.2` beside `SUM_TOLERANCE` with a comment naming the decision doc. Factor the body of `set_mix` from the lock onward (`:183-209`) into `async def _write_mix(executor, *, workspace_id, rows, labels, by_user_id)`; `set_mix` calls it unchanged in behaviour. Add:

   ```python
   async def admit_source(executor, *, workspace_id: str, source_id: str,
                          ratio: float = DEFAULT_ADMIT_RATIO,
                          by_user_id: Optional[str] = None) -> list[dict]:
   ```
   - `ratio = _ratio(ratio, source_id)`; refuse `bad_ratio` when `ratio <= 0`.
   - Take the advisory lock first (same key as `set_mix`), then read: the connected sources (`_connected`), the current rows (`x.ratio` joined on `effective_to IS NULL`), and the available-media count per source (the `mix_view` subquery at `:222-224`), all in the caller's transaction.
   - `source_id` not connected → `MixInvalid("unknown_source")`. `source_id` already carrying a current row → return the current mix unchanged (idempotent; no supersede).
   - Explicit rows exist (any current `ratio > 0`): new rows = every current row with `ratio > 0` scaled by `(1 - ratio)`, every `ratio == 0` row carried over as Off, plus `(source_id, ratio)`.
   - No explicit rows: compute `share = weights(shaped)` over the connected sources with `n = media_count` (an `error` source `n = 0`, as `mix_view` does) and their current rows, so an Off row weighs nothing; new rows = every current `ratio == 0` row carried over as Off (a mix of Off rows and Automatic folders is legal, `category_mix.py:174-179`, and superseding it must not turn an Off folder back on), every source with `share > 0` at `max(round(share * (1 - ratio), 4), 0.0001)` so a frozen share never rounds to Off, plus `(source_id, ratio)`. Sources with no media and no row stay automatic.
   - Round to four places; put the rounding residue on the largest row so `_sum_to_one` holds; then `_write_mix`. The label for each row is `_connected`'s label (the album's name key arrives in phase 06 via `_LABEL`).
2. **The design record.** `documentation/planning/2026-08-02-consolidated-design-plan/03-decision-record.md`, post-ratification rulings: one paragraph, "A system-created source enters the mix at a default share (owner, 2026-09-20)", citing the decision doc and the intent it restores — the posting mix's own fork F4 of the 2026-09-07 category-registry plan (`documentation/archive/2026-09-07-category-registry-and-full-walk/00_EPIC.md`, "a sensible rate, never takes over, never goes silent"), not this epic's F4. The advertised-DDL pin is unaffected (no SQL fence changes).
3. **No card copy in this phase.** Nothing calls `admit_source` until phase 03, so the weights card's sentence about system-created sources, the marking of the system row, and the statement of what the freeze does land with phase 03 (its Steps 8).
4. **CHANGELOG** under Unreleased.

## Test Plan

- Unit, `tests/src/services/target/test_category_mix.py`, a new `TestAdmitSource` on the `_Exec` executor: 70/30 explicit → 56/24/20; all automatic with counts 300/50 → 69/11/20 (frozen); one Off and two Automatic → the Off row stays 0 and the two freeze; a frozen share below 0.00005 floors at 0.0001; an Off row stays 0 beside explicit rows; an automatic source with no media gets no row; a second admit of the same source is a no-op (no supersede statement recorded); an unconnected source is `unknown_source`; `ratio = 0` is `bad_ratio`; a three-way split sums to one within `SUM_TOLERANCE`; the lock is the first statement issued. `set_mix`'s existing tests stay green through the `_write_mix` factoring.
- Gate, `tests/scripts/test_scheduler_clock_gate.py` beside `TestTheCategoryMixShapesTheDraw`: after `admit_source` as `svc_worker`, exactly one current row per source, the superseded rows carry `effective_to`, `created_by_user_id` is NULL, an Off row survives the freeze, and the draw gives the new source its 20% on the gate's shaped rows.
- Mutation battery `tests/mutations/device_native_01.sh` per `.claude/rules/testing.md:171-197`: one named mutation each for the Off carry-over, the 0.0001 floor, the idempotent re-admit and the lock order.

## Verification Checklist

- [ ] `.venv/bin/pytest tests/src/services/target/test_category_mix.py --no-cov -q` green, including the new class.
- [ ] The clock gate's mix class green with the new admit tests, on the real database.
- [ ] `tests/mutations/device_native_01.sh` ends `ran N of N mutations` with every mutation killed.
- [ ] `git diff` shows no change inside `weights()` (`category_mix.py:106-145`).
- [ ] The design record's new ruling paragraph present; the docs guard battery green.

## What NOT To Do

Do not change `weights()` or the automatic rule (M3 rejected; under F12(b) this phase is rewritten instead). Do not add a column or a migration. Do not call `admit_source` from the Drive folder pick (picked folders keep Automatic, by ruling). Do not write a current row for a source that already has one. Do not let a supersede turn an Off row back on.

## Context

Area: services (`category_mix`) · Effort: S · Risk: low · Priority: high (the first PR of both trains).
