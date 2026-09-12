---
name: feedback-prod-readonly-discipline
description: Rules for touching storydump production data — SELECT only, never print connection strings or token columns, filter railway variables to key names
metadata:
  type: feedback
---

When investigating storydump production: **SELECT statements only, inside
`BEGIN TRANSACTION READ ONLY`. Never print a connection string or any credential
value.** Specifically: do not `echo $TARGET_DATABASE_URL`; do not run
`railway variables` unfiltered (pipe through `--json | python3` and emit KEY
NAMES only); from `oauth_credentials` select only `state` / `expires_at` /
`updated_at`, never `encrypted_payload` or any token column.

**Why:** this system posts to Instagram and holds live Meta and Google OAuth
credentials, and `TARGET_DATABASE_URL` turns out to connect as `neondb_owner`
with BYPASSRLS (see [[reference-prod-db-access]]) — so a stray non-SELECT has
nothing standing in its way. CLAUDE.md's safety block already forbids mutating
production; this is the read-side discipline that pairs with it.

**How to apply:** applies to every production investigation, including ones the
user frames as urgent. Writing the SQL to a file and running it with `-f` makes
the statements reviewable before they execute — prefer that over long `-c`
strings. Use `-v ON_ERROR_STOP=0` so one bad column name does not abandon the
whole run. Customer-facing text (outbox `payload->>'text'`) is fine to surface;
it is the sentences users already received.
