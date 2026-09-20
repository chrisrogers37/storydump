# GitHub Workflows

This directory holds two GitHub Actions workflows: `ci.yml`, the pull-request
and push gate, and `schema-drift.yml`, a scheduled audit of the live database.

---

## Current Setup

### Continuous Integration (Automated)

**File**: `ci.yml`

Runs on every push to `main`, `develop` and `feature/*`, and on every pull
request into `main` or `develop`, six jobs:

- **Lint** — `ruff check .` and `ruff format . --check`, the whole repository
  (`ruff.toml` declares the scope; nothing is left unlinted by a directory list)
- **FC-2 Telegram ratchet** — `python scripts/telegram_ratchet.py`: the
  allowlist of modules that may reference Telegram (stdlib-only, no install)
- **Test** — `pytest tests/ -v --cov=src --cov=storydump_cli` against a
  PostgreSQL 15 service container, with `REQUIRE_TEST_DATABASE=1` so a database
  that fails to come up fails the run instead of skipping the tests it backs
- **Security Scan** — pip-audit and bandit, both advisory: each step is
  `|| true` and `continue-on-error`, so the job cannot go red
- **Front End** — in `landing/`: `npm ci`, `npm test`, `npx tsc --noEmit`,
  `npm run lint` (Node 22; `next build` is deliberately absent — Vercel builds
  every PR)
- **Changelog Check** — pull requests only: `CHANGELOG.md` must change unless
  the PR touches only `documentation/`, `*.md` files or `.github/`

All jobs run on **GitHub's cloud runners** (`ubuntu-latest`) — safe for public
repositories. `main` declares no required status checks, so every check is
advisory to GitHub; merge on green is a rule, not an enforcement (a red Test
job has still made Railway skip a deploy — see the CI/CD guide).

### Scheduled: `schema-drift.yml`

Daily at 06:00 UTC (and on manual dispatch), the job **Live schema drift**
compares a live database's `public` schema with the schema the repository
declares (`tests/scripts/test_schema_drift_live.py`). Deliberately **not** a
pull-request gate — production lags `main` between merge and deploy, so red
would be its normal state for every migration PR. It needs the
`SCHEMA_DRIFT_DSN` repository secret (a read-only connection string); without
it the run reports **NOT CHECKED**, not a pass.

### Continuous Deployment (Railway Auto-Deploy)

Railway deploys when changes are pushed to `main`:
1. Push to `main` triggers Railway's build of both services (`worker` and
   `storydump`, the API): `railway.toml`'s `buildCommand`
2. Each service's pre-deploy step runs `python -m scripts.migration_runner apply`
   — pending migrations first; a failing migration aborts the deploy with the
   old version still serving
3. The new version must answer `GET /health` within 30 s (`railway.toml`)

No GitHub Actions deploy workflow is needed — Railway's GitHub integration
handles CD.

See: [documentation/guides/ci-cd-pipeline.md](../documentation/guides/ci-cd-pipeline.md)

---

## CI Failures

If CI is failing, common issues:

### Lint Errors
```bash
# Fix automatically
ruff format .

# Check for issues
ruff check .
```

### Test Failures
```bash
# Run tests locally — the database-backed tests need a PostgreSQL; without
# one they SKIP, which is not what CI does (it sets REQUIRE_TEST_DATABASE=1)
pytest tests/ -v

# Run specific test
pytest tests/path/to/test.py::test_name -v
```

The local recipe for a throwaway PostgreSQL is in `AGENTS.md` › Testing.

### Missing CHANGELOG Update
Every PR that changes behaviour should update `CHANGELOG.md` under
`## [Unreleased]`; docs-only PRs are exempt.

---

## For Contributors

When submitting a PR:
1. Update `CHANGELOG.md` (unless the PR is docs-only)
2. Ensure tests pass: `pytest tests/ -v`
3. Format code: `ruff format .`
4. Check linting: `ruff check .`

CI will verify these automatically when you open a PR.
