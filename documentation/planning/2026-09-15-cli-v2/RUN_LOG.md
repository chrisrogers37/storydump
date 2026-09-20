---
title: "storydump v2 CLI — sprint run log (build-all)"
type: plan
status: completed
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

**Answered 2026-09-16: met.** #1310 (`218c864`), #1311 (`0b0badc`) and #1312 (`4bce602`) merged
under the process — tests red first, two lenses per phase, folds as class sweeps, fresh
re-verifies (three rounds for phase 03), a named mutation per behaviour (47, 33 and 57 killed on
the committed trees, each battery re-run on every later tree), CI green, an admin squash of one
accurate commit each; `cli/` deleted with every live mention outside the owner's own tool
configuration (`tests/test_legacy_cli_gone.py`); the consolidated plan's Live status updated and
the epic and its three phase docs `completed` in #1312; every merge live on Railway (§5). Not
met inside the sprint, by design and queued (§7): the verifications that need an owner-minted
production token or the bot's secrets, and the owner's `.claude/settings.json` rules.

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

Amendment (2026-09-15, phase 02 in review): phase 03 is PREPARED in a worktree branched from the
phase 02 head (`543f267`, branch `implement/cli-v2-03-writes`) while phase 02 waits for CI and its
re-verify read (the session's subagent quota resets 23:10 ET). This is the skill's "plan the
rebase" option, not silent stacking: the moment #1311 squash-merges, `git rebase --onto
origin/main 543f267` on the phase 03 branch, then `git diff origin/main..HEAD --stat` must list
only phase 03's files before the first push. Nothing of phase 03 is pushed before that.

## 4. Phase results

| Phase | Doc | Status | PR | CI | Live |
|---|---|---|---|---|---|
| 01 Tokens | `01_tokens.md` | DONE — merged 2026-09-15 23:01 UTC | #1310 | green (9/9 on `29b20d7`) | Railway deploys of `218c864` watched (§5) |
| 02 Reads | `02_reads.md` | DONE — merged 2026-09-16 02:15 UTC (re-verify: Merge) | #1311 | green (9/9 on `43bd8c2`) | Railway deploys of `0b0badc` watched (§5) |
| 03 Writes, environment, deletion | `03_writes-and-deletion.md` | DONE — merged 2026-09-16 03:18 UTC (three review rounds, §6) | #1312 | green (9/9 on `89b7904`) | Railway deploys of `4bce602` watched with the real `storydump deploys --watch --commit 4bce602` (§5) |

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

### Invariant registry after the phase 01 merge (`218c864`, run on main)

| # | Result |
|---|---|
| I1 | `sessions.py` diff vs the merge base is `new_token` only (13+/2−): the re-draw; `resolve`/`_RESOLVE`/`token_hash` untouched |
| I2 | `test_advertised_ddl` + `test_lineage_lane` in the 74-passed DB run; the tenancy lane inside them; CI Test green |
| I3 | `tests/storydump_cli/test_import_boundary.py` passed (in the 47) |
| I4 | `tests/scripts/test_w4_tap_gate.py` (in the 74) + `test_commands.py` (in the 47) green |
| I5 | `tests/test_agent_docs.py` passed |
| I6 | the CLI's redaction tests passed (in the 103 CLI tests); `grep -rn sdt_ src storydump_cli` shows the prefix constant and the tests only |
| I7 | `tests/scripts/test_service_tokens_gate.py` 3 passed (in the 74) |
| I8 | `python scripts/telegram_ratchet.py` → `[ok] provider_account_ref_log_sites: 0 (baseline 0)`; CI FC-2 green |

### Phase 02 — reads

- Branch `implement/cli-v2-02-reads` cut from `218c864` (no stacking).
- Build (tests first): the views, the router, the allowlist entries and the gate by the runner;
  the CLI read verbs and `--watch` by one background general-purpose agent against a written
  row contract, then cross-checked by that agent against the real routes (one mismatch found and
  fixed: `floating`'s job join, see the plan's Build notes). Composition as in phase 01.
- Gate `tests/scripts/test_ops_views_gate.py`: two workspaces created through the API by two
  people and seeded alike (a floating story with its ready job, a float whose retry died, a
  refused-then-accepted container, a float wait, a review card, a posted story, a card with its
  binding, a failed notification, today's bucket; a real `skip` through each owner's token), plus
  one system job; three arms — every view returns only A's rows as `svc_ingress` under the
  policies; the routes admit a readonly token, a session and a service identity for its own
  workspace, refuse a stranger with 404 and a foreign service identity with `wrong_workspace`;
  and the same reads as a role that bypasses RLS return only A's rows. `3 passed`.
- Evidence (fresh runs): units `tests/src` + agent docs + `tests/storydump_cli`: `3897 passed`
  (+129 over phase 01's merge; the one failure was the producer pin
  `test_operator_floor_preconditions` matching the view's `kind = 'publish_pipeline'` SQL —
  rephrased as `kind IN (...)`, the pin is about producers); `tests/storydump_cli` `204 passed`;
  the view gate `3 passed` (ingress arm, route scopes, bypass-RLS arm); ruff clean.
- Live sample (the demo rig again: a replayed schema seeded with the gate's ledger — a floating
  story with a ready retry, a float whose retry died, a refused-then-accepted container, a float
  wait, a review card, a posted story, a card, a failed notification, today's bucket; the API
  as `svc_ingress`; a readonly token minted through the API): `whoami`, `floating` (both floats,
  `ready (0)` vs `failed (5)`), `floating --watch --every 1` → the first read printed both rows
  as added and exited 6 with "1 floating story whose job has failed", `story <id>` (the intent,
  the float-wait audit row, both permits with variants 0/1 and Meta's 9004/2207052, the card),
  `cards`, `account @demo_acct` (cap 5, today 2/5, zone, next slot, recent outcomes), `jobs
  --since 72h` (groups + failed samples), `outbox` (the failed notification by binding), `burst
  --since 3h` (tap, permit ×2, float_wait, review, outcomes), `--json floating` (one envelope),
  `posture` (role svc_ingress, bypassrls no, 17 tables under RLS, ledger absent on a replayed
  database), `story <unknown>` → exit 1, `--workspace "Demo workspace"` by name, `--workspace
  nope` → exit 1, `--workspace <uuid not a member>` → exit 3, `--since yesterday` → exit 64.
  Transcript in the PR body. One cosmetic defect seen and fixed: the account render doubled the
  `@` on a handle stored with one.
- Battery `tests/mutations/cli_v2_02.sh` on the committed tree: 22 mutations, all killed after
  four test gaps were closed (first run 18 killed, 4 survived, none a defect): a leaked foreign
  `burst` row carries the caller's workspace label, so the gate now asserts no foreign id (the
  other workspace's stories, account, binding) appears in any row; the floating story now also
  has an old failed retry so the live-over-dead preference is exercised; `posture`'s ledger
  answers are pinned by a new unit module (`test_ops_views.py`: absent / unreadable / present,
  the empty story, the burst ordering, the bounds and the tenant predicate on every statement);
  a naive ISO timestamp joined the refused windows.
- **After the review round** (fresh runs on `543f267`, the branch squashed to one commit): units
  `tests/src` + agent docs + `tests/storydump_cli`: `3938 passed, 83 skipped, 39 deselected`
  (+170 over phase 01's merge); `tests/storydump_cli` `213 passed`; the view gate `4 passed`
  (ingress arm, route scopes, bypass-RLS arm, posture's RLS drift + every ledger state live);
  ruff check + format clean; battery `tests/mutations/cli_v2_02.sh` on the committed tree:
  **33 killed, 0 survived, 0 not applied, 0 empty selections** (the run's repairs are in §6);
  `tests/mutations/cli_v2_01.sh` re-run under the stricter check function: 47 killed, none
  flagged. No `landing/` change in this phase — the web gates were not re-run. The ledger's own
  lines were the last amend (docs only; `git diff` between the battery's sha and the pushed sha
  is this file).
- **Merged 2026-09-16 02:15 UTC as `0b0badc` (#1311, admin squash; CI 9/9 green on `43bd8c2`;
  re-verify: Merge).** Railway deploys of `0b0badc` watched with `railway deployment list
  --service {worker,storydump} --json` every 30 s: the worker SUCCESS at once, the API WAITING
  then SUCCESS — both `SUCCESS 0b0badc 2026-09-16T02:15:40.322Z` by 02:25 UTC. Phase 02 is
  LIVE. Invariants I1–I8 re-checked on `main` at `0b0badc`: I1 sessions diff empty and
  the session/API units `397 passed`; I2 advertised DDL + lineage gates, I4 the tap gate, I7 the
  tokens gate: `74 passed` together; the tenancy lane and the FC-2 ratchet clean; I3 the import
  boundary and I5 the agent docs green; I6 `sdt_` appears in `src/`/`storydump_cli/` only as the
  prefix constant, the redaction pattern and docstrings.

### Phase 03 — writes, environment, deletion

- Branch `implement/cli-v2-03-writes` in a worktree, cut from the phase 02 head `543f267`
  (later `43bd8c2`) per the §3 amendment; rebased onto `main` after #1311 merges, then pushed.
- Build (tests first — every new test file was red at collection before a line of the
  implementation existed): the nine write verbs (`commands/writes.py`), `health`/`deploys`/
  `webhook`/`doctor` (`commands/env.py`, `railway.py`, `webhook.py`), the vocabulary's new
  spellings, the deletion and the doc sweep, all by the runner; no background builder this phase.
- Gate `tests/scripts/test_cli_writes_gate.py`: the real CLI over an ASGI bridge (the CLI runs in
  a worker thread; each request is awaited on the app's loop) against the real app as
  `svc_ingress` — a workspace and three tokens minted through the API, six stories seeded; a skip
  lands, its `cli_command` row names the token and the key `skip:<story>`, a re-run replays with
  the dedup row count at 1, a deliberate second execution answers; `approve` refused
  `manual_mode` leaving no `cli_command` row; a readonly token and a service identity exit 3;
  `posted`, `cancel` (the flag), `resolve cancel`; `pause`/`resume` flip `is_paused`; `health`
  through the real routes; a name through the real principal and a stranger's id as 3. `7 passed`.
- Evidence (fresh runs on the branch's one commit over `main` at `0b0badc`, after both review
  rounds): units `tests/src` + agent docs + legacy-gone + `tests/storydump_cli`: `4112 passed,
  83 skipped, 39 deselected` (+174 over the phase 02 merge); the whole DB-gated `tests/scripts`
  suite — the CLI writes gate, the ops and tokens gates, the implicit-admin-fallback gate and
  every other gate — `1398 passed, 1 skipped` in 2 min 51 s (the round-1 evidence had run only
  the CLI gate, which is how the fallback gate's `cli/` pin reached CI); `landing`: vitest `369
  passed` in 34 files (the web's intent-states contract test parses the vocabulary module,
  which changed; no `landing/` file changes); ruff check + format clean; battery
  `tests/mutations/cli_v2_03.sh` on the committed tree: **57 mutations, 57 killed, 0 survived,
  0 not applied, 0 empty selections** (its history: 41 → 40 killed + 1 equivalent mutant;
  43/43; after round 1's fold 53 of 57 with two `NO TEST SELECTED` — a scripted rewrite of the
  test file had dropped two tests, which the hardened check caught — one survivor and one
  stale anchor, repaired to 57/57; after round 2's health rewrite one stale anchor and one
  survivor whose test could not see it, repaired to 57/57); the phase 01 and 02 batteries
  re-run on this tree after each fold: `47 killed` and `33 killed`, none flagged (a sed'd
  runner copy for the worktree — its first cut doubled the venv path and every mutation read
  `KILLED BY ERROR []`; the hardened check flagged it rather than counting kills). A throwaway
  venv with `pip install -e '.[cli]'` of the checkout: `bin/` holds `storydump` and no
  `storydump-cli`, `import cli` does not resolve, and `storydump doctor` there — no token, no
  config, an unlinked directory, against production's public `/health` — reports `token
  missing` with its fix, `api ok` (version 0.2.0), `storage keychain`, `config defaults`,
  `railway wrong` (no project linked in that directory), `ledger skipped`, exit 3.
- Live sample (Path A): the demo rig again (a replayed schema, the gate's ledger, the phase 03
  API as `svc_ingress` on 8001, an operator token minted through the API with the seeded
  session; the real Railway CLI for `deploys`, read-only), the real CLI: `whoami` (the token, its
  role and expiry, the workspace); `floating` (both floats); `skip <open> --workspace "Demo
  workspace"` → "skipped — story … (now skipped)" exit 0; the same again → "already done" exit 0;
  `--json skip` → the envelope with `args`, the key `skip:<story>`, `outcome: replayed`; `skip …
  --idempotency-key second-look` → "answered … (now skipped)"; `approve` on the settled story and
  on the floating one → the port answers with the state (a repeat command answers, F2 (a) of
  the tap plan — the CLI's sentence for `answered` was added from this sample); `resolve
  <review> cancel` → "resolved … (now cancelled)"; `pause`/`resume` → their sentences; `sync
  <unknown>` → exit 1 — its sentence said "no such story", fixed to "no such media source"
  from this sample; `story <open>` shows the tap over channel `cli` and three `cli_command`
  audit rows naming the token and each key; `burst --since 1h` shows the skip as a tap over
  `cli`; `health` → `health ok` with the three surfaces (exit 0); `deploys` → railway 4.30.3,
  project storydump, five deployments per service with commits (`218c864` SUCCESS on both);
  `doctor` → token ok, api ok, storage `env (STORYDUMP_TOKEN)`, config defaults, railway ok,
  ledger WRONG (a replayed database has no runner ledger: "77 migrations in the checkout not
  applied") → exit 4 — the honest answer for the rig; `webhook status` without the variables →
  exit 64 naming `TARGET_TELEGRAM_BOT_TOKEN`, no call made; `--help` with the four sections;
  `skip 0395b173` → usage 64 ("story is a full UUID"); `pause` without `--workspace` → 64.
  Transcript in the PR body. Nothing posted; the rig was dropped afterwards.
- BLOCKED (not silent), with the smallest unblocking action, in §7: `storydump webhook status`
  and `register` against the real bot (the owner exports `TARGET_TELEGRAM_BOT_TOKEN`,
  `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN`, `TARGET_TELEGRAM_BOT_USERNAME` from Railway and runs
  them); the plan's production checklist items (a real `skip` through an owner-minted
  operator token with its card and audit rows; `deploys --watch` following a real deploy —
  the plain `deploys` read WAS live).
- Deploy reachability: merged = live for the API and the worker (Railway auto-deploys `main`;
  nothing in this phase needs a migration); the CLI itself is installed from the checkout
  (`pip install -e '.[cli]'`), so "live" for the CLI is the merge.
- **Merged 2026-09-16 03:18 UTC as `4bce602` (#1312, admin squash; CI 9/9 green on `89b7904`;
  three review rounds folded, §6).** Final evidence on `89b7904`: units `4113 passed, 83
  skipped, 39 deselected`; the whole DB-gated `tests/scripts` suite `1398 passed, 1 skipped`;
  battery `tests/mutations/cli_v2_03.sh` 57/57; the phase 01 and 02 batteries 47/47 and 33/33.
  Railway deploys of `4bce602` followed with the merged CLI itself — `storydump deploys --watch
  --commit 4bce602 --every 30` from the linked checkout (`pip install -e '.[cli]'` into the
  main venv first; the console script had never been installed there): `03:19:34 added
  storydump … WAITING`, `added worker … BUILDING`, `03:20:06 changed worker … SUCCESS`,
  `03:26:46 changed storydump … BUILDING`, `03:27:50 changed storydump … SUCCESS`, exit 0 —
  the plan's "`deploys --watch` follows a real deploy to both services live" item, done. Phase
  03 is LIVE; the epic is complete (the goal condition above).
  Invariants I1–I8 re-checked on `main` at `4bce602`: I1 the sessions diff empty and the
  session/API/CLI units `439 passed`; I2 advertised DDL + lineage, I4 the tap gate, I7 the
  tokens gate and the implicit-admin-fallback gate `88 passed` together; the tenancy lane and
  the FC-2 ratchet clean (I8: the baseline shrank by two, never grew); I3 the import boundary
  and I5 the agent docs green; I6 `sdt_` appears in `src/`/`storydump_cli/` only as the prefix
  constant, the redaction pattern and docstrings. One local-only red: `tests/test_legacy_cli_gone.py`
  saw `cli/` still present in the runner's checkout — ignored `__pycache__` leftovers of the
  deleted package, not files git tracks (a fresh checkout has none; removed, 4 passed).

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

### Phase 02 — round 1 (two lenses: structural + simplify, adversarial)

Verdicts: both "request changes", neither with a blocker; no cross-tenant read found by either
(every statement's predicates read line by line). Folded, each as a class sweep:

- **`burst` rows carried the caller's workspace label** (adversarial Major 1; the bypass arm
  could not see a leaked section row). Class: every row a view emits. Every burst section now
  selects the table's own `workspace_id`; the outcome census groups by it; the gate asserts no
  foreign id (the other workspace's stories, account, binding) in any row and B's skip is not
  counted into A's outcomes. Unit + battery (the tap mutation is killed by the bypass arm).
- **A `since` overflow was a 500 at the API and a traceback at the CLI** (adversarial Major 2).
  Class: every parse of a window. One grammar now lives in the vocabulary module
  (`window_start`): span, timestamp, bare date; thirty days at most; never the future;
  `OverflowError` caught; the API answers 422 and the CLI usage 64. Units on both sides + the
  overflow route test.
- **Burst watch keys collided** (both lenses): two permits of one story in one transaction, one
  post past two waiters. The key carries `generation` and `waiting_id`. Unit.
- **The watch exited 6 on a failure that predated it** (both lenses). The first read is the
  baseline; failure is judged on rows that arrive or change afterwards. Units rewritten
  (a pre-existing failure is printed, not fatal; a group that grows is fatal).
- **`posture` could not show the two facts it exists for** (structural Majors 1–2): the RLS
  list filtered on `relrowsecurity` (a dropped policy vanished) → every tenant-plane table with
  its state; the human render dropped the ledger state → printed. The new gate test found a
  third: `to_regclass` on a schema without USAGE raises, so a role without the grant got a
  500 → the ledger is probed by catalog oid (absent / unreadable / present, each proven live).
- **The sibling join had no positive test and no bound on its waits side** (both): the seed's
  wait now precedes the post by a minute (a real `sibling` row asserted); the waits side is
  bounded to the window less a day.
- Smaller folds: `jobs`/`outbox` list owed rows at any age and window the finished ones (the
  probes' "not finished, any age"); handles match case-insensitively; `supersedes_ref` on cards
  (the twin signal the plan named); `rolsuper` counts as bypassing; `--every` at least a second;
  `--limit` 1–500 and a UUID story id as usage errors; every workspace of a duplicated name is
  read; help text names the watch's waiting; the producer-pin dodge is commented; docstrings say
  "one bounded read per list"; `07` §1 records posture's disclosure as deliberate.
- Recorded, not built: the CLI's `_run_view` restructuring the structural lens sketched; keying
  the watch's diff on full tuples; `Watched.kind` duplication.
- **The battery after the fold** (found by re-running it on the committed tree, as the process
  requires): seven anchors had gone stale with the fold (re-anchored); one new mutation was an
  equivalent mutant (`has_schema_privilege` by NAME never raises — it is the TABLE lookup by name
  that needs schema USAGE; replaced by that); one "kill" was a `-k` selector that matched no
  test — pytest exits 5 on an empty selection and the check function read any non-zero exit
  as a kill. Class: every battery's check function. Swept `tests/mutations/*.sh` (two files):
  both now flag `NO TEST SELECTED` and `KILLED BY ERROR` (a collection or fixture error is not
  a test's verdict) instead of counting them; the phase 01 battery was re-run under the stricter
  check (result in §5). The missing test (`--workspace <name>` shared by two workspaces reads
  both) was written; the gate reads the account by an upper-cased handle so the
  case-insensitive match has a killing test too.
- **Re-verify (a fresh read-only agent on `43bd8c2`): Merge.** Every fold claim confirmed with
  file:line; all 33 mutations located exactly once, none equivalent (the reordered ledger-by-name
  mutant raises `permission denied for schema` in the gate's no-grant step). Three sentences
  noted as imprecise — the guide's "`--json` on any verb gives `data.workspaces`" and "the others
  by the window", the CHANGELOG's "every verb reads every workspace" (`posture` is the exception;
  `cards`/`account` are LIMIT-bounded) — tidied in PR 3, which touches the same files. Recorded,
  not built: a live arm for posture's `rolsuper` answer and for the owed-rows-at-any-age branch
  (units only). Outside the phase: a latent harness interaction (`@pytest.mark.asyncio` then
  `asyncio.run()` under `filterwarnings = error`, reproduced on `main` at `218c864`) → §7.

### Phase 03 — round 1 (two lenses: structural + simplify, adversarial; on `f06ab78`)

Verdicts: both "request changes", both naming the same blocker; no secret leak, no cross-tenant
write found. Folded, each as a class sweep:

- **The workspace verbs' keys swallowed real actions** (both lenses, blocker). `pause`/`resume`
  keyed on the day (then the minute) replayed a second pause after a resume — "already done"
  while the workspace stayed resumed; `sync` the same; `resolve` keyed on the story alone
  replayed a second retry after a re-park and turned `retry` then `cancel` into an
  `admission_conflict` in the API's words. The plan's step 1 mis-cited the web's intent-command
  convention as a workspace one. Class: every key the CLI mints. Now the web's own identities
  (`landing/src/lib/commands.ts`): a story verb `<command>:<story>`; a resolution
  `resolve_review:<story>:<resolution>[:<verdict>]:<episode>` with the episode read from the
  story view; a FRESH key per invocation for `pause`, `resume`, `sync` (`fresh_key`); the
  ingress's `admission_conflict` has a sentence and a fix naming `--idempotency-key`. Units for
  each shape; the gate now runs pause → resume → pause and asserts every one EXECUTES and lands,
  and `resolve retry` refused `manual_mode` (the port re-checks approve's gates) then `resolve
  cancel` executed under an episode key. Docs swept: `writes.py`, `AGENTS.md` §5, the guide,
  the CHANGELOG, the Build notes, every `--idempotency-key` help.
- **`health` judged payloads the API never sends** (both, major). The two dependency surfaces
  carry aggregates, no `status`; a dead worker or a sixteen-day silence read "ok". Class: every
  verdict the CLI passes on a surface. Now the fleet monitors' own signals (`overdue_ready`,
  `stalled`, `max_lag_seconds` past 600 s, 48 h of silence with an active destination), the
  thresholds in the vocabulary and pinned equal to the stdlib-only monitors' defaults by a
  test; the fixtures rewritten to the real shapes; a 503 surface reported as its error (the
  report is still printed) rather than lost. `doctor`'s API check reads `/health` alone.
- **`deploys --watch` could end on the previous deploy; the environment was unchecked**
  (adversarial, majors). `--commit <sha>` gates done and failure on that commit's rows; every
  read names `--environment production` (the binary defaults to the linked one); rows sorted
  newest first by the verb; SLEEPING/SKIPPED count as done; a notice before the JSON is
  skipped; a hung binary is exit 5. The fixture is now the binary's whole real answers.
- **The never-run block omitted `resolve … retry` and `webhook register`** (adversarial,
  major): both added to both documents; the paragraph's false rationale ("only the irreversible
  ones") rewritten. `.claude/settings.json` (tracked, the owner's tool configuration, not
  writable by the runner) still carries the legacy allow and deny rules → §7 with the
  replacement rules. The CHANGELOG no longer claims "every live mention" is gone.
- **The bot-token redaction ate a `sync` key** (structural, major): `(?<![\w-])` before the
  digit run; test_output pins a sync key surviving and a Bot API URL struck.
- Minors folded: the "no fixing verb" mutant that survived (the reason sentence also named the
  verb → the test asserts the fix's own words); `test_help`'s example assertion that Click's
  usage line satisfied; `doctor`'s `count("ok")`; a logged-out Railway is `missing` (the plan's
  checklist); the checkout-behind-the-deployment branch tested and mutated; `doctor`'s railway
  value no longer carries the account line; `rotate-keys`' disposition says the job is
  registered but unbuilt; the runbook's "exits 1" → 4; README heading numbering; dead names
  (`latest_status`, `CheckFn`, `__all__`) deleted; the spec dropped from the legacy-gone
  exemptions; `TimeoutExpired` is exit 5.
- Recorded, not built: `doctor` as a table of check functions; a `name` on each `Backend`;
  `Client.health()` already tolerant; the seams module (both lenses agree leaving it is right —
  a rename-only move with two batteries anchored on the names); `app.py` reading the three
  Telegram variables as literals rather than the vocabulary's constants (a follow-up in the
  API, outside the CLI's scope); the `sync` verb's `already_pending` answer has no sentence of
  its own (the port's `executed`/`enqueued` words stand).

### Phase 03 — round 2 (the fresh re-verify on `d95b13d`, and CI on `179ca82`)

Verdict: "request changes" — one regression and a set of overstatements; every fold claim of
round 1 confirmed with file:line. Folded as class sweeps:

- **The implicit-admin-fallback gate still scanned and pinned `cli/`** (the verdict; CI's `Test`
  job red on the same two tests). Class: every test that names the deleted tree. Swept:
  `tests/scripts/test_no_implicit_admin_fallback.py` (`ROOTS`, the one declared grant
  `cli/commands/backfill.py:118`, its synthetic trees now under `src/`), the SYSTEM_SCOPE
  census (already moved 49 → 40), `TEST_COVERAGE.md`'s legacy `tests/cli/` inventory, the two
  `.claude/*.md` directory tables. The evidence run that missed it had not included
  `tests/scripts` whole; the whole suite ran locally afterwards (`489 passed`).
- **`health` diverged from the fleet monitors both ways** (major): the round-1 fold re-derived
  the signals with its own thresholds — silence gated on an active destination (an exemption
  `posting_health.py` forbids by name), no never-posted grace, no stale-worker rule, no
  strictness about a mistyped payload; and it paged where the monitors do not (a briefly-due
  cursor, a due job inside the 900 s grace). Class: every verdict the CLI passes on. Now the
  monitors' OWN `classify` (`scripts/scheduling_monitor.py`, `scripts/posting_monitor.py`,
  stdlib-only, imported) with their defaults; a surface is not well exactly when its monitor
  would page; the verdicts ride the report. The vocabulary's copied thresholds and their pin
  test are gone. Fixtures satisfy the monitors' strictness; units per verdict (worker down
  past the grace, a due job inside it, a stale worker, a lag past 600 s, a briefly-due cursor,
  silence with and without a destination, a first post past and inside its grace, a mistyped
  payload, a 503).
- **Overstatements** (docs vs code): the root help said every write's key is deterministic;
  "the web's identities" claimed the verdict segment and a trailing `:` for an empty episode
  (the verdict is the CLI's addition, the empty episode is now omitted as the web omits it);
  "the fixture is the binary's whole real answers" (two rows per service); the CHANGELOG's
  "every live mention" (now "outside the owner's `.claude/settings.json`"); `test_env`'s
  "worst finding" (it is the first). Each sentence rewritten.
- **CI's unraisable-warning flake landed on the new gate** (`test_posted_cancel_and_resolve…`,
  `ResourceWarning: unclosed socket` ×3 from another test's event loop, in CI's ordering). The
  gate closes its ASGI responses explicitly and ignores `PytestUnraisableExceptionWarning`
  (its own loops, engines, clients and connections are closed); the root cause stays in §7.
- Minors: `_INVOCATION` steps over global options before the verb and reads `~~~` fences and
  info strings with attributes; a hung `railway` (`TimeoutExpired`) has a unit; `resolve`'s
  help says a key of your own skips the story read.

### Phase 03 — round 3 (the final fresh re-verify on `5783ab2`)

Verdict: "request changes" on the battery's state at that commit — the two defects the runner's
own re-run had already repaired in the next commit (`6a36543`): the "quiet estate" mutation
survived because no unit fed `health` a `no-signal` scheduling surface (a unit now does, for
`no-signal` and `worker-unknown`), and the "bad status word" anchor had gone stale with the
health rewrite (re-anchored); the whole battery re-run on `6a36543`: 57/57. Every other claim
confirmed with file:line, the shipped code called sound. Wording folded: `change_role` is
registered but NOT built (answers 501) — the deployment guide, the security review and the
CHANGELOG's `list-users`/`promote-user` row said the web changes roles; "Settings › Schedule" and
"Settings › Members" are cards under Settings › General, not tabs (README, Makefile, the
deployment guide); `webhook status` changes nothing but is not literally read-only traffic (its
door check is an empty POST the API refuses); the CLI's two bounds against the pollers — no
watch clock for the never-posted grace, one unreachable reading rather than two — stated in the
help and the Build notes; a hung `railway` gets its own fix line; the consolidated plan's
fixed-constraints doc named `cli/commands/instagram.py` in the present tense. Recorded, not
built: a non-default poller threshold (`--stall-threshold`, `--silence-threshold`, `--grace`)
would differ from the CLI's module defaults.

## 7. Owner-decision queue

- The `/os` charter question (project vs. single ticket vs. skip) — unanswered; no ticket created.
- A screen recording of Settings › API tokens minting a token (Path B evidence) — the runner
  could not drive a browser; record it on production after the deploy, or connect the Chrome
  extension and ask for it.
- Live verification items the runner never performs: `storydump skip <story>` against a real story
  (phase 03), `storydump burst --since …` against production (phase 02), the 077 apply in production
  (the worker's pre-deploy runner — confirmed from the deploy log, not run by hand).
- **Phase 03 — BLOCKED items, each with the smallest unblocking action:** `storydump webhook
  status` and `storydump webhook register` against the real bot — the owner exports
  `TARGET_TELEGRAM_BOT_TOKEN`, `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN` and
  `TARGET_TELEGRAM_BOT_USERNAME` from Railway in their own shell and runs them (the CLI never
  prints them); a real `skip` through an owner-minted operator token with its card and its
  `cli_command` audit row — the owner mints a token under Settings › API tokens and runs
  `storydump skip <story> --workspace <ws>` on an awaiting story; `storydump deploys --watch`
  following a real deploy — run it while the next merge deploys (the plain read was live).
- **Owner's own tool configuration:** `.claude/settings.json` (tracked, but the runner's sandbox
  refuses to write it) still allow-lists six `storydump-cli` commands and DENIES four
  (`process-queue`, `create-schedule`, `reset-queue`, `instagram-auth`) — the one
  machine-enforced never-run guard, now naming nothing. Replace the four deny rules with
  `Bash(storydump approve*)`, `Bash(storydump cancel*)`, `Bash(storydump resolve*)`,
  `Bash(storydump tokens revoke*)`, `Bash(storydump webhook register*)`,
  `Bash(storydump webhook deregister*)`, and delete the six allow rules.
- **A latent test-harness flake outside the epic** (found by the phase 02 re-verify; seen in CI
  on the phase 03 writes gate, which now ignores the warning class for itself): a
  `@pytest.mark.asyncio` test followed by a test that calls `asyncio.run()` trips
  `PytestUnraisableExceptionWarning: unclosed event loop` under `pytest.ini`'s
  `filterwarnings = error` when the loop's last reference is dropped mid-run; reproduced on
  `main` at `218c864` with `tests/src/api/test_token_principal.py::TestRequireSession` paired
  with `test_operator_floor_preconditions.py`. Not seen in CI's ordering; a one-line follow-up
  (close the loop in the asyncio test's fixture) if it ever surfaces there. *(It surfaced in CI on
  #1314 and was fixed at the root on 2026-09-16: `tests/conftest.py::pytest_sessionstart` sets
  `asyncio.set_event_loop(None)`.)*
