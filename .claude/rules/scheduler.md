---
paths:
  - "src/worker.py"
  - "src/services/target/scheduler*"
  - "src/services/target/category_mix.py"
  - "src/services/target/work_loop.py"
  - "src/services/target/jobs.py"
  - "src/services/target/publish_pipeline.py"
  - "src/services/target/publish_cap.py"
  - "src/services/target/reconciler.py"
---

# The worker: the clock, the jobs, the publish pipeline

`python -m src.main` dispatches to `src.worker`, the one composition root
(starting it is in `CLAUDE.md`'s safety block). The legacy scheduler loop was
retired in the tear-out (#1216, September 2026). To READ what the worker did,
use the ledger verbs — `storydump jobs`, `storydump floating`,
`storydump account <handle>`, `storydump story <intent_id>`
(`documentation/operations/reading-the-ledger.md`); this page is what the code
does.

## Composition (`src/worker.py`)

- `main` refuses to boot without `TARGET_DATABASE_URL`, or without a credential
  key ring that loads (`oauth_states.RingUnavailable`, #1401) — exit 2 for both,
  before anything connects.
- `compose` (`:262`) builds the whole graph without touching the network. A seam
  the deployment lacks **parks** its kinds with the reason named, rather than
  running them against a fake: no `TARGET_TELEGRAM_BOT_TOKEN` (or a dead or
  wrong-bot token at the startup probe, `:370`) parks `deliver_outbox`; no
  `CLOUDINARY_*` trio parks `publish_pipeline` and `reap_transit_assets`; no
  email provider parks `send_email`; `retention_sweep` and
  `reencrypt_credentials` have no executor at all (`work_loop.UNBUILT_KINDS`).
  A claimed job of a parked kind is rescheduled alive, attempt restored, every
  `park_seconds` (900 s) — never finalized dead (`work_loop.py:875`).
- `run` (`:618`) binds the health endpoint before the first database connection,
  then starts the clock, the lease heartbeat, K claim loops per lane
  (interactive 3, bulk 2; `TARGET_WORKER_*_CONCURRENCY`), the sender sweeper
  (3 s), the prompt sweeper (5 s) and a status line every 60 s. `supervise`
  (`:545`) ends the worker loudly when any of them dies.
- The numbers are `WorkerConfig`'s defaults (`work_loop.py:62`), passed as
  parameters to the doors. Do not hardcode one in a service or a door body.

## The clock (`scheduler.py`)

- One clock per deployment, elected by a session-scoped advisory lock
  (`pg_try_advisory_lock`, `CLOCK_ELECTION_KEY`, `:89`-`:134`). Losing the
  election is the mechanism working, not a failure. The clock mints jobs; it
  executes nothing.
- A tick (15 s, at most 500 inserts) is one call to the `fn_clock_tick` door
  (063's definition): recurring system singletons, due accounts → `plan_slot`
  jobs with the slot cursor (`ig_accounts.next_slot_at`) advanced in the same
  statement, due credential refreshes, due source syncs, weekly reauth prompts.
  The five legs share one transaction and one insert budget.
- Slots are the account's effective `posts_per_day` spread evenly across its
  posting hours in its `tz` — the account's override, else the workspace's
  (`fn_next_slot`, 059). A paused or inactive workspace mints no `plan_slot`.
- Every scheduling decision reads the DATABASE clock (`now()` inside the door);
  the loop paces on `asyncio.sleep`. Do not pass a host timestamp into a door.
- The recurring kinds this worker asks for are `compose`'s (`worker.py:294`):
  `reap_expired` and `alert_stranded_sources` every 6 h, `reconcile_ambiguous`
  every 60 s, `reap_transit_assets` every 6 h when a transit store exists.
  The 60 s beat is what the fleet monitor's worker-down threshold rests on
  (`DEFAULT_WORKER_STALE_S` in `scripts/scheduling_monitor.py`): retire or slow
  `reconcile_ambiguous` and that threshold must rise with it —
  `tests/src/test_worker.py` fails until it does.

## Folder selection (`scheduler.execute_plan_slot` + `category_mix.weights`)

`plan_slot` mints at most one intent for its slot: the insert is
`ON CONFLICT (workspace_id, ig_account_id, schedule_slot_at) DO NOTHING`
(`scheduler.py:492`), so a duplicate job mints nothing.

The draw is weighted over the CONNECTED FOLDERS that have eligible media —
explicit ratios; automatic folders in proportion to their files, together never
more than the smallest explicit weight; Off (ratio 0) never
(`category_mix.py:108`). Within the drawn folder: never-posted files first in
the row id's shuffled order, then least-recently-posted (`scheduler.py:392`).
Eligible means `available`, not already live for this account, and not under a
live `post_locks` row. There is no pool behind the weighted set: when nothing
is eligible the slot lapses and the workspace is told at most once per 24 h
(`_notice_no_media`, `:215`).

## Jobs (`jobs.py`, `work_loop.py`)

- Claim is the `fn_claim_job` door (`FOR UPDATE SKIP LOCKED`, the lease token
  minted and `attempts` incremented at claim). `uq_jobs_serialized_lease` makes
  two live leases on one `serialization_key` impossible for every writer;
  `claim_job` retries the race it loses.
- A lease is 90 s, extended by the heartbeat every 20 s. `finalize_job` is a
  lease-token CAS in the job's own domain transaction and does not commit; a
  stale owner matches zero rows (`JobFenced`). Expired leases are re-readied by
  `fn_reaper_sweep`, not here.
- Per-workspace lane caps (interactive 5, bulk 3) are the claim's, so one
  workspace cannot own a lane.
- An executor that waits on a provider is marked `own_transactions`
  (`work_loop.py:186`): it runs with no job session open and finalizes in a
  short transaction afterwards.
- A new kind needs its name in `ck_jobs_kind` — and, for a system kind, in
  `ck_jobs_system_kinds`, which is a biconditional (065 is the precedent) — an
  entry in `build_registry` (`work_loop.py:232`), and, if the clock mints it,
  the recurring set. A kind missing from the CHECK aborts every leg of the tick
  (#1074).

## The publish pipeline (`publish_pipeline.py`)

- `approve` flips `awaiting_approval → approved` and enqueues the
  `publish_pipeline` job, keyed `ig:<provider_account_ref>`
  (`command_executors.py:431`). The dry-run decision travels in the job payload.
- The executor is a transaction-per-checkpoint ladder over
  `post_intents.publish_step`: the flip to `publishing` (one CTE that debits
  `daily_post_counts` and takes `uq_publish_exclusive`, `publish_cap.py`),
  transit upload, container permit and create, status poll, publish permit and
  publish, then ONE terminal transaction (`posted`, the counters, the recent
  lock, `finalize_job`). Each permit is a `provider_operations` row committed
  BEFORE the provider call; every checkpoint re-asserts the lease.
- **A story does not wait inside the slot.** Any wait between attempts steps the
  intent back `publishing → approved` with its step, transit asset and debit
  intact, and writes a `float_wait` audit row (class, rung, next run). That is
  what `storydump floating` lists.
- A lost answer from `meta.publish` is typed, not parsed: the intent parks
  `publishing_ambiguous` with zero retries, and `reconcile_ambiguous` resolves
  it from the container's status (`PUBLISHED` → posted, `ERROR`/`EXPIRED` →
  failed) or, its ladder spent, parks it `review_required` for the workspace to
  resolve on its card.
- Unexpected exceptions propagate, so the lease expires and the reaper
  re-readies the job. Do not add a blanket `except` to the ladder.
