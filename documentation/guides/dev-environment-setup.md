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
part of the everyday loop; see the safety rules in `AGENTS.md`.

---

## Recommended Solutions

### 1. Local Development Setup

```bash
# Clone and set up
cd ~/Projects
git clone https://github.com/chrisrogers37/storydump.git
cd storydump

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies, the package and the `storydump` CLI (the `cli` extra;
# `make install` does the same, and the Makefile's targets assume ./venv/)
pip install -r requirements.txt
pip install -e '.[cli]'

# Copy and configure environment
cp .env.example .env
# Edit .env — .env.example names every variable something reads
```

No variable is required to load the settings (`src/config/settings.py`): the
API, the tests and the `storydump` CLI load with an empty environment (the
tests additionally need `ENCRYPTION_KEY`, a Fernet key). A process needs what
it reads — the worker refuses to boot without `TARGET_DATABASE_URL`, and the
API answers 503 on every data route without it.

### 2. Database Options

**Option A: Local PostgreSQL (Recommended for Development)**

```bash
# macOS
brew install postgresql
brew services start postgresql

# Create the database and build the schema. `make init-db` applies step 0
# (the seven cluster-wide svc_* roles, then the DDL door migration 050 calls),
# the by-hand base with the one table production made by hand (migration 078
# snapshots it), then every file through the runner (not a psql loop; the
# runner keeps the ledger `storydump doctor` compares the checkout against).
# DB_USER needs CREATEROLE. `setup_database.sql` alone stops the runner at
# migration 050. The two gated files, 079 and 080, are listed as owed and not
# applied (documentation/operations/migration-runner.md).
make create-db init-db
```

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
alias sl='cd ~/Projects/storydump && source venv/bin/activate'

# Quick checks
alias sl-test='cd ~/Projects/storydump && source venv/bin/activate && pytest'
alias sl-lint='cd ~/Projects/storydump && source venv/bin/activate && ruff check .'
alias sl-format='cd ~/Projects/storydump && source venv/bin/activate && ruff format .'
alias sl-precommit='cd ~/Projects/storydump && source venv/bin/activate && ruff check . && ruff format --check . && pytest'

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
   the API under your own token
   ([`reading-the-ledger.md`](../operations/reading-the-ledger.md)).
2. **The escape hatch**, for a question the verbs do not answer — the service's
   own runtime login, a file of `SELECT`s, and the connection string never
   printed:

```bash
railway run --service worker --environment production -- \
  sh -c 'psql "$TARGET_DATABASE_URL" -At -F " | " -f /dev/stdin' < probe.sql \
  | sed -E "s#postgres(ql)?://[^ ]+#postgres://<redacted>#g"
```

`SELECT` only, inside `BEGIN TRANSACTION READ ONLY`. Nothing else enforces it:
as measured on 2026-09-17 production's runtime login was still the database
owner (`scripts/migrations/078_legacy_snapshots_pre_cutover.sql:14-15`), a role
that holds `BYPASSRLS` — the move off it is
[`runtime-database-roles.md`](../operations/runtime-database-roles.md) — so a
stray write has nothing standing in its way. Writing the statements to a file
first makes them reviewable before they run.

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
# Trigger a manual redeploy on Railway
railway up --service worker
railway up --service storydump
```

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

# The runtime login (the API and the worker)
TARGET_DATABASE_URL=postgresql://storydump_user:your_local_password@localhost:5432/storydump
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
  python3 -m venv venv && source venv/bin/activate
  pip install -r requirements.txt && pip install -e '.[cli]'
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

---

## Future Improvements

1. **Docker**: Containerize the app for identical local/cloud environments
2. **Staging environment**: Separate Railway project for pre-production testing
3. **Database branching**: Use Neon branching for isolated dev databases

---

*Last updated: 2026-09-18*
