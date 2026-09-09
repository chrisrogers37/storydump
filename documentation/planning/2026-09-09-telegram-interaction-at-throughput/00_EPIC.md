---
title: The Telegram tap, built for throughput (epic)
type: plan
status: draft
owner: chris
created: 2026-09-09
tags: [plan, telegram, ingress, worker, throughput, admission, epic]
links: []
---

# The Telegram tap, built for throughput

## Summary

In the target tier a button tap on an approval card is admitted, deduplicated on its `update_id`, answered with a 200 — and dropped. No handler consumes the `v1:<action>:<intent>` buttons the cards carry, nothing answers the tap, nothing flips the intent. The plan's W4 inbound half (`01` §1, `02` §5 "interactive commands … executed inline in ingress") was never built; #854 tracks it. This plan builds the tap and, behind it, the two layers that decide whether the product survives thousands of taps at once: the API's admission and process shape, and the worker's parallelism, pacing and retry ceilings. The owner's requirement (2026-09-09): thousands of concurrent taps across many workspaces without a crash or a hang; on one card, rapid taps process the first and never hang; taps across many cards register at once and run in parallel where safe or wait where they must.

## Evidence

The current state, verified 2026-09-09 against `main` @ `d5eea5d`:

- **No tap handler.** `src/services/target/telegram_dispatch.py:41` names `NOT_A_START` for "a callback query"; `__call__` (`:71-91`) returns it unhandled (`:84-88`). Pinned by `tests/src/services/target/test_telegram_dispatch.py:49-54`. `src/services/target/prompts.py:51-66` mints `v1:<action>:<intent-uuid>` tokens for `post/posted/skip/reject` that nothing parses.
- **No answer to a tap.** `answerCallbackQuery` and `editMessage*` appear nowhere in `src/channels/telegram_transport.py` (methods: `_call` `:166`, `send_text` `:230`, `send_media` `:245`, `for_chat` `:281`). The only inbound reply path is `_acknowledge` in `src/api/routes/webhooks.py:200-218`, gated on `result.handled`, sending directly through `runtime.reply` outside the outbox and outside pacing.
- **The route is inline and unbounded.** `src/api/routes/webhooks.py:114-197`: secret → JSON → `update_id` → `admit()` (`command_dedup` on `(telegram, "", update_id)`) → `dispatch()` → `commit()`. No timeout boundary; any exception is a 500, which Telegram redelivers. Pool: `POOL_SIZE_SEAM = 10`, `MAX_OVERFLOW_SEAM = 0`, `POOL_TIMEOUT_SEAM = 3.0` (`src/services/target/unit_of_work.py:81-110`); one process (`Procfile:2` runs `uvicorn` with no `--workers`). No per-workspace admission (`05-operational-numbers.md:51` specifies 30 commands/min/workspace; README Live status row 47: S.2 not built).
- **The flip has no lock and refuses a repeat.** `command_executors._intent_row` (`src/services/target/command_executors.py:91-111`) reads without `FOR UPDATE`; `_flip` (`:125-130`) turns the database's refusal into `CommandRefused("illegal_transition")`; `intent_ledger.transition` (`src/services/target/intent_ledger.py:94-133`) is a bare `UPDATE`. The authority is `trg_intent_guard` (`scripts/migrations/055_intent_ledger_tables.sql:306-323`): terminal rows are immutable, edges must be in `post_intent_transitions` (`awaiting_approval → approved|posted|skipped|rejected|expired|cancelled`). A second tap on a card would therefore be an error, not an answer — the inverse of R6 (`01-target-architecture.md:13`, `02-domain-model.md:1139`).
- **The worker runs two jobs at a time per replica.** One `WorkLoop` per lane (`src/worker.py:172-180`), each `run_once` claims one and awaits it (`src/services/target/work_loop.py:557-591`, `:663-667`). `ws_lane_cap = 2` (`:69`) against `05`'s 5/3 (`05-operational-numbers.md:33`). `sender_hold_seconds = 45` (`:73`). `fn_claim_job` orders globally by `run_at` (`scripts/migrations/059_security_definer_doors.sql:96-121`).
- **One upload stalls every send.** `outbox.deliver` debits `tg_chat` and `tg_global` inside the sender's transaction (`src/services/target/outbox.py:467-482`) and then awaits the provider (`:489`); the `tg_global` row (key `''`) stays locked for the call — up to the 120 s upload budget (#1260).
- **429 is not a wait, retries never end.** `_call` raises `TelegramSendError` for 429 (`src/channels/telegram_transport.py:222`) → `mark_ambiguous` (`outbox.py:502`) → `approval_prompt` returns to pending with no cap (`RESEND_KINDS`, `:103`). `jobs.reschedule_job` (`src/services/target/jobs.py:302-336`) never reads `max_attempts`; `_run_job` backs off a flat `retry_backoff_seconds = 60` (`work_loop.py:71`, `:644-661`). Only `publish_pipeline` enforces attempts.
- **No backpressure signal.** `status_line` (`src/worker.py:230-262`) reports processed/parked/failures/fenced and nothing about ready depth or oldest-runnable age, which `01-target-architecture.md:88` requires.
- **Ratified design to honour.** R5 answered fast, work continues async (`01:12`); R6 terminal-state-first (`01:13`); T2 no cross-tenant starvation (`01:19`); H1 hundreds of concurrent pipelines (`01:24`); H2 interactions < 2 s p95 (`01:25`); H5 everything bounded, visible backpressure (`01:28`); interactive commands are single-transaction flips inline in ingress, the ack is the transaction (`02:1219`); same-fingerprint replay acknowledged without re-execution (`02:1361`); D22 no global command ceiling (`03:99`); S.1/S.2 (`04:348-350`); the `05` numbers (`05:31-52`).

## Architecture

**Where the tap lives.** The tap is a *command*, not a job: `TelegramDispatcher` grows a callback branch that resolves the chat to its workspace through `fn_resolve_binding`, the tapper to a user through `user_identities`, and hands a `Command(kind, workspace_id, actor_user_id, channel="telegram")` to `commands.execute` — the same path the web queue takes (`src/api/routes/v1.py:210` via `commands.ingest`). The webhook's existing admission on `update_id` stays the idempotency ledger; the port is not admitted twice.

**The one-transaction flip, made safe under repeat and race.** The intent executors read the row `FOR UPDATE` and decide *before* writing: a row already in the tapped state, or terminal, or with the same terminal outcome, answers with its current state (an `answered` outcome carrying `state`, `by`, `at`) and writes nothing; only a legal edge writes. The database guard stays the last line, not the first. Inside the transaction: the flip, the `post_locks` row, the `publish_pipeline` job when the action is `post`, and the outbox rows that supersede every live card for the intent across every binding of the workspace. Nothing external runs inside it.

**Answering.** After commit, `answerCallbackQuery` with the outcome text goes straight to Telegram with a 2 s timeout (it is the client's spinner, worthless late and harmless lost); the card edit — buttons replaced by the outcome line — goes through the outbox as the existing `prompt_supersede` kind, paced per chat like every other send.

**Under load.** The API runs two uvicorn workers with the pool arithmetic kept at `2 × 10` connections; the route has one timeout boundary (a delivery that cannot be admitted within budget is refused *before* admission so Telegram redelivers it, never a 500 after admission); per-workspace admission of 30 commands/min through `rate_counters` scope `ws_admission`. Whether a tap is ever queued to the worker instead of executed inline is fork F1, decided by the harness: the plan's lean is that a tiny inline transaction sized correctly needs no queue.

**The worker.** K concurrent claim-and-run tasks per lane in one process (the serialization key already makes concurrent claims safe by unique index), the per-workspace cap raised to the `05` numbers, the global rate debit committed in its own transaction before the provider call, 429 honoured as a wait, an attempt ceiling with exponential backoff and jitter and a terminal `failed`, and ready-depth plus oldest-runnable-age in the status line. Tenant fairness beyond the per-workspace cap is phase 4, flagged and not built until the harness shows starvation.

## Decision Forks

**F1 — What happens to a tap when the API is saturated.**
Context: `02:1219` rules interactive commands inline (the ack is the transaction); R5 allows the work to continue async; the owner asks that taps "register and wait to process or process in parallel". Today saturation is a 3 s pool wait then a 500.
Options: (a) **Inline, always.** Size the pool and processes, keep the transaction tiny, refuse before admission under a short budget so Telegram redelivers; no queue. (b) **Inline when cheap, queued under pressure.** Same as (a), plus a new `telegram_update` job kind (migration: `ck_jobs_kind` edit) the route falls back to when the pool wait exceeds a budget, answering the tap "Got it" at once; the worker executes the same command path. (c) **Always queued.** The route admits and enqueues; the worker flips and answers.
Lean: (a), proven by the phase 2 harness (1,000 concurrent taps across 50 workspaces, zero 5xx, p95 answer < 2 s). If the harness fails the SLO at the `05` pool arithmetic, (b) is the named fallback — the fork is re-served with the numbers.
Ratifier: owner. Status: open.

**F2 — How a repeated tap is recognised.**
Context: Telegram issues a fresh `update_id` per tap, so `command_dedup` cannot collapse two taps on one card.
Options: (a) **Read-then-decide under a row lock.** `SELECT … FOR UPDATE` on the intent; a row already in (or past) the tapped state answers with its current state and writes nothing. (b) **A per-(intent, action) dedup row** in `command_dedup` keyed on the intent rather than the update. (c) Both.
Lean: (a) — the row is the truth and the lock serialises the race; (b) adds a second ledger for a question the row already answers.
Ratifier: owner. Status: open.

**F3 — Who a tap is attributed to.**
Context: the audit trigger refuses an anonymous state change; `06`'s join path makes only *linked* Telegram users members.
Options: (a) **The tapper, resolved through `user_identities`**; an unlinked tapper is answered "Link your Telegram account in Settings first" and nothing flips. (b) **The binding's admin** when the tapper is unknown. (c) Any tapper, recorded by Telegram id only.
Lean: (a) — attribution is the audit row's whole value; (b) forges it; (c) has no `user_id` for `created_by_user_id` on the lock rows.
Ratifier: owner. Status: open.

**F4 — What the card shows after a tap.**
Context: the legacy product edited the card in place; the target tier's only edit path is supersede-then-send (`outbox.supersede_all`, `outbox.py:366-403`), whose `prompt_supersede` payload carries the superseded message ref.
Options: (a) **Edit in place**: the transport gains `editMessageReplyMarkup`/`editMessageCaption`; the supersede row edits the original message to show the outcome line and no buttons. (b) **A new message** under the old card, buttons left inert (a late tap answers per R6). (c) Delete the card and post the outcome.
Lean: (a) — one message per post, the outcome where the buttons were; the ambiguous-ref rule holds because a supersede row targets only a *known* ref.
Ratifier: owner. Status: open.

**F5 — The API's process shape.**
Options: (a) **`uvicorn --workers 2`** with `POOL_SIZE_SEAM = 10` per process, keeping `05`'s `2 × 10` ingress connections. (b) One process with a pool of 20. (c) More Railway replicas of one process each.
Lean: (a) — CPU parallelism for JSON and TLS, the connection budget unchanged; (b) leaves one event loop for everything; (c) costs money before it is shown to be needed.
Ratifier: owner. Status: open.

**F6 — Where the attempt ceiling is enforced.**
Options: (a) **In the loop**: `_run_job` reads `max_attempts`, terminalises `failed` and, for tenant kinds, writes a `notification` outbox row; backoff `1/5/15/60 min` with jitter per `05:45`. (b) **In a door**: a `fn_reschedule_job` that enforces the ceiling in SQL (a migration).
Lean: (a) — no migration, the rule lives beside the retry it bounds, and `publish_pipeline`'s own ceiling (`publish_pipeline.py:513`) is the precedent.
Ratifier: owner. Status: open.

**F7 — Worker parallelism.**
Options: (a) **K tasks per lane in one process**, each with its own pinned claim connection (interactive 8, bulk 4 to start; `05:31`'s 10/50 are the ceilings), the pool arithmetic re-verified. (b) More replicas, one job per lane each. (c) Both.
Lean: (a) — H6 says any replica runs any workspace's job, so replicas remain the scale-out lever, but a replica that runs two jobs wastes the `05` pool it already holds.
Ratifier: owner. Status: open.

**F8 — The global rate row (#1260).**
Options: (a) **Debit first, in its own committed transaction**, then call the provider; a failed send has spent one unit of pacing, which is what pacing is. (b) An in-process token bucket per replica (loses cross-replica exactness). (c) Move the provider call out of the sender's transaction but keep the debit in it (same lock, shorter).
Lean: (a).
Ratifier: owner. Status: open.

**F9 — Tenant fairness beyond the per-workspace cap.**
Options: (a) Age promotion in `fn_claim_job`'s ORDER BY. (b) A persisted per-tenant cursor (round robin). (c) **Defer**: the per-workspace cap plus the harness's starvation scenario; build (a) or (b) only when the harness shows a workspace delayed past SLO.
Lean: (c) — `04:350` already rules fairness a demonstration, not machinery, until shown otherwise.
Ratifier: owner. Status: open.

## Companion Plans

- `documentation/planning/2026-08-02-consolidated-design-plan/` — `01` §R5/R6/T2/H1/H2/H5, `02` §5 (commands are not jobs; job-kind registry), `02` §6 (`rate_counters`, outbox), `04` Phase S (S.1, S.2), `05` (the numbers). This plan builds W4 and the S.1/S.2 content the tap needs; it does not touch the publish pipeline itself.
- #1220 step 3 (the publish leg): the `post` action's `publish_pipeline` job is already enqueued by `approve`; that leg lands the worker's side of it.
- #854 (chat-inbound commands): closed by phase 1 for taps; typed commands in chat stay out of scope.
- #1260 (rate row held during upload): closed by phase 3, F8.
- #1235 (reaper leg for `cancel_requested`): unchanged; a tap on a cancelling card is refused by name as today.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Telegram redelivery storms when the route refuses under load | Refusals multiply load | Refuse *before* admission with a 503 only when the pool wait exceeds the budget; Telegram backs off; the harness measures the redelivery rate. |
| A tap's transaction grows (bindings fan-out, locks, job) and stops being "tiny" | p95 misses 2 s | Everything external stays outside the transaction; the harness pins the transaction's statement count and p95. |
| Cards fan out to several bindings; a tap in one chat must retire the card in the others | Two chats act on one card | The flip supersedes across **every** binding of the workspace (loop `push_bindings`), inside the same transaction. |
| `answerCallbackQuery` lost or late | The spinner ends by itself at ~30 s | Sent after commit with a 2 s timeout, never awaited by the transaction; the card edit carries the outcome regardless. |
| K concurrent runs change the pool arithmetic | Connection exhaustion | `Σ(replica × (pool + overflow)) = 50` re-verified in the phase 3 checklist; K pinned claim connections counted. |
| Attempt ceiling terminalises work that used to retry forever | Silent loss | `failed` writes a notification row for tenant kinds and a log line for system kinds; the reaper already alarms parked intents. |
| The callback token format changes | Old cards' buttons stop working | Format stays `v1:` and the parser refuses unknown versions with a friendly answer. |

## Complexity and Sequencing

| Phase | Doc | Size | Depends on | Parallel with |
|---|---|---|---|---|
| 1 The tap | `01_the-tap.md` | L | — | — |
| 2 The API under load | `02_api-under-load.md` | M | 1 (the harness taps) | 3 |
| 3 Worker throughput | `03_worker-throughput.md` | M | — | 2 |
| 4 Tenant fairness | `04_tenant-fairness.md` | S (flagged) | 2 (the harness) | — |

Critical path: 1 → 2. Phase 3 can land before or beside 2. Phase 4 waits for evidence.

## Implementation Plan

### Dependencies

None outside this repo. Migration 072 is needed only if F1 locks (b) (`telegram_update` job kind).

### Blocks

#1220 step 3 (the `post` button must reach `publish_pipeline`); FC-7 §3's "Telegram works after" end state.

### Steps

1. Ratify the forks (F1–F9) one at a time; lock them in this document with the ruling text.
2. Build phase 1 per `01_the-tap.md`, tests first, one PR, two-lens review, admin merge.
3. Build phase 3 per `03_worker-throughput.md` (independent of 1), same discipline.
4. Build phase 2 per `02_api-under-load.md`, whose harness is the proof for F1 and the gate for T2/H2.
5. Read the harness; re-serve F1 and F9 if the numbers demand; phase 4 only on evidence.

## Test Plan

Each phase carries its own tests-first list. The epic's own proof is the phase 2 harness scenario set: `taps_1000_across_50_workspaces`, `double_tap_one_card`, `taps_across_many_cards`, `one_slow_chat`, run against the Docker `postgres:15` on `localhost:65433` with a fake Telegram.

## Verification Checklist

- [ ] Every fork F1–F9 reads `Status: locked` with a ratifier and the ruling text.
- [ ] Phase 1 merged: `pytest tests/scripts/test_w4_tap_gate.py` green; a tap in the owner's bound group flips the card and answers within 2 s.
- [ ] Phase 3 merged: the status line prints `ready=` and `oldest_age=` per lane; the jobs-lease gate covers the attempt ceiling.
- [ ] Phase 2 merged: the harness report is committed under `tests/scripts/load/` with p95 < 2 s and zero 5xx for `taps_1000_across_50_workspaces`.

## What NOT To Do

- Do not process the tap in the request *before* admission, and do not answer 200 for an update whose command was not executed or queued — that loses the tap.
- Do not introduce a generic `run_command` job kind (`02:1219`).
- Do not add a global command ceiling (D22).
- Do not edit a card on an ambiguous message ref; supersede only known refs.
- Do not build fairness machinery before the harness shows starvation (`04:350`).

## Context

area: target ingress, worker, outbox · effort: XL across four phases · risk: high (the tap is the product's main surface) · priority: P0 — the buttons the product shows do nothing today.
