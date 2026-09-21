# GitHub Actions CI/CD

## Overview

Storydump uses GitHub Actions for continuous integration and Railway for continuous deployment. Since the project is hosted on a public repository, all CI runs on GitHub's cloud runners (`ubuntu-latest`).

---

## CI Pipeline (Automated)

`.github/workflows/ci.yml` runs on every push to `main`, `develop` and
`feature/*`, and on every pull request into `main` or `develop`. It has six
jobs, and none is path-filtered — every job runs on every PR:

```yaml
# .github/workflows/ci.yml (abridged — the file is the authority)
name: CI

on:
  push:
    branches: [main, develop, 'feature/*']
  pull_request:
    branches: [main, develop]

jobs:
  lint:             # ruff check . && ruff format . --check   (the whole repository)
  fc2-ratchet:      # python scripts/telegram_ratchet.py      (stdlib only, no install)
  test:             # pip install -e '.[cli]'
                    # pytest tests/ -v --cov=src --cov=storydump_cli
                    # against a postgres:15 service, REQUIRE_TEST_DATABASE=1
  security:         # pip-audit -r requirements.txt               (GATES)
                    # bandit -r src/ storydump_cli/               (advisory)
  front-end:        # in landing/: npm ci, npm test, npx tsc --noEmit, npm run lint
  changelog-check:  # pull requests only: CHANGELOG.md must change
```

### What CI Checks

| Check | Tool | Purpose |
|-------|------|---------|
| Linting | `ruff check .` | Code style and errors, over the whole repository — `tests/` included |
| Formatting | `ruff format . --check` | Consistent code formatting |
| FC-2 ratchet | `scripts/telegram_ratchet.py` | An allowlist of the modules permitted to reference Telegram: a reference outside it fails. Stdlib-only, so it runs without the app's dependencies |
| Tests | `pytest` | The whole suite against a PostgreSQL 15 service. `REQUIRE_TEST_DATABASE=1` makes a database that failed to come up a failure instead of a silent skip ([`TEST_COVERAGE.md`](TEST_COVERAGE.md)). Coverage of `src` and `storydump_cli` is measured and uploaded to Codecov; no threshold is enforced |
| Security | `pip-audit`, `bandit` | Vulnerability scanning. `pip-audit` GATES: a known-vulnerable pin in `requirements.txt` fails the job. It did not until #1216 — it ran behind `\|\| true` *and* `continue-on-error` with a placeholder `--ignore-vuln GHSA-1234`, and was masking four live advisories the whole time. `bandit` is still advisory (`\|\| true` + `continue-on-error`): its findings on this tree have not been triaged, so read the uploaded report rather than the step's colour |
| Front end | `npm test`, `tsc --noEmit`, `npm run lint` | The web app in `landing/` (Node 22). `next build` is left to Vercel, which builds every PR |
| Changelog | Custom check | A pull request must change `CHANGELOG.md`, unless it touches only `documentation/`, `*.md` files or `.github/` |

The workflow's own note (`ci.yml:195-199`) records that `main` declares no
required status checks, so every check is advisory as far as GitHub is
concerned. Treat a red check as blocking anyway: a red check has made Railway
skip a deploy (below).

### The scheduled schema-drift audit

`.github/workflows/schema-drift.yml` is not part of a pull request. It runs
daily at 06:00 UTC (and on manual dispatch) and compares a live database's
`public` schema with the schema the repository declares
(`tests/scripts/test_schema_drift_live.py`). It is deliberately not a PR gate:
production lags `main` between merge and deploy, so it would be red as its
normal state. Without its secret it reports **NOT CHECKED** rather than passing.

---

## CD Pipeline (Railway Auto-Deploy)

Railway deploys both services from `main` (`ci.yml:4`). For each service,
`railway.toml` decides what a deploy does:

1. **Build** — `pip install -r requirements.txt && pip install -e . && mkdir -p /tmp/media`
2. **Pre-deploy** — `python -m scripts.migration_runner apply`: pending
   migrations are applied first, under the runner's advisory lock, so the two
   services' pre-deploys serialize and the second finds nothing pending. A
   failing migration aborts the deploy with the old version still serving. A
   gated file (`-- runner:manual`) is listed as owed and is not applied
   ([`migration-runner.md`](../operations/migration-runner.md))
3. **Start**, and the health check: `GET /health` must answer within 30 s
4. **Restart policy** — `ON_FAILURE`, 10 retries; the old deployment gets 60 s to drain

A red CI check can hold a deploy back: three merges of the tear-out whose
`Test` job went red had the API's deploys skipped by Railway
(`tests/scripts/test_l5_pipeline_gate.py:2378-2381` records it). Merge on green.

### Railway Services

| Service | Start Command | Purpose |
|---------|--------------|---------|
| `worker` | `python -m src.main` | The target worker (`src.worker`): the clock, the job lanes — planning slots, sending Telegram cards, publishing to Instagram, syncing Drive, refreshing credentials — and its own `/health` listener |
| `storydump` | `uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}` | The API: `/api/v1`, sign-in and the OAuth callbacks under `/auth`, the Telegram webhook, Meta's callbacks, the health surfaces |

The web front end (`landing/`) deploys to Vercel, not Railway
([`landing-vercel-deployment.md`](landing-vercel-deployment.md)).

### Monitoring Deploys

```bash
# Follow the deploy of a commit on both services:
# exit 0 when both reach SUCCESS, 6 on FAILED, CRASHED or REMOVED
storydump deploys --watch --commit <sha>

# The logs
railway logs --service worker
railway logs --service storydump

# Verify health after deploy
storydump health

# What the deploy did on the ledger, as one timeline
storydump burst --since <the deploy's time> --watch
```

---

## Security for Public Repos

Since this is a public repository:

- All CI runs on **GitHub cloud runners** (`ubuntu-latest`) -- safe for public repos
- No self-hosted runners are used (avoids exposing infrastructure to malicious PRs)
- All production secrets are stored in **Railway environment variables** (never in code)
- Deployment is performed by Railway's GitHub integration, not by a CI job: no workflow holds a deploy credential

See: [GitHub's security warning about self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#self-hosted-runner-security)

---

## Environment Setup

### GitHub Secrets

Go to: `https://github.com/chrisrogers37/storydump/settings/secrets/actions`

| Secret | Used by | Notes |
|--------|---------|-------|
| None | `ci.yml` | CI uses no secrets. The test job runs a PostgreSQL service with throwaway credentials and generates a fresh Fernet key per run |
| `SCHEMA_DRIFT_DSN` | `schema-drift.yml` | A **read-only** connection string for the database to audit. Unset, the workflow reports NOT CHECKED |

### Railway Secrets (for CD)

All production secrets are configured in the Railway dashboard:
- `DATABASE_URL`, `TARGET_DATABASE_URL`, `TARGET_TELEGRAM_BOT_TOKEN`, `ENCRYPTION_KEY`, etc.
- See [cloud-deployment.md](cloud-deployment.md) for the full variable reference, by service

---

## Advantages

- **Secure** -- No infrastructure exposed to public PRs
- **Cloud runners** -- No local resources used for CI
- **Auto-deploy** -- Railway deploys on push to main, migrations first
- **Simple** -- No VPN, SSH keys, or tunneling required

---

## Troubleshooting

### CI Failing

```bash
# Run checks locally before pushing
source venv/bin/activate
ruff check .
ruff format --check .
pytest
```

A green local run that skipped its database tests is not CI's run: give the
suite a PostgreSQL and set `REQUIRE_TEST_DATABASE=1`
([`testing-guide.md`](testing-guide.md)).

### Railway Deploy Not Triggering

- Verify Railway GitHub integration is connected
- Check Railway dashboard for build errors
- Ensure the branch matches Railway's configured branch
- Check the commit's CI: a red check can make Railway skip the deploy

### Service Not Starting After Deploy

```bash
# Check Railway logs
railway logs --service worker

# Common issues:
# - the pre-deploy step failed -> a migration failed, or DATABASE_URL is
#   missing or is not the owner login; the old version keeps serving
# - the worker exits 2 -> TARGET_DATABASE_URL is unset on the worker
# - the API answers 503 on data routes -> TARGET_DATABASE_URL is unset on the API
# - Build failed -> Check build logs
```

---

## Runtimes CI pins

- **Python 3.10** in every job (`setup.py` `python_requires=">=3.10"`); Railway
  builds with NIXPACKS' default Python, so the pin is CI's, not the deploy's
- **Node 22** for the front end (mirrored by `engines` in `landing/package.json`)
- **PostgreSQL 15** as the Test job's service container

## Cost

GitHub Actions is free for public repositories; Railway and Neon bill by
usage (their pricing pages, not this guide, are the reference).
