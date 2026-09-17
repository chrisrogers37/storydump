# Storydump - Project Context

**Copy this into Claude web/phone sessions for context.**

---

## What This Project Does

Storydump is a hosted, multi-tenant Instagram Story scheduling service with Telegram-based workflow:
1. Media files are indexed from Google Drive (or local filesystem)
2. A JIT scheduler checks if a posting slot is due each tick
3. At each slot, the bot either:
   - Posts directly via Instagram Graph API (Phase 2), or
   - Sends the image to Telegram for manual posting (Phase 1)
4. Users interact via Telegram bot commands (/start, /status, /setup, /next, /cleanup, /help)

---

## Architecture (3-Layer)

```
┌─────────────────────────────────────┐
│  Interface Layer                    │
│  • storydump_cli/ - the storydump CLI│
│  • src/api/ - the API (FastAPI)     │
│  • src/channels/ - Telegram transport│
└───────────────┬─────────────────────┘
                │
┌───────────────▼─────────────────────┐
│  Service Layer (the target tier)    │
│  • src/services/target/             │
│    - work_loop, jobs, scheduler     │
│    - publish_pipeline, media_sync   │
│    - command_executors, ops_views   │
│    - telegram_dispatch, adapters    │
│  • src/worker.py - composition root │
└───────────────┬─────────────────────┘
                │
┌───────────────▼─────────────────────┐
│  Data Layer                         │
│  • SQL in the target services       │
│    (readers, executors, unit_of_work)│
│  • src/models/target/ - declarative │
│    models, for schema parity        │
│  • scripts/migrations/ + the runner │
│  • Neon PostgreSQL (cloud)          │
└─────────────────────────────────────┘
```

**RULE**: the CLI never imports `src` except `src/services/target/vocabulary.py`; SQL lives in the target services under the unit of work; the API's routes call the services, never the database directly. (The legacy tier — `src/services/core`, `src/repositories`, the legacy models — was deleted in the tear-out, phase 01; #1216.)

---

## Key Database Tables

| Table | Purpose |
|-------|---------|
| `media_items` | All indexed media (source of truth) |
| `posting_queue` | Scheduled posts (ephemeral work items) |
| `posting_history` | Permanent audit log of all posts |
| `instagram_accounts` | Multi-account support (Phase 1.5) |
| `chat_settings` | Per-Telegram-chat configuration |
| `api_tokens` | Encrypted OAuth tokens |

---

## Settings Resolution

**Database overrides .env for per-chat settings:**
- `chat_settings.dry_run_mode` overrides `DRY_RUN_MODE`
- `chat_settings.enable_instagram_api` overrides `ENABLE_INSTAGRAM_API`
- `chat_settings.active_instagram_account_id` selects which account to post from

---

## Key Files

| File | Purpose |
|------|---------|
| `src/worker.py` | The worker's composition root (lanes, the clock, the publish pipeline) |
| `src/api/app.py` | The API (FastAPI): the command port, the ops views, health |
| `src/services/target/work_loop.py` | The lanes and the job registry |
| `src/services/target/publish_pipeline.py` | Publishing a story to Instagram |
| `src/services/target/scheduler.py` | The clock and the slots |
| `src/services/target/media_sync.py` | Drive sync into the media pool |
| `src/services/target/command_executors.py` | The command port's verbs |
| `src/services/target/ops_views.py` | The ledger read views the CLI shows |
| `src/services/target/telegram_dispatch.py` | The Telegram channel (cards, taps) |
| `src/channels/telegram_transport.py` | The Telegram HTTP transport |
| `storydump_cli/main.py` | The `storydump` CLI |
| `scripts/migration_runner.py` | The migration runner (every deploy's predeploy) |

---

## Safety Rules

**NEVER suggest running:**
- `storydump approve <story>` (posts to Instagram — the user's decision)
- `storydump resolve <story> retry` (posts it again)
- `storydump cancel <story>` / `storydump resolve <story> cancel` (destructive)
- `storydump tokens revoke <id>`
- `storydump webhook register` / `storydump webhook deregister` (the production bot's webhook)
- `python -m src.main` (starts the bot)

The canonical list is the safety block in `CLAUDE.md`; this copy is pinned to
it by `tests/test_agent_docs.py`.

**SAFE to suggest:**
- `storydump floating` / `storydump story <id>` / `storydump health` / `storydump doctor` (reads)
- `pytest tests/`
- Database SELECT queries

---

## Current Version: v1.6.0

- ✅ Phase 1: Telegram manual posting
- ✅ Phase 1.5: Multi-account support
- ✅ Phase 1.6: Settings & Telegram UX
- ✅ Phase 2: Instagram API automation
- 🔲 Phase 3: Shopify integration
- 🔲 Phase 4+: Web UI, analytics

---

## Common Patterns

**Adding a new setting:**
1. Add column to `chat_settings` model
2. Create migration in `scripts/migrations/`
3. Add to `SettingsService.TOGGLEABLE_SETTINGS` if it's a toggle
4. Update Telegram /settings handler

**Adding a new command:**
1. Create handler method in the appropriate handler module (e.g., `telegram_commands.py`)
2. Register in `TelegramService.initialize()` via `_register_handlers()`
3. Update help text in `telegram_commands.py`

**Testing:**
- All services should have unit tests in `tests/src/services/`
- Mock repositories, never hit real database
- Run with `pytest tests/ -v`
