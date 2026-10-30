# AGENTS.md

Guidance for any coding agent working in this repository. This is the
vendor-neutral file the wider tool ecosystem reads; `CLAUDE.md` carries the
Claude Code specifics, repeats the safety rules and the rules every PR follows,
and defers to this document for everything else. A PR that adds or changes one
of those rules here makes the same change in `CLAUDE.md`.

**The safety rules below are not advisory.** This system posts to Instagram and
Telegram on behalf of paying tenants.

---

## CRITICAL SAFETY RULES

**THIS SYSTEM POSTS TO INSTAGRAM. DO NOT TRIGGER POSTING WITHOUT EXPLICIT USER APPROVAL.**

### NEVER run these

```bash
python -m src.main                   # Starts the posting worker: the clock, the job lanes and the sweepers (it posts to Instagram)
python -m src.worker                 # Starts the posting worker: the clock, the job lanes and the sweepers (it posts to Instagram)
make run                             # Runs `python -m src.main`: starts the posting worker
make dev                             # Runs `python -m src.main` once .env exists: starts the posting worker
python -m scripts.migration_runner apply --manual <version>   # Applies a gated (runner:manual) file by hand: 002 drops the archive snapshots; only a database converted in place owes 001 and 002
storydump approve <story>            # Posts a story to Instagram — the user's decision, never an agent's
storydump cancel <story>             # Cancels a story: refunds its debit, destroys its upload
storydump resolve <story> cancel     # Gives up on a story parked for review; its debit is retained
storydump resolve <story> retry      # Posts the story again; --not-posted overrides the lost-answer guard
storydump tokens revoke <id>         # Revokes an API token; it stops working at once
storydump webhook register           # Re-points the production bot's webhook; --drop-pending discards queued taps
storydump webhook deregister         # Detaches the bot's webhook — Telegram delivers nothing until it is registered again
storydump admit <email>              # Admits a person and emails them a sign-up link — the owner's decision, never an agent's
```

### Before ANY posting-related action

1. **STOP** and ask for explicit confirmation.
2. Explain exactly what will happen.
3. Wait for an affirmative answer.

### Telegram Web (web.telegram.org)

- **NEVER type or click** — view and screenshot only.
- Anything the bot should do happens through a `storydump` verb under the rules
  above, or on the user's own device.

### Production

Never run against production: the posting scheduler, or mutating SQL against
the ledger — `post_intents`, `jobs`, `channel_outbox` and every other table
`src/models/target/` declares — or the `archive` schema. Do not open a
connection to the production database at all, even to read: a read is the
owner's to run. Say what you would read and leave it to them.

### Reading this list correctly

It names what posts, destroys, emails a person, or re-points the bot. It is not a complete
read-only/read-write taxonomy: the other write verbs (`skip`, `reject`,
`posted`, `resolve <story> posted`, `pause`, `resume`, `sync`, `schedule`,
`reschedule`, `link`) change the
ledger through the command port too — a skip or a reject is a terminal state
for that story, and a scheduled story asks the workspace's chats to approve it
at its time — and every one of them is a posting-related action under the STOP
rule above. The read verbs (`waitlist` among them: it lists, and admits no one),
`health`, `deploys` and `doctor` read only, and `webhook status` changes nothing
(its door check is an empty POST the API refuses by design). When a verb is
not on this list, check what it does before running it rather than inferring
that absence means safe.

The same holds outside the CLI. The migration runner's `apply`, `adopt`,
`convert-ledger` and `repair` write the schema or its ledger on the database
`--database-url` or `DATABASE_URL` names; `status` and `parity` only read. The
`Makefile` exports `.env`, so `make drop-db`, `reset-db` and `db-restore` drop
or overwrite the database it names, `create-db`, `init-db`, `setup-db` and
`quickstart` create or build on it, `db-shell`, `check-db` and `db-backup`
open it, and the `test` targets create and drop test databases on its server. Run any of these, the
schema builder (`python -m src.models.target.ddl`) and `pytest tests/scripts/`
only against a local or throwaway database; against any other they are the
owner's.

### The record

This repository is public: every file, commit, pull request, issue, review
comment, screenshot and GIF is world-readable and indexed, and deleting one
later does not recall it: a pushed commit outlives the branch that carried
it. None of them names a tenant. Write placeholders —
`<workspace>`, `@<handle>`, `<ws-id>`, "the owner's account" — never a
workspace's name, an account's handle, a workspace, account, user or story id
from production, a person's email (the site's published contact address is
the exception), or what an account posted. Never write the owner's other
businesses or their domains, a Telegram chat or user id, or any token or key.
Describe production evidence by its shape and its numbers, and take
screenshots from the demo or a local stack.

The `Public record` CI job (`scripts/public_record.py`) fails a tracked file, a
commit message a push or pull request brings, or a pull request's title or body
that names an identifier on the owner's private list, a repository secret that
never lives in the tree. Its CI log reports only a count, never where or which,
since the log is public. Against your copy in
`~/.config/storydump/private-identifiers.txt`, `scripts/lint.sh` shows the
lines in files and in your branch's commit messages, a push run's log gives the
command that shows its commits, and a pull request's title and body are yours
to check.

A commit message, and a pull request's title and body, describe the change:
its behavior and its tests.

### Main is production

Railway deploys `main` to production on every push, both the API and the
worker, and Vercel builds `storydump.app` from it. So:

- Work on your own branch and open a pull request; never push to `main`, and
  never merge to it without the owner's yes.
- Other agents work in this repository at the same time. Never force-push or
  rewrite a branch you did not create.
- Before pushing, `git fetch --prune --prune-tags --force origin`. Never
  `git push --tags` or `git push --mirror`.

---

## Project overview

**Storydump** is a hosted, multi-tenant Instagram Story scheduling and
automation service with Telegram-based team collaboration. One deployment serves
many tenants, and the operator and a tenant are different parties — which is why
per-tenant isolation is a product requirement rather than a hardening
preference.

**Tech stack:** Python 3.12+, FastAPI, PostgreSQL (Neon in cloud; hand-written
SQL on asyncpg), the Telegram Bot API over HTTP (no bot framework), the
Instagram Graph API, Google Drive, Cloudinary, Railway deployment, Next.js
`landing/` site on Vercel.

## Architecture: one tier, strict separation of concerns

There is one tier. The design plan and the package names call it the *target*
tier (`src/services/target/`, `src/models/target/`) because it was built beside
the tier it replaced. The legacy tier was retired in September 2026, and its
schema and snapshots are gone. Production moved on 2026-10-17 to a database
built from schema code (`documentation/operations/schema-cutover.md`);
`storydump posture` shows the ledger's head.

Each layer is isolated. Do not violate the boundaries:

- **CLI** (`storydump_cli/`) → calls the API over HTTP, never a database. The
  one module it imports from `src` is `src/services/target/vocabulary.py`
  (`tests/storydump_cli/test_import_boundary.py` pins that in a fresh
  interpreter).
- **UI** (`landing/`) → calls the API through its server-side client
  (`landing/src/lib/target-api.ts`), never a service, and holds no database
  credential: the waitlist form posts to the API's `POST /public/waitlist`
  (`src/api/routes/public.py`).
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
  tenant, it sets the `app.tenant_id` the RLS policies of
  `src/models/target/ddl/07_row_security.sql` read). Cross-tenant work goes
  through the database's `SECURITY DEFINER` doors
  (`src/models/target/ddl/04_functions.sql`), never a privileged session. A transaction
  never spans a provider call: write the checkpoint, commit, then call Meta,
  Telegram, Drive or Cloudinary through the egress floor (`egress.py`).
- **Channels** (`src/channels/`) → the Telegram transport and the webhook
  registration. Only the two composition roots import them (`src/worker.py`,
  `src/api/app.py`); a service receives the transport as an injected callable.
- **Models** (`src/models/target/`) → the schema as code: the SQLAlchemy
  models declare the tables, and the SQL files in `ddl/` declare the roles,
  functions, triggers, policies and grants the models cannot
  (`src/models/target/ddl/__init__.py`). They are not an ORM: only the schema
  builder imports them, never a service or `storydump_cli/`. Changes after the
  initial schema ship as files in `scripts/migrations/`, applied by
  `scripts/migration_runner.py`.

**The database is the authority.** The legal edges of a story's state are rows
of `post_intent_transitions` (`src/models/target/ddl/05_reference_rows.sql`)
and the trigger `tg_intent_guard` refuses the rest
(`src/models/target/ddl/06_triggers.sql`; the service `intent_ledger.py` issues
the UPDATE and translates the refusal), one live lease per serialization key
is a unique index on `jobs` (`src/models/target/machinery.py`), and every state
change of a story writes its own `audit_events` row by trigger, which refuses
an anonymous one. Do not add a Python pre-check that copies one of these: a
second authority is how the two drift.

### The command port

State changes go through one closed vocabulary
(`src/services/target/commands.py::VOCABULARY`, re-exported from
`vocabulary.py::COMMANDS` — 32 commands on 2026-10-20). The web adapter exposes
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
invitation, an invited person's own invitations with "Email me the link
again" (`/api/v1/me/invitations`), and the owner's waitlist and an
admission off it (`/api/v1/ops/waitlist`, `/api/v1/admissions`: an
admission belongs to no workspace) (`src/api/routes/v1.py`,
`src/api/routes/tokens.py`, `src/api/routes/ops.py`,
`src/api/routes/admissions.py`).

Two consequences worth knowing before reasoning about reach:

- **Every built command in the vocabulary is reachable over the web API**,
  subject to its role floor (`commands.ROLE_FLOOR`, per command, over the
  ladder `FLOORS`). There is no separate web-exposed subset. The `operator`
  floor has no principal behind it yet, so a command on that floor is
  refused as a role refusal for every caller.
- A vocabulary command with no executor yet is a **named refusal**, not an
  absent name: it answers `CommandNotBuilt`, rendered `501`. Read the current
  unbuilt set from `commands.UNBUILT`, which is *derived* from the registry and
  pinned by `tests/src/services/target/test_commands.py` so it can only shrink
  deliberately — not from a list here, which would be stale the first time
  someone builds one.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e '.[cli]'
```

The `Makefile`'s test targets run the virtualenv's `pytest` from `.venv/`, or
from `venv/` when `.venv/` holds none (`make VENV=<dir>` overrides both).
Both directories are gitignored.

Then the database: `make create-db init-db` builds the schema from code on an
empty PostgreSQL (`python -m src.models.target.ddl init`), records the initial
marker (`migration_runner adopt`) and applies any newer file. It needs `psql`
on your `PATH` and a `DB_USER` with `CREATEROLE` that owns the database;
`README.md` §3 walks through it,
and `make validate-env` loads the settings and builds the key ring, as both
services do at startup. Neither the worker nor the database-gated tests run
before this.

`src/config/settings.py` requires **no variable**: every field has a default, so the settings load with an empty
environment — in the web service, the tests and the `storydump` CLI. A process
needs what it reads: the worker refuses to boot without `TARGET_DATABASE_URL`,
or with one that names a pooled host, no host, or cannot be read (exit 2,
naming it), the API answers 503 on every data route without it, both
refuse to start without a credential key ring that loads (`ENCRYPTION_KEY`, or
`ENCRYPTION_KEYS`), and the CLI imports from `src` only
`src/services/target/vocabulary.py` and needs no variable but its token. Tests
additionally need `ENCRYPTION_KEY`, a Fernet key. `.env.example` names every
variable something reads, and a test fails if it names one nothing does.

`.env`, `.env.test` and `landing/.env.local` are gitignored and never committed.

## Commands

```bash
storydump whoami
storydump floating --watch
storydump story <intent_id>
storydump planned
storydump health
storydump doctor
```

`storydump --help` is the authoritative list, grouped by auth, reads, writes and
environment; the section below walks it.

## The `storydump` CLI (v2)

`storydump` is the developer and agent console over the API — a pure HTTP
client, never a database connection. Install it with
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
   `storydump account <handle>` (its state, cap, zone, next slot, today's
   bucket, recent outcomes) · `storydump jobs --since 3h` · `storydump outbox --since 3h` ·
   `storydump burst --since 2026-09-15T14:50:00Z` (taps, permits, float
   waits, siblings, review cards, outcomes) · `storydump posture` (the
   migration ledger, the role, RLS, the doors; for `OPS_USER_IDS` only, like
   `health`) · `storydump waitlist` (the waitlist and the admissions made off
   it, newest first: each address, the day it joined or "not on the
   waitlist", and its state; for `OPS_USER_IDS` only, like `posture`, with no
   `--workspace`) · `storydump planned [--state
   scheduled,awaiting_approval] [--newest-first]` (the planned stories, soonest
   first: when each is due, its account and item, how it repeats, and who
   planned it; it takes no `--watch`).
5. Write through the command port — the same door a tap or a web click uses,
   so admission, tenancy and audit apply unchanged. `--workspace <id or name>`
   is required (a write goes to ONE workspace). A story verb's idempotency key
   is deterministic (`<command>:<story>`, the web's), so a re-run replays as
   "already done" (exit 0) and `--idempotency-key <k>` is the deliberate second
   execution; a resolution's key carries the review episode, so a later review
   of the same story is new; `pause`, `resume` and `sync` mint a fresh key per
   invocation (their effects are idempotent — a retry is harmless, a later
   action always executes), and so do `schedule`, `reschedule` and `link`
   (planning an item again after a cancel, moving a story back to a time it had,
   or setting a link the item had before, is a new act; a duplicate schedule is
   the database's to refuse):
   `storydump approve|skip|reject|posted|cancel <story>` (`cancel
   --stop-repeat` also ends a repeating story's series) ·
   `storydump resolve <story> retry|posted|cancel [--not-posted]` ·
   `storydump pause` / `storydump resume` · `storydump sync <source_id>` ·
   `storydump schedule <item> --account <handle|id> --at 'YYYY-MM-DD HH:MM'
   [--override-locks] [--repeat day|week [--every N] [--on mon,thu]]` (the time
   is the account's own zone, else the workspace's, and it is the first
   occurrence of a repeat) · `storydump reschedule <story> [--at 'YYYY-MM-DD
   HH:MM'] [--repeat day|week [--every N] [--on …] | --no-repeat]` ·
   `storydump link <item> <https-url>` / `storydump link <item> --clear` (the
   link an item's stories ask a person to add by hand, since an API-published
   story cannot carry one). A
   refusal is an answer, not a failure: the reason's sentence, the fixing
   verb, exit 2. The Telegram adapter's words never appear in a terminal.
   `storydump admit <email> [--resend]` writes outside the port, as
   `tokens revoke` and the webhook verbs do. It is for `OPS_USER_IDS` only, like `posture`: it admits any address, on the
   waitlist or not, to sign up, and queues its email with a one-time sign-up
   link (`POST /api/v1/admissions`). An admission belongs to no
   workspace, so it takes no `--workspace`. A re-run answers "already
   admitted" (exit 0), and `--resend` sends the email again, its new link
   replacing the last. It emails a real person and nothing undoes it, so it
   is on the never-run list above.
6. The environment: `storydump health` (the API's three health surfaces,
   judged by the fleet monitors' verdicts, which
   `storydump_cli/health_classify/` carries: not well when a monitor would
   page; plus the bot's webhook from the API's
   operating details, `GET /api/v1/ops/health`, which answers only the user ids
   in the API's `OPS_USER_IDS` — `storydump whoami` prints yours; without
   them the webhook is `not_checked`, not well, and `data.details` says why;
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
pytest                          # full suite (~9,200 tests); pytest.ini turns coverage on
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
through it, and the Homebrew `postgresql@17` keg is enough:

```bash
docker run -d --name storydump-test-pg -p 65433:5432 \
  -e POSTGRES_USER=test_user -e POSTGRES_PASSWORD=test_password \
  -e POSTGRES_DB=storyline_test postgres:17
PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH" \
DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password \
DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 \
  pytest tests/scripts/
```

To run only the database-free tests, with no server at hand, point the
fixtures at a port where nothing listens, and the database-gated tests skip.
A port that answers counts as a configured database (another PostgreSQL on
5432, say), so a failed login there fails the run rather than skipping
(`tests/conftest.py`, `server_is_listening`):

```bash
DB_HOST=127.0.0.1 DB_PORT=65499 pytest --no-cov tests/src/services/target/
```

## Services

The `Procfile` names the two deployed processes; both run the one tier.

- **Worker:** `python -m src.main` — see the safety rules. `src/main.py` only
  dispatches to `src.worker.main`, which elects the clock, claims jobs on two
  lanes, runs the publish pipeline and sends the Telegram cards. It reads
  `TARGET_DATABASE_URL` from the process environment and exits 2 without it,
  or when it names a pooled host, no host, or cannot be read
  (`src/worker.py::main`; why: `documentation/guides/cloud-deployment.md` ›
  Two logins); `make run` exports `.env`, a bare invocation does not read it.
  It receives nothing from Telegram: nothing in `src` polls.
- **API:** `uvicorn src.api.app:app` → health at `GET /health`, which says ok
  and the version and commit that answer, nothing else (the details — usage
  counts, the database login, the pool, the webhook, the queue — are
  `GET /api/v1/ops/health`, for `OPS_USER_IDS` alone; plus
  `/health/scheduling`, `/health/posting` and `/health/delivery`, the
  surfaces the fleet monitors poll), schema at `/openapi.json` (and `/docs`) only when started with
  `API_DOCS=1`, which Railway's `production` environment ignores — the resource and command surface under
  `/api/v1`, sign-in under `/auth`. Telegram's deliveries — `/start` links,
  group joins, a group's move to a supergroup, taps on a card, the link
  prompt or an operator's waitlist ping — land here, on
  `POST /webhooks/telegram`. The API registers that webhook on the bot at
  startup only in Railway's `production` environment
  (`src/channels/telegram_webhook_registration.py::autoregister_enabled`), so a
  local API leaves the production bot alone unless
  `TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER` is switched on — never do that with
  the production token.
- **Schema:** `python -m scripts.migration_runner apply` is the
  `preDeployCommand` of every deploy of either service (`railway.toml`), so a
  merged migration is an applied one; `status` is the read-only report. A
  gated file (`-- runner:manual`: 001, 002) is never run by a deploy. A
  database built from code adopts both, as production did; only one converted
  in place owes them (`documentation/operations/schema-cutover.md`). The
  runner refuses to run where libpq connected to a pooled host, waits a
  bounded time for another run's lock (`RUNNER_LOCK_WAIT`), naming the holder,
  then fails the deploy with the old version serving, and unlocks before it
  closes.
- **Landing / dashboard:** `npm --prefix landing run dev` → http://localhost:3000;
  the BFF proxies to `TARGET_API_URL` (`BACKEND_URL` is the fallback). The
  owner area at `/ops`, whose first page is the waitlist with Admit and Send
  again, is for the people in the API's `OPS_USER_IDS`, reached by the
  dashboard header's Owner link and the waitlist ping's Open waitlist ↗;
  anyone else gets the site's ordinary 404, and the API's `require_ops` is
  the gate that holds.

Environment variables are per-service in cloud deployment — set them on **both**
the worker and the API.

## What is deliberately not wired

**Outbound email sends only where it is configured.**

- **The provider.** It is Resend, ratified by the owner on 2026-10-11.
- **Credentials.** `src/services/target/email_sender.py` still ships inert
  without them: `sender_from_env` returns `None` unless `RESEND_API_KEY` and
  the sender address (`EMAIL_FROM`) are both set.
- **Invitations.** They are emailed from the API after the invitation commits:
  `invitations.InvitationEmail`, run by the command route. Their link never
  rests in a job row.
  - The response's `delivery` says `sent`, `not_configured`, `over_budget` or
    `failed`.
  - Its `join_url` (`{WEB_APP_URL}/join/{token}`, returned once) is the
    inviter's fallback either way.
  - Only the **API** service needs the two variables for this.
- **The worker.** Its `send_email` jobs serve the activation nudge and
  admissions from the waitlist. An admission's email carries a claim
  link the worker draws as it sends, and only the link's hash is stored, once
  the provider has taken the email.
  - The jobs stay parked until the worker has `RESEND_API_KEY` and
    `EMAIL_FROM` too. The owner sets them there once the claim link works at
    sign-in.
  - The nudge keeps its own switch, `TARGET_ACTIVATION_NUDGE_ENABLED`.

Do not describe an email as sent where the deployment lacks the variables,
and do not wire another provider without the owner's acknowledgement.

**The Instagram past-posts source is on wherever its key ring is.** Its code (the
adapter, the index, selection, the lifecycle and the deletion receipts) is in the
tree, and `past_posts_activation.resolve` arms it in every process that loads a whole
`PAST_POSTS_HMAC_KEYS` ring: there is no record file and no switch. Without the ring
each composition root wires nothing. The worker's log carries the activation line,
`instagram past posts: off (<reason>)`, or `on (run=<run id>)` with the ring. The
API writes that line to a module logger its process does not emit, so the API's own
log shows `telegram ingress: instagram past posts on|off` instead.
The ring is required, not a switch: it keys tombstones and Meta data-deletion
receipts, so never remove it to turn the past-posts source off. A connected account's past-posts source
is switched Off on the posting-mix card instead. Do not build an enabled capability
outside `past_posts_activation.py` and tests. What is still unproven on real providers
is in `documentation/operations/instagram-past-posts-review.md`.

Publishing, dry run and pause are **per-workspace settings** in the ledger
(`workspaces.api_publishing_enabled`, `dry_run_mode`, `is_paused`; the web's
Settings › General), never environment variables.

**Typed chat commands are not served.** The bot answers `/start` links, reads
who is in a bound group, and executes taps on its cards, its link prompt and
an operator's waitlist ping; an "approve" or a
`/pause` typed in a chat is not dispatched
(`src/services/target/telegram_dispatch.py`). The command surfaces are
the card, the web Queue and Settings, and the `storydump` write verbs.

## Pre-commit and CI

```bash
source .venv/bin/activate && ruff check . && ruff format --check . && pytest
```

CI (`.github/workflows/ci.yml`) runs eight required checks on every push and
PR: Lint (ruff check + format over the whole repository), the FC-2 Telegram
ratchet (`scripts/telegram_ratchet.py`), Test (five shards, each on Python 3.12
against a PostgreSQL 17 service with `REQUIRE_TEST_DATABASE=1`, coverage of
`src` and `storydump_cli`),
Security Scan (pip-audit gates on a known-vulnerable pin; bandit is advisory),
Front End (`npm test`, `npx tsc --noEmit`, `npm run lint` and
`npm audit --omit=dev` in `landing/`; the audit gates on a known advisory
against the runtime dependencies; `next build` is left to Vercel's preview),
Public record (`scripts/public_record.py`, the check under The record above),
Changelog Check, and Schema consistency (`scripts/schema_consistency.py`: the
schema built from this tree's code against the pinned schema-code base with
the newer migration files replayed on it). `scripts/lint.sh`
is the local lint pass; `scripts/pr_ready.sh` answers whether a PR is really
ready — it catches a check that was never scheduled, which a green rollup
hides (`documentation/guides/ci-cd-pipeline.md`).

**A PR that touches code or config adds a changelog fragment, and no PR edits
`CHANGELOG.md`**: one new file in `changelog.d/`. `changelog.d/README.md` has
the format and the rule the `changelog-check` job of `.github/workflows/ci.yml`
holds every PR to.

## Documentation

- Full docs: `documentation/README.md`
- New docs go in `documentation/guides/` (how-to) or
  `documentation/operations/` (runbooks), with a line in
  `documentation/README.md`
- Bug fixes and patches: a changelog fragment (`changelog.d/`)
- `CLAUDE.md`, `AGENTS.md`, `README.md`, `.claude/`,
  `documentation/operations/` and `documentation/guides/` are LIVE pages:
  `tests/test_agent_docs.py` fails when one names a legacy-only table, a
  deleted module path or a retired variable
- **Never** scatter markdown files through source directories
