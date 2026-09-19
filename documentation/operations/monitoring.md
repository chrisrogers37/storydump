# Monitoring & Alerting

## Overview

Storydump runs on Railway (two services from one repository: `storydump`, the
API, and `worker`) with a Neon PostgreSQL database. The operator's instruments
are the `storydump` CLI (`pip install -e '.[cli]'`, a token minted on the web
under Settings › API tokens), the fleet monitors under `scripts/`, and Railway's
own dashboard and logs. Nothing here reads the database directly: every
question below has a read verb, and the verbs are the same ones an agent runs.

This page is the target tier's, the only tier there is: the `worker` service
runs `python -m src.main`, which dispatches to the target composition root,
`src.worker`, and to nothing else (`src/main.py`). The legacy tier was retired
in the tear-out (#1216, September 2026); its data survives as the
`archive.*_pre_cutover_20260917` snapshots, and nothing here reads them.

---

## Service Status

### Check Service Health

```bash
# The API's three health surfaces, judged by the fleet monitors' own verdicts
# (exit 0 when every surface is well, 4 otherwise; the report is printed either way)
storydump health
storydump health --json

# The latest deployment of each service, with its commit — through your own
# `railway` login, the linked project checked first
storydump deploys
storydump deploys --watch --commit <sha> --timeout 1800   # right after a push

# Logs (the API service is `storydump`, not `web`)
railway logs --service worker
railway logs --service storydump
```

`storydump doctor` checks the token, the API, the token store, the local
configuration, the Railway login and link, and the migration ledger against the
checkout — the first thing to run on a laptop that cannot reach anything.

### Common Status Indicators

| `storydump deploys` status | Meaning | Action |
|---|---|---|
| `SUCCESS` | The deployment is live | None |
| `SLEEPING` | Live, scaled to zero | None (Railway wakes it on traffic) |
| `SKIPPED` | Nothing to build for that service | None |
| `BUILDING` / `DEPLOYING` | In progress | Wait; `--watch` ends when it lands |
| `FAILED` / `CRASHED` | The deployment did not come up | `railway logs --service <svc>` |
| `REMOVED` | Taken down; it will not become live | Redeploy |

---

## Key Metrics to Monitor

### 1. The float (stories approved and waiting)

```bash
storydump floating                 # every workspace the token can read
storydump floating --watch         # ends 0 when nothing floats twice running; 6 when a job dies
storydump story <id>               # one story's whole timeline
```

### 2. Jobs and the outbox

```bash
storydump jobs --since 3h          # counts by kind × lane × state, the oldest due, failed samples
storydump outbox --since 3h        # pending, sending, ambiguous and failed rows by binding
storydump jobs --watch             # ends 6 when a failed group appears or grows
```

### 3. A posting burst

```bash
storydump burst --since 2026-09-15T14:50:00Z   # taps, permits, waits, siblings, reviews, outcomes
storydump burst --watch                        # ends 0 when nothing is mid-flight, 6 on a review
storydump account <handle>                     # the cap, today's count, the next slot
```

### 4. The deployment's posture

```bash
storydump posture                  # the migration ledger, the connected role, RLS per table, the doors
```

### 5. The worker's own line

```bash
railway logs --service worker | grep "status:" | tail -3
railway logs --service worker | grep -E "worker up|database role|telegram channel live" | tail -5
```

Once a minute the worker logs one `status:` line (`src/worker.py:414-481`): per
lane `tasks processed parked failures exhausted fenced waits`, then
`clock[elected ticks inserts errs]`, `heartbeat[beats short errs]`, the
transport, the two sweepers, and the queue's depth and age per lane. Counters
that stop moving between two lines are a stuck worker; `worker-recovery.md`
reads the line field by field. At boot it logs the login it connected as
(`worker database role: {...}`), the kinds it can run (`worker up: … live_kinds=…`
— a kind missing there is parked, with a `parked kind` warning naming why) and
the bot it sends as. `storydump posture`'s `role` is the API's connection, not
the worker's.

### 6. Error Rate

```bash
railway logs --service worker | grep -c ERROR
railway logs --service storydump | grep -c ERROR
# Or the Railway dashboard's log viewer, filtered on "ERROR"
```

---

## Alerting

The fleet monitors are stdlib-only scripts that poll the API's health surfaces
and page on the same verdicts `storydump health` prints:

| Monitor | Surface | Pages on |
|---|---|---|
| `scripts/scheduling_monitor.py` | `/health/scheduling` | a cursor stalled past 10 min, the worker down, the API unreachable |
| `scripts/posting_monitor.py` | `/health/posting` | 48 h of silence, a first post overdue past its grace, unreachable |

See `scheduling-monitor.md` and `posting-monitor.md` beside this page for the
thresholds and the pollers' two extra rules (a watch clock; two unreachable
readings before paging) that a single `storydump health` reading does not have.
`storydump health` also judges the bot's webhook from `/health` (unregistered,
or a backlog behind a delivery error); `storydump webhook status` asks Telegram
directly.

---

## Log Locations

| Log | Location |
|-----|----------|
| Worker logs | Railway dashboard or `railway logs --service worker` |
| API logs | Railway dashboard or `railway logs --service storydump` |
| Log levels | `WORKER_LOG_LEVEL` sets the worker's root logger (`src/worker.py:795`); `LOG_LEVEL` sets the shared `storydump` logger the API's routes write through (`src/utils/logger.py`). Both default to `INFO` and write to stdout, which Railway captures |
| PostgreSQL logs | Neon dashboard |

---

## Restart Procedures

```bash
railway whoami && railway status              # the storydump project, environment production
railway redeploy --service worker --yes       # re-runs the service's latest deployment
railway redeploy --service storydump --yes
storydump deploys --watch --timeout 900       # both services SUCCESS

# A push to main deploys both; `storydump deploys --watch --commit <sha>` follows it.
```

`railway redeploy` acts on the **linked** environment and takes no
`--environment`; another session's `railway login` silently drops the link, so
check it first (`legacy-window-close.md`, step 0). Every deploy of either
service runs the predeploy, `python -m scripts.migration_runner apply`
(`railway.toml`). Restarting the production worker resumes whatever the ledger
owes — approved stories publish — so it is a production action and an agent
asks first; `worker-recovery.md` is the runbook. A restart does not stop
posting: `storydump pause --workspace <ws>` does.
