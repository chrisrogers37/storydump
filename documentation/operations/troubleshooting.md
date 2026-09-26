# Troubleshooting Guide

## Quick Diagnostics

```bash
# Is it me, or the deployment? Token, API, token store, config, Railway link, ledger.
storydump doctor

# The API's three health surfaces and the bot's webhook, judged (exit 4 when not well)
storydump health

# Recent errors
railway logs --service worker | grep -i error | tail -20
railway logs --service storydump | grep -i error | tail -20
```

The API service on Railway is `storydump` (the worker is `worker`). `railway
shell` has no `-c`; to read a variable on a service, use
`railway run --service worker --environment production -- sh -c 'echo "$NAME"'` — and never print a
secret's value.

This page is the target tier's, the only tier there is: `python -m src.main`
runs the target composition root, `src.worker`, and nothing else
(`src/main.py`). The legacy tier was retired in the tear-out (#1216, September
2026); its data survives as the `archive.*_pre_cutover_20260917` snapshots, and
nothing here reads them.

`storydump doctor`'s ledger check compares the checkout's migration files with
the ledger. A GATED file (`runner:manual`) would be reported on the `ok` line as
"owed to the owner's window", never as missing (`storydump_cli/commands/env.py`,
`_gated_in`); none is gated today — 079 and 080 were applied in the owner's
window on 2026-09-19 (`legacy-window-close.md`). An ordinary file the ledger
lacks is "not applied — deploy main", and that one is real
(`migration-runner.md`).

---

## Common Issues

### 1. Service Won't Start

**Symptoms**: `storydump deploys` shows `FAILED` or `CRASHED`, or repeated restarts.

**Diagnosis**:
```bash
storydump deploys
railway logs --service worker | tail -50
railway logs --service storydump | tail -50
```

**Common Causes**:

| Error | Cause | Fix |
|-------|-------|-----|
| `FATAL: TARGET_DATABASE_URL is unset … Refusing to boot` (the worker, exit 2) | the variable is missing or blank on the worker service (`src/worker.py:799-810`) | set it on the service. The API does not refuse to start without it: `/health` reports `"target_database": false` and every data route answers 503 |
| `FATAL: the credential key ring cannot load. …` (the worker, exit 2) or `… Refusing to start` (the API; the deploy fails its health check) | `ENCRYPTION_KEY` is unset on that service or is not a Fernet key, or `ENCRYPTION_KEYS` — which overrides it — holds a bad entry; the message names which (`src/utils/encryption.py`, #1401) | set the key the stored credentials were encrypted with: the other service holds the same one. A newly generated key boots and then cannot read a single stored credential — generate one only on a first install |
| `ValueError: … exceeds the pool of 10` (the worker) | the lane concurrency does not fit the pool (`src/services/target/work_loop.py:159-182`) | lower `TARGET_WORKER_INTERACTIVE_CONCURRENCY` / `TARGET_WORKER_BULK_CONCURRENCY` |
| `background task <name> DIED` (the worker, exit 1) | a supervised task raised; the worker is fail-fast | `worker-recovery.md` |
| the predeploy fails | a migration or its postcondition failed, or an applied file no longer matches its checksum. The deploy aborts with the old version still serving (`railway.toml`) | `migration-runner.md`; fix forward, never edit an applied file |
| `ModuleNotFoundError` | a missing dependency | `requirements.txt` and `railway.toml`'s build command |
| `Connection refused`, or a timeout, at startup | the database is not reachable | `TARGET_DATABASE_URL` (the runtime login) and `DATABASE_URL` (the runner's, the owner's connection) on that service; Neon's status |
| `permission denied for …` from Postgres | the runtime login lacks a grant the code needs | the log line names the object; `runtime-database-roles.md` |

---

### 2. Stories Not Going Out

**Symptoms**: approved stories float and nothing posts.

**Diagnosis**:
```bash
storydump floating                      # step, counters, the last wait's class and rung, the job
storydump story <id>                    # the timeline: every transition, permit, wait and card
storydump jobs --since 3h               # failed groups with samples (a publish job's last_error)
storydump account <handle>              # the cap, today's count, the next slot
```

**Common Causes**:

| Cause | Check | Fix |
|-------|-------|-----|
| Posting paused for the workspace | `storydump story <id>` shows no permits since the pause | `storydump resume --workspace <ws>` (an operator token) — it restarts POSTING: the owner's decision, an agent asks first |
| Instagram API posting off | the port refuses `approve` with `manual_mode` | Settings › General on the web |
| The account's cap reached | `storydump account <handle>`: today's count at the cap | Wait for the next slot, or raise the cap on the web |
| The frame not ready, or the container not ready | `floating` shows `fetch/<rung>` or `container/<rung>`, the rung climbing | Let the ladder run; `burst --watch` follows it |
| A publish answer was lost | the story is `publishing_ambiguous`; `resolve` refuses with `may_have_posted` | Look at Instagram, then `storydump resolve <story> retry --not-posted` or `resolve <story> posted` (never-run list: ask the user) |
| The publish kind is parked | the worker's log: `parked kind publish_pipeline …` naming the missing seam | set the `CLOUDINARY_*` trio on the worker (`worker-recovery.md`) |
| The worker is down or stuck | `storydump health`: scheduling `worker-down` or `stalled` | `worker-recovery.md` |

---

### 3. Telegram Bot Not Responding

**Symptoms**: taps and messages get no answer.

**Diagnosis**:
```bash
storydump health                         # the webhook verdict from /health (unregistered, undelivered)
storydump webhook status                 # asks Telegram: the bot, the registered URL, the backlog, the API door
storydump outbox --since 3h              # cards owed or failed, by binding
railway logs --service storydump | grep -i telegram
```

**Common Causes**:

| Cause | Fix |
|-------|-----|
| No webhook registered, or registered elsewhere | the API registers it at startup in production; otherwise `storydump webhook register` (never-run list: ask the user) |
| The API refuses the secret (`403` at the door) | `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` differs between the API and the shell |
| A backlog behind a delivery error | `railway logs --service storydump` for the failing route; the backlog drains once it answers |
| Outbox rows failed | `storydump outbox --since 24h`, then `storydump cards <story>` for the card's attempts |
| Cards are owed and never sent: `storydump outbox` shows `pending` growing | the worker's sender is parked — no `TARGET_TELEGRAM_BOT_TOKEN`, a token Telegram rejects (`Telegram credential is DEAD at startup`), or a token that is not the configured bot's (`src/worker.py:370-411`). Fix the variable on the worker and redeploy it (`worker-recovery.md`) |

---

### 4. A Token Is Refused

**Symptoms**: `storydump whoami` exits 3; a verb answers `not authorized`.

**Diagnosis**:
```bash
storydump doctor                         # token wrong / missing / skipped, with the fix
storydump whoami                         # who the token is, its role, its workspaces
```

**Common Causes**:

| Cause | Fix |
|-------|-----|
| Expired or revoked | Mint a new one under Settings › API tokens, `storydump login` |
| A read-only token asked to write | Mint one with the operator role (chosen on purpose; the form defaults to read-only) |
| Below the role floor in that workspace (`insufficient_role`) | Ask a workspace admin or owner |
| A service identity asked for a person's route | Service identities read one workspace and never write |

---

### 5. Media Not Syncing

**Symptoms**: a media source shows old or missing files.

**Diagnosis**:
```bash
storydump jobs --since 24h               # `sync_media_source` jobs by state, with the failed samples
```

**Common Causes**:

| Cause | Fix |
|-------|-----|
| Google no longer accepts the workspace's grant: the source flips to `error` and the workspace is told once (`src/services/target/media_sync.py`) | Reconnect Google Drive under Settings › Integrations |
| The worker cannot refresh a Drive grant — its boot log warns `Drive read leg armed without GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET` (`src/worker.py:835-843`) | set both on the worker service |

**Re-sync**:
```bash
storydump sync <source_id> --workspace <ws>   # or Sync Now under Settings › Integrations
```

---

### 6. Database Connection Issues

**Symptoms**: the API answers 503 (`storydump health` reports the surface's error); `doctor` says the API is degraded.

**Diagnosis**:
```bash
storydump health --json                  # which surface, and the error it answered
storydump posture                        # the connected role and the migration ledger, when the API answers
railway logs --service storydump | grep -iE 'pool|connect'
```

**Common Causes**:

| Cause | Fix |
|-------|-----|
| `TARGET_DATABASE_URL` absent on the API | every data route answers 503 and `/health` reports `"target_database": false`; set it on the `storydump` service |
| Neon endpoint suspended | The first connection wakes it |
| The pool saturated (a 503 naming `pool_saturated`, with `Retry-After: 1`) | Wait; a watch retries three times before giving up |
| Wrong credentials | Check the database variables in the Railway dashboard |
| The Neon plan's compute limit | the Neon console |

---

## Log Analysis

```bash
railway logs --service worker | grep -iE 'exception|traceback|failed'
railway logs --service storydump | grep -iE 'exception|traceback|refused'
```

| Pattern | Meaning |
|---------|---------|
| `refused <METHOD> <path>: …` | the API refused a request (the reason follows) |
| `pool_saturated` | the API is shedding load |
| `telegram webhook not registered` | the startup registration failed; `storydump webhook status` |
| `status: interactive[…] bulk[…] clock[…] heartbeat[…]` | the worker's one-a-minute status line; counters that stop moving are a stuck worker (`worker-recovery.md`) |
| `clock tick failed (N consecutive) — NO JOBS WERE MINTED` | a clock tick raised and minted nothing. Once is a blip; on every tick it is a row the schema refuses — the deployed code ahead of the migration ledger (`worker-recovery.md`) |
| `parked kind <kind> (job <id>): <reason>` | a kind this worker cannot run, and the variable or seam it is missing |

---

## Emergency Procedures

### Stop All Posting Immediately

```bash
storydump pause --workspace <ws>          # per workspace, through the command port, audited
```

`pause` is a workspace verb: run it for each workspace that must stop. It is
idempotent and safe to repeat; `storydump resume --workspace <ws>` lifts it — that
restarts posting, so it is the owner's decision and an agent asks first.
Restarting the worker does NOT stop posting — the ledger's jobs survive a
restart by design.

### A Story Must Not Post

```bash
storydump story <id>                      # where it is
# then, with the user's explicit approval (never-run list):
#   storydump cancel <id> --workspace <ws>
```

### Force Service Restart

```bash
railway whoami && railway status                 # the storydump project, environment production
railway redeploy --service worker --yes
railway redeploy --service storydump --yes
storydump deploys --watch --timeout 900
```

`railway redeploy` acts on the linked environment and takes no `--environment`
(`legacy-window-close.md`, step 0). Restarting the production worker resumes
whatever the ledger owes, so it is a production action: an agent asks first.
