# Development Environment Setup Guide

A guide for local development with cloud deployment (Railway + Neon).

## Current Architecture

- **Local development**: Mac (code editing, tests, linting)
- **Production**: Railway — the `worker` service (`python -m src.main`, the
  target worker) and the `storydump` service (the API) — with Neon PostgreSQL;
  the web front end (`landing/`) on Vercel
- **Deployment**: Push to `main` triggers Railway auto-deploy; each deploy
  applies pending migrations first (`railway.toml`'s `preDeployCommand`)

A local environment is for development and tests. `python -m src.main`
(`make run`, `make dev`) starts the real worker — it sends Telegram cards and
publishes to Instagram for whatever database it is pointed at — so it is not
part of the everyday loop.

---

## Recommended Solutions

### 1. Local Development Setup

```bash
# Clone and set up
cd ~/Projects
git clone <repo-url>
cd storydump

# Create the virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies, the package and the `storydump` CLI (the `cli` extra;
# `make install` does the same with the `pip` on your PATH; the Makefile's
# test targets look for `.venv/` first, then `venv/`)
pip install -r requirements.txt
pip install -e '.[cli]'
make install-dev          # + ruff, bandit, pip-audit — what CI's lint and security jobs run; the aliases below need ruff

# Copy and configure environment
cp .env.example .env
# Edit .env — .env.example names every variable something reads
```

No variable is required to load the settings (`src/config/settings.py`): the
API, the tests and the `storydump` CLI load them with an empty environment
(the tests additionally need `ENCRYPTION_KEY`, a Fernet key). A process needs
what it reads — the worker refuses to boot without `TARGET_DATABASE_URL`, the
API answers 503 on every data route without it, and both refuse to start
without an `ENCRYPTION_KEY` (or `ENCRYPTION_KEYS`) that is a Fernet key.

### 2. Database Options

**Option A: Local PostgreSQL (Recommended for Development)**

```bash
# macOS — PostgreSQL 15 is the compatibility target; CI uses postgres:17.
# The DB gates need `psql` on the PATH
# (/opt/homebrew/opt/postgresql@15/bin; see testing-guide.md).
brew install postgresql@15
brew services start postgresql@15

# Build the empty database from the models and src/models/target/ddl/.
# init-db then adopts the empty 000 marker and applies later migrations.
# DB_USER must own the database and have CREATEROLE for the service roles.
# make reset-db drops and rebuilds the local database.
make create-db init-db
```

Set passwords for `svc_ingress` and `svc_worker` through the local owner
session, then configure their separate runtime URLs as shown in the
[README database setup](../../README.md#3-database-setup). The API uses
`svc_ingress`; start the worker with
`make run TARGET_DATABASE_URL='postgresql://svc_worker:<password>@localhost:5432/storydump'`.
The make command-line variable overrides the API URL in `.env`.

**Option B: Connect to Neon (Shared Dev/Staging)**

```bash
# Connect directly using DATABASE_URL
psql "$DATABASE_URL"

# Point the API and the worker at it: the run-time variable (see .env.example)
TARGET_DATABASE_URL=postgresql://user:pass@ep-xxx.neon.tech/storydump?sslmode=require
```

The `DB_*` components do NOT point the app anywhere: they steer only the test
harness and `make` — `make reset-db` included, which DROPS `DB_NAME` on
`DB_HOST`. Never aim them at a shared database.

---

### 3. Shell Aliases for Mac

Add these to `~/.zshrc` (or `~/.bashrc`) on your Mac:

```bash
# Storydump shortcuts
alias sl='cd ~/Projects/storydump && source .venv/bin/activate'

# Quick checks
alias sl-test='cd ~/Projects/storydump && source .venv/bin/activate && pytest'
alias sl-lint='cd ~/Projects/storydump && source .venv/bin/activate && ruff check .'
alias sl-format='cd ~/Projects/storydump && source .venv/bin/activate && ruff format .'
alias sl-precommit='cd ~/Projects/storydump && source .venv/bin/activate && ruff check . && ruff format --check . && pytest'

# Database shortcuts (local PostgreSQL)
alias sl-db-reset='cd ~/Projects/storydump && make reset-db'

# Railway operations
alias sl-logs='railway logs --service worker'
alias sl-logs-web='railway logs --service storydump'
alias sl-health='storydump health'
alias sl-restart='railway restart --service worker'
```

After adding, reload:
```bash
source ~/.zshrc
```

#### Reading production

There is deliberately no alias that opens `psql` on production, and
`DATABASE_URL` in particular is the database **owner's** login — it is for the
migration runner, not for queries. Production is read, in this order:

1. **The `storydump` verbs** — `storydump story`, `floating`, `account`,
   `jobs`, `outbox`, `burst`, `posture`: bounded, tenant-scoped reads through
   the API under your own token (`storydump --help` lists them).
2. **The escape hatch**, for a question the verbs do not answer — a file of
   `SELECT`s run with the service's own runtime login, the connection string
   never printed.

`SELECT` only, inside `BEGIN TRANSACTION READ ONLY`, which makes the database
refuse a write. Writing the statements to a file first makes them reviewable
before they run.

---

### 4. Deployment Workflow

Since Railway auto-deploys from `main`, the deployment workflow is:

```bash
# 1. Develop on a feature branch
git checkout -b feature/my-feature

# 2. Run pre-commit checks
sl-precommit

# 3. Push and create PR
git push -u origin feature/my-feature
gh pr create

# 4. After PR review and CI passes, merge to main
gh pr merge --merge

# 5. Railway auto-deploys from main: the pre-deploy step applies pending
#    migrations, then the new version of each service starts
storydump deploys --watch --commit <sha>   # 0 when both reach SUCCESS, 6 on a failure
railway logs --service worker
```

### Manual Deploy (if needed)

```bash
# Re-run a service's latest deployment (nothing local is uploaded or rebuilt)
railway redeploy --service worker --yes
railway redeploy --service storydump --yes
# or push to main (an empty commit will do) — the normal path, CI included
```

Never `railway up`: it uploads and deploys the laptop's working tree —
uncommitted edits included — bypassing `main`, the PR flow and CI. And
`redeploy` re-runs whatever Railway holds as the latest deployment, which after
a `railway down` can be an old build: when in doubt, push to `main` and confirm
the commit with `storydump deploys`.

---

### 5. Running the Tests

`pytest` runs from the repository root; how the suite is laid out, what needs a
PostgreSQL and how to give it one is [`testing-guide.md`](testing-guide.md).
The harness loads `.env.test` over the environment before any application
import (`tests/conftest.py:39`), so its values win over `.env`'s.

---

### 6. Environment Files

Two files are read, and both are gitignored: **`.env`** (copied from
`.env.example`) and **`.env.test`** (the test harness loads it over the
environment — `tests/conftest.py:39`). Nothing reads any other name, and a
differently named file is not ignored by git — do not keep credentials in one.

How `.env` is read (`.env.example`'s own header): the code loads only the
`Settings` fields from it; the run-time reads — `TARGET_DATABASE_URL`, the bot
token, the worker's knobs — come from the **process environment**. `make`
exports `.env` into its targets; a bare `python -m src.main` does not read it.

```bash
# .env - Local development settings
DB_HOST=localhost
DB_PORT=5432
DB_NAME=storydump
DB_USER=storydump_user
DB_PASSWORD=your_local_password

# The API runtime login; the worker receives its own svc_worker URL at startup.
# Set each role's password as the owner and percent-encode URL passwords.
TARGET_DATABASE_URL=postgresql://svc_ingress:your_api_password@localhost:5432/storydump
# The owner login (the migration runner). Left commented, as in .env.example:
# `make` exports this file into every target, and `make init-db` builds the
# runner's URL from the DB_* components rather than trusting an ambient one.
# DATABASE_URL=postgresql://storydump_user:your_local_password@localhost:5432/storydump

# The bot the worker sends with (optional: without it the Telegram channel parks)
TARGET_TELEGRAM_BOT_TOKEN=your_test_bot_token
TARGET_TELEGRAM_BOT_USERNAME=your_test_bot

ENCRYPTION_KEY=<a Fernet key>
LOG_LEVEL=DEBUG
```

Production environment variables are stored in the Railway dashboard (never in files).

---

### 7. Quick Reference Card

```
=== LOCAL COMMANDS ===
sl                  - cd to project, activate venv
sl-test             - run pytest
sl-lint             - run ruff check
sl-precommit        - full pre-commit check (lint + format + test)

=== RAILWAY COMMANDS ===
sl-logs             - follow worker service logs
sl-logs-web         - follow API service logs
sl-health           - run health check on production
sl-restart          - restart worker service

=== DEPLOYMENT WORKFLOW ===
1. Create feature branch
2. Run sl-precommit
3. Push and create PR
4. Merge to main (Railway auto-deploys)
5. Monitor with sl-logs
```

---

## Implementation Checklist

- [ ] **Mac: Install dependencies**
  ```bash
  cd ~/Projects/storydump
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt && pip install -e '.[cli]' && make install-dev
  ```

- [ ] **Mac: Set up local PostgreSQL** (optional, for offline dev)
  ```bash
  brew install postgresql && brew services start postgresql
  make create-db init-db     # the database, then the schema through the runner
  ```

- [ ] **Mac: Add aliases to ~/.zshrc**
  - Copy aliases from Section 3
  - Run `source ~/.zshrc`

- [ ] **Mac: Install Railway CLI**
  ```bash
  brew install railway
  railway login
  railway link  # Link to your project
  ```

- [ ] **Test the workflow**
  - `sl` to activate environment
  - `sl-precommit` to verify all checks pass
  - `sl-logs` to monitor production logs
