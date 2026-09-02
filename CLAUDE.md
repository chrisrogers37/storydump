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
eventually disagree; the overlap is deliberately as small as the safety
requirement allows, and `tests/test_agent_docs.py` fails if the two safety lists
drift apart or if either names a command the CLI does not have.

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

- Run `ruff check .` and `ruff format --check .` before pushing; CI gates both.
- **Always update `CHANGELOG.md`** on a PR — CI fails without it.
- The test suite needs a real PostgreSQL; set `REQUIRE_TEST_DATABASE=1` so a
  database that fails to come up produces failures instead of silent skips.
- New documentation goes in `documentation/` subdirectories, never scattered
  through source.
