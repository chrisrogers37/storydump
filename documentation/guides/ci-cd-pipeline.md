# GitHub Actions CI/CD

A new database starts with `make init-db`: build from the models and
`src/models/target/ddl/`, adopt the empty `000_initial.sql` base marker,
then apply later migrations. The marker is never executed. The owner
role performs schema changes; the API uses `svc_ingress` and the worker
uses `svc_worker`. `make reset-db` drops and rebuilds a local database.

## Overview

The CI workflow defines jobs on GitHub-hosted runners (`ubuntu-latest`). It
runs on every pull request and every push to `main`, and Railway waits for it
before deploying.

---

## CI Pipeline (Automated)

`.github/workflows/ci.yml` runs on every push to `main`, `develop` and
`feature/*`, and on every pull request into `main` or `develop`. It declares
nine jobs, one of them a matrix of five test shards, and none is
path-filtered — every job runs on every PR:

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
  telegram-ratchet: # python scripts/telegram_ratchet.py      (stdlib only, no install)
  test-shard:       # matrix group: 1..TEST_SPLITS (5), a postgres:17 service each
                    # pip install -e '.[cli]'; fingerprint the collected suite
                    # pytest tests/ -v --cov=src --cov=storydump_cli
                    #   --splits 5 --group N   (pytest-split, .test_durations)
                    # REQUIRE_TEST_DATABASE=1
  test:             # needs every shard: passes only if all passed and all five
                    # fingerprints match; combined coverage on the run summary
  security:         # pip-audit -r requirements.txt               (GATES)
                    # bandit -r src/ storydump_cli/               (advisory)
  front-end:        # in landing/: npm ci, npm test, npx tsc --noEmit, npm run lint
                    # npm audit --omit=dev                        (GATES)
  public-record:    # python scripts/public_record.py --require-list --count-only
                    # (stdlib only; the list is the PRIVATE_IDENTIFIERS secret)
  changelog-check:  # pull requests only: the rule in changelog.d/README.md
  schema-consistency: # python -m scripts.schema_consistency
                      # current code vs pinned schema-code base + migration tail
                      # two independent disposable postgres:17 clusters
```

### What CI Checks

| Check | Tool | Purpose |
|-------|------|---------|
| Linting | `ruff check .` | Code style and errors, over the whole repository — `tests/` included |
| Formatting | `ruff format . --check` | Consistent code formatting |
| Telegram ratchet | `scripts/telegram_ratchet.py` | An allowlist of the modules permitted to reference Telegram: a reference outside it fails. Stdlib-only, so it runs without the app's dependencies |
| Tests | `pytest`, `pytest-split` | The whole suite, split across five `Test shard` jobs by the recorded test times in `.test_durations`, each against its own PostgreSQL 17 service (the `tests/scripts` suites hold a cluster-wide lock, so two shards cannot share one). `REQUIRE_TEST_DATABASE=1` makes a database that failed to come up a failure instead of a silent skip ([`TEST_COVERAGE.md`](TEST_COVERAGE.md)). The final `Test` job passes only if every shard passed and every shard fingerprinted the same collected suite, which is what makes each test run exactly once. It combines the shards' coverage of `src` and `storydump_cli` into the run summary; no threshold is enforced and coverage never fails the job |
| Security | `pip-audit`, `bandit` | Vulnerability scanning. `pip-audit` GATES: a known-vulnerable pin in `requirements.txt` fails the job. `bandit` is advisory (`\|\| true` + `continue-on-error`): its findings on this tree have not been triaged, so read the uploaded report rather than the step's colour. The npm dependencies of `landing/` are audited in Front end, below |
| Front end | `npm test`, `tsc --noEmit`, `npm run lint`, `npm audit --omit=dev` | The web app in `landing/` (Node 22). The audit GATES: a known advisory against a runtime dependency, direct or transitive, fails the job. It runs on every PR, so a newly published advisory turns every PR red until a bump lands; the step's note in `ci.yml` has its scope, why it has no severity floor, and how to clear an advisory. `next build` is left to Vercel, which builds every PR |
| Public record | `scripts/public_record.py --require-list` | The rule in `AGENTS.md` › The record: a tracked file, a commit message a push or pull request brings, or a pull request's title or body that names an identifier on the owner's private list fails, including an identifier of several words that a hard wrap splits across lines. The list is the `PRIVATE_IDENTIFIERS` repository secret, which GitHub withholds from a fork's pull request and from a run Dependabot starts, so the job skips both; once such a PR lands, the push run checks its files and the commit messages it brings, though not its description. A list of only blank lines and comments counts as no list. The CI log reports only how many lines matched, never where or which, because it is public; `scripts/lint.sh` shows the lines locally, in files and in the branch's commit messages, and a push run's log gives the `--commits` command for the commits it read. A commit message on `main` cannot be rewritten, since `main` refuses force pushes: a push whose run fails on one stays red and Railway skips its deploy, and the next push, whose run reads only its own commits, passes. Stdlib-only, like the ratchet |
| Changelog | `scripts/changelog_fragments.py check --base` | The fragment rule in `changelog.d/README.md`: a change outside the docs adds a fragment, and no pull request but the compile edits `CHANGELOG.md`. Stdlib-only, like the ratchet |
| Schema consistency | `python -m scripts.schema_consistency` | Compares the catalog built from current schema code with the pinned schema-code base plus its migration tail. Two independent disposable PostgreSQL 17 clusters keep roles and memberships separate. Catalog snapshots and differences are uploaded as an artifact |

The local `scripts/pr_ready.sh` helper requires eight checks by name: Lint,
Telegram ratchet, Test, Security Scan, Front End, Public record, Changelog
Check and Schema consistency. The `Test shard` matrix is declared to it as
covered by `Test`: its legs report as `Test shard 1` … `Test shard 5`, and
`Test` needs every one of them. It checks both lists against the workflow. GitHub rulesets govern
merge policy; review that configuration when changing job names. The helper's
expectations do not establish the remote ruleset settings.

### Test shards and their timings

pytest-split divides the collected suite into five contiguous chunks of about
equal recorded time, so the heavy `tests/scripts` gates spread across the
shards while each test file stays, with a few exceptions at chunk edges, in one
shard. A test missing from `.test_durations` gets the average time: stale
timings only make the shards uneven, they never skip a test.

When the shards drift apart (the slowest more than about 25% over the
fastest), refresh the file from a green `main` run: every such run's `Test`
job publishes the merged times as the `test-durations` artifact.

```bash
gh run download <run-id> -n test-durations   # writes .test_durations
```

Commit it at the repository root with a `### Tests` changelog fragment. To
change the number of shards, change `TEST_SPLITS` and the matrix's `group`
list in `ci.yml` together: each leg refuses to run if they disagree.

---

## CD Pipeline (Railway Auto-Deploy)

Railway deploys both services from `main` (`ci.yml:4`). For each service,
`railway.toml` decides what a deploy does:

1. **Build** — `pip install -r requirements.txt && pip install -e .`
2. **Pre-deploy** — `python -m scripts.migration_runner apply`: pending
   migrations are applied first, under the runner's advisory lock, so the two
   services' pre-deploys serialize and the second finds nothing pending. A
   failing migration aborts the deploy with the old version still serving. A
   pending gated file (`-- runner:manual`) is listed as owed and is not
   applied. Production, built from schema code, owes none. A database whose
   schema predates `001` and `002`, such as one converted from the retired
   chain, owes them, and the owner follows the
   [cutover runbook](../operations/schema-cutover.md).
3. **Start**, and the health check: `GET /health` must answer within 30 s
4. **Restart policy** — `ON_FAILURE`, 10 retries; the old deployment gets 60 s to drain

Railway waits for the commit's CI run to finish before deploying, and skips the
deploy when the run fails. The test shards are the long pole, so a deploy lands
about 15 minutes after a merge. Merge on green.

### Railway Services

| Service | Start Command | Purpose |
|---------|--------------|---------|
| `worker` | `python -m src.main` | The target worker (`src.worker`): the clock, the job lanes — planning slots, sending Telegram cards, publishing to Instagram, syncing Drive, refreshing credentials — and its own `/health` listener |
| `storydump` | `uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}` | The API: `/api/v1`, sign-in and the OAuth callbacks under `/auth`, the Telegram webhook, Meta's callbacks, the health surfaces |

Set each service's dashboard Start Command explicitly as described in the
[required start-command settings](cloud-deployment.md#required-dashboard-start-commands).

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

## CI and Deployment Credentials

- The CI workflow uses **GitHub-hosted runners** (`ubuntu-latest`)
- No self-hosted runners are used (avoids exposing infrastructure to malicious PRs)
- All production secrets are stored in **Railway environment variables** (never in code)
- Deployment is performed by Railway's GitHub integration, not by a CI job: no workflow holds a deploy credential

See: [GitHub's security warning about self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#self-hosted-runner-security)

---

## Environment Setup

### GitHub Secrets

Go to: `<repo>/settings/secrets/actions`

| Secret | Used by | Notes |
|--------|---------|-------|
| None | `ci.yml` | CI uses no secrets. The test job runs a PostgreSQL service with throwaway credentials and generates a fresh Fernet key per run |

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
source .venv/bin/activate
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

- **Python 3.12** in every job (`setup.py` `python_requires=">=3.12"`), the version
  Railway's Nixpacks build runs in production (its default; no variable pins it)
- **Node 22** for the front end (mirrored by `engines` in `landing/package.json`)
- **PostgreSQL 17** for every test shard and both Schema consistency clusters

## Cost

GitHub Actions is free for public repositories; Railway and Neon bill by
usage (their pricing pages, not this guide, are the reference).
