---
paths:
  - "src/worker.py"
  - "src/services/target/scheduler.py"
  - "src/services/target/category_mix.py"
  - "src/services/target/content_runway.py"
  - "src/services/target/upcoming.py"
  - "src/services/target/work_loop.py"
  - "src/services/target/jobs.py"
  - "src/services/target/publish_pipeline.py"
  - "src/services/target/publish_cap.py"
  - "src/services/target/reconciler.py"
---

# The worker: the clock, the jobs, the publish pipeline

`python -m src.main` dispatches to `src.worker`, the one composition root
(starting it is in `CLAUDE.md`'s safety block). To READ what the worker did,
use the ledger verbs — `storydump jobs`, `storydump floating`,
`storydump account <handle>`, `storydump story <intent_id>`
(`AGENTS.md` › The `storydump` CLI); this page is what the code does.

## Composition (`src/worker.py`)

- `main` refuses to boot without `TARGET_DATABASE_URL`, with one that cannot be
  read (`unit_of_work.engine_url_from_env`) or names a pooled host or no host
  (`unit_of_work.engine_url_refusal`), or without a credential key ring that
  loads (`oauth_states.RingUnavailable`)
  — exit 2 for all of them, before anything connects.
- `compose` (`:343`) builds the whole graph without touching the network. A seam
  the deployment lacks **parks** its kinds with the reason named, rather than
  running them against a fake: no `TARGET_TELEGRAM_BOT_TOKEN` (or a dead or
  wrong-bot token at the startup probe, `apply_transport_probe`, `:508`) parks `deliver_outbox`; no
  `CLOUDINARY_*` trio parks `publish_pipeline` and `reap_transit_assets`; no
  email provider parks `send_email`; no `PAST_POSTS_HMAC_KEYS` ring parks
  `meta_deletion_cleanup` (without a transit store it runs, and counts an
  upload still owed as unresolved); no mix conversion evidence parks
  `convert_source_mix`; a past-posts source that is not armed (no
  capability, or no past-posts client) parks `reconcile_past_posts_accounts`
  and `prepare_past_post`;
  `reencrypt_credentials` has no executor at all
  (`work_loop.UNBUILT_KINDS`), and `retention_sweep` runs one
  retention class only, `rate_counters` (7 d,
  `scheduler.execute_retention_sweep`).
  A claimed job of a parked kind is rescheduled alive, attempt restored, every
  `park_seconds` (900 s) — never finalized dead (`_run_parked`, `work_loop.py:1265`).
- `run` (`:809`) binds the health endpoint before the first database connection,
  then starts the clock, the lease heartbeat, K claim loops per lane
  (interactive 2, bulk 2, media 1; `TARGET_WORKER_*_CONCURRENCY`), the sender sweeper
  (3 s), the prompt sweeper (5 s) and a status line every 60 s. `supervise`
  (`:743`) ends the worker loudly when any of them dies.
- A task that dies is `supervise`'s; an event loop that stops turning is the
  watchdog thread's (`loop_watchdog.py`, armed by `run` for the whole run): exit
  3 with every thread's stack after `loop_stall_seconds` (300 s). It never acts
  on a wait, so bound a wait where it is made.
- The numbers are `WorkerConfig`'s defaults (`work_loop.py:73`), passed as
  parameters to the doors. Do not hardcode one in a service or a door body.

## The clock (`scheduler.py`)

- One clock per deployment, elected by a session-scoped advisory lock
  (`pg_try_advisory_lock`, `CLOCK_ELECTION_KEY`, `:94`-`:184`). Losing the
  election is the mechanism working, not a failure. The clock mints jobs; it
  executes nothing.
- The elected clock holds the lock ONCE. The lock is re-entrant, so a tick
  confirms a lock its own session already holds (its `pg_locks` row,
  `CLOCK_ELECTION_SQL`) rather than taking it again; a re-take per tick
  stacked a hold per tick, and the one unlock at shutdown handed nothing over.
  Do not turn the election back into a bare `pg_try_advisory_lock` per tick.
- The election connection must BE the session. Through a transaction pooler
  (Neon's `-pooler` host, PgBouncer in transaction mode) the server session
  outlives the worker and can keep the lock, and no successor elects: `main`
  refuses a pooled `TARGET_DATABASE_URL`. Today that connection also sits in
  one open transaction for the worker's life (SQLAlchemy begins one at its
  first statement, the role sample, and nothing commits it). That is current
  behaviour, not a rule: behind a direct host it changes nothing, and behind a
  pooler the name rule cannot see, it is what pins the election to one server
  session while the worker runs.
- A tick (15 s, at most 500 inserts) is one call to the `fn_clock_tick` door:
  recurring system singletons, due accounts → `plan_slot` jobs with the slot
  cursor (`ig_accounts.next_slot_at`) advanced in the same statement, due
  credential refreshes, due source syncs, weekly reauth prompts. The five legs
  share one transaction and one insert budget.
- Slots are the account's effective `posts_per_day` spread evenly across its
  posting hours in its `tz` — the account's override, else the workspace's
  (`fn_next_slot`, `src/models/target/ddl/04_functions.sql:155`). A paused or inactive workspace mints no `plan_slot`.
  The calendar's predicted slots are that function walked forward
  (`upcoming.py`), and its walk may start ahead of the cursor: it relies on
  the function answering ANY instant with a slot of one daily grid, a grid the
  settings alone fix. A change that makes a slot depend on the slots before it
  breaks that read.
- Every scheduling decision reads the DATABASE clock (`now()` inside the door);
  the loop paces on `asyncio.sleep`. Do not pass a host timestamp into a door.
- The recurring kinds this worker asks for are `compose`'s (`worker.py:415`):
  `reap_expired`, `reconcile_ambiguous` and `past_posts_lifecycle_sweep` (it mints
  the deletion-cleanup jobs a Meta deletion receipt is owed, which needs no
  activation, and, only with the activation evidence and an armed past-posts source, at
  most 5 `reconcile_past_posts_accounts` jobs, one per workspace per run digest)
  every 60 s, `alert_stranded_sources`
  every 6 h, `retention_sweep` every hour (5,000-row batches until one comes
  back short or 5 s is spent), `reap_transit_assets` every 6 h when a transit
  store exists, and `activation_nudge_sweep` daily only when it is live: its
  switch (`TARGET_ACTIVATION_NUDGE_ENABLED`, default off), an email provider
  and a web origin, at most `TARGET_ACTIVATION_NUDGE_LIMIT` emails a
  run (3 when unset). The reaper's 60 s and its 500-row budget
  (`WorkerConfig.reap_limit`, the sweep's total across every leg) are
  pinned by `tests/src/test_worker.py`: an expired lease holds its
  serialization key until the next sweep.
  The worker-down threshold (`DEFAULT_WORKER_STALE_S`, 600 s, in
  `storydump_cli/health_classify/scheduling.py`) rests on the fastest of these beats, today
  60 s: slow every 60 s kind and that threshold must rise with them —
  `tests/src/test_worker.py` fails until it does.

## Folder selection (`scheduler.execute_plan_slot` + `category_mix.weights`)

`plan_slot` mints at most one intent for its slot: the insert is
`ON CONFLICT (workspace_id, ig_account_id, schedule_slot_at) WHERE origin = 'cadence' DO NOTHING`
(`scheduler.py:512`), so a duplicate job mints nothing. The predicate is the slot key's own:
`uq_intent_slot` is cadence-only (`src/models/target/intent_ledger.py:231`), so a planned story (`origin = 'planned'`) never absorbs
a slot. Keep the predicate: without it a bare `ON CONFLICT` finds no arbiter in the partial
index and every cadence mint raises.

The draw is weighted over the CONNECTED FOLDERS that have eligible media —
explicit ratios; automatic folders in proportion to their files, together never
more than the smallest explicit weight; Off (ratio 0) never
(`category_mix.py:122`). Within the drawn folder: never-posted files first in
the row id's shuffled order, then least-recently-posted (`scheduler.py:417`).
Eligible means `available`, not already live for this account, and not under a
live `post_locks` row. There is no pool behind the weighted set: when nothing
is eligible the slot lapses and the workspace is told at most once per
account per 24 h (`_notice_no_media`, `:253`).

The rule and the folders' weights are ONE per-account read, `category_mix.pool`
(the rule is `category_mix.ELIGIBLE_SQL`), which the draw and the Overview's
days-left figure share, so the two cannot disagree. The mix card's
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
  guard reads it (`fn_clock_tick`, `04_functions.sql:1562`), because the reaper is one of those singletons and
  cannot revive itself. Every other kind's expired lease is the reaper's.
- A deadline ends a job two ways. A job that runs and fails past its budget is
  ended by the worker (`jobs.budget_exhausted`), with the tenant notice; a
  `ready` job past its `deadline_at` is ended `failed` by the reaper, which
  merges `ended: deadline` into its payload and re-arms a sync kind's source,
  but sends no notice, and only for the kinds a sweep re-mints (the list is
  in `fn_reaper_sweep`'s leg, `04_functions.sql:1753`). A
  deferral (`reschedule_job` with the attempt restored: a park, a pacing
  wait) moves the deadline with `run_at`, so the job keeps its slack; a
  retryable failure keeps its deadline. A new job kind must be classified
  against that list.
- Per-workspace lane caps (interactive 5, bulk 3, media 1) are the claim's.
  Interactive and bulk bind only across several replicas: `fn_claim_job` counts
  the deployment's leases, and one process runs fewer tasks per lane
  (2 interactive, 2 bulk) than those caps. So on a single worker one workspace
  can hold a whole lane. Media's cap is below any K its lane is raised to (the
  default is 1), so it binds on one process too, and one workspace never holds
  more than one file task (`work_loop.py:92-103`).
- An executor that waits on a provider is marked `own_transactions`
  (`work_loop.py:242`): it runs with no job session open and finalizes in a
  short transaction afterwards. So is `retention_sweep`, whose batches each
  commit on their own.
- A new kind needs its name in `ck_jobs_kind` — and, for a system kind, in
  `ck_jobs_system_kinds`, which is a biconditional
  (`src/models/target/machinery.py:69`, `:86`) — an entry in `build_registry`
  (`work_loop.py:303`), and, if the clock mints it, the recurring set. A kind
  missing from the CHECK aborts every leg of the tick.

## Media source adapters (`media_adapters.py`, `media_sync.py`)

- `WorkerDeps.adapters` maps provider names to one `MediaAdapter` per provider.
  Sync and both media-fetch paths resolve that exact provider; none falls back
  to Drive. Bootstrap currently registers only `gdrive`.
- Adapters receive the source's config plus source and workspace ids on listing
  and fetch. The publish load joins `media_sources` by workspace and source id;
  a queued card reads provider and config in its own tenant session, closes it,
  then fetches. Cards and outbox payloads carry no provider config.
- `ProviderNotWired` rides the sync retry ladder without marking a source error.
  A missing/inactive source or unwired provider is terminal for a media fetch.
- Checkpoint progress and whole-inventory completeness belong to the adapter.
  Sync reserves only `walk`, `started_at`, and `last_whole_started_at`; Drive's
  stored v2 cursor format remains compatible. Chunks resume the stored cursor
  under its walk token and compare-and-swap; a completed partial inventory
  never authorizes a tombstone. Missing items require two whole inventories.
- A same-reference move between live sources also requires the same provider;
  equal opaque reference strings from different providers identify no shared file.

## The publish pipeline (`publish_pipeline.py`)

- The approval flip has two producers. A person's `approve` flips
  `awaiting_approval → approved`. The serve step does the same as the
  system, stamping `approval_rule`, when a position or the item's flag says so
  (`auto_approval.approve_if_due`, called in the prompt sweep's advance leg in
  a savepoint of its own). Both enqueue the one `publish_pipeline` job,
  `jobs.mint_publish_job`, keyed `ig:<provider_account_ref>`. The dry-run
  decision travels in the job payload.
- The executor is a transaction-per-checkpoint ladder over
  `post_intents.publish_step`: the flip to `publishing` (one CTE that debits
  `daily_post_counts` and takes `uq_publish_exclusive`, `publish_cap.py`),
  transit upload, container permit and create, status poll, publish permit and
  publish, then ONE terminal transaction (`posted`, the counters, the recent
  lock, `finalize_job`). Each permit is a `provider_operations` row committed
  BEFORE the provider call; every checkpoint re-asserts the lease.
- **The frame is judged before the container permit.** Once the transit
  store's readiness probe sees the story frame serve, what it read (the
  frame's size and, for a video, its head to the end of `moov`) is held to
  Meta's story spec by `media_validation.judge_derivation`. A breach parks
  the story `review_required` at once, with no container call, the check and
  what it measured as its refusal (`last_error -> 'error' -> 'refusal'`).
  Where `moov` sits, edit lists and the audio channel count are recorded but
  refuse only with their `REFUSE_*` switches. Everything read rides the
  probe's observation onto the container permit, with the encoding the frame
  was derived with.
- **A refusal of a checked file is a drift, recorded and told.** When Meta
  refuses a container or a publish and the refusal ends the story (a dead
  container with status `ERROR`, or any create or publish error that parks or
  fails it, except a dead token and throttling, `NOT_ABOUT_THE_FILE`), its
  `spec_drift`, our facts beside Meta's answer, goes on the permit, or beside
  a dead container's reason in `last_error`. After the run the operators get
  one direct message per Meta answer per day (`WorkerDeps.ops_notice`,
  `src/channels/telegram_ops_notice.py`, latched by the `rate_counters` scope
  `ops_notice`); a worker without the bot or `OPS_USER_IDS` logs it instead.
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
