# CLAUDE.md

Guidance for Claude Code in this repository.

**Read [`AGENTS.md`](AGENTS.md) first.** It is the canonical, vendor-neutral
guide — project overview, architecture, the command port, setup, commands,
testing, services, and what is deliberately not wired. This file carries what
is specific to Claude Code, plus two kinds of rule repeated from `AGENTS.md`:
the safety rules and the rules every PR follows. They are repeated here rather
than referenced because an agent must know them before acting, and that must
not depend on a reader following a link.

Everything else shared stays in `AGENTS.md`, behind the pointer above, and is
**not** duplicated here, because two documents kept in agreement by hand
eventually disagree. That includes reference needed only while using a tool,
such as the CLI's usage or one verb's own rules. The never-run lists are
guarded: `tests/test_agent_docs.py` fails if they drift apart, or if either
document names a command the CLI does not have. Nothing pins the rest of either
kind of rule, so a PR that adds or changes one in `AGENTS.md` makes the same
change here, and review checks both files at the same head.

---

## CRITICAL SAFETY RULES

**THIS SYSTEM POSTS TO INSTAGRAM. DO NOT TRIGGER POSTING WITHOUT EXPLICIT USER APPROVAL.**

### NEVER run these

```bash
python -m src.main                   # Starts the posting worker: the clock, the job lanes and the sweepers (it posts to Instagram)
python -m src.worker                 # Starts the posting worker: the clock, the job lanes and the sweepers (it posts to Instagram)
python -m scripts.migration_runner apply --manual <version>   # Applies a gated (runner:manual) file by hand: 002 drops the archive snapshots; only a database converted in place owes 001 and 002
storydump approve <story>            # Posts a story to Instagram — the user's decision, never an agent's
storydump cancel <story>             # Cancels a story: refunds its debit, destroys its upload
storydump resolve <story> cancel     # Gives up on a story parked for review; its debit is retained
storydump resolve <story> retry      # Posts the story again; --not-posted overrides the lost-answer guard
storydump tokens revoke <id>         # Revokes an API token; it stops working at once
storydump webhook register           # Re-points the production bot's webhook; --drop-pending discards queued taps
storydump webhook deregister         # Detaches the bot's webhook — Telegram delivers nothing until it is registered again
storydump admit <email>              # Admits a person and emails them a sign-up link — the owner's decision, never an agent's
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
`src/models/target/` declares — or the `archive` schema.

This list names what is unambiguously destructive or emails a person; absence
from it does not mean a command is read-only. The other write verbs (`skip`, `reject`, `posted`,
`pause`, `resume`, `sync`, `schedule`, `reschedule`, `link`) change the ledger
through the command port too, and each is a posting-related action under the
STOP rule above. See the same section in `AGENTS.md` for the commands that
write despite reading as inspection.

### The record

This repository is private, but what lands in it is not throwaway: every file,
commit, pull request, issue, review comment, screenshot and GIF is read by
every agent and collaborator who works here, and a pushed commit outlives the
branch that carried it. None of them names a tenant. Write placeholders —
`<workspace>`, `@<handle>`, `<ws-id>`, "the owner's account" — never a
workspace's name, an account's handle, a workspace, account, user or story id
from production, a person's email (the site's published contact address is
the exception), or what an account posted. Never write the owner's other
businesses or their domains, a Telegram chat or user id, or any token or key.
Describe production evidence by its shape and its numbers, and take
screenshots from the demo or a local stack.

### Main is production

Railway deploys `main` to production on every push, both the API and the
worker, and Vercel builds `storydump.app` from it. So:

- Work on your own branch and open a pull request; never push to `main`, and
  never merge to it without the owner's yes.
- Other agents work in this repository at the same time. Never force-push or
  rewrite a branch you did not create.
- Before pushing, `git fetch --prune --prune-tags --force origin`. Never
  `git push --tags` or `git push --mirror`.

---

## Domain rules

`.claude/rules/` loads automatically when working in matching files:

| File | Covers |
|---|---|
| `changelog.md` | Changelog entries: a PR that touches code or config adds one fragment in `changelog.d/`, and no PR edits `CHANGELOG.md`; the entry style |
| `database.md` | The target schema: the ledger tables, RLS and the tenancy gate, SQL under the unit of work |
| `development-patterns.md` | Repo-wide code conventions: layers, service modules, logging, security |
| `migrations.md` | Changing the schema: the code, a new migration file, the runner and its markers, the Schema consistency job |
| `scheduler.md` | The worker: the clock, the jobs, the publish pipeline |
| `telegram.md` | The Telegram adapter: the webhook, the outbox, the approval card |
| `testing.md` | Test requirements, layout and markers |

## The other Claude Code files

- `.claude/commands/db-status.md` and `telegram-status.md` — the `/db-status`
  and `/telegram-status` slash commands, read-only `storydump` reads.
- `.claude/PROJECT_CONTEXT.md` and `.claude/QUICK_REFERENCE.md` — satellite
  context pages; each carries its own copy of the never-run list, which
  `tests/test_agent_docs.py` holds equal to the block above.
- There is no `.claude/settings.json`: no permission list stands in for the
  block above, which is the safety rule.

## Working here

Everything else — setup, commands, testing, services, pre-commit and CI,
documentation placement — is in [`AGENTS.md`](AGENTS.md). If you find yourself
about to add operational guidance to this file, it belongs there instead.
