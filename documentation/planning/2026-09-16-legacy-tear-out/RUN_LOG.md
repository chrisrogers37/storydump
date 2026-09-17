---
title: "Legacy tear-out — sprint run log (build-all)"
type: plan
status: in-progress
owner: chris
created: 2026-09-17
tags: [legacy-retirement, migrations, worker, api, docs, run-log]
links: ["https://github.com/chrisrogers37/storydump/issues/1216", "https://github.com/chrisrogers37/storydump/pull/1315"]
---

## Summary

The governed run of the five phases in `00_EPIC.md` (merged as #1315, `4f2b36b`), started on the
owner's directive of 2026-09-17 ("serve forks one by one so we can solve them. Then I want to
build all the plan"). Order: 01 → 02 → 04 → 05, with 03 alongside 01/02 and before 04 — the
epic's own rationale: each earlier phase is reversible and the irreversible one (the drop) is
last; 03 touches only `scripts/migrations` and the ratchets, so it runs beside the code phases.
Goal condition: #1216's acceptance list, clause by clause (the epic's Verification Checklist),
with the owner's pasted production evidence for A2.

## Kickoff gates

| Gate | Disposition |
|---|---|
| F1–F9 ratified | **passed** — locked by the owner in chat, 2026-09-16, each on the plan's lean (`00_EPIC.md`, `10825fd`) |
| #1202's condition (phase 04) | **passed** — the owner ruled the "or" leg met (the target tier is armed and serving, with a connected destination), 2026-09-16; the ruling is quoted on the issue when phase 04 opens |
| PR #1314 (the audit fold) merged before 01 and 05 | **passed** — `53ca6d6`, 2026-09-16 23:55 UTC, on the owner's ruling to merge without the re-verify (recorded in the audit document) |
| CI green on `main` at kickoff | **passed with a finding** — both `main` runs (`53ca6d6`, `4f2b36b`) FAILED on the skip ceiling: 12 skipped against a ceiling of 11, every test green. The twelfth skip was `test_l5_pipeline_gate.py::…::test_a_local_cap_wait_on_a_slot_today_promises_tomorrow`, which skips when the local clock reads 23:55–23:59 — both runs started at 23:55/23:56 UTC. The failed jobs were re-run at 11:xx UTC (queued in the owner queue as a latent CI flake) |
| Deploy reachability at kickoff | **finding** — Railway's API service (`storydump`) marked both `53ca6d6` and `4f2b36b` `SKIPPED`; the worker deployed `4f2b36b` (SUCCESS). The fold's API-side fixes are therefore merged, NOT live, until a deploy of the API lands (phase 01's merge, or `railway redeploy --service storydump` by the owner) |
| The 078 rehearsal and the owner-run probe lines (phase 03 step 1) | **owner-gated** — queued |
| The window itself (phase 04) | **owner-gated** by F7 — the PR ships files, the runner's manual mode and the runbook only |

## Invariant registry

Re-checked after every merge; output pasted under the phase's entry.

| Id | Invariant | Check |
|---|---|---|
| I1 | the suite green with nothing newly skipped | `pytest tests/ -q -rs` (CI); the skip ceiling `MAX_EXPECTED_SKIPS` equals the measured baseline |
| I2 | every migration in the lineage list, and in the advertised stream unless it carries a non-stream marker | `pytest tests/scripts/test_advertised_ddl.py tests/scripts/test_lineage_lane.py -q`; the pin at `test_advertised_ddl.py:290` stays 35 through this plan |
| I3 | no module under `src/services/target`, `src/api`, `src/channels`, `src/worker.py` imports a legacy package | `tests/src/test_legacy_tier_gone.py` (the AST predicate over `src`, `scripts`, `storydump_cli`, `tests`) |
| I4 | the FC-2 ratchet baseline never grows; its core segment empty from phase 01 on | `python scripts/telegram_ratchet.py` → 4 / 0 / 0 / 0 |
| I5 | no runner file on `main` is applied by a deploy unless it is meant to be; a manual file stays owed and does not wedge `apply` | `python -m scripts.migration_runner status` on a checkout; the runner's unit tests (phase 04) |
| I6 | the never-run lists agree | `pytest tests/test_agent_docs.py -q` |
| I7 | the worker entrypoint imports `src.worker` and nothing legacy | `tests/src/test_worker_impl_gate.py`, `scripts/target_reachability.py` |
| I8 | every `runner:` marker in the corpus is one the runner knows (from phase 03 on) | the runner's unknown-marker test |

## Merge topology

One repository. Squash merges with `--admin`, the branch squashed to one accurate commit. No
stacking: every phase branches from `main` after the previous merge (`tear-out/0N-…`). Phase 03
may merge between 01 and 02. Railway deploys both services on every push to `main`; a phase is
"live" only when `storydump deploys --watch --commit <sha>` shows SUCCESS for both services (the
API service skipped the two kickoff commits — see the gate above).

## Phase results

| Phase | Doc | Status | PR | CI |
|---|---|---|---|---|
| 01 delete the legacy code and its tests | `01_delete-the-code.md` | in progress (branch `tear-out/01-delete-the-code`) | — | — |
| 02 retire the settings, the entry point and the config | `02_settings-and-entry-points.md` | pending | — | — |
| 03 the 3f snapshot migration and the ratchet's file rule | `03_snapshot-migrations.md` | pending | — | — |
| 04 the gated drop and stand-down | `04_drop-and-stand-down.md` | pending (owner-gated window) | — | — |
| 05 the documentation's end state | `05_docs-end-state.md` | pending | — | — |

## Phase 01 — delete the legacy code and its tests

**Measured before deleting** (worktree at `4f2b36b`, 2026-09-17):

- The ratchet: `telegram_modules` 19, `core_telegram_modules` 15, `chat_id_functions_outside_adapters` 93, `provider_account_ref_log_sites` 0.
- The suite: `5815/5820 tests collected (5 deselected)`.
- `scripts/target_reachability.py` (dummy variables): worker `src.main` closure +855, target modules reached 35; the positive and negative controls PASS.
- The deletion rule (`tests/src/test_legacy_tier_gone.py::legacy_importing_files` over `tests/`) named **118** files — the plan's 117 plus `tests/src/services/target/test_google_drive_adapter.py`, which took one constant from `media_kind`. Eight of the 118 are rewritten, not deleted (`tests/conftest.py`, `tests/integration/conftest.py`, `tests/scripts/conftest.py`, `test_lineage_lane.py`, `test_migration_gate.py`, `test_schema_drift_live.py`, `tests/src/api/test_security_hardening.py`, `test_google_drive_adapter.py`); **110** were deleted, with the three now-empty test packages (`tests/src/repositories`, `tests/src/models`, `tests/src/services/media_sources`).

**Premise findings (recorded, not papered over):**

- The plan said the Makefile's `init-db`/`setup-db`/`quickstart` "run `scripts/init_db.py`". They do not: `init-db` applies `scripts/setup_database.sql` with `psql` (the legacy schema, by hand). Deleting the script breaks nothing in the Makefile, so the Makefile is untouched here and its legacy-schema targets are phase 02's, as that doc already says.
- `tests/src/test_worker_impl_gate.py` and `tests/scripts/test_target_reachability.py` are not in the rule's list (they import `src.main`/`src.worker_impl`, or name a legacy module as a string) but both had to change: the dispatch tests for the legacy arm, and the reachability specimen (`src.services.core.loops.scheduler_loop` → `scripts.migration_runner`, which imports no target module and adds psycopg2 — a closure the target tier never loads).
- The lineage lane's inventory is a literal now (`tests/scripts/legacy_inventory.py`): `LEGACY_TABLES` (16, what production holds and phase 03 snapshots) and `LINEAGE_TABLES` (15, what the replay creates — the hand-made `posting_history_dedup_archive` is kept out of the lane's subset because no file creates it).
- `ConfigValidator` (`src/utils/validators.py`) ran in no deployed process; its startup secret check goes with it. Whether the target tier wants one is queued for the owner.

**After the deletion:** the ratchet re-measured with `--write-baseline`: 4 / 0 / 0 / 0 (the four
`telegram_modules` are `src/channels/telegram_transport.py`,
`src/channels/telegram_webhook_registration.py`, `src/exceptions/telegram.py`,
`src/services/target/telegram_dispatch.py`). `ruff check .` and `ruff format --check .` clean.
`python-telegram-bot` and `Pillow` removed from `requirements.txt` and `setup.py` (no importer
outside the deleted packages in `src`, `scripts`, `storydump_cli`, `tests`).

**Verification** (2026-09-17, the worktree at `2533475`):

- Units without a database (sandbox off): `2251 passed, 27 skipped`, 4 failed — the four are
  `test_egress_floor.py`'s two loopback-binding tests (they fail on `main` locally too: the
  environment cannot bind `127.0.0.2`) and, before its fix, the closure test reading a log line
  as its answer. The whole suite against the Docker test database (sandbox off):
  `3670 passed, 2 skipped` plus the two loopback tests. Skips: `test_schema_drift_live` (no
  DSN, by design) and the loopback port skip (local only).
- Two implicit dependencies surfaced by the deletion, made explicit: psycopg2's UUID adapter
  had been registered by the legacy SQLAlchemy engine connecting at session start
  (`test_l3_permit_rail.py` failed with `can't adapt type 'asyncpg.pgproto.pgproto.UUID'`);
  `tests/scripts/conftest.py` now calls `register_uuid()` itself. The ratchet's positive
  control assumed the burn-down axes were populated; a planted tree lights them now.
- The #909 naive-column population gate found 0 columns and 0 sites on the tree that remained
  (its subjects were the legacy models and repositories): retired with them, the helpers'
  mirror tests kept in `tests/src/utils/test_datetime_utils_mirrors.py`.
- `ruff check .` and `ruff format --check .`: clean.
- Battery `tests/mutations/legacy_tear_out_01.sh` on the committed tree: 13 mutations, 13
  killed, none unapplied, none by error (the AST walk, the from-import arm, the prefix arm, bare
  `src.models`, the forbidden set's completeness, the ratchet's core segment, a stub package in
  the closure, the garbage-`WORKER_IMPL` refusal, the target root, the models package's exports,
  a re-added dependency, the reachability specimen, a name dropped from the lineage inventory).
- The skip ceiling: `MAX_EXPECTED_SKIPS` stays 11 until CI reports this branch's count (8 of the
  11 baseline skips were legacy tests); the pin is set to the measured number in the fold.
- PR #1316 (draft). Review lenses dispatched on the detached snapshot at `2533475`.
- CI on `2533475`: **red** — `23 failed, 3657 passed, 1 skipped`. The local gate subset had
  passed with the UUID fix because it never ran the 23: `register_uuid()` registers psycopg2's
  TYPECASTER as well as its adapter, so every gate's raw fetch of a uuid column returned
  `uuid.UUID` where 23 gates compare `str` or JSON-serialise. Fixed at `954217a` — the adapter
  alone (`register_adapter(uuid.UUID, UUID_adapter)`), which is exactly the half the legacy
  engine's `on_connect` had registered process-wide. CI on `954217a`: green. The skip ceiling
  pinned at `9bd29cf` (11 → 1, CI's measured count); CI on `9bd29cf`: `3680 passed, 1 skipped`.

**Review round 1** (two lenses on `2533475`; folded at the commit after `9bd29cf`):

- Lens 1 (structural + simplify): no blockers; 2 risks, 15 gaps, 12 simplifications. Lens 2
  (adversarial): 1 blocker (the typecaster — already fixed on the branch before the report
  landed), 3 risks, 7 gaps, 6 surviving mutants. Every claim was verified against the tree
  before folding; two were declined with reasons (below).
- Closed — the predicate: the bare `src.models` arm dropped (it flagged the kept package and the
  legitimate `from src.models import target`); one walker instead of two; relative imports
  resolved against the file's package; literal names handed to `importlib.import_module` and
  `__import__` read; the closure answer a sentinel line, not "the last line"; the entrypoints
  DERIVED from the Procfile, `railway.toml` and the console script (`scripts.migration_runner`
  and `storydump_cli.main` joined `src.main`, `src.api.app`, `src.worker`); the planted control
  is one file importing every prefix, asserted equal to the set; per-root scan floors; the
  ratchet assertion a shape check (every Telegram-named module under the target tier's homes)
  instead of a second copy of the baseline; the dependency check on PEP 503 names (the
  `pillow`/`python_telegram_bot` respellings were surviving mutants).
- Closed — the survivors: `tests/integration/` deleted whole (its one fixture had zero consumers
  after its four leak tests went; `test_instagram_posting.py` held no test) and the second
  zero-test tombstone `tests/src/services/test_posting_delivery_reschedule.py` — class sweep:
  `find tests -name 'test_*.py'` with zero `def test_` → exactly those two; the lane's
  `legacy_declared_tables()` inlined and its test renamed for what it checks; the ratchet's
  planted-tree test dropped (the file already had the planted controls — cited instead);
  `setup_test_database` yields the database URL (its one consumer reads "not None"; the engine it
  built never connected); `tests/scripts/conftest.py` imports `UUID_adapter` on one line and says
  the one ordering fact it relies on; the mirrors file's imports at module level and its
  docstring honest about being a new file; a new `tests/scripts/test_legacy_inventory.py` pins
  `LEGACY_TABLES` (a surviving mutant: the sixteenth name had no consumer until phase 03).
- Closed — the label an operator acts on: `scripts/target_reachability.py`'s gate text said
  "IMPORTABLE-NOT-SERVING until armed" and its two tests pinned it; it now says the root runs
  unconditionally and `WORKER_IMPL` only refuses an unknown value (the spelling
  `WORKER_IMPL=target` kept, as the operator-facing literal); `src/worker_impl.py`'s "unset
  selects legacy — byte-identical deploys" docstring corrected the same way.
- Closed — the class "a present-tense claim about a deleted path" (sweep: the deleted module
  paths and names over `src`, `scripts`, `storydump_cli`, `tests`, `.claude`, the config files;
  18 hits outside the guard and the ratchet's own planted fixtures, all fixed): `src/worker.py`,
  `src/models/target/{__init__,identity_and_tenancy,columns}.py`, `src/services/target/
  {unit_of_work,web_sessions,drive_adapter,google_drive_oauth,ig_login_oauth}.py`,
  `src/api/instagram_client.py`, `src/utils/logger.py` (the `telegram`/`telegram.ext` logger
  routing for an SDK no longer installed), `requirements.txt`'s two comments, the three
  `.claude/rules` files whose `paths:` globs named deleted directories (`database.md` loses two
  globs; `scheduler.md` and `telegram.md` are re-pointed at the target modules and their legacy
  sections — commands, callbacks, the `TelegramService` composition — cut, the target-tier
  sections kept), `.claude/rules/testing.md`'s example import, the QUICK_REFERENCE diagram and
  two PROJECT_CONTEXT lines, `tests/conftest.py`'s hook note, `test_security_hardening.py`'s
  docstring and orphan banner, `test_unit_of_work.py:89`, `test_worker_impl_gate.py`'s "legacy
  boot" rationale, and one operator-facing sentence in `meta-app-review.md` (it said the legacy
  tier was the running system).
- Closed — `src/exceptions/__init__.py` exports what it exported before (`RefusalError` was
  creep); `scripts/migration_runner.py`'s "one home" claim softened (the capabilities probe
  spells the path independently, pre-existing).
- Battery: 20 mutations now (the three predicate arms, the relative-import resolution, the
  literal-name arm, the forbidden set's completeness, the entrypoint derivation, the ratchet's
  core segment and the stray-home check, the stub package against the closure test AND the
  standing guard AND the package-gone test, the garbage refusal, the target root, the models
  package, a respelt dependency, the sixteenth table, a dropped lineage name, `HAND_MADE`
  emptied, and the UUID registration deleted — that last one and the two lane mutations run
  under the DB gate). The reachability-specimen mutation was dropped (it mutated the test's own
  fixture choice, not a behaviour).
- Declined, with reasons: (a) rewriting the predicate as "every `src.*` import resolves to a
  file" (a design the lens itself did not ask for; the hand-kept lists are compared to each
  other and to git's deleted set); (b) `TenantResolutionError`'s package export dropped as a
  surviving mutant — it is the package's prior public name and removing it is a change with no
  behaviour behind it either way; (c) the README quickstart (`psql -f scripts/setup_database.sql`
  then `make run`), which now builds the legacy schema and boots the target worker against it —
  the Makefile and the quickstart are phase 02's by plan, queued below with the settings-built
  URL fallback the lens traced (`unit_of_work.async_database_url()` builds from the legacy
  `DB_*` fields when `TARGET_DATABASE_URL` is unset).
- The plan's step-6 table, measured (importers under `src`, `scripts`, `storydump_cli`, `tests`):
  `google-api-python-client` 0, `google-auth` 0, `google-auth-oauthlib` 0, `python-dateutil` 0,
  `alembic` 0, `tenacity` 0, `httpx2` 0, `anthropic` 0 — kept here (the plan lets the uncertain
  stay; a removal is phase 02's, with the settings they served), `python-multipart` 0 direct
  importers but REQUIRED (FastAPI's `Form` in `src/api/routes/meta.py`), `cloudinary` 2,
  `keyring` 1 (the CLI extra).

## Owner-decision queue

- **The API service's skipped deploys.** Railway marked `53ca6d6` and `4f2b36b` `SKIPPED` on the `storydump` (API) service while the worker deployed; the fold's API-side fixes (`ops_views.py`, the doctor/health paths) are not live until an API deploy lands. Phase 01's merge triggers one; sooner, by hand: `railway redeploy --service storydump`. Not run by the agent.
- **A latent CI flake: the skip ceiling meets a clock-of-day skip.** `tests/scripts/test_l5_pipeline_gate.py::TestTheFirstFetch::test_a_local_cap_wait_on_a_slot_today_promises_tomorrow` skips when the account's local time is 23:55–23:59; any CI run starting in that window breaches `MAX_EXPECTED_SKIPS`. A fix that removes the skip: set the fixture account's `tz` to a zone where the local hour is not 23 at test time (the test already reads `tz` from the row). Target-tier test hygiene, outside this plan's scope.
- **A startup secret check for the target tier?** The legacy `ConfigValidator` (deleted with phase 01) checked `ENCRYPTION_KEY` at boot; nothing in the target tier does the same at import. A decision, not a regression.
- **Phase 02 premise findings from round 1:** `unit_of_work.async_database_url()` falls back to the
  legacy `DB_*` fields when `TARGET_DATABASE_URL` is unset — with the legacy loops gone, a boot
  without the variable (a local `make run`, a preview service) runs the target worker against the
  legacy-configured database; phase 02 makes `TARGET_DATABASE_URL` mandatory at boot and retires
  the fallback with the fields. The README quickstart (`psql -f scripts/setup_database.sql` then
  `make run`) and the Makefile's `run`/`dev`/`init-db` describe the legacy tier (phase 02).
  `railway.toml`'s `drainingSeconds` rationale (Telegram polling) is stale (F2 forbids touching
  it here; phase 02). Eight requirements with zero importers (the table above).
- The tear-out's own gates as they arise (phase 03's probe lines and the 078 rehearsal; phase 04's window).
