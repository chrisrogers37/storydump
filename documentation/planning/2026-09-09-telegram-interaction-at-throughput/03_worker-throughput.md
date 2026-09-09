---
title: "Phase 3 — Worker throughput: parallel runs, pacing that does not lock, retries that end, and a backpressure signal"
type: plan
status: draft
owner: chris
created: 2026-09-09
tags: [plan, worker, outbox, jobs, pacing, observability]
links: []
---

# Phase 3 — Worker throughput

## Summary

The worker runs two jobs at a time per replica, locks the fleet-wide Telegram rate row across a provider call, treats a 429 as an ambiguous send, and retries failures forever at a flat minute. This phase makes a replica run K jobs per lane, commits the pacing debit before the provider call (#1260), waits on 429 for what Telegram says, ends retries at the `05` budget with a terminal `failed`, raises the per-workspace cap to the plan's numbers, and prints ready depth and oldest-runnable age so overload is visible.

## Evidence

- `src/worker.py:172-180` one `WorkLoop` per lane; `:413-417` one pinned claim connection per loop; `:428-431` one task per loop. `src/services/target/work_loop.py:557-591` `run_once` claims one and awaits it; `:663-667` `run`.
- `work_loop.py:60-100` `WorkerConfig`: `ws_lane_cap = 2` (`05:33` says 5 interactive / 3 bulk), `retry_backoff_seconds = 60.0` flat, `sender_hold_seconds = 45.0`, `chat_limit 18/60 s`, `global_limit 25/1 s`.
- `src/services/target/jobs.py:302-336` `reschedule_job` — no `max_attempts` check; `work_loop.py:644-661` backs off flat 60 s on any exception. `src/services/target/publish_pipeline.py:513` is the only attempt ceiling.
- `src/services/target/outbox.py:467-482` debits `tg_chat` and `tg_global` in the sender's transaction, then `:489` awaits the provider; `mark_ambiguous` on any exception (`:502`); `RESEND_KINDS` (`:103`) returns `approval_prompt` to pending with no cap.
- `src/channels/telegram_transport.py:166-222` — 429 falls to `TelegramSendError` (`:222`); `retry_after` never parsed.
- `scripts/migrations/059_security_definer_doors.sql:96-121` `fn_claim_job` with `p_ws_lane_cap` and `ORDER BY j.run_at`; `scripts/migrations/056_machinery_tables.sql:123` `uq_jobs_serialized_lease`.
- `src/worker.py:230-262` `status_line` — no depth or age; `01-target-architecture.md:88` requires "per-lane queue depth + oldest-runnable-age".
- `work_loop.py:692-718` `ensure_sender_jobs` — the mint sweep has no LIMIT (H5).

## Implementation Plan

### Dependencies

None (independent of phases 1 and 2).

### Blocks

Phase 2's `one_slow_chat` scenario passing; #1260.

### Steps

1. **Tests first** (see Test Plan).
2. **K runs per lane (F7 (a)).** `WorkerConfig.lane_concurrency = {"interactive": 8, "bulk": 4}` (env-overridable); `compose` builds K `WorkLoop`s per lane sharing the lane name and registry, each with its own pinned claim connection; `worker.py` starts K tasks per lane; the heartbeat registers every lease. Pool arithmetic: the pinned claim connections come out of `POOL_SIZE_SEAM`; the composition root asserts `K_interactive + K_bulk + 1 (election) + 1 (clock) ≤ POOL_SIZE_SEAM` and logs the numbers.
3. **Per-workspace cap.** `ws_lane_cap` becomes per lane: 5 interactive, 3 bulk (`05:33`).
4. **Debit before the call (F8 (a)).** `outbox.deliver` splits: `pace(session)` — the two `increment` calls in a short transaction committed by the poller before the send; `send(session, row)` — claim, provider call, mark. `OutboxPoller.tick` commits between them. A spent debit on a failed send is accepted (pacing, not accounting).
5. **429 as a wait.** `_call` parses `parameters.retry_after` on 429 and raises `TelegramPaced(retry_after_s)`; `deliver` catches it, leaves the row `pending`, and the poller sleeps `retry_after_s` (bounded at 60) before its next tick; `mark_ambiguous` is no longer the 429 path.
6. **Attempt ceiling (F6 (a)).** `_run_job`'s failure path: `attempts >= max_attempts` → `finalize_job(…, "failed")` and, for a tenant kind, `outbox.fanout_notification` "A background task failed and will not retry: <kind>"; else `reschedule_job` at `now + backoff(attempts)` with `backoff = [60, 300, 900, 3600][min(attempts-1, 3)] ± 20 % jitter`. `RESEND_KINDS` gains a cap: an `approval_prompt` resent 3 times fails and the intent's reaper handles the rest.
7. **Sender hold.** `sender_hold_seconds` 45 → 15, so a busy binding yields its task sooner; the sweep re-mints while rows remain. `ensure_sender_jobs` gains `LIMIT 200` per sweep (H5).
8. **Backpressure signal.** `status_line` gains per lane `ready=<count> oldest_age=<s>` from one query (`SELECT lane, count(*), max(now()-run_at) FROM jobs WHERE state='ready' AND run_at<=now() GROUP BY lane`) and `outbox_pending=<count>`; `scheduling_health` exposes the same for `/health`.
9. **Docs.** `05` rows 3/8 marked built with the chosen numbers; `02` §6 amendment for the debit-before-call rule; CHANGELOG; close #1260.

## Test Plan

Unit: backoff table with jitter bounds; attempt ceiling terminalises and notifies; `TelegramPaced` parsed from a 429 body; the composition assert on pool arithmetic.
Gate (`tests/scripts/test_jobs_lease_gate.py`, `test_outbox_sender_gate.py`, `test_w1_worker_gate.py`): K loops on one lane claim distinct keys and never two of one key (the unique index proves it); a job at `max_attempts` ends `failed` with a notification row; a paced tick leaves the row pending and the global row **unlocked** during a slow send (a second sender proceeds while the first awaits); the status line reports depth and age against seeded rows.

## Verification Checklist

- [ ] `pytest tests/scripts/test_jobs_lease_gate.py tests/scripts/test_outbox_sender_gate.py tests/scripts/test_w1_worker_gate.py -q` green.
- [ ] Worker startup log prints `lanes: interactive×8 bulk×4 pool=10` (or the configured numbers) and the arithmetic assert holds.
- [ ] A seeded 120 s send on one binding does not delay a send on another binding (gate).
- [ ] The status line shows `ready=` and `oldest_age=` per lane.
- [ ] #1260 closed with a citing comment.

## What NOT To Do

- Do not prefetch or batch claims (`05:33` row 5).
- Do not move pacing out of `rate_counters` into process memory.
- Do not let an ambiguous send be blind-resent (R8).

## Context

area: worker, outbox, jobs · effort: M · risk: medium · priority: P1, parallel with phase 2.
