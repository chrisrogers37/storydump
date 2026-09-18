---
name: reference-prod-db-access
description: How to reach storydump's Postgres read-only — the Railway recipe and its two invocation gotchas, plus the neonctl path (org/project ids, sandbox and npx cache requirements) that reaches the same Neon database
metadata:
  type: reference
---

Storydump production Postgres is **Neon, reached through Railway service
variables** — not through any file in the repo. `.env` is empty; nothing is in
the shell environment.

## Working recipe

The command has ONE home — `documentation/operations/reading-the-ledger.md` › *The
escape hatch* — so it cannot drift between pages: `railway run` against the `worker`
service in the `production` environment, `sh -c 'psql "$TARGET_DATABASE_URL" …'` reading a
file of SELECTs on stdin (give the file by absolute path: `< /abs/path/to.sql`), the output
through a redaction. That form ran every read-only probe of the legacy tear-out on
2026-09-17 and 2026-09-18. (This page's first recipe, verified 2026-09-11, named
`--service storydump` with `-f "$0"`; both services carry `TARGET_DATABASE_URL`, so it
reached the same database — the canonical spelling replaced it here so that one is
written down.)

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

## The local Neon account — corrected 2026-09-18

**Superseded:** this section previously said `npx neonctl me` authenticates as
`chris@artemisanalytics.xyz`, whose org holds only `artemis-quality-hub-prod`
and **none** of storydump's tables. That is no longer true — do not act on it.

Verified 2026-09-18: `npx neonctl@latest me` authenticates as
**christophertrogers37@gmail.com** (login `christophertrogers37`) — the *same*
account Railway uses, not the artemisanalytics address. Its org
**`org-ancient-bush-46337162`** ("Christopher") contains project
**`ancient-grass-50759240` / `storyline-ai-db`**, which *is* storydump's
database. So neonctl is a genuine second path to this data — and, unlike
Railway, the one that can create branches for rehearsals. What it holds is in
"What is in the database" below.

Production branch is `br-square-frog-ai37r0qg`. **Its endpoint host is the one
never to connect to during a branch rehearsal** — always derive the host from
the branch's own connection string and assert it differs before running psql.

Mechanics that still hold: neonctl is not installed as a binary (use
`npx --yes neonctl@latest`), `npx` needs `npm_config_cache` redirected to a
writable dir or it dies `EPERM` on `/Users/chris/.npm/_cacache`, and neonctl
writes to `~/.config/neon/`. Both neonctl **and** psql need the sandbox off:
`allowed_domains` does not help psql, because the sandbox's egress proxy is
HTTP-only and raw Postgres TCP fails at DNS
("could not translate host name … to address"). `neonctl projects list` with no
`--org-id` blocks on an interactive org picker — always pass `--org-id`.

## What is in the database

Table names brought to the target schema on 2026-09-18 from the migrations in
the tree (`scripts/migrations/052_*` onward) — not re-queried; confirm with
`\dt public.*` before relying on one.

- **`public` is the target ledger** — the only schema the code reads:
  `workspaces` (the tenant), `ig_accounts`, `media_sources`, `media_items`,
  `post_intents` (one story, for one account, at one slot), `audit_events`,
  `jobs`, `channel_bindings`, `channel_outbox`, `provider_operations`,
  `daily_post_counts`, and the rest of the twenty-six in
  `.claude/rules/database.md`. Eighteen carry a `workspace_id` column
  (`workspaces` is the tenant itself). Because the login bypasses RLS, a query
  spans every tenant: select or group by `workspace_id` when the question is
  about one.
- **`archive.<table>_pre_cutover_20260917`** — sixteen snapshots of the retired
  legacy tier's tables, rows only (migration 078), e.g.
  `archive.posting_history_pre_cutover_20260917`. This is where pre-cutover
  history lives. They carry the 90-day `archive_snapshots` retention class
  (eligible from 2026-12-16; nothing drops them until the `retention_sweep`
  executor exists).
- **`legacy`** — the legacy tier's schema, dropped by the gated migration 079 in
  the owner's window (`documentation/operations/legacy-window-close.md`).
  Whether it still exists is a fact to check
  (`SELECT 1 FROM pg_namespace WHERE nspname = 'legacy'`), not to assume. No
  code reads it; for history, query the snapshots.
- **`runner.schema_migrations`** — the migration ledger (`version`, `status`,
  `applied_at`).

Before `psql` at all: `storydump story|cards|floating|account|jobs|outbox|burst|posture`
answer most ledger questions through the API with no database connection
(`documentation/operations/reading-the-ledger.md`). A probe, when one is needed:

```sql
BEGIN TRANSACTION READ ONLY;
SELECT workspace_id, state, count(*) FROM post_intents GROUP BY 1, 2 ORDER BY 1, 2;
SELECT workspace_id, handle, state, next_slot_at, last_posted_at FROM ig_accounts ORDER BY created_at;
SELECT kind, lane, state, count(*) FROM jobs GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
SELECT count(*) FROM archive.posting_history_pre_cutover_20260917;
COMMIT;
```

**Why:** on 2026-09-06 and again earlier on 2026-09-11, a production
investigation could not run at all for want of a credential, and the two
invocation gotchas above each cost a failed round-trip once access existed.

**How to apply:** before any production query, confirm Railway auth, then use
the exact invocation above. State-sensitive — re-verify identity and
`rolbypassrls` rather than trusting this note, since the role behind
`TARGET_DATABASE_URL` is the kind of thing an ops change would alter silently.
Related: [[feedback-prod-readonly-discipline]].
