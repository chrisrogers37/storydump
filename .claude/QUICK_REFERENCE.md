# Storydump - Quick Reference for Claude

## ⚠️ CRITICAL SAFETY RULES

**NEVER run these commands** (they post to Instagram or modify production):
- `storydump approve <story>` (posts to Instagram)
- `storydump resolve <story> retry` (posts it again)
- `storydump cancel <story>` / `storydump resolve <story> cancel`
- `storydump tokens revoke <id>`
- `storydump webhook register` / `storydump webhook deregister` (the production bot's webhook)
- `storydump admit <email>` (admits a person and emails them a sign-up link)
- `python -m src.main` (starts the posting worker: the clock, the job lanes and the sweepers — it posts to Instagram)
- `python -m src.worker` (starts the posting worker: the clock, the job lanes and the sweepers — it posts to Instagram)
- `make run` / `make dev` (both run `python -m src.main`: the posting worker)
- `python -m scripts.migration_runner apply --manual <version>` (applies a `-- runner:manual` file by name — 001 and 002 are owed only by a database converted in place; a manual file is always the owner's)

The canonical list, with what each command does, is the safety block in
`CLAUDE.md`; `tests/test_agent_docs.py` fails when this copy
misses one of its `storydump` entries.

Not on that list does not mean safe: `storydump skip`, `reject`, `posted`,
`resolve <story> posted`, `pause`, `resume`, `sync`, `schedule`, `reschedule` and `link` also write through the
command port — a skip or a reject is final for that story. Ask first.

The same holds outside the CLI: the migration runner's `apply`, `adopt`,
`convert-ledger` and `repair`, the database `make` targets (`drop-db`,
`reset-db`, `db-restore`, `create-db`, `init-db`, `setup-db`, `quickstart`,
`db-shell`, `test`, `test-integration`), the schema builder and the database
gates run only against a local or throwaway database (`AGENTS.md` › Reading
this list correctly).

**SAFE commands** (read-only):
- `storydump whoami` / `storydump story <id>` / `storydump cards <id>` / `storydump floating` / `storydump account <handle>`
- `storydump jobs` / `storydump outbox` / `storydump burst` / `storydump posture` / `storydump planned`
- `storydump waitlist` (its rows are people's addresses: they never go into the repository)
- `storydump health` / `storydump doctor` / `storydump deploys` / `storydump webhook status`
- `pytest` (all tests; the database gates against a local or throwaway database only)

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
- The services own the SQL, under the unit of work; `src/models/target/` is the schema as code
- Writes go through the command port (`commands.py`); a transaction never spans a provider call

---

## Key Directories

| Path | Purpose |
|------|---------|
| `src/services/target/` | The target tier: lanes, jobs, the publish pipeline, views, executors |
| `src/api/` | The API (FastAPI): routes, auth, the command port |
| `src/channels/` | Telegram transport and webhook registration |
| `src/models/target/` | The schema as code: the models and `ddl/` |
| `scripts/migrations/` | Changes after the initial schema; `scripts/migration_runner.py` applies them |
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
connection:

```bash
storydump floating                 # approved stories waiting between attempts
storydump jobs --since 3h          # the queue by kind, lane and state
storydump account <handle>         # cap, zone, next slot, recent outcomes
storydump posture                  # the migration ledger, the role, RLS, the doors
```

A direct connection to the production database is the owner's alone: do not
open one. Say what you would read and leave the read to them.

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
| `src/services/target/telegram_dispatch.py` | Inbound Telegram: taps, `/start`, group joins, a group's move to a supergroup |
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

Every PR that touches code or config adds a fragment in `changelog.d/`, and no PR edits `CHANGELOG.md` — the format and CI's `changelog-check` rule are in `changelog.d/README.md`
