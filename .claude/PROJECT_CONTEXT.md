# Storydump - Project Context

**Copy this into Claude web/phone sessions for context.**

---

## What This Project Does

Storydump is a self-hosted Instagram Story scheduling system with Telegram-based workflow:
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
│  • cli/commands/  - CLI interface   │
│  • TelegramService - Bot handlers   │
└───────────────┬─────────────────────┘
                │
┌───────────────▼─────────────────────┐
│  Service Layer (Business Logic)     │
│  • src/services/core/               │
│    - TelegramService + handlers     │
│    - PostingService, SchedulerService│
│    - MediaIngestionService          │
│    - DashboardService, OAuthService │
│    - SettingsService, MediaSyncSvc  │
│  • src/services/integrations/       │
│    - InstagramAPIService            │
│    - GoogleDriveService + OAuth     │
│    - CloudStorageService            │
│  • src/services/media_sources/      │
│    - MediaSourceFactory (pluggable) │
│    - GoogleDriveProvider, LocalProv │
└───────────────┬─────────────────────┘
                │
┌───────────────▼─────────────────────┐
│  Data Layer                         │
│  • src/repositories/ - CRUD only    │
│  • src/models/ - SQLAlchemy models  │
│  • Neon PostgreSQL (cloud)          │
└─────────────────────────────────────┘
```

**STRICT RULE**: Never violate layer boundaries. Services call Repositories, never Models directly.

---

## Key Database Tables

| Table | Purpose |
|-------|---------|
| `media_items` | All indexed media (source of truth) |
| `posting_queue` | Scheduled posts (ephemeral work items) |
| `posting_history` | Permanent audit log of all posts |
| `instagram_accounts` | Multi-account support (Phase 1.5) |
| `chat_settings` | Per-Telegram-chat configuration — **one row per tenant/instance** |
| `api_tokens` | Encrypted OAuth tokens |
| `user_chat_memberships` | User ↔ instance (chat_settings) membership — backs the multi-instance dashboard and `/start` instance picker. **Security-sensitive**: this is the table that scopes a user to their tenant(s); a 2026-07 audit found and fixed a cross-tenant IDOR where onboarding/dashboard endpoints didn't check it (see SECURITY_REVIEW.md §10 addendum) |
| `onboarding_sessions` | Short-lived DM onboarding conversation state (naming → awaiting_group → complete) |
| `category_mix` | Type 2 SCD history of per-category posting ratios |

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
| `src/services/core/telegram_service.py` | Telegram bot lifecycle + coordination |
| `src/services/core/telegram_commands.py` | /command handlers |
| `src/services/core/telegram_callbacks.py` | Button callback handlers |
| `src/services/core/posting.py` | Orchestrates posting workflow |
| `src/services/core/scheduler.py` | Creates posting schedules |
| `src/services/integrations/instagram_api.py` | Instagram Graph API wrapper |
| `src/services/integrations/google_drive.py` | Google Drive operations |
| `src/services/media_sources/factory.py` | Media source provider routing |
| `src/api/routes/onboarding/` | Mini App API (dashboard, settings) |
| `src/models/chat_settings.py` | Per-chat settings model |

---

## Safety Rules

**NEVER suggest running:**
- `storydump-cli process-queue` (posts to Instagram)
- `storydump-cli create-schedule` (modifies queue)
- `python -m src.main` (starts the bot)

**SAFE to suggest:**
- `storydump-cli list-queue` / `list-media` / `check-health`
- `pytest tests/`
- Database SELECT queries

---

## Current Version: v1.6.0 (string is stale — see note)

- ✅ Phase 1: Telegram manual posting
- ✅ Phase 1.5: Multi-account support
- ✅ Phase 1.6: Settings & Telegram UX
- ✅ Phase 2: Instagram API automation
- ✅ Multi-tenant / multi-instance rearchitecture (User → Instances, `user_chat_memberships`, `/start` instance picker, `landing/` Next.js web dashboard) — shipped after v1.6.0, not reflected in the version string. See `PROJECT_MISSION.md` for the current mental model.
- 🔲 Phase 3: Shopify integration
- 🔲 Phase 4+: LLM integration, order/email automation

**Note:** `src/__init__.py` still hardcodes `__version__ = "1.6.0"` despite 40+ migrations and dozens of unreleased PRs since (see `CHANGELOG.md` `[Unreleased]`). Don't treat the version string as a feature-completeness signal.

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
