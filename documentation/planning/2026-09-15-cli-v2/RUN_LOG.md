---
title: "storydump v2 CLI — sprint run log (build-all)"
type: plan
status: active
owner: chris
created: 2026-09-15
tags: [cli, api, auth, devx, legacy-retirement, run-log]
links: ["https://github.com/chrisrogers37/storydump/pull/1309"]
---

## Summary

The governed run of the three phases in `00_EPIC.md` (merged as #1309, `ebbc61d`), started on the
owner's directive of 2026-09-15 ("build-all the entire bill of planned work, all phases within this
scoped epic"). Order: 01 tokens → 02 reads → 03 writes-and-deletion — the epic's critical path (02
needs 01's principal, allowlist and envelope; 03 needs both; the legacy `cli/` goes last, F8, so the
new verbs exist before the old are deleted). Goal condition: the epic's verification checklist —
three PRs merged under the repository's process, `cli/` gone, the consolidated plan's Live status
updated, the epic `completed`.

## 1. Kickoff gates

| Gate | Disposition |
|---|---|
| Main at or after `5ea4ea6` (the heal fix) | passed — main is `ebbc61d` |
| All ten decision forks locked with ratifier + evidence | passed — F1–F10 read `Status: locked` in `00_EPIC.md` |
| Owner authorization to change an auth surface (tokens as principals) | passed — the owner's `/goal` directive, 2026-09-15, names all phases; the plan and its forks were ratified by the owner the same day |
| Repository safety rules (never trigger posting; never mutate production; never print a secret) | standing — checklist items that need production or a real story are queued to the owner (§7), never run by the runner |
| Linear ticket for the workstream | owner-gated — the `/os` charter question has never been answered; no ticket is created without approval (§7) |
| Local test substrate (units with the sandbox off; DB gates on the Docker `storydump-test-pg` at 65433) | checked at kickoff, recorded in §5 |

## 2. Invariant registry — re-checked after every merge

| # | Invariant | Check | On breakage |
|---|---|---|---|
| I1 | The web's session path is byte-for-byte unchanged (the `sdt_` prefix routes only tokens) | `git diff <merge-base> -- src/services/target/sessions.py` empty; the session tests in `tests/src/api/` and `tests/src/services/target/test_sessions.py` green | stop; a token change that touches the session path is redesigned |
| I2 | The schema ratchets agree (advertised DDL manifest ↔ `07` blocks ↔ migration files ↔ lineage list ↔ tenancy lane) | `pytest tests/scripts/test_advertised_ddl.py tests/scripts/test_lineage_lane.py` (DB gate) + `python -m scripts.tenancy_gate` per CI | stop; fix the ratchet in the same PR |
| I3 | The CLI package reaches `src` only through the vocabulary module and imports no database driver | `pytest tests/storydump_cli/test_import_boundary.py` (from phase 01) | stop |
| I4 | The command port's semantics are unchanged (the Telegram tap budget and the port's gates) | `pytest tests/scripts/test_w4_tap_gate.py tests/src/services/target/test_commands.py` green | stop; the adapter, not the port, carries the token's facts |
| I5 | The never-run lists agree with the CLI and each other | `pytest tests/test_agent_docs.py` green | stop |
| I6 | No secret reaches a terminal or a log (`sdt_…`, database URLs, webhook secrets) | the redaction tests in `tests/storydump_cli/` + `grep -rn "sdt_" src/ storydump_cli/ --include=*.py` shows only the prefix constant and tests | stop |
| I7 | A token is minted only from a signed-in session; a service identity never writes | `pytest tests/scripts/test_service_tokens_gate.py` (from phase 01) | stop |
| I8 | The FC-2 Telegram ratchet baseline never grows | `python -m scripts.telegram_ratchet` per CI (the "FC-2 Telegram ratchet" check) | stop |

## 3. Merge topology

One repository. Squash merges (admin, after two review lenses + fold + re-verify + CI green), the
branch squashed to one accurate commit first (the repo ships commit messages, not PR bodies). **No
stacking:** each phase branches from fresh `main` after the previous phase merges. Deploy
reachability: `main` auto-deploys the API (`storydump`) and the worker on Railway — the worker's
pre-deploy runner applies migrations — and the web (`landing/`) on Vercel; a phase is "live" only
once `railway deployment list` shows SUCCESS for both services on its merge commit (and Vercel for
the web). Branch names: `implement/cli-v2-01-tokens`, `implement/cli-v2-02-reads`,
`implement/cli-v2-03-writes-and-deletion`.

## 4. Phase results

| Phase | Doc | Status | PR | CI | Live |
|---|---|---|---|---|---|
| 01 Tokens | `01_tokens.md` | in progress | — | — | — |
| 02 Reads | `02_reads.md` | queued | — | — | — |
| 03 Writes, environment, deletion | `03_writes-and-deletion.md` | queued | — | — | — |

## 5. Per-phase entries

### Phase 01 — tokens

- Workspace: branch `implement/cli-v2-01-tokens` in the main checkout (phases are sequential, so
  the one checkout is the isolated workspace; the venv and `landing/node_modules` are shared).
- Substrate: Docker `storydump-test-pg` (postgres:15, 65433) up; Python 3.12.14 venv; ruff 0.15.22.
- Baseline on `ebbc61d` (units, sandbox off, egress-floor tests deselected):
  `3552 passed, 83 skipped, 39 deselected in 29.07s`.
- Plan vs. codebase (build step 2, three explorer maps, 69 evidence lines re-verified): the plan
  held; four deltas recorded in `01_tokens.md` › Build notes (config.json on Python 3.10; the CLI
  tests' directory and the `main` entry point; how the two audit rows pair; a service identity
  revokes only itself). The challenge round ran as the synthesis pass: no open decisions (all ten
  forks locked).
- Build (tests first, red seen before each unit): backend by the runner; the web panel and the
  CLI package by two parallel general-purpose agents against one written contract, then verified
  by the runner. Composition: `build` in full; `worktree` satisfied by the one sequential
  checkout; the simplify pass is folded into the structural review lens (below).
- Evidence (fresh runs, refreshed after the fold — the first-commit numbers were 3625 / 85 /
  2 / 379 / 39):
  - units `tests/src tests/test_agent_docs.py`: `3625 passed, 83 skipped, 39 deselected`
    (+73 over the baseline); `tests/storydump_cli`: `85 passed`.
  - DB gates: schema ratchets and runner (`test_advertised_ddl`, `test_lineage_lane`,
    `test_advertised_ddl_replay`, `test_migration_gate`, the five runner suites):
    `104 passed`; `test_service_tokens_gate.py` (as `svc_ingress`) `2 passed`;
    `test_migration_runner_ledger_grant.py` `3 passed`.
  - web (`landing/`): `npx vitest run` `34 files, 379 tests passed` (+73); `tsc --noEmit` 0;
    `eslint .` 0.
  - ruff check + format clean on every changed Python path.
  - Mutation battery `tests/mutations/cli_v2_01.sh`: 39 mutations, all killed. First run: 37
    killed, 2 survived, 5 gate mutations invalid (the gate setup errored because the demo
    database still held grants to the service roles the gate drops — dropped, re-run, all 5
    killed). The two survivors: the expiry check (killed on a clean re-run; the first run
    overlapped the demo servers) and the envelope's both-present case (no test existed —
    `tests/src/services/target/test_vocabulary.py` added: the closed sets parsed from the
    migrations' CHECK lists, the exit-code table, eleven malformed envelopes; 34 tests).
  - CI on the draft PR: the Test job failed once — `test_rls_runtime_harness.py`'s sweep test
    seeded a `service_tokens` row with no subject, which 077 now refuses; the seed gives the
    token its user (2 passed locally). Everything else was green on the first run.
  - Live sample, full stack (local API as `svc_ingress` on a replayed schema with one seeded
    workspace and session; the landing dev server's BFF in front): mint a person-bound token
    through the BFF → the API resolves the secret as a token principal (`kind: token`, the
    person's workspace under `owner`) → `GET /api/v1/me` refuses it `session_required` → an
    admin mints a service identity through the BFF (role forced `readonly`) → it sees its one
    workspace read-only → `/dashboard/settings?tab=tokens` server-renders the tab with both
    tokens, the mint and revoke controls, and no `sdt_` value anywhere in the page → revoke
    through the BFF → the API answers 401. The real CLI against the same API: `login
    --insecure-storage` from stdin (config dir 0700, token 0600, `token_storage: file`),
    `whoami` (human and `--json` envelope), `tokens list`, `tokens revoke` ("revoked cli-demo"),
    then `whoami` → exit 3 with the not-authorized sentence and an error envelope, `logout`,
    `whoami` → "not signed in" exit 3, `--bogus` → usage exit 64, a non-`sdt_` secret → exit 64,
    an unreachable API → exit 4. Transcript in the PR body.
  - BLOCKED (not silent): a screen recording of the mint flow (the e2e contract's Path B). The
    Claude Chrome extension is not connected in this session; screenshots via headless
    Chromium are attempted below, and the owner can record the flow on production after the
    deploy. Smallest unblocking action: connect the extension (or open Settings › API tokens
    on production after the deploy and mint a token).
- Deploy reachability: `main` auto-deploys the API and the worker on Railway (the worker's
  pre-deploy runner applies 077 and the runner grants its ledger on that run) and the web on
  Vercel. Merged = live once both Railway deploys read SUCCESS on the merge commit; the runner's
  log line for 077 is the confirmation to read.

## 6. Review rounds

### Phase 01 — round 1 (two lenses: structural + simplify, adversarial; both re-dispatched
after the first pair died on the session rate limit)

Verdicts: adversarial "request changes" (one Major, nine Minors, no blocker); structural "request
changes, narrowly" (six Majors, none a runtime defect; ten Minors; eight simplifications; a list
of untested behaviours). Every finding folded or recorded; the class sweep per finding:

- **A `readonly` person-bound token could revoke the person's other tokens and, as an admin,
  the workspace's identities** (adversarial Major). Class: every write a token can reach outside
  `_dispatch`. Swept `src/api/routes/tokens.py` for its two DELETE routes and `v1.py` for the
  command route (already fenced): `_token_may_revoke` on both DELETEs — a token revokes itself
  freely, anything else needs `operator`. Unit (3) + gate (both routes, the rows stay live) +
  mutation.
- **The `cli_command` row's entity was the caller's claim** (adversarial Minor 1). Class: every
  field of that row taken from the request. `detail.kind` is the command's; `entity_id` now comes
  from the port's answer (`result.data["intent_id"]`), a non-uuid or no intent falls back to the
  workspace. Unit (3) + mutation.
- **Two claimed ratchets did not exist** (both lenses: `_TOKEN_STATUS` totality; the vocabulary's
  CHECK lists). Class: every "pinned by a test" sentence in the diff. Swept the new docstrings:
  `app.py` (now `test_token_reasons`), `vocabulary.py` (`test_vocabulary.py`, added before this
  round for the battery survivor), `service_tokens.py` (the stamp claim — see below); also
  `workspaces.INTENT_STATES` is now the vocabulary's tuple (one spelling), and the landing app's
  `intent-states-contract.test.ts` reads `vocabulary.py`.
- **`keyring` in `requirements.txt` shipped to the API and the worker** (both lenses). Removed;
  the extra alone carries it; CI's Test job runs the CLI suite without it (the keychain import is
  lazy; the tests fake the module).
- **`last_used_at` stamped on refused authentications** (both lenses). The person-bound stamp is
  gated on the person being active; a service identity is stamped only after its workspace read
  passes. Unit (3) + gate (`last_used_at` stays NULL after a refused attempt) + mutations (2).
- **A bearer over plain http off this machine** (adversarial Minor 5): `refuse_plain_http` in the
  client, loopback allowed, `STORYDUMP_INSECURE_HTTP=1` for a dev server elsewhere. Unit (client
  ×3, main ×2) + mutation.
- **A file login left the keychain copy** (adversarial Minor 6): retired best-effort. Unit + mutation.
- **A session value could wear the token prefix** (adversarial Minor 7): `sessions.new_token`
  re-draws; the one edit to `sessions.py`, and I1 now reads "the session resolution path". Unit
  (3) + mutation.
- **Dead browser-side list path** (structural Major 6): `listMyTokens`/`listServiceTokens`/
  `listTokensRefusalCopy`, two BFF GET handlers and their tests removed (the page reads both lists
  server-side). The DELETE BFF routes now answer 502 `malformed_response` when the API did not
  confirm, the mint routes' convention.
- Smaller folds: `require_own_workspace` shared by both routers; the audit row's channel from the
  GUC like its actor; `jsonable_encoder` instead of a hand reshape; the unused `TOKEN_PREFIX`
  alias and its test gone; token id path segments are `uuid.UUID` (422 like every other id);
  docstrings corrected (`me_principal`, `_presenting_user`, `ApiError`); the keychain `delete`
  wraps the store's error like `get`/`set`; login's non-auth error no longer says "no such
  workspace"; the help test asserts an Example; the import-boundary test walks every import
  statement (lazy ones included); `07` §1 and the plan's Build notes updated; `isAdmin` reused
  on the settings page.
- Untested behaviours named by the structural lens, now tested: a member's operator token acts as
  a member and never above (gate); a readonly token admitted to `/me/principal` and `/me/tokens`
  (gate); an operator token revoking the person's other token (gate); the workspace fallback of
  the audit entity (unit); login where keyring chose `fail` (unit, end to end); a `KeyringError`
  on delete (unit, after the re-verify).
- Recorded, not built (owner queue / phase 03): an audit row for minting and revoking a token
  (`audit_events` is workspace-keyed; a person-bound token has no workspace — a user-plane audit
  is a design question); the `effective role` (membership ∧ token role) is reported as two facts
  rather than computed; the private `v1._open_tenant/_admin/_json_object` seams shared by two
  routers become a seams module when phase 02's `/ops` router joins; the CLI's fix sentences and
  CLI-only reasons could live beside the vocabulary's sentences; the router-level
  `require_session` dependency (a judgment call left per-route, where the enumeration test pins
  it); the tab's client-side `new Date()` for expiry copy (hydration text at an hour boundary).
- Production probe (read-only, `railway run --service worker`): `service_tokens` has 0 rows, so
  077's subject check lands on an empty table.
- Battery after the fold: 47 mutations (39 + 8 for the fold's fixes), all killed, on the
  committed tree (`b26da0e`). Two tool findings on the way: (1) four anchors went stale with the fold's
  renames — re-anchored; (2) the expiry mutation "survived" twice in the full run and never
  alone: two adjacent mutations of the same byte size written within the same second match the
  interpreter's bytecode cache (mtime + size), so the second run executed the first mutation's
  code — the battery now purges the mutated file's `__pycache__` per mutation and runs with
  `PYTHONDONTWRITEBYTECODE=1`. Also: a refused authentication never persists a stamp with or
  without the CTE guard, because the resolver runs inside the authentication transaction, which
  rolls back on refusal — the guard documents the reading and is pinned by a unit test; the
  gate's refused-attempt check proves the rollback.

## 7. Owner-decision queue

- The `/os` charter question (project vs. single ticket vs. skip) — unanswered; no ticket created.
- A screen recording of Settings › API tokens minting a token (Path B evidence) — the runner
  could not drive a browser; record it on production after the deploy, or connect the Chrome
  extension and ask for it.
- Live verification items the runner never performs: `storydump skip <story>` against a real story
  (phase 03), `storydump burst --since …` against production (phase 02), the 077 apply in production
  (the worker's pre-deploy runner — confirmed from the deploy log, not run by hand).
