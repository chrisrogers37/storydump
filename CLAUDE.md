# CLAUDE.md

Guidance for Claude Code in this repository.

**Read [`AGENTS.md`](AGENTS.md) first.** It is the canonical, vendor-neutral
guide — project overview, architecture, the command port, setup, commands,
testing, services, and what is deliberately not wired. This file carries only
what is specific to Claude Code, plus the safety rules, which are repeated here
rather than referenced because they are the one thing that must not depend on a
reader following a link.

Everything shared lives in `AGENTS.md` and is **not** duplicated here. Two
documents that must be manually kept in agreement are two documents that will
eventually disagree, so the overlap is **exactly one section** — the safety
rules below, repeated because they must not depend on a reader following a
link. That single overlap is guarded: `tests/test_agent_docs.py` fails if the
two never-run lists drift apart, or if either document names a command the CLI
does not have. Nothing else here is shared, and nothing else needs guarding.

---

## CRITICAL SAFETY RULES

**THIS SYSTEM POSTS TO INSTAGRAM. DO NOT TRIGGER POSTING WITHOUT EXPLICIT USER APPROVAL.**

### NEVER run these

```bash
python -m src.main                   # Starts the posting scheduler + Telegram bot
python -m scripts.migration_runner apply --manual <version>   # Applies a gated file: 079 DROPS the legacy schema — the owner runs the window (F7)
storydump approve <story>            # Posts a story to Instagram — the user's decision, never an agent's
storydump cancel <story>             # Cancels a story: refunds its debit, destroys its upload
storydump resolve <story> cancel     # Gives up on a story parked for review; its debit is retained
storydump resolve <story> retry      # Posts the story again; --not-posted overrides the lost-answer guard
storydump tokens revoke <id>         # Revokes an API token; it stops working at once
storydump webhook register           # Re-points the production bot's webhook; --drop-pending discards queued taps
storydump webhook deregister         # Detaches the bot's webhook — Telegram delivers nothing until it is registered again
```

### Before ANY posting-related action

1. **STOP** and ask the user for explicit confirmation.
2. Explain exactly what will happen.
3. Wait for the user to approve.

### Telegram Web (web.telegram.org)

- **NEVER type or click** — view and screenshot only.
- All bot interactions go through the database or the user's own device.

### Production

Never run against production: the posting scheduler, or mutating SQL against
the ledger — `post_intents`, `jobs`, `channel_outbox` and every other table
`src/models/target/` declares — or any `archive` snapshot.

This list names what is unambiguously destructive; absence from it does not mean
a command is read-only. See the same section in `AGENTS.md` for the commands
that write despite reading as inspection.

---

## Domain rules

`.claude/rules/` loads automatically when working in matching files:

| File | Covers |
|---|---|
| `changelog.md` | CHANGELOG conventions |
| `database.md` | The target schema, RLS and tenancy, how queries are written, the migration runner |
| `development-patterns.md` | Repo-wide code conventions: layers, service modules, logging, security |
| `scheduler.md` | The worker: the clock, the jobs, the publish pipeline |
| `telegram.md` | The Telegram adapter: the webhook, the outbox, the approval card |
| `testing.md` | Test requirements, layout and markers |

## Working here

Everything else — setup, commands, testing, services, pre-commit and CI,
documentation placement — is in [`AGENTS.md`](AGENTS.md) and is deliberately
not repeated here. If you find yourself about to add operational guidance to
this file, it belongs there instead: this file is Claude Code specifics plus
the safety block, and nothing else.
