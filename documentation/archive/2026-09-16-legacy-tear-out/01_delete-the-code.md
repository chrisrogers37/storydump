---
title: "Legacy tear-out — phase 01: delete the legacy code and its tests (PR 1)"
type: plan
status: completed
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, worker, tests]
links: [https://github.com/chrisrogers37/storydump/issues/1216]
---

> **Built 2026-09-17 as #1316 (`2369a9b`).** The ledger is `RUN_LOG.md`; its phase-01 entry records where the build departed from this text (118 files, not 117; `tests/integration/` deleted whole; the Makefile's `init-db`/`setup-db` kept and rebuilt in phase 02 — they never ran `init_db.py`).

## Summary

Delete every package nothing deployed imports — `src/services/core`, `src/services/integrations`,
`src/services/media_sources`, `src/repositories`, the non-target `src/models` modules, the legacy
sync engine `src/config/database.py`, `src/services/base_service.py`, the empty
`src/services/domain`, six legacy-only utilities, two legacy scripts — the legacy branch of
`src/main.py`, and their tests; rewrite the fifteen surviving test files that reached into them;
shrink the FC-2 ratchet's baseline so its core segment reads empty; drop the two dependencies
only they used; prove every deployed entrypoint still imports and the target suite is green with
nothing skipped. The reversible half of the retirement. One PR, deliberately: the import chain
`src.main → core → repositories → models → database` leaves an entrypoint broken or a package
untested under any split, so the deletion lands whole and its proof is the import of all three
entrypoints (the reference class, #1312's 90-file deletion, was reviewed the same way).

## Evidence

- Importers of the legacy packages outside themselves (grep on `d8f5c72`, re-run 2026-09-16 after
  both ironclad lenses): `src/main.py` (`src.services.core.loops.*` at `:11-23`, the service
  imports inside `main_async` at `:125-132`, `src.services.integrations.cloud_storage` at `:211`),
  `src/services/base_service.py` (the repositories), `scripts/init_db.py` (`src.config.database.
  init_db`), `scripts/backfill_memberships.py` (`src.config.database.get_db`; reads
  `settings.TELEGRAM_BOT_TOKEN` at `:124`), and tests. No importer under `src/services/target`,
  `src/api`, `src/channels`, `src/worker.py`, `storydump_cli/`.
- `src/services/media_sources/` (5 files / 946 lines) is imported only by five `src/services/core`
  modules and `src/services/integrations/google_drive.py`; the target's `drive_adapter.py:13`
  names it in a docstring, not an import. It goes, with `tests/src/services/media_sources/` (4
  files). Its `factory.py:149` imports `src.services.core.settings_service` INSIDE a function —
  the AST test must `ast.walk`, not read module-level imports only.
- `src/utils`, non-legacy importers under `src/` and `scripts/` (test files excluded): `logger`
  15, `datetime_utils` 1 (`src/services/target/transit.py`), `encryption` 1
  (`src/services/target/ig_login_oauth.py`) — stay. `validators` 0, `file_hash` 0 (its one
  importer is the legacy model `src/models/media_item.py`), `media_kind` 0, `resilience` 0
  (the only non-core importer of the `telegram` SDK), `image_processing` 0, `webapp_auth` 0 — go,
  with `tests/src/utils/test_{validators,file_hash,media_kind,resilience,image_processing,
  webapp_auth}.py`. Two constants survive them: `MIGRATIONS_DIR` (`validators.py:11`; importers
  `tests/scripts/conftest.py:42`, `tests/scripts/test_schema_drift_live.py:74`) and
  `INSTAGRAM_VIDEO_SUFFIXES` (`media_kind`; importer `tests/src/services/target/
  test_google_drive_adapter.py:35` — the adapter itself has no such constant, the test builds
  fixture names with it). `validators.py:45-64` reads the three settings phase 02 deletes;
  `ConfigValidator.validate_all` (`:26-101`) runs in no deployed process (importer: the legacy
  `lifecycle.py`); `tests/src/api/test_security_hardening.py::TestStartupSecretValidation` tests
  it and goes with it.
- `src/config/database.py` — the legacy sync engine (`engine`, `SessionLocal`, `Base`, `get_db`,
  `init_db`): importers are the legacy packages, `src/utils/{validators,resilience}.py`, the two
  scripts above and tests; the target tier's `unit_of_work.py:22` says retiring it "is M.3's".
  It goes with `tests/src/config/test_database.py`. `src/services/domain/` is an empty package
  and goes.
- `src/worker_impl.py` holds `WORKER_IMPL_*`/`resolve_worker_impl` (importers `src/main.py`,
  `scripts/target_reachability.py:466-470` and their tests); phase 02 deletes it.
- `python-telegram-bot` (`requirements.txt:15`, `setup.py:27`) and `Pillow` (`:20`, `:21`) have no
  importer outside the legacy packages in `src`, `scripts`, `storydump_cli` or `tests` (the target
  Telegram adapter is httpx). Both go.
- `Makefile:106-120` `init-db`/`setup-db` and `:182` `quickstart` run `scripts/init_db.py`; they
  go with it (the rest of the Makefile is phase 02).
- `src/models/__init__.py` re-exports the legacy models; `src/models/target/` is a separate
  package the target tier imports by its own path.
- `scripts/telegram_ratchet_baseline.json`: `core_telegram_modules` 15, `telegram_modules` 19,
  `chat_id_functions_outside_adapters` 93 — all sets of module paths; the gate compares the tree
  to the baseline and `--write-baseline` re-measures.
- `scripts/target_reachability.py` probes `src.main`, `src.api.app` and `src.worker` in their own
  interpreters and needs the three legacy settings variables today (it failed without them on
  2026-09-16 because `src.main` imports `src.services.core.loops.lifecycle`, which loads settings).
- `tests/src/test_worker_impl_gate.py` pins the dispatch in `src/main.py`.
- The DB gates are NOT untouched: `tests/scripts/test_lineage_lane.py:207-211`
  `legacy_declared_tables()` imports `src.models` and reads the legacy `Base.metadata.tables`;
  `:426-470` asserts `legacy` holds exactly that inventory (positive control "the legacy models
  registered nothing") and `:893-902` asserts the two bases are separate metadata;
  `tests/scripts/test_migration_gate.py:339-353` compares the replayed legacy schema to
  `Base.metadata.create_all` of the legacy models. `scripts/setup_database.sql` is the lane's
  legacy seed (`test_lineage_lane.py:81-92`) and stays.
- The fifteen surviving test files that reach into the deleted code, with what each needs:
  `tests/conftest.py` (`:42` legacy `Base`; `:745-757` `create_all`/`drop_all` of the legacy
  schema for the unit-test database; `:806-818` `route_repos_to_test_db`; `:837,851` a legacy
  repository inside two fixtures), `tests/src/services/conftest.py:7-8`, `tests/src/
  test_health_endpoint.py` (whole file — its subject `src.main._build_health_response` is the
  legacy loops' liveness server, `main.py:49`), `tests/src/test_main_scheduler_loop.py`,
  `tests/src/test_periodic_scheduler.py`, `tests/src/exceptions/test_{backfill,google_drive,
  instagram}_exceptions.py`, `tests/src/api/test_security_hardening.py:53,69`,
  `tests/integration/conftest.py:23`, the four `tests/integration/` files, `tests/scripts/
  test_target_reachability.py:540`, and the two lane files above.
- `src/exceptions/__init__.py:4-21` imports the `backfill`, `google_drive` and `instagram`
  exception modules, so every `from src.exceptions.tenancy import …` in the target tier
  (`principal.py:46`, `v1.py:52`, `command_executors.py:63`, `service_tokens.py:39`, …) executes
  the legacy exception modules' imports — the architecture lane of the 2026-09-16 audit found the
  package absent from #1216's inventory. `src/utils/logger.py:7` loads `settings`, which is how
  the API inherits the legacy Telegram requirement (phase 02).

## Implementation Plan

### Dependencies
PR #1314 (the audit fold) merged — `main` at or after it. F1, F2 and F3 ratified.

### Blocks
Phase 02 (the settings' readers must be gone first); the F.6 segment's emptiness.

### Steps

1. **Measure before deleting.** Paste into the PR: the importer grep above re-run on the branch
   base; `scripts/target_reachability.py` output (with the dummy variables); the ratchet's three
   counts; `pytest --collect-only -q | tail -1` for the whole suite.
2. **Delete the packages**: `git rm -r src/services/core src/services/integrations
   src/repositories src/services/media_sources src/services/domain src/services/base_service.py
   src/config/database.py scripts/init_db.py scripts/backfill_memberships.py`; the Makefile's
   `init-db`, `setup-db` and `quickstart` targets (`:106-120,182`); in `src/exceptions/` the modules only the
   deleted code raised (`backfill`, `google_drive`, `instagram` — measure each: a class the target
   tier catches or raises stays, moved into `tenancy.py` or its own kept module) and their lines
   in `src/exceptions/__init__.py`; the non-target models `src/models/{api_token,
   audit_log,category_mix,chat_settings,enums,instagram_account,media_item,media_lock,
   onboarding_session,posting_history,posting_queue,service_run,user_chat_membership,
   user_interaction,user}.py` (verify `enums.py` has no target importer first — `grep -rn
   "src.models.enums\|from src.models import" src/services/target src/api src/channels`); rewrite
   `src/models/__init__.py` to export nothing (or delete it if `src/models/target` does not need
   the package — it does, keep an empty module with a docstring); `src/utils/{validators,
   file_hash,media_kind,resilience,image_processing,webapp_auth}.py`. Re-home the two constants
   first: `MIGRATIONS_DIR` becomes `scripts/migration_runner.py`'s (`Path(__file__).parent /
   "migrations"` — the runner's own tree constant; `tests/scripts/conftest.py:42` and
   `test_schema_drift_live.py:74` import it from there), and `INSTAGRAM_VIDEO_SUFFIXES` is
   inlined in `tests/src/services/target/test_google_drive_adapter.py` as the test's own data
   (the adapter never read it).
3. **`src/main.py`** per F2 (a): keep the module; delete `main_async`, the loop imports (the
   `src.services.core` imports at `:11-23`, `:125-132` and `:211`), `_build_health_response` and
   its server (the legacy loops' liveness), the shutdown handlers and the legacy arm of the
   dispatch; `main()` becomes `target_worker.main()` (the `WORKER_IMPL` read and
   `src/worker_impl.py` itself go in phase 02, with the variable). The docstring says the Procfile
   runs it and it dispatches to `src.worker`. Update `tests/src/test_worker_impl_gate.py` to pin
   the new shape (the module imports `src.worker` and nothing legacy).
4. **Delete the tests by rule, not by list**: every file under `tests/` whose AST (walked, so a
   function-level import counts) imports a deleted module goes — run the `test_legacy_tier_gone.py`
   predicate over `tests/` and paste the list (the grep of 2026-09-16 matched 117 of 265 files;
   the predicate's list is the truth). Known members: `tests/src/services/test_*.py` (56 files),
   `tests/src/services/media_sources/` (4), `tests/src/repositories/` (22, including
   `test_f1_fail_closed.py`, whose module-level repository imports settle it: it goes, no
   re-homing), `tests/src/models/` (11; there is no `tests/src/models/target/`), the six utils
   tests, `tests/src/config/test_database.py`, `tests/src/test_health_endpoint.py` (whole),
   `tests/src/test_main_scheduler_loop.py`, `tests/src/test_periodic_scheduler.py`,
   `tests/src/exceptions/test_{backfill,google_drive,instagram}_exceptions.py`, and four
   `tests/integration/` files (`test_callback_authorization_gate`, `test_commit_refresh_sweep`,
   `test_tenant_provisioning_door`, `test_track_execution_leak`; `test_instagram_posting.py`
   stays). REWRITE, not delete — each with its line: `tests/conftest.py` (`:42` the `Base` import
   and `:745-757` the `create_all`/`drop_all` of the legacy schema go — the unit-test database
   fixture builds nothing legacy any more; `:806-818` `route_repos_to_test_db` and the two
   repository fixtures at `:837,851` go); `tests/src/services/conftest.py:7-8` (the fixtures that
   build a legacy service go, the rest stays); `tests/integration/conftest.py:23` (the fixture
   importing `src.config.database` goes); `tests/src/api/test_security_hardening.py` (the
   `TestStartupSecretValidation` class goes with `ConfigValidator`; the rest stays);
   `tests/scripts/test_target_reachability.py:540` (a target module as the specimen);
   `tests/scripts/conftest.py:42`, `tests/scripts/test_schema_drift_live.py:74` and
   `tests/src/services/target/test_google_drive_adapter.py:35` (the re-homed constants of step 2).
   THE LANE: write `tests/scripts/legacy_inventory.py` with `LEGACY_TABLES`, the sixteen names as
   a literal (the fourteen of `02` §9 plus `posting_history_dedup_archive` and `schema_version`;
   phase 03's gate imports the same list — one home); `test_lineage_lane.py`'s
   `legacy_declared_tables()` returns it, `test_legacy_holds_the_inventory_the_running_application_
   declares` keeps its shape (the `DROP TABLE` mutation included) against the literal, its
   positive control becomes "the literal is non-empty and every name exists in the replayed
   `legacy`", and `test_the_target_base_is_a_separate_metadata_from_the_legacy_one` goes with its
   subject; `test_migration_gate.py::TestSchemaParity::test_replayed_schema_equals_models_schema`
   goes with the legacy models (the replayed target schema's parity is the advertised-stream
   gate's already).
5. **Ratchets**: `python scripts/telegram_ratchet.py --write-baseline` — the script asserts set
   EQUALITY and rewrites, so "only removals" is the reviewer's check on the diff, not the
   script's; the counts measured on `d8f5c72` with the deletion applied to the inventory:
   `core_telegram_modules` 15 → 0, `telegram_modules` 19 → 4 (`src/channels/telegram_transport.py`,
   `src/channels/telegram_webhook_registration.py`, `src/exceptions/telegram.py`,
   `src/services/target/telegram_dispatch.py`), `chat_id_functions_outside_adapters` 93 → 0,
   `provider_account_ref_log_sites` 0 → 0. `test_legacy_tier_gone.py` pins those four counts.
   `tests/scripts/test_no_implicit_admin_fallback.py` asserts its `admin-grant-ok:` allowlist by
   equality — the sanctioned sites are in deleted core modules, so the allowlist shrinks to what
   survives (measure; expected empty); the gate itself is retired in phase 02 with the field it
   guards.
6. **`setup.py` / `requirements.txt`**: drop `python-telegram-bot` and `Pillow` (measured: no
   importer outside the legacy packages in `src`, `scripts`, `storydump_cli`, `tests`); measure
   every other requirement the same way (grep the remaining tree for its import name) and paste
   the table. Anything uncertain stays; a removal is a measured line in the PR. `pip install -e
   '.[cli]'` and the suite green afterwards are the proof.
7. **CI**: `bandit -r src/ storydump_cli/` and coverage already cover the tree; the deleted
   `tests/integration` files were in `pytest tests/` — nothing to add.
8. **Docs touched by this PR only where a path stops existing**: `AGENTS.md`'s architecture
   section (the legacy packages), `README.md`'s tree, `.claude/PROJECT_CONTEXT.md`'s module map;
   `CHANGELOG.md` gets its `### Removed` entry with the counts (`.claude/rules/changelog.md`; the
   CI check requires it). The runbooks and guides are phase 05.
9. **Close #945 as stale** in the PR body: the dashboard's write path is the target router
   (`landing/src/app/api/dashboard/[...path]/route.ts:2` proxies to it; the queue's commands go
   through `landing/src/lib/commands.ts` to the port) — nothing dies at 3g. The proxy's legacy
   allowlist itself is phase 05's (the audit's AP-L3).

## Test Plan

- Tests red first: `tests/src/test_legacy_tier_gone.py` (new) asserts the packages and files are
  absent, that no module under `src/`, `scripts/` or `storydump_cli/` imports
  `src.services.core|integrations|media_sources`, `src.repositories`, `src.config.database`, a
  deleted `src.utils` module or a non-target `src.models` name (AST-based over `ast.walk`, so a
  function-level import counts, like `tests/test_legacy_cli_gone.py`; with a positive control
  that plants a module importing each forbidden prefix under a `tmp_path` tree and expects a
  hit for every prefix), that the ratchet's four counts are the numbers of step 5, that importing
  `src.main`, `src.api.app` and `src.worker` in a fresh interpreter WITH the dummy Telegram
  variables leaves no `src.services.core|integrations`, `src.repositories` or legacy
  `src.models` module in `sys.modules`, and that the ratchet baseline's core segment is empty —
  all red on the base. (The import WITHOUT the variables is phase 02's test: `Settings()` still
  requires them at import until F5 lands there.)
- `scripts/target_reachability.py` in CI (a test that runs it with the dummy variables and
  asserts the three closures import) — green on the base; it pins the closures' shape after the
  deletion.
- The whole suite: `pytest tests/ -q` green with the legacy tests deleted; the count of collected
  tests before and after pasted; no new `skip`.
- The DB gates green after the lane rewrite of step 4 (they are NOT unchanged — two of them
  derived the legacy inventory from the deleted models); the replayed `legacy` still equals the
  sixteen-name literal.
- Battery `tests/mutations/legacy_tear_out_01.sh`: narrow the AST test's forbidden-prefix set by
  one prefix (killed by its positive control — re-adding a real legacy import would die at
  collection, a KILLED BY ERROR, so the mutation is on the test's predicate); re-add a core
  module to the baseline (killed by the emptiness test); leave one legacy module in
  `src.main`'s closure (killed by the `sys.modules` test); drop one name from `LEGACY_TABLES`
  (killed by the lane's inventory test).

## Verification Checklist

- [ ] `grep -rn "src.services.core\|src.services.integrations\|src.services.media_sources\|src.repositories\|src.config.database" src scripts storydump_cli tests` → 0 lines.
- [ ] `env TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 python -c "import src.main, src.api.app, src.worker, sys; print(sorted(m for m in sys.modules if m.startswith(('src.services.core', 'src.repositories', 'src.services.integrations'))))"` prints `[]` (the variables themselves go in phase 02).
- [ ] `python scripts/telegram_ratchet.py` passes; the four axes read 0 / 4 / 0 / 0.
- [ ] `pip install -e '.[cli]'` succeeds without `python-telegram-bot` and `Pillow`; `make help` no longer lists `init-db`, `setup-db`, `quickstart`.
- [ ] `pytest tests/ -q`: green, collected count pasted, `-rs` shows no new skip.
- [ ] `scripts/target_reachability.py`: three entrypoints, closures pasted.
- [ ] `git diff --stat origin/main..HEAD` shows only deletions plus the files named above.

## What NOT To Do

- Do not delete a `src/utils` module because it "looks legacy" — delete by measured importer
  count, and keep the tests of what stays.
- Do not change the Procfile, `railway.toml`, or a Railway variable in this PR (F2 (a)); the
  never-run list's `python -m src.main` line stays true.
- Do not touch `src/api/routes/retired.py` (F3) or the settings fields (phase 02) here.
- Do not skip a legacy test to keep the suite green — delete it with the code it tested.
- Do not renumber or edit any migration; this PR ships none.
- Do not delete `scripts/setup_database.sql` or `scripts/window/step0_bootstrap.sql`: SQL that
  describes the legacy lineage is the lane's input, not legacy code.
- Do not rebuild `ConfigValidator`'s startup secret check inside the target tier here — it ran in
  no deployed process; whether the target wants one is the owner's question (queued in the
  ledger), not this PR's scope.
- `src/utils/encryption.py:32-33`'s pointer to the unbuilt `reencrypt_credentials` executor is
  the target tier's own note: out of scope here.

## Context

Area: worker, models, config, tests · Effort: XL (mechanical, large: about 28,000 lines and 117
test files; the reference class #1312 was 90 files) · Risk: low with the import proof, high
without · Priority: first, after #1314 merges.
