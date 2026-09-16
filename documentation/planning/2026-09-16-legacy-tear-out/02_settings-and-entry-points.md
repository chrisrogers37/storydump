---
title: "Legacy tear-out — phase 02: retire the legacy settings, the worker variable and the config surfaces (PR 2)"
type: plan
status: draft
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, config, worker, docs]
links: [https://github.com/chrisrogers37/storydump/issues/1222, https://github.com/chrisrogers37/storydump/issues/1205]
---

## Summary

With the legacy readers gone (phase 01), retire what they required: the three bare Telegram
variables `Settings` demands of every process (#1222), the `WORKER_IMPL` switch whose legacy arm
no longer exists, the legacy lines of `.env.example` and the Makefile (#1205), and the dummy
values CI, the test recipes and the docs set to satisfy a requirement nothing reads. After this PR
a process needs only the variables the target tier reads.

## Evidence

- `src/config/settings.py:192-194` — `TELEGRAM_BOT_TOKEN: str`, `TELEGRAM_CHANNEL_ID: int`,
  `ADMIN_TELEGRAM_CHAT_ID: int` (required), instantiated at import (`:400`); the class comment
  at `:196-209` says the legacy variable survives only until #1222.
- `.github/workflows/ci.yml:123-125` sets the three (`test_token`, two chat ids); the local
  recipes and every battery set `TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1
  ADMIN_TELEGRAM_CHAT_ID=1`; `AGENTS.md:112-116` tells every entry point to set dummy values.
- `.env.example:10` `ENABLE_INSTAGRAM_API=false`, `:40-46` the three variables, `:64-65`
  `POSTING_HOURS_*`, `:153` `DRY_RUN_MODE` — legacy surfaces (#1205 names `ENABLE_INSTAGRAM_API`
  and the Makefile's `create-schedule` target).
- `Makefile` targets: `run`, `dev`, `check-db`, `db-shell`, `db-backup`, `db-restore`,
  `validate-env`, `env-example`, `quickstart` — read each; the audit (R-L6) found `install` lacks
  `[cli]`, `install-dev` names an absent `dev` extra, and `make dev` gates a local worker on
  production's health.
- `src/main.py` after phase 01 dispatches unconditionally; `WORKER_IMPL_*` and
  `resolve_worker_impl` live in `src/worker_impl.py` (importers: `src/main.py`,
  `scripts/target_reachability.py` — `worker_gate_facts` at `:442`, `_label_deployed` at `:488` —
  and their tests `tests/src/test_worker_impl_gate.py`, `tests/scripts/test_target_reachability.py`).
- The Railway variables `WORKER_IMPL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID` and
  `ADMIN_TELEGRAM_CHAT_ID` are set on both services and read by nothing after this phase.
- `Makefile:197` `validate-env` calls `src.config.settings.get_settings`, which does not exist —
  the target is broken today.
- `tests/scripts/test_no_implicit_admin_fallback.py` is a shape gate over
  `settings.ADMIN_TELEGRAM_CHAT_ID` (`ADMIN = "ADMIN_TELEGRAM_CHAT_ID"`, roots `("src",)`); once
  the field is gone its hazard cannot be written and no positive control can exist.
  `tests/src/config/test_settings.py` and `test_settings_never_echo_values.py` name the three
  variables (the "required" tests and the redaction tests).
- Environment read outside `Settings`: `WORKER_LOG_LEVEL` (`src/worker.py:792`), `PORT`
  (`src/services/target/health.py:152`), `DATABASE_URL` (the runner only) — an `.env.example`
  agreement test must allow them explicitly.

## Implementation Plan

### Dependencies
Phase 01 merged. F5 ratified.

### Blocks
Phase 05 (the docs describe this end state).

### Steps

1. **`src/config/settings.py`**: delete the three required fields and every field only the
   deleted code read (measure each remaining field: `grep -rn "settings\.<NAME>" src scripts
   storydump_cli` — a field with zero readers goes; paste the table). Delete `WORKER_IMPL` and
   `resolve_worker_impl` if phase 01 left them. Keep `TARGET_TELEGRAM_*`, the database fields,
   the target tier's fields.
2. **`tests/src/config/test_settings.py`, `test_settings_never_echo_values.py`**: the "required"
   tests for the three fields become "not present" tests; a redaction test that used one of the
   three as its secret specimen uses `TARGET_TELEGRAM_BOT_TOKEN` (the redaction rule is the
   thing under test, not the field). Retire `tests/scripts/test_no_implicit_admin_fallback.py`
   with the field: a gate whose hazard cannot be written is vacuous, and the CHANGELOG line says
   so (the `admin-grant-ok:` inventory it maintained is empty after phase 01).
3. **CI and recipes**: remove the three variables from `.github/workflows/ci.yml`; from
   `tests/mutations/*.sh`'s `UNIT`/`GATE` strings; from `documentation/guides/
   dev-environment-setup.md`, `testing-guide.md`, `AGENTS.md:112-116`; from the audit's scratch
   recipes' documentation. A variable set for nothing is a lie the next reader has to disprove.
4. **`.env.example`**: delete the legacy lines (`ENABLE_INSTAGRAM_API`, the three variables,
   `POSTING_HOURS_*`, `DRY_RUN_MODE`, `MEDIA_SOURCE_*` if unread — measured as in step 1); the
   file describes the target tier's variables only, grouped by service. The Makefile's
   `validate-env` target (`:195-197`; there is no `scripts/validate_env.py`) is broken — it calls
   a `get_settings` that does not exist — so it becomes `python -c "from src.config.settings
   import settings"` or is deleted; `make help` lists what runs.
5. **Makefile**: delete the targets that ran legacy surfaces (`create-schedule` if still
   present, `run` if it runs the legacy worker, `check-db` if it queries legacy tables); fix
   `install` to `pip install -e '.[cli]'`, `install-dev` to a real extra or a requirements file,
   `dev` to a local worker without production's health as a gate. `make help` lists what exists.
6. **`src/main.py`** docstring and `tests/src/test_worker_impl_gate.py` renamed to
   `test_worker_entrypoint.py`: the entrypoint imports `src.worker` only; no environment switch.
7. **Delete `src/worker_impl.py`** and the `WORKER_IMPL` read in `src/main.py`; update
   `scripts/target_reachability.py` (`worker_gate_facts` `:442`, `_label_deployed` `:488` and its
   import) and `tests/scripts/test_target_reachability.py`; `tests/src/test_worker_impl_gate.py`
   becomes `test_worker_entrypoint.py` (the entrypoint imports `src.worker` only).
8. **`.claude/settings.json`** allow/deny rules that still name legacy commands (`:54-59`) —
   owner-owned; list the lines in the PR for the owner (the sandbox refuses to write the file).
9. **The Railway variables (owner-run, after the deploy)**: remove `WORKER_IMPL`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID` and `ADMIN_TELEGRAM_CHAT_ID` from both services —
   a variable set for nothing is the lie in the environment F5 condemns. Listed here, never
   done inside the PR.

## Test Plan

- Red first: a settings test that `Settings()` constructs with no `TELEGRAM_*` variable in the
  environment, and that `import src.main, src.api.app, src.worker` succeeds in a fresh
  interpreter with no `TELEGRAM_*` variable set (phase 01's proof was with the dummy values);
  a test that `.env.example` names only variables `Settings` declares (parse both;
  every `NAME=` line has a field, every required field has a line; an explicit allowlist for the
  variables read outside `Settings` — `WORKER_LOG_LEVEL`, `PORT`, `DATABASE_URL` — asserted by
  equality so a fourth is a visible diff) — red on the base because the three legacy lines have
  fields but the target tier's example is incomplete, or vice versa (read the current state; the
  assertion is exact agreement).
- `tests/test_agent_docs.py` still passes (the never-run list is unchanged: `python -m src.main`
  still starts the worker).
- Unit suite green with NO Telegram variables set in the run (the recipe change is the proof).
- Battery `tests/mutations/legacy_tear_out_02.sh`: re-add a required legacy field (killed by the
  construct-without-variables test); add a `.env.example` line for a variable no field declares
  (killed by the agreement test).

## Verification Checklist

- [ ] `env -i PATH=$PATH DATABASE_URL=… python -c "from src.config.settings import settings"` succeeds.
- [ ] `grep -rn "TELEGRAM_BOT_TOKEN\b\|TELEGRAM_CHANNEL_ID\|ADMIN_TELEGRAM_CHAT_ID" src scripts tests .github Makefile documentation AGENTS.md CLAUDE.md` → only `CHANGELOG.md`, `documentation/archive/` and `documentation/planning/`.
- [ ] `make validate-env` runs and exits 0 (or the target is gone from `make help`).
- [ ] `grep -rn WORKER_IMPL src scripts tests` → 0.
- [ ] `make help` lists only targets that run; `make install` installs the CLI extra.
- [ ] CI green with the variables removed from the workflow.
- [ ] Owner-run: the four Railway variables removed from both services after the deploy; `.claude/settings.json`'s legacy rules replaced.
- [ ] The PR closes #1222 and #1205.

## What NOT To Do

- Do not remove a settings field by name-guessing; every removal cites its zero-reader grep.
- Do not touch `TARGET_TELEGRAM_*`, the database fields, or the Railway variables — production
  reads them.
- Do not change the Procfile or `railway.toml`.
- Do not rewrite the runbooks here (phase 05); only the lines that set the dead variables.
- Do not remove the Railway variables inside the PR — list them for the owner (step 9).

## Context

Area: config, CI, Makefile · Effort: M · Risk: low (every change is measured) · Priority: after 01.
