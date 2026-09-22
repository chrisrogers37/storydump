---
title: "Legacy tear-out — sprint run log (build-all)"
type: plan
status: completed
owner: chris
created: 2026-09-17
tags: [legacy-retirement, migrations, worker, api, docs, run-log]
links: ["https://github.com/chrisrogers37/storydump/issues/1216", "https://github.com/chrisrogers37/storydump/pull/1315"]
---

> **Closed 2026-09-20.** Five phases merged (#1316, #1319, #1318, #1321, #1322), the window run on 2026-09-19, the residue #1324 and the docs PR #1325 merged 2026-09-20, the marker branch retired and the dead variables deleted. What the owner-decision queue still lists is outside the tear-out: #751 (the runtime logins), #739 (the Facebook secret fallback), the PITR floor (`05` §DR 7 d vs Neon's 24 h), the l8 admission flake, `.claude/settings.json`'s legacy rules, `railway.toml`'s `mkdir -p /tmp/media`. The 2026-12-16 expiry of the `archive.*_pre_cutover_20260917` snapshots is tracked at #1326; the sweeper it would need (`retention_sweep`, unbuilt) at #1327; the PITR floor at #1328.

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
| 04 the gated drop and stand-down | `04_drop-and-stand-down.md` | **DONE — merged `c8482b3` (2026-09-19 19:44 UTC, by the owner); the worker deployed it (SUCCESS) and its predeploy owed 079/080 and applied nothing, measured; the API's deploy under the phase's merged entry; the window itself is the owner's (F7)** | #1321 | green at `29537a8` (3753 passed, 1 skipped) |
| 05 the documentation's end state | `05_docs-end-state.md` | **DONE — merged `3c8efd4` (2026-09-19 21:06 UTC, by the owner); both services deployed it (SUCCESS); its window-dependent sentences and the epic's closeout wait for the owner's window** | #1322 | green at `2c82168` (3760 passed, 1 skipped) |

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

## Phase 04 — the gated drop and stand-down

**Read before building** (main at `32d2d66`, 2026-09-18): the runner's marker grammar (phase 03's ONE
dispatch on the word after `runner:`), `apply_pending`'s below-head loop, `ledger_discrepancies` (ledger
rows vs files only — a pending file below the head is `apply`'s concern alone), `adopt`'s false-probe rule
(a trailing pair of false probes stays pending; 079/080 are last), `split_statements`' dollar-quote
handling (the DO blocks split correctly), the design plan's printed stand-down (`04:211-262`) and D40's
amendment (`03:181`), the epic's F6/F7/F8, the M.2 spec's §3 probes, the CI cluster's version
(**PostgreSQL 15**; production 17), the Railway and Neon commands the runbook prints (`railway down`,
`railway redeploy`, `neonctl branches restore <target> <source> --preserve-under-name`).

**Premise findings before writing:** (1) `uuid-ossp` rides into `legacy` with the 051 move and drops
with the schema — no target file or module calls `uuid_generate*` (7 target files use
`gen_random_uuid()`); the gate asserts the extension is gone and `gen_random_uuid()` still answers.
(2) Every door file already brackets the SCHEMA half of `ALTER FUNCTION … OWNER TO svc_*` itself —
`GRANT CREATE ON SCHEMA public TO svc_x; … REVOKE` (059:93/601, 062:49/168, 063:64/196, 064:54/73,
068:34/149, 076:68/83); only the MEMBERSHIP half was ever the window's, and it is what F8 (a) keeps.
(3) On PG16+ `OWNER TO` needs SET on the receiving role; the owner login's SET runs through
`svc_migration`'s memberships (078's header measured it) — so 080 may revoke NEITHER `svc_migration`'s
four memberships NOR the owner's membership of `svc_migration` (the printed stand-down revoked both;
the plan's F8 (a) text named only the first). Recorded in the D40 amendment. (4) `public` in production
is owned by the OWNER LOGIN, not `svc_migration` (phase 03's probe): the printed gate's "steady-state
design fact" line is false there; 080's gate asserts the measured shape. (5) The plan's checklist line
"`grep -rn WORKER_IMPL …`" has no phase-04 analogue; its "`storydump posture` shows 079/080 applied" is
owner-run (the CLI needs a signed-in token).

**Red first** (`tests/scripts/test_window_close.py` — 13 tests; `TestManual` and two marker tests in
`test_migration_runner.py`; the lineage rule's manual case; the lane's two lists; the never-run pin): on
the base the runner has no door — collection fails on `ImportError: cannot import name 'apply_manual'`,
the honest red for a door that did not exist.

**Built** (`0ff46e9`, the battery fix `0db363b`): `MANUAL_MARKER` in `KNOWN_MARKERS`; `Migration.manual`;
`ApplyReport.owed`/`StatusReport.owed`; `apply_pending` owes manual files before the below-head loop;
`apply_manual(dsn, dir, version)` — refuses a version not in the tree, one without the directive, one
already recorded, then lock → ledger → integrity → `_apply_one`; `apply --manual VERSION` and the
`owed (manual) NNN` lines in `apply` and `status`; `target_lineage_files` excludes manual files.
`079_drop_legacy_schema.sql` (manual + unadvertised; the DO block over the sixteen names: snapshot
present, source present, counts equal, else RAISE; two postconditions) and `080_window_stand_down.sql`
(manual + unadvertised; the identity guard, `DROP SCHEMA IF EXISTS window_ddl CASCADE`, `REVOKE CREATE
ON DATABASE … FROM svc_migration`; the gate's eleven lines as comments with their answers; two
postconditions). The gate (three DB layers + the files' unit pins), the runbook
`documentation/operations/legacy-window-close.md` (the rehearsal, production in order, the backout,
what not to do), the D40 amendment beside `03:189`, `migration-runner.md`'s marker and door, the
never-run lists in `CLAUDE.md`, `AGENTS.md` and both satellites, the CHANGELOG.

**Verification** (2026-09-18, the worktree at `0ff46e9`/`0db363b`, no variable of either tier set):

- The gate, the runner's three suites and the never-run pin under the Docker test database (PG 15):
  67 passed. The gate is version-aware — SET on 16+, membership on 15 — because CI's cluster is 15 and
  production 17; the first draft asserted 'SET' and failed on 15.
- The whole suite against the test database: `3689 passed, 1 skipped`.
- `ruff format --check .` / `ruff check .` clean.
- Battery `tests/mutations/legacy_tear_out_04.sh` on the committed tree, in its own worktree: 24
  mutations; on `0ff46e9` 23 killed and ONE SURVIVED — the "below-head rule sees a manual file again"
  mutation edited the loop that no longer sees manual files (equivalent); the wedge the plan describes is
  the NEXT deploy after an ordinary file lands above an owed one, so the test gained that scenario and
  the mutation moves the split below the check — killed on `0db363b`. `ran 24 of 24`.

**Review round 1** (two lenses on `0ff46e9`). The structural lens: no blocker — `apply_manual` is the
smallest honest door (the same lock, ledger creation and integrity check in the same order; `_apply_one`
has exactly two callers; `adopt` only records, `repair` only updates), both files correct as PL/pgSQL and
single-transaction, the D40 amendment's two factual claims measured in-tree. The adversarial lens on the
one property that matters — can any deploy drop `legacy`? — no, on the committed text, by construction
(the pinned `preDeployCommand` carries no `--manual`; `tests/test_deploy_guardrails.py` fails CI if it
ever does). What both found, folded in `9a94f04` and the commit after it:

- **RISK (adversarial): a marker could be demoted to prose silently.** `--- runner:manual` (three dashes),
  `-- -- runner:manual` (an editor's "comment this line" on a comment), `/* runner:manual */`,
  `# runner:manual`, `-- runner manual`, a marker after code on its line, and a byte-order mark before a
  line-1 marker all read as PROSE — after which 079 is an ordinary file the next predeploy applies, its
  precondition passes in production by design, and the schema drops; the only guard was CI's unit pin.
  Closed IN THE RUNNER: a near miss — a comment opener followed straight by `runner` and a known word in a
  frame the grammar does not read — is refused at discovery (`_NEAR_MISS_RE`; a mention mid-sentence stays
  prose, pinned); files decode as `utf-8-sig`; a file that is not UTF-8 is refused by name. Seven
  near-miss spellings, the BOM and a UTF-16 file are tests.
- **BLOCKER (adversarial): the runbook's rehearsal had a production-reaching path.** `neonctl
  connection-string` with an EMPTY branch name resolves to the default branch — production — and the
  host guard was an advisory `grep -v` that stopped nothing; with `$NAME` unset in a fresh shell, step 4
  would have applied 079 to production as the owner. Closed: `${NAME:?}` wherever the name is used; a
  `case`-based guard that unsets the URL and ends the shell for production's host, an empty host, or a
  non-Neon host.
- **GAP (both): the wedge scenario had no test and its battery mutation was inert** (the loop it edited
  no longer saw manual files). The test now lands an ordinary 004 while 002 is owed below the head —
  production's exact shape after phase 05's first file — and the mutation moves the split below the
  check; killed.
- **GAP (adversarial): `apply_manual` applied over a wedged tree** — an ordinary file still pending below
  the gated one. Refused now, naming the file and the way through (`apply`); tested.
- **RISK (adversarial): 079's precondition saw counts only.** An update in place, a delete-and-insert,
  a column added since 078, or a relation added to `legacy` after 078 (dropped by CASCADE with no
  snapshot) all passed. Closed: exactly sixteen relations of any kind in `legacy` (measured in
  production the same day: 16 tables, 77 indexes, nothing else), and every row hashed
  (`md5(row::text)`, the multiset difference) beside the count. Four refusal arms parametrized —
  insert, delete, update in place, column added — and a stray-relation arm. Disclosed in the file and
  the runbook: indexes, constraints, defaults and sequence values are what the drop takes unseen.
- **GAP (adversarial): 080's guard could not tell a completed 3g from a database that never held
  `legacy`** (a fresh target-only database passes "jobs present AND legacy absent"). Closed: the guard
  also requires 079's ledger row; tested by dropping `legacy` by hand and expecting the refusal.
  Measured in production for the file's header: `window_ddl` was never created there (the drop is a
  no-op in production and CI's to exercise), and `svc_migration` DOES hold `CREATE ON DATABASE`, so the
  revoke and its postcondition do real work.
- **GAP (adversarial): the gate's roleid-side line exempted ANY owner membership.** Tightened to the
  creator auto-grant (`admin_option`, 16+) or the bootstrap's explicit `svc_migration` grant; a stray
  `GRANT svc_worker TO <owner>` fails it (the battery plants one). The runbook's rehearsal and production
  gates gained that line and the `public`-owner line, which only the 17 rehearsal and production can
  show (CI's cluster is 15).
- **RISK (both): the runbook.** The backout restores by branch ID, not the name `production`; `railway
  redeploy` takes no `--environment` and acts on the LINKED one (a link check before down and redeploy,
  a fallback named); `railway run` executes the LOCAL checkout with production's owner login, so step 0
  demands a clean checkout at the deployed commit and records the files' sha256; the gate runs as the
  login 080's `current_user` lines are written for (`DATABASE_URL`, the owner), not the runtime one;
  "every line 080 prints" → "the load-bearing lines (the gate test runs all eleven)"; the marker branch's
  durability stated as Neon's documented model, unmeasured; the vacuous `posture … doors` claim
  corrected in the runbook AND the plan's checklist (`doors` reads `public` only and never listed the
  step-0 door).
- **Simplify (structural):** one guarded apply shared by both doors (`_apply_guarded`); an unused name;
  one clearer assertion on 080's revokes; comments on the two refusal tests whose kills hinge on their
  MESSAGE asserts; 079's header names its third refusal.
- **Declined, with reasons:** an ordering rule inside `apply_manual` (`--manual 80` before `79`) — the
  files' own guards carry it (080's identity guard now includes 079's row); refusing a dotted file name
  at discovery (`002.5_x.sql` is silently ignored — pre-existing, every lineage's, queued for the owner).
- **Premise findings the round added:** the plan's F8 (a) text named only `svc_migration`'s four
  memberships as what door files need; the owner's OWN membership of `svc_migration` is the other link
  of the SET chain, and 080 keeps both. The epic's risk-table row "both services' predeploys fail on
  every push" was claimed covered by a test that did not exercise it — it does now.

**Re-verify after the fold** (a fresh lens on `bb0175a`, read-only, no database — the sandbox refuses
the port, so every database-backed claim was verified by reading, collection and static counts;
`git diff 0ff46e9 bb0175a --stat`: 11 files, +494/−111). Round 1's eight claims, each measured:

- **The near-miss rule — CLOSED.** The regex consulted only where `_MARKER_RE` fails; a synthetic
  corpus, one file per spelling: the seven must-refuse spellings REFUSED, the BOM read, UTF-16 refused
  "not UTF-8", four must-stay-prose lines silent; the REAL corpus — 82 files, 5,900 lines, 131 marker
  lines — 0 would-refuse hits, `discover_migrations` → 80 files, manual = [79, 80]. Still silent, and
  recorded: `-- runner-manual`, `-- runner=manual`, a bare `-- runner:`, `-- runners:manual`, a
  zero-width space, an em dash, a full-width colon, a string literal — and a BOM-less UTF-16-LE file,
  whose bytes are valid UTF-8 (ASCII with a NUL after every character): it discovered as an ordinary
  marker-less file and failed loudly at apply (psycopg2 refuses a NUL), never silently.
- **`apply_manual` over a wedged tree — CLOSED.** The below check sits behind the session advisory
  lock, the ledger and the integrity check, so a concurrent `apply` cannot race it; the message names
  the file and the way through.
- **079's precondition — CLOSED.** The relkinds exclude indexes, sequences and TOAST (production's 16
  tables + 77 indexes + uuid-ossp count 16); with equal counts, `legacy EXCEPT ALL archive = ∅` proves
  equality (multiplicities ≤ everywhere and sums equal); `md5(row::text)` is never NULL and both sides
  render in one session; every column type across `setup_database.sql` + 001–050 renders, no column is
  named `x` or `y`, 078 copies with `CREATE TABLE … AS TABLE`; the two `%I` bind positionally. The four
  arms are killed by TWO clauses (insert and delete by the count, update and column-added by the
  content), which is why the battery's `-k '… and update'` kills two arms at once; the "not present to
  compare" clause is reachable only with a stray relation compensating a missing table — defensive,
  untested, fine.
- **080's guard — CLOSED.** The ledger exists before the DO block runs (`_ensure_ledger` first, and the
  below check needs 001–078 recorded); `adopted` is rightly excluded — `adopt` would record 079
  wherever its postconditions already hold, the by-hand drop the guard exists to refuse; `repaired` is
  an operator's status, not proof (`repair` flips any row the operator names).
- **The roleid-side gate line — CLOSED, with a caveat.** On 15 the assertion is exact (five rows, all
  exempt, and `_memberships_of` asserts they exist); on 17 the auto-grants are exempt by
  `admin_option`. The caveat: an explicit `GRANT svc_x TO <owner> WITH ADMIN OPTION` was exempt too,
  indistinguishable from the auto-grant. The battery's plain `GRANT svc_worker TO current_user` is a
  real kill on both versions.
- **The runbook — PARTLY.** The mechanism is fail-closed, measured in bash and zsh: `URL=$(…
  "${NAME:?}" …)` with `NAME` unset kills the SUBSHELL and the parent continues with `URL` empty, which
  the `case` guard's `""` arm refuses (unset, exit); the comment said `${NAME:?}` stops the shell — false
  for that line — and `exit 1` closes an interactive shell. The host guard measured: a branch endpoint
  passes, production's is refused (its id is the repository's own record, `scripts/observed_use.py`),
  an `@` in a password fails closed, a non-Neon host is refused; no path to production constructible.
  `railway redeploy`'s flags as claimed (`-s`, `--yes`, no `--environment`); the worker carries
  `DATABASE_URL` by `railway.toml:14-16`; step 4's `TARGET_DATABASE_URL` is a harmless inconsistency.
- **The battery — PARTLY at `bb0175a`.** Parsed by the shell with `check()` overridden: 31 checks,
  every `-k` collects, 30 anchors exactly once — and mutation 25's anchor stale (old = 0) since the
  079-row clause. No equivalent mutant: mutation 2's loop uses `applied_head`, defined before its
  anchor; mutation 8's 8-space anchor cannot match the 12-space copy; the BOM mutation is real
  (`str.strip` does not strip U+FEFF, `\s` does not match it). Already closed on the tree before the
  report: `5e39a53` re-anchored it and `bc2cc4a` gave the legacy clause its own test; `ran 31 of 31`,
  31 killed, on `bc2cc4a`.
- **Regressions and prose — PARTLY.** 70 passed without a database (one error: a test that needs it).
  Three counts were pre-fold: the CHANGELOG's "the drop and its two refusals, the stand-down and its
  refusal", the gate test's docstring, and this ledger's "twelve" gate lines (080 prints eleven; the
  runbook had it right).

Nine findings: one gap (the anchor — closed before the report), one low gap (the ADMIN exemption),
seven observations (the NUL file; `-- runner-manual` and `-- runner=manual` as prose; the runbook's
explanation; `repaired` in 080's header; the prose; CASCADE's dependents OUTSIDE the schema — a
`pg_depend` precheck suggested; a wrong `python` under `railway run` fails loudly, never dangerously).

**Fold 3** (`2ade589`), every finding taken, with the class sweeps:

- **079 refuses an outside dependent.** Any dependent of a legacy relation, row type or function whose
  own schema is not `legacy` — a view, a foreign key, a default, a function signature: what CASCADE
  would take with the schema, unseen — refuses the drop, naming it; an object class the query does not
  know counts (a refusal to read, never a drop). Measured in production the same day with the query as
  written: **0** outside dependents, **294** in-schema dependency rows excluded (the arms do real work);
  the wider probe beside it: 16 tables + 77 indexes + uuid-ossp in `legacy`, 0 user triggers, 0 views
  anywhere mentioning `legacy.`, 0 foreign keys into it, 0 row-type dependents. The test plants a
  `public` view over a legacy table: "outside legacy … v_over_legacy", `legacy` and the view intact,
  the ledger untouched. The mutation drops the clause; killed.
- **The runner refuses a NUL-bearing file at discovery, by name**, beside the not-UTF-8 refusal (test:
  UTF-16-LE without its mark → "NUL"; mutation: the check → `if False`, killed). **The near-miss rule
  reads a dash or `=` for the colon** (`[:=\-]?`; two more parametrized spellings; the mutation narrows
  it back to `:?`, killed). `sorted(KNOWN_MARKERS and _KNOWN_WORDS)` → `sorted(_KNOWN_WORDS)`.
- **The gate line is exact on its grantor.** The auto-grant arm is `m.admin_option AND m.grantor = 10`
  — the bootstrap superuser, oid 10 on every cluster; measured in production: oid 10 is `cloud_admin`,
  every auto-grant row's grantor is `cloud_admin`, the bootstrap's explicit `svc_migration` grant is
  `admin = f, grantor = neondb_owner`. Class sweep of the predicate: four copies (080's comment, the
  test's assert, the runbook's rehearsal and production gates) — `grep -c "m.admin_option AND
  m.grantor = 10"` → 1, 1, 2; all changed. The mutation plants `GRANT svc_worker TO <owner> WITH ADMIN
  OPTION` in 080 (its grantor is the owner on 15 and 17); killed.
- **080's header** says what `repaired` means to its guard: a status only `repair` writes, over a row
  the operator names — a deliberate act.
- **The runbook.** The rehearsal block is saved and run with `bash -eu rehearse.sh` (a failed
  substitution ends the script; the connection string never enters an interactive shell); the comment
  says what closes an unset name; the guard "ends the script". Class sweep of "stops the shell": one
  hit, fixed (round 1's entry above keeps its wording as history).
- **Prose.** Sweep of "two refusals" / "its refusal" / "twelve lines" / "admin or not" across the tree:
  the CHANGELOG entry (both), the gate test's docstring (both), this ledger's build entry ("eleven"
  now), the test's gate comment — four sites, all fixed; every other "twelve" in the tree is an older
  entry's or an investigation's.
- **Declined:** nothing this round.

**Verification on `2ade589`** (2026-09-18, the worktree, no variable of either tier set):

- The gate (20), the runner's suites (57), the lane (14), the ratchet (26), the doc pins (11) and the
  deploy guardrails (3) under the Docker test database (PG 15): **131 passed**.
- `ruff format --check` / `ruff check`: clean.
- Battery `tests/mutations/legacy_tear_out_04.sh` on the committed tree, in its own worktree:
  **`ran 35 of 35`, 35 killed** — no SURVIVED, NOT APPLIED, NO TEST SELECTED or KILLED BY ERROR.
- The whole suite against the test database: **3710 passed, 1 skipped** (44 deselected: the local egress-floor deselect and CI's five).
- CI: **green at `2ade589`** — run 35382101176, `3749 passed, 1 skipped, 5 deselected`; every check green (Lint, Test, Changelog Check, Security Scan, Front End, Vercel). (On `bc2cc4a`, before the fold, two attempts each failed ONE load-sensitive test the
  phase does not touch — `test_w2_transport_gate.py::…delivered_by_the_live_worker` asserting
  `'superseded' == 'sent'`, then `test_l8_webhook_admission.py::…200_distinct_updates…`, which had
  failed on `main` at `53ca6d6` on 2026-09-16 — with 3,744 passed each time; the two commits after the
  green run at `bb0175a` changed only the gate test and the battery.)

**A third lens, scoped to the fold** (`git diff bc2cc4a 2ade589`; read-only, no database): all eight
of round 2's findings CLOSED on the tree. What it executed: the near-miss regex over the real corpus —
82 files, 131 matching lines, every one a real marker, **0 would-refuse** — and over a synthetic one
(the two new spellings refuse; a bare `-- runner:`, `-- runners:manual`, a mid-sentence mention,
`-- runner-side check` and the door's own command line stay prose); `discover_migrations` → 80 files,
manual = [79, 80]; 15 no-database pins green; the battery parsed with `check()` overridden — 35 anchors
exactly once, `EXPECTED` = 35, every `-k` collects (nine for the near-miss parametrization). What it
proved by reading: the NUL guard sits after the only decode and before the only parse, split and
`Migration(` construction; the gate predicate is byte-identical in its four copies; the creator
auto-grant's grantor is `BOOTSTRAP_SUPERUSERID` by PostgreSQL's own source (`CreateRole()` →
`AddRoleMems(…, BOOTSTRAP_SUPERUSERID, …)`, admin true, set false; `pg_authid.dat`, oid 10), so the arm
is exact on 16+ and inert on 15, and the battery's `WITH ADMIN OPTION` plant lands with grantor = the
owner on both; under `bash -eu` an unset `NAME` ends the script at the assignment (measured), and in a
bare shell the `case` guard refuses the empty host; every prose count true (four drop-refusal tests,
the second in four ways; three stand-down refusals; eleven gate lines printed and eleven asserted).
The precheck class by class — defaults, constraints, indexes, triggers, rules, row types, array
types, sequences: each placed by an arm or filtered by `deptype`; a comment records no dependency; a
function BODY naming `legacy.x` records none either (CASCADE does not take it; the header scopes its
claim to signatures). **Findings:** one gap — no arm for a policy, an extended-statistics object or a
publication membership ON a legacy table, so an in-schema object of those kinds would be counted AND
described as "outside legacy" (fail-closed; the lineage holds none) — and five observations: the new
query never executed inside the lens; `-- runner-manual …` now refuses as the OPENING of a prose
comment; `adopt`-then-`repair` turns an `adopted` 079 row into one 080's guard accepts (two deliberate
acts, each with a written reason; no change proposed); the host guard hard-codes production's
endpoint id; the function-body case. **Verdict: nothing is a blocker; ready to merge once CI's gate
run is green.**

**Fold 4** (`6ab4253`): three arms place a policy, a statistics object and a publication membership,
and the refusal says "outside legacy — or of a kind this check cannot place in a schema"; 079's header
states the function-body case; `migration-runner.md` binds prose to the near-miss rule (a comment
never opens with `runner` and a marker word, in any spelling); the runbook names where the guard's
endpoint id comes from (`scripts/observed_use.py`, `EXPECTED_HOST`) and when to fix the arm. The
lens's "never executed" is answered by this session's runs: the precheck lifted VERBATIM from the
file, read-only in production → **0** (before the fold: 0, with 294 in-schema rows excluded); measured
beside it, every one 0: function bodies outside `legacy` naming `legacy.` or `uuid_generate`, view and
materialized-view definitions naming it, policies / statistics / publication memberships on legacy
tables, defaults outside it calling uuid-ossp, role or database `search_path` settings naming it.
NOT re-lensed — the diff is three CASE arms, one message and two documentation sentences, and its
verification is the gate, the battery and CI below. **Declined, with reasons:** a live endpoint
comparison in the rehearsal (this `neonctl` shows a branch's host only through its connection string,
and the rehearsal should never hold production's; the name guard and `bash -eu` stand in front of the
host guard); a status check inside `repair` (pre-existing, every file's; two deliberate acts with
written reasons).

**Verification on `6ab4253`:** the gate (20), the runner's suites (57), the lane (14), the ratchet
(26), the doc pins (11), the deploy guardrails (3) and the legacy-CLI pins (4) under the Docker test
database: **135 passed**. Battery on the committed tree, in its own worktree: **`ran 35 of 35`, 35
killed**. CI: **green at `6ab4253`** — run 35384235627, `3749 passed, 1 skipped, 5 deselected`; every check green.

**Convergence:** round 1 — one blocker, four risks, five gaps; round 2 — one gap (closed before the
report), one low gap, seven observations; round 3 — one gap on a fail-closed path, five observations,
"ready to merge". Each round closed everything the one before it found; none reopened a closed
finding.


## Phase 04 — merged

**Merged by the owner at 19:44 UTC on 2026-09-19 as `c8482b3`** (the admin squash, the prepared subject
and body; no issue closed by keyword), after this session's own merge was refused by its permission
classifier. **The deploy owed the pair and applied nothing — measured, not assumed:** the worker's
deployment `317826ff…` of `c8482b3` is `SUCCESS`, and its predeploy log reads

```
owed (manual) 079 (079_drop_legacy_schema.sql)
owed (manual) 080 (080_window_stand_down.sql)
0 applied
2 owed (manual): waiting for an operator's `apply --manual <version>`; a deploy never runs them
```

The read-only production probe after it: the ledger's head is 78 with 78 rows and no row for 79 or
80; `legacy` still holds its 16 tables; `archive` its 16 snapshots. The API service's deployment of
`c8482b3` waited on `main`'s CI, as every API deploy does (phase 02's finding), and landed `SUCCESS`
at 19:53 UTC once that run went green — **both services run the merge**. That is the epic's checklist
clause 5, agent-run half: DONE.

**The invariant registry on `main` at `c8482b3`:** I2 — 40 passed, the normative pin 35; I3 — 49
passed; I4 — 4 / 0 / 0 / 0; I5 and I8 — the runner's suites, the window gate and the deploy
guardrails 80 passed, doctor's 21; I6 — 11 passed (phase 05's wider pin arrives with #1322); I7 — 3
passed. I1 — `main`'s CI run on the merge: green. **All eight hold on `main` at `c8482b3`.**

**Phase 05 rebased** the same hour: `git rebase --onto origin/main 29537a8` — seven commits moved
clean; `git diff origin/main..HEAD --stat` shows 51 files, every one a document, a rule, the ledger, the
CHANGELOG, the two pin tests or the battery. The declared deviation is discharged; #1322 is a pull
request against `main` with phase 05's scope alone.

**The rehearsal — GREEN, run by the owner on 2026-09-19 at 19:48 UTC** (`bash -eu rehearse04.sh` from the
checkout at `29537a8`, clean; 079 `a9e3cff0…`, 080 `bf9a15d6…`), on the branch
`claude/window-rehearsal-20260919-1948` (`br-solitary-paper-aibcw8jf`, endpoint `ep-late-salad-ai7po457`),
its timeline `41249fbafba987577c059d4cd003558c` measured not to be production's before anything ran:

- Before: PostgreSQL 17.11 as `neondb_owner`; the ledger 78 / 78; `legacy` 16 tables + 77 indexes; 16
  snapshots; no `window_ddl`; `svc_migration` holds `CREATE ON DATABASE`; uuid-ossp present; the
  database 82,763,776 bytes. `status`: 077 and 078 applied, 079 and 080 owed.
- **079 applied in 1.594 s wall-clock; 080 in 0.501 s.**
- The gate: `0, 0, t, t, 16, t, neondb_owner` — every line as 080 prints it.
- F8's positive control: 076's bracketed hand-off landed for `svc_clock` and for `svc_maintenance`;
  the function's owner after it `svc_maintenance`.
- A plain `apply` afterwards: `0 applied`.
- After: the ledger's tail 78, 79, 80 `applied` by `neondb_owner`; the sixteen snapshots hold 30,132
  rows in all; uuid-ossp gone and `gen_random_uuid()` answering; `public.jobs` present; the database
  38,936,576 bytes — **43.8 MB smaller**.

**One finding, fixed in the runbook before the production window:** `neonctl branches create` PRINTS the
new branch's connection string in its default output — the same role password as production's. The
harness redacted it; the runbook's step 1 and production's step 3 (the marker branch) as printed would
have echoed it. Both lines now pass through the redaction.

## Phase 04 — #1202 closed; the merge and the rehearsal refused by the session's permissions

**2026-09-18, on the owner's instructions in chat** ("you can close it"; "yes sure please do this" for
the merge; "you have full prod permissions to manage this" for the window): #1202 was CLOSED with the
ruling of 2026-09-16 as the plan records it — its own words ("either leg satisfies it: record the Track 3
videos, **or** arm and verify the target tier"), the "or" leg met, what guards 3g today and what that
guard is not. The owner's verbatim ruling is not in the transcripts as quotable text, so the comment
cites the plan and the ledger and says who posted it and on whose instruction.

The two actions after it were REFUSED by the session's permission classifier, not by the owner and not
by the runner, and were not worked around: the admin squash merge of #1321 ("Merge Without Review") and
the window's rehearsal script against a scratch Neon branch ("Production Deploy"). A grant in chat does
not change what the harness allows. Both commands were handed to the owner as printed (the merge with
its subject and the squash body at `scratchpad/t04_squash.md`; the rehearsal as `bash -eu rehearse04.sh`
from the phase-04 checkout — the runbook's block as printed plus a redaction on every output, a second
provenance gate on the branch's Neon timeline, and before/after facts). The same limit will meet the
production window's commands. Owner-queued, first in order.

## Phase 05 — the documentation's end state

**Topology — a declared deviation.** The ledger's rule is no stacking. Phase 05 is built on
`tear-out/04-drop-and-stand-down`'s head (`524998a`) instead of `main`, because #1321's merge is refused
to this session and eight of 05's files are files 04 also edits (the never-run satellites, `AGENTS.md`,
`CLAUDE.md`, `migration-runner.md`, the CHANGELOG, this ledger, `tests/test_agent_docs.py`). The parent
is final — reviewed over three rounds, green, nothing pending on it — and the squash's tree is
`524998a`'s tree, so the planned move is mechanical: `git rebase --onto origin/main 524998a` the moment
#1321 merges, then `git diff origin/main..HEAD --stat` must show phase 05's scope only. The PR stays a
draft until that rebase.

**Measured before building** (the plan's step-1 grep, on the phase-04 tree, 2026-09-18): **37 documents**,
not the dozen the plan's Evidence lists. Read, not counted: `media_items`, `users`,
`category_post_case_mix` and `onboarding_sessions` are TARGET tables too — derived from the target's own
metadata (26 tables, `src/models/target/`), not listed.

**Premise findings.** (1) The plan says "`audit_log`, `media_items` and `users` are also TARGET names":
`audit_log` is not — the target's audit table is `audit_events`; `audit_log` is legacy-only, and the two
reused names the plan did not list are `category_post_case_mix` and `onboarding_sessions`. The pin
derives the set, so the plan's sentence cannot mislead it. (2) The plan's step-1 pattern has no leading
word boundary on `TELEGRAM_BOT_TOKEN`, so it counts `TARGET_TELEGRAM_BOT_TOKEN` — the target's own, live
variable — in four guides and the README; the pin uses boundaries on both sides. (3) A bare
`TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_CHAT_ID` ARE still read by something: the landing app on Vercel
(`landing/src/lib/telegram.ts:1-2`) — its guide keeps them, as a reasoned exemption. (4) `src/main.py`
is NOT a deleted path: it stays as the Procfile's dispatch to `src.worker` (fork F2). (5) The checklist's
first line ("the step-1 grep → only archive, CHANGELOG, planning") and step 1's own "exempt (with why)"
disagree; this phase reports every file with its row and treats a reasoned exemption as the plan's own
option. (6) Six documents in the population are HISTORY shelved under live directories — three dated
update notes of January 2026, the May 2026 Telegram postmortem, the July Cloudinary gap analysis, and a
top-level `planning/multi-account-dashboard.md`: archived by the repository's convention (`git mv`, a
status banner, an index row), with the two links the moves affect repaired.

**Red first.** The pin went into `tests/test_agent_docs.py` before any page changed — the legacy-only
tables (the inventory minus the names the target's metadata reuses), the deleted paths (checked to BE
gone), phase 02's dead-variable list (one home each), a positive control and an exemption-staleness
test — and ran on the base: **24 of 43 live pages red**, about 95 mentions, the safety block's
`posting_history` line in `CLAUDE.md` and `AGENTS.md` among them.

**Built** (`b56b8ed`, on phase 04's head `29537a8`; four documentation agents on disjoint file groups in
one worktree, each under one brief — describe what the code DOES with `path:line`, never touch the
never-run fence, report what could not be verified — and the coordinator on the history-shaped files,
the pin, the battery and the plans):

- **The pin** (`tests/test_agent_docs.py`): legacy-only tables = the inventory minus the names the
  target's metadata reuses (`category_post_case_mix`, `media_items`, `onboarding_sessions`, `users` —
  pinned as today's value of the derivation); the deleted paths, checked to BE gone; phase 02's
  `DEAD_VARIABLES`; a positive control (a planted page trips each arm; a target name and a snapshot's
  name do not); a roots-coverage test; an exemption-staleness test. **Two reasoned exemptions:** the
  landing app's own `TELEGRAM_BOT_TOKEN` / `ADMIN_TELEGRAM_CHAT_ID` on Vercel
  (`landing/src/lib/telegram.ts:1-2`), and `posting-monitor.md`'s `TELEGRAM_BOT_TOKEN` — the environment
  of `tg-post.sh`, the fleet host's pager script outside this repository.
- **Rewritten against the code:** the five `.claude/rules/` files (`testing.md` too — it taught the
  legacy `service.repo` mock and cited a test that does not exist), `PROJECT_CONTEXT.md` and
  `QUICK_REFERENCE.md` (their never-run bullets byte-identical), the two slash commands onto real
  `storydump` read verbs, the neon-analyst's reference, `AGENTS.md` (one tier, one sentence of history),
  `CLAUDE.md` (the production line onto the ledger — `post_intents`, `jobs`, `channel_outbox` "and every
  other table `src/models/target/` declares" — in both documents; the rules table), `README.md`, the
  documentation index (92 links, 0 dead), eight runbooks and nine guides.
- **Archived, not rewritten** (`git mv`, a status banner, an index row): the three update notes of
  January 2026, the May Telegram postmortem, the July Cloudinary gap analysis, the legacy multi-account
  dashboard plan, and the January security review — every finding of which names legacy code (five of
  its six source files are gone). `documentation/updates/` and `documentation/cloudinary/` and the
  top-level `planning/` are empty and gone from the tree; `tests/test_legacy_cli_gone.py`'s HISTORY no
  longer lists the first.
- **The plans:** `04-execution-sequence.md`'s M.3 line and the consolidated plan's Live status say what
  is true today — 3f applied as 078 on 2026-09-18; 3g and step 8 are the gated files 079 and 080, owed
  by every deploy, the owner's to apply. `meta-app-review.md`'s standing constraint is "discharged"
  (its heading renamed with the key `tests/test_meta_runbook_markers.py` pins).
- **What the agents corrected beyond names** (each verified in the tree): the fresh-database sequence in
  both deployment guides omitted `tests/scripts/fixtures/legacy_by_hand.sql`, so the runner would fail
  at 078; the Google client needs BOTH redirect URIs; `railway restart` (attested nowhere) →
  `railway redeploy --service … --yes`; the bot token is set on BOTH services (the API reads it too);
  `/start inv-…` is NOT a served lane (`build_router` registers `link-` and `bind-` only); media kind is
  decided by MIME type, not suffix; CORS reads `WEB_APP_URL`; 26 commands, not 25; `ROLE_FLOOR`, not
  `FLOORS`; the policy test lives at `tests/test_integration_coverage_policy.py`; the gates under
  `tests/scripts/` ERROR without PostgreSQL rather than skip; the landing app reads neither
  `JWT_SECRET` nor `NEXT_PUBLIC_SITE_URL` and its Telegram Login Widget is gone. Removed as
  unverifiable: per-test lists, "56% coverage", cost and timing claims, the legacy dump cron, the
  phase roadmap.
- **A phase-04 defect the pass found, fixed on phase 04's branch before its merge** (`29537a8`):
  `storydump doctor` called any checkout file the ledger lacks "not applied — deploy main" and exited
  4 — which 079 and 080 are BY DESIGN from the merge until the window. doctor reads the `manual`
  directive now (four tests red first, two battery mutations; the battery `ran 37 of 37`; CI green,
  3,753 passed).

**Verification** (2026-09-18, `c50e395`): the pin and the pins beside it green; `ruff` clean; every
relative link on the 56 live and changed pages resolves; battery `tests/mutations/legacy_tear_out_05.sh`
on the committed tree in its own worktree — `ran 15 of 15`, 15 killed; the whole suite against the test
database — `3720 passed, 1 skipped`; CI on the draft (#1322, merged with `main`) — green, run
35402026360, `3759 passed, 1 skipped`.

**Review round 1** (two lenses on `c50e395`, read-only). The adversarial lens checked about 178 claims
against the tree — 93 citations across the five rules, 70 across the runbooks, the safety surface by
hand — and could not falsify ONE cited behavioural claim; the never-run fence is byte-identical in both
documents and unchanged from the parent; both slash commands name read verbs only. "Every false
sentence I found is a sentence written WITHOUT a `path:line`." The structural lens: the pin is the right
shape, the scope is clean (no code outside `tests/`), the archive moves follow the convention. What they
found, folded:

- **RISK (adversarial): three pages documented a bug the parent commit had fixed.** `db-status.md`,
  `troubleshooting.md` and `worker-recovery.md` told the reader `storydump doctor` calls 079/080 "not
  applied — deploy main"; true when the agents wrote it, false since `29537a8`. Class sweep — every live
  sentence about doctor and the gated files (`grep -rni doctor … | grep -i "gated|not applied|owed|079"`):
  four hits; the three corrected with the code cited (`env.py`, `_gated_in`), the fourth
  (`migration-runner.md`) already true.
- **RISK (adversarial): a pasteable block ended in a never-run command.** `telegram-webhook.md`'s
  diagnostic fence ended with `storydump webhook register --drop-pending`, guarded by a trailing comment
  (it predates this phase). Lifted out: the pasteable block only reads; arming the bot is its own
  block, named the owner's.
- **GAP (both): the pin was narrower than its comment.** A table in capitals (`POSTING_HISTORY` in a SQL
  example), a deleted module in its dotted spelling (`src.services.core`) and `documentation/*.md`
  passed unseen; the deleted paths were a hand-copy of phase 01's lists, already missing
  `src/services/domain` and every deleted FILE; one assertion could not fail, and the `archive` filter
  read ABSOLUTE parts — a checkout under a directory named `archive` would have had no live pages and
  a pin passing over nothing. Now: tables case-insensitive (variables NOT — `workspaces.dry_run_mode` is
  a live column), paths matched with `[/.]`, `documentation/`'s own pages a root, the paths imported
  from `tests/src/test_legacy_tier_gone.py` (its redundant twin test deleted here), the filter on
  relative parts with a real test of both directions. No new offender under the wider predicate.
- **GAP (adversarial): an exemption covered every future mention of its name on the page.** Each
  exemption now pins the COUNT of mentions it was read for (3, 2, 2): one more — a stale line about the
  worker's token hiding beside the pager's — or one fewer fails and the page is read again.
- **GAP (adversarial): the backup page had traded a documented 30-day copy for a documented 24-hour
  window without saying so.** The rewrite dropped the scheduled off-Neon dump and the partial-restore
  commands because the old script's `BACKUP_DIR` is a dead APPLICATION variable — the pin matched an
  operator's own shell variable. Restored with the script's own names, framed honestly (nothing in the
  tree schedules it; whether the owner's host still does is not something the tree can say), and the
  restore into a SCRATCH database only.
- **GAP/observations (adversarial):** `database.md` cited a function as a manifest key; `testing.md`
  said a warning fails the test without the three classes `pytest.ini` ignores; four citations off by
  one; `resume` and `posted` offered as fixes without the owner's-decision caution their neighbour
  `cancel` carried — all corrected.
- **Simplify (structural) — one home each:** the production read recipe stood in four pages in two
  spellings (`--service worker` and `--service storydump`, with and without `--environment
  production`): it lives in `reading-the-ledger.md` › The escape hatch now, with links; the four-file
  fresh-database sequence, duplicated across both deployment guides (the agents had fixed the same
  omission in both), lives in `cloud-deployment.md`; the Docker gate recipe lives in `AGENTS.md` ›
  Testing, as the repository's rule says shared guidance does. `.claude/rules/database.md` loaded 172
  lines for every one of 64 service files: its migration rules are their own rule now
  (`.claude/rules/migrations.md`, loaded for `scripts/migrations/**` only), and `CLAUDE.md`'s table
  lists it. The documentation index's counts re-measured (archive 49) and its two rows for directories
  this PR deleted removed.
- **Battery**: rewritten for the pin's new shape — 21 mutations (the three arms, the boundary, the
  case fold, the dotted spelling, a live column read as a dead variable, the derivation, both roots,
  the exemptions honoured, an unused exemption, a stale mention beside an exempt one, the archive
  filter in both directions, six legacy names planted back into live pages); an interrupt no longer
  leaves a planted page behind.
- **Declined, with reasons:** a required-substring form of exemption (the count does the same work
  and is simpler to read); `.github/**`, `landing/**` and non-Markdown files as roots (code and config
  comments are the owner queue's — a documentation pin that reads YAML comments is a different
  instrument); `test_the_names_both_tiers_use_are_not_legacy_names` stays — it is the only test that
  fires when the target grows a table with a legacy name.
- **Queued for the owner, from the lenses:** `src/services/target/webhook_ingress.py`'s docstring
  ("the legacy adapter already gets this right") beside the code comments already queued; the
  post-window pass over about ten sentences ("is dropped by 079 in the owner's window", the three
  doctor sentences, the never-run line's comment) belongs on the window's own checklist.

**Verification on `63714e9`** (the fold of round 1): the pin and the pins beside it — 134 passed; links
and anchors on every live and changed page — 0 dead; battery `ran 21 of 21`, 21 killed, on the committed
tree; CI green — run 35403440369, `3759 passed, 1 skipped`.

**Re-verify after the fold** (a fresh lens on `63714e9`, read-only): every claim it tried to falsify
held — the three doctor sentences match `env.py`'s `missing` / `owed` split and the class is swept; the
hardened pin has no false positive constructible from the target's metadata (zero target tables or
COLUMNS collide case-insensitively with the twelve legacy-only tables) or from any tracked path (all 33
path patterns against `git ls-files` and every tracked module's dotted spelling: zero hits); the `ROOT`
monkeypatch does not leak (the exemption test runs after it and still reads 3/2/2 off the real tree);
the battery is internally consistent (21 = 21, every anchor once, every selector collects); the rule
split lost no migration rule and a migration author still gets the RLS line; nothing outside the
phase's scope in the diff. **Verdict: ready to merge.** Three gaps and five observations, folded in the
commit after this entry:

- **GAP: the restored cron recipe could not run.** `railway run` resolves the linked project from the
  working directory and cron starts in `$HOME` (the repository's own record of that gotcha is the
  neon-analyst's reference); the script had no shebang. Both added. The `sh -c` quoting was checked and
  is right: the inner shell expands `$DATABASE_URL`, the outer one `$DUMP_DIR` and the date.
- **GAP: "one home" was not true.** The neon-analyst's reference kept its own spelling of the
  production read recipe (`--service storydump`, `-f "$0"`) beside a pointer. Its fence is the pointer
  now; the old spelling is one sentence of history on that page. (`backup-restore.md`'s read as the
  OWNER login is a different door — the window's gate uses it — and stays.)
- **GAP: a counted exemption could still rot** — a count of 0, or a name in no list, passed. The check
  is a function now (`_exemption_errors`) with a unit test of every direction on a page of its own:
  exact, one more, one fewer, zero, a name that is no legacy name, a page that is gone.
- **Observations:** the deleted-path pattern gained a trailing boundary (`src/services/domain` is not
  a future `src/services/domain_events.py`; zero false positives today either way); the `register`
  fence got its in-fence warning back; `deployment.md` names `apply` on the page again; the pin's
  unmutated behaviours the lens listed have mutations — the `.py` suffix, `rglob` on the nested roots,
  `glob` on the flat one, the exemption's one-fewer direction and both guards. Pre-existing and left:
  a dead link inside `CHANGELOG.md` (history) to a plan directory archived before this epic.
- **Not re-lensed:** the fold is two guards, one lookahead, a shebang and a `cd`, three sentences and
  seven battery lines; its verification is the battery and CI below.

**Verification on `82c55b3`** (the fold of the re-verify, and one commit after it): the battery on
`99d7081` `ran 28 of 28` with **two SURVIVORS** — the two new guards of `_exemption_errors` (a name in
no list; a count below one) could be removed and the new unit test still passed, because its
scenarios failed the COUNT comparison anyway. The test was wrong, not the guard: the page now names
the unknown name once, and the zero-count case uses a legacy name the page does not carry, so each
guard is the only thing between its case and a pass. On `82c55b3`: **`ran 28 of 28`, 28 killed**; the
pin and the pins beside it green (18 in `tests/test_agent_docs.py`); CI green — run 35404263398,
`3760 passed, 1 skipped`.

**After the rebase onto `main`** (#1321 merged): CI green on the rebased head `00f10f6` (run 35465461764) and on
the current head `165d588` (run 35465736580) — `3760 passed, 1 skipped` each; #1322 marked READY on
2026-09-19; its merge is the owner's.

**Convergence:** round 1 — two risks, four gaps, a simplify list, observations; the re-verify — three
gaps, five observations, "ready to merge"; each round closed everything the one before it found, none
reopened a closed finding. **Status: READY as a change; a DRAFT as a pull request** until #1321 merges
and this branch is rebased onto `main` (`git rebase --onto origin/main 29537a8`) — and its own merge,
like #1321's, is refused to this session by the permission classifier: the owner's.

**Window-dependent sentences, to update once the window has run** (each is true today and says so):
the documentation index's tear-out row ("the owner's window has not run"), the consolidated plan's Live
status and M.3 line, `meta-app-review.md`'s discharged-constraint section, the M.2 rehearsal spec's
status line, `00_EPIC.md`'s `status:` and its goal condition.

## Phase 05 — merged

**Merged by the owner at 21:06 UTC on 2026-09-19 as `3c8efd4`** (the admin squash, the prepared subject
and body; no issue closed by keyword). Both services deployed it: the worker `SUCCESS` at once, the
API `SUCCESS` at 21:16 UTC once `main`'s CI on the merge went green (the phase-02 finding, again). The
worker's predeploy owed 079 and 080 and applied nothing, as with phase 04.

**The invariant registry on `main` at `3c8efd4` — after the final phase, all eight:** I1 — `main`'s CI on
the merge green; I2 — 40 passed, the normative pin 35; I3 — 49 passed; I4 — 4 / 0 / 0 / 0; I5 and I8 — 80
passed; I6 — 27 passed (the wider pin, the legacy-CLI pin and the App Review markers); I7 — 3 passed.

The five phases are on `main`; the worktrees and branches of 04 and 05 are removed. The window ran the
same evening (the entry below).

## The window — run by the owner on 2026-09-19

**Two attempts, one script.** The runbook's steps 0–8 as one script (`scratchpad/window04.sh`: the rehearsal
harness's discipline — every check before any change, every output redacted, the applies timed, the
gate compared to what 080 prints), typed by the owner at 21:2x UTC from the checkout at `3c8efd4`,
clean, both services `SUCCESS` there (079 `a9e3cff0…`, 080 `bf9a15d6…`). The first attempt stopped at
step 1: `railway down` timed out against Railway's API — and had taken effect: the worker read
`REMOVING`, then `REMOVED`, with nothing yet changed in the database. The script was adapted to accept
an already-stopped worker and typed again at 21:34 UTC.

**What ran** (the log at `scratchpad/window04.log`; every line below is from it or from the read-only
probe after it):

- The marker: `pre-3g-20260919-2134` (`br-round-mud-aikp3w1c`, endpoint `ep-twilight-boat-ais65dz9`),
  created at 21:34:04 UTC, its connection string redacted. **It stays until the worker has run a day.**
- Before: the ledger 78 / 78, 079 and 080 owed; `legacy` 16 tables; 16 snapshots; no `window_ddl`;
  `svc_migration` holding `CREATE ON DATABASE`; the database 82,788,352 bytes.
- **079 applied in 1.341 s wall-clock; 080 in 1.019 s** (21:34:07 UTC by the ledger's own clock).
- The gate, as the runner's login: `0, 0, t, t, {svc_claim,svc_clock,svc_maintenance,svc_membership},
  16, t, neondb_owner, 80|80` — **GREEN, every line as 080 prints it.**
- After: the ledger's tail 78, 79, 80 `applied` by `neondb_owner`; uuid-ossp gone; `gen_random_uuid()`
  answering; `public.jobs` present; the database 38,961,152 bytes — **43.8 MB smaller**; the schemas
  left: `archive`, `public`, `runner`; the sixteen snapshots 9,064 kB.

**The restart went wrong, and was put right in four minutes.** Step 8's `railway redeploy --service
worker --yes` neither refused nor re-ran the removed deployment: it re-ran an OLD one — `3d94cd2`, a
commit of 2026-09-03 — and the worker came back `SUCCESS` on stale code. The script's last check saw
the wrong commit and stopped with a "worker still down" message that was itself wrong: the worker was
up. Its log showed why nothing worse happened: `WORKER_IMPL=target` is still set on the service (an
owner-queue item never done), so the old entrypoint dispatched to the target root, elected the clock
and served; had the variable been removed, that commit's default was the legacy scheduler. The way
back was the runbook's own fallback, an empty commit to `main` (`3d54b72`, pushed 21:37 UTC under the
owner's bypass): the worker deployed it at 21:39 UTC — its predeploy `0 applied`, nothing owed, the
current worker up with its five lanes — and the API's deployment waited on `main`'s CI as every API
deploy does, landing `SUCCESS` at 21:49 UTC — both services on `main`'s head, `storydump health` ok. The runbook's step 8 now says never `railway redeploy` after a `down`, and its backout
uses the same push; step 1 records the timeout.

**The goal condition — #1216's acceptance list through the epic's checklist — is MET**, every clause
ticked in `00_EPIC.md` with its evidence: A1 by the AST guard and the reachability probe; A2 by the
window above and the read-only probe after it (`legacy` gone, the sixteen snapshots present, 079/080
`applied`); A3 moot since the legacy CLI's deletion; A4 by every phase's suite, green with the legacy
tests deleted, not skipped. The epic's `status:` is `completed`.

## (superseded) The sprint's state at the end of 2026-09-18 — kept as the dated snapshot; the window entry above is the outcome

**Phase rows (updated 2026-09-19).** 01, 02, 03, 04, 05 — DONE and live: 04 merged `c8482b3` and 05 merged
`3c8efd4`, both by the owner's hand after this session's admin merges were refused by its permission
classifier; both services deployed each. The rehearsal — DONE, green, run by the owner. The window —
the owner's (F7), still owed.

**The invariant registry, run PRE-MERGE on phase 05's head** (`82c55b3`, which holds phases 04 and 05; the
registry's own rule is after every merge, so this run is to be repeated after each of the two): I1 —
CI `3760 passed, 1 skipped`, the one skip the ceiling allows; I2 — `test_advertised_ddl.py` +
`test_lineage_lane.py` 40 passed, the normative pin still 35; I3 — `test_legacy_tier_gone.py` 49
passed; I4 — `telegram_ratchet.py` 4 / 0 / 0 / 0; I5 and I8 — the runner's suites, the window gate and
the deploy guardrails 80 passed (a manual file is owed and wedges nothing; every marker known); I6 —
`test_agent_docs.py` 18 passed; I7 — `test_worker_entrypoint.py` 3 passed, `target_reachability.py` ran.

**The goal condition — #1216's acceptance list through the epic's checklist — is NOT met yet, and what
is left is the owner's.** Agent-run clauses: (1) the import grep is unmet BY DESIGN, as phase 01's entry
records — today's hits are four provenance docstrings in `src`, two scripts and the guards themselves;
the AST predicate is the standing guard and is green; (2) the three entrypoints import with no
`TELEGRAM_*` variable — measured today on this head; (3) the ratchet passes with its core segment empty;
(4) the suite green at every phase, nothing newly skipped — CI's counts 3,653 → 3,680 → 3,700 → 3,753 →
3,760; (5) **BLOCKED** — `status` owing 079/080 on a checkout at phase 04's merge, and a predeploy log
naming them, need that merge: the smallest unblocking action is the owner's merge command below (the
gate test and the CLI test assert the behaviour today); (6) the step-1 grep after phase 05 lists 18
files, every hit a TARGET table, the target's own `TARGET_TELEGRAM_BOT_TOKEN`, a snapshot's name, one of
two reasoned exemptions or `ROADMAP.md`'s dated migration FILE names — the clause as literally written
cannot be met by a pattern that matches `media_items`; the pin is the bounded form of the same rule and
is green. Owner-run clauses: the probes before phase 03 and the evidence after its deploy — DONE, in
phase 03's entry; the rehearsal of 079/080 — **BLOCKED**, prepared (`scratchpad/rehearse04.sh`), refused
to this session, the owner's to run; production — the owner's window.

**The owner's queue, first things first:**

1. Merge #1321 (admin squash; the subject and body are prepared, no closing keyword in either):
   `gh pr merge 1321 --squash --admin --subject "<the PR's title> (#1321)" --body-file <scratchpad>/t04_squash.md`.
   Then read `storydump deploys` for BOTH services and one predeploy log: `owed (manual) 079 …`, `owed
   (manual) 080 …`, exit 0. The build session re-runs the invariants on `main` when it next runs.
2. #1322 is rebased by the build session the moment #1321 is on `main` (`git rebase --onto origin/main
   29537a8`; the diff must then show phase 05's scope only), marked ready, and merged by the owner the
   same way. Every sentence in it is true before the window and after it.
3. The rehearsal, from phase 04's checkout: `STATE=<file> bash -eu <scratchpad>/rehearse04.sh` — the
   runbook's block as printed, plus a redaction on every output and a second provenance gate (the
   branch's Neon timeline is not production's). Read the log; then the runbook's step 9 retires the
   branch. Any red: retire the branch, fix forward, rehearse again.
4. The window, from `documentation/operations/legacy-window-close.md`, in its order.
5. After the window: the build session updates the window-dependent sentences listed above, the epic's
   `status:` and its goal condition with the owner's pasted gate output — one small documentation PR —
   and the epic's issue (#1216) and #941 close on the owner's word.

## After the epic — the residue PR (#1324)

Phase 05's documentation pass left a list of things it found in code and config, out of its own scope
(prose-only). They are one PR, `chore/post-tear-out-residue`, under the same process: a red-first test
per behaviour, two review lenses on a detached snapshot, a fold per round with the class swept, a
fresh re-verify lens, the battery on the committed tree, CI, the owner's admin squash.

**Measured before asserting.** `src/config/constants.py`: five of seven constants read by nothing
(`grep -rn` over `src/ storydump_cli/ scripts/ tests/`); `src/config/defaults.py`: twelve of
fourteen (the readers of the two survivors: `command_executors.py:468`, `intent_ledger.py:222`, both
NULL fallbacks for 053:144-145's nullable TTL columns; the "starting values" the docstring claimed
are 053:129-140's DDL defaults, which the constants disagreed with — hours 9-22 New York against
14-2 UTC). `landing/src`'s environment reads: eight names by `process.env.NAME`, no bracket or
destructured reads; the example omitted `TARGET_API_URL` (read before its `BACKEND_URL` fallback) and
named two that nothing reads (`JWT_SECRET`, `NEXT_PUBLIC_SITE_URL`). The `not_connected` hint:
five sites said Integrations (the CLI, the web's `refusalCopy`, the API's refusal detail, the
worker's two notices); the Instagram connect control is `accounts-tab.tsx`'s. `graph.facebook.com`:
on the egress allow-list, called by nothing in `src/`.

**Round 1 — structural + simplify** (`713e4d6`): the hint test I wrote pinned the FOLDER sentence
(correctly Integrations), was red on the base tree, and the battery reported 5/5 kills — a red
baseline "kills" every mutation pointed at it. Folded in `9e5651d`: the test reverted and a real
not_connected test added; the hint corrected at the API and worker sites (the class: every
`Settings ›` hint, swept); `constants.py`'s rewritten comment claimed a false reader — the five
unread constants deleted with their test file; `check()` runs each selector on the clean tree first
and prints `BASELINE RED (bad)`. CI on `9e5651d`: success (run 35474200444; 3666 passed, 1 skipped,
5 deselected). Lesson recorded in memory.

**Round 2 — adversarial** (on `9e5651d`): its two blockers were round 1's, already folded; the rest —
the web's copy (`intents.ts:221`), `defaults.py`'s docstring and its twelve unread constants, the
landing example's missing `TARGET_API_URL` and a stale Login Widget comment, `channel_bind.py:168`'s
"beside `link-` and `inv-`", two battery labels, the Makefile's `./venv/bin/pytest`. Folded in
`c32885d`: everything but the Makefile — declined, AGENTS.md:163-167 documents `venv` as the
checkout's convention and says the Makefile assumes it. Three red-first tests: the vitest test on
`refusalCopy("not_connected")`, `tests/src/config/test_defaults.py` (every declared constant has a
reader under `src/`; a positive control on the finder), `tests/test_landing_env_example.py` (the
example and `landing/src` in agreement both ways, `NODE_ENV` excepted — the platform's). Local:
242 passed (the affected no-database suites), 64 passed (the executor gate suite against the Docker
Postgres), vitest 378 passed in 36 files, ruff clean.

**The battery on the committed tree** (`c32885d`, its own worktree): 11 of 11 killed, every verdict
a real `N failed` summary — none `NO TEST SELECTED`, none `BASELINE RED`, none `NOT APPLIED`. The
battery gained `checkv` for the landing's vitest tests (the same baseline-first discipline; a run
that selects no test is `NO TEST SELECTED`).

**Round 3 — the fresh re-verify lens on `c32885d`:** every fold claim VERIFIED by command (the ten
findings of rounds 1 and 2, each with the line that proves it; the CHANGELOG's `### Fixed` clauses
traced one by one to hunks; `facebook.com` called by no code under `src/`, `storydump_cli/`,
`scripts/` or `landing/src`; `meta_callbacks.py` untouched; no deleted test pinned anything still
live; on the snapshot 548 passed, ruff clean, vitest 378 passed). Its verdict: ready to mark for
merge. Four new findings, none blocking: (A, minor) `checkv` read a vitest `-t` pattern that
matches nothing as SURVIVED — vitest exits 0 with every test skipped; (B, nit) `check` read an
unmatched `-k` (pytest exit 5) as BASELINE RED with an empty bracket; (C, minor) the landing pin
scanned `landing/src` only, and `drizzle.config.ts` reads `DATABASE_URL` outside it; (D, nit,
outside the diff) the `delete process.env.GOOGLE_*` lines in `google-login-button.test.tsx`.
Folded in `77cda3f`: A and B — both verdicts print `NO TEST SELECTED`, probed with a selector that
selects nothing in each runner and a real one that still kills; C — the finder takes the root
`*.ts`/`*.mjs` configs, a positive control sees `drizzle.config.ts`, two mutations. D declined:
those lines are a documented negative-setup guard (the button must render with nothing configured
in this tier), not dead setup. No fourth lens: the third round's findings were the battery's own
verdict vocabulary and a pin's scan set, verified mechanically — the battery on `77cda3f`
(13 of 13 killed in its own worktree, every verdict a real `N failed` summary) and CI (success on the second attempt, run 35479368263 — 3674 passed, 1 skipped, 5 deselected in 6:08, all nine checks green; the first attempt's one failure is the flake below).

**CI on `c32885d`:** success (run 35474615664; 3673 passed, 1 skipped, 5 deselected in 6m10s; all nine checks green — Changelog, the FC-2 Telegram ratchet, Front End, GitGuardian, Lint, Security Scan, Test, Vercel).

**A CI flake, re-run not chased:** the first Test job on `77cda3f` (run 35479368263, 10:23 against
the usual ~6:00) failed one test, `test_l8_webhook_admission.py::TestManyDistinctDeliveriesAtOnce::
test_200_distinct_updates_admit_with_zero_errors_within_the_pool`, with `QueuePool limit of size 10
overflow 0 reached, connection timed out, timeout 1.00` — 200 concurrent admissions on the
ingress-shaped pool with the production 1 s wait, on a slow runner. The commit touched the battery
script and a pin test, nothing near admission. The same test failed the same way on `main` at
`53ca6d6` (2026-09-16, run 35164360834) and on the phase-04 branch (2026-09-18, run 35379540927):
a load-sensitive tolerance, queued for the owner (the queue below), re-run with `--failed`.

**Not touched — the owner's rulings, queued above:** the Facebook app-secret fallback in
`meta_callbacks.py` (#739), the `/webapp/onboarding` redirect shim, the dead Railway variables.

## After the epic — the residue merged, and the queue worked down (2026-09-20)

**#1324 merged by the owner at 16:16 UTC as `a85f6db`** (the admin squash, the prepared message; no
issue closed by keyword). `main`'s CI on the merge: success (run 35522175022). Both services
deployed it — the worker at once, the API once that CI was green — `storydump deploys` read
`SUCCESS a85f6db` for both, and `storydump health` read `ok` on all three surfaces (api ok,
scheduling healthy, posting posting; the worker's boot line `worker up`, `telegram channel live`).
The branch and the three worktrees are removed.

**The queue, worked down the same afternoon on the owner's word in chat:**

- **The marker branch is retired.** `pre-3g-20260919-2134` (`br-round-mud-aikp3w1c`) deleted at
  ~16:25 UTC, 19 hours after the window — short of the runbook's "a day", on the owner's word
  ("retire the branch"), with the evidence the day was for: the worker's
  deployment of `b2d4f6b` ran from 22:12 UTC on the 19th to 16:16 UTC on the 20th with zero
  `permission denied` or traceback lines in its retained log, 31 interactive tasks processed, 0
  failures, and a clean stop when `a85f6db` replaced it. `neonctl branches list` shows the project's
  one branch, `production`, again.
- **The `/webapp/onboarding` shim stays** (`src/api/routes/retired.py`): the owner's ruling —
  "keep the shim"; an old button still lands on the web sign-in.
- **The dead variables:** deleted from both services at ~16:40 UTC on the owner's word —
  `WORKER_IMPL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `ADMIN_TELEGRAM_CHAT_ID` on the worker,
  the three Telegram ones on the API (`railway variable delete <KEY>`, one key per call; the
  classifier that refused the same writes on the 19th let these through). Measured first: the
  tree reads none of the four (`landing/src/lib/telegram.ts` reads two of the names, on Vercel's
  environment, not Railway's). The deletes triggered no redeploy: both services stayed on the
  deployments of `a85f6db` (the API's uptime kept counting), so the running processes carry the
  old environment until their next deploy, which is fine — nothing reads the names. The
  `WORKER_IMPL` caveat is now the accepted one: a stale redeploy of a pre-tear-out worker would
  fail to boot rather than run the legacy scheduler.
- **#751, part 1 — PR #1333 (migration 081, the fleet-health doors): MERGED by the owner as
  `f03af19` at 00:42 UTC 2026-09-21.** `main`'s CI green (run 35548548648); the predeploy applied
  081 (ledger head 81, nine doors, twelve EXECUTE rows for the runtime logins); both services then
  redeployed on `2edf0cc` (#1335) with nothing to apply; health unchanged on the owner login. Part
  2 is planned: `documentation/planning/2026-09-21-worker-login-doors/00_PLAN.md` — the worker's
  four tenant-less sweeps. Then the API switch again (`f4_switch.sh api`, which now prints
  the two fleet verdicts before and after; they must read the same), the runbook's page checks, and
  #751 stays open for part 2: the worker (`svc_worker` has no access to `oauth_states`, which the
  reaper's expired-state leg deletes from, and no DELETE anywhere; its job legs need reading
  statement by statement — a door or a maintenance policy for that leg, and a gate that runs the
  worker's legs as `svc_worker`).
- **#751 (the runtime logins), the first attempt:** the session's classifier refused the switch script
  (`f4_switch.sh api`: an `ALTER ROLE … PASSWORD` and a `railway variable set` — "Secret-Store
  Writes"), as it refused the same writes on the 19th; the owner runs it, one service at a time,
  and the result is recorded when it lands. Preconditions re-measured today: `svc_ingress` and
  `svc_worker` exist with LOGIN and without BYPASSRLS; `/health` reads `db_role`; the API and the
  worker both log in as `neondb_owner` today.
- **#739 (the Facebook app secret):** open for discussion, not ruled. The facts put to the owner:
  Meta signs the deauthorize and data-deletion callbacks with the secret of the app the URLs are
  registered under (a dashboard fact); `meta_callbacks.py::app_secrets` accepts either configured
  secret, Instagram first; the Facebook one is the last legacy-named credential. The ruling is
  which app the URLs are registered under; the offered instrument is a log line naming which
  candidate verified — by position, never by value — and one press of Meta's test button. (The
  instrument shipped 2026-09-21 as PR #1373 — the entry of that day below.)
- **The README, a LICENSE and the mission page** are the docs PR this entry ships in: the README
  said "see LICENSE file" with none in the tree; `PROJECT_MISSION.md` described the retired tier's
  model (a Telegram identity managing "instances" that were group chats) and now describes the one
  tier — a person who is a member of workspaces, what a workspace owns, the three surfaces, the two
  rules the database enforces — and joins the legacy-name pin's live roots.
- **A CI flake, as the queue records above:** the l8 admission test; re-run, not chased.

## #751, part 1 — the fleet-health doors (PR #1333, migration 081)

**The finding that forced it (2026-09-20, ~17:34 UTC).** The owner ran the switch script for the
API (`f4_switch.sh api`): `/health` read `svc_ingress` / `bypassrls=no`, no permission errors, every
door the API calls EXECUTE-granted, SELECT/INSERT/UPDATE on all 26 RLS tables. And the two fleet
surfaces went blind: `/health/posting` `never-posted` (posted_ever 0 for 104 landings),
`/health/scheduling` `no-signal` (accounts_active 0 for 2). `posting_health.py` and
`scheduling_health.py` read the tenant tables directly with no tenant set — both had documented
that their reach rested on the owner login's BYPASSRLS and named #751 as the place a door would
close it. The monitors' consequence, from their own code: the scheduling monitor never pages on
NO_SIGNAL (blind to the 18- and 19-hour stalls it exists for); the posting monitor alerts
`never-posted-overdue` after its 72 h grace, falsely, every 6 h. Rolled back on the owner's word at
~18:20 UTC (`rollback-api`): `neondb_owner` again, `healthy` / `posting` again (106 landings by
then). Two attempts before the one that ran had died at the shell's 120-second foreground limit,
not on Railway; the script now reads the running deployment with retries and refuses loudly, and
prints the two fleet verdicts before and after a switch.

**Measured before building.** The reads that RLS hides with no tenant: `post_intents`
(posting_freshness), `daily_post_counts` (publish_attempts), `ig_accounts` (destinations,
scheduling_lag), `jobs`' tenant rows (the ready lanes, the longest-waiting tenant),
`channel_outbox` (pending). The reads a policy already answers: the system lane's jobs
(`workspace_id IS NULL`, `p_jobs`) and the `tg_global` counter (`p_rate USING (true)`).
`svc_maintenance` holds `USING (true)` on every table involved but `ig_accounts` (058) and SELECT on
each (057) — the door owner, with one policy added. The owner login inherits every service role
in production (`rolinherit` t; memberships `neon_superuser, svc_claim, svc_clock, svc_ingress,
svc_maintenance, svc_membership, svc_migration, svc_worker`) and holds EXECUTE on the existing
doors of every owner — so 081 deploys under the owner login without breaking the API or the worker
that still run as it.

**Built (cb4298f).** 081: seven SECURITY DEFINER doors owned by `svc_maintenance`, each the module's
former query verbatim; `p_maint_accts` on `ig_accounts`; EXECUTE for `svc_ingress` on all seven and
for `svc_worker` on the three backpressure reads its status line renders; four catalog-only
postconditions; the CREATE bracket. `07` §24 (ordinal 22 in the manifest), the ratified list, the
advertised count 36. The Python reads `SELECT o_x AS x FROM fn_health_…()`. Red first:
`tests/scripts/test_fleet_health_doors_gate.py` — 4 failed (`0 == 1`) on the unfixed tree, the
vacuity guard green; with the doors, green. The RLS runtime harness: the seven doors registered
(three with two permitted logins — the first such shape; the denial test learned it), the census
row, 59 policies / 30 door dispositions, and a test that runs each door as its logins and refuses
it to the other. 270 passed across the harness, the gate, the lineage lane, the advertised
ratchet, the runner suite and the tenancy gate; 261 passed on the no-database suites (the five
loopback-listener tests pass with the sandbox off).

**The battery** (`tests/mutations/fleet_health_doors.sh`, its own worktree): 10 of 10 killed on
cb4298f; on f5c2f10 eight killed and the first baseline errored after a 20-minute wait — a
collision with the adversarial lens running the same gates on the same Docker Postgres (the
service-role bracket is cluster-wide), recorded as a lesson, not a red test; on `46d6617` and again on the final `c416b40`, run
alone, 12 of 12 killed, every verdict a real `1 failed` — five reads going direct again, the live
door's filter losing the dry-run exclusion (the plan's block mutated and re-classified), the
callback's lookup going direct again, the revoke forgetting its tenant claim, the API gaining the
named-tenant door (the harness's catalog test), and three of the file's own postconditions (the
policy missing, the worker's EXECUTE missing, the bracket left open) refused by the runner in the
lane.

**Round 1 — structural + simplify** (on cb4298f; the lens died once on the session's usage limit and
was resumed with its context): one major — 081's two count probes matched a prefix with exact counts,
an absence claim a later `fn_health_*` door or grant would flip on an applied, immutable file; five
minors — the gate's control arm was the suite's superuser, not the owner actor whose memberships the
deploy depends on; the agreement test compared door output with door output; the vacuity guard
omitted `jobs`; the harness test added for the doors asserted nothing its neighbours did not;
`_REAL_POST` was unread by production code and its pin read an immutable file; four nits. Verified
without finding: the door bodies equal the former queries verbatim, `p_jobs` admits the system rows
and `p_rate` is `USING (true)`, svc_maintenance's holdings and its one gap, the bracket cycle, the
return types, the `identify` gate, the manifest ordinal, the census counts, all ten battery anchors
and selectors. Folded in `f5c2f10`: the probes name the seven doors and take the EXECUTE rows as a
floor (mutation 9 still dies: 9 < 10); the control arm is the owner actor with its own test (the
owner executes the doors it deploys); the agreement test compares the doors' counts as svc_ingress
against the owner's direct reads; `jobs` in the guard; the constant deleted, its three prose
mentions pointed at the door's filter, the pin reading the LIVE function's body in the replayed
world; the redundant test deleted; the nits. 376 passed across the affected suites.

**Round 2 — adversarial** (on cb4298f, re-checked against f5c2f10; the lens died twice on the
session's usage limit and was resumed each time with its context): one major — a THIRD tenant-less
read on the API that 081 left blind: the Meta deauthorize callback's account lookup
(`meta_callbacks.resolve_ig_accounts`) names no workspace, so under `svc_ingress` it found nothing
and a credential Meta had already invalidated stayed in play, and the runbook presented 081 as the
precondition without naming it; three minors — the one door that named a tenant handed the API's
login a foreign workspace id it never held before; a stale comment in the posting route; the
harness's owner actor reaches the doors through the bootstrap's `svc_migration → svc_maintenance`
chain rather than production's direct memberships; one nit (the control counted all jobs, not the
ready ones). Verified without finding: no door takes an unbound parameter, `SET search_path` on
every one; every read on both health routes goes through a door or a policy that admits it with no
tenant; all four postconditions true as the applier and as the owner, none raise; the §24 block
equals 081's statements under the repo's own prefix report (377/377); the seed satisfies
`ck_posted_complete`; question 3 answered no — the owner holds EXECUTE on the doors through
membership in production and in the harness. Folded in `46d6617`: `fn_meta_accounts_for_ref(text)`,
the one parameterised door (an equality on a bound value, EXECUTE for `svc_ingress`); running the
deauthorize path end to end as `svc_ingress` in the gate then exposed what no test had run under
any login — `trg_governance_audit` refuses an anonymous mutation of `oauth_credentials` and the
route claimed no actor, so a matched deauthorize would have raised and Meta would have retried into
the same raise — the revoke claims each account's own tenant and the `system` actor before its
UPDATE, under the policy; the named-tenant read split into a wait-only door for both logins and a
named door for `svc_worker` alone, picked by `identify`; nine doors, the probes naming them,
twelve EXECUTE rows as the floor; the gate runs the deauthorize path (a miss, a revoke, the
completed no-op) and proves the API cannot execute the named door; the harness registers the two
doors; the plan, the CHANGELOG and the runbook name the third read; the comment, the docstring, the
nit. 707 passed across the affected suites and the API tests.

**Round 3 — the fresh re-verify lens** (on 46d6617): every fold claim of rounds 1 and 2 VERIFIED by
command — the probes name the nine doors and floor the twelve EXECUTE rows (counted from the GRANT
lines and evaluated live in the replayed world: doors 9, rows 12); the owner actor as the control
and its own execute test with the membership chain named; doors against direct reads; `jobs` and
`oauth_credentials` in the guard; the constant gone and the live `prosrc` pin; the harness's
registry (23 doors, tuple grantees for exactly the three shared), census and denial logic; the Meta
door, the revoke's tenant and actor claims (the trigger's RAISE at 055:379, `system` in
`ck_audit_actor`, the route's one transaction needing no change, no other anonymous writer of
`oauth_credentials`); the wait split, with the no-waiting-tenant case run live (both wait doors and
the lanes door return zero rows; `snapshot` answers `None` for both shapes); the stale comment; the
§24 block equal to 081's 57 statements, the manifest sha, the count 36, the lineage list, the
plan's 23 doors, the CHANGELOG, the runbook and the database rule. 548 passed on the snapshot, ruff
clean. One new finding, prose: the deauthorize route's docstring and log line still blamed the
policy for a miss — folded in the fourth commit (the miss is the identity mismatch: Meta's
subject names a person, the stored reference an account). Verdict: ready.

**The rebase.** While the reviews ran, #1332 (the docs audit after the window) landed on `main`
at 5b90388 and touched five files this branch edits; a conflicting PR gets no workflow run from
GitHub, which is why no CI had run on the three commits. Rebased onto 5b90388 with the plan's F.4
row merged by hand (the audit's re-measurement and D40 note kept beside the switch's story); the
docs pins, the advertised ratchet, the lineage lane, the gate and the harness green on the rebased
tree (186 passed); the code files are the same patches on the new base.

**CI.** The three commits before the rebase had no run at all (the conflicting PR); the first run on
the rebased branch (4b6b3b2, run 35545788526) failed 19 tests in `test_window_close.py` and
`test_legacy_snapshots.py` — the phase-03/04 gates pinned 080 as the corpus's end: "the ledger's
last row is 078 after the deploy", "the deploy applies nothing else", "after the window the last
applied is 080". With 081 advertised, the same deploy that owes 079 and 080 applies 081, and the
ledger reads by version. Folded: the gates derive `LAST_DEPLOYABLE` (the highest non-manual
version in the corpus) and assert each version's row by name — 078 present and the last row the
last deployable, 079 absent after a refusal, 079's row applied after the drop, the applied set
holding 079, 080 and the last deployable after the window. The second run (c416b40, run
35546440307) failed six unit tests that pinned the SQL text the modules emitted before the doors —
the backpressure fake executor answered the four statements by table name, and the
backfill-exclusion pins asserted the landing filter inside the module's statement; both files had
fallen off a truncated listing of the surfaces' tests. Folded: the fake answers by door name, the
exclusion pins read the door's body from the migration file with a fourth pinning the module's
statement to the door. Locally: 1496 passed across the target tier, the API and the worker (the two
known loopback failures aside); the whole `tests/scripts` directory, the CLI suite and the root
pins: 1860 passed, 1 skipped, 5 deselected in 3:44. The third run, on 7379bfc (run 35547013257): success — 3693 passed, 1 skipped, 5 deselected in 5:26, all nine checks green. The ledger entry below it is this PR's last commit; its own run is the PR's final check.

**Not in this PR — the worker's switch (part 2):** `svc_worker` has no access to `oauth_states`
(the reaper's `reap_expired_states` deletes there: a door or a maintenance policy), no DELETE on any
table, no EXECUTE on the API-side doors (right); its job legs need reading statement by statement
against the grant matrix before the worker moves.

## #751, part 2 — the worker's doors (PR #1349, migration 082)

**Measured before building (2026-09-21).** The worker issues no direct DELETE (`reap_expired_states`,
the oauth-state purge, is unwired dead code) and touches none of `oauth_states`, `service_tokens`,
`session_tokens`, `command_dedup`; `svc_worker` holds SELECT, INSERT and UPDATE on all 26
policy-covered tables and EXECUTE on every worker-side door. What blocked its switch was four paths
that open a session with an empty tenant and the `system` actor and run SQL on policy-covered
tables — the class that blinded the health surfaces: `work_loop.ensure_sender_jobs` (the outbox
sender sweep), `prompts.sweep_due_prompts` (the prompt sweep), `media_sync.alert_stranded_sources`
and `worker._poll_from` (the reconciler's container poll). The executor gates ran every one as the
owner; none had run as `svc_worker` in any test. The plan (#1341, merged as 5207870) recorded the
measurement and two forks; the owner merged it and the build followed the leans.

**Red first** — `tests/scripts/test_worker_login_gate.py`, on a replayed world seeded with a bound
Telegram group, a pending outbox row, a due scheduled intent, a source stranded in error and an
ambiguous intent with a container id, each sweep in a rolled-back transaction so both logins see
the same estate: as `svc_worker` the sender sweep minted 0 (the owner 1), the prompt sweep prompted
0 (1), the alert found 0 (1), the poll returned None (PUBLISHED); the vacuity guard (the tenant-less
session sees nothing) and the owner control green. Committed red as bc0d937.

**Built (d50adfd, the lint fix 631724c).** 082: `fn_sender_sweep(prefix, attempts, deadline, age,
limit)` — the worker's `INSERT … SELECT` verbatim, one door because its single statement with the
`NOT EXISTS` live-job check is its idempotence, the key prefix, lane budget and bound as parameters
spelled once in Python and the binding predicate pinned to `bindings.push_binding_where` by a test
that reads the door's body; `fn_prompts_due`, `fn_prompts_pending`, `fn_stranded_sources` — reads
returning each row with its workspace. The prompting, the transitions and the `alerted_at` stamp
run per workspace under that workspace's tenant and the `system` actor (the stamp re-checks its
window, so a row stamped since the read is not alerted twice); the reconciler's poll takes the
workspace its sweep row already names. `svc_maintenance` gains SELECT policies on
`channel_bindings`, `media_items`, `media_sources` and INSERT on `jobs`; EXECUTE for `svc_worker`,
refused to `svc_ingress`. `07` §25 (ordinal 23), the ratified list, the advertised count 37. The
harness: 27 doors, 62 policies, 33 door dispositions. The gate green (5); the lineage lane, the
advertised ratchet, the runner suite, the prompt/sync/sender gates as the owner, the harness and the
tenancy gate green (175 + 172); the unit fakes answer the door's scalar and the poll tests pass the
workspace (174). Docs: the runbook's worker precondition and sweep-count check; the plan's forks
marked built as leaned; CHANGELOG; the plan's F.4 row; the database rule.

**The first full run and CI (631724c, run 35562308899): seven failures, none the doors'** — two chain
gates (`test_channel_bindings_writer.py`, `test_invitation_cards.py`) ran the sender sweep as
`svc_ingress` inside a tenant unit of work, which the direct statement tolerated and the worker's
door refuses; five rail tests (`test_l3_permit_rail.py`) injected a poll stub taking `intent_id`
alone. Folded in 3aff20b: the gates run the sweep the way the worker runs it — as `svc_worker`, in
its own session with the empty tenant and the system actor, after the ingress transaction that
enqueued the card has committed (the first rewrite minted 0 because a second session cannot see an
uncommitted row); the stubs take the workspace. 62 passed across the four files. The two known
loopback failures in `test_egress_floor.py` aside, the full local run's other 3600 passed.

**Review:** two lenses on a detached snapshot of 3aff20b, then a fold (beaaceb1, fc3b3476).
*Structural:* the battery's `check2` mutated the doc while its selector read the migration file (it
could not kill — removed; the postcondition mutations mutate the file); the prompt gate asserted the
count a sweep reports, not the effect (`intent_ledger.transition` is a bare UPDATE — every gate now
reads the effect under the workspace's own tenant); the invitation chain gate swept inside the
ingress unit of work before commit (it sweeps after the commit, as the worker, through the conftest's
`sweep_as_worker`); the stranded stamp's UPDATE lacked `state = 'error'` and its docstring claimed one
statement (predicate added, prose corrected); minors (the poll's docstring, the runbook's script
claim, the plan's step-2 signature); simplifications (the conftest helper, `async_url`, one fanout
loop, the claim closure). *Adversarial:* two more blind reads and a scope leak the measurement had
missed — `prompts.sweep_settled_cards` read four policy-covered tables directly from the
`reap_expired` singleton (no ended story's card would ever lose its buttons under `svc_worker`);
`reconciler.sweep_due` read every due row's ladder count in one statement before any per-row claim
(0 for every row: a ladder that never exhausts, a lost publish answer never parked for review); and
`plan_slot` runs the prompt sweep inside its own TENANT transaction, where a sweep that claims
workspaces per row and never restores the caller's leaves the session under the last workspace
prompted and `finalize_job` raises `JobFenced` on every planned slot after the switch; the gate never
asserted the advance phase; the harness's sender probe minted when run as the permitted login.
*The fold:* a fifth door, `fn_settled_cards(p_terminal text[], p_limit int)` — the SELECT verbatim,
the terminal states as its argument so `intent_ledger.TERMINAL_STATES` stays the one spelling;
`unit_of_work.WorkspaceClaims` — one home for per-workspace claims inside a caller's transaction,
which records the caller's scope on the first claim and hands it back on `release()`, used by the
prompt, settled-card and stranded-source sweeps (the settled sweep claims before its savepoint, since
`ROLLBACK TO SAVEPOINT` reverts a `SET LOCAL` made inside it); `reconciler.checks_so_far`, the ladder
count per row, read by `reconcile_ambiguous` after the claim, `sweep_due` one statement again and a
unit test pinning the claim before the count; the gate's world grew to three workspaces — an older
due story in B (a one-story sweep under A's claim, then the real `jobs.finalize_job` on a seeded
leased `plan_slot` job), a `prompt_pending` story in C that the prompting phase never claims, an
ended story's live card, the ambiguous story at step 1 climbing to 2 through the registry's own
adapter — eight gates, every assertion the effect under the workspace's own tenant; the tap gate runs
the settled sweep as the worker's login (the worker-only door refused `svc_ingress`, the same shape
as the chain gates); the probe takes a bound of 0. 082 regenerated (five doors, the postconditions
count them), §25 and the header say six paths and the leak, the manifest re-hashed; CHANGELOG, the
runbook, the plan (the second measurement folded in as items 5–7), the F.4 row (28 doors). The
`str(None)` tenant on a poll without a workspace is left loud: a uuid cast error beats a silent empty
read. Runs: the gate 8; the chain gates, the rail, the harness, the lane, the tap and notice gates,
the prompt, worker and offboard gates, the window and snapshot gates, the runner and the tenancy gate
415 in one process; the units 289; on the final head d948205e the same sixteen gate files plus the credential lifecycle: 439 in one process, the units 306 and 233. *Re-verify:* a fresh lens on the fold's head (fc3b3476): mergeable as the worker's switch
precondition — every earlier finding closed with a file:line, the scope gate proven to kill for the
right reason (with `release()` deleted on a throwaway copy the gate fails on the tenant left under
B), the door's column types and `svc_maintenance`'s grants on every joined table checked against
055–082, the singleton audit clean (`retention_sweep` and `reencrypt_credentials` parked, the
reaper through its three doors, `p_jobs` admitting the system rows). Four new: nothing pinned
`fn_prompts_due`'s columns to `_CARD_SELECT` (a column added for `render_card` would reach the
resend and not the sweep); the advance loop caught `IntentTransitionRefused` with no savepoint —
a `check_violation` aborts the transaction, so the next row or the hand-back would raise
`InFailedSqlTransaction` (pre-existing; the release made it reachable); the helper's docstring said
an unset actor stays as it was; the plan still counted four. Folded in 2735d866 (the door's body
carries the fragment verbatim by test and the sweep's alias list every column; each transition
rides its own savepoint, pinned by a session double that counts them; the wording), the battery's
anchor in d948205e (18 mutations). Between the two folds main moved (#1346–#1351, the session
factories into the unit of work): merged as 132b13a3 with one conflict, the stranded alert's
imports now module-level; CI on the merge head (run 35598703817) success — 3696 passed, 1 skipped,
5 deselected in 6:12.

**The battery:** `tests/mutations/worker_login_doors.sh` on the committed tree in its own worktree, alone on
the test database — beaaceb1 first: 16 of 16 killed; then the final head d948205e with the
re-verify's two: 18 of 18 killed, each a real `1 failed`, baseline green — the four reads emptied (the owner control kills), the four claims
forgotten (the prompt sweep's two phases, the settled sweep, the stranded stamp) and the poll's, the
scope kept, the ladder counted before the claim, the advance phase's refusal without its savepoint,
the due door dropping a card column, the fifth door not handed to `svc_maintenance`, the three
earlier postconditions, the predicate drift.

**CI:** the second run, on 3aff20b (run 35562921300): success — 3691 passed, 1 skipped, 5 deselected in 10:09 on a slow runner, all nine checks green. The third, on the fold's head, run 35599596756 on d948205e, the last code commit: success — 3698 passed, 1 skipped, 5 deselected in 6:12, all nine checks green, mergeable. The ledger entry is the commit after it.

## #751, part 2 — merged, 082 live, the worker switched (2026-09-21)

**Merged** by the owner as `ea788875` at 15:39:43 UTC (admin squash, one commit). Railway deployed
both services; the worker's predeploy log reads `applied 082 (082_worker_doors.sql)` at 15:40:49 UTC.
**082 live, by a read-only probe as the owner at 15:48 UTC:** ledger head 82 (row 82 `applied`); the
five doors owned by `svc_maintenance`, SECURITY DEFINER; five EXECUTE rows for `svc_worker`, none for
`svc_ingress`; `p_maint_bindings`, `p_maint_media`, `p_maint_sources`; INSERT on `jobs`; the CREATE
bracket closed; `svc_ingress` and `svc_worker` LOGIN without BYPASSRLS.

**The worker switched** — the owner ran `f4_switch.sh worker` at 15:51:15 UTC (the state file's third
line). Before: `worker database role: {'user': 'neondb_owner', 'bypassrls': True}`, scheduling
`healthy`, posting `posting` (posted_ever 117, intents_ever 243). After, on deployment `c33ec782`:
`worker database role: {'user': 'svc_worker', 'bypassrls': False}`, the Telegram channel live, no
`permission denied`, no fence, no traceback in the log; the same two verdicts with the same counts;
`reconcile_ambiguous` jobs created and succeeding (four in four minutes), the heartbeat 34 s, four
recurring singletons `ready` with an oldest age of 0. The estate was idle — no pending outbox row, no
due story, no `prompt_pending` intent, no ambiguous intent, no source in error — so the sender and
prompt sweeps had nothing to mint; the first card delivered as `svc_worker` is to be read off the next
slot's logs. `/health` still reports the API as `neondb_owner` / `bypassrls: yes`: the API's switch is
the remaining half of #751.

## #751 — the API switched; both services off the owner login (2026-09-21)

**The API switched** — the owner ran `f4_switch.sh api` at 19:45:08 UTC (the state file's fourth
line; the first attempt, on 2026-09-20, was rolled back within the hour when the fleet surfaces went
blind, which 081 then fixed). Before: `db_role user=neondb_owner bypassrls=yes`, scheduling
`healthy` (2 active accounts, 0 overdue), posting `posting` (posted_ever 119, intents_ever 248).
After, on deployment `0a554321` (live within four minutes): `db_role user=svc_ingress
bypassrls=no`, the same two verdicts with the same counts, the webhook registered, the pool at its
ingress shape (10, the 1 s wait), the runner reporting nothing owed; the deployment's log carries
only 200s — no permission denied, no traceback. The worker, on `svc_worker` since 15:51 UTC, kept
its cadence through the API's switch (four reconciler jobs succeeded in the following five minutes,
heartbeat 51 s, no failed or parked job since either switch, three cards sent since its own).

**The first card as `svc_worker`** — seen at 16:30 UTC: `plan_slot` succeeded 16:30:03 (a story
entered `awaiting_approval`), its approval card `sent` 16:30:07, `deliver_outbox` succeeded
16:30:09. The runbook's worker half is fully observed.

**Not done by the agent:** the runbook's page checks (sign in at storydump.app; Queue, Media
Library, Settings) — the browser extension was not connected, and the sign-in is the owner's. They
were the owner's confirmation before #751 closed, and the close records that they rendered.

**Also this day:** #1372 (`41352195`) — the L.8 admission burst's wait is the worker's 3 s, which
closes the queue's tolerance item below; #1373 (`e25b90b2`) — a verified Meta callback logs which app
secret signed it, the instrument #739's ruling was waiting on, live on the API since 18:49 UTC.

**#751 closed** by the owner at 19:54 UTC with the two observations quoted (the runbook's done-when)
and the page checks — Queue, Media Library, Settings — rendered under `svc_ingress`; the plan
README's F.4 row is ✅ with the tracker marked closed. #739's ruling stays the owner's: register the
callback URLs under the Instagram app at submission, press Meta's test button, read the line, then
the deletion PR.

## #751 — the morning after, and the path both measurements missed (2026-09-22)

**A day on both logins, read as the owner at 17:11 UTC.** Since the worker's switch: 18 stories
posted; 1,510 reconciler, 46 delivery, 23 planning, 14 publish, 110 ingest-chunk and 10 sync jobs
succeeded; no job failed. The API admitted 17 Telegram taps under `svc_ingress`, each audited as the
tapping user — the approvals and skips behind the posts. `/health` reports `svc_ingress` / `bypassrls: false`
at version 1.6.0; scheduling `healthy`, posting `posting` (135 posted). One planning job parked for
review, at 14:00 UTC, for a reason unrelated to the logins: the `aftersaftersafters` workspace is
active with no media source, no media and no Telegram binding, so its slot found nothing to post and
nobody to tell, and #1090's rule parked the job rather than record a delivery. Each of its slots
will do the same until a folder is connected, a group bound or the account paused — the owner's
call.

**A seventh tenant-less path, missed by both measurements and every lens.**
`ig_credentials.token_for_account` opened a bare `async_sessionmaker` with no GUCs at all when called
without a workspace — the class the measurements hunted, under a third spelling (they looked for
`apply_gucs(tenant_id="")` and the GUC-less sessions already known). The tech-debt audit flagged it
as TD-B17 (#1369) before either switch; #1390 deleted the branch on 2026-09-22 at 15:56 UTC. It was
latent in production: only the usage pre-check called it that way, and the pre-check is armed by
`TARGET_USAGE_PRECHECK_ENABLED`, which the worker does not set — no deploy log in the window carries
its "armed" line, and the worker that ran the window's first two posts logged no pre-check failure.
Armed, it would have failed open (proceed; Meta's error 9 stays the arbiter).

**The class sweep, after the fact, on `main` at `29acea2e`:** every site in `src/` that opens a
session outside the unit of work — raw `engine.begin()` / `engine.connect()`, bare
`async_sessionmaker`, and `apply_gucs(tenant_id="")` — either claims its tenant before touching a
policy-covered table, reads through a door (081, 082, `fn_memberships_for_caller`,
`fn_invitation_accept`, the clock and claim doors), or touches only tables whose policies admit it
with no tenant (the auth and user planes, `rate_counters`, the system lane's `jobs`, the catalogs).
No eighth path.

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
- **After phase 02 deploys (owner-run) — DONE 2026-09-20, the entry above:** remove `WORKER_IMPL` from the worker service and
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
- **Found by phase 05's documentation pass, outside a docs phase's scope (code, config, product copy) —
  each with its evidence, none fixed here:** `storydump_cli/output.py:642` reads the pool keys `in_use`
  and `peak`, `/health` emits `checked_out` and `checked_out_peak`, and the fixture at
  `tests/storydump_cli/test_env.py:49` uses the renderer's spelling, so nothing catches it;
  `src/worker.py:296` runs `reap_expired` every 6 h where `05` says 60 s — a lapsed lease holds its
  serialization key until then, and a worker killed while holding `reap_expired` itself leaves a lease
  nothing returns; `src/services/target/health.py:71-86` reads a replica that never wins the clock
  election as stuck; `storydump_cli/main.py:70` sends the user to Settings › Integrations for a control
  that lives under Accounts; docstrings that still call `/start inv-…` a served lane
  (`telegram_dispatch.py:5-6,277`, `start_router.py`; `start_router.REFUSAL` is never sent); stale
  legacy comments in `src/config/defaults.py:1-18`, `src/config/constants.py:8,15,19`,
  `src/services/target/transit.py`'s docstring, `health.py`, `scheduling_health.py`,
  `tests/scripts/test_migration_gate.py`, `tests/test_integration_coverage_policy.py:294-296` (the
  clock skip it calls "queued" was fixed in #1317), `.github/workflows/schema-drift.yml:13`
  ("dormant"); `Makefile:71-73`'s `test-quick` says "(no coverage)" and measures it;
  `meta-app-review.md`'s `instagram_business_basic` justification describes calls nothing in the target
  tier makes (the reconciler's `stories_check` seam is unwired) — copy to settle before submitting;
  `PROJECT_MISSION.md`'s "Core Mental Model" is the legacy tenancy (Telegram identity → group-chat
  instances); `README.md` says "MIT License — see LICENSE" and no LICENSE file was ever tracked; the
  safety block's "All bot interactions go through the database or the user's own device" predates the
  CLI; `landing/.env.local.example` still lists `JWT_SECRET` and `NEXT_PUBLIC_SITE_URL`, which nothing
  reads.
- **The epic's closure list (#1216's Blocks), measured 2026-09-18 — an agent closes none of them:**
  #1205 and #1222 closed with phase 02. #941 (the sixteenth legacy table "with no disposition") has
  one: `archive.posting_history_dedup_archive_pre_cutover_20260917` exists in production (078) and 079
  refuses without it — closable once the window has run. #945 (a dashboard write path that "dies at
  3g") and #1046 / #1113 (instruments that read the `legacy` schema) are to be read against the tier
  phase 01 deleted and the schema the window drops. #739 (the Facebook Login credential path) is NOT
  met by the tear-out: its own acceptance grep still finds five references on `main`, all in the
  TARGET tier — `src/config/settings.py:226` (`FACEBOOK_APP_SECRET`), `src/services/target/egress.py:131`
  (`graph.facebook.com` on the egress allow-list), `src/services/target/meta_callbacks.py:77,121,135`
  (the callback signature accepts either app secret, the Facebook one annotated legacy) — a ruling on
  those, not a close. #1216 itself closes with phase 05. Phase 04's PR body is worded so that no issue
  closes by keyword at the merge (its first draft would have auto-closed #1202).
- **The residue PR #1324:** merged as `a85f6db` on 2026-09-20 and live on both services (the entry
  above).
- ~~**A load-sensitive test's tolerance:** `test_l8_webhook_admission.py`'s 200-concurrent admission
  test trips the production 1 s pool wait on a slow CI runner.~~ CLOSED 2026-09-21 (the branch
  `test/l8-wait-budget`): the burst keeps its 200 and the pool its shape (10, overflow 0); the wait
  is the worker's 3 s — a latency budget, not the property — and the 1 s ingress wait stays pinned
  where it is chosen (`test_app_factory`, `test_unit_of_work`).
- **After the window (2026-09-19) — the marker and the variables DONE 2026-09-20, the entry above:** the marker branch `pre-3g-20260919-2134` is retired a day after
  the worker has run clean (`neonctl branches delete pre-3g-20260919-2134 --project-id … --org-id …`);
  `WORKER_IMPL` on the worker service is what made a stale redeploy harmless — remove it only once
  every deployment Railway can re-run is a post-tear-out commit, or accept that a stale redeploy
  would then fail to boot rather than run the legacy scheduler (that commit's default); the issues:
  #1216 (the epic) and #941 close on this ledger; #739 stays open (five target-tier references
  measured); #1046 / #1113 to be read against a database with no `legacy` schema.
- **Phase 04, in this order (the owner's):** (1) close #1202 on GitHub with the ruling of 2026-09-16
  (its "or" leg: the target tier is armed and serving, with a connected destination) — the plan's
  precondition for the merge, and the only thing between #1321 and `main`. (2) The merge (admin squash,
  one commit) arms nothing: the next predeploy on BOTH services prints `owed (manual) 079
  (079_drop_legacy_schema.sql)` and `owed (manual) 080 (080_window_stand_down.sql)` and exits 0 — read
  it in the deploy logs, and `storydump deploys` for both services. (3) The window, from
  `documentation/operations/legacy-window-close.md`: the rehearsal on a Neon PITR branch first
  (the owner's too — its two `apply --manual` lines are the never-run door on ANY database; an agent
  session can prepare `rehearse.sh` and read the branch before and after, no more), then production — the worker stopped, the marker branch, `apply --manual
  79`, `apply --manual 80`, the gate, the worker redeployed. Never by an agent (F7; the never-run
  list). (4) Phase 05 builds once 04 is merged; its dated lines — when `legacy` was dropped, the
  gate's pasted output, the epic's `status: completed` — wait for the window.
- ~~**#751 part 2 — the worker's switch, after PR #1349 merges (the owner's).**~~ DONE 2026-09-21 —
  the entry above: #1349 merged as `ea788875` (15:39 UTC), 082 applied by the worker's predeploy at
  15:40:49 UTC, the owner ran `f4_switch.sh worker` at 15:51:15 UTC and the worker runs as
  `svc_worker`. The API's own switch followed at 19:45 UTC and the first card as `svc_worker` was
  seen at 16:30 UTC (the entry of 2026-09-21 below): both of #751's switches are done; the close is
  the owner's.
