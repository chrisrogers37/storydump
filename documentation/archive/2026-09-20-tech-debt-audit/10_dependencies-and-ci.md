---
title: "Prune the eight dependencies nothing imports, reconcile setup.py with requirements.txt, and make the security job able to fail"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, ci]
links: []
---

# 10 — Dependencies and the security tier

| | |
|---|---|
| **PR title** | chore(deps): the eight packages nothing imports, `setup.py` in agreement with `requirements.txt`, and a security job that can fail |
| **Risk** | Low — no application code changes; the risk is a transitive import nobody noticed, which the import probe and the full suite catch |
| **Effort** | S (≈2h, most of it waiting on a clean-venv install and the suite) |
| **Files modified** | `requirements.txt`, `setup.py`, `.github/workflows/ci.yml`, `tests/test_dependency_declarations.py` (new), `CHANGELOG.md` |
| **Findings addressed** | TD-O1, TD-O2, TD-O3 |
| **Depends on** | nothing |
| **Blocks** | nothing (but do it before any dependency bump, so the bump lands on a real list) |

## Summary

`requirements.txt` pins eight packages that no module under `src/`, `storydump_cli/`, `scripts/`
or `tests/` imports — including the three `google-*` packages, which are not how this system talks
to Drive (that is httpx through the egress floor), and `anthropic`, which nothing calls at all.
`setup.py`'s `install_requires` drifts from that file in both directions and omits `asyncpg`, so
`pip install -e .` alone produces an API and a worker that cannot open a database connection.
CI's security job runs `pip-audit` and `bandit` behind `|| true` *and* `continue-on-error: true`,
with a placeholder advisory id (`GHSA-1234`) as its one suppression — it cannot go red, so the
tier the deployment guide advertises gates nothing. What must not change: every package a process
actually imports stays, at the pin it has today; this PR removes names, it does not bump versions.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-O1 | `requirements.txt:13,16,23,24,30,31,32,43` | Eight packages nothing imports |
| TD-O2 | `setup.py:8,11-28` | `install_requires` drifts both ways from `requirements.txt` and omits `asyncpg`; `author="Your Name"` |
| TD-O3 | `.github/workflows/ci.yml:157-163` | `pip-audit`/`bandit` behind `|| true` + `continue-on-error` with a placeholder ignore |

## Dependencies

None. Independent of every other doc in this session — it touches no module any of them edit.
Land it early: docs 01–09 all end with a full-suite run, and each is cheaper against a smaller
install.

## Implementation Plan

### Steps

1. **Prove each of the eight is unimported before removing it.** Run, and paste the output into
   the PR:

   ```bash
   for m in alembic httpx2 dateutil tenacity googleapiclient google_auth_oauthlib anthropic; do
     echo "== $m: $(grep -rlE "^\s*(import|from) $m(\.|\s|$)" src storydump_cli scripts tests --include='*.py' | wc -l | tr -d ' ') files"
   done
   grep -rnE "^\s*(import|from) google(\.|\s)" src storydump_cli scripts tests --include='*.py'
   ```

   Every count must be `0` and the last grep must print nothing. (`python-dateutil` imports as
   `dateutil`, `google-api-python-client` as `googleapiclient`, `google-auth` as `google.auth` /
   `google.oauth2` — hence the separate grep.) If any count is non-zero, that package stays and
   the step says so.

2. **Delete the eight lines and the two comment headers that become orphans** —
   `requirements.txt`.

   Before (`:12-16`, `:22-24`, `:29-32`, `:42-43`):
   ```
   asyncpg==0.31.0
   alembic==1.18.4

   httpx==0.28.1
   httpx2==2.2.0

   # Utilities
   python-dateutil==2.9.0.post0
   tenacity==9.1.2

   # Google Drive (Cloud Media Phase 02)
   google-api-python-client==2.196.0
   google-auth==2.53.0
   google-auth-oauthlib==1.4.0

   # AI Caption Generation
   anthropic==0.102.0
   ```

   After:
   ```
   asyncpg==0.31.0

   httpx==0.28.1
   ```

   The `# Utilities`, `# Google Drive (Cloud Media Phase 02)` and `# AI Caption Generation`
   headers go with their only contents. Leave every other line and pin untouched.

3. **Do not remove these, though a naive grep suggests it** — `python-multipart` (no import
   statement; FastAPI requires it for the `Form(default="")` parameters at
   `src/api/routes/meta.py:56` and `:86`, and the app raises at import time without it),
   `python-dotenv` (pydantic-settings loads `.env` through it — `src/config/settings.py:145`
   documents the resolution order), `psycopg2-binary` (the migration runner and every DB gate),
   `sqlalchemy` (the async engine wrapper over asyncpg; 52 importing files), `cloudinary`,
   `cryptography`, `uvicorn`. State this list in the PR body so a reviewer does not re-derive it.

4. **Make `setup.py`'s runtime set the real one** — `setup.py:11-28`.

   Before:
   ```python
   install_requires=[
       "alembic>=1.18.0",
       "click>=8.1.7",
       "cloudinary>=1.36.0",
       "cryptography>=41.0.0",
       "fastapi>=0.109.0",
       "google-api-python-client>=2.100.0",
       "google-auth>=2.23.0",
       "google-auth-oauthlib>=1.1.0",
       "httpx>=0.25.2",
       "psycopg2-binary>=2.9.9",
       "pydantic>=2.5.0",
       "pydantic-settings>=2.1.0",
       "python-dateutil>=2.8.2",
       "python-dotenv>=1.0.0",
       "rich>=13.7.0",
       "sqlalchemy>=2.0.23",
       "uvicorn>=0.27.0",
   ],
   ```

   After (floors, not pins — `requirements.txt` holds the pins; the two files have different
   jobs and only their *names* must agree):
   ```python
   install_requires=[
       "asyncpg>=0.29.0",
       "click>=8.1.7",
       "cloudinary>=1.36.0",
       "cryptography>=41.0.0",
       "fastapi>=0.109.0",
       "httpx>=0.25.2",
       "psycopg2-binary>=2.9.9",
       "pydantic>=2.5.0",
       "pydantic-settings>=2.1.0",
       "python-dotenv>=1.0.0",
       "python-multipart>=0.0.9",
       "rich>=13.7.0",
       "sqlalchemy>=2.0.23",
       "uvicorn>=0.27.0",
   ],
   ```

   Also `setup.py:8`: `author="Your Name"` → `author="Chris Rogers"`.

5. **Pin the agreement so it cannot drift again** — new `tests/test_dependency_declarations.py`,
   in the style of the repo's other document pins (`tests/test_agent_docs.py`,
   `tests/test_landing_env_example.py`): parse the two files, compare the runtime name sets
   (excluding `requirements.txt`'s `pytest*` test block and `setup.py`'s `cli` extra), and fail
   naming the offending package in both directions. One test for "every runtime name in
   `requirements.txt` is in `install_requires`", one for the converse. Normalize names
   (`-`/`_`, case) before comparing.

6. **Install from scratch and prove nothing transitive was load-bearing.**

   ```bash
   python3 -m venv /tmp/dep-check && /tmp/dep-check/bin/pip install -r requirements.txt \
     && /tmp/dep-check/bin/pip install -e '.[cli]' \
     && /tmp/dep-check/bin/python -c "import src.api.app, src.worker, src.main, storydump_cli.main; print('ok')" \
     && /tmp/dep-check/bin/python -c "import scripts.migration_runner, scripts.schema_parity, scripts.tenancy_gate, scripts.telegram_ratchet; print('scripts ok')"
   ```

   Both must print. This is the step that catches a package imported through a name the grep in
   step 1 did not spell.

7. **Make the security job able to fail** — `.github/workflows/ci.yml:157-163`.

   Before:
   ```yaml
   - name: Run pip-audit
     run: pip-audit -r requirements.txt --ignore-vuln GHSA-1234 || true
     continue-on-error: true

   - name: Run bandit security scan
     run: bandit -r src/ storydump_cli/ -ll -ii --format json --output bandit-report.json || true
     continue-on-error: true
   ```

   After:
   ```yaml
   - name: Run pip-audit
     run: pip-audit -r requirements.txt

   - name: Run bandit security scan
     # Advisory for now: bandit's findings on this tree have not been triaged, so it
     # reports without gating. pip-audit above DOES gate — a known-vulnerable pin fails CI.
     run: bandit -r src/ storydump_cli/ -ll -ii --format json --output bandit-report.json || true
     continue-on-error: true
   ```

   **Run both locally first and record the output in the PR** — `pip-audit -r requirements.txt`
   must be clean on the pruned file before the `|| true` comes off, or this PR turns CI red on
   landing:

   ```bash
   /tmp/dep-check/bin/pip install pip-audit bandit
   /tmp/dep-check/bin/pip-audit -r requirements.txt
   /tmp/dep-check/bin/bandit -r src/ storydump_cli/ -ll -ii
   ```

   If `pip-audit` reports an advisory, **stop**: bumping a pin is a different PR with its own
   test run (precedent: #762). Land steps 1–6 alone and open the bump separately. If a real,
   reviewed suppression is needed, replace `GHSA-1234` with the real id and a comment naming why
   — never leave a placeholder.

8. **Keep `bandit`'s report upload as is** (`ci.yml:165-170`, `if: always()`) — it still
   publishes the artifact whether or not the step gated.

9. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **The dependency list is what the code imports (#1216).** Eight packages nothing under
   > `src/`, `storydump_cli/`, `scripts/` or `tests/` imports are gone from `requirements.txt` —
   > `alembic`, `httpx2`, `python-dateutil`, `tenacity`, the three `google-*` packages (Drive and
   > the Meta Graph API are reached over httpx through the egress floor, never a Google client
   > library) and `anthropic`. `setup.py`'s `install_requires` now names the same runtime set:
   > it gains `asyncpg` and `python-multipart` — a `pip install -e .` without
   > `requirements.txt` previously produced an API and a worker that could not open a connection
   > and refused the Meta `Form()` routes — and loses the five it listed that nothing imports.
   > `tests/test_dependency_declarations.py` holds the two files' runtime names equal, so the
   > next addition lands in both. CI's `pip-audit` now gates: it ran behind `|| true` with a
   > placeholder `--ignore-vuln GHSA-1234`, so the advertised security tier could not go red
   > (bandit stays advisory, and says so).

## Test Plan

- **Pre-change baseline** on the parent commit, recorded in the PR:
  `REQUIRE_TEST_DATABASE=1 pytest --no-cov` — green (the suite is ~3,750 tests and needs a real
  PostgreSQL; `AGENTS.md` §Testing has the throwaway-server recipe).
- **The characterization for this change is the import probe of step 6**, not a test file: the
  suite imports what it tests, but a package that only a deployed entrypoint needs would not show
  up there — hence probing `src.api.app`, `src.worker`, `src.main`, `storydump_cli.main` and the
  four standalone scripts in a venv built only from the pruned files.
- **Targeted:** `pytest tests/test_dependency_declarations.py --no-cov` (new),
  `pytest tests/src/api/test_meta_routes.py --no-cov` if it exists, else
  `pytest tests/src/api -k meta --no-cov` — the `Form()` routes are what `python-multipart`
  is there for.
- **New pin:** `tests/test_dependency_declarations.py` asserts the runtime name sets of
  `requirements.txt` and `setup.py::install_requires` are equal, naming any package on only one
  side.
- **Post-change:** the same baseline command, plus step 6's venv probe and step 7's local
  `pip-audit`/`bandit` run.

## Verification Checklist

- [ ] baseline `REQUIRE_TEST_DATABASE=1 pytest --no-cov` green on the parent commit, output in the PR
- [ ] step 1's grep output pasted in the PR: all eight counts `0`, the `google.` grep empty
- [ ] clean-venv install + both import probes print `ok` / `scripts ok`
- [ ] `pip-audit -r requirements.txt` clean locally **before** `|| true` is removed, output in the PR
- [ ] `pytest tests/test_dependency_declarations.py --no-cov` green
- [ ] full suite green after
- [ ] `ruff check . && ruff format --check .`
- [ ] manual smoke: `python -c "from src.config.settings import settings"` (the `make validate-env` check) and `storydump --help` in the clean venv
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not bump any pin in this PR.** Removing names and changing versions in one diff makes a
  regression un-bisectable. If `pip-audit` finds an advisory, land steps 1–6 and open the bump
  separately.
- **Do not remove `python-multipart`, `python-dotenv`, `psycopg2-binary` or `sqlalchemy`** because
  a grep for their import name comes back thin — step 3 explains each.
- **Do not make `bandit` gate** in the same PR as the pruning. Its findings on this tree have not
  been triaged; a first triage is its own piece of work.
- **Do not "fix" the placeholder by suppressing a real advisory** to keep CI green. A real
  suppression carries a comment naming the advisory and why it does not apply.
- **Do not add `requirements-dev.txt` or a lockfile** here. The two-file split is the repo's
  existing shape; changing the packaging model is not tech-debt cleanup.
- **Do not touch `landing/package.json`** — its unused `jose` is doc 14's step.

## Related

- `00_TECH_DEBT.md` — the audit this plan comes from (findings O1–O3).
- `13_legacy-instruments.md` — the other "delete what nothing uses" PR, on the scripts side.
- `documentation/guides/ci-cd-pipeline.md` — describes the security tier this PR makes real.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/orchestrator.md`, findings TD-O1, TD-O2, TD-O3.
