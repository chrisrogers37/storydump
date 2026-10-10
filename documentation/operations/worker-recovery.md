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
  `reap_transit_assets`; no email provider parks `send_email`; `reencrypt_credentials` has
  no executor at all, and `retention_sweep` deletes only `rate_counters` rows older than 7 days. A parked job is rescheduled every 900 s
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

**A loop that stops turning ends the process too.** Supervision runs on the event loop, so it
cannot act when the loop itself is not running: a call that does not return on the loop's thread,
or a process that was stopped and resumed. A watchdog thread
(`src/services/target/loop_watchdog.py`) is armed for the whole of `run()`, startup and teardown
included. The loop stamps a beat every 5 s (`loop_beat_seconds`); when it has been silent for more
than 300 s (`loop_stall_seconds`), or the thread itself was absent for more than that and one beat
between two of its own checks, the thread writes
`FATAL: worker watchdog: <reason>. Every thread's stack follows; …` and each thread's stack to
stderr, and the process exits 3. Three things it cannot see:

- **A loop that turns while its tasks wait** on a database or a provider. From inside, a wait that
  will never end looks the same as an outage, and exiting through an outage spends the ten
  restarts. So a worker whose counters stop while its status lines keep arriving is waiting, and
  this does not restart it.
- **A call that holds the interpreter lock for the whole stall.** The thread needs that lock to
  run. It reports such a call only after it returns, as the watchdog having been absent, with
  stacks that no longer name it.
- **A process that no longer runs at all** (a host that is gone). That is the monitor's page
  (`scheduling-monitor.md`) and a redeploy, below.

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
`leased` past its `locked_until`, and `fn_reaper_sweep`'s first leg returns it to `ready`
(`scripts/migrations/076_publish_wait_edges.sql:29-31`). That door is called by the `reap_expired`
kind alone (`src/services/target/scheduler.py:508-540`), which this root asks the clock to mint
every **60 seconds** (`src/worker.py:316`, the plan's `05` number). The one lease the reaper cannot
return is its own: when the dead process held `reap_expired`, the clock's next tick returns that
lease to `ready` itself, as it does for every recurring singleton it mints
(`scripts/migrations/084_clock_revives_singleton_leases.sql`), and the revived reaper then frees
the rest. Until the next sweep the job's serialization key is held: the claim door skips a `ready` row
whose key has a `leased` holder without reading the lease's expiry (`059:103-104`), and the sender
sweep mints no second `deliver_outbox` job for that binding (`work_loop.py:1110-1112`). No verb
returns a lapsed lease sooner; if the wait is not acceptable, that is the owner's decision and a
hand-written statement against `jobs`, never an agent's.

**What a job nothing claims becomes.** A `ready` job can sit unclaimed: another job holds its
key, or its (workspace, key) scope is quarantined, which the claim door skips. Once it is past its
`deadline_at` (every mint writes one except `publish_pipeline`'s), the reaper's last leg ends it
`failed` and merges `ended: deadline` into its payload, so `payload->>'ended'` tells a job the
reaper ended from one whose run failed
(`scripts/migrations/086_reaper_ends_ready_jobs_past_deadline.sql`). It does so only for the kinds a
sweep re-mints, which come back by themselves: the clock's singletons and its slot, refresh and
reauth jobs, the sender's `deliver_outbox`, and the sync kinds, whose source it re-arms for 24 hours
later. An email, an offboarding, a credential revocation, a retention or re-encryption run and a
publish are never ended this way: nothing would re-mint them. A deferral moves its job's deadline
with its `run_at` (`jobs.reschedule_job`), so a parked or paced job keeps its slack. Nobody is
notified: the worker's own spent-budget path sends the tenant a notice, the reaper does not.

### The clock election

There is one clock per deployment. Each worker process tries
`pg_try_advisory_lock(0x5701C10C)` on a session it holds for its whole life
(`scheduler.py:89`, `104-138`) and, when it wins, calls `fn_clock_tick` every 15 s
(`clock_interval_seconds`). A tick mints due work — the recurring system singletons, due account
slots, credential refreshes, source syncs, reauth prompts — at most 500 rows
(`clock_max_inserts`). The recurring set this root hands it is `reap_expired` (60 s),
`alert_stranded_sources` (6 h), `reconcile_ambiguous` (60 s) and, with Cloudinary configured,
`reap_transit_assets` (6 h) (`src/worker.py:314-337`).

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
and 080 were applied in the owner's window on 2026-09-19
([`legacy-window-close.md`](../archive/2026-09-16-legacy-tear-out/legacy-window-close.md)). Only an
ORDINARY file the ledger lacks is "not applied".

In the logs:

```bash
railway logs --service worker | grep -E "FATAL|DIED|worker up|worker stopped|database role" | tail -20
railway logs --service worker | grep -E "status:" | tail -5
railway logs --service worker | grep -E "clock tick failed|lease heartbeat failed|claim failed|parked kind|prompt sweep failed|planned-miss sweep failed" | tail -20
```

The `status:` line is the instrument for a worker that is alive: per lane
`tasks processed parked failures exhausted fenced waits`, then
`clock[elected ticks inserts errs]`, `heartbeat[beats short errs]`,
`transport[bot auth_failures media_fetch_failures]`, `sweeper[sweeps mints]`,
`prompts[sweeps prompted advanced missed unheard]` (`missed` counts planned stories the miss leg
ended, `unheard` those whose workspace had no chat to tell), `watchdog[armed]` (`armed=False` on
a running worker means the watchdog thread is gone and nothing would end a stopped loop) and the
queue's depth and age per lane (`status_line` in `src/worker.py`). Counters that stop moving
between two lines are the stuck worker;
`elected=False` with `ticks=0` long after a deploy means another session still holds the clock.

## Before restarting: find the cause

A restart that lands on the same fault re-enters it, and spends Railway's ten retries doing so.

| In the logs | Cause | Fix |
|---|---|---|
| `FATAL: TARGET_DATABASE_URL is unset` (exit 2) | the variable is missing on the worker service | set it on the service; the redeploy follows |
| `ValueError: … exceeds the pool of 10`, or `… must be an integer` | a lane-concurrency variable the pool cannot hold | lower `TARGET_WORKER_*_CONCURRENCY` |
| `background task <name> DIED (…)` | the named task raised; the exception is on the line | fix forward; the restart policy has been retrying it |
| `FATAL: worker watchdog: the event loop has not turned for N s` (exit 3) | a call on the loop's thread did not return; the stacks that follow the line name it | fix the call (bound it, or move it off the loop); the restart policy has already restarted the worker |
| `FATAL: worker watchdog: the watchdog was absent N s between two checks` (exit 3) | the process was stopped and resumed on the platform's side, or one call held the interpreter lock that long. The stacks are from after it, so they do not say which | the restart is the repair. If it repeats, ask the platform about the container, and look for a long synchronous call in whatever ran just before |
| the `status:` line stops with no line after it, and the deployment still reads `SUCCESS` | either the process is not running at all (nothing inside it can report or exit), or its loop turns while every task waits, the status reporter's own read included | the container's metrics tell them apart: if they stop with the log, the host is gone; if they continue, the worker is waiting. Redeploy (below) either way, and raise a stopped container with the platform |
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
([*The checkout and the link*](#the-checkout-and-the-link), below). If the worker's latest row
reads `REMOVED` — a `railway down` — do not redeploy it this way: a redeploy after a `down` can
bring back an old build ([*After a `railway down`*](#after-a-railway-down-never-railway-redeploy)).

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

## Production acts from a checkout

Two rules hold for any production act run from a laptop: a `railway run` against production, a
`railway down`, a redeploy. Both were learned in the owner's window of 2026-09-19; its record is
[`legacy-window-close.md`](../archive/2026-09-16-legacy-tear-out/legacy-window-close.md) (steps 0
and 8).

### The checkout and the link

`railway run` executes the LOCAL checkout with the service's environment: the code that runs is
the text on disk, not the deployed commit. A migration applied that way records the checksum of
the file on disk, so a checkout that is not the deployed commit fails every later deploy's
integrity check on both services. And `railway redeploy` acts on the LINKED environment (it takes
no `--environment`); another session's `railway login` silently drops the link. Check both before
the act, and the link again before any redeploy:

```bash
git status --porcelain                  # empty
git rev-parse --short HEAD              # the commit `storydump deploys` shows for both services
shasum -a 256 scripts/migrations/<the file>.sql   # when the act applies a migration: paste into the PR
railway whoami && railway status        # the storydump project, environment production
railway environment production          # re-link if it is not
```

### After a `railway down`, never `railway redeploy`

`railway redeploy` re-runs whatever Railway holds as the service's latest deployment, and after a
`railway down` that need not be the deployed commit: in the window of 2026-09-19 it re-ran an OLD
deployment, a commit of 2026-09-03, and the worker came back on stale code. The way back is a push
to `main` — an empty commit — which deploys both services through the normal path and runs the
predeploy. The alternative is the dashboard's Redeploy on the worker's last `SUCCESS` deployment
*at the deployed commit*, chosen by hand. Either way, read the commit `storydump deploys` shows for
the worker: it must be `main`'s head.

```bash
git commit --allow-empty -m "redeploy: the worker after a down" && git push origin main
storydump deploys --watch --timeout 900     # both services SUCCESS at main's head (the API after CI)
storydump health                            # every surface ok; the worker's last success age falls
```

## Related

- `documentation/operations/reading-the-ledger.md` — the read verbs, their bounds and `--watch`.
- `documentation/operations/scheduling-monitor.md` — the poller that pages on `worker-down`.
- `documentation/operations/troubleshooting.md` — symptoms that are not the worker.
- `documentation/operations/runtime-database-roles.md` — the login the worker connects as.
