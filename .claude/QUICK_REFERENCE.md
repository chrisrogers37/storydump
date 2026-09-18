# Storydump - Quick Reference for Claude

## ⚠️ CRITICAL SAFETY RULES

**NEVER run these commands** (they post to Instagram or modify production):
- `storydump approve <story>` (posts to Instagram)
- `storydump resolve <story> retry` (posts it again)
- `storydump cancel <story>` / `storydump resolve <story> cancel`
- `storydump tokens revoke <id>`
- `storydump webhook register` / `storydump webhook deregister` (the production bot's webhook)
- `python -m src.main`
- `python -m scripts.migration_runner apply --manual <version>` (applies a gated file; 079 drops the legacy schema — the owner's window)

The canonical list, with what each command does, is the safety block in
`CLAUDE.md`; this copy is pinned to it by `tests/test_agent_docs.py`.

Not on that list does not mean safe: `storydump skip`, `reject`, `posted`,
`pause`, `resume` and `sync` also write through the command port — a skip or a
reject is final for that story. Ask first.

**SAFE commands** (read-only):
- `storydump whoami` / `storydump story <id>` / `storydump cards <id>` / `storydump floating` / `storydump account <handle>`
- `storydump jobs` / `storydump outbox` / `storydump burst` / `storydump posture`
- `storydump health` / `storydump doctor` / `storydump deploys` / `storydump webhook status`
- `pytest` (all tests)

---

## Architecture (3-Layer)

```
storydump CLI ─HTTP→ API (src/api) ─┐
Telegram ─webhook→ API             ├→ target services (src/services/target) ─SQL→ Neon
worker (src/worker.py) ────────────┘
```

**NEVER violate layer boundaries:**
- The CLI (`storydump_cli/`) is an HTTP client of the API; its one `src` import is the vocabulary
- The API and the worker call the target services (`src/services/target/`)
- The services own the SQL, under the unit of work; `src/models/target/` exists for schema parity
- Writes go through the command port (`commands.py`); a transaction never spans a provider call

---

## Key Directories

| Path | Purpose |
|------|---------|
| `src/services/target/` | The target tier: lanes, jobs, the publish pipeline, views, executors |
| `src/api/` | The API (FastAPI): routes, auth, the command port |
| `src/channels/` | Telegram transport and webhook registration |
| `src/models/target/` | Declarative models (schema parity with the migrations) |
| `scripts/migrations/` | The migration corpus; `scripts/migration_runner.py` applies it |
| `storydump_cli/` | The `storydump` CLI (an HTTP client of the API) |
| `landing/` | The web app (Next.js); its BFF proxies to the API |
| `tests/` | Mirrors src/ structure; `tests/scripts/` holds the DB gates |

---

## Common Tasks

| Task | Command/Action |
|------|----------------|
| Run tests | `pytest tests/ -v` |
| Check linting | `ruff check .` |
| Format code | `ruff format .` |
| Check the bot and its chats | `/telegram-status` skill |
| Check the ledger and the deployment | `/db-status` skill |
| Pre-commit check | `ruff check . && ruff format --check . && pytest` |

---

## Reading Production

Read the ledger with `storydump` first — every verb is a bounded,
tenant-scoped read through the API under your token, never a database
connection (`documentation/operations/reading-the-ledger.md`):

```bash
storydump floating                 # approved stories waiting between attempts
storydump jobs --since 3h          # the queue by kind, lane and state
storydump account <handle>         # cap, zone, next slot, recent outcomes
storydump posture                  # the migration ledger, the role, RLS, the doors
```

`psql` through Railway is the escape hatch for a question no verb answers.
Production is the user's to open: ask first, SELECT only, and the connection
string is never printed.

```bash
railway run --service worker -- sh -c 'psql "$TARGET_DATABASE_URL" -At -F " | " -f /dev/stdin' < probe.sql \
  | sed -E "s#postgres(ql)?://[^ ]+#postgres://<redacted>#g"
```

```sql
-- probe.sql: target tables only
SELECT state, count(*) FROM post_intents GROUP BY state ORDER BY state;
SELECT kind, lane, state, count(*) FROM jobs GROUP BY kind, lane, state ORDER BY 1, 2, 3;
SELECT handle, state, next_slot_at FROM ig_accounts ORDER BY created_at;
```

The legacy tier's data survives only as the `archive.*_pre_cutover_20260917`
snapshots (078); no code reads them.

---

## Key Files to Know

| File | Contains |
|------|----------|
| `src/worker.py` | The worker's composition root |
| `src/api/app.py` | The API |
| `src/services/target/commands.py` | The command port and its role floors |
| `src/services/target/unit_of_work.py` | The tenant-scoped transaction |
| `src/services/target/work_loop.py` | Lanes and the job registry |
| `src/services/target/publish_pipeline.py` | Publishing to Instagram |
| `src/services/target/telegram_dispatch.py` | Inbound Telegram: taps, `/start`, group joins |
| `src/services/target/outbox.py` | Outbound Telegram: the delivery record |

---

## Settings Flow

**Per-workspace rows in the ledger — never environment variables:**
- `workspaces.dry_run_mode` — rehearse the whole flow, post nowhere
- `workspaces.api_publishing_enabled` — off means manual posting (`approve` is refused as `manual_mode`)
- `workspaces.is_paused` — `storydump pause` / `storydump resume`
- `ig_accounts` schedule overrides — NULL inherits the workspace

Changed on the web (Settings) or through the command port (`settings_change`,
`account_settings_change`).

---

## CHANGELOG Reminder

Every PR must update `CHANGELOG.md` under `## [Unreleased]`
