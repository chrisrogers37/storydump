# GitHub Workflows

A new database starts with `make init-db`: build from the models and
`src/models/target/ddl/`, adopt the empty `000_initial.sql` base marker,
then apply later migrations. The marker is never executed. The owner
role performs schema changes; the API uses `svc_ingress` and the worker
uses `svc_worker`. `make reset-db` drops and rebuilds a local database.

This directory holds one GitHub Actions workflow: `ci.yml`, the pull-request
and push gate.

---

## Current Setup

### Continuous Integration (Automated)

**File**: `ci.yml`

Runs on every push to `main`, `develop` and `feature/*`, and on every pull
request into `main` or `develop`, six jobs:

- **Lint** — `ruff check .` and `ruff format . --check`, the whole repository
  (`ruff.toml` declares the scope; nothing is left unlinted by a directory list)
- **Telegram ratchet** — `python scripts/telegram_ratchet.py`: the
  allowlist of modules that may reference Telegram (stdlib-only, no install)
- **Test** — `pytest tests/ -v --cov=src --cov=storydump_cli` against a
  PostgreSQL 15 service container, with `REQUIRE_TEST_DATABASE=1` so a database
  that fails to come up fails the run instead of skipping the tests it backs
- **Security Scan** — pip-audit gates (a known-vulnerable pin in
  `requirements.txt` fails the job); bandit is advisory (`|| true` and
  `continue-on-error`)
- **Front End** — in `landing/`: `npm ci`, `npm test`, `npx tsc --noEmit`,
  `npm run lint` (Node 22; `next build` is deliberately absent — Vercel builds
  every PR), then `npm audit --omit=dev`, which gates: a known advisory
  against the runtime dependencies fails the job
- **Changelog Check** — pull requests only, `scripts/changelog_fragments.py
  check --base`: the fragment rule in `changelog.d/README.md`

All jobs run on **GitHub's cloud runners** (`ubuntu-latest`) — safe for public
repositories. The `main` ruleset requires a pull request and all six of these
checks by name (Lint, Telegram ratchet, Test, Security Scan, Front End,
Changelog Check), so GitHub refuses a merge until they are green. Renaming one
of those jobs means updating the ruleset too.

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

The local recipe for a throwaway PostgreSQL is in the
[testing guide](../documentation/guides/testing-guide.md#giving-the-gates-a-server).

### Missing changelog fragment
Every PR that changes behaviour adds one fragment in `changelog.d/` and leaves
`CHANGELOG.md` alone; `changelog.d/README.md` has the format and the rule.

### Front End: npm audit
A newly published advisory against a runtime dependency fails every PR's
Front End, including PRs that change nothing in `landing/`, until a bump lands.
```bash
# The step, locally; it reads only the lockfile, so no install is needed
cd landing && npm audit --omit=dev
```

Clear it with a bump, or an `overrides` pin in `landing/package.json`. For an
advisory with no fixed release, see the note on the step in `ci.yml`.

---

## For Contributors

When submitting a PR:
1. Add a changelog fragment in `changelog.d/` (unless the PR is docs-only)
2. Ensure tests pass: `pytest tests/ -v`
3. Format code: `ruff format .`
4. Check linting: `ruff check .`

CI will verify these automatically when you open a PR.
