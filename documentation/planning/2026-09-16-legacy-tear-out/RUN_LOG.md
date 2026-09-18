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
| I7 | the worker entrypoint imports `src.worker` and nothing else | `tests/src/test_worker_entrypoint.py` (renamed from `test_worker_impl_gate.py` in phase 02), `scripts/target_reachability.py` |
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
| 01 delete the legacy code and its tests | `01_delete-the-code.md` | **DONE** — merged `2369a9b` (2026-09-17 23:57 UTC); the worker deployed it (SUCCESS); the API service SKIPPED it behind a red `main` check (the midnight skip, below) | #1316 | green at `e6e854b` (3653 passed, 1 skipped) |
| 02 retire the settings, the entry point and the config | `02_settings-and-entry-points.md` | **DONE** — merged `f59fe43` (2026-09-18 15:44 UTC); round 1 and a fresh re-verify folded; the deploys under the phase's entry | #1319 | green at `9d14304` (3680 passed, 1 skipped) |
| 03 the 3f snapshot migration and the ratchet's file rule | `03_snapshot-migrations.md` | **DONE** — rehearsed on a Neon PITR branch, merged `3ffa750` (2026-09-18 17:23 UTC), 078 applied in production by the deploy at 17:24 UTC; the probe under the phase's entry | #1318 | green at `6da8d00` (3700 passed, 1 skipped) |
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
  other; git's deleted set was compared by hand — 114 deleted modules, 114 forbidden — not by a
  test); (b) `TenantResolutionError`'s package export dropped as a
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

**After the fold** (`e7aa4f9`): CI green — `3653 passed, 1 skipped`, all nine checks; the battery on
the committed tree 20/20 killed (three under the DB gate); a fresh re-verify read dispatched on
the detached snapshot at `e7aa4f9`.

**Re-verify** (a fresh read on the detached snapshot at `e7aa4f9`): **HOLDS** — every Closed
item closed, every positive control tests a real arm, all 20 battery anchors occur exactly once,
the recipe green (192 passed, 3 skipped, the sandbox's loopback denials aside). It found residue
the class sweep had missed in the sweep's own roots, folded in the next commit: the
`development-patterns.md` rule (its service, tracking, error and image sections taught
`BaseService`, `track_execution` and `ImageProcessor` — deleted classes, loaded for every
`src/**/*.py` edit; rewritten onto the target tier, and its `cli/**` glob re-pointed at
`storydump_cli/**`), `testing.md`'s tree and its `tests/integration/` paragraph, three
docstrings in `src` (`api/routes/retired.py`, `exceptions/tenancy.py`, `config/defaults.py`),
`worker_impl.py`'s module docstring and refusal text, a ratchet test docstring, the README tree
(phase 01 owned it and had only dropped one line), and — operator-facing, so not left to phase
05 — `telegram-webhook.md`'s "the legacy scheduler clears the webhook" diagnosis, the three
`meta-app-review.md` citations of the deleted Instagram modules and
`google-oauth-verification.md`'s scope table (the target flow requests `drive.readonly` only).
The checklist line "`grep … src.services.core … → 0 lines`" is unmet BY DESIGN: the survivors
say "deleted in #1216", the ratchet's `CORE_SEGMENT` constant and its planted fixtures name the
directory, and the guard lists the prefixes — the checklist is ticked with that note.

## Phase 01 — merged

`2369a9b`, squash-merged with `--admin` on 2026-09-17 23:57 UTC after the residue fold's CI (`e6e854b`:
3653 passed, 1 skipped, nine checks). Invariants re-run on `main` at `2369a9b`: I4 the ratchet 4 / 0 / 0 / 0;
I2/I3/I6/I7 and the guard — 114 tests green (the lane, the advertised-DDL pin at 35, `test_agent_docs`, the
worker gate, the inventory pin). Deploy reachability: the worker deployed `2369a9b` (SUCCESS) — the deletion
is live on the worker; the API service marked it `SKIPPED`, its third in a row, because `main`'s CI run for
the merge FAILED on the skip ceiling: the merge landed at 23:57 UTC and `test_l5_pipeline_gate.py`'s
cap-wait test skipped inside its 23:55–23:59 window (2 skipped, ceiling 1). That flake was queued after
phase 01's kickoff; it now blocks the API's deploy of every merge landing in that window, so it was fixed
outside the plan's phases as a test-only PR (`test/cap-wait-noon-tz`: the account gets the `Etc/GMT±N`
zone where it is noon right now, nothing skips) and merged first; the API's deploy is followed from there.

## Phase 02 — retire the settings, the entry point and the config

**Measured before deleting** (worktree at `deb29c2`, 2026-09-17/18). The first measure was a text
match (`grep -rnE '\bNAME\b' src scripts storydump_cli`, plus `self.NAME` inside the module); round
1 of the review showed it counting comments, docstrings and same-named environment reads as
readers, so the table below is the SECOND measure — by AST: `settings.NAME`, `getattr(settings,
"NAME")`, the OAuth clients' named-setting idiom, and properties that themselves have such a reader,
over the code roots and the test harness (the settings' own unit tests excluded):

| Field | Readers in the tree | In the module | Disposition |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `ADMIN_TELEGRAM_CHAT_ID` | 0 | 0 | deleted (required by the deleted tier; #1222) |
| `TELEGRAM_MAX_CONCURRENT_UPDATES`, `TELEGRAM_RATE_LIMITER_ENABLED`, `TELEGRAM_RATE_LIMITER_MAX_RETRIES` | 0 | 0 | deleted (PTB knobs; PTB went in phase 01) |
| `MEDIA_DIR`, `BACKUP_DIR`, `BACKUP_RETENTION_DAYS` | 0 | 0 | deleted |
| `FACEBOOK_APP_ID`, `GOOGLE_REFRESH_TOKEN_TTL_DAYS` | 0 | 0 | deleted |
| `CLOUD_STORAGE_PROVIDER`, `CLOUD_UPLOAD_RETENTION_HOURS`, `CLOUD_UPLOAD_TIMEOUT_SECONDS` | 0 | 0 | deleted |
| `INSTAGRAM_PUBLISH_LIMIT_FALLBACK`, `MEDIA_SYNC_INTERVAL_SECONDS`, `ANTHROPIC_API_KEY`, `CAPTION_MODEL` | 0 | 0 | deleted |
| `META_GRAPH_API_VERSION` | 0 | 2 (the two `meta_*_graph_base` properties, 0 callers) | deleted with the properties |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | 0 (the only mentions were comments saying they are NOT read) | 0 | deleted in the fold: no engine reads them, the target pool is pinned in code |
| `DATABASE_URL` (the field) | 0 | 1 (`database_url`, whose "6 callers" in this table's first draft were the runner's argparse `args.database_url`; real callers: 0) | deleted in the fold with the property; the runner still reads the VARIABLE |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | 0 (`src/worker.py` reads the environment under the same names) | 0 | deleted in the fold as FIELDS; the variables stay |
| `DB_SSLMODE` | 0 | `test_database_url` (called by `tests/conftest.py`) | kept — the harness's |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `TEST_DB_NAME` | `unit_of_work.async_database_url` and the harness | — | kept — the harness's; no deployed process reads them |
| the other 16 | 1–9 each, in deployed code | — | kept |

48 fields → 23, none required. The rule is a test (`test_every_surviving_field_has_a_reader`, with
planted controls for what is and is not a read), not this table.
Environment reads OUTSIDE `Settings`, measured the same day and pinned by equality
(`ENV_READ_OUTSIDE_SETTINGS`, 28 names): the two database logins, the bot and its webhook (7),
the worker's knobs (4), `PORT`/`WEB_CONCURRENCY`, the Cloudinary trio and `META_GRAPH_VERSION`, Resend's
two, Railway's three, the CLI's four. `WORKER_IMPL` was the twenty-ninth and went with its module.

**Red first** (the final guard, 58 tests, run against `deb29c2` in a throwaway worktree): **51 failed,
7 passed**. The seven: the five planted-source controls of the reader rule (tree-independent by
design) and two pure regression pins — `[Makefile]` (the base Makefile named no dead variable) and
`every_file_the_makefile_feeds_psql_exists` (the base `init-db` named one file, which exists; the pin
is for round 1's blocker). CORRECTION: this entry first said "42 tests, 34 failed" — a count read off
truncated output, withdrawn. And a disclosure: that first red run executed the real `worker.main()`
on the base, where the fallback was live; it died on a refused connection to a dead port (no
database, no bot token, nothing sent), and it is why the refusal tests now stub everything past the
refusal.

**Built** (`177025b`): the 19 deletions; `src/main.py` reduced to the dispatch (the `WORKER_IMPL` read,
`src/worker_impl.py` and the instrument's gate axis — `worker_gate_facts`, the `gate` parameter, the
`worker_gate` JSON key — deleted; `tests/src/test_worker_impl_gate.py` → `test_worker_entrypoint.py`);
`src/worker.py` refuses without `TARGET_DATABASE_URL` (exit 2, naming `unit_of_work.DATABASE_URL_VAR`)
and `create_engine` requires a URL (`async_database_url` survives as the harness's door); `.env.example`
rewritten (every `NAME=` line a field or a measured read; grouped by service; the CLI's four included);
the Makefile (`install` → `.[cli]`; `install-dev` → ruff/bandit/pip-audit, the tools CI runs — there was
never a `dev` extra; `init-db` → the by-hand base then the runner, the lane's own sequence; `dev` without
the production health gate; `validate-env` → `from src.config.settings import settings`); the three
variables plus `DRY_RUN_MODE` and `MEDIA_DIR` out of `ci.yml` and `schema-drift.yml`; the eight recipe
lines of five batteries; `AGENTS.md` §Setup (and its stale `/api/onboarding/*` paragraph — the route is
retired); the README's configuration list and `make init-db`; the four guides' env blocks and the CI
guide's line; `tests/scripts/test_no_implicit_admin_fallback.py` retired; the redaction tests on a
subclass with one required sibling and `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` as the sentinel field.

**Class sweep — "a setter names a dead variable"**, first pass (`grep -rnE '^\s*#?\s*(WORKER_IMPL|
TELEGRAM_BOT_TOKEN|TELEGRAM_CHANNEL_ID|ADMIN_TELEGRAM_CHAT_ID|MEDIA_DIR)\s*[=:]'` over `documentation
README.md AGENTS.md CLAUDE.md .claude .github Makefile .env.example`, excluding `planning/` and
`archive/`): 14 hits in three guides + the 6 workflow lines + `.env.example` + the 8 recipe lines — all
fixed. Round 1 showed that pass too narrow twice over (four names, and an anchor that missed table
rows and inline "Set `X=`"); the second pass is in the round's entry below. Prose mentions
(`cloud-deployment.md`'s table and troubleshooting row, `ci-cd-pipeline.md`'s list, `README.md`'s
list) fixed too. Left for phase 05, by the plan's rule ("only the lines that set the dead variables"):
prose in `monitoring.md`, `troubleshooting.md`, `posting-monitor.md`, `telegram-webhook.md`,
`SECURITY_REVIEW.md`. `landing-vercel-deployment.md` names the landing app's OWN `TELEGRAM_BOT_TOKEN` /
`ADMIN_TELEGRAM_CHAT_ID` (read from Vercel by `landing/src/lib/telegram.ts`) — a different consumer,
correct as it stands.

**Premise findings:**

- The plan's `.env.example` allowlist ("`WORKER_LOG_LEVEL`, `PORT`, `DATABASE_URL`") was three names
  short of the tree by twenty-five: the target tier reads 28 variables outside `Settings`. Measured
  and pinned rather than allowlisted by hand.
- The redaction tests' specimen was blind in CI BEFORE this phase: pydantic truncates a `missing`
  error's `input_value` (the raw input dict) keeping its head and tail, so a sentinel in a field that
  sits between two declared fields the environment sets (`DB_PORT`, `ENCRYPTION_KEY`, `LOG_LEVEL` in
  CI) is cut out of the repr, and the leak assertions pass against a boundary that redacts nothing.
  Found by the battery (the mutation `error = str(exc)` survived); closed in the fold by a fixture that
  strips every declared field from the environment but the secret, and a premise test on the raw path.
- A required field back in `Settings` kills at COLLECTION (`settings = Settings()` at module scope
  refuses to import with no variable set) — the battery names that verdict rather than "error".
- `install-dev` named a `dev` extra that never existed; `validate-env` called a `get_settings` that
  never existed (the plan's evidence, confirmed).

**Verification** (2026-09-18, the worktree at `177025b`, NO Telegram variable and no `WORKER_IMPL` in
any run):

- Units without a database (sandbox off, `tests --ignore=tests/scripts`, the two loopback tests
  deselected): `2216 passed, 26 skipped`. (`tests/scripts`' fixtures error rather than skip without a
  database — pre-existing; the gates run covers them.)
- The whole suite against the Docker test database: `3626 passed, 1 skipped`.
- CI on `177025b`: every check green; `3665 passed, 1 skipped, 5 deselected`.
- `ruff format --check .` and `ruff check .` clean. `grep -rn WORKER_IMPL src scripts storydump_cli` → 0.
- Battery `tests/mutations/legacy_tear_out_02.sh` on the committed tree `177025b`: 22 mutations —
  15 killed, 2 killed at collection (the required-field ones, reclassified as above), 3 survived and
  1 not applied, all four the battery's own defects: the refusal-message mutation left the variable
  name in the message's second interpolation (mutation widened); the `.env.example` anchor
  `LOG_LEVEL=INFO` matched twice (anchor moved); the redaction mutation survived through the
  truncation blindness above (the tests fixed, not the mutation); removing the `ValidationError` arm
  is EQUIVALENT for the leak property (the tail rung `except ValueError` catches pydantic's
  `ValidationError`, a `ValueError`) — what it loses is the field NAME, so the field-name test is
  the checker now. Re-run of the five: all killed; the whole battery re-runs on the fold commit.
- The instrument: `python -m scripts.target_reachability` — the worker axis still reaches the tier
  (`work_loop` in the closure), the JSON carries no `worker_gate`, the text names the dispatch and
  "no environment switch".

**Review round 1** (two lenses on `049f8b5`; the first dispatch of both died on the session's rate
limit and was re-run). The runtime core held under attack — nothing deployed names the switch,
settings load with an empty environment, nothing reads settings or the network between the
environment read and the refusal, the API keeps its 503 path. The surfaces and the guard did not.
Both lenses converged on the first four; closed in `5816b1b`:

- **BLOCKER (both lenses): `make init-db` could not run on this branch.** It named
  `tests/scripts/fixtures/legacy_by_hand.sql`, which exists only on phase 03's branch (and is
  gitignored here, so a local copy hid it), and it skipped step 0 — `step0_bootstrap.sql` (the seven
  `svc_*` roles 131 later GRANTs name) and `step0_legacy_ddl_door.sql` (the `window_ddl` door
  migration 050 calls) — which every lane run applies first. `setup-db`, `quickstart`, `reset-db`
  (after dropping the database) and README step 3 failed with it, under a CHANGELOG line saying the
  targets run. Rewritten onto the lane's real sequence and RUN: a throwaway `postgres:15` container
  (never the shared test cluster — step 0 creates cluster-wide roles), `make create-db init-db` as
  its superuser → 77 files applied, `legacy` 15 tables, `public` 26, ledger 77/77; container removed.
  Class: "the Makefile names a file" — a test now checks every `-f` path exists, and another pins the
  order. **For phase 03's rebase:** `init-db` must gain the by-hand fixture line there (078 needs the
  hand-made table in a tree-built database); the `-f` test will hold it to the file's existence.
- **RISK (both): `.env.example` could re-point the production webhook.** The sample webhook URL
  named a path that does not exist (`/telegram/webhook`; the only ingress route is
  `/webhooks/telegram`), and the file offered `TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER=true` and
  `RAILWAY_ENVIRONMENT_NAME=production` — the two switches that make a laptop holding the production
  token register itself as production's webhook, the never-run `storydump webhook register` effect by
  another door — with no warning, in a file the Makefile exports into `make run`. Also wrong: the
  connection cap (40; the code's default is 10), the lane concurrency (1/1; the dataclass's is 3/2),
  `PORT` (one description for two listeners with different fallbacks), the Railway tokens' reader
  (`scripts/observed_use.py` only — the CLI reads neither), "`make init-db` uses `DATABASE_URL`" (it
  builds its own), and a pool-sizing block for an engine that does not exist. All corrected against
  the code; the two platform-set names carry "never by hand"; `DATABASE_URL` is commented so the
  Makefile cannot export an ambient one into every target.
- **RISK (both): the reader rule was unsound**, and the ledger's table with it. A text match took
  the sentence "deliberately NOT read from `settings.DB_MAX_OVERFLOW`" for that field's reader, an
  `env.get("CLOUDINARY_API_KEY")` for a read of the FIELD, and the runner's argparse
  `args.database_url` for six callers of a property nothing calls. By AST now, with planted controls
  for each of those shapes. Class swept on the real tree: six survivors had no reader — deleted (the
  table above); `tests/src/config/test_settings.py` loses the property's tests and the pool tests,
  `test_unit_of_work.py`'s "not read from settings" pin becomes "not sized by the environment"
  (both names driven to 0/20/99, the pool and the overflow still the seams).
- **RISK (both): the setter sweep was four names wide.** 37 dead names are pinned now (the 25
  retired fields less the four still read from the environment, fifteen legacy `.env` names, the
  switch), with a soundness check that the list names nothing the tree reads. Second pass, same
  eleven files: 28 further mentions, 22 of them in `cloud-deployment.md` — fixed: the
  `DRY_RUN_MODE` go-live steps in both deployment guides (a safety step that did nothing; the real
  control is the workspace's Dry Run Mode, on the web), the pool-sizing blocks (three guides), the
  `MEDIA_DIR` section and troubleshooting row, the `DB_*` "alternative to `DATABASE_URL`" table
  (true at the base through the fallback; a boot refusal after this phase), the legacy schedule and
  media-source table, `FACEBOOK_APP_ID`, the rate-limit row's fallback variable, and `AGENTS.md`'s
  paragraph about a `chat_settings` column. `.env.example` agreement is asserted BOTH directions.
- **RISK (adversarial): the refusal test would have booted a worker** under the regression it
  guards — it called the real `worker.main()` with nothing stubbed (see the disclosure under "Red
  first"). `create_engine`, `compose` and `asyncio.run` now fail the test instead of running.
- **RISK (adversarial): the unverified precondition.** After this merge the worker exits 2 without
  `TARGET_DATABASE_URL`. Checked BY NAME ONLY (`railway variables --json` piped through a filter that
  prints keys, 2026-09-18): both services carry it. The same read gives the owner's removal list
  exactly — `WORKER_IMPL` on the worker, the three Telegram variables on both; none of the other 33
  dead names is set on either service.
- **GAP (adversarial): a blank `TARGET_DATABASE_URL`** passed `is None` and died in `create_engine`
  with a traceback (the API: at import). `engine_url_from_env` strips; blank is absent; tested.
- **GAP (structural): `DATABASE_URL_VAR`** moved to the vocabulary beside its five siblings; the
  API's 503 detail, its startup warning and the CLI's `webhook` verb read it from there.
- **GAP (both): the README dead-ended at the new refusal** — `TARGET_DATABASE_URL` in `.env`, then a
  bare `python -m src.main`, which does not read `.env` (only the settings fields load from it; the
  base's fallback had hidden that). `make run` now, and `.env.example` says how it is read.
  `pip install -e .` → `'.[cli]'` in the README and `AGENTS.md`.
- **GAP (both): untested behaviours** — the entrypoint "reads nothing" is an AST fact now (it
  imports `src.worker` and no other module); both spawns run from an empty directory with
  `PYTHONPATH`, so a developer's `.env` cannot satisfy a re-added requirement; `validate-env` exits 1
  when it fails (it echoed and exited 0); `make dev`'s missing health gate is pinned.
- **Battery (adversarial):** the two required-field mutations died at collection, so their named
  tests never gave a verdict — they run with the three variables re-armed now (one recipe, labelled),
  and one collection kill stays as its own behaviour. 34 mutations.
- **Simplify (structural):** the worker's stale W1-slice docstring, its double settings import, the
  instrument's label narrating a switch that no longer exists and overstating what is fatal, an
  assertion that could never fail (`"IMPORTABLE-NOT-SERVING"`; the instrument prints a comma), a no-op
  `env=`, the redaction file's name collision and redundant `delenv`, a case-insensitive strip in its
  fixture, field counts dropped from prose, the runner's docstring reason for standing alone.
- **Declined, with reasons:** a `PYTHON ?=` variable for the Makefile (the README activates the venv
  one step earlier; the test targets' `./venv/bin/` is a separate, older inconsistency); moving
  `async_database_url` out of `src/` (it is the harness's door and one test file's; noted);
  `railway.toml`'s residue (`mkdir -p /tmp/media`, the polling rationale of `drainingSeconds`) — F2
  forbids touching the file here; queued.
- **Premise findings the round added:** the plan's checklist line `grep -rn WORKER_IMPL src scripts
  tests → 0` cannot be met — the guard and the battery must name the switch to refuse it (0 in `src`,
  `scripts`, `storydump_cli`; 6 in those two test files). The prose mentions left for phase 05, by
  name: `operations/troubleshooting.md:23`, `monitoring.md:13`, `telegram-webhook.md:15` (the switch),
  `SECURITY_REVIEW.md:59` ("loaded via `settings.TELEGRAM_BOT_TOKEN`", now false).

**Re-verify after the fold** (2026-09-18; no variable of either tier set in any run):

- Units without a database: `2230 passed, 26 skipped`. The whole suite against the Docker test
  database: `3640 passed, 1 skipped`. CI on `3649a4d`: every check green, `3679 passed, 1 skipped,
  5 deselected`. `ruff format --check .` / `ruff check .` clean.
- Battery on the committed tree, in its own worktree (so it cannot mutate files under a running
  suite): **34 of 34 killed** at `3649a4d` — 33 by their named test's verdict, one by design at
  collection. The first run on `5816b1b` had died at mutation 17: two mutation NAMES used the
  single-quote escape idiom inside double quotes, which inverted the quoting for thirty lines and
  aborted on a glob (`zsh -n` passed — parity returned). Found by reading the count of `killed` lines
  against the expected 34, fixed in `3649a4d`. The battery now ends with `ran N of M mutations`, so a
  mid-script abort has no last line.
- A FRESH lens on `5816b1b` (it had not seen the work): items 1, 2, 5, 6 and 8 of the fold CLOSED
  with evidence (the recipe and its files, the AST rule — it tried model dumps, aliased receivers,
  variable `getattr`s and store-only "reads", and found no field resting on a blind spot but one,
  below — the refusal tests' safety, the vocabulary move and the CLI boundary, no fold-introduced
  failure by a differential against the pre-fold tree under the same sandbox); item 7 NOT CLOSED at
  that commit — the battery defect above, already fixed when the report arrived; items 3 and 4
  PARTLY, on prose. Its findings, folded in the commit after `3649a4d`:
  - Two guide passages still implied `DB_*` points a deployed app at a database
    (`cloud-deployment.md`'s troubleshooting row, `dev-environment-setup.md`'s Neon option) — only the
    deleted property ever did that. They now name `TARGET_DATABASE_URL`, and say what `DB_*` DOES
    steer: the harness and `make`, `reset-db` included.
  - Class "a documented fresh-database sequence that cannot complete" (round 1's blocker, one level
    out): three guides applied `setup_database.sql` and went straight to the runner, which stops at
    migration 050 without step 0. Swept (`grep -rn setup_database.sql` over the live docs, the
    README, the agent docs, the Makefile): 5 hits — the three guides now carry step 0, the Makefile
    already did, the fifth is a dated update's prose. The README says what `make init-db` creates.
  - `FACEBOOK_APP_SECRET` was labelled "Facebook Login OAuth", a flow that does not exist (its one
    reader verifies Meta's signed callbacks); the sample webhook secret was a public string that would
    arm the ingress if uncommented as written. Both fixed.
  - The API's blank-URL path had no test (the shared function had): one added; the warning says
    "unset or blank".
  - RECORDED, not changed: the reader rule is syntactic, and `DB_NAME` survives on a read no caller
    takes (`database or settings.DB_NAME`; every caller passes `database`). The guard's docstring says
    so. Pre-existing and queued: the Makefile's `APP_DB_URL` does not URL-encode the password.

**Deploy reachability:** Railway deploys `main` on merge; the worker takes it (SUCCESS at every commit
since the cutover). The API service (`storydump`) has SKIPPED every deploy since `d8f5c72`
(2026-09-16): `53ca6d6`, `4f2b36b`, `2369a9b`, `deb29c2` — each behind a red `main` check at deploy
time; `main`'s re-run going green did NOT trigger a redeploy (checked 2026-09-18 00:5x UTC with
`storydump deploys`). This phase's merge is the next chance, provided `main`'s check is green at that
moment; otherwise the owner's `railway redeploy --service storydump`. Until an API deploy lands, the
API-side changes of five merges are not live — the fold's API fixes, phase 01's deletion, this phase's
settings.

## Phase 02 — merged

`f59fe43`, squash-merged with `--admin` on 2026-09-18 15:44 UTC after CI on the final commit `9d14304`
(`3680 passed, 1 skipped`, every check) and the battery on that committed tree (`ran 34 of 34
mutations`, 34 killed). Invariants re-run on `main` at `f59fe43`: I2 the lane and the advertised stream —
36 passed; I3/I6/I7 and the settings guard — 122 passed with no variable of either tier set; I4 the
ratchet 4 / 0 / 0 / 0; the instrument: worker closure +532, 35 target modules, both controls PASS; I1 the merge's own CI run on `main` green (`3680 passed, 1 skipped`). Deploy reachability
(`storydump deploys --watch --commit f59fe43`): the worker deployed it — `SUCCESS` at 15:46 UTC, so the new
boot refusal did not fire in production (both services carry `TARGET_DATABASE_URL`, checked by name before
the merge). The API service: the API deployed it too — `SUCCESS` (a fresh process, `storydump health` ok on every surface, the webhook registered), its FIRST deployment since `d8f5c72` (2026-09-16). With it the 2026-09-16 review fold's API fixes, phase 01's deletion, #1317 and phase 02 are all live on both services. CLOSED.

## Phase 03 — the 3f snapshot migration and the ratchet's file rule

**Step 1, measured** (2026-09-17, the read-only probe via `railway run --service worker -- sh -c 'psql
"$TARGET_DATABASE_URL" …'`, output in the scratchpad's `probe03.out`):

- PostgreSQL 17.11. `current_user` under `TARGET_DATABASE_URL` is `neondb_owner` — the runtime login IS
  the database owner (#751's posture is production's state; the runner's own `DATABASE_URL` is that same
  owner login per `railway.toml`).
- The `legacy` schema holds exactly the sixteen tables of `LEGACY_TABLES` — 42 MB in total (`media_items`
  15 MB, `service_runs` 15 MB, `user_interactions` 7 MB, `posting_history` 2 MB, the rest under 1 MB);
  `n_live_tup` reads 0 for all (stats not collected since the move — the postconditions count rows).
- Schema owners: `legacy` → `svc_migration` (the bootstrap ran); `public`, `archive`, `runner` →
  `neondb_owner`; the legacy TABLES are `neondb_owner`'s (built by hand as the owner). Memberships:
  `svc_migration` is a MEMBER of `svc_claim`, `svc_clock`, `svc_maintenance` and `svc_membership` (the
  bootstrap's leg 2, no admin — the rows phase 04's stand-down concerns); `neondb_owner` holds every
  `svc_*` directly WITH ADMIN (it created them) and `svc_migration` (leg 4). A second probe (2026-09-17,
  `probe03b.out`) answered the plan's owner-run lines under the same login: `pg_has_role(current_user,
  'svc_maintenance', 'MEMBER'|'USAGE'|'SET')` all true, `has_schema_privilege(current_user, 'archive',
  'CREATE')` true — so `OWNER TO svc_maintenance` is legal for the owner on PG17's SET rule with no
  bracket, and 078 carries none. `rolbypassrls` is true for `neondb_owner` only among the app's roles.
- A third probe (`probe03c.out`): every ledger row since the runner was armed, 066–077, was applied by
  `neondb_owner` — the runner's actor is measured, not inferred from `railway.toml`'s comment;
  `statement_timeout` is 0 and `idle_in_transaction_session_timeout` 5 min on that login's session;
  `n_tup_ins/upd/del` are 0 on all sixteen legacy tables since the stats' last reset (nothing writes to
  `legacy`); `archive`'s ACL is `neondb_owner=UC, svc_maintenance=UC` with no default ACL;
  `posting_history` holds 4,642 rows and `media_items` 4,619.
- The runner's ledger head is 077 (77 rows). `posting_history_dedup_archive`: 19 columns (uuid ids, naive
  timestamps, `archived_at timestamptz`), no constraints, no indexes — now `tests/scripts/fixtures/
  legacy_by_hand.sql`.
- Owner-run, still owed and GATING THE MERGE (the plan's own step 1): the 078 rehearsal on a Neon PITR
  branch with its wall-clock (42 MB of copies in one transaction; `statement_timeout` is 0, so the budget
  is Railway's predeploy, which `railway.toml` does not bound). `runner status` under `DATABASE_URL` is
  answered read-only by the ledger probe (`head 077`, every row `neondb_owner`).

**Built:** `scripts/migrations/078_legacy_snapshots_pre_cutover.sql` (sixteen `CREATE TABLE … AS TABLE` +
`OWNER TO svc_maintenance`, 32 postconditions, `-- runner:unadvertised`, no `07` block, no manifest row —
the pin at `test_advertised_ddl.py:290` stays 35); the runner's `UNADVERTISED_MARKER`, `KNOWN_MARKERS` and
the unknown-marker refusal at discovery (naming the file); `target_lineage_files` excludes an unadvertised
file (one definition, both consumers); the lane's world seeds the hand-made table beside the legacy seed
(`BY_HAND_SQL`, applied INTO `public` before the 051 move, as production's was) and its inventory literal
is one list again (`LEGACY_TABLES`; `HAND_MADE` records provenance and is pinned equal to the fixture's
`CREATE TABLE` names); the gate `tests/scripts/test_legacy_snapshots.py` applies 078 as the OWNER actor
(production's shape) and as `svc_migration` (the lane's), on seeded tables (the hand-made one with a row,
`schema_version` with the lineage's fifty), asserts existence, owner, row counts and no read for
`svc_ingress`, that a second `apply` is a no-op, and that a missing table fails the file whole with no
partial archive. Tests red first: the runner's marker tests, the ratchet's file rule, the lane's list and
the gate all failed on the base (the gate could not even find a 078).

**Premise findings:** the plan's F4 dispositions listed `HAND_MADE` as a subset the lane could not hold;
the honest world holds it (production does), so `LINEAGE_TABLES` is gone and the lane compares all sixteen.
The plan's "the runner applies files as `svc_migration`" was corrected by the lenses and is now measured
false for production (the owner) — the gate's owner arm is the one that matches the next deploy.

**Verification before the fold** (the worktree at `e738e2e`/`84b1a2f`): `tests/scripts` whole against the
Docker test PostgreSQL — `1412 passed, 1 skipped` (the live-drift audit); the gate's six tests green in
both actor arms; units `2225 passed` plus the environment's two loopback failures; CI on `84b1a2f`
(the fixture, gitignored by `*.sql` at `e738e2e`, tracked with an exception): `3666 passed, 1 skipped`,
nine checks green. Battery on `e738e2e`: 13 killed, one collection-time refusal reclassified.

**Review round 1** (two lenses on `e738e2e`): no blocker for the next deploy. Lens 1 (structural): 4
risks, 7 gaps, 7 simplifications. Lens 2 (adversarial): 6 risks, 8 gaps, 4 surviving mutants. Closed:
- The marker grammar was narrower than F6 promised — `-- runner: manual` (a space after the colon),
  `-- Runner:manual` and a bare `-- runner:postcondition` all read as prose. Now ONE dispatch on the word
  after `runner:` (`_MARKER_RE` case-insensitive on `runner`, any spacing): an unknown word is refused, a
  flag refuses an argument, a bare postcondition is refused; prose that merely mentions a marker
  mid-line is not one. Seven new cases in `TestMarkers`; `Migration.unadvertised` has no default.
- Fourteen of the sixteen row-count postconditions compared 0 = 0 in both DB arms (only the hand-made
  table and `schema_version` carried rows), so the plan's own `WITH NO DATA` mutation on
  `posting_history` would have survived — the ledger's earlier text did not say so. Closed by a UNIT PIN
  of the file: its thirty-two statements and thirty-two postconditions are generated from
  `LEGACY_TABLES` and compared exactly (`normalize_statements`), which kills `WITH NO DATA` on any
  table, a dropped `OWNER TO`, a stray `GRANT`, a swapped row-count source, a mixed date and a second
  transaction marker without a database; the DB arms keep the runtime facts, and the `svc_ingress`
  denial is asserted on every snapshot, not one.
- 078's header now states what was inferred: the actor and its rights as measured (the ledger's
  `applied_by`, `pg_has_role … 'SET'`), the transient memberships F8's stand-down keeps and which any
  stand-down must follow by phase order, the no-writer premise the same-transaction count check
  rests on (measured), the one-transaction shape, that the retention clock runs from the NAME's date
  (eligible 2026-12-16 whatever day 079 runs — "the drop-era date" was wrong) with the export option,
  that the postconditions serve as adoption evidence only until 3g, that the date is frozen at merge
  (the runner checksums bytes), and that a database built from the tree must pre-create the hand-made
  table from the fixture. "Readable by nothing but the owner" → readable by `svc_maintenance`'s members
  (the owner; `svc_migration` until the stand-down) — CHANGELOG and header alike.
- The lane pins the two lists apart (the advertised lineage 052–077; the unadvertised files above the
  move, `["078…"]`) under a heading that no longer calls them all "the target lineage".
- Docs: `migration-runner.md`'s marker contract (schema-move, unadvertised, the refusal),
  `backup-restore.md`'s F9 line, and phase 04's step 1 gains the two obligations the grammar imposes
  (`manual` joins `KNOWN_MARKERS` in the same commit as the first manual file; 079/080 stay out of the
  prefix diff by `unadvertised` or by the rule learning `manual`).
- The ledger's own errors, fixed above: the membership direction was reversed (`svc_migration` is the
  member); "the lane's actor" was wrong (the lane applies as the test admin; the `svc_migration` arm is
  the gate's own, per the runbook); `schema_version` holds 48 rows in the replay, not fifty.
- Declined, with reasons: seeding one row in every legacy table (the unit pin closes the hole without
  sixteen INSERTs against a schema with foreign keys); `n_tup_ins` flatness as a pre-deploy owner step
  (measured here, 0 on all sixteen — the header states the premise); a `statement_timeout` for the
  runner (the plan sets none; a timeout mid-078 rolls back whole and aborts the deploy loudly — the
  rehearsal sizes the copy instead).
- Battery: 19 mutations (13 without a database, 4 under the DB gate, plus the lane's two-list pin).

**Re-verify after the fold** (the fold commit `000da0e`, 2026-09-18): CI green on the fold (`3673 passed,
1 skipped, 5 deselected`; every check passed); `main`'s re-run of the DB-gate teardown flake completed
green. Battery on the committed tree: 18 of 19 killed — the parser arms (unknown marker, a stray space
after the colon, a bare postcondition, `unadvertised` read off the file), the F.2 prefix rule and the
snapshot file's own pin (not advertised, every table of the inventory, `WITH NO DATA` without a database,
a stray `GRANT` without a database, a row-count postcondition per table and one compared to the wrong
source, one transaction, a name dropped from the inventory, the hand-made subset) and the four DB gates
(the owner hand-off to `svc_maintenance`, `WITH NO DATA` at runtime on the ledger's rows, `svc_ingress`
denied on every table, the lane's world holding the hand-made table). ONE SURVIVED: "the lane keeps the
two lists apart" mutated the LANE TEST's own derivation (`m.unadvertised` → `m.version == 78`), which is
an EQUIVALENT mutant on this corpus — 078 is the only unadvertised file, so both predicates select the
same list. Lesson recorded in the battery: a test-side mutation of a list the corpus makes coincide
proves nothing. Re-pointed at the ratchet's filter (`not m.unadvertised` dropped) with the lane's pin as
the checker — killed (`addfefb`).
- Deploy reachability at this phase: the merge waits on the owner's rehearsal; Railway applies 078 at
  BOTH services' next predeploy after the merge. The API service had SKIPPED every deploy since
  `53ca6d6` behind red `main` checks; #1317 merged (`deb29c2`), the worker deployed it, the API skipped
  again behind a DB-gate teardown flake on `main` (`role "svc_ingress" cannot be dropped` at a scratch
  database's teardown — pre-existing, re-run requested); queued.

**Rebased onto `f59fe43`** (2026-09-18, after phase 02 merged; the branch squashed to one commit
first, so the changelog and this ledger conflicted once). What phase 02's merge asked of this branch:
the battery's two recipes lose the three dummy variables (main's new guard refuses a recipe that sets a
dead one) and the battery ends with `ran N of M`; `make init-db` gains
`tests/scripts/fixtures/legacy_by_hand.sql` beside the by-hand base — 078 snapshots the hand-made
table, so a database built from the tree must hold it — and the guard's sequence test learns the line.
PROVEN on a throwaway `postgres:15` container (never the shared test cluster): `make create-db
init-db` → `78 applied`; `legacy` 16 tables, `archive` 16 snapshots, every one owned by
`svc_maintenance`; container removed. The whole suite against the Docker test database on the rebased
tree, no variable of either tier set: `3661 passed, 1 skipped`. The battery and CI on the rebased commit
are recorded in the PR.

**The 078 rehearsal on a Neon PITR branch** (2026-09-18 17:11–17:14 UTC; agent-driven with the owner's
production permission, granted in chat; the M.2 spec's discipline — provenance gate, structural
isolation, settled-state measurements). Access: the machine's Neon login had to be re-authenticated by
the owner into the account that owns the project (`ancient-grass-50759240`, `storyline-ai-db`,
org `org-ancient-bush-46337162`); no Neon key exists on either Railway service or in the repo.
P4 observed, not assumed: the project's `history_retention_seconds` is **86400** — a 24-hour PITR
window, NOT the ≥ 7 days `05` §DR states (recorded for the owner; the branch was taken at head).

- Branch `br-odd-cake-ai91lhcl` (`claude/078-rehearsal-20260918-1711`), parent = the production
  branch `br-square-frog-ai37r0qg`, LSN `A/2CE04A20`, created 17:11:38Z; endpoint
  `ep-fancy-surf-ai21idz6` (production's is `ep-hidden-shadow-aify76h5`; the gate refuses a
  connection string whose host is production's or not a Neon endpoint). The harness holds ONLY the
  branch's owner connection string, obtained from `neonctl connection-string <branch name>` — the
  production endpoint appears nowhere in its inputs; nothing is printed unredacted.
- Pre: PostgreSQL 17.11; `current_user = neondb_owner` with `pg_has_role(…,'svc_maintenance','SET')`
  and `has_schema_privilege(…,'archive','CREATE')` both true; ledger head 077 (77 rows), 078 pending
  `[wrapped]`; the 16 legacy tables at production's counts (posting_history 4,642, media_items 4,619,
  service_runs 7,527, user_interactions 11,163, schema_version 46, the hand-made table 13); legacy
  total 43,835,392 bytes; database 72,433,664 bytes; zero archive snapshots.
- **`apply`: 078 applied, wall-clock 3.42 s** (the whole runner invocation, one transaction). Ledger
  row 78 `applied_by = neondb_owner` at 17:13:07Z; head 78/78.
- Post: 16 snapshots `archive.<t>_pre_cutover_20260917`, every one owned by `svc_maintenance`; every
  row count equal to its source (all sixteen pairs); `svc_ingress` denied SELECT on all sixteen;
  archive total 9,461,760 bytes; database 82,018,304 bytes — **storage delta 9,584,640 bytes (9.1 MB),
  ≤ the 42 MB ceiling** (a copy carries rows only: no indexes, no toast of its own beyond the rows).
- Second `apply`: `0 applied`; 16 snapshots unchanged.
- Production, read-only through the sanctioned probe after the branch apply: ledger head 077,
  zero archive snapshots, 16 legacy tables — untouched (pasted in the PR).
- An independent read-only verification of the branch by the Neon analyst agent (its report in the
  PR), then the branch deleted.

## Phase 03 — merged

`3ffa750`, squash-merged with `--admin` on 2026-09-18 17:23 UTC after the rehearsal (above), its
independent verification, and CI on `6da8d00` (`3700 passed, 1 skipped`, every check). Invariants re-run
on `main` at `3ffa750`: I2 and the snapshot gates — 45 passed under the database; I3/I6/I7 and the
guards — 138 passed with no variable of either tier set; I4 the ratchet 4 / 0 / 0 / 0; I8 — the runner's
three suites, 58 passed under the database; I5 — the one pending file was the one the deploy was meant
to apply.

**Deploy reachability — applied in production.** Both services deployed `3ffa750` (`SUCCESS`; the API
behind `main`'s green check, ~7 minutes after the worker). The predeploy applied 078: ledger row 78,
`applied_by = neondb_owner`, 17:24:26 UTC. The read-only probe at 17:30 UTC: 16
`archive.<t>_pre_cutover_20260917` tables, every one owned by `svc_maintenance`; every row count equal
to its source (all sixteen pairs, the same counts the rehearsal saw); `svc_ingress` denied SELECT on
all; archive 9,281,536 bytes against the 42 MB ceiling (legacy 43,835,392 bytes); database
81,887,232 bytes; `storydump health` ok on every surface with the new processes up. Phase 04's
dependency — "phase 03 merged and applied in production, every snapshot present" — is met.

## Owner-decision queue

- **The PITR window is 24 hours, not 7 days.** The project's `history_retention_seconds` is 86400;
  `05` §DR states a ≥ 7-day floor "verified at 0.2's gate". Phase 04's backout is PITR to the marker
  taken before the window — a 24-hour window is enough for a same-day window with the worker stopped,
  but the plan's stated floor is not the configured one. The owner's call: raise it in Neon before
  phase 04's window, or amend `05` §DR to the measured value.
- **Close #1202 on its "or" leg on GitHub** (ruled in chat, 2026-09-16, gate 2: the target tier is
  armed and serving with a connected destination): phase 04's PR may not merge before the issue is
  closed with that ruling. One comment and a close, the owner's.

- **The API service's skipped deploys.** Railway marked `53ca6d6`, `4f2b36b`, `2369a9b` and `deb29c2`
  `SKIPPED` on the `storydump` (API) service while the worker deployed — each behind a red `main` check
  at deploy time (the midnight skip-ceiling flake three times, then a DB-gate teardown flake); a later
  re-run going green did NOT redeploy. Phase 02's merge `f59fe43` landed with `main` green: the API deployed it too — `SUCCESS` (a fresh process, `storydump health` ok on every surface, the webhook registered), its FIRST deployment since `d8f5c72` (2026-09-16). With it the 2026-09-16 review fold's API fixes, phase 01's deletion, #1317 and phase 02 are all live on both services. CLOSED.
- ~~A latent CI flake: the skip ceiling meets a clock-of-day skip.~~ CLOSED by #1317 (`deb29c2`): the
  cap-wait test gives its account a noon timezone and no longer skips, after the flake blocked the API's
  deploy three times.
- **A startup secret check for the target tier?** The legacy `ConfigValidator` (deleted with phase 01) checked `ENCRYPTION_KEY` at boot; nothing in the target tier does the same at import. A decision, not a regression.
- **Phase 02 premise findings from round 1:** `unit_of_work.async_database_url()` falls back to the
  legacy `DB_*` fields when `TARGET_DATABASE_URL` is unset — with the legacy loops gone, a boot
  without the variable (a local `make run`, a preview service) runs the target worker against the
  legacy-configured database; phase 02 makes `TARGET_DATABASE_URL` mandatory at boot and retires
  the fallback with the fields. The README quickstart (`psql -f scripts/setup_database.sql` then
  `make run`) and the Makefile's `run`/`dev`/`init-db` describe the legacy tier (phase 02).
  `railway.toml`'s `drainingSeconds` rationale (Telegram polling) is stale (F2 forbids touching
  it here; phase 02). Eight requirements with zero importers (the table above).
- **After phase 02 deploys (owner-run):** remove `WORKER_IMPL` from the worker service and
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `ADMIN_TELEGRAM_CHAT_ID` from BOTH services — measured by
  name on 2026-09-18, these are the only dead variables set on either (nothing reads them; the landing
  app's own two on Vercel stay); replace `.claude/settings.json:54-59`'s four deny rules that
  name the deleted legacy CLI's commands (commands that no longer exist) — the `python -m src.main`
  rules stay.
- **`railway.toml`'s residue** (F2: untouched here): the build command still runs `mkdir -p
  /tmp/media` for a directory nothing reads, and `drainingSeconds`' comment explains a Telegram polling
  session nothing holds. A comment-and-build-line edit, the owner's call on when.
- **The Makefile's `APP_DB_URL` does not URL-encode `DB_PASSWORD`** (pre-existing): a password
  containing `@` mis-parses into the host. Local development only.
- The tear-out's own gates as they arise: phase 04's window (F7 — the owner runs it; the PR ships the files, the runner's manual mode and the runbook).
