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
python -m scripts.migration_runner apply --manual <version>   # Applies a gated file: 079 DROPS the legacy schema — the owner runs the window (F7)
storydump approve <story>            # Posts a story to Instagram — the user's decision, never an agent's
storydump cancel <story>             # Cancels a story: refunds its debit, destroys its upload
storydump resolve <story> cancel     # Gives up on a story parked for review; its debit is retained
storydump resolve <story> retry      # Posts the story again; --not-posted overrides the lost-answer guard
storydump tokens revoke <id>         # Revokes an API token; it stops working at once
storydump webhook register           # Re-points the production bot's webhook; --drop-pending discards queued taps
storydump webhook deregister         # Detaches the bot's webhook — Telegram delivers nothing until it is registered again
```

### Before ANY posting-related action

1. **STOP** and ask for explicit confirmation.
2. Explain exactly what will happen.
3. Wait for an affirmative answer.

### Telegram Web (web.telegram.org)

- **NEVER type or click** — view and screenshot only.
- All bot interactions go through the database or the user's own device.

### Production

Never run against production: the posting scheduler, or mutating SQL against
the ledger — `post_intents`, `jobs`, `channel_outbox` and every other table
`src/models/target/` declares — or any `archive` snapshot.

### Reading this list correctly

It names what posts, destroys, or re-points the bot. It is not a complete
read-only/read-write taxonomy: the other write verbs (`skip`, `reject`,
`posted`, `pause`, `resume`, `sync`) change the ledger through the command port
too — a skip or a reject is a terminal state for that story — and every one of
them is a posting-related action under the STOP rule above. The read verbs,
`health`, `deploys` and `doctor` read only, and `webhook status` changes nothing
(its door check is an empty POST the API refuses by design). When a verb is
not on this list, check what it does before running it rather than inferring
that absence means safe.

---

## Project overview

**Storydump** is a hosted, multi-tenant Instagram Story scheduling and
automation service with Telegram-based team collaboration. One deployment serves
many tenants, and the operator and a tenant are different parties — which is why
per-tenant isolation is a product requirement rather than a hardening
preference.

**Tech stack:** Python 3.10+, FastAPI, PostgreSQL (Neon in cloud; hand-written
SQL on asyncpg), the Telegram Bot API over HTTP (no bot framework), the
Instagram Graph API, Google Drive, Cloudinary, Railway deployment, Next.js
`landing/` site on Vercel.

## Architecture: one tier, strict separation of concerns

There is one tier. The design plan and the package names call it the *target*
tier (`src/services/target/`, `src/models/target/`) because it was built beside
the tier it replaced. The legacy tier was retired in the tear-out (#1216,
September 2026); its data survives as the `archive.*_pre_cutover_20260917`
snapshots (migration 078), and the `legacy` schema itself is dropped by the
gated 079 in the owner's window
(`documentation/operations/legacy-window-close.md`).

Each layer is isolated. Do not violate the boundaries:

- **CLI** (`storydump_cli/`) → calls the API over HTTP, never a database. The
  one module it imports from `src` is `src/services/target/vocabulary.py`
  (`tests/storydump_cli/test_import_boundary.py` pins that in a fresh
  interpreter).
- **UI** (`landing/`) → calls the API through its server-side client
  (`landing/src/lib/target-api.ts`), never a service. The one table it owns
  is the marketing waitlist (`landing/src/lib/schema.ts`, Drizzle), which no
  Python migration manages.
- **API** (`src/api/`) → authenticates a principal (`src/api/principal.py`:
  a web session or an API token), then calls a module under
  `src/services/target/`. Reads are resources; state changes are commands
  (below).
- **Worker** (`src/worker.py`, the composition root `src/main.py` dispatches
  to) → runs the same services as jobs: the elected clock (`scheduler.py`),
  the two claim lanes `interactive` and `bulk` over the kind→executor registry
  (`work_loop.py`, `jobs.py`), the publish pipeline (`publish_pipeline.py`)
  and the outbox sender (`outbox.py`). A kind whose seam the deployment lacks
  is parked by name, never run against a fake.
- **Services** (`src/services/target/`) → hand-written SQL under the
  tenant-scoped unit of work (`unit_of_work.py`; unconstructible without a
  tenant, it sets the `app.tenant_id` the RLS policies of migration 058
  read). Cross-tenant work goes through the database's `SECURITY DEFINER`
  doors (migration 059 onward), never a privileged session. A transaction
  never spans a provider call: write the checkpoint, commit, then call Meta,
  Telegram, Drive or Cloudinary through the egress floor (`egress.py`).
- **Channels** (`src/channels/`) → the Telegram transport and the webhook
  registration. Only the two composition roots import them (`src/worker.py`,
  `src/api/app.py`); a service receives the transport as an injected callable.
- **Models** (`src/models/target/`) → schema definitions only, kept for parity
  with the migrations (`scripts/migrations/`, applied by
  `scripts/migration_runner.py`). They are not an ORM: nothing under `src/` or
  `storydump_cli/` imports them.

**The database is the authority.** The legal edges of a story's state are rows
of `post_intent_transitions` and a trigger refuses the rest (migration 055;
`intent_ledger.py` issues the UPDATE and translates the refusal), one live
lease per serialization key is a unique index on `jobs` (056), and every state
change of a story writes its own `audit_events` row by trigger, which refuses
an anonymous one. Do not add a Python pre-check that copies one of these: a
second authority is how the two drift.

### The command port

State changes go through one closed vocabulary
(`src/services/target/commands.py::VOCABULARY`, re-exported from
`vocabulary.py::COMMANDS` — 26 commands on 2026-09-18). The web adapter exposes
them as a single route — `POST /api/v1/workspaces/{ws}/commands/{command}`
(`src/api/routes/v1.py`) — whose path segment is validated against that
vocabulary, so the route table cannot drift from it. `create_workspace` is the
one exception and has its own route (`POST /api/v1/workspaces`). Three adapters
hand the port the same `Command`: a web click and a `storydump` write verb
through that route (admission channels `web` and `cli`), and a tap on a
Telegram card through the webhook
(`src/services/target/telegram_dispatch.py::TelegramDispatcher._tap`).

What is NOT a command is a resource with its own route: adding a destination
or a Drive folder, the Drive grant, the category mix, API tokens, accepting an
invitation (`src/api/routes/v1.py`, `src/api/routes/tokens.py`).

Two consequences worth knowing before reasoning about reach:

- **Every built command in the vocabulary is reachable over the web API**,
  subject to its role floor (`commands.ROLE_FLOOR`, per command, over the
  ladder `FLOORS`). There is no separate web-exposed subset. The `operator`
  floor has no principal behind it yet (#1124), so a command on that floor is
  refused as a role refusal for every caller.
- A vocabulary command with no executor yet is a **named refusal**, not an
  absent name: it answers `CommandNotBuilt`, rendered `501`. Read the current
  unbuilt set from `commands.UNBUILT`, which is *derived* from the registry and
  pinned by `tests/src/services/target/test_commands.py` so it can only shrink
  deliberately — not from a list here, which would be stale the first time
  someone builds one.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt && pip install -e '.[cli]'
```

The repo's `Makefile` targets assume `./venv/`. (`.venv/` is also gitignored, so
a local one will not be committed, but the Makefile will not find it.)

`src/config/settings.py` requires **no variable** (the tear-out's phase 02;
#1222): every field has a default, so the web service, the tests and the
`storydump` CLI load with an empty environment. A process needs what it reads:
the worker refuses to boot without `TARGET_DATABASE_URL` (exit 2, naming it),
the API answers 503 on every data route without it, and the CLI imports only
`src/services/target/vocabulary.py` and needs no variable but its token. Tests
additionally need `ENCRYPTION_KEY`, a Fernet key. `.env.example` names every
variable something reads, and a test fails if it names one nothing does.

`.env`, `.env.test` and `landing/.env.local` are gitignored and never committed.

## Commands

```bash
storydump whoami
storydump floating --watch
storydump story <intent_id>
storydump skip <story> --workspace <ws>
storydump health
storydump doctor
```

`storydump --help` is the authoritative list, grouped by auth, reads, writes and
environment; the section below walks it.

## The `storydump` CLI (v2)

`storydump` is the developer and agent console over the API — a pure HTTP
client, never a database connection
(`documentation/planning/2026-09-15-cli-v2/`). Install it with
`pip install -e '.[cli]'`.

1. Mint a token on the web: **Settings › API tokens**. A person-bound token
   acts as you in every workspace you belong to, never above your role; the
   secret is shown once.
2. `storydump login` — paste the secret at the prompt (or pipe it on stdin).
   It is kept in the OS keychain; `--insecure-storage` writes a 0600 file
   instead; agents and CI set `STORYDUMP_TOKEN` and never run `login`.
3. `storydump whoami`, `storydump tokens list`, `storydump tokens revoke <id>`,
   `storydump logout`. `--json` on any verb prints one envelope
   `{"v": 1, "kind", "data", "error"}`. Exit codes: 0 ok · 1 not found ·
   2 refused · 3 not authorized · 4 API unreachable or failed · 5 Railway
   unreachable · 6 a watched condition ended in failure · 64 usage. `STORYDUMP_API` overrides the API URL
   (default `https://api.storydump.app`).
4. Read the ledger (every workspace you belong to, or `--workspace <id or name>`;
   `--json`; `--watch [--every 30]` prints only changes):
   `storydump story <intent_id>` (the timeline: audit rows, provider
   operations, cards) · `storydump cards <intent_id>` · `storydump floating`
   (approved stories waiting between attempts, with their retry job) ·
   `storydump account <handle>` (cap, zone, next slot, today's bucket, recent
   outcomes) · `storydump jobs --since 3h` · `storydump outbox --since 3h` ·
   `storydump burst --since 2026-09-15T14:50:00Z` (taps, permits, float
   waits, siblings, review cards, outcomes) · `storydump posture` (the
   migration ledger, the role, RLS, the doors). The guide:
   `documentation/operations/reading-the-ledger.md`.
5. Write through the command port — the same door a tap or a web click uses,
   so admission, tenancy and audit apply unchanged. `--workspace <id or name>`
   is required (a write goes to ONE workspace). A story verb's idempotency key
   is deterministic (`<command>:<story>`, the web's), so a re-run replays as
   "already done" (exit 0) and `--idempotency-key <k>` is the deliberate second
   execution; a resolution's key carries the review episode, so a later review
   of the same story is new; `pause`, `resume` and `sync` mint a fresh key per
   invocation (their effects are idempotent — a retry is harmless, a later
   action always executes):
   `storydump approve|skip|reject|posted|cancel <story>` ·
   `storydump resolve <story> retry|posted|cancel [--not-posted]` ·
   `storydump pause` / `storydump resume` · `storydump sync <source_id>`. A
   refusal is an answer, not a failure: the reason's sentence, the fixing
   verb, exit 2. The Telegram adapter's words never appear in a terminal.
6. The environment: `storydump health` (the API's three health surfaces,
   judged by the fleet monitors' own verdicts — the `classify` of
   `scripts/scheduling_monitor.py` and `scripts/posting_monitor.py`, imported:
   not well when a monitor would page; plus the bot's webhook from `/health`;
   exit 4 then, the report and each verdict still printed. Two bounds against
   the pollers: one reading has no watch clock, and one unreachable reading
   is reported where the pollers wait for two) · `storydump deploys
   [--watch --commit <sha> --timeout <s>]` (the latest deployments on Railway
   through your own `railway` login, with the linked project checked first;
   exit 5 with the fix when Railway cannot be read; `--watch` ends 0 on both
   SUCCESS, 6 on FAILED, CRASHED or REMOVED, or when `--timeout` runs out;
   `--commit` takes any prefix of the hash, the whole one included)
   · `storydump webhook status|register|deregister` (the bot's Telegram
   webhook, the deployment's variables read from this shell, no secret ever
   printed; exit 4 when a check fails) · `storydump doctor` (the token, the
   API, the token store, the config, Railway, the migration ledger against
   this checkout — each ok, wrong, missing or skipped with a one-line fix).

`storydump --help` is the authoritative list. A new verb is classified for the
safety block above before it ships: `tests/test_agent_docs.py` pins the names
the two documents use to the registry, not the judgement of what is dangerous.

## Testing

```bash
pytest                          # full suite (~3,750 tests); pytest.ini turns coverage on
pytest tests/src/services/      # one area
pytest -m unit                  # only the tests marked `unit`: a small subset, not every database-free test
pytest --no-cov                 # skip coverage (faster)
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

The `Procfile` names the two deployed processes; both run the one tier.

- **Worker:** `python -m src.main` — see the safety rules. `src/main.py` only
  dispatches to `src.worker.main`, which elects the clock, claims jobs on two
  lanes, runs the publish pipeline and sends the Telegram cards. It reads
  `TARGET_DATABASE_URL` from the process environment and exits 2 without it
  (`src/worker.py::main`); `make run` exports `.env`, a bare invocation does
  not read it. It receives nothing from Telegram: nothing in `src` polls.
- **API:** `uvicorn src.api.app:app` → health at `GET /health` (plus
  `/health/scheduling` and `/health/posting`, the surfaces the fleet monitors
  poll), schema at `/openapi.json`, the resource and command surface under
  `/api/v1`, sign-in under `/auth`. Telegram's deliveries — `/start` links,
  group joins, taps on a card — land here, on `POST /webhooks/telegram`. The
  API registers that webhook on the bot at startup only in Railway's
  `production` environment
  (`src/channels/telegram_webhook_registration.py::autoregister_enabled`), so a
  local API leaves the production bot alone unless
  `TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` is switched on — never do that with
  the production token.
- **Schema:** `python -m scripts.migration_runner apply` is the
  `preDeployCommand` of every deploy of either service (`railway.toml`), so a
  merged migration is an applied one; `status` is the read-only report. The
  gated files (`-- runner:manual`: 079, 080) are owed by a deploy and never run
  by it (`documentation/operations/migration-runner.md`).
- **Landing / dashboard:** `npm --prefix landing run dev` → http://localhost:3000;
  the BFF proxies to `BACKEND_URL`.

Environment variables are per-service in cloud deployment — set them on **both**
the worker and the API.

## What is deliberately not wired

**Outbound email does not send.** `src/services/target/email_sender.py` ships
inert by design: `sender_from_env` returns `None` unless `RESEND_API_KEY` and
the sender address (`EMAIL_FROM`) are both set, and the job registry parks
`send_email` with a reason naming what is missing. The provider choice is a
flagged decision that has not been ratified, and deferring it is deliberate.
An invitation created today therefore reports
`delivery: {"channel": "email", "state": "not_configured"}` — the row and its
token are real, the message is never delivered.

Do not describe email as working, and do not wire a provider without the owner
acknowledgement the design calls for.

Publishing, dry run and pause are **per-workspace settings** in the ledger
(`workspaces.api_publishing_enabled`, `dry_run_mode`, `is_paused`; the web's
Settings › General), never environment variables.

**Typed chat commands are not served.** The bot answers `/start` links, reads
who is in a bound group, and executes taps on its cards; an "approve" or a
`/pause` typed in a chat is not dispatched
(`src/services/target/telegram_dispatch.py`, #854). The command surfaces are
the card, the web Queue and Settings, and the `storydump` write verbs.

## Pre-commit and CI

```bash
source venv/bin/activate && ruff check . && ruff format --check . && pytest
```

**Always update `CHANGELOG.md`** when opening a PR — CI fails without it (the
`changelog-check` job of `.github/workflows/ci.yml`; a PR that touches only
`documentation/`, `.md` files or `.github/` is exempt).
[Keep a Changelog](https://keepachangelog.com/) format, entries under
`## [Unreleased]`.

## Documentation

- Full docs: `documentation/README.md`
- New docs go in `documentation/` subdirectories: `planning/` (plans and
  specs), `guides/` (how-to), `operations/` (runbooks)
- Bug fixes and patches: dated filenames in `documentation/updates/`
- A finished, superseded or abandoned document moves to
  `documentation/archive/` with a status banner and a row in
  `documentation/archive/README.md`. `CLAUDE.md`, `AGENTS.md`, `README.md`,
  `.claude/`, `documentation/operations/` and `documentation/guides/` are LIVE
  pages: `tests/test_agent_docs.py` fails when one names a legacy-only table, a
  deleted module path or a retired variable
- **Never** scatter markdown files through source directories
