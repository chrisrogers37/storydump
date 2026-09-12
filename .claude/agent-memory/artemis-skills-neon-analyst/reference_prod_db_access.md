---
name: reference-prod-db-access
description: How to reach the storydump production Postgres read-only via Railway (working as of 2026-09-11), the two invocation gotchas that silently break it, and why the local Neon account is the wrong one
metadata:
  type: reference
---

Storydump production Postgres is **Neon, reached through Railway service
variables** — not through any file in the repo. `.env` is empty; nothing is in
the shell environment.

## Working recipe (verified 2026-09-11 22:04 UTC)

```
railway run --service storydump -- sh -c 'psql "$TARGET_DATABASE_URL" -v ON_ERROR_STOP=0 -f "$0"' /abs/path/to.sql
```

Two gotchas, both of which fail in ways that look like something else:

1. **Never `cd` out of `/Users/chris/Projects/storydump`.** Railway resolves the
   project from the cwd (`~/.railway/config.json`). A `cd` to the scratchpad
   gives `No linked project found. Run railway link` even though linkage is
   fine. Keep cwd at the repo root and pass the `.sql` by absolute path.
2. **Don't write `railway run -- psql "$TARGET_DATABASE_URL"` directly.** The
   local shell expands the variable to empty *before* Railway injects it, so
   psql falls back to a unix socket and reports
   `connection to server on socket "/tmp/.s.PGSQL.5432" failed`. Wrap in
   `sh -c '...'` with single quotes so the child expands it.

Also: run these with the sandbox disabled — the sandbox blocks network egress.

## Environment shape

- Railway project `storydump` (`33d1ccca-353c-4236-8d39-0d8fd916f054`), env
  `production`, **two** services: `storydump` (API) and `worker`. Both carry
  `TARGET_DATABASE_URL` and `DATABASE_URL`.
- Railway CLI authenticates as **christophertrogers37@gmail.com** — a different
  address from the git/user email. Auth was stale earlier on 2026-09-11 and was
  re-established by the user; it can go stale again, so re-check with
  `railway status --json` before assuming access.
- **Correction to prior belief:** `TARGET_DATABASE_URL` does *not* connect as a
  restricted `svc_ingress`/`svc_worker` role. It connects as `neondb_owner` with
  `rolbypassrls = t`. So RLS does **not** hide rows and no
  `SET LOCAL app.tenant_id` is needed. Confirm with
  `SELECT current_user, (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user)`.
  See `documentation/operations/runtime-database-roles.md` for the intended
  split, which the live variable does not currently reflect.

## The local Neon account is the wrong one

`npx neonctl me` authenticates as `chris@artemisanalytics.xyz`, whose single org
holds only `artemis-quality-hub-prod` — a Dagster/DeFiLlama analytics schema
containing **none** of storydump's tables. Do not mistake it for production.
neonctl is not installed as a binary (use `npx neonctl@latest`), `npx` needs
`npm_config_cache` redirected, and neonctl writes to `~/.config/neon/` so it too
needs the sandbox off.

**Why:** on 2026-09-06 and again earlier on 2026-09-11, a production
investigation could not run at all for want of a credential, and the two
invocation gotchas above each cost a failed round-trip once access existed.

**How to apply:** before any production query, confirm Railway auth, then use
the exact invocation above. State-sensitive — re-verify identity and
`rolbypassrls` rather than trusting this note, since the role behind
`TARGET_DATABASE_URL` is the kind of thing an ops change would alter silently.
Related: [[feedback-prod-readonly-discipline]].
