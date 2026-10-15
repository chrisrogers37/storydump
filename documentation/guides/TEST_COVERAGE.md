# Test Coverage

What the suite covers, how coverage is measured, and the policy that stops a
green run from claiming coverage it never took. Running and writing tests is
[`testing-guide.md`](testing-guide.md).

This page does not list tests by name: the tree is the list, and the command
below prints it.

---

## The suite's shape

The load-harness scenarios are deselected by default. The suite is counted in
about a second, with no database:

```bash
pytest --collect-only -qqq --no-cov tests/    # one `path: count` line per file
```

| Directory | What it covers |
|---|---|
| `tests/scripts/` | The DB gates: the migration runner and the lineage lane, the advertised DDL, row-level security, the command port, the job lanes, the publish pipeline, the outbox, the ops views, and the real `storydump` CLI against the real app — on scratch PostgreSQL databases, one per test or module. A few files here need no database (the ratchets, `test_pr_ready.py`) |
| `tests/src/services/target/` | The target tier's services with their seams patched (the egress floor, Meta, Drive, Telegram) |
| `tests/storydump_cli/` | The `storydump` console against a scripted API — no network, no database |
| `tests/src/api/` | The API's routes over a fake engine whose session refuses SQL (`tests/src/api/conftest.py`) |
| `tests/src/` (top level) | The worker's composition root and entrypoint, the worker's `/health`, and the two legacy-removal guards (`test_legacy_tier_gone.py`, `test_legacy_settings_gone.py`) |
| `tests/` (top level) | The harness's own policy (`test_integration_coverage_policy.py`, `test_session_database_isolation.py`, `test_stray_database_reaping.py`), `test_legacy_cli_gone.py`, `test_deploy_guardrails.py`, `test_landing_env_example.py` (the landing's example environment names only what the front end reads) and `test_dependency_declarations.py` (`requirements.txt` and `setup.py`'s `install_requires` name the same runtime set) |
| `tests/src/channels/` | The Telegram transport and the webhook registration |
| `tests/src/utils/` | Encryption, datetime helpers, the logger |
| `tests/src/config/` | Settings and constants |
| `tests/src/exceptions/` | The exception base |

Outside the table:

- **`tests/scripts/load/`** — the load harness. Its scenarios carry the `load`
  marker and are *deselected* at collection unless `RUN_LOAD_HARNESS=1`
  (`tests/scripts/load/conftest.py:13`) — deselected rather than skipped, so
  they do not count against the skip ceiling below.
- **`landing/`** — the front end has its own tests (`npm test`, vitest), run by
  CI's `front-end` job.

Markers are declared in `pytest.ini` (`unit`, `integration`, `slow`, `load`)
and `--strict-markers` refuses any other. They are not a reliable "needs a
database" switch: most tests carry neither `unit` nor `integration`, and some
gates that take a database fixture are unmarked
(`tests/scripts/test_w4_tap_gate.py`). The directory is the better guide — the
database lives in `tests/scripts/`.

---

## How coverage is measured

- **Every plain `pytest` run measures line coverage of `src`.** `pytest.ini`'s
  `addopts` carries `--cov=src --cov-report=term-missing --cov-report=html`
  (`pytest.ini:9-15`), so the terminal report and `htmlcov/` are produced
  without asking. `--no-cov` turns it off.
- **CI measures `src` and `storydump_cli`**:
  `pytest tests/ -v --cov=src --cov=storydump_cli --cov-report=xml --cov-report=term-missing`
  (the `Run tests with coverage` step of `.github/workflows/ci.yml`), and uploads
  `coverage.xml` to Codecov in the `Upload coverage to Codecov` step, which is
  advisory: it continues on error and times out after five minutes, so an upload
  never fails `Test`.
- **No threshold is enforced.** Nothing sets `--cov-fail-under`, and the
  repository has no `.coveragerc` or `codecov.yml`. A percentage is reported,
  never gated.

This page states no percentage. A meaningful one needs the database-backed run
(most of the tests under `tests/scripts/` need a PostgreSQL), and the
current figure is whatever CI's `Test` job last printed — a number copied here
would be stale at the next merge.

---

## The integration-coverage policy

The number that matters more than a percentage is whether the database-backed
tests ran at all. Without a policy, a run whose database never came up
reports `N passed, M skipped` at exit 0 (#758). The policy lives in
`tests/conftest.py` as pure functions, and
`tests/test_integration_coverage_policy.py` pins its truth table without a
PostgreSQL:

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
  `pytest_sessionfinish` (`:88`, `:149`). The ceiling is hand-maintained: a
  new legitimate skip raises it on purpose.
- A missing precondition in the root suite — a test role without `CREATEROLE`
  or `CREATEDB` — skips locally and fails where the database is required
  (`precondition_absent`, `:331`). `tests/scripts/` skips on the same missing
  privilege (`owner_actor`, `tests/scripts/conftest.py:575`); where the
  database is required it is the skip ceiling that turns that into a failure.
- CI sets `REQUIRE_TEST_DATABASE: "1"` (in `ci.yml`). Set it locally whenever
  a green result is going to be reported anywhere.

What a laptop with no PostgreSQL sees: the root suite's database tests skip
(in `tests/test_stray_database_reaping.py`,
`tests/src/services/target/test_unit_of_work.py` and
`tests/test_integration_coverage_policy.py`), and a gate under
`tests/scripts/` that takes a database fixture **errors** at `admin_conn`
(`tests/scripts/conftest.py:380`) rather than skipping.
[`testing-guide.md`](testing-guide.md#giving-the-gates-a-server) has the
throwaway-server recipe that runs them.

---

## What is not covered

- **The live providers.** The suite holds no provider credential — CI's `Test`
  job sets only the `DB_*` components, `ENCRYPTION_KEY`,
  `REQUIRE_TEST_DATABASE` and `LOG_LEVEL` (in `ci.yml`). The provider
  seams are patched (`egress.request`, in
  `tests/src/services/target/conftest.py`) or pointed at a local double
  (`tests/scripts/load/fake_telegram.py`), so Meta, Google, Telegram and
  Cloudinary are exercised as recorded shapes, never live.
- **The deployed environment.** Railway, Neon and the registered webhook are
  checked from outside the suite: `storydump health`, `storydump doctor` and
  `storydump webhook status`.
