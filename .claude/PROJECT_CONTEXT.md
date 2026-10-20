# Storydump - Project Context

**Copy this into Claude web/phone sessions for context.** The canonical guide is
`AGENTS.md`; this page is the short version of it.

---

## What This Project Does

Storydump is a hosted, multi-tenant Instagram Story scheduling service. A
tenant is a **workspace**; people sign in on the web with Google and can link
Telegram.

1. A workspace connects Google Drive folders (`media_sources`); the worker syncs
   what is inside them into the media pool (`media_items`).
2. For each Instagram account the clock plans slots — the account's posts per
   day spread across its posting hours — and mints one story per slot
   (`post_intents`), drawn from the connected folders by the workspace's mix.
3. The workspace decides. An approval card goes to every Telegram chat bound to
   the workspace, and the same queue is on the web: approve (the worker
   publishes through the Instagram API), post by hand and mark it posted, skip,
   or reject.
4. Every write goes through one command port, and every state change is an
   audited row in the ledger. `storydump` reads that ledger from a terminal.

---

## Architecture (3-Layer)

```
┌─────────────────────────────────────┐
│  Interface Layer                    │
│  • storydump_cli/ - the storydump CLI│
│  • src/api/ - the API (FastAPI)     │
│  • src/channels/ - Telegram transport│
│  • landing/ - the web app (Next.js) │
└───────────────┬─────────────────────┘
                │
┌───────────────▼─────────────────────┐
│  Service Layer                      │
│  • src/services/target/             │
│    - work_loop, jobs, scheduler     │
│    - publish_pipeline, media_sync   │
│    - commands, command_executors    │
│    - outbox, prompts, ops_views     │
│    - telegram_dispatch, *_adapter   │
│  • src/worker.py - composition root │
└───────────────┬─────────────────────┘
                │
┌───────────────▼─────────────────────┐
│  Data Layer                         │
│  • SQL in the services, by hand,    │
│    under unit_of_work               │
│  • src/models/target/ - the schema  │
│    as code (models + ddl/)          │
│  • scripts/migrations/ + the runner │
│  • Neon PostgreSQL (cloud)          │
└─────────────────────────────────────┘
```

**RULE**: the CLI never imports `src` except `src/services/target/vocabulary.py`; SQL lives in the services under the unit of work; the API's routes call the services. There is one tier: the legacy tier was retired in September 2026, and its schema and snapshots are gone.

---

## Key Database Tables

Forty-four tables in `public`, every one under row-level security; thirty-three
are keyed on the workspace, the rest are the user plane, the machinery counters,
reference data and the owner's sign-up admissions (`.claude/rules/database.md` › Tenancy). The ones a
conversation usually needs:

| Table | Purpose |
|-------|---------|
| `workspaces` | The tenant, and its settings (`dry_run_mode`, `is_paused`, `api_publishing_enabled`, the schedule) |
| `workspace_members`, `users`, `user_identities` | Who belongs to a workspace, and how they sign in |
| `ig_accounts` | A workspace's Instagram accounts; the slot cursor (`next_slot_at`) and per-account schedule overrides |
| `media_sources`, `media_items` | Connected Drive folders and the media synced from them |
| `instagram_media_index`, `instagram_media_assets` | Archive posts and each postable file, including excluded unsupported children |
| `post_intents` | The ledger: one story, for one account, at one slot — its state and publish step |
| `audit_events` | Every state change, with who made it and through which channel |
| `jobs` | The worker's queue: kind, lane, lease |
| `channel_bindings`, `channel_outbox` | The Telegram chats bound to a workspace, and every message sent to them |
| `provider_operations` | One permit per Instagram container-create and publish call, written before the call |
| `oauth_credentials` | Encrypted Instagram and Drive grants |
| `service_tokens` | API tokens for the CLI (hashed) |

The full list and the rules for touching it: `.claude/rules/database.md`.

---

## Settings Resolution

Publishing, dry run, pause and the schedule are **per-workspace rows in the
ledger**, changed on the web (Settings) or through the command port — never
environment variables:

- `workspaces.dry_run_mode` — a dry run walks the whole flow and posts nowhere
- `workspaces.api_publishing_enabled` — off means the card offers "Posted
  myself" and `approve` is refused as `manual_mode`
- `workspaces.is_paused` — `storydump pause` / `storydump resume`; a paused
  workspace plans no slots
- `ig_accounts.posts_per_day`, posting hours, `tz` — per-account overrides; NULL
  inherits the workspace

Environment variables configure the DEPLOYMENT (`TARGET_DATABASE_URL`, the bot
token and webhook secret, the Cloudinary trio, the Google client). `.env.example`
names every one something reads.

---

## Key Files

| File | Purpose |
|------|---------|
| `src/worker.py` | The worker's composition root (lanes, the clock, the publish pipeline) |
| `src/api/app.py` | The API (FastAPI): the command port, the ops views, health |
| `src/services/target/commands.py` | The command port: the closed vocabulary and the role floors |
| `src/services/target/command_executors.py` | The command port's verbs |
| `src/services/target/unit_of_work.py` | The tenant-scoped transaction every query runs under |
| `src/services/target/work_loop.py` | The lanes and the job registry |
| `src/services/target/scheduler.py` | The clock and the slots |
| `src/services/target/publish_pipeline.py` | Publishing a story to Instagram |
| `src/services/target/media_sync.py` | Drive sync into the media pool |
| `src/services/target/prompts.py`, `outbox.py` | The approval card and its delivery record |
| `src/services/target/telegram_dispatch.py` | Inbound Telegram: taps, `/start`, group joins, a group's move to a supergroup |
| `src/channels/telegram_transport.py` | The Telegram HTTP transport |
| `src/services/target/ops_views.py` | The ledger read views the CLI shows |
| `src/services/target/vocabulary.py` | The closed vocabularies and the CLI's wire contract |
| `storydump_cli/main.py` | The `storydump` CLI |
| `src/models/target/ddl/` | The schema builder: roles, functions, triggers, policies, grants |
| `scripts/migration_runner.py` | The migration runner (every deploy's predeploy) |

---

## Safety Rules

**NEVER suggest running:**
- `storydump approve <story>` (posts to Instagram — the user's decision)
- `storydump resolve <story> retry` (posts it again)
- `storydump cancel <story>` / `storydump resolve <story> cancel` (destructive)
- `storydump tokens revoke <id>`
- `storydump webhook register` / `storydump webhook deregister` (the production bot's webhook)
- `storydump admit <email>` (admits a person and emails them a sign-up link — the owner's decision)
- `python -m src.main` (starts the posting worker: the clock, the job lanes and the sweepers — it posts to Instagram)
- `python -m src.worker` (starts the posting worker: the clock, the job lanes and the sweepers — it posts to Instagram)
- `make run` / `make dev` (both run `python -m src.main`: the posting worker)
- `python -m scripts.migration_runner apply --manual <version>` (applies a `-- runner:manual` file by name — 001 and 002 are owed only by a database converted in place; a manual file is always the owner's)

The canonical list is the safety block in `CLAUDE.md`; `tests/test_agent_docs.py` fails when this copy
misses one of its `storydump` entries.

Not on that list does not mean safe: `storydump skip`, `reject`, `posted`,
`resolve <story> posted`, `pause`, `resume`, `sync`, `schedule`, `reschedule` and `link` also write through the
command port, and a skip or a reject is final for that story. Ask before
suggesting any of them.

The same holds outside the CLI: the migration runner's `apply`, `adopt`,
`convert-ledger` and `repair`, the database `make` targets (`drop-db`,
`reset-db`, `db-restore`, `create-db`, `init-db`, `setup-db`, `quickstart`,
`db-shell`, `test`, `test-integration`), the schema builder and the database
gates run only against a local or throwaway database (`AGENTS.md` › Reading
this list correctly).

**SAFE to suggest:**
- `storydump whoami` / `storydump story <id>` / `storydump cards <id>` / `storydump floating` / `storydump account <handle>` (reads)
- `storydump jobs` / `storydump outbox` / `storydump burst` / `storydump posture` / `storydump planned` (reads)
- `storydump waitlist` (a read; its rows are people's addresses, which never go into the repository)
- `storydump health` / `storydump deploys` / `storydump doctor` / `storydump webhook status` (the deployment; nothing changes)
- `pytest tests/` (its database gates against a local or throwaway database only)

---

## State of the System

- The target tier is the only tier. Version: `src/__init__.py`; changes:
  `CHANGELOG.md` under `## [Unreleased]`, and the fragments in `changelog.d/`
  not yet folded into it.
- **Outbound email sends only where it is configured**: Resend, ratified
  2026-10-11. An invitation is emailed from the API after it commits, when the
  API has `RESEND_API_KEY` and `EMAIL_FROM`. Its response says whether it was
  sent, and it always carries the link, shown once
  (`AGENTS.md` › What is deliberately not wired).
- Some vocabulary commands have no executor yet and answer 501
  (`commands.UNBUILT`); one job kind has none (`work_loop.UNBUILT_KINDS`).
- Production moved on 2026-10-17 to a database built from schema code, and
  the legacy schema and its snapshots are gone
  (`documentation/operations/schema-cutover.md`). An agent never applies a
  `-- runner:manual` file.

---

## Common Patterns

**Adding a workspace setting:**
1. A migration file adds the column to `workspaces` (next number, with
   postconditions) and the model in `src/models/target/` changes in the same
   PR; the Schema consistency job fails when the two disagree
2. Add the key to `workspaces.SETTINGS_COLUMNS` so `settings_change` may set it
3. Surface it on the web's Settings page

**Adding a write:**
1. Name it in the vocabulary (`vocabulary.COMMANDS`, pinned to the architecture
   document) and give it a floor in `commands.ROLE_FLOOR`
2. Write the executor in `command_executors.py` — read the row `FOR UPDATE`,
   decide from that read, let the database refuse what is illegal
3. If the CLI should expose it, add the verb to
   `storydump_cli/commands/writes.py` and classify it for the safety block
   before it ships

**Testing:**
- Unit tests live in `tests/src/services/target/` and script their executors
- The DB gates under `tests/scripts/` run against a scratch schema and need a
  real PostgreSQL (`AGENTS.md` › Testing); set `REQUIRE_TEST_DATABASE=1` when a
  green result will be reported
- Run with `pytest tests/ -v`
