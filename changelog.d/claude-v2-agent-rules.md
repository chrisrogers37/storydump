### Documentation

- **Coding agents get the repository's safety rules again.** `AGENTS.md`, `CLAUDE.md` and the `.claude/` rules, context pages and read-only slash commands came back, rewritten for this repository: the never-run list (approve, cancel, resolve, revoke, the webhook verbs, admit, the worker and the manual migration door), the rule that `main` deploys straight to production, and the per-area guides for the schema code, migrations, the worker, Telegram and tests.

### Tests

- **The never-run lists are pinned again.** `tests/test_agent_docs.py` fails when the two documents' never-run lists disagree, when a context page's copy misses an entry, when either names a CLI command that does not exist, or when a new `python -m` entry point is left unclassified.
