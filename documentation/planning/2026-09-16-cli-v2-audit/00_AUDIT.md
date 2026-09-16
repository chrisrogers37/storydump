---
title: "System review of the v2 CLI surface — findings and their fold"
type: audit
status: in-progress
owner: chris
created: 2026-09-16
tags: [system, cli, audit]
---

# System review of the v2 CLI surface (2026-09-16)

**Scope.** The surface merged by the v2 CLI plan (#1310 tokens, #1311 reads,
#1312 writes + env verbs + the legacy CLI's deletion; `main` at `d8f5c72`):
`storydump_cli/`, the API side it talks to (`src/api/principal.py`,
`routes/tokens.py`, `routes/ops.py`, the command port's token fences,
`src/services/target/{service_tokens,ops_views,vocabulary}.py`, migration
077), the web's token panel and BFF routes, and the tests, gates and batteries
that pin them. The legacy tier (retired by #1216) is out of scope.

**Method.** The `/audit` system lens: an intake brief, three comprehension
maps (architecture, code, data model), a validation baseline, then one
concern lane per angle as a read-only subagent on a frozen worktree —
security & secrets (depth), correctness at rest (with the read views' SQL),
reliability & operations (with docs-vs-code and performance), test quality
(with hand-picked mutants), architecture & maintainability, and access-path
consistency. Every CRITICAL/HIGH was re-verified against source before the
fold; every candidate was reconciled against the tracker (386 open, 60
recently closed, plus a targeted `--state all` search per finding).

**Baseline on arrival** (all green on the audited surface): `ruff check .`
and `ruff format --check .` clean; the five DB gates 123 passed and the
lineage lane 37 passed against the replayed schema; the surface's unit
suites green; `landing/` 81 vitest tests, `tsc --noEmit` clean. Baseline
state, not attributed to the surface: two `test_egress_floor.py` tests need
`127.0.0.2`, which this Mac does not alias (green in CI's Linux), and
`test_unit_of_work.py::test_an_empty_answer_is_none` is order-dependent
(passes alone).

## The lanes

| Lane | Status | Findings (C/H/M/L) | File |
|---|---|---|---|
| Map: architecture | done | 32 components, 7 `Unverified` | `system-map.md` (scratch) |
| Map: code | done | 13 flows, 6 `Unverified` | `code-map.md` (scratch) |
| Map: data model | done | 15 relations | `data-model-map.md` (scratch) |
| Security & secrets | done | 0/0/2/8 | `findings-security.md` |
| Correctness at rest | done | 0/2/9/10 | `findings-correctness.md` |
| Reliability & operations | done | 0/3/11/11 | `findings-reliability.md` |
| Test quality | PARTIAL — killed by the session rate limit after its mutants ran (9 of 10 hand-picked mutants SURVIVED; results salvaged) | — | `repros/tests-lane/` |
| Architecture & maintainability | done (re-dispatched after the reset) | 0/2/7/8 | `findings-architecture.md` |
| Access-path consistency | PENDING — killed by the session rate limit; re-dispatched after the reset | — | — |

The maps corrected the intake brief twice (the exit-code contract; the env
variable names) — the brief was written from memory and the maps from source,
which is the point of the maps.

## The findings, reconciled and dispositioned

Buckets: **net-new** (nothing in the tracker), **extends #N**, **duplicate of
#N**. Disposition: **folded** (this PR), **queued** (the owner's decision, or
the tear-out's), **accepted** (by design, documented).

### HIGH

| # | Finding | Lane | Bucket | Disposition |
|---|---|---|---|---|
| C-H1 | `burst`'s `float_wait` section casts `seconds` to int; the ladder writes `60.0`; Postgres refuses `'60.0'::int` → every window holding a float wait answered 500 (confirmed end to end: the writer, the value, the cast on Postgres 15) | correctness | net-new | folded: `::numeric` cast; the gate seeds the float |
| C-H2 | `deploys --watch --commit <full sha>` never ends: rows keep 7 chars and the match was `short.startswith(given)`; a FAILED row of that commit was never reported either | correctness | net-new | folded: the row keeps `commit_hash`; prefix match either way, case-insensitive; `--timeout` |
| R-H1 | `.claude/QUICK_REFERENCE.md` and `.claude/PROJECT_CONTEXT.md` carry never-run lists missing `resolve <story> retry` and `webhook register`; the parity test polices only CLAUDE.md/AGENTS.md | reliability | net-new | folded: the lists completed; `test_agent_docs.py` pins every satellite and refuses an unpinned one |
| R-H2 | `monitoring.md` is the legacy tier (`--service web`, `posting_queue`, a `health_check.sh` that cannot run) — a tier nothing deployed runs since 2026-08-24 | reliability | net-new | folded: rewritten onto the v2 surface |
| R-H3 | `troubleshooting.md`: `railway shell … -c` does not exist; legacy tables; the emergency stop is a legacy UPDATE | reliability | net-new | folded: rewritten; the emergency stop is `storydump pause --workspace` |
| A-H1 | The Telegram variable names are "one spelling" for the CLI only: the vocabulary's `TELEGRAM_*_VAR` had no API reader — `app.py` read the literals in five places, `worker.py` in two — so the pin test pinned the constants to themselves | architecture | net-new (RUN_LOG §7 had queued it) | folded: the API and the worker read them from the re-export; a ratchet test forbids a literal `TARGET_TELEGRAM_` read outside the vocabulary; the settings' field names are pinned to the constants |
| A-H2 | Six wire spellings copied into the web with no contract test (token roles, the secret prefix, the name/expiry bounds, the key bound, the resolutions, the verdict) — a prefix drift would 502 over a minted-but-never-shown token | architecture | net-new | folded: `landing/src/lib/wire-contract.test.ts` reads them from `vocabulary.py`; the bounds moved into the vocabulary and `service_tokens.py` reads them by reference |
| A-M1 | Invariant I3 is import-time only, with the two `scripts` monitors and the third-party set unpinned | architecture | net-new | folded: the exact closure is pinned (the two monitors, stdlib-only, and four third-party packages at any depth); the distribution question goes to the tear-out |
| A-M2 | The vocabulary's tests-only mirrors and the bounds it lacks; a five-section module under a three-kinds docstring | architecture | net-new | partly folded (the bounds and the floating limits are in, read by reference); the mirrors-as-sources and the split are queued (tech-debt; the batteries anchor on the module) |
| A-M3 | The CLI's `error.reason` had no closed set: two tables in two modules, seven reasons raised inline, `check_envelope` validating the code only | architecture | net-new | folded: `CLI_REASONS` (the port's refusals plus the CLI's eight own), `check_envelope` refuses the rest, both tables pinned as subsets; the one-table refactor is queued |
| A-M4 | The idempotency keys are a second derivation of the web's with no pin; the workspace verbs' docstring said "per click" where the web says per attempt | architecture | net-new | folded: `tests/fixtures/idempotency_keys.json` read by pytest and vitest, the two deliberate differences stated; the docstring says per attempt |
| A-M5 | `output.py` is four modules; the renderer registry is unpinned against the verb set | architecture | net-new | folded (the totality test); the split is queued |
| A-M6 | `doctor` is one function of six try-ladders; the check order is spelled twice | architecture | net-new | queued (tech-debt) |
| A-M7 | The batteries anchor on verbatim source text; every structural fix re-cuts anchors | architecture | net-new | queued (process): `# mut:` markers |
| A-L1..L8 | the webhook seam bypassing `Runtime.transport`; the `Watched` bag; deployment identities in three modules and a second project id in `scripts/observed_use.py`; misleading names; dead names; magic literals; verb modules chained for helpers; the API's legacy reach through `src.utils.logger` and `src/exceptions/__init__.py` | architecture | net-new | folded: the deployment identities (`API_URL`, `RAILWAY_PROJECT_*`) spelled once and pinned; `surface_is_well` and `PRINCIPAL_KINDS` deleted; the floating limits by reference. Queued: the seams, the bag, the names, the helpers. The legacy reach went into the tear-out plan (#1315: `src/exceptions`, the logger's settings load) |
| T-1 | Nine hand-picked mutants survived the suites: `dispatch` returning 0 on Ctrl-C, a non-JSON 2xx read as `{}`, a failed `railway deployment list` read as no deployments, `webhook status` ignoring a URL mismatch, `revoke` ignoring the owner (unit AND gate), `doctor` reading a 4xx `/health` as ok, `deploys --watch` ending on one service, a non-object health payload read as well, a port answer without an outcome read as executed | tests | net-new | folded: a test per mutant (the revoke one in the tokens gate: a stranger's revoke is 404 and the row stays live); the battery carries them |

### MEDIUM

| # | Finding | Lane | Bucket | Disposition |
|---|---|---|---|---|
| S-M1 | A shared-chat card says "approved by <token name>" — the minter's free text stands in for the person (`_actor_name` → `actor_label`); the ledger's `actor_user_id` stays truthful | security | net-new | queued (owner): fork F4 was locked "the token's name as the client label"; rendering the person's name with the token as a suffix is a wording change to the adapter the owner should ratify |
| S-M2 | A person-bound token acts in every workspace the person can write to, for up to a year; no per-workspace scope | security | net-new (near #1148) | queued (owner): a scoped person token is a feature (a third subject shape), not a fold |
| C-M1 | Ctrl-C outside a watch: exit 64 with nothing printed, no envelope | correctness | net-new | folded: an `interrupted` envelope, 64 (inside a watch, 0 as documented) |
| C-M2 | `--json` not honoured for a usage error raised before the flag's callback | correctness | net-new | folded: the flag is read from the arguments; the envelope names the verb |
| C-M3 | A reason-less 403 (below the role floor) rendered "not authorized — run storydump login" | correctness | net-new | folded: `insufficient_role` sentence and fix |
| C-M4 | `jobs` windows failed rows on `created_at` while `reschedule_job` reuses the row: a long float's retry that died today is outside today's window | correctness | net-new | folded: the window is `updated_at` for the finished states; the gate seeds the case |
| C-M5 | `burst --watch` ends 0 while a story is `publishing_ambiguous` | correctness | net-new | folded |
| C-M6 | `story`/`cards`/`burst` lists truncate at 500 keeping the OLDEST rows, silently | correctness | net-new | folded: newest kept, still oldest-first; a story names the cut lists (`truncated`); the terminal says so |
| C-M7 / R-M3 | A watch aborts on the first transient failure (a 503 with Retry-After, a dropped connection) | correctness, reliability | net-new | folded: three consecutive unanswered reads before exit 4; a definitive answer still ends it at once |
| C-M8 | `jobs`/`outbox --watch` exit 6 on ANY change to a failed group, a shrink included; the runbook says "appears or grows" | correctness | net-new | folded: a changed row is judged only when it got worse (`Watched.worse`) |
| C-M9 | A broken pipe exits 1 (the contract's "not found") with a traceback | correctness | net-new | folded: 0, quietly |
| R-M1 | `doctor` blames the token for an API 5xx (`except ApiError` shadows `except Unreachable`) | reliability | net-new | folded |
| R-M2 | `health` exits 0 with `/health.webhook.ok=false` and a backlog behind a delivery error | reliability | net-new | folded: a fourth verdict, `webhook` |
| R-M4 | Ctrl-C inside a watch is 0 and indistinguishable from done; `deploys --watch` has no deadline and no reading for REMOVED | reliability | net-new | folded: `--timeout`, `REMOVED` fails; the Ctrl-C = 0 rule is kept and stated (a watch stopped by hand is not a failure) |
| R-M5 | An OSError from the config directory is a traceback (login, logout) | reliability | net-new | folded: a usage envelope naming the path |
| R-M6 | `story`'s audit read and `floating`'s LATERAL skip `ix_audit_entity`'s `entity_kind` column (Likely; needs EXPLAIN) | reliability | net-new | queued (follow-up): an index change is a migration; measure with EXPLAIN on the replayed schema first |
| R-M7 | `telegram-webhook.md` names `verify` (no such subcommand), calls the URL mismatch a NOTE, "four questions" lists three | reliability | net-new | folded |
| R-M8 | `ci-cd-pipeline.md` shows a workflow that is not `ci.yml`; `ruff check src/ tests/ cli/`; "tests use mocked dependencies" | reliability | extends #728 | folded: the excerpt describes the real five jobs |
| R-M9 | `backup-restore.md` restarts `--service web`, backs up legacy `api_tokens`/`media_items` | reliability | net-new | folded: the service name; the legacy sections marked until #1216 |
| R-M10 | `deployment.md` applies migrations by a 001–021 psql loop; services named Worker/Web | reliability | extends #531 | folded: the runner; `worker`/`storydump` |
| R-M11 | `dev-environment-setup.md` aliases: `ruff check src/ tests/ cli/` ×3, `--service web` ×2, the loop | reliability | extends #990 | folded (the aliases; #990's wider rewrite stays open) |
| D-1 | `--since 30d` (the documented maximum) is refused by the API whenever its clock second is ahead of the CLI's (reproduced) | data model | net-new | folded: five minutes of slack, clamped to the bound |
| D-2 | The `{v:1}` JSONB envelope CHECKs pass on a missing key (the ops gate itself writes `'{"fetch": 1}'`) | data model | net-new | queued (follow-up): a CHECK change is a migration over rows already written without `v`; measure first |
| D-3 | Production's API connects as the BYPASSRLS owner, so the `/ops` views' tenancy rests on their explicit predicates and the membership gate | data model | duplicate of #751 (and #1134) | accepted here: the views are proven under `svc_ingress` AND under a bypass arm by the ops gate; #751 owns the switch |

### LOW

| # | Finding | Lane | Bucket | Disposition |
|---|---|---|---|---|
| S-L1 | Two unmapped exception types reach the terminal as tracebacks (a non-ASCII idempotency key; a newline in an env variable) | security | net-new | folded (the key is validated as printable ASCII; the OSError class is mapped); the bot-token shape check is a follow-up |
| S-L2 | `DATABASE_URL_PATTERN` misses the `postgresql+asyncpg://` dialect this deployment uses | security | net-new | folded |
| S-L3 | `/ops/posture` discloses deployment internals to any principal, a read-only service identity included | security | net-new | queued (owner): `07` §1 says "any authenticated principal, by design"; `doctor` needs the ledger — trimming it for service identities is a design change |
| S-L4 | No rate limit or lockout on bearer verification | security | net-new | queued (owner/infra): an edge budget on 401s |
| S-L5 | The web's mint dialog defaults to `operator` | security | net-new | folded: `readonly` |
| S-L6 | `STORYDUMP_INSECURE_HTTP=1` disarms the plain-http guard for any host, silently | security | net-new | queued (follow-up): warn naming the host |
| S-L7 | CI never scans, audits or measures `storydump_cli` or its `keyring` dependency | security | net-new | folded (bandit, coverage, the `cli` extra); `pip-audit \|\| true` is pre-existing and stays advisory |
| S-L8 | Client-chosen strings enter audit rows and card text unescaped (Hypothesis: no interpreting renderer found) | security | net-new | accepted: pinned as data everywhere traced; revisit if a markup renderer appears |
| C-L1 | `exit_code_for` edges: 404 `unknown_command` → 3; 501 → 4; 3xx → 4 "refused"; 422 loses FastAPI's list | correctness | net-new | folded (the 422 list); the rest are unreachable from the CLI's own verbs and left |
| C-L2 | `_render_tokens` raises on a non-object row | correctness | net-new | folded |
| C-L3 | Non-ASCII `--idempotency-key` is an uncaught UnicodeEncodeError | correctness | net-new | folded (= S-L1) |
| C-L4 | `--since <ISO with microseconds>` in the current second is "in the future" | correctness | net-new | folded (= D-1) |
| C-L5 | `--watch --json`'s shape in the spec and plan 02 is not the shipped one | correctness | net-new | folded: the spec and the plan say `data.workspaces[].changes` |
| C-L6 | Help: "6 when a read shows the failure condition" (the baseline never fails); `--since` "back from now" under a watch | correctness | net-new | queued (follow-up wording) |
| C-L7 | "Exactly the web's idempotency identities" is inert across doors (dedup is per channel and principal) and `resolve` adds a verdict segment | correctness | net-new | accepted: the key SHAPE is the web's; the docs say "the same key shape" — a wording pass is queued |
| C-L8 | Spec drift: `resolve … giveup`, `outbox` paced holds, `health` heartbeat freshness, `--watch` "keyed by id" | correctness | net-new | folded (the spec corrected) |
| C-L9 | `storydump webhook --json status` is a usage error | correctness | net-new | folded |
| C-L10 | `doctor`'s ledger check compares versions only, never checksums | correctness | net-new | queued (follow-up) |
| R-L1 | `jobs`/`outbox`/`burst.permit` and `floating`'s job pick have no workspace-leading index (Likely) | reliability | net-new | queued (follow-up, with R-M6) |
| R-L2 | The Railway version is reported, never compared with `TESTED_VERSION`; `json_from`'s notice assumption; stdin not DEVNULL | reliability | net-new | queued (follow-up) |
| R-L3 | 429 → exit 2 "refused"; Retry-After never honoured | reliability | net-new | folded (exit 4, "try again"; a watch's retry budget covers the wait) |
| R-L4 | `doctor`: a checkout ahead reads "wrong"; no token + API down → exit 3 | reliability | net-new | accepted (both wrong was the plan's rule; the exit order is the documented report order) |
| R-L5 | No `--debug`, no request id, refusal logs carry no principal | reliability | net-new | queued (follow-up) |
| R-L6 | Packaging: `scripts/migrations/*.sql` not shipped (honestly skipped); Makefile `install` lacks `[cli]`; `install-dev` names an absent extra | reliability | net-new | queued (follow-up, with the Makefile's other targets) |
| R-L7 | `health --help` omits the two monitor bounds; webhook verbs show `--api`; the group lacks `--json` | reliability | net-new | folded (the bounds; the group's `--json`); `--api` stays (harmless, the global option) |
| R-L8 | README's tree lists `cli/`; "488 tests" | reliability | net-new | folded |
| R-L9 | AGENTS.md says the CLI needs the three Telegram variables | reliability | extends #1222 | folded |
| R-L10 | `deployment-options.md` `--service web`; `cloud-deployment.md` ghost `/check-health`, "polling mode", the loop | reliability | net-new | folded |
| R-L11 | `testing-guide.md` lists `tests/cli/`, says CI runs `make test`; `TEST_COVERAGE.md` pins pytest 7.4.3 and an unenforced threshold | reliability | net-new | folded |
| H-1 | A vitest cache under a root `node_modules/` was committed by #1310; the root `.gitignore` never covered it | hygiene | net-new | folded |

## The class sweeps (measured, not asserted)

| Class | Population | Fixed | Left (and why) |
|---|---|---|---|
| `--service web` in live docs | 7 (backup-restore ×2, dev-environment-setup ×2, deployment-options ×2, ci-cd-pipeline ×1) | 7 | 0 |
| `ruff … src/ tests/ cli/` | 4 (dev-environment-setup ×3, ci-cd-pipeline ×2 in one excerpt) | 4 | 0 |
| the 001–021 psql migration loop | 3 (deployment, dev-environment-setup, cloud-deployment) | 3 | 0 |
| `railway shell … -c` | 3 (troubleshooting ×2, cloud-deployment ×1) | 2 (troubleshooting rewritten) | 1 in cloud-deployment.md (a variable listing; the tear-out's) |
| never-run lists outside CLAUDE.md/AGENTS.md | 2 (`.claude/QUICK_REFERENCE.md`, `.claude/PROJECT_CONTEXT.md`) | 2 | 0, and a new one cannot appear unpinned |
| JSON-text casts in `ops_views.py` | 3 (`seconds`, `rung` ×2, `next_run_at`) | `seconds` and both `rung`s through numeric | `next_run_at::timestamptz` (an ISO string by construction) |
| bounded lists keeping the oldest rows | 9 statements (`_AUDIT`, `_OPERATIONS`, `_STORY_CARDS`, `_CARDS`, five `burst` sections) | 9 | 0 |
| legacy tables in live docs (`posting_queue`, `chat_settings`, `api_tokens`) | monitoring, troubleshooting, backup-restore, `.claude/rules/database.md`, `.claude/PROJECT_CONTEXT.md`, `.claude/QUICK_REFERENCE.md`, `.claude/commands/telegram-status.md`, `worker-recovery.md`, `SECURITY_REVIEW.md`, `ROADMAP.md`, `migration-runner.md`, `instagram-login-setup.md` | monitoring, troubleshooting (rewritten), backup-restore (marked) | the rest describe the tier that still runs the worker — the tear-out's (#1216) documentation phase |

## The fold

One PR, under the repository's process: the tests above written red first
(35 red, 451 old green), the fixes, two review lenses, a fold, a fresh
re-verify, the battery `tests/mutations/cli_v2_audit.sh` (35 mutations) run on
the committed tree together with the three phase batteries, CI green, an
admin squash. The ledger of that run is below.

### Run log

- 2026-09-16 15:0x UTC — maps and baseline; security, correctness and
  reliability lanes returned; the test-quality, architecture and access-path
  lanes were killed by the session rate limit (reset 17:40 UTC); the
  test-quality lane's mutant results were salvaged from scratch.
- 15:1x — the five HIGHs verified against source by the orchestrator (the
  verify lane could not run): all upheld; C-H1 upgraded from Likely to
  Confirmed (`'60.0'::int` refused on Postgres 15; the writer's value is
  `60.0`).
- 15:2x–15:4x — red tests, fixes, docs; units 487 passed, the four DB gates
  120 passed, `landing/` 40 vitest + tsc + eslint clean, ruff clean.
- 15:41 — committed as `audit/cli-v2-fold`, draft PR #1314. The batteries on
  the committed tree: `cli_v2_01.sh` 47/47 killed, `cli_v2_02.sh` 33/33 (three
  anchors re-cut for the fold's SQL and window grammar), `cli_v2_03.sh` 57/57
  (one anchor re-cut for the commit match), `cli_v2_audit.sh` 35/35 after two
  equivalent mutants exposed a dead clause in `_of_commit` (removed). 172
  mutations, none surviving, none unapplied.
- CI on #1314: green on the head `458932a` (all eight jobs).
- 16:15 — the rate limit lifted early; the architecture lane returned (2 HIGH,
  7 MEDIUM, 8 LOW); the access-path lane and the two review lenses dispatched.
- Round 2 (the architecture lane): eleven red tests (the ratchet on
  `TARGET_TELEGRAM_` literals, the closed set of reasons, the bounds and
  identities by reference, the exact import closure, the shared idempotency
  fixture on both sides, the renderer totality), then the fixes; units green,
  the DB gates 120 passed, the web's 47 vitest cases green, tsc and eslint
  clean. Ten mutations added to the battery (45).

## Owner queue

- S-M1 the card's attribution (the person's name with the token as a suffix?) — a wording change to the adapter, fork F4 territory.
- S-M2 a workspace-scoped person token — a feature.
- S-L3 `posture` for service identities; S-L4 a rate limit on 401s (edge).
- R-M6 / R-L1 index changes (measure with EXPLAIN on the replayed schema first); D-2 the envelope CHECK.
- The tear-out's documentation phase: every live doc that still describes the legacy tables (the population above).
