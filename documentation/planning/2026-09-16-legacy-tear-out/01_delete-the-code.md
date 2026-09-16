---
title: "Legacy tear-out — phase 01: delete the legacy code and its tests (PR 1)"
type: plan
status: draft
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, worker, tests]
links: [https://github.com/chrisrogers37/storydump/issues/1216]
---

## Summary

Delete every package nothing deployed imports — `src/services/core`, `src/services/integrations`,
`src/repositories`, the non-target `src/models` modules, `src/services/base_service.py`, the two
legacy-only utilities — the legacy branch of `src/main.py`, and their tests; shrink the FC-2
ratchet's baseline so its core segment reads empty; prove every deployed entrypoint still imports
and the target suite is green with nothing skipped. The reversible half of the retirement.

## Evidence

- Importers of the legacy packages outside themselves (grep on `d8f5c72`): `src/main.py`
  (`src.services.core.loops.lifecycle` and the loop modules, lines 26-42), `src/services/
  base_service.py` (the repositories), `tests/src/services/test_*.py` (56 files), `tests/src/
  repositories` (22), `tests/src/models` (11), `tests/integration/test_track_execution_leak.py`,
  `test_callback_authorization_gate.py`, `test_commit_refresh_sweep.py`. No importer under
  `src/services/target`, `src/api`, `src/channels`, `src/worker.py`, `scripts/`, `storydump_cli/`.
- `src/utils`: `logger` (15 non-legacy importers), `validators` (6), `file_hash` (3),
  `datetime_utils` (2), `encryption`, `media_kind`, `resilience` (1 each) stay;
  `image_processing` and `webapp_auth` (0) go, with `tests/src/utils/test_image_processing.py`
  and `test_webapp_auth.py`.
- `src/models/__init__.py` re-exports the legacy models; `src/models/target/` is a separate
  package the target tier imports by its own path.
- `scripts/telegram_ratchet_baseline.json`: `core_telegram_modules` 15, `telegram_modules` 19,
  `chat_id_functions_outside_adapters` 93 — all sets of module paths; the gate compares the tree
  to the baseline and `--write-baseline` re-measures.
- `scripts/target_reachability.py` probes `src.main`, `src.api.app` and `src.worker` in their own
  interpreters and needs the three legacy settings variables today (it failed without them on
  2026-09-16 because `src.main` imports `src.services.core.loops.lifecycle`, which loads settings).
- `tests/src/test_worker_impl_gate.py` pins the dispatch in `src/main.py`.

## Implementation Plan

### Dependencies
None (main at or after the audit fold, #1314). F1, F2 and F3 ratified.

### Blocks
Phase 02 (the settings' readers must be gone first); the F.6 segment's emptiness.

### Steps

1. **Measure before deleting.** Paste into the PR: the importer grep above re-run on the branch
   base; `scripts/target_reachability.py` output (with the dummy variables); the ratchet's three
   counts; `pytest --collect-only -q | tail -1` for the whole suite.
2. **Delete the packages**: `git rm -r src/services/core src/services/integrations
   src/repositories src/services/base_service.py`; the non-target models `src/models/{api_token,
   audit_log,category_mix,chat_settings,enums,instagram_account,media_item,media_lock,
   onboarding_session,posting_history,posting_queue,service_run,user_chat_membership,
   user_interaction,user}.py` (verify `enums.py` has no target importer first — `grep -rn
   "src.models.enums\|from src.models import" src/services/target src/api src/channels`); rewrite
   `src/models/__init__.py` to export nothing (or delete it if `src/models/target` does not need
   the package — it does, keep an empty module with a docstring); `src/utils/image_processing.py`,
   `src/utils/webapp_auth.py`.
3. **`src/main.py`** per F2 (a): keep the module; delete `main_async`, the loop imports (lines
   26-42 and the lifecycle imports), the shutdown handlers, `resolve_worker_impl`'s legacy arm and
   `WORKER_IMPL_*`; `main()` becomes `target_worker.main()`. The docstring says the Procfile runs
   it and it dispatches to `src.worker`. Update `tests/src/test_worker_impl_gate.py` to pin the
   new shape (the module imports `src.worker` and nothing legacy; `WORKER_IMPL` no longer read —
   phase 02 removes the variable from the settings).
4. **Delete the tests**: `tests/src/services/test_*.py` (the 56 legacy files — verify each
   imports a deleted module; a file that tests a kept utility stays), `tests/src/repositories/`,
   `tests/src/models/` (keep `tests/src/models/target/` if it exists), the two utils tests, the
   three `tests/integration` files, and any fixture in `tests/conftest.py` or `tests/src/conftest.py`
   that builds a legacy repository or session (grep `repositories\|services.core` there).
5. **Ratchets**: `python scripts/telegram_ratchet.py --write-baseline` — the diff must only
   REMOVE entries (the gate's own invariant; paste the counts: core segment 15 → 0); the F.1
   `SYSTEM_SCOPE` pin in `tests/src/repositories/test_f1_fail_closed.py` moves with its directory
   — re-home the pin to `tests/scripts/` if it guards target code, else it goes with the legacy
   tests (read it first: it scans `src/` for `SYSTEM_SCOPE`); `tests/scripts/
   test_no_implicit_admin_fallback.py` roots stay `("src",)`.
6. **`setup.py` / `requirements.txt`**: drop dependencies only the deleted packages used (measure:
   for each requirement, grep the remaining tree; `python-telegram-bot` is used by the target
   Telegram adapter? — verify before removing; Pillow by `image_processing` only?). Anything
   uncertain stays; a removal is a measured line in the PR.
7. **CI**: `bandit -r src/ storydump_cli/` and coverage already cover the tree; the deleted
   `tests/integration` files were in `pytest tests/` — nothing to add.
8. **Docs touched by this PR only where a path stops existing**: `AGENTS.md`'s architecture
   section (the legacy packages), `README.md`'s tree, `.claude/PROJECT_CONTEXT.md`'s module map.
   The runbooks and guides are phase 05.

## Test Plan

- Tests red first: `tests/src/test_legacy_tier_gone.py` (new) asserts the packages are absent,
  that no module under `src/` imports `src.services.core|integrations`, `src.repositories` or a
  non-target `src.models` name (AST-based, like `tests/test_legacy_cli_gone.py`), that
  `src.main` imports without the three Telegram variables set, and that the ratchet baseline's
  core segment is empty — all four red on the base.
- `scripts/target_reachability.py` in CI (a test that runs it and asserts the three closures
  import) — red on the base because it needs the variables.
- The whole suite: `pytest tests/ -q` green with the legacy tests deleted; the count of collected
  tests before and after pasted; no new `skip`.
- The DB gates unchanged and green (they touch nothing legacy).
- Battery `tests/mutations/legacy_tear_out_01.sh`: re-add one legacy import to a target module
  (killed by the AST test); re-add a core module to the baseline (killed by the emptiness test);
  make `src.main` read a legacy variable (killed by the import-without-variables test).

## Verification Checklist

- [ ] `grep -rn "src.services.core\|src.services.integrations\|src.repositories" src scripts storydump_cli tests` → 0 lines.
- [ ] `python -c "import src.main, src.api.app, src.worker"` succeeds with no `TELEGRAM_*` variable set (phase 02 makes the settings agree; here the import must not need them).
- [ ] `python scripts/telegram_ratchet.py` passes; `core_telegram_modules` is `[]`.
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

## Context

Area: worker, models, tests · Effort: XL (mechanical, large) · Risk: low with the import proof,
high without · Priority: first.
