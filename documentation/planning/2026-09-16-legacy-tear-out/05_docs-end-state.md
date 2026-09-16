---
title: "Legacy tear-out — phase 05: the documentation's end state (PR 5)"
type: plan
status: draft
owner: chris
created: 2026-09-16
tags: [plan, legacy-retirement, docs]
links: [https://github.com/chrisrogers37/storydump/issues/1216, https://github.com/chrisrogers37/storydump/issues/1205]
---

## Summary

Bring every live page to the tier that exists: delete or rewrite the documents that describe
legacy tables, commands, variables or modules; close the consolidated plan's M.3 line and its
Live status; retire the legacy-tier rules under `.claude/rules/`; and extend the parity tests so a
legacy name cannot reappear in an agent-facing document. The audit of 2026-09-16 measured the
population; this PR finishes it.

## Evidence

- The audit (`../2026-09-16-cli-v2-audit/00_AUDIT.md`, "The class sweeps") lists the live
  documents still naming `posting_queue`, `chat_settings` or `api_tokens`:
  `.claude/rules/database.md` (4 + 2 + 1 mentions), `.claude/PROJECT_CONTEXT.md` (6 + 1),
  `.claude/QUICK_REFERENCE.md` (3 + 1), `.claude/commands/telegram-status.md`,
  `documentation/operations/worker-recovery.md`, `SECURITY_REVIEW.md`, `ROADMAP.md`,
  `migration-runner.md`, `documentation/guides/instagram-login-setup.md`, and the marked
  sections of `backup-restore.md`; `cloud-deployment.md` still carries one `railway shell -c`
  variable listing.
- `AGENTS.md`'s architecture and services sections describe both tiers; `README.md`'s tree lists
  `src/repositories`, `src/services` (mixed), `src/utils`.
- `documentation/planning/2026-08-02-consolidated-design-plan/README.md:7-18` Live status
  (position as of 2026-09-04) and `04-execution-sequence.md:97` (3f, 3g, step 8 owed).
- `tests/test_agent_docs.py` pins the never-run lists and their satellites; `tests/
  test_legacy_cli_gone.py` pins the legacy CLI's absence with a HISTORY exemption list.
- `.claude/rules/database.md`, `scheduler.md`, `telegram.md` are loaded by path pattern
  (`CLAUDE.md`'s table); the scheduler and Telegram rules describe the legacy loops and bot.

## Implementation Plan

### Dependencies
Phases 02 and 04 merged (the end state exists to describe).

### Blocks
The epic's goal condition (5).

### Steps

1. **Measure**: `grep -rln "posting_queue\|chat_settings\|api_tokens\|instagram_accounts\|media_posting_locks\|user_interactions\|src/services/core\|src/repositories\|WORKER_IMPL\|TELEGRAM_BOT_TOKEN\b" --include='*.md' . | grep -v 'archive\|CHANGELOG\|documentation/planning\|node_modules'` — the population, pasted; each file gets one row in the PR body: rewritten, deleted, or exempt (with why).
2. **`.claude/rules/`**: `database.md` describes the target schema only (the ledger tables, RLS,
   the runner); `scheduler.md` describes the target's clock, jobs and the publish pipeline, or is
   deleted if `documentation/operations/reading-the-ledger.md` already says it (link, do not
   duplicate); `telegram.md` describes the target adapter (the webhook, the outbox, the card);
   `CLAUDE.md`'s table matches the files that exist.
3. **`.claude/PROJECT_CONTEXT.md`, `QUICK_REFERENCE.md`, `.claude/commands/*.md`**: the module
   map and the "safe commands" name target modules and `storydump` verbs; a command file that ran
   legacy SQL is deleted or rewritten onto `storydump`.
4. **`AGENTS.md` / `README.md`**: one tier; the architecture section's legacy paragraph becomes
   one sentence of history with the retirement date and the archive snapshots' names; the tree
   is the tree.
5. **Runbooks and guides**: `worker-recovery.md` onto the target worker (`src.worker`, the lease
   heartbeat, the clock election, `storydump jobs|posture`); `backup-restore.md`'s marked
   sections replaced by the target's (the `archive` snapshots are the legacy backup; the target's
   backup is Neon PITR — `05` §DR); `migration-runner.md`'s legacy mentions; `SECURITY_REVIEW.md`
   and `ROADMAP.md` get a dated "retired" note where they describe the legacy tier;
   `instagram-login-setup.md` onto the target's Instagram Login; `cloud-deployment.md`'s last
   `railway shell -c`.
6. **The plans**: `04-execution-sequence.md:97` updated to "3f applied <date> (078), 3g and step
   8 run by the owner <date> (079/080), gate green"; the README's Live status position moved to
   the retirement; `00_EPIC.md` of this plan `status: completed` with the goal condition answered.
7. **Pins**: `tests/test_agent_docs.py` gains a table-name check — no agent-facing document
   (`CLAUDE.md`, `AGENTS.md`, `.claude/**/*.md`) names a legacy table or module path (the list
   from step 1, as code); `tests/test_legacy_cli_gone.py`'s exemption list reviewed (the archive
   and the plans stay exempt).

## Test Plan

- Red first: the table-name pin against the base — red on `.claude/rules/database.md` and the
  two context files.
- `tests/test_agent_docs.py`, `tests/test_legacy_cli_gone.py` green; the whole suite green.
- No code changes; CI's docs-only path.

## Verification Checklist

- [ ] The step-1 grep after the PR → only `documentation/archive/`, `CHANGELOG.md` and `documentation/planning/` (history).
- [ ] `CLAUDE.md`'s rules table names only files that exist; each rule file describes the target tier.
- [ ] The consolidated plan's Live status and `04:97` name the dates and the migration numbers.
- [ ] `00_EPIC.md` of this plan: `status: completed`, the goal condition answered with the owner's pasted gate output.

## What NOT To Do

- Do not delete history: `documentation/archive/`, `CHANGELOG.md`, the plan directories and the
  postmortems keep their legacy names — they describe what was.
- Do not rewrite a page to say what the code should do; describe what it does, with a
  `file:line`.
- Do not fold code changes into this PR.

## Context

Area: docs · Effort: M · Risk: low · Priority: last.
