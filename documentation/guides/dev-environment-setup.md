# Development Environment Setup Guide

A guide for local development with cloud deployment (Railway + Neon).

## Current Architecture

- **Local development**: Mac (code editing, tests, linting)
- **Production**: Railway (worker + web services) with Neon PostgreSQL
- **Deployment**: Push to `main` triggers Railway auto-deploy

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

# Install dependencies
pip install -r requirements.txt
pip install -e .

# Copy and configure environment
cp .env.example .env
# Edit .env with your local or Neon database credentials
```

### 2. Database Options

**Option A: Local PostgreSQL (Recommended for Development)**

```bash
# macOS
brew install postgresql
brew services start postgresql

# Create database
createdb storydump
psql -d storydump -f scripts/setup_database.sql

# Apply the migrations through the runner (never a psql loop): it keeps the
# ledger `storydump doctor` compares the checkout against
python -m scripts.migration_runner apply
```

**Option B: Connect to Neon (Shared Dev/Staging)**

```bash
# Connect directly using DATABASE_URL
psql "$DATABASE_URL"

# Or set individual vars in .env
DB_HOST=ep-xxx.neon.tech
DB_PORT=5432
DB_NAME=storydump
DB_USER=storydump_user
DB_PASSWORD=neon_password
DB_SSLMODE=require
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
```

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

# Production database queries (via Neon)
alias sl-db-prod='psql "$DATABASE_URL"'
```

After adding, reload:
```bash
source ~/.zshrc
```

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

# 5. Railway auto-deploys from main
# Monitor: railway logs --service worker
```

### Manual Deploy (if needed)

```bash
# Trigger a manual redeploy on Railway
railway up --service worker
railway up --service storydump
```

---

### 5. Environment File Standardization

Create a `.env.dev` for local development:

```bash
# .env.dev - Local development settings
DB_HOST=localhost
DB_PORT=5432
DB_NAME=storydump
DB_USER=storydump_user
DB_PASSWORD=your_local_password

# The runtime login (the API and the worker) and the owner login (the runner)
TARGET_DATABASE_URL=postgresql://storydump_user:your_local_password@localhost:5432/storydump
DATABASE_URL=postgresql://storydump_user:your_local_password@localhost:5432/storydump

# The bot the worker sends with (optional: without it the Telegram channel parks)
TARGET_TELEGRAM_BOT_TOKEN=your_test_bot_token
TARGET_TELEGRAM_BOT_USERNAME=your_test_bot

ENCRYPTION_KEY=<a Fernet key>
LOG_LEVEL=DEBUG
```

Production environment variables are stored in the Railway dashboard (never in files).

---

### 6. Quick Reference Card

```
=== LOCAL COMMANDS ===
sl                  - cd to project, activate venv
sl-test             - run pytest
sl-lint             - run ruff check
sl-precommit        - full pre-commit check (lint + format + test)

=== RAILWAY COMMANDS ===
sl-logs             - follow worker service logs
sl-logs-web         - follow web service logs
sl-health           - run health check on production
sl-restart          - restart worker service
sl-db-prod          - connect to Neon production database

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
  pip install -r requirements.txt && pip install -e .
  ```

- [ ] **Mac: Set up local PostgreSQL** (optional, for offline dev)
  ```bash
  brew install postgresql && brew services start postgresql
  createdb storydump
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

*Last updated: 2026-03-03*
