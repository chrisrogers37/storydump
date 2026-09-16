---
title: "CLI v2 — phase 03: the command verbs, health, deploys, webhook, doctor, and the legacy CLI's deletion (PR 3)"
type: plan
status: completed
owner: chris
created: 2026-09-15
tags: [plan, cli, api, devx, legacy-retirement]
links: []
---

## Summary

The CLI becomes a complete console: the command port's verbs as writes with deterministic
idempotency, `health`, `deploys`, `webhook` and `doctor`, and the legacy `cli/` package goes —
with its console script, Makefile targets, tests, ratchet-baseline entries and every mention in
the repository, the never-run lists rewritten and re-pinned by a doc test that walks nested
groups.

## Evidence

- `src/api/routes/v1.py:932` `POST /workspaces/{ws}/commands/{command}`; `:112-116` the web's
  key `<command>:<intent_id>`; `src/api/app.py:324-327` a replay answers `{"outcome":
  "replayed"}` with no data; `:279-291` only `CommandRefused` carries `reason`.
- `src/services/target/command_executors.py:630-654` — `resolve_review` takes the resolutions
  `retry | posted | cancel`; `retry` after a lost publish answer refuses `may_have_posted` unless
  a `not_posted` verdict rides the command (`:639-644`; the tap's `notposted`,
  `telegram_dispatch.py:94`).
- `src/api/app.py:636` `/health`, `:663` `/health/scheduling`, `:740` `/health/posting`.
- `scripts/telegram_webhook.py:187` `cmd_status`, `:259` `cmd_register`, `:287` `cmd_deregister`;
  env `TARGET_TELEGRAM_BOT_TOKEN`, `TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN`,
  `TARGET_TELEGRAM_BOT_USERNAME`; no secret ever printed.
- `cli/main.py:49-78` registers 31 commands, among them `rotate-keys`, `revoke-tokens`,
  `instagram-auth`, `promote-user`, `create-schedule`, `sync-media`; every `cli/commands/*.py`
  imports legacy modules; `setup.py:32-34`; `Makefile:131-149` six targets.
- `storydump-cli` is named in `CLAUDE.md:30-33`, `AGENTS.md:21-24` and `:116-122`, the three
  `documentation/operations/` runbooks (troubleshooting, monitoring, backup-restore),
  `documentation/guides/*` (#1205), `SECURITY_REVIEW.md`, `ROADMAP.md`, and
  `07-security-model.md`.
- `tests/test_agent_docs.py:42` `_INVOCATION` captures one word; `:70` imports `cli.main`;
  `_registry()` reads top-level commands; `:59` the never-run block; `:76`, `:89` the two
  guarantees.
- `scripts/telegram_ratchet_baseline.json` lists `cli/commands/backfill.py::backfill_instagram`
  and `cli/commands/tokens.py::revoke_tokens`.
- `railway whoami` and `railway deployment list --service <svc> --json` work on the developer
  machine; another session's `railway login` is known to drop the project link (memory,
  2026-09-13).

## Implementation Plan

### Dependencies
Phases 01 and 02.

### Blocks
#1216 step 4 closes; step 3's `cli/` line is done.

### Steps

1. **Write verbs** in `storydump_cli/commands/writes.py`: `approve`, `skip`, `reject`, `posted`,
   `cancel <story>`, `resolve <story> retry|posted|cancel [--not-posted]` (the flag carries the
   `not_posted` verdict for a lost publish answer), `pause`, `resume`, `sync`. Each calls the
   command route with the token and a **deterministic** `Idempotency-Key` —
   `<command>:<intent_id>` for story commands, `<command>:<workspace>:<date>` for workspace ones,
   the web's convention — so a re-run replays; `--idempotency-key <k>` for a deliberate second
   execution. `--workspace` is required. The answer is rendered with the CLI's own sentences from
   the vocabulary module (an `enqueued` approve: "approved — posting shortly"; a replay: "already
   done", exit 0; a refusal: the reason and the fixing verb, exit 2). `--json` carries `outcome`
   and the API's data in `data`.
2. **`health`**: `/health` plus the two sub-checks as a status table (version, role, pool, taps,
   webhook liveness, scheduling, posting); `--json` passes the payloads through.
3. **`deploys [--watch]`** (F1): shell out to `railway`; before reading, check `railway whoami`
   and the linked project id against the repository's expected project (a wrong account or a
   dropped link exits 5 with the fix); then `railway deployment list --service <svc> --json` for
   `worker` and `storydump`, printing status, created time and commit; `--watch` exits 0 when
   both are `SUCCESS`, 6 when one is `FAILED`/`CRASHED`; a missing or logged-out `railway` exits
   5 with a plain sentence. Tests use a version-stamped fixture of the real JSON; `doctor`
   reports the detected `railway` version.
4. **`webhook status|register|deregister`**: `scripts/telegram_webhook.py`'s behaviour moves into
   `storydump_cli/commands/webhook.py` (same variables, same redaction); the script is deleted in
   this PR and every doc that named it points at the verb.
5. **`doctor`**: the token (present, valid via `GET /me/principal`, role, expiry), the API
   (reachable, version), the storage backend in use, the `railway` binary, login and linked
   project, the local configuration, and the migration ledger (`posture`) against the
   repository's migration files; each line `ok | wrong | missing` with a one-line fix.
6. **Help**: every verb has help text with one example; `storydump --help` groups verbs by
   reads, writes, environment and auth; `AGENTS.md` names `storydump --help` as authoritative.
7. **Delete the legacy CLI** — one commit with step 8, because `tests/test_agent_docs.py` imports
   `cli.main`: `git rm -r cli/` and its tests; remove `storydump-cli` from `setup.py`; remove the
   six Makefile targets (`check-health` becomes `storydump health`); drop the two `cli/commands/*`
   entries from `scripts/telegram_ratchet_baseline.json` (the ratchet passes with the baseline
   shrunk, never grown). A **retired-verbs table** in the CHANGELOG names every one of the 31
   commands with its disposition: replaced by a `storydump` verb, replaced by the web app, or
   retired with no replacement (the owner does not use the CLI; `rotate-keys`, `revoke-tokens`,
   `instagram-auth`, `promote-user`, `create-schedule`, `sync-media` are retired — their
   target-tier equivalents, where any exist, are the web's).
8. **Docs and the safety rules**: `CLAUDE.md` and `AGENTS.md` never-run blocks lose the four
   legacy verbs and name the genuinely destructive `storydump` verbs (`tokens revoke`, `webhook
   deregister`, `resolve … cancel`, `cancel`) with the STOP-and-confirm rule; `AGENTS.md`'s CLI
   section describes `storydump`; `tests/test_agent_docs.py` is re-pointed to `storydump <group>
   <verb>` and its registry walks nested Click groups so a subcommand is pinned, not its group;
   every file that names `storydump-cli` is updated (the three operations runbooks, the guides,
   `SECURITY_REVIEW.md`, `ROADMAP.md`, `07-security-model.md`), and a test asserts a repo-wide
   grep for `storydump-cli` returns nothing outside the CHANGELOG's history; CHANGELOG.

## Test Plan

Written red first; one named mutation per behaviour in `tests/mutations/cli_v2_03.sh`.

- **CLI write tests** against the in-process app: each verb hits the command route with the
  token and the deterministic key; a refused command prints the reason and exits 2; an
  `enqueued` answer prints the CLI's sentence; a `readonly` token exits 3; a service identity
  exits 3; re-running the same command replays ("already done", exit 0, nothing written);
  `--idempotency-key` executes again; `resolve … retry --not-posted` carries the verdict.
- **`deploys`**: a version-stamped fixture of `railway`'s JSON; `--watch` exits 0 on both
  `SUCCESS`, 6 on `FAILED`; a missing binary, a logged-out account and a wrong project each exit
  5 with their sentence.
- **`webhook`**: the script's tests moved to the verb; no secret in any output.
- **`doctor`**: each check's `ok | wrong | missing` against scripted answers.
- **Deletion**: `tests/test_agent_docs.py` green against the rewritten lists and a nested
  never-run verb; the grep-for-zero test; the FC-2 ratchet green with the shrunk baseline; the
  unit and gate suites green with the legacy tests deleted, not skipped; `python -c "import
  cli"` fails.

## Verification Checklist

- [x] `storydump skip <story>` with a person-bound `operator` token skips a real story; the
      intent's audit row reads `channel = 'cli'`; the `cli_command` row names the token and the
      same `external_ref`; running it again prints "already done" and exits 0 — the gate
      `tests/scripts/test_cli_writes_gate.py` and the live sample (`RUN_LOG.md` §5). The card's
      "⏭️ Skipped by <you>" on a bound Telegram group and the run against production are the
      owner's (queued, §7).
- [ ] `storydump deploys --watch` follows a real deploy to both services live — `deploys` read
      the real production deployments live (§5); the watch of a real deploy is run at PR 3's own
      merge (`--commit <sha>`) and recorded in §5, else queued (§7).
- [x] `storydump doctor` reports the missing token as `missing` with its fix, and the storage
      backend in use — run from a throwaway venv with no token (§5); the missing Railway login
      is `missing` by unit test (this machine is logged in).
- [x] `storydump-cli` is not on `PATH` after `pip install -e '.[cli]'` (the throwaway venv, §5);
      `cli/` is absent; `grep -r storydump-cli` is empty outside the history files
      (`tests/test_legacy_cli_gone.py`); `CLAUDE.md`, `AGENTS.md` and `tests/test_agent_docs.py`
      agree; CI green on the PR.

## What NOT To Do

- No write outside the command route; no direct database access for any verb.
- No fresh idempotency key by default; a re-run must replay.
- No Telegram adapter words in the terminal; the CLI has its own sentences.
- No deletion beyond `cli/`, its console script, its Makefile targets, its tests, its baseline
  entries and its mentions; `src/services/core` is #1216's.
- No secret printed by `webhook` or `doctor`, ever — the redaction test stays.

## Context

area: CLI, API client, docs and safety rules, legacy retirement · effort: M · risk: medium
(the never-run list is a safety rule; the deletion is broad but mechanical) · priority: P1

## Build notes (2026-09-15, PR 3)

- The verbs and their commands: `posted` → `mark_posted`, `resolve` → `resolve_review`,
  `sync <source_id>` → `sync_now` (the executor needs the source), `pause`/`resume` →
  `pause_workspace`/`resume_workspace`. Keys are the web's (`landing/src/lib/commands.ts`), not the plan's
  `<command>:<workspace>:<date>` — step 1 mis-cited the web's intent-command convention as a
  workspace one, and a day (or minute) bucket answered "already done" to a second pause after a
  resume while the workspace stayed resumed (both review lenses): a story verb is
  `<command>:<story>`; a resolution is `resolve_review:<story>:<resolution>[:<verdict>]:<episode>`
  with the episode the story's `entered_state_at` (one read of the story view first; no such
  story is the answer before anything is sent), so a later review of the same story is new and a
  refused retry then `--not-posted` are two keys — the verdict segment is the CLI's addition to
  the web's identity, and an empty episode is omitted as the web omits it; `pause`, `resume` and
  `sync` mint a fresh key per invocation (`fresh_key`, the web's submission id) because their
  effects are idempotent.
  The ingress's `admission_conflict` (the same key, a different command) has a sentence and a
  fix naming `--idempotency-key`. `--workspace` names ONE workspace for a write: a name two workspaces share
  is a usage error (the reads read both). The answer's `data` is `{workspace_id, command, args,
  idempotency_key, outcome, result}` — `args` is what was sent, so the human line names the
  story the port acted on.
- `health` judges `/health` by its `status` and the two dependency surfaces — which carry
  aggregates, no `status` — by the fleet monitors' OWN verdicts: `commands/env.py` imports
  `scripts/scheduling_monitor.py` and `scripts/posting_monitor.py` (stdlib-only, so the CLI
  stays a pure client) and calls their `classify` with their defaults; a surface is not well
  exactly when the monitor would page (stalled, worker down, silent, never-posted past its
  grace, unreachable — including a mistyped payload), and a quiet estate (`no-signal`, a first
  post inside its grace) is well; a `worker-unknown` estate (no destination, no system job yet)
  is a notice, not a page, and well. Two bounds against the pollers: the CLI has no watch clock
  (`watched_s=0`), so a first post is judged overdue by the estate's own ages alone and a poller
  that has watched longer pages sooner; and one unreachable reading is not well here where the
  pollers wait for a second consecutive one. The verdicts ride the report (`data.verdicts`). A surface
  answering 503 is reported as its error and is not well; exit 4 with the report still printed.
  (The round's first cut re-derived the signals with its own thresholds and diverged both ways
  from the monitors — the re-verify caught it; reusing the monitors is the one spelling.)
- `deploys` calls `railway whoami` → `railway status --json` (the name AND the id must be this
  repository's project, pinned in `railway.py`) → `railway deployment list --service <svc>
  --environment production --json` (the binary defaults to the LINKED environment; production is
  named) for `storydump` and `worker`, sorted newest first by the verb itself, five rows each;
  under `--watch` one row per service, a failure at the FIRST read is the answer
  (`Watched.failed_on_baseline`), SLEEPING and SKIPPED count as done, and `--commit <sha>` makes
  the watch wait for that commit's rows instead of ending on the previous SUCCESS (a watch
  started right after a push). A notice the binary prints before its JSON is skipped; a hung
  binary is exit 5. The fixture is the binary's real answers (`status` whole; the two newest
  deployments per service, whole objects).
- `webhook`: httpx instead of urllib, `follow_redirects=False`; the report is `{action, ok,
  checks: [{check, state, detail}]}` and the exit code its verdict — 4 for a failed check (the
  spec's "API unreachable or failed"), 64 for a missing or refused variable (the script's 1/2
  mapped). The spellings (the variables, `ALLOWED_UPDATES`, the cap, `bot_matches`) moved into
  the vocabulary module — the CLI's one `src` import (invariant I3) — and
  `telegram_webhook_registration.py` re-exports them; a bot token is a redacted shape now.
- `doctor`'s exit code is the first non-ok check's own code in report order (token 3 · api 4 ·
  storage 3 · config 64 · railway 5 · ledger 4); `skipped` never fails it; a missing binary or a
  missing login is `missing`, another project `wrong`; the API check reads `/health` alone; the
  railway value never carries the account line. The ledger comparison reads `scripts/migrations/`
  relative to the package, reports a checkout behind the deployment as well as ahead, and is
  skipped outside a checkout.
- Deviations from the steps: `approve` joins the never-run list (it posts — the STOP rule)
  beside the four the plan named; `make check-health` is re-pointed to `storydump health` rather
  than deleted (`make dev` calls it) — the other five targets are gone; `tests/test_legacy_cli_gone.py`
  exempts the archive, the dated updates, this plan and the owner's `.claude/settings*.json`
  (queued to the owner, not the repository's to edit); its `import cli` check pins the ORIGIN,
  because a sibling checkout installed editable can still answer. The SYSTEM_SCOPE census pin
  (`test_f1_fail_closed.py`) moved 49 → 40 with `cli/`'s nine sites. The seams module for
  `v1._open_tenant`/`_member` is not built (five call sites in `tokens.py`, two in `ops.py`, and
  both earlier batteries anchor on the names) — a recorded follow-up.
- Tests: `tests/storydump_cli/test_writes.py`, `test_env.py`, `test_webhook.py` (the script's
  tests ported to the verb) and `test_help.py`; `tests/test_agent_docs.py` rewritten (an
  invocation is code — a backtick span or a fence — and a group's subcommand is pinned);
  `tests/test_legacy_cli_gone.py`; the gate `tests/scripts/test_cli_writes_gate.py` drives the
  real CLI over an ASGI bridge against the app as `svc_ingress`; the battery
  `tests/mutations/cli_v2_03.sh`.
