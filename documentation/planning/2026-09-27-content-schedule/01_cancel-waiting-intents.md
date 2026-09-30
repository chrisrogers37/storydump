---
title: "[plan] Phase 1: lease recovery and the reaper's cadence (1a, #1329), then cancel-flagged waiting intents end as cancelled (1b, #1235) — content schedule"
type: plan
status: draft
owner: astrid
created: 2026-09-27
tags: [storydump, content-schedule, reaper, clock, jobs, lease-recovery, cancel, issue-1329, issue-1235, issue-1413]
repos: storydump
---

# Phase 1 — Lease recovery and the reaper's cadence (1a, #1329), then cancel-flagged waiting intents end as `cancelled` (1b, #1235)

## Summary

Part of #1413 (the prerequisite track). Size: M overall (1a S, 1b M). Two PRs, in this order:

- **1a (#1329, closes it).** The job system recovers when a worker dies holding the reaper's own
  lease, and the reaper runs at its designed cadence: every 60 seconds with 500 rows a sweep
  (`05-operational-numbers.md:65`), instead of the as-built 6 hours and 200.
- **1b (#1235, closes it).** The reaper gains the leg that turns a cancel-flagged intent in
  `scheduled`, `prompt_pending`, `awaiting_approval` or `approved` into `cancelled`. Today
  `cancel` and `disable_account` both leave such rows flagged until the approval TTL expires them
  as `expired`.

1a comes first because everything after it rests on the reaper. The reaper's first leg is the only
lease recovery the job system has, for every job kind. Today one worker death while holding
`reap_expired`'s lease ends that recovery for good: every other kind's dead lease then blocks its
serialization key indefinitely (deliveries, publishing, slot planning, credential refresh). 1b's
cancel leg rides the same reaper, and phase 1 takes it from 4 runs a day to 1,440, which widens
the window for exactly that death. Phase 3 and phase 5 wait for both (overview, F3).

## Evidence

**The wedge (1a), read at `f228f4f87`:**

- Lease recovery has one home. The reaper's first leg re-readies every expired lease
  (`076:29-31`), and `.claude/rules/scheduler.md:93-94` says so: "Expired leases are re-readied by
  `fn_reaper_sweep`, not here." The claim door takes only `ready` rows (`059:102`).
- A recurring singleton's serialization key is its kind (`083:83`, "system singletons key on their
  kind"). The tick mints one only when no row of the kind is `ready` or `leased` (`083:73-74`),
  and it never reads `locked_until`.
- The claim door refuses a `ready` job whose key is held by any `leased` row, again without reading
  `locked_until` (`059:103-104`). `uq_jobs_serialized_lease` allows one `leased` row per key
  (`056:123`).
- The tick's insert has no conflict clause, and no unique index covers `ready` rows (`083:82-86`,
  `056:119-123`), so a second `ready` singleton inserts without conflict.
- `svc_clock`, which owns `fn_clock_tick`, holds `SELECT, INSERT` on `jobs` and no UPDATE
  (`057:140`). Its policy is `FOR ALL ... USING (true)` (`058:221`).
- A job's `deadline_at` is read only on the failure path (`jobs.budget_exhausted`, called at
  `work_loop.py:968`), so a revived singleton past its old deadline still runs; if that run fails,
  it ends, and the next tick mints a fresh one.

**Reproduced on Postgres 15.19**, in a scratch database dropped afterwards. The objects were copied
verbatim from `f228f4f87`: `jobs` and its indexes (`056:81-123`), `fn_claim_job` (`059:96-120`),
the reaper's first leg (`076:29-31`), and the tick's recurring leg (`083:66-89`, with legs 2–5
left out because they mint no singletons). The scenario: a worker claims `reap_expired` and a
tenant delivery on key `tg:binding-1`, with a second delivery queued on that key, and then the
worker dies and both leases lapse.

| Tick variant | What the reproduction showed |
|---|---|
| **Today** (`083:73-74`) | Three ticks mint nothing. The claim door hands out nothing in either lane: the queued delivery is blocked behind the dead lease on its key. Nothing recovers. |
| **Widened guard** (`state = 'ready' OR (state = 'leased' AND locked_until >= now())`) | The first tick mints a second `reap_expired`, and the claim door refuses it, because the dead lease still holds the key. The next tick mints nothing, because a `ready` row now exists. Still wedged, now with an extra row. Forcing that lease fails with `duplicate key value violates unique constraint "uq_jobs_serialized_lease"`. |
| **Revive the kind's own expired lease, then the unchanged guard** | The tick mints nothing, and the dead singleton is `ready` again. A new worker claims it. The reaper's first leg frees the tenant's dead lease, and the queued delivery is claimed in key order. No expired lease remains, and there is still exactly one `reap_expired` row. |

Not exercised in the scratch run: the fix under the real `svc_clock` role (no service roles were
created, because they are cluster-scoped), and the Python loop around the doors. The 1a tests
cover both.

**The cadence (1a):** `reap_expired` is minted every 6 hours (`src/worker.py:314-316`) with a
200-row total budget (`WorkerConfig.reap_limit`, `src/services/target/work_loop.py:95`). The design
is every 60 seconds and 500 (`05-operational-numbers.md:65`). `reconcile_ambiguous` already runs
every 60 seconds (`.claude/rules/scheduler.md:61-63`).

**The cancel leg (1b):**

- `command_executors.py:783`: `cancel` sets `cancel_requested` and nothing else.
- The only consumer that ends a flagged row is publish admission, for `approved` rows only
  (`publish_pipeline.py:519`, `:659`).
- All four edges are already seeded: `055:292-298`.
- The function to extend is `fn_reaper_sweep`, whose current body at `f228f4f87` is `076:24-58`
  (SECURITY DEFINER, owned by `svc_maintenance`). It has six legs: job leases, cadence expiry, the
  approval TTL, expired locks, invitations and onboarding sessions.
- #1233's belt stays as it is: the Queue shows a flagged card as *Cancelling*, and the card verbs
  refuse it.
- At the as-built cadence, a cancelled planned row would stay live for up to 6 hours, and longer
  whenever earlier legs spend the sweep's 200-row budget. For as long as it stays live,
  `uq_intent_live_subject` refuses scheduling the same item for the same account (overview, G5).
  After 1a it is about a minute.

## Implementation Plan

### Dependencies
The owner's approval of the DDL: for 1a, `fn_clock_tick` and one column grant; for 1b, a
function-body change (overview, Owner decisions). 1b lands after 1a.

### Blocks
Phase 3, which redefines `fn_reaper_sweep` and starts from the body 1b leaves, and phase 5
(cancelling a planned item). 1a closes #1329, and 1b closes #1235.

### Steps

**1a — lease recovery survives the reaper's own death, and the reaper runs every minute (#1329).
One PR, with `Closes #1329` in a branch commit (a squash merge closes issues from commit text, not
from the PR body).**

1. A new migration, taking the next free number, redefines `fn_clock_tick` from its current live
   definition (`083` at `f228f4f87`; check it against `pg_get_functiondef` on a Neon branch). In
   the recurring leg, before the existing guard, it returns this kind's expired lease to `ready`:

   ```sql
   UPDATE jobs SET state = 'ready', locked_by = NULL, lease_token = NULL, locked_until = NULL
    WHERE kind = k AND state = 'leased' AND locked_until < now();
   ```

   The guard itself does not change, so a second singleton is never minted. The same migration
   adds `GRANT UPDATE (state, locked_by, lease_token, locked_until) ON jobs TO svc_clock;`; the
   policy `p_clock_jobs` already allows the update.
2. Restore the designed cadence and budget. This is configuration, not DDL: set
   `"reap_expired": 60.0` in the worker's recurring set (`src/worker.py:316`) and
   `WorkerConfig.reap_limit = 500` (`work_loop.py:95`). Pin both to the `05` row with a test, the
   way `tests/src/test_worker.py` already pins the approval TTL. The same limit also bounds
   `sweep_settled_cards` (`work_loop.py:307`). That sweep only enqueues supersessions in the
   database, and the outbox poller sends the Telegram edits at its own rate.
3. Bring the words in line with the code: the `05-operational-numbers.md:65` row, the
   `railway.toml:25-27` comment (#1329's step 3), and `.claude/rules/scheduler.md`. That rule file
   needs its cadence line (`:61-63`) updated, and its lease line (`:93-94`) must name the one
   exception: the clock revives its own singletons, because the reaper cannot revive itself.
4. Update the advertised DDL for `fn_clock_tick` in the consolidated plan, in step, and add a
   `CHANGELOG.md` entry.
5. Deploy in a quiet window. The first runs after the deploy carry the backlog built up under the
   6-hour cadence (overview, Risks).

**1b — cancel-flagged waiting intents end as `cancelled` (#1235). One PR, after 1a.**

6. A new migration, taking the next free number, runs `CREATE OR REPLACE FUNCTION fn_reaper_sweep`,
   copied verbatim from the current live definition. That is the newest migration that defines it
   at build time, checked against `pg_get_functiondef` on a Neon branch of production; at
   `f228f4f87` it is `076`. The migration adds the leg #1235 specifies:
   `UPDATE post_intents SET state = 'cancelled' WHERE cancel_requested AND state IN
   ('scheduled','prompt_pending','awaiting_approval','approved')`. The leg is bounded by the
   remaining `rem` budget and performs the same outbox supersession as the expiry leg.
7. `approved` rows are also cancelled at publish admission (`publish_pipeline.py:519`). Keep both
   paths: each `UPDATE` is guarded on state, and a row already `cancelled` is frozen, so the race
   is benign. A test pins that.
8. Update the consolidated plan's advertised DDL (`07`) in the same PR, as #1235 asks, so the
   replay gate diffs the two, and add a `CHANGELOG.md` entry.

## Test Plan

**1a:**

- The wedge, as reproduced above, run through the real `fn_clock_tick` under `svc_clock`, so that
  the new grant is exercised. The clock gate suite (`tests/scripts/test_scheduler_clock_gate.py`)
  runs the tick with the service roles. After a dead `reap_expired` lease, one tick leaves the row
  `ready` with no second row; it can be claimed, and the reaper then frees every other expired
  lease.
- A live lease is never touched: a `reap_expired` row whose lease is still in the future stays
  leased through a tick.
- The guard still never mints a second singleton while a `ready` or leased row exists.
- The cadence pin: `reap_expired` runs every 60 seconds and `reap_limit` is 500, both pinned to
  the `05` row.
- Revert check: without the revive statement, the wedge test goes red.

**1b:**

- For each of the four states, a flagged row ends `cancelled` after one sweep, and its card loses
  its buttons on the next settled-card sweep (`prompts.sweep_settled_cards`, called from
  `work_loop.py` after the reap).
- An unflagged row in each state is untouched.
- The budget holds: with `p_lim = n`, at most `n` rows move across all legs together.
- Every existing leg still does its job: a leased job is released, a past-due cadence row
  expires, a TTL-expired card expires, an expired lock is deleted, an invitation expires, and an
  expired onboarding session is deleted.
- Latency: a row flagged just after a sweep is `cancelled` by the next one, about a minute later.
- Revert check: every new assertion fails when the new leg is removed.

## Verification Checklist

- [ ] 1a: the clock gate suite passes, including the wedge test.
- [ ] 1a: on a Neon branch (never production), set an expired lease on a `reap_expired` row. It is
      `ready` after one tick, and `succeeded` after the next run, with no second `reap_expired` row.
- [ ] 1b: the reaper and door suites pass.
- [ ] 1b: on a Neon branch with the worker running, flag an `awaiting_approval` intent. Within about
      a minute, `SELECT state FROM post_intents WHERE id = '<id>'` returns `cancelled`, and
      `audit_events` holds the transition with actor `reaper`.
- [ ] Both: the migration replay and advertised-DDL gate passes.

## What NOT To Do

- Don't widen the tick's guard to mint around a dead lease. The second singleton can never be
  claimed while the dead lease holds the key, as the reproduction above shows.
- Don't make the claim door skip expired leases in its key check. The lease it would then grant
  collides with the dead one on `uq_jobs_serialized_lease`, as the forced lease above shows.
- Don't revive other kinds' leases in the tick. The reaper stays their one home; the clock only
  revives its own singletons, because the reaper cannot revive itself.
- Don't rebuild either function from memory, from an older migration, or from a body pinned in this
  plan. Copy the live definition at build time.
- Don't have the `cancel` command write `cancelled` itself. The user never writes a terminal state
  (`command_executors.py:37`).

## Context
- Source skill: forge · Area: `scripts/migrations/` (`fn_clock_tick`, `fn_reaper_sweep`), `src/worker.py`, `src/services/target/work_loop.py`, `.claude/rules/scheduler.md` · Effort: M (1a S, 1b M) · Risk: Medium · Priority: High
