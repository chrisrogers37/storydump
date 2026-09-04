# AGENTS.md

Guidance for any coding agent working in this repository. This is the
vendor-neutral file the wider tool ecosystem reads; `CLAUDE.md` carries the
Claude Code specifics and defers to this document for everything shared, so the
two cannot disagree about the substance.

**The safety rules below are not advisory.** This system posts to Instagram and
Telegram on behalf of paying tenants.

---

## CRITICAL SAFETY RULES

**THIS SYSTEM POSTS TO INSTAGRAM. DO NOT TRIGGER POSTING WITHOUT EXPLICIT USER APPROVAL.**

### NEVER run these

```bash
python -m src.main                   # Starts the posting scheduler + Telegram bot
storydump-cli reset-queue            # Mutates the posting queue
storydump-cli instagram-auth         # Mutates stored authentication
storydump-cli revoke-tokens          # Destroys stored OAuth tokens for a service
storydump-cli rotate-keys            # Re-encrypts every stored token row
```

### Before ANY posting-related action

1. **STOP** and ask for explicit confirmation.
2. Explain exactly what will happen.
3. Wait for an affirmative answer.

### Telegram Web (web.telegram.org)

- **NEVER type or click** — view and screenshot only.
- All bot interactions go through the database or the user's own device.

### Production

Never run against production: the posting scheduler, or mutating SQL on
`posting_history`.

### Reading this list correctly

It names commands that are **unambiguously** destructive. It is not a complete
read-only/read-write taxonomy: several inspection-flavoured commands do write
(`index-media` writes the media index, `sync-media` pulls from Drive, and
`dedup-media` mutates under `--apply` though it is dry-run by default). When a
command is not on this list, check what it does before running it rather than
inferring that absence means safe.

---

## Project overview

**Storydump** is a hosted, multi-tenant Instagram Story scheduling and
automation service with Telegram-based team collaboration. One deployment serves
many tenants, and the operator and a tenant are different parties — which is why
per-tenant isolation is a product requirement rather than a hardening
preference.

**Tech stack:** Python 3.10+, FastAPI, PostgreSQL (Neon in cloud), Telegram Bot,
Railway deployment, Next.js `landing/` site.

## Architecture: strict separation of concerns

Each layer is isolated. Do not violate the boundaries:

- **CLI / API** → call Services (never Repositories or Models directly)
- **UI** → calls the API (never Services directly)
- **Services** → orchestrate business logic, call Repositories
- **Repositories** → CRUD, return Models
- **Models** → schema definitions only, no business logic

### The command port

Writes in the target tier go through one closed vocabulary
(`src/services/target/commands.py::VOCABULARY`, 25 commands). The web adapter
exposes them as a single route — `POST /workspaces/{ws}/commands/{command}` —
whose path segment is validated against that vocabulary, so the route table
cannot drift from it. `create_workspace` is the one exception and has its own
route.

Two consequences worth knowing before reasoning about reach:

- **Every built command in the vocabulary is reachable over the web API**,
  subject to its role floor (`FLOORS`, per-command). There is no separate
  web-exposed subset.
- A vocabulary command with no executor yet is a **named refusal**, not an
  absent name: it answers `CommandNotBuilt`, rendered `501`. Read the current
  unbuilt set from `commands.UNBUILT`, which is *derived* from the registry and
  pinned by `tests/src/services/target/test_commands.py` so it can only shrink
  deliberately — not from a list here, which would be stale the first time
  someone builds one.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt && pip install -e .
```

The repo's `Makefile` targets assume `./venv/`. (`.venv/` is also gitignored, so
a local one will not be committed, but the Makefile will not find it.)

`src/config/settings.py` requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`
and `ADMIN_TELEGRAM_CHAT_ID` **even for the web service, the CLI and the tests**
— every entry point loads settings. Dummy values are sufficient for anything
that is not the worker. Tests additionally need `ENCRYPTION_KEY`, a Fernet key.

`.env`, `.env.test` and `landing/.env.local` are gitignored and never committed.

## Commands

```bash
storydump-cli check-health
storydump-cli queue-preview
storydump-cli list-categories
storydump-cli update-category-mix
```

31 commands are registered; `storydump-cli --help` is the authoritative list.

## Testing

```bash
pytest                          # full suite (~4200 tests)
pytest tests/src/services/      # one area
pytest -m unit                  # unit tests only
pytest --cov=src                # with coverage
```

The suite needs a real PostgreSQL. CI sets `REQUIRE_TEST_DATABASE=1` so that a
database that fails to come up produces failures rather than silent skips — a
green run that skipped its integration coverage is the failure mode that guard
exists to prevent. Set it locally too when a green result is going to be
reported anywhere.

To run the database-gated suites on a laptop, give them a throwaway server
shaped like CI's and a `psql` on your PATH — the fixtures apply migrations
through it, and the Homebrew `postgresql@15` keg is enough:

```bash
docker run -d --name storydump-test-pg -p 65433:5432 \
  -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password \
  -e POSTGRES_DB=storyline_test postgres:15
PATH="/opt/homebrew/opt/postgresql@15/bin:$PATH" \
DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password \
DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 \
  pytest tests/scripts/
```

## Services

- **Worker:** `python -m src.main` (scheduler + Telegram bot) — see the safety rules.
- **API:** `uvicorn src.api.app:app` → health at `GET /health`, schema at `/openapi.json`.
- **Landing / dashboard:** `npm --prefix landing run dev` → http://localhost:3000;
  the BFF proxies to `BACKEND_URL`.

Dashboard routes under `/api/onboarding/*` require Telegram WebApp `init_data`
(HMAC-signed with `TELEGRAM_BOT_TOKEN`) plus an active membership for the chat.

Environment variables are per-service in cloud deployment — set them on **both**
the worker and the API.

## What is deliberately not wired

**Outbound email does not send.** `src/services/target/email_sender.py` ships
inert by design: `sender_from_env` returns `None` when `RESEND_API_KEY` and the
sender address are absent, and the job registry parks `send_email` with a reason
naming what is missing. The provider choice is a flagged decision that has not
been ratified, and deferring it is deliberate. An invitation created today
therefore reports `delivery: {"channel": "email", "state": "not_configured"}` —
the row and its token are real, the message is never delivered.

Do not describe email as working, and do not wire a provider without the owner
acknowledgement the design calls for.

`ENABLE_INSTAGRAM_API` is a **per-workspace database setting**
(`chat_settings.enable_instagram_api`, default `false`), not an environment
variable — `settings.py` does not read it. `.env.example` still lists it, which
is stale.

## Pre-commit and CI

```bash
source venv/bin/activate && ruff check . && ruff format --check . && pytest
```

**Always update `CHANGELOG.md`** when opening a PR — CI fails without it.
[Keep a Changelog](https://keepachangelog.com/) format, entries under
`## [Unreleased]`.

## Documentation

- Full docs: `documentation/README.md`
- New docs go in `documentation/` subdirectories (`planning/`, `guides/`,
  `updates/`, `operations/`, `cloudinary/`)
- Bug fixes and patches: dated filenames in `documentation/updates/`
- **Never** scatter markdown files through source directories
