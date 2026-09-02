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
storydump-cli reset-queue            # Mutates the posting queue
storydump-cli instagram-auth         # Mutates stored authentication
storydump-cli revoke-tokens          # Destroys stored OAuth tokens for a service
storydump-cli rotate-keys            # Re-encrypts every stored token row
```

### Before ANY posting-related action

1. **STOP** and ask the user for explicit confirmation.
2. Explain exactly what will happen.
3. Wait for the user to approve.

### Telegram Web (web.telegram.org)

- **NEVER type or click** — view and screenshot only.
- All bot interactions go through the database or the user's own device.

### Production

Never run against production: the posting scheduler, or mutating SQL on
`posting_history`.

This list names what is unambiguously destructive; absence from it does not mean
a command is read-only. See the same section in `AGENTS.md` for the commands
that write despite reading as inspection.

---

## Domain rules

`.claude/rules/` loads automatically when working in matching files:

| File | Covers |
|---|---|
| `changelog.md` | CHANGELOG conventions |
| `database.md` | Schema, migrations, query patterns |
| `development-patterns.md` | Repo-wide code conventions |
| `scheduler.md` | Posting scheduler behaviour |
| `telegram.md` | Telegram bot surface |
| `testing.md` | Test layout and fixtures |

## Working here

Everything else — setup, commands, testing, services, pre-commit and CI,
documentation placement — is in [`AGENTS.md`](AGENTS.md) and is deliberately
not repeated here. If you find yourself about to add operational guidance to
this file, it belongs there instead: this file is Claude Code specifics plus
the safety block, and nothing else.
