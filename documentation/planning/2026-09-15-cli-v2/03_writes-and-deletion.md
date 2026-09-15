---
title: "CLI v2 — phase 03: the command verbs, health, deploys, webhook, doctor, and the legacy CLI's deletion (PR 3)"
type: plan
status: draft
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

- [ ] `storydump skip <story>` with a person-bound `operator` token skips a real story; the card
      reads "⏭️ Skipped by <you>"; the intent's audit row reads `channel = 'cli'`; the
      `cli_command` row names the token and the same `external_ref`; running it again prints
      "already done" and exits 0.
- [ ] `storydump deploys --watch` follows a real deploy to both services live.
- [ ] `storydump doctor` on a fresh clone reports the missing token and the missing Railway login
      as `missing` with their fixes, and the storage backend in use.
- [ ] `storydump-cli` is not on `PATH` after `pip install -e '.[cli]'`; `cli/` is absent;
      `grep -r storydump-cli` outside the CHANGELOG is empty; `CLAUDE.md`, `AGENTS.md` and
      `tests/test_agent_docs.py` agree; CI green.

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
