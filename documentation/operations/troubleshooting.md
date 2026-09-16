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

This page is the target tier's — the only tier deployed: the worker dispatches
to the target composition root (`WORKER_IMPL=target`, since 2026-08-24). The
legacy tables (`posting_queue`, `chat_settings`, `api_tokens`) survive in the
`legacy` schema, undeployed, until #1216 snapshots and drops them; nothing here
reads them.

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
| `ModuleNotFoundError` | Missing dependency | Check `requirements.txt` and the build command |
| `Connection refused` | Database not reachable | Check `TARGET_DATABASE_URL` / `DATABASE_URL` and Neon status |
| a migration refused at pre-deploy | the runner found drift | `documentation/operations/migration-runner.md` |
| `Permission denied` | Environment misconfiguration | Verify the variables in the Railway dashboard |

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
| Posting paused for the workspace | `storydump story <id>` shows no permits since the pause | `storydump resume --workspace <ws>` (an operator token) |
| Instagram API posting off | the port refuses `approve` with `manual_mode` | Settings › General on the web |
| The account's cap reached | `storydump account <handle>`: today's count at the cap | Wait for the next slot, or raise the cap on the web |
| The frame not ready, or the container not ready | `floating` shows `fetch/<rung>` or `container/<rung>`, the rung climbing | Let the ladder run; `burst --watch` follows it |
| A publish answer was lost | the story is `publishing_ambiguous`; `resolve` refuses with `may_have_posted` | Look at Instagram, then `storydump resolve <story> retry --not-posted` or `resolve <story> posted` (never-run list: ask the user) |
| The worker is down | `storydump health`: scheduling `worker-down` | `railway logs --service worker`, restart |

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
| Neon endpoint sleeping | The first connection wakes it (cold start ~1-2 s) |
| The pool saturated (a 503 naming `pool_saturated`, with Retry-After) | Wait; a watch retries three times before giving up |
| Wrong credentials | Check the database variables in the Railway dashboard |
| Neon free-tier limit | Neon dashboard, compute hours |

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

---

## Emergency Procedures

### Stop All Posting Immediately

```bash
storydump pause --workspace <ws>          # per workspace, through the command port, audited
```

`pause` is a workspace verb: run it for each workspace that must stop. It is
idempotent and safe to repeat; `storydump resume --workspace <ws>` lifts it.
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
railway restart --service worker
railway restart --service storydump
```
