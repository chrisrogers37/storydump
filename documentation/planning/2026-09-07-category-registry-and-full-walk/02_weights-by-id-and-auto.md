---
title: Phase 2 — weights by id, auto weights, API v2, the discovery notice
type: plan
status: draft
owner: chris
created: 2026-09-07
tags: [plan, scheduler, category-mix, api, phase-2]
---

# Phase 2 — Weights by id, auto weights, API v2, the discovery notice

## Summary

Switch the weighted draw and the mix service from category names to registry ids, give folders discovered after the mix was set an auto weight (F4 lean b), expose the registry and effective percentages through `GET /workspaces/{ws}/category-mix`, accept `PUT` by id, and say a new folder once to the workspace's bindings. The web card (phase 3) is the consumer; until it lands, the existing card keeps working against a compatibility shape.

## Evidence

- `src/services/target/scheduler.py:382-440` — mix read by name, counts grouped by `m.category`, weighted draw, pick by `m.category = :category`.
- `src/services/target/category_mix.py:53-160` — `normalize`, `set_mix`, `current_mix`, `discovered_categories` by name.
- `src/api/routes/v1.py:627-660` — the routes; `src/api/app.py` `MixInvalid` handler (400, `reason = invalid_mix_<why>`).
- `src/services/target/scheduler.py:213` `_notice_no_media` and `prompts.push_bindings` — the pattern for a one-time notice to bindings.
- `landing/src/lib/category-mix.ts:1-60` — the shape the current card reads (`mix`, `categories`).

## Implementation Plan

### Dependencies
Phase 1 (registry rows and `category_id` columns exist; the first walk has run).

F5 (locked): rows in `category_post_case_mix` without a `category_id` are ignored by the planner and the API, and are superseded like any other current row by the first `set_mix` from the new card. No migration and no matching rule.

### Blocks
Phase 3.

### Steps

1. **Planner** (`scheduler.execute_plan_slot`): read `SELECT c.id, c.name, x.ratio FROM media_categories c LEFT JOIN category_post_case_mix x ON x.workspace_id = c.workspace_id AND x.category_id = c.id AND x.effective_to IS NULL WHERE c.workspace_id = :ws AND c.state = 'active'`; count eligible media by `m.category_id`; explicit rows = those with a ratio; auto rows = active rows with eligible media and no ratio, weight = `(count / total_eligible) × mean(explicit ratios)` (when no explicit rows exist at all, every row is auto and the draw is proportional to media, i.e. the whole pool); draw over rows with eligible media; pick `WHERE m.category_id = :category_id`. Keep the rotation order and the locks from #1251. `rng` stays injectable. Rows with `category_id IS NULL` do not join (F5).

2. **Service** (`category_mix.py`): `normalize` takes `[{"category_id", "ratio"}]`; `set_mix` validates every id is an active registry row of the workspace (`invalid_mix_unknown_category`), writes `category_id` and the row's current `name` into `category` (label), keeps the advisory lock and the supersede; `current_mix` returns rows by id; new `registry_view(executor, workspace_id)` → `[{category_id, name, path, state, media_count, ratio | null, effective}]` where `effective` is the planner's weight as a percentage (the same function computes both, so the card never disagrees with the draw). `discovered_categories` is deleted.

3. **Routes**: `GET /workspaces/{ws}/category-mix` → `{"rows": registry_view, "explicit_total": <sum of explicit ratios>}`; `PUT` body `{"rows": [{"category_id", "ratio"}]}`; a body in the old `{"mix": [{"category", "ratio"}]}` shape is refused `invalid_mix_by_name` (phase 3 ships the new card in the same release train, so no compatibility shim outlives review).

4. **Discovery notice**: in the sync's registry upsert (phase 1 returns `created`), when a NEW active row appears for a workspace that has at least one current mix row, enqueue one `notification` to the workspace's bindings — "New folder 'events' found under storydump-media — it posts on auto (≈5 %); set a weight in Settings › General › Category mix" — and stamp `announced_at`; never twice for one row.

5. **Docs**: `06` §3 (the draw by id and auto weights), `03` ruling (F4), CHANGELOG.

## Test Plan

- Unit (`tests/src/services/target/test_work_loop.py`): the draw over ids; an auto row's weight = media share × mean explicit; no explicit rows → proportional to media; a gone row never drawn; locks and rotation preserved (SQL shape).
- Unit (`test_category_mix.py`): validation by id (`unknown_category`, sum-to-one over explicit rows only), `registry_view` effective percentages match the planner's weights (one function, asserted by calling both).
- Unit (`tests/src/api/test_v1_routes.py`): `GET` shape, `PUT` by id at the admin floor, the by-name body refused.
- Gate (`tests/scripts/test_scheduler_clock_gate.py`): weights by id on the real table; auto share for a folder with no mix row; the discovery notice lands once in `channel_outbox`.

## Verification Checklist

- `pytest tests/src/services/target/test_work_loop.py tests/src/services/target/test_category_mix.py tests/src/api/test_v1_routes.py` green.
- `pytest tests/scripts/test_scheduler_clock_gate.py tests/scripts/test_customer_notice_gate.py` green on postgres:15.
- Production: `GET /workspaces/<ws>/category-mix` returns three rows (Unsorted, memes, merch) with `effective` summing to 100 ± 0.1.

## What NOT To Do

- Do not make auto rows silent (weight 0) — that is the failure mode this epic exists to remove.
- Do not compute the effective percentage in the web; one Python function feeds both the draw and the API.
- Do not keep a by-name write path.

## Context

area: scheduler · category-mix service · API — effort: M — risk: low-medium (draw semantics change) — priority: high
