---
title: "Phase 3 — Worker throughput: retries that end, a 429 that holds durably, a backpressure signal (3a); parallel runs without pinned connections (3b)"
type: plan
status: draft
owner: chris
created: 2026-09-09
tags: [plan, worker, outbox, jobs, pacing, observability]
links: []
---

# Phase 3 — Worker throughput

## Summary

The worker runs two jobs at a time per replica, treats a 429 as an ambiguous send, retries failures forever at a flat minute, and reports nothing about depth or age. This phase lands as **two PRs**. **3a (S)** — the fixes that each retire a known harm: a 429 becomes a durable hold on the `tg_global` rate row plus a rescheduled sender job (never an in-task sleep); retries end at `05:38`'s per-lane attempt *and deadline* budgets with a terminal `failed` and a user-language notice for tenant kinds that are not re-minted; the per-workspace cap rises to the `05` numbers; the sender hold shortens and the mint sweep gets its `LIMIT`; and the status line prints ready depth, oldest-runnable age, the `tg_global`-paced count and the per-workspace oldest wait — F9's production trigger (#716). **3b (M)** — K concurrent claim-and-run tasks per lane with **no pinned claim connections**: claims go through a pooled checkout returned at once, job sessions are released across holds and provider waits, and K is sized from the *measured* connections per running job so Σ = 50 stands. The pacing debit and the claim already commit before the provider call — phase 1 landed that (F8), because the tap's supersede needed it — so #1260 is closed before this phase starts.

## Evidence

- `src/worker.py:172-180` one `WorkLoop` per lane; `:413-415` one pinned claim connection per loop (`bind_claim_conn`); `:408` the election connection; `:428-431` one task per loop. `src/services/target/work_loop.py:557-591` `run_once` claims one and awaits it; `:663-667` `run`.
- **The pool model.** `worker.py:88-95` wraps every executor in `session.begin()` — a running job holds a pooled connection for its whole run; `work_loop.py:613-627` finalizes inside it; `deliver_outbox` (`:319`) holds that transaction across its 45 s hold (`:356-387`) while its poller borrows a second connection per tick (`:346`; `outbox.py:570`); the heartbeat borrows one per beat (`jobs.py:419`). `jobs.claim_job` (`src/services/target/jobs.py:118-151`) owns and commits its own transaction. `unit_of_work.py:89` `POOL_SIZE_SEAM = 10`, `:81` overflow 0, `:110` `POOL_TIMEOUT_SEAM = 3.0`, pinned by `create_engine` (`:180-189`). `05:41`: "a pool slot is an asyncio task, not a connection; connections are held only inside transaction blocks". The first draft's `8 + 4 + 2 ≤ 10` could not hold.
- `work_loop.py:61-82` `WorkerConfig`: `ws_lane_cap = 2` (`:69`; `05:33` says 5 interactive / 3 bulk), `claim_idle_seconds = 1.0` (`:70`), `retry_backoff_seconds = 60.0` flat (`:71`), `sender_hold_seconds = 45.0` (`:73`), `poller_interval_seconds = 2.0` (`:77`), `chat_limit 18/60 s` (`:78-79`), `global_limit 25/1 s` (`:80-81`).
- `jobs.py:302-336` `reschedule_job` — no `max_attempts` check; `work_loop.py:644-661` backs off flat 60 s on any exception; `scripts/migrations/056_machinery_tables.sql:102` `deadline_at` is never read. `src/services/target/publish_pipeline.py:513` is the only attempt ceiling. `05:38` row 8: interactive 3 attempts, deadline +10 min; bulk 5 attempts, backoff 1/5/15/60 min, deadline = slot end (or +6 h for non-slot jobs); no jitter named. `deliver_outbox` is minted with `max_attempts 3` (`work_loop.py:707`) and re-minted while rows are pending (`:713-715`) — **which `05:38` budget applies to it:** the interactive one (3 attempts, deadline +10 min), and because the sweep re-mints it, its `failed` is a log line, never a tenant notice.
- `src/services/target/outbox.py:103` `RESEND_KINDS` returns `approval_prompt` to pending with no cap; `:503` `mark_ambiguous` on any exception. After phase 1, `deliver` commits the debits and the claim before the provider call and marks after.
- `src/channels/telegram_transport.py:166-222` — 429 falls to `TelegramSendError` (`:222`); `retry_after` never parsed. The 2026-08 `memory://` limiter was removed (#1035; #581/#578): per-process pacing multiplies by replicas — a sleeping poller is that again.
- `scripts/migrations/059_security_definer_doors.sql:96-121` `fn_claim_job` with `p_ws_lane_cap` and `ORDER BY j.run_at`; its per-workspace count (`:109-112`) has no leased `(lane, workspace_id)` index; `056:123` `uq_jobs_serialized_lease`.
- `src/worker.py:229-262` `status_line` — no depth or age; `01-target-architecture.md:88` requires "per-lane queue depth + oldest-runnable-age"; #716 asks for a live saturation counter on the global bucket as fairness's trigger.
- `work_loop.py:692-718` `ensure_sender_jobs` — the mint sweep has no LIMIT (H5).
- `tests/scripts/test_l8_webhook_admission.py:330` — the concurrency-gate shape (#672).

## Implementation Plan

### Dependencies

3a: none (independent of phases 1 and 2; may land at any point). 3b: 3a; phase 1 (the sender's checkpointing, F8). F6 ratifies with 3a, F7 with 3b. The leased `(lane, workspace_id)` index is a migration (the next free number; owner approval).

### Blocks

3a: phase 2's `one_slow_chat` edit-landed criterion; phase 4's production trigger. 3b: phase 4 (the leased index); the tight `one_slow_chat` bound.

### Steps

**3a — the fixes (one PR)**

1. **Tests first** (see Test Plan).
2. **429 as a durable hold.** `_call` parses `parameters.retry_after` on 429 and raises `TelegramPaced(retry_after_s)`. `deliver` catches it outside any transaction (the sender is checkpointed): the row stays `pending` (its `sending` claim returned to `pending` by `_leave_sending`), and in one short transaction the hold is written to `rate_counters` — scope `tg_global`, key `''`, every 1 s window from now through `now + min(retry_after, 60)` upserted to `count = limit` in one statement — so every replica's `increment` defers (`OutboxPaced`) until the hold passes; `deliver_outbox` then ends its hold early and reschedules its own job at `now + retry_after` with `restore_attempt=True`. No in-task sleep; `mark_ambiguous` is no longer the 429 path; the poller's `deferred` counter counts it. A 429 that Telegram scopes to one chat writes the same hold on `tg_chat` for that binding.
3. **Attempt ceiling and deadlines (F6 (a)).** `_run_job`'s failure path reads `max_attempts` and `deadline_at`: `attempts >= max_attempts` or `now >= deadline_at` → `finalize_job(…, "failed")`; else `reschedule_job` at `now + backoff(lane, attempts)` — interactive: `[10, 30, 60] s`, deadline-bound (+10 min); bulk: `[60, 300, 900, 3600][min(attempts-1, 3)] s` — both ± 20 % jitter. For a tenant kind that the sweep does not re-mint (`deliver_outbox` is excluded; `publish_pipeline` keeps its own ceiling) a `notification` outbox row in user language naming the thing, not the kind — "The sync of your Drive folder failed and won't retry until the next scheduled sync; open Settings on the web" — with the machine detail in the log. `RESEND_KINDS` gains a cap: an `approval_prompt` resent 3 times fails and the intent's reaper handles the rest.
4. **Per-workspace cap.** `ws_lane_cap` becomes per lane: 5 interactive, 3 bulk (`05:33`).
5. **Sender hold and sweep bound.** `sender_hold_seconds` 45 → 15, so a busy binding yields its lane sooner; the sweep re-mints while rows remain. `ensure_sender_jobs` gains `LIMIT 200` per sweep (H5).
6. **Backpressure signal.** `status_line` gains per lane `ready=<count> oldest_age=<s>` from one query (`SELECT lane, count(*), max(now()-run_at) FROM jobs WHERE state='ready' AND run_at<=now() GROUP BY lane`), `outbox_pending=<count>`, `tg_global_paced=<count in the last minute, hold active y/n>` and `ws_oldest_wait=<workspace, s>` (the workspace whose oldest ready job has waited longest) — the two numbers #716 asked for, F9's production trigger; `scheduling_health` exposes the same for `/health/scheduling`.
7. **Docs.** `05:33` (per-workspace cap) and `05:38` (budgets) marked built with the chosen numbers; `02` §6 amendment for the durable 429 hold; CHANGELOG.

**3b — K concurrency (one PR)**

8. **Tests first** (see Test Plan).
9. **K tasks per lane, no pinned connections (F7 (a)).** `WorkerConfig.lane_concurrency = {"interactive": 4, "bulk": 2}` (env-overridable); `compose` builds K `WorkLoop`s per lane sharing the lane name and registry; `bind_claim_conn` is retired — `run_once` claims on a pooled checkout (`engine.connect()` → `jobs.claim_job`, which commits → returned) and holds nothing between claims; the election connection stays the one pinned connection; the heartbeat registers every lease. **Job sessions released across waits:** `_run_job`'s `session_for` becomes per-checkpoint — an executor that waits (the sender's hold, a provider call, a container poll) does so outside any transaction, and `finalize_job` runs in its own transaction; `deliver_outbox` opens the job session only to finalize, its poller ticks running on their own short transactions as today.
10. **The ceiling assert and the measurement.** The composition root logs `lanes: interactive×K bulk×K pool=10` and asserts `K_interactive + K_bulk + 3 (election, clock, heartbeat) ≤ POOL_SIZE_SEAM` for the *start* values — the bound under which even every task DB-active at once fits. Raising K past that bound requires the measurement the gate records: pool `checked_out_peak` with K jobs of the most database-active kind running; K may rise toward `05:31`'s 10/50 only while `peak + 3 ≤ POOL_SIZE_SEAM` holds with zero `TimeoutError`, and the PR that raises it cites the number. Σ(replica × (pool + overflow)) = 50 is unchanged.
11. **The leased index.** Migration (next free number): `CREATE INDEX ix_jobs_leased_lane_ws ON jobs (lane, workspace_id) WHERE state = 'leased'` — `fn_claim_job`'s per-workspace count runs K times as often.
12. **Re-run `one_slow_chat`** with the tight bound: other chats' edit-landed p95 within one poller cadence of the same run's `taps_across_many_cards` — the K senders are what free the lane from a slow binding.
13. **Docs.** `05:31` (per-process concurrency) with the chosen K and the measured connections per running job; `05:41` cited as honoured; README rows; CHANGELOG.

## Test Plan

Unit (3a): backoff tables with jitter bounds per lane; the deadline path; the attempt path; the notification text for a tenant kind and its absence for `deliver_outbox`; `TelegramPaced` parsed from a 429 body; the hold-window upsert statement. Unit (3b): the composition assert at the start values and its refusal at 8/4; `lane_concurrency` from the environment.
Gate (`tests/scripts/test_jobs_lease_gate.py`, `test_outbox_sender_gate.py`, `test_w1_worker_gate.py`; every concurrency assertion proves the property and that the race was concurrent — `test_l8_webhook_admission.py:330`'s shape, never wall-clock, #672): 3a — a 429 from the fake writes the hold rows and reschedules the sender job at `now + retry_after`, a second replica's poller defers on the hold, and the row stays `pending`; a job at `max_attempts` ends `failed` with a notification row; a job past `deadline_at` ends `failed` with fewer attempts; a `deliver_outbox` job that fails three times ends `failed` with no notification and the sweep re-mints it; the status line reports depth, age, paced count and oldest wait against seeded rows. 3b — K loops on one lane claim distinct keys and never two of one key (the unique index proves it); K `deliver_outbox` jobs under a slow fake run with pool `checked_out_peak + 3 ≤ POOL_SIZE_SEAM` and zero `TimeoutError`, the number recorded; no lease expires during a 30 s provider wait (the heartbeat's checkout is never starved); a seeded 120 s send on one binding does not delay a send on another.

## Verification Checklist

- [ ] 3a: `pytest tests/scripts/test_jobs_lease_gate.py tests/scripts/test_outbox_sender_gate.py tests/scripts/test_w1_worker_gate.py -q` green; the status line shows `ready=`, `oldest_age=`, `tg_global_paced=` and `ws_oldest_wait=` per lane; a 429 in the gate writes a hold and reschedules, never sleeps.
- [ ] 3b: worker startup log prints `lanes: interactive×4 bulk×2 pool=10` (or the configured numbers) and the ceiling assert `K_interactive + K_bulk + 3 ≤ POOL_SIZE_SEAM` holds; the gate's measured `checked_out_peak` is recorded in the PR and `peak + 3 ≤ 10` with zero `TimeoutError`.
- [ ] 3b: a seeded 120 s send on one binding does not delay a send on another binding (gate); `one_slow_chat` re-run and its edit-landed bound met.
- [ ] The leased index migration applied with its postcondition.
- [ ] `05:31`, `05:33`, `05:38` marked built; #716 answered with a citing comment.

## What NOT To Do

- Do not prefetch or batch claims (`05:35` row 5).
- Do not pin claim connections; do not hold a job session across a hold, a provider call or a container poll.
- Do not sleep in-task on a 429; do not move pacing out of `rate_counters` into process memory (#1035).
- Do not tell a tenant that a re-minted job "will not retry".
- Do not let an ambiguous send be blind-resent (R8).
- Do not raise K past the assert without the measured number in the PR.

## Context

area: worker, outbox, jobs · effort: S (3a) + M (3b), two PRs · risk: medium · priority: P1 — 3a at any point, 3b after phase 2 (or beside it).
