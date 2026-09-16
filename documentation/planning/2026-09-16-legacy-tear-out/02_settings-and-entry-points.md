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

- `src/config/settings.py:186-189` — `TELEGRAM_BOT_TOKEN: str`, `TELEGRAM_CHANNEL_ID: int`,
  `ADMIN_TELEGRAM_CHAT_ID: int` (required); the class comment at `:191-204` says the legacy
  variable survives only until #1222.
- `.github/workflows/ci.yml:124-127` sets the three (`test_token`, two chat ids); the local
  recipes and every battery set `TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1
  ADMIN_TELEGRAM_CHAT_ID=1`; `AGENTS.md:112-116` tells every entry point to set dummy values.
- `.env.example:10` `ENABLE_INSTAGRAM_API=false`, `:40-46` the three variables, `:64-65`
  `POSTING_HOURS_*`, `:153` `DRY_RUN_MODE` — legacy surfaces (#1205 names `ENABLE_INSTAGRAM_API`
  and the Makefile's `create-schedule` target).
- `Makefile` targets: `run`, `dev`, `check-db`, `db-shell`, `db-backup`, `db-restore`,
  `validate-env`, `env-example`, `quickstart` — read each; the audit (R-L6) found `install` lacks
  `[cli]`, `install-dev` names an absent `dev` extra, and `make dev` gates a local worker on
  production's health.
- `src/main.py` after phase 01 dispatches unconditionally; `WORKER_IMPL` is read nowhere else
  (`grep -rn WORKER_IMPL src scripts` after phase 01 — verify).
- `scripts/target_reachability.py:106` prints the `WORKER_IMPL` note; `tests/src/
  test_worker_impl_gate.py` pins the dispatch.

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
2. **`tests/src/config/`** (or wherever the settings tests live): the "required" tests for the
   three fields become "not required, not present" tests; the redaction tests stay.
3. **CI and recipes**: remove the three variables from `.github/workflows/ci.yml`; from
   `tests/mutations/*.sh`'s `UNIT`/`GATE` strings; from `documentation/guides/
   dev-environment-setup.md`, `testing-guide.md`, `AGENTS.md:112-116`; from the audit's scratch
   recipes' documentation. A variable set for nothing is a lie the next reader has to disprove.
4. **`.env.example`**: delete the legacy lines (`ENABLE_INSTAGRAM_API`, the three variables,
   `POSTING_HOURS_*`, `DRY_RUN_MODE`, `MEDIA_SOURCE_*` if unread — measured as in step 1); the
   file describes the target tier's variables only, grouped by service. `scripts/validate_env.py`
   (or the Makefile's `validate-env`) agrees with it.
5. **Makefile**: delete the targets that ran legacy surfaces (`create-schedule` if still
   present, `run` if it runs the legacy worker, `check-db` if it queries legacy tables); fix
   `install` to `pip install -e '.[cli]'`, `install-dev` to a real extra or a requirements file,
   `dev` to a local worker without production's health as a gate. `make help` lists what exists.
6. **`src/main.py`** docstring and `tests/src/test_worker_impl_gate.py` renamed to
   `test_worker_entrypoint.py`: the entrypoint imports `src.worker` only; no environment switch.
7. **`scripts/target_reachability.py`**: delete the `WORKER_IMPL` axis note; the worker
   entrypoint reaches the target root unconditionally.
8. **`.claude/settings.json`** allow/deny rules that still name legacy commands — owner-owned;
   list the lines in the PR for the owner (the sandbox refuses to write the file).

## Test Plan

- Red first: a settings test that `Settings()` constructs with no `TELEGRAM_*` variable in the
  environment; a test that `.env.example` names only variables `Settings` declares (parse both;
  every `NAME=` line has a field, every required field has a line) — red on the base because the
  three legacy lines have fields but the target tier's example is incomplete, or vice versa (read
  the current state; the assertion is exact agreement).
- `tests/test_agent_docs.py` still passes (the never-run list is unchanged: `python -m src.main`
  still starts the worker).
- Unit suite green with NO Telegram variables set in the run (the recipe change is the proof).
- Battery `tests/mutations/legacy_tear_out_02.sh`: re-add a required legacy field (killed by the
  construct-without-variables test); add a `.env.example` line for a variable no field declares
  (killed by the agreement test).

## Verification Checklist

- [ ] `env -i PATH=$PATH DATABASE_URL=… python -c "from src.config.settings import settings"` succeeds.
- [ ] `grep -rn "TELEGRAM_BOT_TOKEN\b\|TELEGRAM_CHANNEL_ID\|ADMIN_TELEGRAM_CHAT_ID" src scripts tests .github Makefile documentation AGENTS.md CLAUDE.md` → only CHANGELOG and archive.
- [ ] `grep -rn WORKER_IMPL src scripts tests` → 0.
- [ ] `make help` lists only targets that run; `make install` installs the CLI extra.
- [ ] CI green with the variables removed from the workflow.

## What NOT To Do

- Do not remove a settings field by name-guessing; every removal cites its zero-reader grep.
- Do not touch `TARGET_TELEGRAM_*`, the database fields, or the Railway variables — production
  reads them.
- Do not change the Procfile or `railway.toml`.
- Do not rewrite the runbooks here (phase 05); only the lines that set the dead variables.

## Context

Area: config, CI, Makefile · Effort: M · Risk: low (every change is measured) · Priority: after 01.
