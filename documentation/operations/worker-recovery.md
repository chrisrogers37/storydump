# Worker Recovery Runbook

**When to use:** `storydump health` reports scheduling `worker-down` or `stalled`, the worker's
latest deployment reads `CRASHED`, `FAILED` or `REMOVED`, jobs sit `ready` past their `run_at` with
nothing claiming them, or cards stop arriving while the API is well. This page says what the worker
is, how a dead or stuck one is recognised from outside, and how it is brought back on Railway.

**Restarting the production worker is a production action.** It resumes whatever the ledger owes,
and approved stories publish. An agent stops and asks first (`CLAUDE.md`, the STOP rule);
`python -m src.main` is on the never-run list for the same reason.

The legacy scheduler this runbook once covered — its catch-up loop and its polling bot — was retired
in the tear-out (#1216, September 2026); its data survives as the `archive.*_pre_cutover_20260917`
snapshots.

## What runs

The Procfile's `worker` line is `python -m src.main`, which only dispatches to the target
composition root, `src.worker` (`src/main.py:18-22`). `src.worker.main()` (`src/worker.py:793`):

- refuses to boot without `TARGET_DATABASE_URL` — `FATAL: TARGET_DATABASE_URL is unset …` on
  stderr, exit 2 (`src/worker.py:799-810`; pinned by `tests/src/test_legacy_settings_gone.py`);
- refuses to boot when the credential key ring cannot load — `FATAL: the credential key ring
  cannot load. …` on stderr, exit 2, before anything connects (#1401; pinned by
  `tests/src/test_key_ring_at_boot.py`). Give it the key the stored credentials were encrypted
  with — the API holds the same one; a newly generated key boots and cannot read them;
- composes the registry of job kinds and **parks** any kind whose seam it cannot build, by name
  (`src/services/target/work_loop.py:524-616`): no `TARGET_TELEGRAM_BOT_TOKEN` parks
  `deliver_outbox`; an incomplete `CLOUDINARY_*` trio parks `publish_pipeline` and
  `reap_transit_assets`; no email provider parks `send_email`; `retention_sweep` and
  `reencrypt_credentials` have no executor at all. A parked job is rescheduled every 900 s
  (`park_seconds`) with a `parked kind <kind> (job <id>): <reason>` warning, and the rest of the
  worker runs — a parked kind is a degraded worker, not a dead one;
- refuses a lane concurrency the pool cannot hold — `K_interactive × 1 + K_bulk × 2 + 3 ≤ 10`
  (`work_loop.py:159-182`; defaults 3 and 2, from `TARGET_WORKER_INTERACTIVE_CONCURRENCY` and
  `TARGET_WORKER_BULK_CONCURRENCY`) — with a `ValueError` naming the numbers.

`run()` (`src/worker.py:618`) then, in this order: binds the health listener on `PORT` (default
8080) *before* the first database connection; opens the election connection and logs
`worker database role: {...}`; starts the lease heartbeat, the clock and the claim loops; logs
`worker up: lanes=… live_kinds=… recurring=…`; probes the Telegram credential
(`telegram channel live as @<bot>`, or an ERROR that parks `deliver_outbox` on a dead token or on a
token that is not `TARGET_TELEGRAM_BOT_USERNAME`'s bot — `src/worker.py:370-411`); and starts the
status reporter (one `status:` line a minute), the sender-job sweeper (3 s) and the prompt sweeper
(5 s).

**It is fail-fast.** Every one of those tasks is supervised (`src/worker.py:545-579`): a task that
raises, or returns before a stop was asked for, is logged
`background task <name> DIED (…) before stop was requested`, the worker stops, and the process
exits 1 (`src/worker.py:698-709`, `858-861`). A lane whose claim fails ten times running
(`lane_max_consecutive_errors`) raises and takes that path too (`work_loop.py:844-856`). A pool
that is momentarily full is not an error: it is counted as `waits` on the status line. Railway
restarts a process that exits non-zero (`railway.toml`: `restartPolicyType = "ON_FAILURE"`,
`restartPolicyMaxRetries = 10`).

### The job lease and its heartbeat

A claim is one call to the `fn_claim_job` door: it takes the oldest `ready` row of the lane whose
`run_at` has come, marks it `leased`, mints a fresh `lease_token`, sets `locked_until = now() + 90 s`
(`lease_seconds`) and consumes an attempt (`scripts/migrations/059_security_definer_doors.sql:96-120`;
`src/services/target/jobs.py:166-205`). One heartbeat task per process extends every lease it holds
each 20 s (`heartbeat_interval_seconds`) through `fn_extend_leases`, on its own connection
(`jobs.py:420-507`). A failed beat logs `lease heartbeat failed; continuing` and raises the status
line's `heartbeat[… errs=N]`; a beat that extended fewer leases than it holds raises `short=`.

Finishing a job is a compare-and-set on the token — `WHERE id = … AND lease_token = … AND
state = 'leased'` — inside the job's own transaction (`jobs.py:227-248`). Zero rows means the
token is stale — the job was claimed again after its lease lapsed, or it is already terminal:
`JobFenced` aborts that transaction and the status line's `fenced=` rises.

**What a dead process leaves behind.** A clean stop (SIGTERM) lets each lane finish the job it
holds before the process exits (`src/worker.py:721-727`). A job cut off by a kill or a crash stays
`leased` past its `locked_until`, and only `fn_reaper_sweep`'s first leg returns it to `ready`
(`scripts/migrations/076_publish_wait_edges.sql:29-31`). That door is called by the `reap_expired`
kind alone (`src/services/target/scheduler.py:508-540`), which this root asks the clock to mint
every **6 hours** (`src/worker.py:296`; the plan's `05` row says 60 s — the number above is the one
that runs). Until then the job's serialization key is held: the claim door skips a `ready` row
whose key has a `leased` holder without reading the lease's expiry (`059:103-104`), and the sender
sweep mints no second `deliver_outbox` job for that binding (`work_loop.py:1110-1112`). No verb
returns a lapsed lease sooner; if the wait is not acceptable, that is the owner's decision and a
hand-written statement against `jobs`, never an agent's.

### The clock election

There is one clock per deployment. Each worker process tries
`pg_try_advisory_lock(0x5701C10C)` on a session it holds for its whole life
(`scheduler.py:89`, `104-138`) and, when it wins, calls `fn_clock_tick` every 15 s
(`clock_interval_seconds`). A tick mints due work — the recurring system singletons, due account
slots, credential refreshes, source syncs, reauth prompts — at most 500 rows
(`clock_max_inserts`). The recurring set this root hands it is `reap_expired` (6 h),
`alert_stranded_sources` (6 h), `reconcile_ambiguous` (60 s) and, with Cloudinary configured,
`reap_transit_assets` (6 h) (`src/worker.py:294-310`).

Losing the election is not a failure: the loser returns at once and tries again next interval
(`scheduler.py:609-615`). A clean stop releases the lock (`Clock.stop`, `pg_advisory_unlock`); a
killed process releases it when its database session dies, so during a deploy's overlap the new
process reads `clock[elected=False …]` until the old one is gone.

A tick that raises is swallowed and logged on every failure:
`clock tick failed (N consecutive) — NO JOBS WERE MINTED`, with the recurring kinds and the
database's DETAIL line (`scheduler.py:627-672`). All five legs share one transaction, so one
refused row — a job kind the `jobs` table's check constraint does not know, because a migration
the deployed code needs has not been applied — stops every leg. That worker is alive, its
heartbeat beats, and it schedules nothing.

### The worker's own `/health`

The listener answers 200 with the counters (`lanes`, `clock`, `heartbeat`) and 503 —
`"reason": "clock has not advanced; the worker is alive but stuck"` — when `clock.ticks` has not
moved between two probes more than 30 s apart (twice the clock interval;
`src/services/target/worker_health.py:82-97`). The failure counters are reported and never gate it: a
database blip must not spend the restart budget. `ticks` advances only on the process that holds
the election, so a second replica that never wins it would read as stuck here.

## Recognising a dead or stuck worker

| Instrument | What it reads | The reading that means trouble |
|---|---|---|
| `storydump health` | the API's `/health/scheduling`, judged by `scripts/scheduling_monitor.py`'s `classify` | `worker-down`: a due system job unclaimed for more than 900 s, or no system job finished for more than 600 s. `stalled`: an active account's slot cursor more than 600 s behind |
| `storydump deploys` | each service's latest deployments on Railway | the worker's latest row `CRASHED`, `FAILED` or `REMOVED` |
| `storydump jobs --since 3h` | the workspace's jobs by kind × lane × state: `count`, `oldest run at`; a `failed` or `review_required` group carries samples (`id`, `attempts`, `run at`, `error`). `ready` and `leased` rows are listed at any age | a `ready` group whose `oldest run at` keeps receding; a `leased` group that never clears (a lapsed lease, above) |
| `storydump outbox --since 3h` | pending, sending, ambiguous and failed cards by binding | `pending` growing for a binding: nothing is sending |
| `storydump posture` | the migration ledger (`version`, `status`, `applied at`, `checksum`), the connected role, RLS per table, the SECURITY DEFINER doors | a migration the deployed commit needs is absent from the ledger |
| `railway logs --service worker` | the process itself | below |

Three bounds on those verbs. `storydump jobs` never shows the system singletons
(`workspace_id IS NULL` — `reap_expired`, `reconcile_ambiguous` and the rest); the `worker` block
of `/health/scheduling` is what reads them, and `storydump health` is how to see it.
`storydump posture`'s `role` is the **API's** connection, not the worker's — the worker states its
own in its boot line. And `storydump doctor`'s ledger check reports a gated file (`runner:manual`)
as "owed to the owner's window" on its `ok` line rather than as missing
(`storydump_cli/commands/env.py`, `_gated_in`; `migration-runner.md`) — none is owed today: 079
and 080 were applied in the owner's window on 2026-09-19 (`legacy-window-close.md`). Only an
ORDINARY file the ledger lacks is "not applied".

In the logs:

```bash
railway logs --service worker | grep -E "FATAL|DIED|worker up|worker stopped|database role" | tail -20
railway logs --service worker | grep -E "status:" | tail -5
railway logs --service worker | grep -E "clock tick failed|lease heartbeat failed|claim failed|parked kind" | tail -20
```

The `status:` line is the instrument for a worker that is alive: per lane
`tasks processed parked failures exhausted fenced waits`, then
`clock[elected ticks inserts errs]`, `heartbeat[beats short errs]`,
`transport[bot auth_failures media_fetch_failures]`, `sweeper[sweeps mints]`,
`prompts[sweeps prompted advanced]` and the queue's depth and age per lane
(`src/worker.py:414-481`). Counters that stop moving between two lines are the stuck worker;
`elected=False` with `ticks=0` long after a deploy means another session still holds the clock.

## Before restarting: find the cause

A restart that lands on the same fault re-enters it, and spends Railway's ten retries doing so.

| In the logs | Cause | Fix |
|---|---|---|
| `FATAL: TARGET_DATABASE_URL is unset` (exit 2) | the variable is missing on the worker service | set it on the service; the redeploy follows |
| `ValueError: … exceeds the pool of 10`, or `… must be an integer` | a lane-concurrency variable the pool cannot hold | lower `TARGET_WORKER_*_CONCURRENCY` |
| `background task <name> DIED (…)` | the named task raised; the exception is on the line | fix forward; the restart policy has been retrying it |
| `lane <lane>: claim failed … 10/10 consecutive` | the database refused the claim door ten times running | `storydump health --json`; Neon status; the worker's role and its grants (`runtime-database-roles.md`) |
| `clock tick failed (N consecutive)` on every tick | a row the schema refuses — the deployed code is ahead of the ledger | `storydump posture` against the checkout; the pre-deploy log of the last deploy (`migration-runner.md`) |
| `Telegram credential is DEAD at startup` / `token belongs to @x but the configured bot is @y` | the sender is parked; everything else runs | replace `TARGET_TELEGRAM_BOT_TOKEN` on the worker (`telegram-webhook.md`), then redeploy |
| `parked kind publish_pipeline …` | the Cloudinary trio is incomplete on the worker | set `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` |

## Recovery procedure

### 1. Confirm what is deployed

```bash
storydump deploys                 # both services: status and commit
git fetch origin main && git log --oneline origin/main -3
```

If the fix is a commit, it is live when the worker's latest row is `SUCCESS` on that commit —
`storydump deploys --watch --commit <sha> --timeout 1800` follows a push. A predeploy migration
that fails aborts the deploy with the old version still serving (`railway.toml`).

### 2. Redeploy the worker (with the user's approval)

```bash
railway whoami && railway status          # the storydump project, environment production
railway redeploy --service worker --yes
storydump deploys --watch --timeout 900
```

`railway redeploy` acts on the **linked** environment and takes no `--environment`; another
session's `railway login` silently drops the link, so check it first
(`legacy-window-close.md`, step 0).

### 3. Confirm a fresh boot

```bash
railway logs --service worker | grep -E "health endpoint listening|database role|worker up|telegram channel live" | tail -8
```

In order: `health endpoint listening on :<port>`, `worker database role: {...}`,
`worker up: lanes=[…] live_kinds=[…] recurring=[…]`, `telegram channel live as @<bot>`. Read
`live_kinds`: a kind missing from it is parked, and the `parked kind` warning says why.

### 4. Verify work is moving

```bash
storydump health                                    # scheduling no longer worker-down; the last success age falls
storydump jobs --since 3h --watch                   # ready groups drain; ends 6 if a failed group appears or grows
storydump outbox --since 3h                         # pending cards fall
storydump burst --since <the deploy's time> --watch # the post-deploy read (reading-the-ledger.md)
```

The first `status:` line arrives a minute after boot: `clock[elected=True ticks=…]` with `ticks`
rising, `heartbeat[… errs=0]`, `processed` rising on a lane that had work. A `leased` group in
`storydump jobs` that does not clear while the status line shows no work in flight is a lapsed
lease waiting for the reaper (above), not a second fault.

## What a restart does not do

- **It does not stop posting.** The ledger's jobs survive a restart by design. To stop a
  workspace's posting, `storydump pause --workspace <ws>`; `storydump resume --workspace <ws>`
  lifts it. Both go through the command port and are audited.
- **It does not return a lapsed lease**, and it does not un-park a kind whose variable is still
  missing.
- **It does not apply a gated migration.** The predeploy runs `migration_runner apply`, which owes
  a `runner:manual` file and applies nothing of it (`migration-runner.md`).

## Related

- `documentation/operations/reading-the-ledger.md` — the read verbs, their bounds and `--watch`.
- `documentation/operations/scheduling-monitor.md` — the poller that pages on `worker-down`.
- `documentation/operations/troubleshooting.md` — symptoms that are not the worker.
- `documentation/operations/runtime-database-roles.md` — the login the worker connects as.
