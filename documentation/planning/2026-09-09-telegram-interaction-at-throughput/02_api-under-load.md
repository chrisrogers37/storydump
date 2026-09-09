---
title: "Phase 2 — The API under load: admission, a timeout boundary, two processes, and the harness that proves it"
type: plan
status: draft
owner: chris
created: 2026-09-09
tags: [plan, ingress, admission, load, harness]
links: []
---

# Phase 2 — The API under load

## Summary

Thousands of taps arriving at once must never turn into 500s that Telegram redelivers. The route gains one timeout boundary that refuses *before* admission, per-workspace admission of 30 commands a minute through `rate_counters`, and the API runs as two processes with the connection budget unchanged. The versioned load harness (S.1) is built here with the four scenarios the owner named, and its report decides fork F1.

## Evidence

- `src/api/routes/webhooks.py:114-197` — no `asyncio.wait_for`; the seam refuses 503 **before** admission only when no dispatcher is wired (`:156-168`); after admission any exception is a 500.
- `src/services/target/unit_of_work.py:81` `MAX_OVERFLOW_SEAM = 0`, `:89` `POOL_SIZE_SEAM = 10`, `:110` `POOL_TIMEOUT_SEAM = 3.0`; `05-operational-numbers.md:52` "DB connections 50 total (3×10 workers + 2×10 ingress)".
- `Procfile:2` `web: uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}` — one process.
- `src/services/target/rate_counters.py:43-` `increment(scope, key, window_start, limit)` returns `None` over the limit; `02-domain-model.md` §6 names scope `ws_admission`; `05:51` "30 commands/min/workspace; no global ceiling" (D22, `03:99`).
- `tests/scripts/test_l8_webhook_admission.py` — admission gate: `TestTwoSimultaneousDeliveriesProduceExactlyOneAdmission` (`:256`), `TestTheGatesOwnNumber` (`:424`, 200 replays of one `update_id`); no concurrent-distinct-update or latency scenario.
- `04-execution-sequence.md:348` S.1 "versioned harness (200-click / 250-due-account scenarios)"; `:350` S.2 "pg fixed-window per-workspace admission … fail-closed".

## Implementation Plan

### Dependencies

Phase 1 (the harness taps real cards).

### Blocks

The F1 decision; phase 4.

### Steps

1. **Tests first**: the harness scenarios and the admission gate below.
2. **Timeout boundary.** In `telegram_webhook`, wrap `runtime.connect()` acquisition in `asyncio.wait_for(…, ADMIT_BUDGET_S = 1.0)`; on timeout log `parked: pool saturated` and raise 503 *before* admission (the provider redelivers; nothing is lost). After admission, exceptions from `dispatch` are caught, logged with the `update_id`, the transaction rolled back, and the route still answers 200 with `{"status": "admitted", "outcome": "dispatch_failed"}` only when the dispatcher returned a named refusal; an unexpected exception after admission rolls back the admission too (the `admit` row is in the same transaction) and answers 503 so the update is redelivered once the fault clears.
3. **Per-workspace admission (S.2).** In `TelegramDispatcher._tap`, after `resolve_chat`: `rate_counters.increment(scope="ws_admission", key=workspace_id, window_start=window_start(now, 60), limit=settings.TARGET_ADMISSION_PER_MINUTE (30))`; `None` → answer the tap "Too many actions at once — try again in a minute" and write nothing. The same call guards `commands.ingest` for the web route so the budget is one per workspace across channels.
4. **Two processes (F5 (a)).** `Procfile:2` → `uvicorn src.api.app:app --workers 2 --host 0.0.0.0 --port ${PORT:-8000}`; `POOL_SIZE_SEAM` stays 10 per process; the composition root logs the pool arithmetic at startup. `railway.toml` unchanged.
5. **The harness (S.1).** `tests/scripts/load/` with `harness.py` (asyncio client posting synthetic `callback_query` updates at the route with the secret header; a fake Telegram server for `answerCallbackQuery`/`editMessage*` capturing latency) and scenarios: `taps_1000_across_50_workspaces` (1,000 taps in ≤ 1 s over 50 workspaces × 20 cards), `double_tap_one_card` (50 taps on one card in 100 ms), `taps_across_many_cards` (200 cards, one tap each, concurrently), `one_slow_chat` (one binding's fake Telegram answers in 5 s while others answer at once). Report: 5xx count, redeliveries, p50/p95 answer latency, flips landed, audit rows. Written as a pytest module marked `load` and skipped unless `RUN_LOAD_HARNESS=1`, on the Docker database.
6. **The F1 evidence.** Run the harness at the `05` arithmetic (2 × 10). Commit the report under `tests/scripts/load/reports/2026-MM-DD.md`. If `taps_1000_across_50_workspaces` shows any 5xx after admission or p95 > 2 s, re-serve F1 with the numbers; option (b) then adds migration 072 (`ck_jobs_kind` + `telegram_update`), a `TelegramUpdateExecutor` that calls the same `_tap`, and the route's fallback when `wait_for` times out.
7. **Docs.** `05` row 10 (admission) marked built; README Live status rows S.1/S.2; `07` §18 only if 072 exists; CHANGELOG.

## Test Plan

Unit: the boundary (timeout → 503 before admission; refusal after admission → 200 with outcome; unexpected → rollback + 503), admission (31st tap in a minute answered by name), the `--workers 2` startup log.
Gate: `tests/scripts/test_l8_webhook_admission.py` gains `TestManyDistinctDeliveriesAtOnce` (200 distinct updates concurrently: 200 admissions, zero errors, pool never exceeded); the harness scenarios above.

## Verification Checklist

- [ ] `pytest tests/scripts/test_l8_webhook_admission.py -q` green.
- [ ] `RUN_LOAD_HARNESS=1 pytest tests/scripts/load -q -m load` produces a report; `taps_1000_across_50_workspaces`: 5xx after admission = 0, p95 < 2 s; `double_tap_one_card`: exactly one flip; `taps_across_many_cards`: 200 flips; `one_slow_chat`: other chats' p95 unchanged.
- [ ] `Procfile` runs two workers; `/health` reports the pool arithmetic.
- [ ] F1 locked in `00_EPIC.md` with the report cited.

## What NOT To Do

- No global ceiling (D22). No process-local limiter (the 2026-08 `memory://` bucket is gone for a reason).
- Never answer 200 for an update that was neither executed nor answered.
- Do not run the harness against production.

## Context

area: ingress, admission, load · effort: M · risk: medium · priority: P0 after phase 1.
