---
title: "Phase 2 — The API under load: admission, a timeout boundary, the process shape, and the harness that proves it"
type: plan
status: active
owner: chris
created: 2026-09-09
tags: [plan, ingress, admission, load, harness]
links: []
---

# Phase 2 — The API under load

## Summary

Thousands of taps arriving at once must never turn into 500s that Telegram redelivers, nor into spinners that die unanswered. The route's one boundary becomes the pool's own timeout, mapped to a refusal *before* admission — for a tap, an answer by name ("Busy — tap again") rather than a 503; after admission only a database error around commit is a 503, and everything else is a named `tap_failed`. Per-workspace admission (S.2) is checked before admission and debited only for executed flips, at the number F12 rules. The process shape (F5) is measured, not assumed: the harness runs first at today's one process. The versioned load harness (S.1) is built here with the four scenarios the owner named — each with its workspace spread stated, its client bound to Telegram's `max_connections`, the real Procfile running as a subprocess with the target worker — and its report decides F1 on end-to-end tap→answer latency, redelivery included.

## Evidence

- `src/api/routes/webhooks.py:114-197` — no timeout boundary; the seam refuses 503 **before** admission only when no dispatcher is wired (`:156-168`); a pool `TimeoutError` at `runtime.connect()` (`:170`) is a 500; after admission any exception is a 500. `tests/scripts/test_l8_webhook_admission.py:371` `TestAnAbortedWinnerDoesNotPoisonTheKey` is the precedent for a rolled-back admission.
- `src/services/target/unit_of_work.py:81` `MAX_OVERFLOW_SEAM = 0`, `:89` `POOL_SIZE_SEAM = 10`, `:110` `POOL_TIMEOUT_SEAM = 3.0` ("the saturation policy" — the boundary already exists, unmapped); `create_engine` (`:180-189`) pins all three; `05-operational-numbers.md:52` "DB connections 50 total (3×10 workers + 2×10 ingress)".
- `Procfile:2` `web: uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}` — one process; `railway.toml` sets no `startCommand`. `Procfile:1` is the legacy `python -m src.main`; the target worker is `python -m src.worker` (`src/worker.py:3`).
- `scripts/telegram_webhook.py:245-254` — `setWebhook` without `max_connections` (Telegram default 40, max 100): the route never sees more than that many simultaneous deliveries; the rest queue at Telegram as `pending_update_count` (`:192`). `src/api/app.py:454` `/health`, `:467` `/health/scheduling` (`scheduling_health.scheduling_lag`/`worker_freshness`, `:530-531`).
- `src/services/target/rate_counters.py:43-` `increment(scope, key, window_start, limit)` returns `None` over the limit; its `ON CONFLICT … WHERE rc.count < :limit` (`:65-69`) is the fail-closed backstop; `02-domain-model.md` §6 names scope `ws_admission` (`scripts/migrations/056_machinery_tables.sql:231-232`); `05:51` "30 commands/min/workspace; no global ceiling" (D22, `03:99`) — a *per-workspace abuse guard*. The web route's single call site is `src/api/routes/v1.py:210` (`commands.ingest`).
- `src/services/target/telegram_dispatch.py:17-25` — never raise after admission (a raise rolls the admission back; Telegram redelivers the same update forever through a per-bot FIFO); #985 — refuse before admission, never after.
- `tests/scripts/test_l8_webhook_admission.py` — admission gate: `TestTwoSimultaneousDeliveriesProduceExactlyOneAdmission` (`:256`), `test_the_race_was_genuinely_concurrent` (`:330`), `TestTheGatesOwnNumber` (`:424`, 200 replays of one `update_id`); no concurrent-distinct-update or latency scenario. `tests/src/api/test_webhook_ingress_route.py` drives the route with an in-process ASGI client — one process, one pool — so a process-shape decision cannot be measured there. `tests/scripts/conftest.py:623` `seed_workspace_chain`, `:650` `seed_intent_chain` seed one card each; no seeder for 50 × 20 sent cards.
- `04-execution-sequence.md:348` S.1 "versioned harness (200-click / 250-due-account scenarios)"; `:350` S.2 "pg fixed-window per-workspace admission … fail-closed". #557/#686: the unanswered-until-redelivery hang. `src/services/target/work_loop.py:77` poller cadence 2 s, `:78-80` per-chat 18/60 s and global 25/1 s — the card edit's pacing.

## Implementation Plan

### Dependencies

Phase 1 (the harness taps real cards; the sender is checkpointed); 3a (`one_slow_chat`'s edit-landed criterion needs the 15 s sender hold and the durable 429 hold); F11 ratified (the order); F5 and F12 ratify with this phase.

### Blocks

The F1 decision; phase 4 (the harness; the scenario phase 4 adds).

### Steps

1. **Tests first**: the harness scenarios and the admission gate below.
2. **The boundary is the pool's.** The API's engine is created with an ingress-specific pool timeout — a new pinned seam `INGRESS_POOL_TIMEOUT_SEAM = 1.0` in `unit_of_work`, used by the API's `create_engine` beside `POOL_TIMEOUT_SEAM` for the worker (never `asyncio.wait_for` around a checkout, which can cancel mid-checkout and leak a connection). The route catches `sqlalchemy.exc.TimeoutError` from `runtime.connect()` *before* admission: for a `callback_query` it answers by name without a database — parse the token first (no connection), `answer_callback("Busy — tap again")`, return 200 `{"status": "refused", "outcome": "busy"}`, nothing admitted (F1 (d): the tap is re-derivable because its buttons remain); for any other update it logs `parked: pool saturated` and raises 503 (a message has no spinner; redelivery is right). After admission: `_tap` never raises (phase 1 step 9) — every non-database exception is committed as outcome `tap_failed`, logged with `update_id`, counted; only a database error around `admit()`/`commit()` maps to 503, the admission row rolling back with the transaction (`test_l8_webhook_admission.py:371`) so the update is redelivered once the fault clears. The route answers 200 with `{"status": "admitted", "outcome": <named outcome>}` for every named outcome, refusals included.
3. **Per-workspace admission (S.2), for taps.** In `TelegramDispatcher._tap`, after `resolve_chat` and **before `admit()`** (#985): a read of the workspace's current `ws_admission` window count (no debit) against `settings.TARGET_TAP_ADMISSION_PER_MINUTE` (F12's number); at the limit the tap is answered "Too many actions at once — try again in a minute" (`show_alert=True`), 200, not admitted — the update is consumed deliberately because the tapper was told, and a repeat tap is a fresh update. The **debit** (`rate_counters.increment`) runs inside the flip's transaction only after `_settle` has decided a flip executes — an `answered` outcome and a refusal never spend budget; if the increment returns `None` there (two taps raced the check), the flip rolls back and the tap is answered by name — never a 503. **Is extending `ws_admission` to the web route in scope?** A rider: it is S.2's own README line at one call site (`v1.py:210`) and lands in this phase only if F12 locks (a) (one number, one key per workspace); under (b) or (c) it is filed as its own S.2 follow-up so the tap's number does not decide the web's.
4. **The process shape (F5), measured first.** The harness runs at today's one process (`Procfile:2` unchanged) before anything changes. If the baseline misses p95 with the "Busy" rate bounded, `Procfile:2` → `uvicorn src.api.app:app --workers 2 --host 0.0.0.0 --port ${PORT:-8000}` on one replica, `POOL_SIZE_SEAM` staying 10 per process, and `max_connections` re-registered to 20. Either way the composition root logs the pool arithmetic at startup **and `/health` reports it**: `pool: {size, overflow, timeout_s, checked_out_peak}`, `ingress_workers`, `webhook: {max_connections, pending_update_count}` — the last sampled in the background every 60 s from `getWebhookInfo`, never inline — plus the tap counters from phase 1 step 12.
5. **The harness (S.1).** `tests/scripts/load/` (new): `harness.py` — an asyncio client posting synthetic `callback_query` updates at the route with the secret header, **bound to `max_connections` simultaneous deliveries** (read from the registration constant): offered taps beyond that queue in the harness as Telegram's `pending_update_count` would, and a non-2xx delivery is redelivered on a documented schedule (the harness's constant, since Telegram's is undocumented); `fake_telegram.py` — `answerCallbackQuery`, `editMessageReplyMarkup`, `editMessageCaption`/`editMessageText`, `getWebhookInfo` (reporting the harness's own backlog), capturing per-call latency and a per-binding delay; `seed.py` — 50 workspaces × 20 `sent` cards with refs and two bindings each, built on `seed_workspace_chain`/`seed_intent_chain`; the **real `Procfile` `web:` command as a subprocess** on the scratch DSN (so F5 is measurable) and the **target worker as a subprocess** (`python -m src.worker` with the transport's `api_base` at the fake — never `python -m src.main`) so the card edit lands; container settings pinned (`postgres:15` with `synchronous_commit=off` and `max_connections` sized to Σ for the harness, named in the report). Scenarios, each stating its workspace spread: `taps_1000_across_50_workspaces` (50 workspaces × 20 cards; 1,000 taps offered in ≤ 1 s, delivered at `max_connections`); `double_tap_one_card` (one workspace, one card, 50 taps in 100 ms); `taps_across_many_cards` (10 workspaces × 20 cards, one tap each, concurrently — under F12 (a) that is 20/workspace, inside the cap; the scenario says so); `one_slow_chat` (10 workspaces; one binding's fake answers every call in 5 s while the others answer at once). Report: 5xx count; pre-admission refusals ("Busy" answers and 503s); redeliveries; p50/p95 **tap→answer end-to-end** from the first delivery attempt, redelivery included; route-200 p95; edit-landed p95 (the strip and the outcome line); flips landed; audit rows; `pending_update_count` peak; pool `checked_out_peak`; the transaction's statement count. Written as a pytest module marked `load` and skipped unless `RUN_LOAD_HARNESS=1`, on the Docker database. One additional run at production-like RTT (a Railway scratch database or an injected-latency proxy) so F1/F5 are not decided on Docker fsync.
6. **The F1 evidence.** **Which latency is the SLO:** the *answer* — tap → `answerCallbackQuery` received at the fake, end-to-end, redelivery included; the route's 200 and the edit are reported, not judged. Run the harness at today's shape, then at the `05` arithmetic if F5 (a) is taken. Commit the report under `tests/scripts/load/reports/2026-MM-DD.md`. If `taps_1000_across_50_workspaces` shows any 5xx, an answer p95 > 2 s, or a "Busy" rate above the bound the owner sets when ratifying F1, re-serve F1 with the numbers: option (b) then adds migration 072 (`ck_jobs_kind` + `telegram_update`), a `TelegramUpdateExecutor` that calls the same `_tap`, and the route's fallback when the pool times out; option (d)'s stronger form (answer "Got it" after the parse, outcome in the edit) is the cheaper alternative.
7. **Docs.** `05:51` (admission) marked built with F12's number; README Live status rows 46–47 (S.1/S.2); `07` §18 only if 072 exists; CHANGELOG.

## Test Plan

Unit: the boundary (pool timeout → "Busy" answer + 200 not admitted for a tap; → 503 before admission for a message; a database error around commit → 503 with the admission rolled back; a non-database exception after admission → 200 `tap_failed`), admission (the check refuses before `admit()` at the limit; an `answered` outcome does not debit; an executed flip debits once), the `/health` fields, the `--workers 2` startup log if taken.
Gate: `tests/scripts/test_l8_webhook_admission.py` gains `TestManyDistinctDeliveriesAtOnce` (200 distinct updates concurrently: 200 admissions, zero errors, pool `checked_out_peak` ≤ `POOL_SIZE_SEAM`, the race proven concurrent — never wall-clock, #672); the harness scenarios above. **Does `double_tap_one_card` also assert zero 5xx and p95?** Yes: exactly one flip, 49 `answered`, zero 5xx, answer p95 < 2 s while the taps convoy on the row lock — that criterion is what would re-serve F2 toward (d).

## Verification Checklist

Built 2026-09-10 in two PRs (steps 2–4; steps 5–6). The reports:
`tests/scripts/load/reports/2026-09-10.md` (loopback), `…-rtt10ms.md` (≈ 20 ms database RTT
through the harness's latency proxy — the deciding run) and `…-rtt10ms-workers2.md` (F5 (a),
measured). Read against the deciding run:

- MET: `taps_1000_across_50_workspaces` 5xx 0, delivery-side answer p95 0.667 s, pre-admission
  refusals 0, `pending_peak` 990 reported; `taps_across_many_cards` 200 flips; `one_slow_chat` other
  chats' answer p95 0.684 s, within 200 ms of the run's `taps_across_many_cards` (0.618 s); the busy
  boundary exercised at twenty connections against the pool of ten (1 busy, 0 5xx).
- MISSED, on the numbers: `double_tap_one_card` answer p95 **2.027 s** against the 2 s bound (2.022 s
  with two workers; 0.086 s on loopback) — the row-lock convoy of 50 taps in 100 ms on ONE card, each
  waiting for the previous flip's commit at ≈ 24 round trips × 20 ms. The harness's own assertion
  fails on it in the deciding run, deliberately: the plan's Test Plan says this criterion is what
  would re-serve F2 toward (d). **Ratified (owner, 2026-09-10 — "as long as these are filed for fixing, I'm ok with moving
  on"):** F2 (a) stands; the bound reads "≈ 2 s at 20 ms RTT, shrinking with #1286". The harness
  assertion for `double_tap_one_card` is loosened to 2.1 s with that reading. **#1286 landed
  (2026-09-11):** a post tap is 12 statements inside the dispatch (was 20) — the GUCs and lock
  timeout in one, the tapper and their name in one, no second tenant set in the gate, the token
  read with the intent, the supersede of every binding in one — pinned by the tap gate at
  `TAP_STATEMENT_BUDGET = 11` cursor statements; the route adds its dedup insert and the commit.
  **#1284 landed** with it: the answer and strip are background tasks behind the 200.
  **Re-read (2026-09-11, `reports/2026-09-11-rtt10ms.md`, after #1290):** `double_tap_one_card`
  answer p95 **1.733 s** (was 2.027 s) — F2 (a)'s 2 s bound is met without the exception, and the
  harness assertion is restored to `< 2.0`; `taps_1000_across_50_workspaces` answer p95 0.577 s
  (was 0.667), user-side wait p95 43.1 s (was 56.0), `answer_late` 348 (was 496);
  `taps_across_many_cards` 0.616 s; `one_slow_chat` 0.602 s; the 20-connection boundary 1.048 s,
  wait p95 45.3 s, late 375. The burst's wait is still throughput-bound (one process, ten
  connections, ≈ 13 round trips per tap) — phase 3b's and F5's territory, not the tap's.
  **After 3b (#1291, `reports/2026-09-11-rtt10ms-post3b.md`):** `double_tap_one_card` 1.693 s;
  the 1,000-burst answer p95 0.541 s, wait p95 41.1 s, `answer_late` 312; `taps_across_many_cards`
  0.615 s; `one_slow_chat` 0.537 s; the 20-connection boundary 0.996 s, wait 42.3 s, late 329. The
  tap side is unchanged by 3b, as expected — the worker's concurrency moves the sender's throughput,
  which this harness cannot yet read (#1292).
- NOT MET and not claimed: the user-side wait for a 1,000-simultaneous burst (56 s, half past
  Telegram's expiry — throughput-bound, #1286); `one_slow_chat`'s edit-landed criterion (the sender
  lands ≈ 0.5 supersede rows/s fleet-wide — one lane, claim-one-await-one; phase 3a/3b's number).

- [x] `pytest tests/scripts/test_l8_webhook_admission.py -q` green.
- [x] `RUN_LOAD_HARNESS=1 pytest tests/scripts/load -q -m load` produces a report; `taps_1000_across_50_workspaces`: 5xx = 0, end-to-end answer p95 < 2 s, pre-admission refusals within the ratified bound, `pending_update_count` peak reported; `taps_across_many_cards`: 200 flips over 10 workspaces; `one_slow_chat`: other chats' answer p95 < 2 s and within 200 ms of the same run's `taps_across_many_cards` p95.
- [ ] `double_tap_one_card`: exactly one flip, zero 5xx — met; **p95 < 2 s — missed by 27 ms at the deciding RTT (owner ruling pending, above).**
- [ ] `one_slow_chat`: other chats' edit-landed p95 within one poller cadence (2 s) plus pacing — not met (the sender's throughput; phase 3a/3b).
- [x] The report names the container settings, the RTT of the run, and which latency each number is.
- [x] `/health` reports the pool arithmetic, `max_connections`, `pending_update_count` and the tap counters; the startup log prints the arithmetic; the Procfile matches the F5 ruling.
- [x] F1, F5 and F12 locked in `00_EPIC.md` with the report cited (F2's exception awaits the owner).

## What NOT To Do

- No global ceiling (D22). No process-local limiter (the 2026-08 `memory://` bucket is gone for a reason, #1035).
- Never answer 200 for an update that was neither executed nor answered by name; never 503 after admission for anything but a database error.
- Never `asyncio.wait_for` a pool checkout.
- Do not debit `ws_admission` for an `answered` outcome or a refusal.
- Do not run the harness against production; do not run `python -m src.main` anywhere in it.
- Do not decide F1 or F5 on a run whose client exceeds `max_connections` or whose database is Docker fsync alone.

## Context

area: ingress, admission, load · effort: M · risk: medium · priority: P0 after phase 1 (and after #1220 step 3 if F11 locks (a)).
