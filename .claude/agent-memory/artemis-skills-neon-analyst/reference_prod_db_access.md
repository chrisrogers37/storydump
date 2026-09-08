---
name: reference-prod-db-access
description: Where the storydump production Postgres connection string lives, and why the locally authenticated Neon account cannot reach it
metadata:
  type: reference
---

Storydump production Postgres is **Neon, reached through Railway service
variables** — not through any file in the repo.

- `DATABASE_URL` = database-owner connection (holds DDL; used by the migration
  runner's `preDeployCommand`).
- `TARGET_DATABASE_URL` = runtime login (`svc_ingress` / `svc_worker`),
  deliberately without DDL rights. This is the one to use for read-only
  analysis.
- Both live in the Railway service environment. See
  `documentation/operations/runtime-database-roles.md` and
  `documentation/operations/migration-runner.md` for the role split.

**The locally authenticated Neon account is the wrong one.** `npx neonctl me`
authenticates as `chris@artemisanalytics.xyz`, whose single org
(`org-ancient-base-30492340`) holds exactly one project,
`artemis-quality-hub-prod` (`orange-cake-73990528`). That project's only
database is a Dagster/DeFiLlama analytics schema (`dg_*`, `defillama_*`,
`agent_runs`, `dimension_change_*`) — it contains **none** of storydump's
tables (`workspaces`, `media_sources`, `media_items`, `jobs`, `ig_accounts`,
`oauth_credentials`, `post_intents`, `channel_outbox`, `audit_events`).
Do not mistake it for production; verified by listing its tables on 2026-09-06.

To get access, one of: `railway link` then `railway variables`; a local `.env`
carrying `TARGET_DATABASE_URL`; or authenticate neonctl against the account
that actually owns the storydump project.

**Tooling gotchas on this machine (macOS):**
- `neonctl` is not installed as a binary — run it as `npx neonctl@latest`.
- `npx` fails `EPERM` on `~/.npm/_cacache`; set `npm_config_cache="$TMPDIR/..."`.
- neonctl writes a refresh lock to `~/.config/neon/`, outside the sandbox write
  allowlist, so neonctl calls need the sandbox disabled.
- `timeout` does not exist; use the Bash tool's own timeout instead.

**Why:** as of 2026-09-06 there was no `.env`/`.env.production`/`.env.local` in
the repo (only `.env.example` and `landing/.env.local.example` templates), the
working copy was not `railway link`ed, and no relevant env vars were set — so a
Neon investigation could not run at all.

**How to apply:** check these three sources before starting any production
query. State-sensitive: the user may add a `.env` or link Railway at any time,
so re-verify rather than assuming this is still blocked.
