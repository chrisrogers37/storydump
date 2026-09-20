# Testing Guide

How the suite is laid out, how to run it, what needs a PostgreSQL, and the
patterns the tests follow. What is covered and how coverage is measured is
[`TEST_COVERAGE.md`](TEST_COVERAGE.md) (3,675 tests in 156 files on
2026-09-20; that page has the command that re-counts them).

The suite tests the target tier only. The legacy tier's tests — the
repositories, the polling bot's handlers, the old CLI, `tests/integration/` —
were deleted with it in the tear-out (#1216, September 2026), and the fixtures
they used (`test_db`, a schema built from the ORM models) went with them.

## Quick Start

```bash
# Everything (coverage of src is measured on every run — pytest.ini's addopts)
pytest

# One area, one file, one test
pytest tests/src/services/target/
pytest tests/storydump_cli/test_main.py
pytest tests/storydump_cli/test_main.py::test_whoami_json_is_one_envelope_of_kind_whoami

# Faster: no coverage
pytest --no-cov

# Only what failed last time
pytest --lf
```

The `make` targets wrap the same commands and assume the virtualenv is `./venv/`:

| Target | Runs |
|---|---|
| `make test` | `pytest -v --cov=src --cov-report=term-missing` |
| `make test-unit` | `pytest -v -m unit` |
| `make test-integration` | `pytest -v -m integration` |
| `make test-quick` | `pytest -v --no-cov` — the run without coverage |
| `make test-failed` | `pytest -v --lf` |
| `make test-watch` | `ptw -- -v` — needs `pytest-watch`, which `requirements.txt` does not install |

## What Needs a Database

| Where | Needs PostgreSQL? |
|---|---|
| `tests/src/`, `tests/storydump_cli/` | No — seams are patched, engines are fakes, the API is scripted. The exceptions are marked `integration` and skip without a server (`tests/src/services/target/test_unit_of_work.py`) |
| `tests/` (top level) | The harness's own policy tests: mostly pure; the ones that need a server skip without it |
| `tests/scripts/` | **Yes** — the DB gates. Each builds its world in a scratch database and acts as the production roles. A few files are database-free (`test_posting_monitor.py`, `test_scheduling_monitor.py`, `test_telegram_ratchet.py`, `test_pr_ready.py`) |

With no PostgreSQL at `DB_HOST:DB_PORT` the root suite's database tests
**skip**, and a gate under `tests/scripts/` that takes a database fixture
**errors** at `admin_conn` (`tests/scripts/conftest.py:380`) — measured on
2026-09-18. With `REQUIRE_TEST_DATABASE=1` a missing server fails the session
instead: that is CI's setting, and the one to use locally whenever a green
result is going to be reported anywhere.

### Giving the gates a server

The gates need a PostgreSQL 15, a login that is a superuser or holds
`CREATEROLE` and `CREATEDB`, and a `psql` on your `PATH` — the fixtures apply
SQL files through it (`tests/scripts/conftest.py:1093-1108`). A throwaway
container shaped like CI's is enough. The `docker run` line and the environment
the gates read are written down once, in [`AGENTS.md` › Testing](../../AGENTS.md#testing),
so the two pages cannot disagree about a port or a variable.

## The Test Databases

Nothing is set up by hand, and nothing is shared between runs.

### The session database (`tests/conftest.py`)

1. **Probe.** `server_answered` (`:233`) asks whether a PostgreSQL answers at
   the configured address; `integration_verdict` (`:98`) turns that and
   `REQUIRE_TEST_DATABASE` into run, skip or fail.
2. **Name.** The database is named per session: `TEST_DB_NAME` plus ten hex
   characters (`:59-61`), so two checkouts on one cluster never share — and
   never drop — each other's database (#758).
3. **Claim, reap, create.** The session takes an advisory lock that marks the
   database as owned (`claim_session_database`, `:485`), reports leftover
   databases of crashed runs — it drops them only with
   `REAP_ORPHAN_TEST_DATABASES=1` (`:328`) — and creates its own.
4. **No schema is built here.** `setup_test_database` (`:708`) yields the
   database's URL and nothing else: the suites that need tables build them —
   the gates through the migration runner, the app through its own engine.
5. **Drop** at the end of the session, then release the lock.

### The gates' scratch databases (`tests/scripts/conftest.py`)

Each gate gets its own database (`runner_test_…`), dropped at teardown — some
cloned from a session-scoped template (`runner_tpl_…`) that already holds a
replayed schema (`replayed_db`, `at45_db`), some replaying the stream
themselves (`scratch_db`, `owner_db`). Two facts shape how this directory runs:

- **It serializes.** The seven `svc_*` service roles are cluster-scoped and
  cannot be namespaced per session, so every session takes one cluster-wide
  advisory lock (`SUITE_CLUSTER_LOCK_KEY`, `:162`) and a second run queues
  behind the first, for up to 20 minutes, naming the holder.
- **It refuses pytest-xdist** (`pytest_configure`, `:62`): under `-n` the
  workers would only queue on that lock. Run the rest in parallel and this
  directory on its own:

```bash
pytest --ignore=tests/scripts -n auto     # needs pytest-xdist; not in requirements.txt
pytest tests/scripts
```

### Configuration (`.env.test`)

The harness loads `.env.test` over the environment before any application
import (`tests/conftest.py:39`). It is gitignored.

```bash
DB_HOST=localhost
DB_PORT=5432
DB_USER=postgres              # a superuser, or CREATEDB + CREATEROLE
DB_PASSWORD=postgres
TEST_DB_NAME=storydump_test   # the BASE name; each session appends _<10 hex>

# No setting is required (the tear-out, phase 02); the suite needs a Fernet key
ENCRYPTION_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
LOG_LEVEL=DEBUG
```

## Test Structure

```
tests/
├── conftest.py                 # the session database and the run/skip/fail policy
├── test_*.py                   # the harness's own policy, the documentation pins, the deploy guardrails
├── storydump_cli/              # the `storydump` console against a scripted API (no network)
├── scripts/                    # the DB gates, on scratch databases
│   ├── conftest.py             # scratch databases, templates, actors, the cluster lock
│   ├── fixtures/               # legacy_by_hand.sql
│   ├── legacy_inventory.py     # the legacy tables' inventory the tear-out's pins read
│   └── load/                   # the load harness (RUN_LOAD_HARNESS=1)
├── src/
│   ├── api/                    # routes over a fake engine (conftest.py: client, signed_in, tenant)
│   ├── channels/               # the Telegram transport, the webhook registration
│   ├── config/                 # settings, constants
│   ├── exceptions/
│   ├── services/target/        # the target tier's services, seams patched
│   ├── utils/                  # encryption, datetime helpers, the logger
│   └── test_*.py               # the worker's composition root and entrypoint, its /health, the tear-out guards
├── fixtures/                   # idempotency_keys.json
└── mutations/                  # the per-phase mutation batteries (shell; run by hand)
```

The tree mirrors `src/`: a module under `src/services/target/` is tested under
`tests/src/services/target/`.

## Test Markers

Declared in `pytest.ini`; `--strict-markers` refuses any other.

```python
@pytest.mark.unit           # fast, isolated
@pytest.mark.integration    # slower, multiple components
@pytest.mark.slow           # skip with -m "not slow"
@pytest.mark.load           # the load harness; deselected unless RUN_LOAD_HARNESS=1
```

Most tests carry no marker, and a few gates that need a database are unmarked,
so a marker is not a "needs PostgreSQL" switch — the directory is
([`TEST_COVERAGE.md`](TEST_COVERAGE.md) has the counts).

## Writing Tests

Every new behaviour gets a test, at the layer where it lives. Four patterns
cover almost everything; copy from the named file rather than from a template.

### A target service — patch the seam

Services reach providers through one door (`src/services/target/egress.py`).
A unit test patches that door and asserts on the request's shape and the
service's answer. `capture_egress` in `tests/src/services/target/conftest.py`
is the shared helper; `tests/src/services/target/test_google_drive_oauth.py`
uses it.

### An API route — the fake engine

`tests/src/api/conftest.py` provides `client` (a `TestClient` over
`create_app(engine=FakeEngine())`), `signed_in` (a resolved principal) and
`tenant` (the unit-of-work seam). The fake session refuses `execute`, so a
route that grows a query without a seam fails here instead of passing against
nothing. The same routes against a real database are the gates
(`tests/scripts/test_web_router_x2_gate.py`).

### A `storydump` verb — the scripted API

`tests/storydump_cli/test_main.py` defines `Api` (a table of
`(method, path) → (status, body)` behind an `httpx.MockTransport`), `runtime`
and `run`. A verb's test scripts the API's answers and asserts on the exit
code and the envelope:

```python
def test_whoami_json_is_one_envelope_of_kind_whoami(tmp_path):
    result = run(runtime(tmp_path, person_api()), "whoami", "--json")
    assert result.exit_code == EXIT_OK, result.output
    document = one_envelope(result)
    assert document["kind"] == "whoami"
    assert document["data"] == PERSON
```

### A DB gate — a world, then the production role

A gate builds a scratch database, replays the schema into it, and acts as the
role production uses — `tests/scripts/test_ops_views_gate.py:43-59` is the
shape (`_scratch`, `replay_advertised_stream`, then a `svc_ingress`
connection). Mark a gate `integration`, and `slow` where it is.

### Rules the harness enforces

- **A warning is a failure.** `pytest.ini` sets `filterwarnings = error`
  (deprecation and future warnings excepted): an unclosed socket or event loop fails the
  test that happens to be running when it is collected. Close what you open.
- **`asyncio_mode = auto`**: an `async def test_…` needs no decorator.
- **A legitimate new skip raises `MAX_EXPECTED_SKIPS`** (`tests/conftest.py:88`)
  on purpose; where the database is required, skips above it fail the run.
- **Do not make real provider calls.** CI holds no provider credential.

### Best Practices

1. **Test isolation** — no reliance on execution order, no shared state; a
   gate's database is its own.
2. **Descriptive names** — the suite names tests as sentences
   (`test_a_refusal_from_a_live_server_is_not_an_absent_server`), so a failure
   reads as the broken claim.
3. **Assert the effect, not the call** — a gate reads the ledger row the
   command left, not that a function ran.
4. **A pin names what it cannot see.** Where a test bounds a property rather
   than proving it, its docstring says so (`tests/test_agent_docs.py` is the
   model).

The per-phase mutation batteries (`tests/mutations/*.sh`) check the tests
themselves: each plants one named mutation and expects the named test to fail.
They restore files from the committed tree, so commit first.

## Coverage

```bash
pytest                                        # src, term-missing + htmlcov/ (pytest.ini)
pytest --cov=src --cov=storydump_cli          # what CI measures
open htmlcov/index.html
```

No threshold is enforced and this guide sets no per-layer percentage goals;
[`TEST_COVERAGE.md`](TEST_COVERAGE.md) explains why the policy that matters is
whether the database-backed tests ran.

## Continuous Integration

`.github/workflows/ci.yml`'s `test` job (abridged):

```yaml
services:
  postgres:
    image: postgres:15
    env:
      POSTGRES_USER: test_user
      POSTGRES_PASSWORD: test_password
      POSTGRES_DB: storyline_test

- name: Install package
  run: pip install -e '.[cli]'

- name: Run tests with coverage
  env:
    DB_HOST: localhost
    DB_USER: test_user
    DB_PASSWORD: test_password
    TEST_DB_NAME: storyline_test
    ENCRYPTION_KEY: ${{ steps.gen-key.outputs.key }}   # a fresh Fernet key per run
    REQUIRE_TEST_DATABASE: "1"
  run: pytest tests/ -v --cov=src --cov=storydump_cli --cov-report=xml --cov-report=term-missing
```

The other jobs — lint, the FC-2 ratchet, security, the front end, the changelog
check — are in [`ci-cd-pipeline.md`](ci-cd-pipeline.md).

## Troubleshooting

### "No PostgreSQL answered at …" and the integration tests skipped

Nothing listens at `DB_HOST:DB_PORT`:
```bash
# macOS (Homebrew)
brew services start postgresql

# or the throwaway container above
```

### "A server IS listening … and refused the connection"

The server answered and refused — wrong `DB_USER` / `DB_PASSWORD` in
`.env.test`, or the role does not exist. The tests run and fail rather than
skip (#769); fix the credentials.

### "Permission denied to create database" / a gate skips for want of `CREATEROLE`

The `DB_USER` in `.env.test` lacks privileges:
```bash
# Option 1: use the postgres superuser (simplest)
# Edit .env.test: DB_USER=postgres

# Option 2: grant the privileges
psql -U postgres -c "ALTER ROLE storydump_user CREATEDB CREATEROLE;"
```

### "tests/scripts/ serializes every session …" (a UsageError under `-n`)

That directory refuses pytest-xdist. Run it separately (above).

### `tests/scripts/` sits waiting at start-up

Another run holds the suite's cluster lock; the wait names the holder's pid and
gives up after 20 minutes (`SUITE_LOCK_WAIT_SECONDS`). The message is captured
unless you pass `-s`.

### Tests are slow

```bash
pytest --no-cov                         # skip coverage
pytest --lf                             # only what failed
pytest --ignore=tests/scripts           # everything but the DB gates
pytest -m "not slow"
```

### Want to inspect a test database?

Run with `--pdb` to pause at the first failure, before teardown:
```bash
pytest --pdb -x
```

Then, in another terminal, find the database — the session's is
`<TEST_DB_NAME>_<10 hex>`, a gate's is `runner_test_…`:
```bash
psql -U postgres -c "\l" | grep -E "storydump_test_|runner_test_"
```

---

**Questions?** See `AGENTS.md` for the development guidelines or open an issue.
