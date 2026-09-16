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
survives is undeployed code (about 28,000 lines across `src/services/core`, `src/services/
integrations`, `src/services/media_sources`, `src/repositories`, the non-target `src/models`, the
legacy sync engine `src/config/database.py` and six of the ten `src/utils` modules, plus the
legacy branch of `src/main.py`, two scripts and about 117 test files), the settings requirement every
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
  `src/services/media_sources` 5 / 946; `src/utils` 10 / 1,180; `src/main.py` 363. Tests: 117
  of the 265 `test_*.py`/`conftest.py` files under `tests/` match the deletion grep of 2026-09-16
  (`tests/src/services` 61 incl. `media_sources/` and `conftest.py`, `tests/src/repositories` 22,
  `tests/src/models` 11, `tests/src/utils` 6, four `tests/integration` files, and the fifteen
  named below) — the AST rule at build time is the exact set.
- No module under `src/services/target`, `src/api`, `src/channels` or `src/worker.py` imports
  `src.services.core`, `src.services.integrations`, `src.services.media_sources`,
  `src.repositories`, `src.config.database` or a non-target `src.models` module (grep,
  2026-09-16, re-run after both ironclad lenses). `src/services/media_sources/` is imported ONLY
  by five `src/services/core` modules and `src/services/integrations/google_drive.py` — the
  target's `drive_adapter.py:13` names it in a docstring, not an import — so it goes (with
  `tests/src/services/media_sources/`, 4 files); its `factory.py:149` imports
  `src.services.core.settings_service` inside a function, which is why the AST test walks nested
  imports. Of `src/utils`, the target tier imports `logger` (15 sites), `datetime_utils`
  (`src/services/target/transit.py`) and `encryption` (`src/services/target/ig_login_oauth.py`);
  `validators`, `file_hash` (its one non-legacy importer is the legacy model
  `src/models/media_item.py`), `media_kind`, `resilience`, `image_processing` and `webapp_auth`
  have no importer under `src/` or `scripts/` outside the legacy packages and go. Two surviving
  tests take a constant from them — `tests/scripts/conftest.py:42` and
  `tests/scripts/test_schema_drift_live.py:74` import `MIGRATIONS_DIR` from `validators`,
  `tests/src/services/target/test_google_drive_adapter.py:35` imports `INSTAGRAM_VIDEO_SUFFIXES`
  from `media_kind` — re-homed in phase 01. `src/config/database.py` (the legacy sync engine,
  `Base`, `get_db`, `init_db`) has no importer under the target tier (`unit_of_work.py:22` says
  retiring it is M.3's); its importers are the legacy packages, `src/utils/{validators,
  resilience}.py`, `scripts/init_db.py`, `scripts/backfill_memberships.py` (a backfill of the
  legacy `user_chat_memberships` that reads `settings.TELEGRAM_BOT_TOKEN` at `:124`) and tests —
  all four go. `src/services/domain/` is an empty package. `src/services/base_service.py`
  imports the repositories and has no target consumer. `src/worker_impl.py` holds
  `WORKER_IMPL_*` and `resolve_worker_impl` (importers: `src/main.py`,
  `scripts/target_reachability.py:466-470`, their tests). `src/exceptions/__init__.py:4-21`
  imports the legacy `backfill`, `google_drive` and `instagram` exception modules on every
  tenancy import (the 2026-09-16 audit's architecture lane); `src/utils/logger.py:7` loads
  `settings`, which is how every process inherits the legacy requirement below.
  `python-telegram-bot` and `Pillow` (`requirements.txt:15,20`, `setup.py:21,27`) have no
  importer outside the legacy packages in `src`, `scripts`, `storydump_cli` or `tests`.
- Legacy importers under `tests/` outside the obvious directories (the fifteen): `tests/conftest.py`
  (`:42` imports the legacy `Base`, `:745-757` `create_all`/`drop_all` the legacy schema for the
  unit-test database, `:806-818` `route_repos_to_test_db`, `:837,851` a legacy repository inside
  two fixtures), `tests/src/services/conftest.py:7-8` (module-level `src.services.core` imports —
  every test under `tests/src/services/target/` collects through it), `tests/src/
  test_health_endpoint.py` (its whole subject is `src.main._build_health_response`, the legacy
  loops' liveness server), `tests/src/test_main_scheduler_loop.py`, `tests/src/
  test_periodic_scheduler.py`, `tests/src/test_worker_impl_gate.py`, `tests/src/exceptions/
  test_{backfill,google_drive,instagram}_exceptions.py`, `tests/src/config/test_database.py`,
  `tests/src/api/test_security_hardening.py:53,69` (`ConfigValidator`, which runs in no deployed
  process), four files under `tests/integration/` (`test_callback_authorization_gate`,
  `test_commit_refresh_sweep`, `test_tenant_provisioning_door`, `test_track_execution_leak`) and
  `tests/integration/conftest.py:23` (`src.config.database` inside a fixture), `tests/scripts/
  test_lineage_lane.py:207-211` (`legacy_declared_tables()` reads the legacy models' `Base`; used
  by `:426-470` and `:893-902`), `tests/scripts/test_migration_gate.py:339-353` (the replayed
  legacy schema compared to the legacy models), and `tests/scripts/test_target_reachability.py:540`
  (a legacy module name used as a specimen). The DB gates are therefore NOT untouched by phase 01.
- `src/config/settings.py:192-194` — the three Telegram variables are REQUIRED fields, and
  `settings.py:400` instantiates the class at import, so `src.main` (through `src/utils/logger.py:7`)
  cannot import without them until phase 02; the class comment says the legacy variable "survives
  only until #1222 retires it". `AGENTS.md` tells every entry point to set dummy values.
- `scripts/telegram_ratchet_baseline.json` — `core_telegram_modules` holds 15 entries; FC-2 clause
  3 requires that segment to reach empty; the baseline stores sets and is re-measured with
  `--write-baseline`.
- The 16 legacy tables: the 14 of `02` §9 (`category_post_case_mix`, `audit_log`, `api_tokens`,
  `instagram_accounts`, `chat_settings`, `media_items`, `onboarding_sessions`,
  `posting_history`, `user_chat_memberships`, `media_posting_locks`, `service_runs`,
  `user_interactions`, `posting_queue`, `users` — `src/models/*.py` `__tablename__`), plus
  `posting_history_dedup_archive` (#941, no disposition anywhere) and `schema_version` (the
  legacy lineage's own ledger, `02` §9's "+ ledger") — the two F4 dispositions.
- `04-execution-sequence.md:97` — M.3 "3a–3d done; 3e abandoned; 3f, 3g and step 8 owed";
  `:199` 3f = `CREATE TABLE archive.<t>_pre_cutover_<YYYYMMDD> AS TABLE legacy.<t>` + `ALTER …
  OWNER TO svc_maintenance` for every legacy table, after `archive` exists; `:200` 3g = `DROP
  SCHEMA legacy CASCADE` as the last runner file; `:211-240` the success stand-down: a
  subject-identity guard, `DROP SCHEMA IF EXISTS window_ddl CASCADE`, the four `REVOKE`s, and the
  printed gate. `railway.toml` runs `python -m scripts.migration_runner apply` before EVERY
  deploy of either service — a pending runner file on `main` is applied by the next deploy, and
  a FAILING one aborts both services' deploys (an API hotfix included) until fixed forward.
- The runner, read for this plan: `scripts/migration_runner.py:72-75` knows four markers
  (`no-transaction`, `postcondition`, `reapply-safe`, `schema-move`); `_parse_markers`
  (`:119-136`) IGNORES an unknown `runner:` line; `apply_pending` (`:479-487`) raises for ANY
  pending file numbered below the applied head unless it is `reapply-safe`, before the apply
  loop (pinned by `tests/scripts/test_migration_runner.py:172`); `_connect` (`:316-319`) uses the
  DSN as given — no `SET ROLE` — so production's runner acts as `DATABASE_URL`'s login, the
  database owner (`railway.toml`), and every file through 077 was applied as that role
  (`documentation/operations/migration-runner.md:88-94`; `00` FC-7 §7: "the D40 privilege split
  was never armed"); CI's lane applies the post-move files as `svc_migration`
  (`tests/scripts/conftest.py:709-716`). `repair` (`:696-706`) only UPDATEs an existing ledger
  row. `_load_manifest` (`:521-577`) pairs every corpus file with adoption evidence: a file
  carrying `runner:postcondition` lines needs no manifest entry.
- The prefix ratchet: `scripts/advertised_ddl.py:307-334` — `target_lineage_files` is EVERY
  numbered file above the 051 move and `target_lineage_statements` every statement of them;
  `tests/scripts/test_advertised_ddl.py:414-440` requires that list to be a positional prefix of
  the advertised stream, which is replayed from an EMPTY `public` with no `legacy` schema
  (`conftest.py:594-616`); `test_lineage_lane.py:95-123` slices the stream by the same length
  ("ONE HOME FOR THE PREFIX RULE"). The manifest's `_comment` classifies `07` BLOCKS by hash and
  carries no rule for a file. A snapshot, a drop or a stand-down cannot be in the stream and
  cannot be outside it without a file rule — phase 03 adds one.
- Door files after the window hand functions to service roles with `ALTER FUNCTION … OWNER TO
  svc_*`: `062:166`, `063:194`, `064:71`, `068:145-147`, `076:81` — five of the seventeen
  post-window files. The statement needs membership of the new owner (or superuser), which the
  window bootstrap's transient grants supply (`scripts/window/step0_bootstrap.sql:58,73`) and the
  printed stand-down revokes (`04:224-226`). Whether the bootstrap ever ran in production is
  unmeasured (phase 03 step 1 measures it).
- `05-operational-numbers.md:90` gives the M.3 snapshot tables the `archive_snapshots` retention
  class: 90 days, `DROP TABLE`; `059:440-441` already parses `<table>_pre_cutover_<ymd>` for it;
  the `retention_sweep` executor is unbuilt (`src/services/target/work_loop.py:225-228`).
- `src/services/target/ops_views.py:355-363` — `posture`'s RLS list reads `public` only, so "no
  `legacy` table in `rls`" is true before and after 3g; the ledger view (`:346-349`) carries no
  actor column. Production is PostgreSQL 17.10 (#787, cited by the M.2 spec README), so the
  stand-down gate's PG16+ branch is the one that applies.
- #1202's own words: the constraint is an ordering, and "either leg satisfies it: record the
  Track 3 videos, or arm and verify the target tier". The target tier has served since
  2026-08-24 with a real publish path; `documentation/operations/meta-app-review.md:32-47`
  carries the standing constraint in prose. #410's last comment is 2026-08-19 ("nothing
  submitted").
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
production: public (target) + legacy (16 tables, untransformed)   production: public (target) + archive.*_pre_cutover_<date> (16)
                                                                    legacy schema absent; window_ddl gone; the stand-down per F8
```

- **Deletion is by package, checked by import.** Every deployed entrypoint (`src.main`,
  `src.api.app`, `src.worker`) must import cleanly with the packages gone; the reachability probe
  and CI's collection prove it. Shared utilities the target imports stay; the six legacy-only ones
  go, and the two constants surviving tests took from them are re-homed.
- **The schema's exit is two runner files the deploy cannot run by itself.** 3g and the step-8
  stand-down are irreversible and gated on the owner (F6); they ship with a `-- runner:manual`
  directive the runner's `apply` skips unless asked for by name, so merging them arms nothing;
  a manual file is exempt from the runner's below-head rule, so a later ordinary file does not
  wedge the deploys behind it.
- **The snapshots are ordinary runner files.** 3f copies data into `archive` and hands the tables
  to `svc_maintenance`; it runs on the next deploy like any migration, adds storage, drops nothing.
  It carries a non-stream marker so the advertised-DDL prefix ratchet excludes it by rule.
- **Documentation follows the end state**, not the plan: every live page that describes a legacy
  table or command is rewritten or deleted in the last phase, with the never-run lists' parity
  test extended to the pages that carry them.

## Decision Forks

**F1 — The order: code before schema.** Context: #1216 lists the schema steps first; the code has
no deployed consumer either way. Options: (a) delete the code first (phases 01–02; reversible by
`git revert`), snapshot next, drop last; (b) snapshot and drop first, then delete. Lean: (a) —
each earlier phase is reversible and the irreversible one is last; nothing deployed runs the
legacy code (a `search_path` override could point it at `legacy`; nothing does), so keeping it
"for the demo videos" (#1202) buys nothing — the videos are recorded on the target tier.
Ratifier: owner. Status: open.

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

**F4 — The fifteenth and sixteenth tables.** Context: `posting_history_dedup_archive` (#941)
exists in production with no disposition, and `schema_version` — the legacy lineage's own ledger
— is "+ ledger" in `02` §9's count; 3g drops both with the schema. Options: (a) snapshot both
like the other fourteen (sixteen `archive` tables; `schema_version` is the record of which
legacy migrations ran, worth one small table); (b) snapshot the fifteen and let `schema_version`
die (the runner's ledger already records the adoption of 001–050); (c) leave the fifteenth out
of 3f and let 3g take it undocumented. Lean: (a) — the snapshot obligation is "every legacy
table", and a disposition is one row in the phase-03 table. Ratifier: owner. Status: open.

**F5 — The settings requirement (#1222).** Context: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`,
`ADMIN_TELEGRAM_CHAT_ID` are required by `Settings`; the target tier reads `TARGET_TELEGRAM_*`.
Options: (a) make the three optional in phase 02 and remove their readers with the code; (b)
delete the fields outright. Lean: (b) once phase 01 has removed every reader — a required field
nothing reads is a lie in the environment; CI, the Makefile and every doc that sets dummy values
lose the lines. Ratifier: owner. Status: open.

**F6 — How 3g and the stand-down are gated (#1202).** Context: the runner applies every pending
file before every deploy; a merged 3g would run on the next push. Options: (a) a `-- runner:manual`
directive: `apply` skips a manual file and reports it as owed; `apply --manual <version>` runs it
by name, in the ledger like any other — AND a manual file is exempt from the below-head rule in
both doors (`apply` skips it where it stands; `--manual` applies it below the head), AND an
unknown `runner:` marker is a hard error (a misspelt `manaul` is otherwise an ordinary file
applied at the next deploy); (b) keep the files out of `main` until the window and merge them
that day; (c) a gate row the file checks, raising when absent (a failed deploy is the "guard").
Lean: (a) — mechanical, testable in the gate, and the ledger still records the application; (b)
is a process rule, (c) breaks deploys. Ratifier: owner. Status: open.

**F7 — Who runs the window.** Context: 3g and step 8 are one-shot, irreversible, and gated on
#410's videos existing or the target being able to record them (#1202). Options: (a) the owner
runs the window from the runbook (`railway run --service worker -- python -m scripts.migration_
runner apply --manual 079`, then `apply --manual 080` — both as `DATABASE_URL`'s owner login,
which is what the runner connects as), after the M.2 rehearsal on a Neon branch; (b) an agent
runs it under explicit instruction. Lean: (a). Ratifier: owner. Status: open.

**F8 — What the stand-down revokes.** Context: `04:213-240`'s success variant revokes the four
`svc_*` memberships from `svc_migration` and `svc_migration` from the owner login; but door files
after the window hand functions to service roles with `OWNER TO svc_*` (five files since the
window, `062`–`076`), which needs membership of the new owner; production's runner IS the owner
login, not `svc_migration`; and whether the bootstrap's grants ever ran in production is
unmeasured (phase 03 step 1 measures `pg_auth_members`, `admin_option` and `nspowner`). The
printed stand-down would certify, as "steady state", the shape in which the next door migration
aborts both services' deploys. Options: (a) partial stand-down — drop `window_ddl`, revoke
`CREATE ON DATABASE` from `svc_migration` (the legacy-schema grants die with the tables at 3g),
KEEP the memberships door files need, and record a D40 amendment in `03-decision-record.md`
beside the one that already records the window running outside its runbook (`03:189`); (b) the
full stand-down as printed, and every future door file self-brackets its `OWNER TO` with `GRANT
svc_x TO current_user … REVOKE` (legal for the role's creator on PG16+); (c) defer 080 — ship 3g
alone and leave the stand-down to the F.4 posture increment (#751), whose end state D40's is by
`03:189`'s own words. Lean: (a) — the gate then asserts what actually holds (`window_ddl` absent,
`legacy` absent, no `svc_*` role a member of anything, the owner's memberships as measured
before and asserted after); (b) taxes every future migration to keep a shape nothing enforces;
(c) leaves the door standing. Whichever is chosen, the rehearsal applies ONE door-replacing file
after the stand-down as the positive control. Ratifier: owner. Status: open.

**F9 — The snapshots' lifetime.** Context: `05-operational-numbers.md:90` gives `archive`
snapshot tables the `archive_snapshots` class — 90 days, then `DROP TABLE` — and `059:440-441`
already parses `<table>_pre_cutover_<ymd>` for it; the `retention_sweep` executor is unbuilt
today, so nothing drops them, but the day it is built phase 05's "the snapshots are the legacy
backup" is false at day 91. Options: (a) 90 days per the ratified table — the snapshots are a
backstop for the drop, not an archive; 078's header, the backup page and the runbook state the
date they become eligible and the owner's export option before it (`pg_dump -n archive` to cold
storage); (b) indefinite — exempt `*_pre_cutover_*` from the class (a `059` amendment and a `05`
row) and say so. Lean: (a) — this plan does not silently extend a ratified retention; the owner
picks (b) knowing its cost is a door change. Ratifier: owner. Status: open.

## Companion Plans

- `../2026-08-02-consolidated-design-plan/04-execution-sequence.md` — the M.3 window (3f, 3g,
  step 8) this epic completes; `00-fixed-constraints.md` FC-7 §6–§8 (legacy data untransformed).
- `../2026-08-17-m2-rehearsal-spec/README.md` — the rehearsal that governs 3g.
- `../2026-09-15-cli-v2/` — the CLI that replaced the legacy one (step 4, done).
- `../2026-09-16-cli-v2-audit/00_AUDIT.md` — the docs population this epic's last phase finishes;
  it lands with PR #1314 (the audit fold), which merges before this plan's first phase begins —
  until then it is readable on that PR's branch.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| A target module reaches a deleted module transitively (a lazy import the grep cannot see) | the API or worker fails at boot | phase 01 imports every entrypoint in CI (`scripts/target_reachability.py` and the existing `test_worker_impl_gate`); the closure counts are pasted |
| A deleted legacy test was the only cover of a shared utility | a regression in `src/utils` | the kept utilities keep their tests; coverage of `src/utils` measured before and after |
| `archive` schema absent or owned wrongly in production | 3f fails at predeploy, deploy aborts with the old version serving | the replay gate creates the schema exactly as production has it; `storydump posture` reads the ledger before the merge |
| The snapshots copy large tables (`posting_history`, `user_interactions`) | Neon storage and a slow predeploy | sizes measured with `pg_total_relation_size` on the read-only probe before the merge; `CREATE TABLE … AS` is one statement per table |
| A merged 3g runs on the next deploy | irreversible data loss | F6 (a): the manual directive, proven by a gate that a manual file is NOT applied by `apply` |
| A pending manual file numbered below the applied head, once any later file lands | `apply` raises before applying anything; BOTH services' predeploys fail on every push until the owner runs the window | F6 (a)'s exemption: manual files are skipped where they stand; a unit test applies a LATER file with 079 pending and expects exit 0 |
| The snapshot migration fails in production's predeploy (an unmeasured table, a missing membership, a timeout on the big tables) | both services' deploys — an API hotfix included — blocked until fixed forward; the copy's wall-clock is paid inside the predeploy | the gate seeds production's shape (the sixteenth table from the pasted `\d`, a row per table) and runs 078 as the OWNER actor; the owner rehearses 078 on a PITR branch and records the wall-clock before merging |
| The stand-down revokes the membership door files need (`OWNER TO svc_*`, five files since the window) | the next door migration aborts both services' deploys; the printed gate certifies the break | F8; the rehearsal applies one door-replacing file AFTER the stand-down |
| The backout "PITR to the marker" discards every target write after the marker | lost posts, cards and audit rows if the worker kept running | the runbook's first line stops the worker service; the marker is taken immediately before 079; the window is announced |
| The `archive_snapshots` class drops the snapshots at day 91 once the sweep is built | the legacy backup vanishes silently | F9; 078's header and the backup page state the decision |
| `posting_history_dedup_archive` exists only in production (nothing in the tree declares it, #941) | 078 fails on "relation does not exist" in CI, or the inventory is wrong in production | phase 03 step 1 pastes `\d`; the gate's fixture creates it from that DDL; 078 names it unconditionally so an inventory error fails in CI, never silently |
| #751's posture switch lands between phases | the ops gates' assumptions move | the gates run both arms (`svc_ingress` and bypass) already |

## Complexity and Sequencing

| Phase | Doc | Size | Depends on | Parallel with |
|---|---|---|---|---|
| 01 delete the legacy code and its tests | `01_delete-the-code.md` | XL | #1314 merged; F1–F3 ratified | 03 |
| 02 retire the settings, the entry point and the config | `02_settings-and-entry-points.md` | M | 01; F5 ratified | 03 |
| 03 the 3f snapshot migration and the ratchet's file rule | `03_snapshot-migrations.md` | M | F4, F9 ratified (no code phase: the snapshot reads `legacy.*` whatever the code) | 01, 02 |
| 04 the gated drop and stand-down | `04_drop-and-stand-down.md` | L | 03 applied in production; F6–F8 ratified; #1202 ruled closed by the owner | — |
| 05 the documentation's end state | `05_docs-end-state.md` | M | 02, 04; #1314 merged | — |

Critical path: 01 → 02 → 04 → 05, with 03 alongside and before 04. Every phase is one PR;
squash merges; no stacking; PRs merge one at a time in the order they are ready (03 may merge
between 01 and 02). Two sizing decisions, stated: 01 stays ONE PR (the import chain `src.main →
core → repositories → models → database` leaves an entrypoint broken or a package untested under
any split; the reference class, #1312's 90-file deletion, was reviewed by its import proof, not
its line count — a reviewer who wants two halves splits `tests/` from `src/`, never `src/` in
two); 04 stays ONE PR (the runner's manual-skip path is observable only once a manual file
exists, so a runner-only PR proves nothing its unit tests do not). Migration numbers 078/079/080
assume no other migration lands first: a file is named by its role — the snapshot, the drop, the
stand-down — and renumbered above the head at merge; 079 reads the date from 078's name.

## Implementation Plan

### Dependencies
The owner ratifies F1–F9 (each phase's own Dependencies name the forks it needs) and, before
phase 04, rules #1202 closed on its "or" leg (the target tier is armed and serving; the owner
confirms a connected destination exists on it). PR #1314 (the audit fold) merged, so the audit
document the last phase reads exists on `main`. #751 (the F.4 posture) is independent by
#1216's own words and does not gate this plan; the gates run both arms already.

### Blocks
#1216; the closures it lists (#941, #945, #739, #1205, #1222; #1046/#1113 where they concern
`legacy`-schema instruments).

### Steps
0. The owner ratifies the nine forks in this document (`Status: locked`, ratifier and date).
1. Phase 01 — `01_delete-the-code.md` (one PR).
2. Phase 02 — `02_settings-and-entry-points.md` (one PR, after 01).
3. Phase 03 — `03_snapshot-migrations.md` (one PR: the snapshot file, the ratchet's file rule and
   the unknown-marker error; alongside 01/02, before 04).
4. Phase 04 — `04_drop-and-stand-down.md` (one PR shipping the files, the runner's `--manual`
   and the runbook; the window itself is the owner's, F7).
5. Phase 05 — `05_docs-end-state.md` (one PR, last).
Each phase under the repository's process: tests red first, two review lenses, a fold, a fresh
re-verify, a named mutation per behaviour in `tests/mutations/legacy_tear_out_0N.sh`, CI green,
an admin squash; the sprint ledger `RUN_LOG.md` beside this file re-checks the invariants below
after every merge.

## Test Plan

- Per phase: the phase's own Test Plan (red first) and battery; the whole suite green with no
  new skip; the DB gates and the lineage replay green.
- Cross-phase invariants, re-checked after every merge: I1 the target suite green with nothing
  skipped; I2 every migration in the lineage list, and in the advertised stream unless it carries
  a non-stream marker (`manual`, `unadvertised`) — `test_advertised_ddl`'s pin stays 35 through
  this plan; I3 no module under `src/services/target`, `src/api`, `src/channels`, `src/worker.py`
  imports a legacy package (grep = 0); I4 the FC-2 ratchet baseline never grows, and its core
  segment is empty from phase 01 on; I5 no runner file on `main` is applied by a deploy unless it
  is meant to be (a manual file stays owed, and does not wedge `apply`); I6 the never-run lists
  agree (`tests/test_agent_docs.py`); I7 the worker entrypoint imports `src.worker` and nothing
  legacy; I8 every `runner:` marker in the corpus is one the runner knows (from phase 03 on).

## Verification Checklist

Agent-run:
- [ ] `grep -rn "src.services.core\|src.services.integrations\|src.repositories" src scripts storydump_cli tests` → 0 lines (phase 01).
- [ ] `python -c "import src.main, src.api.app, src.worker"` succeeds with no `TELEGRAM_*` variable set (phase 02).
- [ ] `python scripts/telegram_ratchet.py` passes with `core_telegram_modules` empty (phase 01).
- [ ] `pytest tests/ -q` green, the collected count pasted before and after, `-rs` shows no new skip (every phase).
- [ ] `python -m scripts.migration_runner status` on a checkout after phase 04's merge lists 079 and 080 as owed (manual), and a deploy's predeploy log shows them skipped by name.
- [ ] The step-1 grep of phase 05 finds a legacy table, module or variable only under `documentation/archive/`, `CHANGELOG.md` and `documentation/planning/`.

Owner-run (pasted into the ledger):
- [ ] The read-only production probe lists exactly the 16 tables of F4 before phase 03 merges, with their sizes.
- [ ] Before phase 03 merges: the actor probe of phase 03 step 1 (`version()`, `current_user`, the `svc_*` membership graph, `nspowner` of `legacy`/`archive`/`public`) pasted; 078 rehearsed on a PITR branch with its wall-clock.
- [ ] After phase 03's deploy: 16 `archive.*_pre_cutover_*` tables owned by `svc_maintenance`, row counts equal to their sources; `storydump posture` shows the snapshot file applied.
- [ ] The M.2 rehearsal of 079/080 on a Neon branch, green, wall-clock recorded, with one door-replacing file applied after the stand-down.
- [ ] Production: `SELECT count(*) FROM pg_namespace WHERE nspname = 'legacy'` → 0 (the owner's query — `posture`'s RLS list is `public`-only and cannot show this); the stand-down gate's queries answer as printed; `storydump posture` shows 079 and 080 `applied` and no `window_ddl` door.

This satisfies #1216's acceptance list clause by clause: A1 ("no deployed entrypoint imports a
legacy module; `scripts/target_reachability.py` shows the legacy closure empty") — the probe has
no legacy axis; a deleted package is in no closure, and `tests/src/test_legacy_tier_gone.py` is
the standing guard; A2 the schema and the snapshots (owner-run above); A3 (the CLI over the API,
no `src.services.core` import under `cli/`) is moot since #1312 deleted `cli/`; A4 the suite
green with the legacy tests deleted, not skipped.

## What NOT To Do

- Do not stack the phases' branches (squash merges make a stacked child conflict); branch each
  from `main` after the previous merge.
- Do not run 079 or the stand-down from an agent session, on any branch that is not the owner's
  rehearsal (F7); do not merge phase 04 without the manual directive proven in the gate (F6).
- Do not touch `public` in any migration of this plan; do not drop `archive` or a snapshot.
- Do not change the Procfile, `railway.toml` or a Railway variable inside a PR — a variable
  removal is the owner's step, listed in phase 02.
- Do not delete a module by name-guessing; every deletion cites its zero-importer grep.
- Do not delete `scripts/setup_database.sql` (the lineage lane's legacy seed) or
  `scripts/window/step0_bootstrap.sql` (the window's bootstrap) — SQL that describes the legacy
  lineage is the replay's input, not legacy code.
- Do not write a `07` block or a manifest row for a snapshot, a drop or a stand-down; they are
  not advertised DDL, and their `runner:postcondition` lines are their adoption evidence.

## Context

Area: worker, models, migrations, config, docs · Effort: XL in total (01 XL, 02 M, 03 M, 04 L,
05 M) · Risk: low for 01–03 and 05 (reversible), high for 04 (mitigated by the guard, the
rehearsal and PITR) · Priority: next after the audit fold; the owner's window sets 04's date.
