# Storydump - Instagram Story Automation System

A hosted, multi-tenant Instagram Story scheduling service with Telegram-based team collaboration.

## Features

- **Scheduling per workspace**: posts per day, a posting-hours window and a timezone (`workspaces.posts_per_day`, `posting_hours_start`/`_end`, `tz`); an account may override them. The worker's clock mints one slot per account as it falls due.
- **Media from Google Drive**: a workspace connects Drive once and picks folders under that grant; everything inside a connected folder syncs, and the posting mix weights how often each connected folder posts (`src/services/target/category_mix.py`).
- **Approval on Telegram and on the web**: a slot that finds eligible media becomes a story awaiting approval — a card in each bound Telegram chat (Post now · Posted myself · Skip · Reject · Open Instagram) and a row in the web Queue. A slot that finds none tells the bound chats, at most once a day per account (`src/services/target/scheduler.py`).
- **Manual or API publishing, per workspace**: with `api_publishing_enabled` off a person posts and taps *Posted myself*; with it on, *Post now* runs the publish pipeline (Drive → Cloudinary → the Instagram Graph API). Dry run and pause are workspace settings too.
- **Locks**: a posted item is locked for that account for the repost TTL (30 days by default), a skipped one for the skip TTL (45 days by default), a rejected one permanently (`post_locks`).
- **Audit trail**: every state change of a story writes an `audit_events` row by trigger, naming who acted and through which channel; `storydump story <intent_id>` reads the timeline.
- **Tenancy**: workspaces with members and roles (owner, admin, member). Tenant tables carry row-level-security policies keyed on the workspace (migration 058); whether the deployed login is subject to them is what `storydump posture` reports (`documentation/operations/runtime-database-roles.md`).
- **A console**: the `storydump` CLI reads the ledger and writes through the same command port the card and the web use.

## How it works

1. A person signs in with Google on the web, creates a workspace, connects an Instagram account (Instagram Login) and Google Drive, and binds a Telegram group.
2. The worker syncs the connected folders into `media_items`.
3. The elected clock ticks in the database (`fn_clock_tick`): for each account whose next slot is due it mints a `plan_slot` job and advances the slot cursor. The job picks a media item and inserts a `post_intents` row in `scheduled`.
4. The prompt sweep moves the story to `awaiting_approval` and writes one approval card per bound chat into `channel_outbox`; the outbox sender delivers them.
5. A member acts — on the card, in the web Queue, or with a `storydump` write verb. All three hand the same command to the command port (`src/services/target/commands.py`).
6. An approval in an API-publishing workspace enqueues the `publish_pipeline` job: a checkpointed ladder (transit upload, container, poll, publish) in which no database transaction spans a provider call.

There is one tier. The legacy tier was retired in the tear-out (#1216, September 2026); its data survives as the `archive.*_pre_cutover_20260917` snapshots. [`AGENTS.md`](AGENTS.md) carries the architecture and the layer boundaries.

Where it runs: the worker and the API are two Railway services from this repository (`Procfile`, `railway.toml`), the database is Neon PostgreSQL, and the web front end in `landing/` deploys to Vercel — `documentation/guides/cloud-deployment.md`. Python 3.10 or newer (`setup.py`; CI runs 3.10).

## Local Development Setup

These steps stand up a development environment for working on storydump. They
are not a deployment guide — the service runs as one hosted deployment we
operate, and tenants are provisioned on it rather than installing their own.

**Read the safety rules in [`AGENTS.md`](AGENTS.md) first.** The worker posts to
Instagram for whatever database it is pointed at; never point a local process at
production.

### 1. Installation

```bash
# Clone repository
git clone <your-repo-url>
cd storydump

# Create virtual environment (the Makefile's targets assume ./venv/)
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install the package and the `storydump` CLI (the `cli` extra)
pip install -e '.[cli]'
```

`make install` runs the two `pip` lines.

### 2. Configuration

```bash
cp .env.example .env    # or: make env-example
```

`.env.example` lists every variable the code reads, with what each one does.
No variable is required to load settings; a process needs what it reads:

- `TARGET_DATABASE_URL`: the database the API and the worker run against (the runtime login). The worker exits 2 without it; the API answers 503 on every data route.
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`: what `make create-db`, `make init-db` and the test harness connect with. No deployed process reads them.
- `DATABASE_URL`: the database-owner login the migration runner applies the schema with. Locally `make init-db` builds it from the `DB_*` components.
- `ENCRYPTION_KEY`: a Fernet key for the stored credentials (the test suite needs one too).
- `TARGET_TELEGRAM_BOT_TOKEN` and `TARGET_TELEGRAM_BOT_USERNAME`: the bot the worker sends with. Without them the worker runs with its Telegram channel parked.
- `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`: all three, or the publish kind parks by name.
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`: the OAuth client a Drive grant is minted and refreshed with.

The code loads only the `Settings` fields from `.env`. The run-time reads —
`TARGET_DATABASE_URL`, the bot token, the worker's knobs — come from the process
environment: `make run` exports `.env` for you, a bare `python -m src.main` or
`uvicorn` does not.

### 3. Database Setup

```bash
# Create the database, then build the schema on it
make create-db init-db      # the same as: make setup-db
```

`init-db` builds the schema the way the lineage lane proves it: step 0 (the
`svc_*` service roles — they are cluster-wide, so `DB_USER` needs `CREATEROLE` —
and the DDL door), the by-hand base, then every file under `scripts/migrations/`
through the migration runner (`python -m scripts.migration_runner apply`). It
needs `psql` on your `PATH`.

The runner replays the whole lineage, so a fresh database also holds the
replayed `legacy` schema and its empty `archive` snapshots: migrations 079 (the
drop of `legacy`) and 080 are gated (`-- runner:manual`), so `apply` reports
them as owed and never runs them. Nothing under `src/` reads either schema.
Production is past that point — the owner applied 079 and 080 by hand on
2026-09-19 — so "owed" on a fresh laptop is expected, not a defect.
`make validate-env` loads the settings the way every process does, and
`make check-health` asks the deployed API.

### 4. Run the API and the worker

The `Procfile` names the two processes.

```bash
# The API (the Procfile's `web` line): GET /health, /openapi.json, /api/v1
export TARGET_DATABASE_URL=postgresql://<user>@localhost:5432/<database>   # the value in your .env
uvicorn src.api.app:app --port 8000

# The worker (the Procfile's `worker` line), in the foreground. `make run`
# exports `.env` into the process; a bare `python -m src.main` does not read it
# and refuses to boot without TARGET_DATABASE_URL in its environment.
# For an agent this IS `python -m src.main`, which is on the never-run list: ask first.
make run
```

The web front end is `npm --prefix landing run dev` (http://localhost:3000); it
calls the API at `TARGET_API_URL` (`BACKEND_URL` is the fallback when that is
unset; `landing/.env.local.example`).

### 5. Connect media and set the schedule on the web

Media comes from a connected Google Drive folder (Settings › Integrations) and
the schedule from the Posting Schedule card under Settings › General. From then
on the worker mints a slot for each account as it falls due and asks for
approval in the bound Telegram chats and the web Queue.

## The `storydump` CLI

`pip install -e '.[cli]'`, mint a token under Settings › API tokens, then:

```bash
storydump login                        # the token from the prompt or stdin
storydump whoami
storydump floating --watch             # approved stories waiting to post
storydump story <intent_id>            # one story's whole timeline
storydump burst --since 3h             # what the last burst did
storydump skip <story> --workspace <ws>
storydump health
storydump deploys --watch
storydump doctor
```

`storydump --help` lists every verb by section (auth, reads, writes,
environment); `documentation/operations/reading-the-ledger.md` is the guide.
Everything goes over HTTP — the storydump API, and for `deploys`, `doctor` and
`webhook` your own Railway login and Telegram's Bot API; the CLI never opens a
database.

## Telegram

The product runs one bot. It is an adapter of the API, not a command console:

- **`/start` links.** The web mints the deep links, and the payload's prefix
  decides what `/start` does: `link-…` links your Telegram identity to your
  account, `bind-…` (opened through "add to group") binds that group to a
  workspace. Those two are the registered lanes
  (`src/services/target/telegram_dispatch.py::build_router`); any other prefix
  is refused — named in the log, unanswered in the chat — and a bare `/start`
  in a DM is greeted. An invitation is accepted on the web (`/join/<token>`),
  not in the chat.
- **The approval card.** Each story awaiting approval is a card in every bound
  chat: **Post now** (only where API publishing is on), **Posted myself**,
  **Skip**, **Reject**, and an *Open Instagram* link. A tap is executed as the
  tapping member through the command port; the card is then edited to its
  outcome. A story parked for review gets its own card: *It posted*, *Not there
  — post again*, *Give up* (`src/services/target/prompts.py`).
- **Group membership.** A person with a linked Telegram identity who speaks in,
  or is added to, a bound group becomes a member of that group's workspace;
  leaving the group removes nothing (`src/services/target/membership_sync.py`).

Typed commands are not served: a `/pause` or an "approve" typed in a chat is not
dispatched (#854). Pause, resume, the schedule and the queue live on the web and
in the `storydump` verbs.

Telegram delivers to the API's webhook (`POST /webhooks/telegram`); the worker
only sends. `documentation/operations/telegram-webhook.md` covers registration.

## Development

### Running Tests

The suite runs against a local PostgreSQL. Without one, the root suite's
database tests skip and the gates under `tests/scripts/` ERROR at their first
fixture — leave them out (`--ignore=tests/scripts`) for a no-database run. CI sets `REQUIRE_TEST_DATABASE=1` so a database
that fails to come up is a failure, never a silent skip; [`AGENTS.md`](AGENTS.md)
has the throwaway-server recipe.

```bash
# Everything (about 3,700 tests; pytest.ini turns coverage on)
pytest

# One area
pytest tests/src/services/

# Without coverage (faster)
pytest --no-cov

# The tests marked `unit` / `integration` — marked subsets, not the whole of either kind
pytest -m unit
pytest -m integration
```

### Project Structure

```
storydump/
├── src/                    # The API and the worker
│   ├── api/               # FastAPI: app.py, principal.py, the OAuth clients, routes/ (auth, v1, tokens, ops, webhooks, meta, retired)
│   ├── channels/          # Telegram transport and webhook registration
│   ├── config/            # settings.py (no variable is required), constants, defaults
│   ├── exceptions/        # Typed errors: base, identity, telegram, tenancy
│   ├── models/target/     # Declarative models (schema parity with the migrations, not an ORM)
│   ├── services/target/   # The tier: the command port, the ledger, jobs, the clock, the publish pipeline, the outbox, the provider adapters, the read views
│   ├── utils/             # datetime_utils, encryption, logger
│   ├── worker.py          # The worker's composition root
│   └── main.py            # The worker entrypoint (dispatches to worker.py)
├── storydump_cli/         # The `storydump` console (an HTTP client of the API)
├── landing/               # The Next.js site and dashboard (deployed on Vercel)
├── scripts/               # migration_runner.py, migrations/, window/, the fleet monitors, the gates
├── tests/                 # src/, scripts/ (the DB gates), storydump_cli/, mutations/
├── documentation/         # Guides, runbooks, plans, archive
├── docs/                  # GitHub Pages static files (the Instagram deep-link redirect) — not documentation
├── Procfile               # worker: python -m src.main · web: uvicorn src.api.app:app
├── railway.toml           # preDeployCommand: the migration runner's `apply`
└── Makefile               # install, create-db, init-db, run, test, …
```

## Documentation

📚 **[Complete Documentation Index](documentation/README.md)**

Key resources:
- **[AGENTS.md](AGENTS.md)** - The canonical developer and agent guide: safety rules, architecture, the command port, setup, commands, testing, services
- **[CLAUDE.md](CLAUDE.md)** - Claude Code specifics, plus the safety rules repeated
- **[Quick Start Guide](documentation/guides/quickstart.md)** - Start here: using, developing, or operating Storydump
- **[Reading the ledger](documentation/operations/reading-the-ledger.md)** - The `storydump` read and write verbs
- **[Deployment Guide](documentation/guides/deployment.md)** - Production deployment checklist
- **[Testing Guide](documentation/guides/testing-guide.md)** - How to run and write tests
- **[Consolidated design plan](documentation/planning/2026-08-02-consolidated-design-plan/README.md)** - The authoritative multi-tenant plan (ratified, in execution; per-increment status in its *Live status*)

## License

MIT — see [`LICENSE`](LICENSE).

## Support

For issues and questions, please open a GitHub issue.
