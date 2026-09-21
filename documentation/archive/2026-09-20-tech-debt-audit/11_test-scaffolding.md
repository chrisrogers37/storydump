---
title: "One asyncpg URL rewrite and one home for the gate fixtures the suite pastes into every file"
type: plan
status: draft
owner: chris
created: 2026-09-20
tags: [plan, tech-debt, priority:medium, tests]
links: []
---

# 11 — Test scaffolding

| | |
|---|---|
| **PR title** | test: the asyncpg rewrite and the lane fixtures live once, not in eighteen files |
| **Risk** | Low — tests only; no `src/` change. The risk is a fixture scope mismatch, which step 3 checks per file |
| **Effort** | S–M (≈3h, mostly the mechanical edit across 18 files and one full suite run) |
| **Files modified** | `tests/scripts/conftest.py`, 18 gate test files, `tests/scripts/load/fake_telegram.py`, `tests/scripts/load/latency_proxy.py`, `tests/test_integration_coverage_policy.py`, `CHANGELOG.md` |
| **Findings addressed** | TD-O4, TD-O5 |
| **Depends on** | nothing |
| **Blocks** | nothing |

## Summary

`src/services/target/unit_of_work.py:169` holds `asyncpg_url()`, whose docstring says it exists
"so the rewrite lives once" — and 18 test files under `tests/scripts/` rewrite the DSN by hand at
35 call sites anyway, 8 of them through an identical private `_async_url` helper. The same files
paste `lane_db` (6 copies) and `sync_conn` (5 copies) verbatim, though they all sit under a
1,237-line `tests/scripts/conftest.py` that already owns `bootstrapped_db`, `txn`, `execute`,
`fetch_one` and `in_tenant`. Nothing here touches production code: the suite's behaviour must be
identical, test for test, before and after. Two helpers that *look* duplicated are deliberately
different and stay — step 6 says which and why.

## Findings addressed

| ID | Where | One line |
|---|---|---|
| TD-O4 | 18 files, 35 call sites; 8 `_async_url` defs | Hand-rolled `dsn.replace("postgresql://", "postgresql+asyncpg://", 1)` beside `unit_of_work.asyncpg_url()` |
| TD-O5 | `lane_db` ×6, `sync_conn` ×5 | Fixtures pasted verbatim beside the conftest that owns their dependencies |
| TD-O5 (part) | `_free_port` ×3 | Same helper, three files, one of them a different implementation |
| TD-O5 (part, **withdrawn**) | `_run(coro)` ×9 | **Not duplication** — three distinct event-loop semantics; see step 6 |

## Dependencies

None, and it blocks nothing. Land it whenever; it makes every later doc's full-suite run read the
same. It does not collide with docs 01–09 (they edit `src/`, this edits `tests/scripts/`), but if
02 lands first, re-run step 1's grep — 02 deletes test files.

## Implementation Plan

### Steps

1. **Record the exact call-site inventory in the PR** (it is the call-site audit for this PR):

   ```bash
   grep -rn 'replace("postgresql://", "postgresql+asyncpg://"' tests --include='*.py' | wc -l   # expect 35
   grep -rl 'replace("postgresql://", "postgresql+asyncpg://"' tests --include='*.py' | wc -l   # expect 18
   grep -rn '^def _async_url' tests --include='*.py'                                            # expect 8
   grep -rl '^def lane_db' tests --include='*.py'                                               # expect 6
   grep -rl '^def sync_conn' tests --include='*.py'                                             # expect 5
   ```

   After the PR the first three must be `0`, `0`, empty; the last two must be `1` each
   (the conftest).

2. **Give `tests/scripts/conftest.py` the one rewrite**, delegating to the module that owns it
   rather than re-implementing:

   ```python
   def async_url(dsn: str) -> str:
       """The asyncpg URL for *dsn* — the application's own rewrite, not a second one.

       `unit_of_work.asyncpg_url` is the deploy door both roots share (its docstring:
       "so the rewrite lives once"). Eighteen gate files spelled the `postgresql+asyncpg`
       swap by hand; a test that hand-rolls what the application does is a test that can
       agree with itself while the application changes. It also strips the libqp-only
       `sslmode`/`channel_binding` params — inert for a local test DSN, which carries
       neither, and correct if one ever does.
       """
       from src.services.target.unit_of_work import asyncpg_url

       return asyncpg_url(dsn)
   ```

   The import is inside the function deliberately: `tests/scripts/conftest.py` is collected for
   the standalone-script gates too, and the repo's precedent for a deferred `src` import in that
   file is the existing `ingress_engine`/`in_tenant` fixtures — check how they import before
   copying this shape, and match them.

3. **Replace the 8 `_async_url` definitions with the conftest helper**, one file at a time.
   Delete the local def; the call sites keep their spelling if you name the helper `_async_url`
   in the importing module, but prefer the explicit import so a reader can find the owner:

   Before (`tests/scripts/test_w1_worker_gate.py:29-30`):
   ```python
   def _async_url(dsn: str) -> str:
       return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
   ```
   After: delete it; the file's fixtures take `async_url` as a fixture argument, or import it —
   whichever matches how that file already reaches conftest helpers (the w-gates use fixture
   arguments; `test_scheduler_clock_gate.py` calls module-level helpers). **Check each file's
   existing style and follow it** rather than imposing one.

   The 8 files: `test_w1_worker_gate.py:29`, `test_w2_transport_gate.py:29`,
   `test_w3_prompt_gate.py:28`, `test_w5de_credential_lifecycle.py:28`, `test_w6_sync_gate.py:35`,
   `test_worker_freshness_gate.py:41`, `test_invitation_create_gate.py:43`,
   `test_customer_notice_gate.py:71`.

4. **Replace the 27 remaining inline rewrites** in the other 10 files — `test_publish_cap_gate.py:65`,
   `test_jobs_lease_gate.py:706`, `test_intent_ledger_gate.py:746`,
   `test_scheduler_clock_gate.py:303,385,434,522,585,813,873,1008,1081` (9 sites),
   `test_p3_settings_idempotency.py:75`, `test_l8_webhook_admission.py:56,485`,
   `test_outbox_sender_gate.py:248,520,603,907,1049,1219,1342,1546,1752` (9 sites),
   `test_l6_ig_login_oauth.py:61`, `test_l5_pipeline_gate.py:81`, `test_l3_permit_rail.py:56`.

   Before (`tests/scripts/test_outbox_sender_gate.py:248`):
   ```python
           outbox_db["worker"].replace("postgresql://", "postgresql+asyncpg://", 1),
   ```
   After:
   ```python
           async_url(outbox_db["worker"]),
   ```

   Do this file by file and run that file's tests before moving on — a 9-site file is where a
   typo hides.

5. **Move `lane_db` and `sync_conn` to the conftest**, checking each copy's scope first.

   All six `lane_db` copies are `@pytest.fixture()` (function scope) with the same body:
   ```python
   @pytest.fixture()
   def lane_db(bootstrapped_db):
       run_lane(bootstrapped_db)
       return bootstrapped_db
   ```
   `test_w1_worker_gate.py:34` additionally carries the docstring *"The full-lineage world
   (legacy schema + target public), once per test."* — **keep that docstring on the conftest
   copy**; it is the only place the fixture's meaning is written down. Note its parenthetical is
   now stale (the legacy schema was dropped by 079 on 2026-09-19): reword to name what
   `run_lane` builds today, reading `tests/scripts/test_lineage_lane.py::run_lane` to say it
   correctly. `run_lane` is imported from `tests.scripts.test_lineage_lane` — the conftest must
   import it the same way.

   The five `sync_conn` copies are identical:
   ```python
   @pytest.fixture()
   def sync_conn(lane_db):
       conn = psycopg2.connect(lane_db)
       yield conn
       conn.close()
   ```

   Delete all eleven local definitions; the consuming tests need no edit, because a conftest
   fixture resolves by name. **Verify that claim per file** by running each file's tests after
   its deletion (step 8).

6. **Leave these alone — they are not duplication:**
   - **`_run(coro)`** appears in 9 files with **three different bodies**:
     `_LOOP.run_until_complete(coro)` (`test_publish_cap_gate.py:192`, `test_l5_pipeline_gate.py:468`),
     `asyncio.run(coro)` (`test_service_tokens_gate.py:57`, `test_ops_views_gate.py:62`, others),
     and a documented "a fresh loop per call" variant (`test_l3_permit_rail.py:183`). A shared
     module-scoped loop and a per-call loop are different test semantics — unifying them would
     change what the tests exercise. The original finding read them as one helper; it is
     **withdrawn**.
   - **The 14 `world` fixtures** (12–88 lines each) are per-file scenario builders, not copies.
   - **`_free_port`** is worth one home, but the three are not identical:
     `tests/test_integration_coverage_policy.py:52` has a docstring and no import-inside-function;
     `tests/scripts/load/fake_telegram.py:176` imports `socket` inside the function;
     `tests/scripts/load/latency_proxy.py:83` does neither. Move one copy into
     `tests/scripts/load/__init__.py` for the two load files (they are a package already), and
     leave `tests/test_integration_coverage_policy.py`'s alone — it sits in a different tree and
     importing across it would couple the policy test to the load harness. Two homes, not one, is
     the right answer here; say so in the PR.

7. **Delete now-unused imports** the edits orphan — `psycopg2` in a file whose only use was
   `sync_conn`, `asyncio` likewise. `ruff check .` (rule `F401`) catches these; run it before
   the suite.

8. **Run the touched files as you go, then the whole `tests/scripts/` tree:**
   ```bash
   REQUIRE_TEST_DATABASE=1 pytest tests/scripts/test_w1_worker_gate.py --no-cov      # per file, as edited
   REQUIRE_TEST_DATABASE=1 pytest tests/scripts/ --no-cov                            # the whole tree
   ```

9. **CHANGELOG** — under `## [Unreleased]` → `### Changed`:

   > **The suite's asyncpg rewrite and lane fixtures live once (#1216).** Eighteen gate files
   > spelled `postgresql://` → `postgresql+asyncpg://` by hand at thirty-five call sites, eight
   > of them through their own copy of `_async_url`, while
   > `src/services/target/unit_of_work.py::asyncpg_url` exists so that rewrite lives once — and
   > strips the libpq-only params asyncpg refuses, which the hand copies did not. `lane_db` and
   > `sync_conn` were pasted into six and five files that already share
   > `tests/scripts/conftest.py`. All of it now resolves from that conftest. The nine `_run(coro)`
   > helpers stay as they are: three of them are deliberately different event-loop semantics, not
   > copies of one helper.

## Test Plan

- **Pre-change baseline:** `REQUIRE_TEST_DATABASE=1 pytest --no-cov` on the parent commit — green,
  with the pass count recorded. **The pass count is the characterization**: this PR must not
  change which tests run or how many, only where their helpers live. A differing count means a
  fixture failed to resolve and tests were skipped or errored.
- **Targeted, per step:** each edited file individually
  (`REQUIRE_TEST_DATABASE=1 pytest tests/scripts/<file> --no-cov`), then
  `REQUIRE_TEST_DATABASE=1 pytest tests/scripts/ --no-cov`.
- **New pins:** none. This PR adds no assertions; it relocates helpers. (A pin that the rewrite
  is not re-implemented would be a lint rule, not a test — out of scope.)
- **Post-change:** the same full command, and the pass count compared to the baseline number.

## Verification Checklist

- [ ] baseline `REQUIRE_TEST_DATABASE=1 pytest --no-cov` green on the parent commit, **pass count recorded**
- [ ] step 1's five greps re-run after: `0`, `0`, empty, `1`, `1`
- [ ] every edited file's own tests green before moving to the next
- [ ] `REQUIRE_TEST_DATABASE=1 pytest tests/scripts/ --no-cov` green
- [ ] full suite green after, **pass count identical to the baseline**
- [ ] `ruff check . && ruff format --check .` (catches the orphaned imports of step 7)
- [ ] manual smoke: none applicable — no runtime code changes
- [ ] `CHANGELOG.md` entry under `[Unreleased]`
- [ ] nothing in "What NOT To Do" happened

## What NOT To Do

- **Do not unify the nine `_run(coro)` helpers.** Three distinct loop semantics; step 6.
- **Do not re-implement the rewrite in the conftest.** Call `unit_of_work.asyncpg_url` — a second
  implementation in the test tree is the same defect one level down.
- **Do not consolidate the 14 `world` fixtures.** They are scenario builders with different
  bodies; merging them would couple unrelated gates.
- **Do not change any fixture's scope** while moving it. Two files use `scope="module"` for
  *their own* helpers (`test_invitation_create_gate.py`, `test_customer_notice_gate.py`) — check
  before assuming function scope, and if a copy differs, leave that copy local and say so.
- **Do not touch `src/`.** If a step seems to need a change under `src/`, it belongs in another
  doc.
- **Do not add a `_free_port` import from `tests/scripts/load/` into
  `tests/test_integration_coverage_policy.py`** — step 6.
- **Do not "tidy" unrelated test code** you pass through. The diff should be exactly the helper
  moves, their call sites and the orphaned imports.

## Related

- `00_TECH_DEBT.md` — findings TD-O4, TD-O5.
- `02_dead-lane-and-surfaces.md` — deletes test files; re-run step 1's grep if it lands first.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research:
`tech-debt-2026-09-20/research/orchestrator.md`, findings TD-O4 and TD-O5. The `_run` finding was
re-verified during planning and withdrawn.
