---
paths:
  - "src/worker.py"
  - "src/services/target/scheduler*"
  - "src/services/target/category_mix.py"
  - "src/services/target/content_runway.py"
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
  email provider parks `send_email`; `reencrypt_credentials` has no executor
  at all (`work_loop.UNBUILT_KINDS`), and `retention_sweep` runs one `05`
  retention class only, `rate_counters` (7 d,
  `scheduler.execute_retention_sweep`).
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
- A tick (15 s, at most 500 inserts) is one call to the `fn_clock_tick` door:
  recurring system singletons, due accounts → `plan_slot` jobs with the slot
  cursor (`ig_accounts.next_slot_at`) advanced in the same statement, due
  credential refreshes, due source syncs, weekly reauth prompts. The five legs
  share one transaction and one insert budget.
- Slots are the account's effective `posts_per_day` spread evenly across its
  posting hours in its `tz` — the account's override, else the workspace's
  (`fn_next_slot`, 059). A paused or inactive workspace mints no `plan_slot`.
- Every scheduling decision reads the DATABASE clock (`now()` inside the door);
  the loop paces on `asyncio.sleep`. Do not pass a host timestamp into a door.
- The recurring kinds this worker asks for are `compose`'s (`worker.py:314`):
  `reap_expired` and `reconcile_ambiguous` every 60 s, `alert_stranded_sources`
  every 6 h, `retention_sweep` every hour (5,000-row batches until one comes
  back short or 5 s is spent), `reap_transit_assets` every 6 h when a transit
  store exists, and `activation_nudge_sweep` daily only when it is live: its
  switch (`TARGET_ACTIVATION_NUDGE_ENABLED`, default off), an email provider
  and a web origin (#1481). The reaper's 60 s and its 500-row budget
  (`WorkerConfig.reap_limit`, the sweep's total across every leg) are `05`'s,
  pinned by `tests/src/test_worker.py`: an expired lease holds its
  serialization key until the next sweep.
  The fleet monitor's worker-down threshold (`DEFAULT_WORKER_STALE_S` in
  `scripts/scheduling_monitor.py`) rests on the fastest of these beats, today
  60 s: slow every 60 s kind and that threshold must rise with them —
  `tests/src/test_worker.py` fails until it does.

## Folder selection (`scheduler.execute_plan_slot` + `category_mix.weights`)

`plan_slot` mints at most one intent for its slot: the insert is
`ON CONFLICT (workspace_id, ig_account_id, schedule_slot_at) WHERE origin = 'cadence' DO NOTHING`
(`scheduler.py:436`), so a duplicate job mints nothing. The predicate is the slot key's own:
`uq_intent_slot` is cadence-only (089), so a planned story (`origin = 'planned'`) never absorbs
a slot. Keep the predicate: without it a bare `ON CONFLICT` finds no arbiter in the partial
index and every cadence mint raises.

The draw is weighted over the CONNECTED FOLDERS that have eligible media —
explicit ratios; automatic folders in proportion to their files, together never
more than the smallest explicit weight; Off (ratio 0) never
(`category_mix.py:121`). Within the drawn folder: never-posted files first in
the row id's shuffled order, then least-recently-posted (`scheduler.py:375`).
Eligible means `available`, not already live for this account, and not under a
live `post_locks` row. There is no pool behind the weighted set: when nothing
is eligible the slot lapses and the workspace is told at most once per 24 h
(`_notice_no_media`, `:205`).

The rule and the folders' weights are ONE per-account read, `category_mix.pool`
(the rule is `category_mix.ELIGIBLE_SQL`), which the draw and the Overview's
days-left figure share (#1478), so the two cannot disagree. The mix card's
"Posts about" share (`category_mix.mix_view`) is not that read: it weighs each
folder by its `state = 'available'` files across the workspace, a
workspace-level approximation of what any one account draws. A mint that
leaves the account with fewer than `WorkerConfig.low_runway_days` (7) days of
eligible content tells the workspace once, through the same push bindings, and
the next drop is told only after the account climbs back to 8 days,
`content_runway.REARM_MARGIN_DAYS` (1) above that level
(`content_runway.after_mint`; the latch is the account's `low_content_notice` /
`low_content_rearmed` audit rows, read and written under the account's
`runway:` advisory lock). The Overview marks an account low at the same
`low_runway_days`: its read takes the level from `WorkerConfig`. That notice
never decides the slot: it is written in a savepoint after the mint, and its
verdict is not carried back on `SlotOutcome`, so a slot that minted finalizes
`succeeded` even when the workspace has no push binding (the latch row records
`told: 0`, and the warning is logged once per crossing) or the notice could not
be written. Only the empty library's notice, where nothing was minted, parks a
`plan_slot` job `review_required`.

## Jobs (`jobs.py`, `work_loop.py`)

- Claim is the `fn_claim_job` door (`FOR UPDATE SKIP LOCKED`, the lease token
  minted and `attempts` incremented at claim). `uq_jobs_serialized_lease` makes
  two live leases on one `serialization_key` impossible for every writer;
  `claim_job` retries the race it loses.
- A lease is 90 s, extended by the heartbeat every 20 s. `finalize_job` is a
  lease-token CAS in the job's own domain transaction and does not commit; a
  stale owner matches zero rows (`JobFenced`). Expired leases are re-readied by
  `fn_reaper_sweep`, not here — with one exception: the tick returns an
  expired lease of its OWN recurring singletons to `ready` before its mint
  guard reads it (084), because the reaper is one of those singletons and
  cannot revive itself. Every other kind's expired lease is the reaper's.
- A deadline ends a job two ways. A job that runs and fails past its budget is
  ended by the worker (`jobs.budget_exhausted`), with the tenant notice; a
  `ready` job past its `deadline_at` is ended `failed` by the reaper, which
  merges `ended: deadline` into its payload and re-arms a sync kind's source,
  but sends no notice, and only for the kinds a sweep re-mints (086, #1429:
  the list is in the leg, and the lease gate pins every kind to one side). A
  deferral (`reschedule_job` with the attempt restored: a park, a pacing
  wait) moves the deadline with `run_at`, so the job keeps its slack; a
  retryable failure keeps its deadline. A new job kind must be classified in
  that pin.
- Per-workspace lane caps (interactive 5, bulk 3) are the claim's, and they
  bind only across several replicas: `fn_claim_job` counts the deployment's
  leases, and one process runs fewer tasks per lane (3 interactive, 2 bulk)
  than the caps. So on a single worker one workspace can hold a whole lane
  (`work_loop.py:70-79`; #1428 pins the relation).
- An executor that waits on a provider is marked `own_transactions`
  (`work_loop.py:186`): it runs with no job session open and finalizes in a
  short transaction afterwards. So is `retention_sweep`, whose batches each
  commit on their own.
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
- **Only the cadence spends the daily cap.** A planned story
  (`origin = 'planned'`) neither spends it nor waits on a spent day: the flip
  stamps its `cap_consumed_on` without a debit, and the refunds return nothing
  for it. Every write to `daily_post_counts` lives in `publish_cap.py` (the
  flip, the manual post, the refunds), where they all ask one predicate;
  `tests/src/services/target/test_publish_cap.py` fails on a new SQL string
  literal under `src/` that writes the table by its bare name (it does not
  see ORM or Core writes, a built or schema-qualified name, or a write
  outside `src/`), because a writer that forgot the rule would drift the
  day's count.
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
