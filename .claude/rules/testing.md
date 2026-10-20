---
paths:
  - "tests/**"
  - "pytest.ini"
---

# Testing

## Layout

```
tests/
├── conftest.py          # the per-session test database, the run/skip/fail verdict, the skip ceiling
├── test_*.py            # repo-level pins: the agent docs, the deploy guardrails, the policy itself
├── src/                 # unit tests, mirroring src/ — no database, no network
│   ├── api/             #   routes, through create_app(engine=<fake>)
│   ├── channels/  config/  exceptions/  models/  utils/
│   └── services/target/ #   one test_<module>.py per service module
├── storydump_cli/       # the CLI against a scripted API
├── scripts/             # the DB gates: real PostgreSQL, the schema built from code, the production roles
│   ├── conftest.py      #   scratch databases, actors, the world builders
│   ├── load/            #   the load harness (marker `load`, opt-in)
│   └── onboarding_10k/  #   the 10,000-post Instagram onboarding archives (`RUN_ONBOARDING_10K=1`, opt-in)
└── fixtures/            # shared data files and fake providers
```

There is no repository layer to mock.

## Two kinds of test

- **Unit tests** (`tests/src/`, `tests/storydump_cli/`) meet SQL with a scripted
  executor and a provider with a fake at its seam. Fast, and blind to anything
  only PostgreSQL can refuse.
- **DB gates** (`tests/scripts/`) build a scratch database per test or module,
  build the schema from code and adopt the migration ledger (`build_world`,
  `tests/scripts/conftest.py:594`), and act as the PRODUCTION roles
  (`svc_ingress`, `svc_worker`) with a second workspace seeded, so "only this
  workspace's rows" is proven rather than assumed
  (`tests/scripts/test_ops_views_gate.py:1`).

New behaviour gets a unit test. Anything whose truth lives in the database — a
constraint, a trigger, a policy, a door, a bound parameter's type — also gets a
gate: a scripted executor cannot fail the way PostgreSQL does
(`.claude/rules/database.md` › Bound parameters).

## Running

Without a database — port 65432 has no listener, so whatever needs PostgreSQL
skips honestly and everything else runs:

```bash
env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME \
    -u REQUIRE_TEST_DATABASE -u TARGET_DATABASE_URL DB_PORT=65432 \
    ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") \
  pytest -q --no-cov -p no:cacheprovider tests/src tests/storydump_cli
```

The gates, against the Docker test PostgreSQL (`AGENTS.md` › Testing has the
`docker run`; a `psql` must be on the PATH — the fixtures apply SQL through it):

```bash
PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH" \
DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password \
DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 \
  pytest --no-cov tests/scripts/
```

- `pytest.ini` turns coverage on (`--cov=src`, an HTML report); `--no-cov` is the
  fast path. CI's Test job runs `pytest tests/` on Python 3.10 with
  `postgres:17` and `REQUIRE_TEST_DATABASE=1`.
- `asyncio_mode = auto`: an `async def test_…` needs no marker.
- `filterwarnings = error`: a warning fails the test — EXCEPT the three classes
  `pytest.ini:30-32` ignores (`DeprecationWarning`, `PendingDeprecationWarning`,
  `FutureWarning`), so a deprecated API does not fail CI. An unclosed client or
  event loop is a `ResourceWarning`, and it can fail whichever test happens to
  be running when it is collected (`pytest_sessionstart`, `tests/conftest.py:128`).
  Close what you open.
- `tests/scripts/` refuses pytest-xdist (`tests/scripts/conftest.py:54`): the
  `svc_*` roles are cluster-scoped, so the directory serializes on one
  cluster-wide advisory lock.
- Inside the agent's Bash sandbox, a test that binds a loopback socket fails
  (`tests/test_isolation_guard.py:638`). That is the sandbox, not the code;
  run such a test with the sandbox off.

## The database verdict and the skip ceiling

`tests/conftest.py` names one test database PER SESSION and decides once
(`integration_verdict`, `:102`): a server answered → run; no server and none
required → skip; no server where `REQUIRE_TEST_DATABASE` is set → fail. A
database that answered and then failed propagates; nothing after the probe is
swallowed.

`MAX_EXPECTED_SKIPS = 1` (`:92`) is the backstop, enforced at session finish
only where a database is required (`:150`) and pinned by
`tests/test_integration_coverage_policy.py:284`. The one legitimate skip is the
live-drift audit, which runs only on its schedule
(`tests/scripts/test_schema_drift_live.py:71`). So:

- A new `pytest.skip` breaches the ceiling in CI. If the skip is legitimate,
  raise the number AND its pin in the same PR, with the reason in both.
- An opt-in suite DESELECTS instead of skipping — the load harness drops its
  `load` items at collection unless `RUN_LOAD_HARNESS=1`
  (`tests/scripts/load/conftest.py:14`), and the 10,000-post onboarding
  archives (`tests/scripts/onboarding_10k/`, minutes rather than seconds) drop
  out unless `RUN_ONBOARDING_10K=1`
  (`tests/scripts/onboarding_10k/conftest.py:20`). The 100, 500 and 2,000-post
  archives run by default (`tests/scripts/test_instagram_onboarding_budgets.py`).
- Report a green result only from a run that had a database:
  `N passed, M skipped` at exit 0 says nothing about tests that did not execute.

## Fixtures the suite actually uses

- **A scripted executor for a service function** — `_ScriptedExecutor`
  (`tests/src/services/target/test_provisioning.py:540`): each `execute` is
  answered from a queue of `(rowcount, first_row)` pairs and recorded, and the
  test asserts the statements and their parameters in order.
- **A fake engine for a route** — `FakeSession` refuses SQL outright
  (`tests/src/api/conftest.py:148`), `app` is `create_app(engine=FakeEngine())`
  (`:188`), `signed_in` overrides `current_principal` (`:201`). Each test patches
  the service function its route calls, so a route that grows a query without a
  seam fails loudly. The same path against a real database is
  `tests/scripts/test_web_router_x2_gate.py`.
- **A fake at the provider seam** — `capture_egress`
  (`tests/src/services/target/conftest.py:23`) patches `egress.request`, the one
  door provider HTTP goes through, and records the request's shape.
- **A scripted API for the CLI** — `Api` (`tests/storydump_cli/test_main.py:87`)
  maps `(method, path)` to `(status, body)` behind `httpx.MockTransport`;
  `runtime()` (`:147`) gives a temp config directory and an in-memory token
  store, `run()` (`:162`) invokes the verb, and `one_envelope()` checks every
  JSON document against the wire contract.
- **The gates' world** (`tests/scripts/conftest.py`): `_scratch` / `scratch_db`
  (`:514`, `:536`) for a database dropped on teardown, `build_world` (`:594`)
  and `world_db` (`:843`) for the target schema, `as_user` (`:196`) to connect as
  a service role, `seed_workspace_chain` / `seed_intent_chain` (`:630`, `:672`)
  for rows. A gate that runs commands as `svc_ingress` imports `ingress_world`
  and re-registers it as its own (`world = ingress_world`) rather than pasting
  the fixture: `world["owner"]` is the built schema as its owner,
  `world["ingress"]` the same database as `svc_ingress`.

```python
from src.services.target.provisioning import disable_destination

# _ScriptedExecutor: the class at tests/src/services/target/test_provisioning.py:540


class TestDisableDestination:
    async def test_disables_revokes_flags_and_retires_in_that_order(self):
        ex = _ScriptedExecutor((1, {"id": "acct"}), (1, None), (2, None), (1, None))

        result = await disable_destination(ex, workspace_id="ws", ig_account_id="acct")

        assert result["intents_flagged"] == 2
        disable, revoke, *_ = ex.statements
        assert "UPDATE ig_accounts" in disable[0]
        assert disable[1] == {"acct": "acct", "ws": "ws"}
        assert "UPDATE oauth_credentials SET state = 'revoked'" in revoke[0]
```

Assert the statement AND its parameters: a test that checks only the return
value passes against a query that names the wrong tenant.

## Markers

`--strict-markers` is on, so a marker not in `pytest.ini` is an error.

| Marker | Used for |
|---|---|
| `integration` | The DB gates; most set it for the module: `pytestmark = [pytest.mark.integration, pytest.mark.slow]` |
| `slow` | Gates that build a world; `-m "not slow"` leaves them out |
| `unit` | Carried by only a few files — `-m unit` is a small subset, not "every test that needs no database" |
| `load` | `tests/scripts/load/`; deselected unless `RUN_LOAD_HARNESS=1` |
