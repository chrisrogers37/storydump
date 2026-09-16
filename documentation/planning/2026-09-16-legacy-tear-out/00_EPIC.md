---
title: "Retire the legacy tier — delete the code, snapshot and drop the schema, retire the settings (epic, #1216)"
type: plan
status: draft
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, migrations, worker, api, docs, epic]
links: [https://github.com/chrisrogers37/storydump/issues/1216]
---

## Summary

Nothing deployed runs the legacy tier: the worker dispatches to the target composition root
(`WORKER_IMPL=target`, since 2026-08-24) and the API is the target router (since 2026-08-31). What
survives is undeployed code (about 27,000 lines across `src/services/core`, `src/services/
integrations`, `src/repositories`, the non-target `src/models` and half of `src/utils`, plus the
legacy branch of `src/main.py` and roughly a hundred test files), the settings requirement every
process inherits from it (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `ADMIN_TELEGRAM_CHAT_ID`),
and the legacy **data** in the `legacy` schema — untransformed by ruling (`00` FC-7 §6), owed a
snapshot (M.3 step 3f) and a drop (3g) with the window's privilege stand-down (step 8). Five PRs
retire all of it, in the order the reversible precedes the irreversible: delete the code, retire
the settings and entry points, snapshot every legacy table into `archive`, ship the gated drop and
stand-down for the owner's hand, then bring the documentation to the end state. The v2 CLI
(#1310–#1312) already did #1216's step 4; the audit fold (#1314) did part of step 5.

## Evidence

- `src/main.py:340-358` — `main()` dispatches to `src.worker.main()` when `resolve_worker_impl`
  answers `WORKER_IMPL_TARGET`, else runs the legacy `main_async` loops; `Procfile` runs
  `python -m src.main`. `scripts/target_reachability.py` measures the worker entrypoint's closure
  and notes the default when the variable is unset is `legacy` — a boot fact, not a deploy fact.
- Sizes on `main` at `d8f5c72`: `src/services/core` 51 files / 14,949 lines; `src/services/
  integrations` 10 / 4,035; `src/repositories` 19 / 5,507; non-target `src/models` 16 / 1,258;
  `src/utils` 10 / 1,180; `src/main.py` 363. Tests: `tests/src/services/test_*.py` 56 files,
  `tests/src/repositories` 22, `tests/src/models` 11, `tests/src/utils` 10, `tests/integration`
  (three files import legacy repositories).
- No module under `src/services/target`, `src/api`, `src/channels` or `src/worker.py` imports
  `src.services.core`, `src.services.integrations`, `src.repositories` or a non-target
  `src.models` module (grep, 2026-09-16). The target tier imports `src.utils.logger` (15 sites),
  `src.utils.datetime_utils`, `src.utils.validators` (6), `src.utils.file_hash` (3),
  `src.utils.encryption`, `src.utils.media_kind`, `src.utils.resilience`; `image_processing` and
  `webapp_auth` have no non-legacy importer. `src/services/base_service.py` imports the
  repositories and has no target consumer.
- `src/config/settings.py:186-189` — the three Telegram variables are REQUIRED fields; the class
  comment says the legacy variable "survives only until #1222 retires it". `AGENTS.md` tells every
  entry point to set dummy values.
- `scripts/telegram_ratchet_baseline.json` — `core_telegram_modules` holds 15 entries; FC-2 clause
  3 requires that segment to reach empty; the baseline stores sets and is re-measured with
  `--write-baseline`.
- The 15 legacy tables: the 14 of `02` §9 (`category_post_case_mix`, `audit_log`, `api_tokens`,
  `instagram_accounts`, `chat_settings`, `media_items`, `onboarding_sessions`,
  `posting_history`, `user_chat_memberships`, `media_posting_locks`, `service_runs`,
  `user_interactions`, `posting_queue`, `users` — `src/models/*.py` `__tablename__`) plus
  `posting_history_dedup_archive` (#941, no disposition anywhere).
- `04-execution-sequence.md:97` — M.3 "3a–3d done; 3e abandoned; 3f, 3g and step 8 owed";
  `:199` 3f = `CREATE TABLE archive.<t>_pre_cutover_<YYYYMMDD> AS TABLE legacy.<t>` + `ALTER …
  OWNER TO svc_maintenance` for every legacy table, after `archive` exists; `:200` 3g = `DROP
  SCHEMA legacy CASCADE` as the last runner file; `:211-240` the success stand-down: a
  subject-identity guard, `DROP SCHEMA IF EXISTS window_ddl CASCADE`, the four `REVOKE`s, and the
  printed gate. `railway.toml` runs `python -m scripts.migration_runner apply` before EVERY
  deploy of either service — a pending runner file on `main` is applied by the next deploy.
- `tests/scripts/test_lineage_lane.py:273-372` enumerates the target lineage's files (ends at
  `077_service_token_subject.sql`); `tests/scripts/test_advertised_ddl.py:290` pins 35 normative
  blocks; `scripts/advertised_ddl_manifest.json` lists the `07` blocks (ordinal 21 = 077).
- The web's dashboard proxies `/api/dashboard/*` to the target router
  (`landing/src/app/api/dashboard/[...path]/route.ts:2`); #945's "the dashboard write path is
  legacy-backed and dies at 3g" predates the target write path and is verified stale in phase 01.
- Gate issues: #751 (F.4 posture) OPEN; #1202 (a guard for 3g) OPEN; #410 (App Review videos)
  OPEN; #941 OPEN; #739 (Facebook Login credential path, lands with the deletion) OPEN; #1205
  OPEN; #1195 CLOSED (the runner is armed); #841 CLOSED.

## Architecture

```
before                                              after
Procfile worker → src.main ─┬─ WORKER_IMPL=target → src.worker      Procfile worker → src.main → src.worker (only)
                            └─ legacy main_async (core loops)       src/services/core, integrations, repositories,
src/services/core|integrations|repositories|models (legacy)           non-target models, legacy utils: gone
settings: TELEGRAM_BOT_TOKEN … REQUIRED for every process           settings: target-prefixed variables only
production: public (target) + legacy (14 + 1 tables, untransformed) production: public (target) + archive.*_pre_cutover_20260916
                                                                    legacy schema absent; window_ddl gone; svc_* memberships revoked
```

- **Deletion is by package, checked by import.** Every deployed entrypoint (`src.main`,
  `src.api.app`, `src.worker`) must import cleanly with the packages gone; the reachability probe
  and CI's collection prove it. Shared utilities the target imports stay; the two legacy-only ones
  go.
- **The schema's exit is two runner files the deploy cannot run by itself.** 3g and the step-8
  stand-down are irreversible and gated on the owner (F6); they ship with a `-- runner:manual`
  directive the runner's `apply` skips unless asked for by name, so merging them arms nothing.
- **The snapshots are ordinary runner files.** 3f copies data into `archive` and hands the tables
  to `svc_maintenance`; it runs on the next deploy like any migration, adds storage, drops nothing.
- **Documentation follows the end state**, not the plan: every live page that describes a legacy
  table or command is rewritten or deleted in the last phase, with the never-run lists' parity
  test extended to the pages that carry them.

## Decision Forks

**F1 — The order: code before schema.** Context: #1216 lists the schema steps first; the code has
no deployed consumer either way. Options: (a) delete the code first (phases 01–02; reversible by
`git revert`), snapshot next, drop last; (b) snapshot and drop first, then delete. Lean: (a) —
each earlier phase is reversible and the irreversible one is last; nothing in the legacy code can
run against a `legacy` schema anyway, so keeping it "for the demo videos" (#1202) buys nothing —
the videos are recorded on the target tier. Ratifier: owner. Status: open.

**F2 — What `src/main.py` becomes.** Context: the Procfile runs `python -m src.main`, which
dispatches to `src.worker`; the never-run lists name `python -m src.main`. Options: (a) keep
`src/main.py` as a thin dispatcher that only calls `src.worker.main()` (the Procfile and the
never-run lists unchanged; `WORKER_IMPL` retired); (b) change the Procfile to `python -m
src.worker` and delete `src/main.py` (a Railway start-command change, the never-run lists renamed).
Lean: (a) — no deploy-time change in a deletion PR; the module's docstring says what it is.
Ratifier: owner. Status: open.

**F3 — `src/api/routes/retired.py`.** Context: it answers the Mini App's baked buttons with a
redirect to the web sign-in (410 without a front-end origin); #1216 lists it for deletion; the
buttons were minted until 2026-08-24 01:16 and real people hold them. Options: (a) keep it (it is
target-tier code with no legacy import; a bounded courtesy); (b) delete it and let the old URL
404. Lean: (a) until the owner says the buttons have aged out; it costs nothing and imports
nothing legacy. Ratifier: owner. Status: open.

**F4 — The fifteenth table.** Context: `posting_history_dedup_archive` (#941) exists in production
with no disposition; 3g drops it with the schema. Options: (a) snapshot it like the other
fourteen (one more `archive` table, dropped with the rest at 3g); (b) leave it out of 3f and let 3g
take it undocumented. Lean: (a) — the snapshot obligation is "every legacy table", and a
disposition is one row in the phase-03 table. Ratifier: owner. Status: open.

**F5 — The settings requirement (#1222).** Context: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`,
`ADMIN_TELEGRAM_CHAT_ID` are required by `Settings`; the target tier reads `TARGET_TELEGRAM_*`.
Options: (a) make the three optional in phase 02 and remove their readers with the code; (b)
delete the fields outright. Lean: (b) once phase 01 has removed every reader — a required field
nothing reads is a lie in the environment; CI, the Makefile and every doc that sets dummy values
lose the lines. Ratifier: owner. Status: open.

**F6 — How 3g and the stand-down are gated (#1202).** Context: the runner applies every pending
file before every deploy; a merged 3g would run on the next push. Options: (a) a `-- runner:manual`
directive: `apply` skips a manual file and reports it as owed; `apply --manual <version>` runs it
by name, in the ledger like any other; (b) keep the files out of `main` until the window and merge
them that day; (c) a gate row the file checks, raising when absent (a failed deploy is the
"guard"). Lean: (a) — mechanical, testable in the gate, and the ledger still records the
application; (b) is a process rule, (c) breaks deploys. Ratifier: owner. Status: open.

**F7 — Who runs the window.** Context: 3g and step 8 are one-shot, irreversible, and gated on
#410's videos existing or the target being able to record them (#1202). Options: (a) the owner
runs the window from the runbook (`railway run --service worker -- python -m scripts.migration_
runner apply --manual 079`, then the stand-down as `neondb_owner`), after the M.2 rehearsal on a
Neon branch; (b) an agent runs it under explicit instruction. Lean: (a). Ratifier: owner. Status:
open.

## Companion Plans

- `../2026-08-02-consolidated-design-plan/04-execution-sequence.md` — the M.3 window (3f, 3g,
  step 8) this epic completes; `00-fixed-constraints.md` FC-7 §6–§8 (legacy data untransformed).
- `../2026-08-17-m2-rehearsal-spec/README.md` — the rehearsal that governs 3g.
- `../2026-09-15-cli-v2/` — the CLI that replaced the legacy one (step 4, done).
- `../2026-09-16-cli-v2-audit/00_AUDIT.md` — the docs population this epic's last phase finishes.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| A target module reaches a deleted module transitively (a lazy import the grep cannot see) | the API or worker fails at boot | phase 01 imports every entrypoint in CI (`scripts/target_reachability.py` and the existing `test_worker_impl_gate`); the closure counts are pasted |
| A deleted legacy test was the only cover of a shared utility | a regression in `src/utils` | the kept utilities keep their tests; coverage of `src/utils` measured before and after |
| `archive` schema absent or owned wrongly in production | 3f fails at predeploy, deploy aborts with the old version serving | the replay gate creates the schema exactly as production has it; `storydump posture` reads the ledger before the merge |
| The snapshots copy large tables (`posting_history`, `user_interactions`) | Neon storage and a slow predeploy | sizes measured with `pg_total_relation_size` on the read-only probe before the merge; `CREATE TABLE … AS` is one statement per table |
| A merged 3g runs on the next deploy | irreversible data loss | F6 (a): the manual directive, proven by a gate that a manual file is NOT applied by `apply` |
| The window's stand-down revokes a membership something still uses | the API or worker loses a right | the stand-down's printed gate; the M.2 rehearsal on a branch first |
| #751's posture switch lands between phases | the ops gates' assumptions move | the gates run both arms (`svc_ingress` and bypass) already |

## Complexity and Sequencing

| Phase | Doc | Size | Depends on | Parallel with |
|---|---|---|---|---|
| 01 delete the legacy code and its tests | `01_delete-the-code.md` | XL | — | — |
| 02 retire the settings, the entry point and the config | `02_settings-and-entry-points.md` | M | 01 | — |
| 03 the 3f snapshot migrations | `03_snapshot-migrations.md` | M | — (ratchets serialise it after 02) | — |
| 04 the gated drop and stand-down | `04_drop-and-stand-down.md` | L | 03; F6, F7 | — |
| 05 the documentation's end state | `05_docs-end-state.md` | M | 02, 04 | — |

Critical path: 01 → 02 → 03 → 04 → 05. Every phase is one PR; squash merges; no stacking.

## Goal condition

Answered from the ledger: (1) no deployed entrypoint imports a legacy module (the grep is empty
and the reachability probe imports all three); (2) the `legacy` schema is absent in production,
every one of the 15 tables has an `archive.*_pre_cutover_*` snapshot owned by `svc_maintenance`,
and the step-8 success gate printed green — pasted by the owner who ran it; (3) the suite is green
with the legacy tests deleted, not skipped; (4) the F.6 core segment is empty; (5) no live
document or never-run list names a legacy table, command or variable.
