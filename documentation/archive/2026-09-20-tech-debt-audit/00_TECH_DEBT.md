---
title: "Tech-debt audit of the one tier — 75 findings, 16 cleanup PRs, 12 defects flagged for rulings"
type: audit
status: completed
owner: chris
created: 2026-09-20
tags: [audit, tech-debt, worker, pipeline, api, cli, services, tests, ci, landing]
links: []
---

# Tech-debt audit — storydump, whole repo (2026-09-20)

> **Archived 2026-09-21 — COMPLETED.** All sixteen cleanup PRs merged and live: 01 #1336 ·
> 02 #1343 · 03 #1345 · 04 #1346 · 05 #1351 · 06 #1356 · 07 #1353 · 08 #1354 · 09 #1355 ·
> 10 #1337 · 11 #1339 · 12 #1352 · 13 #1340 · 14 #1338 · 15 #1344 · 16 #1350, plus #1335
> (these plan docs), #1342 and #1348 (two defects the sprint introduced and caught) and #1347
> (the ledger). The frozen baseline is unregressed — the final tree runs **3,679 passed** with the
> same two macOS-only failures and two explained skips the sprint began with.
>
> **The flagged defects grew from twelve to thirteen and none of them was fixed here.** Doc 02's
> re-homing of the RLS gate onto the live writers surfaced the thirteenth: `identity.py:83` erases
> a stored display name when a Google token omits `name`. Each remains owed a ruling or a bug-fix
> PR. See [`RUN_LOG.md`](RUN_LOG.md) §7 for the queue and §8 for the closeout.
>
> **None of the sixteen docs was executed exactly as written.** Roughly sixty corrections are
> recorded across the PR bodies and in `RUN_LOG.md`: line numbers that had moved, greps that were
> false when written, counts that were wrong, and about a dozen "these are duplicates" claims that
> turned out to be two things that merely looked alike. One *withdrawal* was also wrong and was
> reinstated (doc 08's cap-warning block). Read this document with that caveat: its structural
> findings held up; its duplication claims needed checking one by one.
>
> **Update 2026-09-22 — all thirteen are resolved, and the sixteen phase docs are deleted.** After the
> closeout the defects were worked as their own PRs: A1 #1384 · C1 #1377 · B4 #1376 · B3 #1380 ·
> C5 #1375 · B2 #1387 + #1388 · B17 #1390 · B16 #1391 · D2 #1383 · D11 #1382 · D10 #1385 · the
> thirteenth (the display-name erasure) #1374. B19, the two advisory-lock hash widths, was ruled
> deliberate — measured, said at each call site and ratcheted — rather than changed (#1389). With
> every phase merged, `01_…`–`16_…` were deleted from this folder: `RUN_LOG.md` keeps the per-phase
> record, and each doc is in git history
> (`git show 29acea2ec4d9cf72b5e6d8ddcbff23080d4da06e:documentation/archive/2026-09-20-tech-debt-audit/<file>`).

## Summary

The `/artemis-skills:audit tech debt` lens, run on `main` at `0966771` three days after the
tear-out's last phase merged (#1216) and one day after the owner's window dropped the `legacy`
schema (079, 2026-09-19). Four Explore agents read the Python tree in full (`src/` 32,773 lines,
`storydump_cli/` 4,792, `scripts/` 6,397) and `landing/` (21,589 lines of TS/TSX); the
orchestrator covered `scripts/`, the test scaffolding, packaging and CI directly and re-opened
every High against the code. **79 raw findings, 75 after merging the four that two agents reached
independently** — 15 labelled High, 48 Medium, 12 Low — plus **12 defects** the scan surfaced
that change behaviour to fix and therefore leave this lens for owner rulings or bug-fix PRs.

The shape of the debt is two-fold. The tear-out left a dead sync-writer lane (~680 lines nothing
in `src/` calls, whose RLS gate is the only thing still proving it) and a layer of docstrings that
describe code that no longer exists. Independently, the tier grew by copying: the same constant,
predicate, fan-out loop, error-translation idiom or SQL shape spelled at 3–13 sites beside the one
module that already owns it — `vocabulary.py`, `intent_ledger.TERMINAL_STATES`,
`jobs.LANE_BUDGETS`, `outbox.fanout_notification`, `unit_of_work.asyncpg_url`, `RefusalError`.
Two of those copies have already drifted into user-visible defects (`storydump health` renders
blank tap counts; `/health.version` says `0.2.0` beside a `1.6.0` package). The remediation
therefore leads with *one spelling* and the dead-lane deletion, and only then decomposes the three
1,000–2,000-line modules.

**Objective baseline.** 0 `TODO/FIXME/HACK/XXX` markers in Python (one counsel note in
`landing/`); the project's ruff policy (`E4,E7,E9,F`) clean; `tsc --noEmit` and `eslint` clean; an
advisory ruff pass (not policy) counts 39 functions over the complexity threshold, 24 over 50
statements, 99 with more than 5 parameters, and 56 in-function imports in `src/`; 8 packages in
`requirements.txt` that nothing imports.

## Why it matters

This system posts on behalf of paying tenants, and its own design doctrine is *one authority per
rule* (the database for state, `vocabulary.py` for spellings, the command port for writes). Every
hand copy of a constant or a predicate is a second authority waiting to disagree, and two already
do. The dead lane is worse than dead: the tenancy gate that should be proving the live writers
run under RLS as `svc_ingress` proves the retired twin instead, so a regression in the live path
has no test to fail.

## Scope and method

| Slice | Read by | Lines | Report |
|---|---|---|---|
| Pipeline / worker / command side (`publish_pipeline`, `outbox`, `work_loop`, `command_executors`, `jobs`, `scheduler`, `transit`, `reconciler`, `provider_ops`, `publish_cap`, `backpressure`, `rate_counters`, `intent_ledger`, `usage_precheck`, `telegram_dispatch`, `invitation_cards`, `prompts`, `commands`, `vocabulary`, `worker.py`, `main.py`) | Explore agent A | ≈12,600 | `research/pipeline-worker.md` |
| Integrations / identity / tenancy / data access (36 modules under `src/services/target/`) | Explore agent B | 11,719 | `research/integrations-identity.md` |
| API, channels, config, models, utils, exceptions, the health views, the CLI | Explore agent C | ≈13,500 | `research/api-channels-cli.md` |
| `landing/` (Next.js) | Explore agent D | 21,589 | `research/landing.md` |
| `scripts/`, tests scaffolding, packaging, CI | orchestrator | 6,397 + | `research/orchestrator.md` |

Axes: DRY / rule of three · magic numbers and strings · naming · single responsibility · KISS;
hygiene: markers · large files · missing tests · outdated dependencies · dead code. Constraints
applied: no functionality regression, call-site check on every edited surface, "when in Rome"
(hand-written SQL, modules of functions, the explanatory docstring style), the Feathers/Martin
frame. Every High was re-opened by the orchestrator before this document was written; the four
research files hold the full evidence and the call-site lists the plan docs were written from.

## Severity scoring

Severity here measures *what happens if the debt is left*, not how ugly it reads. Blast radius is
how much of the tier a drift would reach; complexity is how hard the cleanup is; risk is the chance
the cleanup itself regresses something.

| Family | Findings | Severity | Blast radius | Complexity | Risk of the fix |
|---|---|---|---|---|---|
| Dead sync-writer lane proving the wrong thing | B1/C3, C4, A16, B5, C16, A3, A6 | High | tenancy (RLS gate) | M | Low–Med (gate rewrite) |
| Hand-copied constants beside their owner | A2, A4, A5, A11, A15, B6, B20, C2, C6, C7, C9, C14 | High | every consumer of the copied value | S | Low |
| Job-ledger contract copied by hand | A4, A5, B3 | High | the worker's lanes | S/M | Low (B3's fix is a defect fix) |
| A guard that does not guard (`_IN_TRANSACTION`) | B2 | High | every worker provider call | M | Uncertain — flagged |
| `landing/` contract copies and a dead door | D1, D3 | High | queue/dashboard/calendar pages; an authenticated route | S/M | Low |
| Copies past the rule of three (services) | A8, A9, A10/B13, A19, B7, B10, B11, B12, B14 | Medium | services | M | Low |
| Copies past three (API/CLI/landing) | C10, C15, C17, C18, C19, D5, D6, D8, D9, D10 | Medium | routes, CLI, BFF | M | Low |
| Oversized / mixed-responsibility functions | A7, A13, A14, A17, A18, B9, C11, C12, C13, D7 | Medium | the module each lives in | L | Medium (bounded by the gates) |
| Wrong home / in-function imports | A12, B8, B15 | Medium | import graph | M | Low |
| Dead and test-only surfaces, legacy instruments | A16, B5, C16, D4, D11, O7 | Medium | none at runtime | S | Low |
| Dependencies and CI | O1, O2, O3 | Medium | deploy image; the security tier | S | Low |
| Test scaffolding | O4, O5 | Medium | tests only | S | Low |
| Stale words, names, config drift | A20, B18, C16, C20, D12, O6 | Low | readers | S | Low |

## Inventory

IDs are the research files' (A = pipeline/worker, B = integrations/identity, C = API/channels/CLI,
D = landing, O = orchestrator). "Plan" is the phase doc that folds the finding in; "flagged" means
the finding leaves this lens (see *Questions*).

### High

| ID | Where | Finding | Plan |
|---|---|---|---|
| A1 | `publish_pipeline.py:263,:1670,:2079` vs `intent_ledger.py:222` / `config/defaults.py:21` | Repost-lock fallback is 7 days on the worker's publish path (`repost_ttl_days_default=7`, no caller overrides it), 30 everywhere else — already drifted | flagged |
| A2 | `command_executors.py:933`, `scheduler.py:381`, `prompts.py:385`, `publish_pipeline.py:284,:618` | Terminal-state list hand-copied at 5 sites beside `intent_ledger.TERMINAL_STATES` | 01 |
| A3 | `work_loop.py:530-536` vs `:649-658` | `build_registry` parks the Drive kinds twice with two different reasons; the first block is dead | 02 |
| A4 | `work_loop.py:1099,:1114` vs `jobs.py:128` | `ensure_sender_jobs` hand-codes the interactive lane budget (`3`, `'10 minutes'`), `LIMIT 200`, the `tg:` prefix ×3 | 01 |
| A5 | `publish_pipeline.py:124` vs `jobs.py:138` | `DEFAULT_BACKOFF_SECONDS` duplicates `jobs.BACKOFF_SECONDS["bulk"]`; `backoff_seconds=` never passed | 01 |
| B1/C3 | `identity_provisioning.py`, `workspace_provisioning.py`, `web_sessions.py`, `sync_tx.py`, `src/exceptions/identity.py`, `tenancy.py:81-101` | Sync psycopg2 writer lane has zero `src/` callers; only `tests/scripts/test_identity_writers.py` reaches it, so the RLS gate proves the dead twin; the twins already diverged (`disabled_user`, `TOKEN_PREFIX`, `COALESCE(:tz,'UTC')`) | 02 |
| B2 | `unit_of_work.py:130-132,330-337`; `work_loop.py:1036-1079`; `egress.py:342-347` | `_IN_TRANSACTION` is set only by `UnitOfWork.begin()`; both worker session idioms bypass it while `credential_lifecycle.py:17-19` and `email_sender.py:67-69` claim the floor enforces | flagged |
| B3 | `media_sync.py:619-633`, `offboarding.py:306-342` vs `jobs.py:291-358` | Hand-written `INSERT INTO jobs` bypass `jobs.enqueue`; `deadline_at` (#1288) never written for chunk jobs; `max_attempts=5` copies `LANE_BUDGETS` | flagged (the enqueue routing); 01 (the literal) |
| B4 | `bindings.py:227-245`; caller `work_loop.py:480-484` | `repoint` swallows a unique violation and returns `False` inside the caller's transaction; `revoke_by_id` then runs on an aborted asyncpg transaction | flagged |
| C1 | `storydump_cli/output.py:650` vs `routes/webhooks.py:138-143`; fixture `tests/storydump_cli/test_env.py:51` | `storydump health` reads `taps.executed`/`taps.replayed`; `/health` nests counts under `taps.taps` and `replayed` is not a tap outcome; the fixture encodes the wrong shape | flagged |
| C2 | `routes/webhooks.py:92` vs `vocabulary.py:389` | `SECRET_HEADER` hand-copied from `WEBHOOK_SECRET_HEADER`; the route reads one, the CLI sends the other; no pin | 01 |
| D1 | `landing/src/lib/intents.ts:22-72` vs `dashboard-payloads.ts:50-70,161-215` | Two TypeScript contracts for `GET …/intents`; `IntentRow` already lacks the `account_*` columns and disagrees on nullability; the contract test pins one copy | 14 |
| D2 | `calendar/page.tsx:110-118` vs `lib/schedule.ts:17-18` | Posting-window arithmetic duplicated; the Calendar's "Posting Rate" is wrong for wrap-midnight and 24-hour windows | flagged |
| D3 | `app/api/dashboard/[...path]/route.ts`, `lib/dashboard-api.ts`, `accounts-tab.tsx:103-114`, `settings/page.tsx:293` | Legacy BFF proxy to 25 API paths that no longer exist, reachable only via a permanently-disabled button | 14 |

### Medium

| ID | Where | Finding | Plan |
|---|---|---|---|
| A6 | `publish_pipeline.py:465-468`; `:527,:581,:1415` | `_next_slot` has zero callers while its body is inlined ×3 | 02 |
| A7 | `publish_pipeline.py:1316-1357` vs `:1359-1400` | `_ladder` (520 lines) copy-pastes the readiness-verdict routing incl. the `ContainerDead` dict | 06 |
| A8 | `publish_pipeline.py:972,:1678,:1806,:2093` vs `outbox.py:767` | Four per-binding `restate_cards` loops where `restate_everywhere` is one statement | 03 |
| A9 | `publish_pipeline.py:821,:1876`; `reconciler.py:306,:194` | `customer_notified` evidence-merge SQL written 3× | 03 |
| A10/B13 | `media_sync.py:300,:482`, `credential_lifecycle.py:365` (+7) vs `outbox.py:223-252` | "Notify every push binding" loop copied 10× in the tier despite `fanout_notification` | 03 |
| A11 | `prompts.py:339-360`; `outbox.py:619,:806`; `work_loop.py:1101` | Push-binding predicate: one declared owner, four spellings | 01 |
| A12/B8 | 45 sites (`work_loop:744/:750`, `reconciler:192/…`, `scheduler:253/…`, `prompts:145/:411`, `credential_lifecycle:159,244,320`, `media_sync:275,324,482,669`, …) | In-function imports: 3 real seams, 4 re-imports of module-level names, 8 false "cycle" comments, 6 forced by `poller_session_factory` living in `work_loop`, 4 `_json` helpers | 05 |
| A13 | `publish_pipeline.py:714-730` + 13 call sites | `_retry_or_poison` takes 10 kwargs because three wait classes are threaded by hand | 06 |
| A14 | `work_loop.py:869-1024` | `_run_job` 155 lines, 5-deep, the finalize decision copied into both branches | 06 |
| A15 | `worker.py:537,:678`; `outbox.py:958,:1182,:1196`; `work_loop.py:83,:1114`; `publish_pipeline.py:1906,:1959` | Operational literals in door bodies; `WorkerConfig.retry_backoff_seconds` (`work_loop.py:83`) has no production reader — its one reader is `tests/scripts/test_w1_worker_gate.py:202`, which is rewritten against `jobs.BACKOFF_SECONDS` | 01 / 02 |
| A16 | `rate_counters.py:43`; `publish_cap.py:317`; `scheduler.py:95`; `intent_ledger.py:66`; `outbox.py:992,:1468`; `invitation_cards.py:110`; `reconciler.py:44/:377`; `telegram_dispatch.py:69/:106` | Dead (`count`, `current_day_debit`, `ClockNotElected`, `PUBLISH_LEG_LIVE`, `rate_limited`) and test-only (`deliver`, `Poller.start/stop`, `legal_transitions`, `announce`, `evidence_capture`) surfaces | 02 |
| A17 | `command_executors.py:411-445` vs `:694-741`; four helper pairs; refusal literal ×3 | Publish-job mint + gates duplicated across `approve`/`resolve_review` | 07 |
| A18 | `telegram_dispatch.py:324-479`, `:530-533` | `_tap` 156 lines with a duck-typed `begin_nested` double path; `_observe_all` redundant branch | 07 |
| A19 | `or "UTC"` ×13; ZoneInfo degrade ×3; `_utcnow` ×3; `elapsed_ms` ×4 | Small helpers copied past the rule of three | 03 |
| B5 | `drive_adapter.py:110,125,178-203,236-293`; `google_drive_adapter.py:41-50,778-836`; `drive_credentials.py:24-25`; `google_drive_oauth.py:45-54` | `StubDriveAdapter.list_files`/`DriveFile`/`DrivePage`/`DriveAuthError`/`probe`/`ProbeResult` test-only; headers assert "no refresh door yet" | 02 |
| B6 | `ig_login_oauth.py:92`, `provisioning.py:91,760`, `workspaces.py:244,284`, `drive_adapter.py:107`, `google_drive_oauth.py:86` + 3 inline | `"ig_login"`/`"gdrive"` spelled as 7 module constants; the `provisioning.py:758` cycle justification is false | 01 |
| B7 | `google_oidc.py:60-70`, `ig_login_oauth.py:516-527`, `bindings.py:94-102`, `category_mix.py:50-58`, `invitations.py:59-64`, `provisioning.py:127-136` | Six hand-rolled copies of `RefusalError.__init__` | 03 |
| B9 | `media_sync.py:323-665`; `google_drive_adapter.py:292-528` | `_run_sync` 343 lines / `list_changes` 237 lines, nesting 5, the cap-warning block pasted twice | 08 |
| B10 | `invitations.py:88-94,198-204`; `workspaces.py:125-130`; `_dbapi.py:21-29` | Two idioms for asyncpg error translation; `_dbapi`'s docstring claims a migration that never happened | 03 |
| B11 | `identity_link.py:60-67`, `channel_bind.py:57-64`, `ig_login_oauth.py:169-176`, `provisioning.py:837-844`; `drive_credentials.py:115,260,292`, `google_drive_oauth.py:329,360`, `workspaces.py:307` | "Retire live oauth_states" UPDATE ×4; the workspace-gdrive-credential predicate ×6 | 03 |
| B12 | `google_drive_oauth.py:216-219,271-278`, `ig_login_oauth.py:631-634`, `credential_lifecycle.py:105-114` | Token-response parsing re-derived 4–5×; literal `3600`; `ig_refresh` unguarded `resp.json()` | 03 |
| B14 | `credential_lifecycle.py:220-239`; `offboarding.py:278-303` (+ `publish_pipeline.py:479,932`) | Hand-written `INSERT INTO audit_events` with inconsistent actor/channel recording | 03 (uncertain step) |
| B15 | `ig_login_oauth.py:117-319,341-349` | The tier's generic `oauth_states` machinery and `ring()` live in "Instagram Login OAuth"; six non-Instagram modules import it | 08 |
| B16 | `category_mix.py:266-307`; `v1.py:776,799` | Self-declared one-release v1 compat shim (#1262, 2026-09-08) still wired | flagged |
| B17 | `ig_credentials.py:63-80`; via `usage_precheck.py:60` | Tenant-less credential read (bare sessionmaker, no GUCs) exists only because `usage_precheck` drops `workspace_id` | flagged |
| B18 | `meta_adapter.py:4-11`, `email_sender.py:3-7,48-50`, `egress.py:125-127`, `provisioning.py:26-31`, `google_oidc.py:55-56`, `webhook_ingress.py:53-57`, `bindings.py:3-15` | Docstrings asserting present-tense facts that are false | 12 |
| C4 | `src/exceptions/telegram.py:7-98`; `scripts/telegram_ratchet_baseline.json` | `AmbiguousDeliveryError`/`ChatMigratedError`: zero callers, kept alive by the ratchet's set-equality entry | 02 |
| C5 | `src/api/app.py:87` vs `src/__init__.py:1` | `/health.version` = hand-copied `"0.2.0"` while the package is `1.6.0`; `doctor` prints it | flagged |
| C6 | `telegram_transport.py:50,602`; `telegram_webhook_registration.py:63,75`; `storydump_cli/webhook.py:58` | `RAILWAY_ENVIRONMENT_NAME`/`"production"` and `https://api.telegram.org` re-spelled outside the vocabulary | 01 |
| C7 | `app.py:418-423` vs `telegram_webhook_registration.py:71` | `_register_webhook` re-copies the autoregister off-words | 01 |
| C8 | `routes/webhooks.py:162-165`, `routes/v1.py:93` vs `app.py:413-414,447,536` | Same Telegram variables read via `settings` in routes and raw `env` in the factory; `create_app(env=…)` cannot arm the door | 09 (optional, a ruling) |
| C9 | `src/exceptions/tenancy.py:73` vs `vocabulary.py:133-137` | `TokenRefused.REASONS` hand copy of `TOKEN_REFUSALS`; `"unprovisioned_channel"` has no raiser | 01 |
| C10 | `v1.py:135,154,186,596`; `tokens.py:94-240` (8), `ops.py:56,59` | Three routers reach `v1._open_tenant/_member/_admin/_json_object` as private names | 04 |
| C11 | `app.py:550-852`, helpers `:401-503` | `create_app` = lifespan + wiring + middleware + three inline health routes; registration/sampler are channel logic in the API root | 09 |
| C12 | `storydump_cli/commands/env.py:511-738` | `doctor`: 215-line body, six try/except ladders, nesting 4 | 09 |
| C13 | `telegram_transport.py:469-574` | `for_chat.send`: 95-line closure, four fallback `except` arms | 09 |
| C14 | `env.py:551,611,645` vs `main.py:56,60,237` | Three fix sentences hand-copied because `env.py` cannot import `main.FIXES` | 01 |
| C15 | `app.py:288-357` | `_register_handlers`: the `TABLE.get(reason) → _unmapped → JSONResponse` shape ×5 | 04 |
| D4 | `package.json:22`, `lib/types.ts:32-50`, `dashboard-payloads.ts:465-507`, `utils.ts:8-16`, `ui/progress.tsx`, `ui/slider.tsx`, `next.config.ts:22-33`, `sidebar.tsx:22` | `jose` and six zero-importer leftovers; a Telegram-widget CSP on `/login` for a widget that is gone; a "Coming Soon" nav target | 14 |
| D5 | `lib/tokens.ts:229-245` + 8 copies; `queue-list.tsx:130-147` | fetch→`{ok,error,status}` wrapper copied 9×; the queue bypasses `submitCommand` | 15 |
| D6 | 19 route files; 5 pages | Route/page preambles copy-pasted (401 / `invalid_workspace` / pass-through / `getSession`→`/login`→`/welcome`) | 15 |
| D7 | `integrations-tab.tsx` (821 lines) | Three cards + a folder-picker dialog + 19 `useState`s in one component while its siblings are split into cards | 16 |
| D8 | five components; `api/workspaces/route.ts:6-11`; tone maps ×3; `session.ts:242` vs `commands.ts:58` | Shadow copies of lib types, `Workspace` typed twice, three badge-tone maps, two UUID predicates | 14 |
| D9 | 16 banner sites | Inline notice banners re-typed 16×, `role="alert"` on 4 of 9 | 15 |
| D10 | name max `100` ×3 (+ `120`), TTL `900`/`15` ×4, inline `limit=` ×4, `INVITE_COOKIE` in a route | Magic numbers, a cookie name exported from a route, `unreachableCopy` unused | 15 (constants); flagged (the fabricated defaults) |
| D11 | `sidebar.tsx:38-43`, `header.tsx:35,45,52,60`, the `editable` flags, `create-workspace-form.tsx:25`, `general-tab.tsx:140`, `category-weights-card.tsx:43-47` | Dead props and fixed-value flags; one hides the mobile-nav defect | 15 (dead props); flagged (the mobile nav) |
| O1 | `requirements.txt` | 8 packages nothing imports: `alembic`, `httpx2`, `python-dateutil`, `tenacity`, `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `anthropic` | 10 |
| O2 | `setup.py:11-28` | `install_requires` drifts both ways from `requirements.txt` and omits `asyncpg`; `author="Your Name"` | 10 |
| O3 | `.github/workflows/ci.yml:142-170` | The security job can never fail (`|| true`, `continue-on-error`, placeholder `GHSA-1234`) | 10 |
| O4 | 19 test files | Hand-rolled `replace("postgresql://", "postgresql+asyncpg://", 1)` though `unit_of_work.asyncpg_url()` owns the rewrite | 11 |
| O5 | `tests/scripts/test_w*_gate.py` and siblings | `lane_db` ×6, `sync_conn` ×5, `_free_port` ×3, `_run(coro)` ×8 pasted verbatim beside a 1,237-line `tests/scripts/conftest.py` | 11 |
| O7 | `scripts/fork_a_attribution.py`, `fork_e_lock_cost.py`, `fc8_gate.py`, `observed_use.py`, `target_reachability.py` (+ 4 tests, ≈2.9K lines) | Legacy-tier measurement instruments whose questions closed with the tear-out — and whose tables 079 dropped on 2026-09-19 | 13 |

### Low

| ID | Where | Finding | Plan |
|---|---|---|---|
| A20 | `transit.py:4/:72/:75/:212/:271`; `jobs.py:432`; `commands.py:3`; `publish_pipeline.py:58/:1583`; `publish_cap.py:302`; `provider_ops.py:84`; `vocabulary.py:451`; `prompts.py:230`; `worker.py:645/:673` | Stale references to torn-out code; `TERMINAL_STATES`/`window_start` name clashes; `text` shadowing; undeclared `health_server` | 12 |
| B19 | `identity.py:65,:261` vs `provisioning.py:425,:495`, `category_mix.py:186` | Five advisory-lock sites, two hash widths (`hashtext` vs `hashtextextended`) | flagged — unifying changes each lock's key value, which a rolling deploy cannot do safely |
| B20 | `channel_bind.py:37,39`, `membership_sync.py:29`, `bindings.py:76,114`, `tenant_resolution.py:59`, `invitations.py:82,139`, `service_tokens.py:188`, `workspaces.py:195-197,713-718`, `command_executors.py:886-892` | Small vocabularies spelled 2–3×; two hand-rolled `set_config`; `remove_member` raises untyped `LookupError`/`ValueError` and the caller parses `str(exc)` | 01 |
| C16 | `app.py:544-547`; `datetime_utils.py:10-11,29-59`; `logger.py:74-87`; `encryption.py:126,132,139-166` | `_telegram_reply` (no caller); `naive_utc`/`get_logger`/`rotate` (test-only); a docstring naming legacy tables; two f-string logs | 02 / 12 |
| C17 | `app.py:63/65, 566-620, 498, 696` | Duplicate `webhooks` import; `os.environ if env is None else env` ×5; an unnamed `60`; a needless `getattr` | 04 |
| C18 | `v1.py:337,437,550,576,754,1014`; `tokens.py:171,245` | `HTTPException(404, "not found")` ×8 | 04 |
| C19 | `auth.py:360-366,448-454` vs `v1.py:119`, `principal.py:107` | `channel="web"`/`actor_kind="user"` literals; the 7-line UoW construction ×2 | 04 |
| C20 | `src/services/target/health.py:148-169`; `worker.py:645` | The worker's raw-socket probe is named `health.py` beside `/health`, CLI `health` and two monitors → `worker_health.py` | 12 |
| D12 | `commands.ts`, `command-client.ts`, `refusal-copy.ts`, `general-tab.tsx`, `integrations-tab.tsx`, `auth/error/page.tsx` + 9 stale claims | Ten docblocks on the wrong symbol; comments that state the opposite of the code | 15 |
| O6 | `Makefile` ×6 targets; `setup.py:8` | `./venv/bin/pytest` while the checkout uses `.venv/`; `make logs` tails a file nothing may write; `author="Your Name"` | 12 |

### Cleared

Read in full and found nothing material beyond the cross-cutting findings that name them:
`backpressure.py`, `usage_precheck.py`, `main.py`, `egress.py`, `unit_of_work.py`, `readers.py`,
`_dbapi.py` (needed), `tenant_resolution.py`, `sessions.py`, `service_tokens.py`, `identity.py`,
`identity_link.py`, `channel_bind.py`, `start_router.py`, `membership_sync.py`,
`webhook_ingress.py`, `meta_callbacks.py`, `meta_adapter.py`, `instagram_graph.py`,
`google_oidc.py`, `google_drive_oauth.py`, `email_sender.py` (parked by design),
`src/api/{google,instagram,oauth}_client.py` (live leaves, 7 call sites — the "tombstone" lead
was refuted), `routes/retired.py` (deliberate, mounted, tested — names no sunset condition),
`routes/meta.py`, `routes/ops.py`, `principal.py`, `src/config/*` (both small files are read and
pinned by `test_defaults.py`), `src/models/**` (as designed: no importer under `src/` or the CLI;
only the parity/tenancy gates), `src/exceptions/base.py`, `scheduling_health.py`,
`posting_health.py` (the one repeated SQL is documented as deliberately unshared), `ops_views.py`,
`callback_tokens.py`, the whole CLI envelope path (one `dispatch`, one `emit`/`RENDERERS`, no verb
re-implements exit codes), `storydump_cli/watch.py` (one `watch()`), the layer rules (no violation
anywhere: channels imported only by the two roots, the CLI's `src` closure is exactly
`vocabulary`), `scripts/unreachable_capabilities.py` (audits the live tree; keep), and in
`landing/` the DB and API layer rules, the command envelope, `api-tokens-tab.tsx` (the model the
other tabs should follow), naming, escape hatches (one `as unknown as`, no `any`).

## Blockers

None. The one dependency that was external — the owner's 079 window — ran on 2026-09-19, so the
legacy-instrument retirement (13) is executable now.

## Risks

The High rows above, in the order the plans take them: the copied constants (01), the dead lane
and its mis-aimed gate (02), the job-ledger literals (01) with the `enqueue` routing flagged (B3),
the blind tripwire (B2, flagged), and the two `landing/` contracts (14). Each is a drift that has
either happened (A1, C1, C5, D1, D2) or has nothing stopping it.

## Gaps

- **Test coverage worth closing** (the scan's per-module check, false positives removed):
  `command_executors.reconnect_account` (no test names it); `transit.TransitStore.destroy_asset`'s
  refusal branch; the live async writers `identity.upsert_google_identity`/`sessions.*` have no
  real-database RLS gate (the gate covers the dead twin — 02 re-homes it); `TapMetrics.snapshot()`'s
  shape is unbound to the CLI renderer (C1); `bindings.repoint`-conflict-then-revoke in one
  transaction (B4); `meta_callbacks.resolve_ig_accounts`/`revoke_for_accounts` SQL never executed
  (route tests stop at the 503 engine gate); in `landing/`, `lib/workspaces.ts`,
  `start-grant.ts`/`start-proxy.ts` (the redirect-host guard seam) and the one write door
  `api/workspaces/[id]/commands/[command]/route.ts` (no test of its own). Coverage by area:
  landing lib 15/29 modules, route handlers 4/19, components 5/≈45 (vitest runs in `node`).
- **Maintainability gaps** — the Medium rows: every copy past three, every function the gates
  have to characterize because it cannot be read in one sitting.

## Questions

Flagged for owner rulings or bug-fix PRs — **not planned by this lens.**

Each of these changes behaviour to fix, so it must not ride inside a cleanup PR. They are recorded
here with their evidence so nothing is lost; each wants its own ruling or a bug-fix PR through
`/build`.

1. **Repost-lock fallback: 7 or 30 days?** `run_publish_pipeline(repost_ttl_days_default=7)` with
   no caller overriding it, beside `DEFAULT_REPOST_TTL_DAYS = 30` read by `intent_ledger` — a
   worker publish and a manual `posted` lock the same media for different spans. (A1)
2. **`storydump health` renders blank tap counts.** `/health` emits
   `taps: {taps_total, taps: {outcome: n}, answer_failed}`; `output.py:650` reads `taps.executed`
   and `taps.replayed`; the fixture at `test_env.py:51` encodes the wrong shape so the test
   passes. The pool block had the same defect, fixed as `POOL_FACTS` in #1324. (C1)
3. **`bindings.repoint` leaves the caller on an aborted transaction** when the new chat id is
   already bound: it swallows the unique violation, returns `False`, and `work_loop.py:484` runs
   `revoke_by_id` on the same session. Untested; a supergroup-migration edge. (B4)
4. **Chunk jobs never get `deadline_at`** because `media_sync.py:619` and `offboarding.py:306`
   insert into `jobs` by hand; routing them through `jobs.enqueue` gives them the deadline #1288
   added. (B3)
5. **`/health.version` says `"0.2.0"`** (`app.py:87`, unchanged since #1035) while
   `src.__version__` is `1.6.0`; `doctor` prints it. Point it at the package. (C5)
6. **The `_IN_TRANSACTION` tripwire covers no worker path.** Setting it in `make_session_for` and
   `poller_session_factory` is the fix, and it may start raising on an existing violation — a
   verification pass first. (B2)
7. **A tenant-less credential read** (`ig_credentials.py:63-80`: bare sessionmaker, no GUCs,
   cross-tenant "prefers active") exists only because `usage_precheck.py:60` drops
   `workspace_id`. (B17)
8. **The `category_mix` v1 compat shim** (#1262, "one release", 2026-09-08) is still wired at
   `v1.py:776,799`; removing it is a contract change. (B16)
9. **The Calendar's "Posting Rate" card is wrong for wrap-midnight and 24-hour windows** —
   `calendar/page.tsx:110-118` recomputes `end - start` while `schedule.ts:17-18` mirrors
   `fn_next_slot`. Sharing the helper changes the rendered figure. (D2)
10. **The mobile navigation drawer renders a `hidden` sidebar** — `Sidebar`'s `mobile` prop is never
    passed (`layout.tsx:45`, `header.tsx:52`) and the trigger (`lg:hidden`) and aside
    (`md:block`) breakpoints disagree. (D11)
11. **Settings cards fabricate `30`/`45`/`"enhanced"` for NULL** (`repost-cadence-card.tsx:32-37`,
    `caption-style-card.tsx:40`) where the card's own header says NULL means "not set". (D10)
12. **Two advisory-lock hash widths** — `identity.py:65,:261` use `hashtext` while
    `provisioning.py:425,:495` and `category_mix.py:186` use `hashtextextended`, the latter
    deliberately (`provisioning.py:484-487`: the 32-bit variant "collides often enough at estate
    scale to serialize unrelated folders"). Unifying them is defensible but **changes each lock's
    key value**, so during a rolling deploy old and new processes would take different locks for
    the same logical key and the lock would not hold across that window. Needs a ruling and a
    deploy plan, not a cleanup PR. (B19 — moved here from doc 12 during planning.)

## Observations

The Low rows, plus three notes outside the axes: `landing/src/lib/telegram.ts` holds
`TELEGRAM_BOT_TOKEN` + `ADMIN_TELEGRAM_CHAT_ID` in the site's environment for a 25-line waitlist
ping with no timeout (the API's adapter owns that token — worth deciding whether the site should
hold it); the marketing copy at `config/faqs.ts:8-11,23-26` and `setup/connect/page.tsx:101-104`
describes the pre-dashboard product; `.claude/settings.json`'s allow/deny lists still name the
deleted legacy CLI verbs (an owner edit, noted in `CLAUDE.md`).

## Corrections made while planning

Each phase doc re-opened the lines its findings cite before writing a step, and that pass
corrected the scan in nine places. They are recorded here because an audit that quietly drops a
claim is worth less than one that says which claims did not survive.

| Claim | Verdict | Where it is recorded |
|---|---|---|
| `workspaces.list_bindings` / `list_invitations` have zero refs | **Wrong** — called from `v1.py:469,:478` | this doc (Gaps, corrected) |
| `drive_credentials.provider_from_engine` has zero refs | **Wrong** — called from `worker.py:845`, `v1.py:883` | this doc (Gaps, corrected) |
| `WorkerConfig.retry_backoff_seconds` has zero readers | **Narrowed** — no production reader; one test reader at `test_w1_worker_gate.py:202`, rewritten against `jobs.BACKOFF_SECONDS` | doc 02 |
| A11's push predicate belongs in `prompts.py` | **Wrong home** — `prompts.py:49` imports `outbox`, so that is a cycle; the predicate goes to the leaf `bindings.py` | doc 01 |
| `ProvisioningRefused`'s message can share `RefusalError.__init__` | **Withdrawn** — the message reaches `/v1` as `detail`; changing it is a contract change | doc 03 |
| B12's `json_body` recurs 3+ times | **Withdrawn** — it recurs twice; two is coincidence | doc 03 |
| `ig_refresh`'s `expires_in` guard duplicates its siblings | **Withdrawn** — the guards genuinely differ | doc 03 |
| The three ZoneInfo degrades are one helper | **Withdrawn** — three behaviours, not one | doc 03 |
| C19's UoW construction recurs 3+ times | **Withdrawn** — two sites | doc 04 |
| The nine `_run(coro)` test helpers are one helper | **Withdrawn** — three distinct event-loop semantics | doc 11 |
| "3 stdlib in-function imports in `src/`" | **Withdrawn** — `ruff --select PLC0415` finds none beyond the three `import json` | doc 05 |
| The four `_json` helpers are one helper | **Narrowed** — two are identical one-liners (coincidence); `media_sync`'s carries a `{"v": 2}` default and stays, renamed | doc 05 |
| The legacy instruments total ≈2.9K lines | **Corrected** — 3,542 lines with their tests | doc 13 |

Doc 04 also found two call sites the scan missed: `tests/mutations/cli_v2_01.sh:118,120` and
`cli_v2_02.sh:75` embed the `v1._open_tenant` call text verbatim, so a rename breaks those shell
batteries silently. Every later doc's call-site audit now greps `tests/mutations/*.sh` too.

## Remediation order and dependency matrix

One PR per doc. The order puts the reversible before the structural and every extraction after
the constant it will import.

| # | Doc | Findings | Depends on | Blocks | Effort |
|---|---|---|---|---|---|
| 01 | `01_one-spelling.md` | A2 A4 A5 A11 A15 B6 B20 C2 C6 C7 C9 C14 | — | 02 03 04 | M |
| 02 | `02_dead-lane-and-surfaces.md` | B1/C3 C4 A16 B5 C16 A3 A6 A15 | 01 | 05 12 | L |
| 03 | `03_rule-of-three-services.md` | A8 A9 A10/B13 A19 B7 B10 B11 B12 B14 | 01 | 06 07 08 | M–L |
| 04 | `04_rule-of-three-api-cli.md` | C10 C15 C17 C18 C19 | 01 | 09 | M |
| 05 | `05_imports-and-homes.md` | A12 B8 | 02 | 06 | M |
| 06 | `06_publish-pipeline-shape.md` | A7 A13 A14 | 03 05 | — | L |
| 07 | `07_executors-and-tap.md` | A17 A18 | 03 | — | M |
| 08 | `08_integrations-shape.md` | B9 B15 | 03 | — | L |
| 09 | `09_composition-roots.md` | C11 C12 C13 (C8 optional) | 04 | — | M |
| 10 | `10_dependencies-and-ci.md` | O1 O2 O3 | — | — | S |
| 11 | `11_test-scaffolding.md` | O4 O5 | — | — | S–M |
| 12 | `12_stale-words-and-names.md` | A20 B18 C16 C20 O6 | 02 | — | S |
| 13 | `13_legacy-instruments.md` | O7 | — | — | S |
| 14 | `14_landing-one-contract.md` | D1 D3 D4 D8 | — | 15 | M |
| 15 | `15_landing-shared-shapes.md` | D5 D6 D9 D10 D11 D12 | 14 | 16 | M |
| 16 | `16_landing-integrations-tab.md` | D7 | 15 | — | M |

Independent starts: 01, 10, 11, 13, 14 can open in parallel on day one. The critical path is
01 → 02 → 05 → 06 (the pipeline decomposition waits for the constants, the deletions and the
session-factory re-homing). Every plan carries the behaviour-preservation gate: baseline
(`REQUIRE_TEST_DATABASE=1 pytest --no-cov`) on the parent commit, the same after, a call-site grep
for every edited surface, a manual smoke where applicable, and a `CHANGELOG.md` entry (CI gates
on it).

## Live status

| Doc | Status | PR |
|---|---|---|
| 01–16 | merged | the banner lists each |

(Every row merged by 2026-09-21; the directory was archived that day, #1357.)

## Related

- `documentation/archive/2026-09-16-legacy-tear-out/00_EPIC.md` — the tear-out whose residue
  half of this audit is.
- `documentation/archive/2026-09-16-cli-v2-audit/00_AUDIT.md` — the previous audit (system lens)
  of the CLI surface, whose fold #1314 already took the CLI's share.
- `documentation/operations/legacy-window-close.md` — the window that makes 13 executable.

## Origin

`/artemis-skills:audit tech debt`, 2026-09-20, on `main` at `0966771`. Research files (scratch,
not committed): `tech-debt-2026-09-20/research/{pipeline-worker,integrations-identity,api-channels-cli,landing,orchestrator}.md`.
