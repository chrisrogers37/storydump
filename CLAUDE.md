# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.
Domain-specific rules are in `.claude/rules/` and load automatically when working in matching files.

---

## CRITICAL SAFETY RULES

**THIS SYSTEM POSTS TO INSTAGRAM. DO NOT TRIGGER POSTING WITHOUT EXPLICIT USER APPROVAL.**

### NEVER run these commands:
```bash
python -m src.main                   # Starts the JIT posting scheduler (worker) + Telegram bot
storydump-cli reset-queue            # Modifies production queue
storydump-cli instagram-auth         # Modifies authentication
storydump-cli revoke-tokens          # Revokes live OAuth tokens for a service (breaks posting until re-auth)
storydump-cli rotate-keys            # Re-encrypts all stored tokens with a new key; wrong ENCRYPTION_KEYS ordering can lose access to tokens. No dry-run.
```

**Note (verified 2026-07-06):** `storydump-cli process-queue` and `storydump-cli create-schedule` no longer exist as CLI commands — the manual Phase-1-era "generate a schedule, then process it" workflow was replaced by the always-on JIT scheduler inside `python -m src.main` (see `.claude/rules/scheduler.md`). That command is still correctly listed above as the actual trigger for automatic posting.

### SAFE commands you CAN run:
```bash
storydump-cli list-queue             # Reading/inspection only
storydump-cli list-media
storydump-cli list-categories
storydump-cli list-users
storydump-cli check-health
storydump-cli instagram-status
storydump-cli list-instagram-accounts
storydump-cli google-drive-status
storydump-cli validate-image <path>
storydump-cli category-mix-history
storydump-cli sync-media
storydump-cli sync-status
storydump-cli backfill-instagram --dry-run
storydump-cli backfill-status
storydump-cli pool-health
storydump-cli dedup-media            # Dry-run by default (--apply mutates)
pytest                               # Tests - always safe
```

### Before ANY posting-related action:
1. **STOP** and ask the user for explicit confirmation
2. Explain exactly what will happen
3. Wait for user to type "yes" or approve

### Telegram Web (web.telegram.org):
- **NEVER type or click** — view/screenshot only
- All bot interactions must go through the database or user's phone

---

## Cloud Deployment (Railway + Neon)

- **Worker**: `python -m src.main` (scheduler + Telegram bot)
- **API**: `uvicorn src.api.app:app` (REST API + Mini App)
- **Database**: Neon PostgreSQL — connect via `psql "$DATABASE_URL"`
- Env vars are per-service — must set on both worker AND API
- **NEVER run on production**: `python -m src.main` (starts the JIT scheduler — see CRITICAL SAFETY RULES above), or mutating SQL on `posting_history`

---

## Project Overview

**Storydump** is a multi-tenant Instagram Story scheduling and automation system with Telegram-based team collaboration. A single deployment serves many independent teams ("Instances"), each scoped to its own Telegram group, media, queue, and schedule — see `PROJECT_MISSION.md` for the current product mental model (User → Instances → accounts/media/queue).

**Core Philosophy**: Phased delivery — 100% manual posting (Phase 1), optional Instagram API automation (Phase 2), multi-tenant rearchitecture (shipped post-v1.6.0), web dashboard (in progress). See `documentation/planning/phases/00_MASTER_ROADMAP.md` for current per-phase status — don't treat "Phase 3" as "web UI"; the phase docs number Phase 3 as Shopify integration and the dashboard as Phase 7 (currently in progress).

**Tech Stack**: Python 3.10+, FastAPI, Neon PostgreSQL, Telegram Bot, Railway deployment, Next.js (`landing/` — both the marketing site and the multi-tenant web dashboard).

## Architecture: STRICT SEPARATION OF CONCERNS

**CRITICAL** — each layer is strictly isolated. NEVER violate layer boundaries:

- **CLI/API** → calls Services (never touches Repositories or Models directly)
- **UI** → calls API (never calls Services directly)
- **Services** → orchestrate business logic, call Repositories
- **Repositories** → CRUD operations, return Models
- **Models** → database schema definitions only (no business logic)

## Essential Commands

```bash
# Development setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt && pip install -e .

# Common tasks
storydump-cli check-health
storydump-cli queue-preview
storydump-cli list-categories
storydump-cli update-category-mix

# Testing
pytest                          # All tests
pytest tests/src/services/      # Service tests only
pytest -m unit                  # Unit tests only
pytest --cov=src                # With coverage
```

## Feature Flags

```bash
ENABLE_INSTAGRAM_API=false  # Phase 1: Telegram-only (default)
ENABLE_INSTAGRAM_API=true   # Phase 2: Hybrid mode
```

When enabled, Instagram API is tried first. On failure or rate-limit, falls back to Telegram automatically. Posts are never lost.

## Pre-Commit & CI Requirements

**Run before every commit:**
```bash
source venv/bin/activate && ruff check src/ tests/ && ruff format --check src/ tests/ && pytest
```

**ALWAYS update CHANGELOG.md** when creating PRs — CI will fail without it. Use [Keep a Changelog](https://keepachangelog.com/) format, entries under `## [Unreleased]`.

## Documentation

- **Full docs**: `documentation/README.md`
- **All new docs** go in `documentation/` subdirectories (planning/, guides/, updates/, operations/, api/)
- **Bug fixes/patches**: Use dated filenames in `documentation/updates/` (e.g., `2026-01-04-bugfixes.md`)
- **Never** create markdown files scattered in source directories
