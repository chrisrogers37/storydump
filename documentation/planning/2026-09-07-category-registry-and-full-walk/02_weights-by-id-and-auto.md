---
title: Phase 2 — weights by id, automatic weights, API v2 beside v1, the walk-completion notice
type: plan
status: draft
owner: chris
created: 2026-09-07
tags: [plan, scheduler, category-mix, api, phase-2]
---

# Phase 2 — weights by id, automatic weights, API v2 beside v1, the walk-completion notice

## Summary

Switch the weighted draw and the mix service from category names to registry ids, apply the F4 rule to folders without a weight, keep the whole-pool fallback, expose the registry and effective percentages through `GET /workspaces/{ws}/category-mix` **beside** the v1 keys the live card reads, accept `PUT` by id (and by name until phase 3), and say what a walk found once per walk to the workspace's bindings. Phase 3 is the consumer; the live card keeps working across the phase 2 → 3 deploy window because the v1 shape survives until phase 3 removes it.

## Evidence

- `src/services/target/scheduler.py:382-440` — mix read by name (`:387`), counts grouped by `m.category`, weighted draw (`:419`), pick by `m.category = :category`, whole-pool fallback (`:430`); per-account eligibility (`:369-380`: live intents and `recent` locks).
- `src/services/target/category_mix.py:53-160` — `normalize` (`:88-90` sum-to-one), `set_mix`, `current_mix`, `discovered_categories` by name; `:19-20` the one-level docstring.
- `src/api/routes/v1.py:627-660` — the routes (`:637` GET shape `mix`/`categories`; `:653` the `mix` body); `src/api/app.py` `MixInvalid` handler (400, `reason = invalid_mix_<why>`); `:232-234` the unmapped 500.
- `src/services/target/media_sync.py:350-370` — the sync's own binding notice (the nearer precedent to `_notice_no_media`); `src/services/target/outbox.py:166` `fanout_notification`; `src/services/target/scheduler.py:288`.
- `landing/src/lib/category-mix.ts:28,99` — the live card reads `data.mix` on GET and on the PUT response.

## Implementation Plan

### Dependencies
Phase 1b (registry rows and `category_id` columns exist; a walk has completed) and the F4 re-lock.

F5 (locked): rows in `category_post_case_mix` without a `category_id` are ignored by the planner and the id view, and are superseded like any other current row by the first `set_mix`. No migration and no matching rule.

### Blocks
Phase 3.

### Steps

1. **Planner** (`scheduler.execute_plan_slot`): read active registry rows joined to current mix rows by id — `SELECT c.id, c.name, c.debuted_at, x.ratio FROM media_categories c LEFT JOIN category_post_case_mix x ON x.workspace_id = c.workspace_id AND x.category_id = c.id AND x.effective_to IS NULL WHERE c.workspace_id = :ws AND c.state = 'active'`; count eligible media by `m.category_id` (the existing per-account eligibility). Rows: **explicit** (ratio > 0), **off** (ratio = 0: never drawn, excluded from automatic maths), **automatic** (no mix row). The F4 rule as re-locked (lean (a)): `A = min(Σ media share of automatic rows with eligible media, min explicit ratio)`; with no explicit rows `A = 1`; automatic rows split `A` by media share; explicit rows split `1 − A` by ratio. **Debut:** before the weighted draw, if an automatic row with eligible media has `debuted_at IS NULL`, draw from it (oldest `first_seen_at` first) and stamp `debuted_at = now()` (with `workspace_id` in the predicate). Draw over rows with eligible media; pick `WHERE m.category_id = :category_id`; keep the rotation order and the locks from #1251; `rng` stays injectable. **Fallback:** when the weighted set has no eligible media, the whole pool oldest-first as today (`:430`), so media with `category_id IS NULL` or in gone rows never goes silent and `_notice_no_media` fires only when nothing is eligible.
2. **Service** (`category_mix.py`): `normalize` takes `[{"category_id", "ratio"}]`, ratios ≥ 0 with 0 allowed (Off), sum-to-one over ratios > 0; `set_mix` validates every id is a registry row of the workspace in **any state** (`invalid_mix_unknown_category` otherwise; the planner filters `active`), writes `category_id` and the row's current `name` into `category` (label), keeps the advisory lock and the supersede; `current_mix` returns rows by id; `registry_view(executor, *, workspace_id)` → `[{category_id, source_id, source_name, name, is_root, state, media_count, ratio | null, effective}]` where `effective` is computed by the same function the planner uses, over **workspace-wide** eligibility (`state = 'available'`, no per-account locks) — the docstring, the route and the card say "approximate". Gone rows appear only while they hold a current ratio. `discovered_categories` is deleted; the docstring at `:19-20` is rewritten.
3. **Routes**: `GET /workspaces/{ws}/category-mix` → `{"rows": registry_view, "explicit_total": <sum of explicit ratios>, "mix": <current rows by name for the v1 card>, "categories": <active names>}`; `PUT` accepts `{"rows": [{"category_id", "ratio"}]}` and, until phase 3, the v1 `{"mix": [{"category", "ratio"}]}` body resolved by unique active name (`invalid_mix_ambiguous_name` when two active rows share a name); the response is the GET shape. Phase 3 removes `mix`/`categories` and the by-name body.
4. **Walk-completion notice** (in `media_sync.py` at the step-7 completion of phase 1b): once per completed walk, if any registry row of the source has `announced_at IS NULL` and media: the first completed walk says "Found N folders under <folder_name>: memes (3,474), merch (1,077), … — posting in proportion until you set weights in Settings › General › Category mix"; later walks say "New folder(s) under <folder_name>: events (100 files) — posts automatically (≈x %)"; 0-file rows are skipped and left unannounced; `announced_at` stamped on the rows named. One `fanout_notification` to the workspace's bindings per walk, never per row.
5. **Docs**: `06` §3 (the draw by id, the F4 rule, Off, the debut, the fallback), `03` ruling (F4 as re-locked, F8, F9), CHANGELOG.

## Test Plan

- Unit (`tests/src/services/target/test_work_loop.py`): the draw over ids; the F4 arithmetic (explicit 70/30 + a 10-file automatic folder; a 50,000-file automatic folder capped at 30 %; no explicit rows → proportional); Off rows never drawn and excluded from the maths; the debut draw and its stamp; a gone row never drawn; the whole-pool fallback when the weighted set is empty; locks and rotation preserved.
- Unit (`test_category_mix.py`): validation by id (`unknown_category`, 0 allowed, sum-to-one over ratios > 0), gone rows accepted by `set_mix`, `registry_view`'s `effective` equals the planner's weights (one function, asserted by calling both), v1 `mix`/`categories` derived from the view.
- Unit (`tests/src/api/test_v1_routes.py`): GET carries both shapes; `PUT` by id and by unique name at the admin floor; `ambiguous_name` refused; the PUT response is the GET shape.
- Gate (`tests/scripts/test_scheduler_clock_gate.py`): weights by id on the real table (the by-name seed at `:976-985` moves to ids); the automatic share for a folder with no mix row; the debut; the notice lands once per walk in `channel_outbox` (`test_customer_notice_gate.py` or the w6 gate).

## Verification Checklist

- `pytest tests/src/services/target/test_work_loop.py tests/src/services/target/test_category_mix.py tests/src/api/test_v1_routes.py` green.
- `pytest tests/scripts/test_scheduler_clock_gate.py tests/scripts/test_customer_notice_gate.py tests/scripts/test_w6_sync_gate.py` green on postgres:15.
- Production: `GET /workspaces/<ws>/category-mix` returns Unsorted, memes, merch with `effective` summing to 100 ± 0.1 and still carries `mix`/`categories`; the live card renders and saves unchanged.

## What NOT To Do

- Do not make automatic rows silent — that is the failure mode this epic exists to remove; do not make them dominant either (the cap).
- Do not compute the effective percentage in the web; one Python function feeds both the draw and the API.
- Do not remove the v1 keys or the by-name body in this phase; phase 3 does, in the same release train.
- Do not send one notice per registry row, and never before the walk has completed.

## Context

area: scheduler · category-mix service · API · sync notice — effort: M — risk: medium (draw semantics change; deploy-window compatibility) — priority: high
