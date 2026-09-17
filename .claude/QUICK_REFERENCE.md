# Storydump - Quick Reference for Claude

## ⚠️ CRITICAL SAFETY RULES

**NEVER run these commands** (they post to Instagram or modify production):
- `storydump approve <story>` (posts to Instagram)
- `storydump resolve <story> retry` (posts it again)
- `storydump cancel <story>` / `storydump resolve <story> cancel`
- `storydump tokens revoke <id>`
- `storydump webhook register` / `storydump webhook deregister` (the production bot's webhook)
- `python -m src.main`

The canonical list, with what each command does, is the safety block in
`CLAUDE.md`; this copy is pinned to it by `tests/test_agent_docs.py`.

**SAFE commands** (read-only):
- `storydump floating` / `storydump story <id>` / `storydump cards <id>` / `storydump jobs`
- `storydump health` / `storydump doctor` / `storydump deploys`
- `pytest` (all tests)

---

## Architecture (3-Layer)

```
CLI/Telegram → Services → Repositories → Models/DB
```

**NEVER violate layer boundaries:**
- The CLI (`storydump_cli/`) is an HTTP client of the API; its one `src` import is the vocabulary
- The API and the worker call the target services (`src/services/target/`)
- The services own the SQL, under the unit of work; `src/models/target/` exists for schema parity

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
| `tests/` | Mirrors src/ structure; `tests/scripts/` holds the DB gates |

---

## Common Tasks

| Task | Command/Action |
|------|----------------|
| Run tests | `pytest tests/ -v` |
| Check linting | `ruff check src/ tests/` |
| Format code | `ruff format src/ tests/` |
| Check bot status | `/telegram-status` skill |
| Check DB status | `/db-status` skill |
| Pre-commit check | `ruff check src/ tests/ && ruff format --check src/ tests/ && pytest` |

---

## Database (Neon PostgreSQL)

```bash
# Connect to production database
psql "$DATABASE_URL"

# Safe queries
psql "$DATABASE_URL" -c "SELECT * FROM posting_queue WHERE status = 'pending';"
psql "$DATABASE_URL" -c "SELECT * FROM posting_history ORDER BY posted_at DESC LIMIT 10;"
psql "$DATABASE_URL" -c "SELECT * FROM instagram_accounts WHERE is_active = true;"
```

---

## Key Files to Know

| File | Contains |
|------|----------|
| `src/worker.py` | The worker's composition root |
| `src/api/app.py` | The API |
| `src/services/target/work_loop.py` | Lanes and the job registry |
| `src/services/target/publish_pipeline.py` | Publishing to Instagram |
| `src/services/target/telegram_dispatch.py` | The Telegram channel |

---

## Settings Flow

**Database overrides .env for these:**
- `dry_run_mode` → `chat_settings.dry_run_mode`
- `enable_instagram_api` → `chat_settings.enable_instagram_api`
- `active_instagram_account_id` → per-chat account selection

---

## CHANGELOG Reminder

Every PR must update `CHANGELOG.md` under `## [Unreleased]`
