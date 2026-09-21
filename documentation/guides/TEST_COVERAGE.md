# Test Coverage

What the suite covers, how coverage is measured, and the policy that stops a
green run from claiming coverage it never took. Running and writing tests is
[`testing-guide.md`](testing-guide.md).

This page used to be a per-file table of the legacy tier's tests (the
repositories, the polling bot, the old CLI); those tests were deleted with the
tier in the tear-out (#1216, September 2026). It no longer lists tests by name:
the tree is the list, and the command below prints it.

---

## The suite's shape

Collected on 2026-09-20: **3,709 tests in 158 files**, plus the 5 load-harness
scenarios, which are deselected by default. Re-measure in about a second, with
no database:

```bash
pytest --collect-only -qqq --no-cov tests/    # one `path: count` line per file
```

| Directory | What it covers | Files | Tests |
|---|---|---|---|
| `tests/scripts/` | The DB gates: the migration runner and the lineage lane, the advertised DDL, row-level security, the command port, the job lanes, the publish pipeline, the outbox, the ops views, and the real `storydump` CLI against the real app — on scratch PostgreSQL databases, one per test or module. A few files here need no database (the two monitors' `classify`, the ratchets, `test_pr_ready.py`) | 67 | 1,381 |
| `tests/src/services/target/` | The target tier's services with their seams patched (the egress floor, Meta, Drive, Telegram) | 47 | 1,128 |
| `tests/storydump_cli/` | The `storydump` console against a scripted API — no network, no database | 11 | 426 |
| `tests/src/api/` | The API's routes over a fake engine whose session refuses SQL (`tests/src/api/conftest.py`) | 9 | 327 |
| `tests/src/` (top level) | The worker's composition root and entrypoint, the worker's `/health`, and the two tear-out guards (`test_legacy_tier_gone.py`, `test_legacy_settings_gone.py`) | 5 | 167 |
| `tests/` (top level) | The harness's own policy (`test_integration_coverage_policy.py`, `test_session_database_isolation.py`, `test_stray_database_reaping.py`), the documentation pins (`test_agent_docs.py`, `test_legacy_cli_gone.py`, `test_meta_runbook_markers.py`), `test_deploy_guardrails.py`, `test_landing_env_example.py` (the landing's example environment names only what the front end reads) and `test_dependency_declarations.py` (`requirements.txt` and `setup.py`'s `install_requires` name the same runtime set) | 9 | 126 |
| `tests/src/channels/` | The Telegram transport and the webhook registration | 2 | 66 |
| `tests/src/utils/` | Encryption, datetime helpers, the logger | 4 | 47 |
| `tests/src/config/` | Settings and constants | 3 | 36 |
| `tests/src/exceptions/` | The exception base | 1 | 5 |

Outside that count:

- **`tests/scripts/load/`** — the load harness. Its scenarios carry the `load`
  marker and are *deselected* at collection unless `RUN_LOAD_HARNESS=1`
  (`tests/scripts/load/conftest.py:13`) — deselected rather than skipped, so
  they do not count against the skip ceiling below.
- **`tests/mutations/*.sh`** — the per-phase mutation batteries. Each plants
  one named mutation per pinned behaviour, expects the named test to fail, and
  restores the file from the committed tree. They are run by hand per phase and
  are not part of `pytest` or CI.
- **`landing/`** — the front end has its own tests (`npm test`, vitest), run by
  CI's `front-end` job.

Markers are declared in `pytest.ini` (`unit`, `integration`, `slow`, `load`)
and `--strict-markers` refuses any other. They are not a reliable "needs a
database" switch: on the same collection `-m unit` selects 127 tests and
`-m integration` 950, most tests carry neither, and some gates that take a
database fixture are unmarked (`tests/scripts/test_w4_tap_gate.py`). The
directory is the better guide — the database lives in `tests/scripts/`.

---

## How coverage is measured

- **Every plain `pytest` run measures line coverage of `src`.** `pytest.ini`'s
  `addopts` carries `--cov=src --cov-report=term-missing --cov-report=html`
  (`pytest.ini:9-15`), so the terminal report and `htmlcov/` are produced
  without asking. `--no-cov` turns it off.
- **CI measures `src` and `storydump_cli`**:
  `pytest tests/ -v --cov=src --cov=storydump_cli --cov-report=xml --cov-report=term-missing`
  (`.github/workflows/ci.yml:134`), and uploads `coverage.xml` to Codecov with
  `fail_ci_if_error: false` (`ci.yml:136-140`).
- **No threshold is enforced.** Nothing sets `--cov-fail-under`, and the
  repository has no `.coveragerc` or `codecov.yml`. A percentage is reported,
  never gated.

This page states no percentage. A meaningful one needs the database-backed run
(most of the 1,363 tests under `tests/scripts/` need a PostgreSQL), and the
current figure is whatever CI's `Test` job last printed — a number copied here
would be stale at the next merge.

---

## The integration-coverage policy

The number that matters more than a percentage is whether the database-backed
tests ran at all. A run whose database never came up used to report
`N passed, M skipped` at exit 0 (#758). The policy lives in `tests/conftest.py`
as pure functions, and `tests/test_integration_coverage_policy.py` (28 tests)
pins its truth table without a PostgreSQL:

| PostgreSQL at `DB_HOST:DB_PORT` | `REQUIRE_TEST_DATABASE` | Verdict |
|---|---|---|
| answered | either | **run** |
| nothing listening | unset | **skip** — the one honest skip: a contributor with no PostgreSQL |
| nothing listening | `1` / `true` / `yes` | **fail** — the session fixture raises, so every test errors instead of skipping |

- The verdict is `integration_verdict` (`tests/conftest.py:98`); the session
  fixture `setup_test_database` (`:708`) applies it.
- "Answered" includes a server that *refused* the connection: a bad password
  or a connection limit is a configured database that failed, not an absent
  one, so the tests run and fail rather than skip (`server_answered`, `:233`;
  #769). `TestTheRefusalAgainstARealServer` re-proves that premise against a
  real server with no mocks (#804).
- **The skip ceiling**: where the database is required, more than
  `MAX_EXPECTED_SKIPS = 1` skipped tests fails the session at
  `pytest_sessionfinish` (`:88`, `:149`). The one expected skip is the live
  schema-drift audit, `tests/scripts/test_schema_drift_live.py`, which skips
  without `SCHEMA_DRIFT_DSN` and runs only in the scheduled `schema-drift`
  workflow. The ceiling is hand-maintained: a new legitimate skip raises it on
  purpose.
- A missing precondition in the root suite — a test role without `CREATEROLE`
  or `CREATEDB` — skips locally and fails where the database is required
  (`precondition_absent`, `:331`). `tests/scripts/` skips on the same missing
  privilege (`owner_actor`, `tests/scripts/conftest.py:575`); where the
  database is required it is the skip ceiling that turns that into a failure.
- CI sets `REQUIRE_TEST_DATABASE: "1"` (`ci.yml:131`). Set it locally whenever
  a green result is going to be reported anywhere.

What a laptop with no PostgreSQL sees, measured on 2026-09-18: the root
suite's database tests skip (17 in `tests/test_stray_database_reaping.py`, 6 in
`tests/src/services/target/test_unit_of_work.py`, 3 in
`tests/test_integration_coverage_policy.py`), and a gate under
`tests/scripts/` that takes a database fixture **errors** at `admin_conn`
(`tests/scripts/conftest.py:380`) rather than skipping. `AGENTS.md` (Testing)
has the throwaway-server recipe that runs them.

---

## What is not covered

- **The live providers.** The suite holds no provider credential — CI's `Test`
  job sets only the `DB_*` components, `ENCRYPTION_KEY`,
  `REQUIRE_TEST_DATABASE` and `LOG_LEVEL` (`ci.yml:116-132`). The provider
  seams are patched (`egress.request`, in
  `tests/src/services/target/conftest.py`) or pointed at a local double
  (`tests/scripts/load/fake_telegram.py`), so Meta, Google, Telegram and
  Cloudinary are exercised as recorded shapes, never live.
- **The deployed environment.** Railway, Neon and the registered webhook are
  checked from outside the suite: `storydump health`, `storydump doctor`,
  `storydump webhook status`, and the fleet monitors
  (`documentation/operations/monitoring.md`).
- **Production's schema** is audited by the scheduled `schema-drift` workflow,
  not by a pull request — production lags `main` between merge and deploy, so
  a PR-time check would be red as its normal state
  (`.github/workflows/schema-drift.yml`).
