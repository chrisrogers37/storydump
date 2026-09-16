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
  `documentation/operations/worker-recovery.md`, `documentation/SECURITY_REVIEW.md`,
  `documentation/ROADMAP.md`, `documentation/operations/migration-runner.md`,
  `documentation/guides/instagram-login-setup.md`, and the marked sections of
  `documentation/operations/backup-restore.md`; `documentation/guides/cloud-deployment.md` still
  carries one `railway shell -c` variable listing. (The audit document lands with PR #1314, which
  merges before this plan starts.)
- `AGENTS.md`'s architecture and services sections describe both tiers; `README.md`'s tree lists
  `src/repositories`, `src/services` (mixed), `src/utils`.
- `documentation/planning/2026-08-02-consolidated-design-plan/README.md:7-18` Live status
  (position as of 2026-09-04) and `04-execution-sequence.md:97` (3f, 3g, step 8 owed).
- `tests/test_agent_docs.py` pins the never-run lists and their satellites; `tests/
  test_legacy_cli_gone.py` pins the legacy CLI's absence with a HISTORY exemption list.
- `.claude/rules/database.md`, `scheduler.md`, `telegram.md` are loaded by path pattern
  (`CLAUDE.md`'s table); the scheduler and Telegram rules describe the legacy loops and bot.
- The safety block's production line names `posting_history` (`CLAUDE.md:53`, `AGENTS.md:44`:
  "mutating SQL on `posting_history`") — a legacy table; `tests/test_agent_docs.py` compares only
  the fenced never-run block, so this prose line is per-file and both must change together.
- `documentation/operations/meta-app-review.md:32-47` is the standing "do not drop `legacy`"
  constraint; after phase 04 it describes a schema that no longer exists and a ruling (#1202's
  "or" leg) it should cite.
- `documentation/operations/backup-restore.md` must state the snapshots' lifetime (F9), because
  "the archive snapshots are the legacy backup" is only true for as long as the retention class
  keeps them.

## Implementation Plan

### Dependencies
Phases 02 and 04 merged (the end state exists to describe).

### Blocks
The epic's goal condition (5).

### Steps

1. **Measure**: `grep -rln "posting_queue\|posting_history\|chat_settings\|api_tokens\|instagram_accounts\|media_posting_locks\|user_interactions\|user_chat_memberships\|onboarding_sessions\|service_runs\|category_post_case_mix\|media_items\|audit_log\|schema_version\|src/services/core\|src/services/integrations\|src/services/media_sources\|src/repositories\|src/config/database\|WORKER_IMPL\|TELEGRAM_BOT_TOKEN\b\|ADMIN_TELEGRAM_CHAT_ID" --include='*.md' . | grep -v 'archive\|CHANGELOG\|documentation/planning\|node_modules'` — the population, pasted; each file gets one row in the PR body: rewritten, deleted, or exempt (with why). `audit_log`, `media_items` and `users` are also TARGET names — a hit on those is read, not counted.
2. **`.claude/rules/`**: `database.md` describes the target schema only (the ledger tables, RLS,
   the runner); `scheduler.md` describes the target's clock, jobs and the publish pipeline, or is
   deleted if `documentation/operations/reading-the-ledger.md` already says it (link, do not
   duplicate); `telegram.md` describes the target adapter (the webhook, the outbox, the card);
   `CLAUDE.md`'s table matches the files that exist.
3. **`.claude/PROJECT_CONTEXT.md`, `QUICK_REFERENCE.md`, `.claude/commands/*.md`**: the module
   map and the "safe commands" name target modules and `storydump` verbs; a command file that ran
   legacy SQL is deleted or rewritten onto `storydump`.
4. **`AGENTS.md` / `README.md` / `CLAUDE.md`**: one tier; the architecture section's legacy
   paragraph becomes one sentence of history with the retirement date and the archive snapshots'
   names; the tree is the tree; the safety block's production line is rewritten onto the target
   ledger in BOTH `CLAUDE.md` and `AGENTS.md` ("mutating SQL against the ledger —
   `post_intents`, `jobs`, `outbox` — or any `archive` snapshot"), the never-run fence untouched.
5. **Runbooks and guides**: `worker-recovery.md` onto the target worker (`src.worker`, the lease
   heartbeat, the clock election, `storydump jobs|posture`); `backup-restore.md`'s marked
   sections replaced by the target's (the `archive` snapshots are the legacy backup; the target's
   backup is Neon PITR — `05` §DR); `migration-runner.md`'s legacy mentions; `SECURITY_REVIEW.md`
   and `ROADMAP.md` get a dated "retired" note where they describe the legacy tier;
   `instagram-login-setup.md` onto the target's Instagram Login; `cloud-deployment.md`'s last
   `railway shell -c`; `meta-app-review.md:32-47` replaced by two sentences — the constraint was
   discharged by #1202's ruling on its "or" leg on <date>, and `legacy` was dropped on <date>
   (079) — with #410's own tracker untouched; `backup-restore.md` states the snapshots' lifetime
   per F9 and the date they become eligible.
6. **The plans**: `04-execution-sequence.md:97` updated to "3f applied <date> (078), 3g and step
   8 run by the owner <date> (079/080), gate green"; the README's Live status position moved to
   the retirement; `2026-08-17-m2-rehearsal-spec/README.md:3-7`'s status line (the rehearsal ran
   for 3g and step 8 on <date>); `00_EPIC.md` of this plan `status: completed` with the
   verification checklist ticked.
7. **Pins**: `tests/test_agent_docs.py` gains a legacy-name check — no live document under
   `CLAUDE.md`, `AGENTS.md`, `README.md`, `.claude/**/*.md`, `documentation/operations/**/*.md`
   and `documentation/guides/**/*.md` names a legacy-only table (`LEGACY_TABLES` minus the
   target-reused names, imported from `tests/scripts/legacy_inventory.py`), a deleted module
   path, or a retired variable (the step-1 pattern as code, one home); `documentation/archive/`,
   `documentation/planning/` and `CHANGELOG.md` are the stated exemptions; a positive control
   plants a legacy name in a `tmp_path` page and expects the hit; `tests/test_legacy_cli_gone.py`'s
   exemption list reviewed (the archive and the plans stay exempt).

## Test Plan

- Red first: the legacy-name pin against the base — red on `.claude/rules/database.md`, the two
  context files, the safety block's `posting_history` line and the runbooks of step 5.
- `tests/test_agent_docs.py`, `tests/test_legacy_cli_gone.py` green; the whole suite green.
- No code changes; CI's docs-only path.

## Verification Checklist

- [ ] The step-1 grep after the PR → only `documentation/archive/`, `CHANGELOG.md` and `documentation/planning/` (history).
- [ ] `CLAUDE.md`'s rules table names only files that exist; each rule file describes the target tier.
- [ ] `grep -n posting_history CLAUDE.md AGENTS.md` → nothing; `tests/test_agent_docs.py` green with the pin's roots covering the two documentation directories.
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
