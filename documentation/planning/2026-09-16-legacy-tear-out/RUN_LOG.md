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

**Verification** — filled as the runs land (see the entries below).

## Owner-decision queue

- **The API service's skipped deploys.** Railway marked `53ca6d6` and `4f2b36b` `SKIPPED` on the `storydump` (API) service while the worker deployed; the fold's API-side fixes (`ops_views.py`, the doctor/health paths) are not live until an API deploy lands. Phase 01's merge triggers one; sooner, by hand: `railway redeploy --service storydump`. Not run by the agent.
- **A latent CI flake: the skip ceiling meets a clock-of-day skip.** `tests/scripts/test_l5_pipeline_gate.py::TestTheFirstFetch::test_a_local_cap_wait_on_a_slot_today_promises_tomorrow` skips when the account's local time is 23:55–23:59; any CI run starting in that window breaches `MAX_EXPECTED_SKIPS`. A fix that removes the skip: set the fixture account's `tz` to a zone where the local hour is not 23 at test time (the test already reads `tz` from the row). Target-tier test hygiene, outside this plan's scope.
- **A startup secret check for the target tier?** The legacy `ConfigValidator` (deleted with phase 01) checked `ENCRYPTION_KEY` at boot; nothing in the target tier does the same at import. A decision, not a regression.
- The tear-out's own gates as they arise (phase 03's probe lines and the 078 rehearsal; phase 04's window).
