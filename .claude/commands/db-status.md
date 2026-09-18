---
description: "Check the database's posture, the job queue and the API's health through storydump (read-only)"
---

Report the state of the ledger through the `storydump` CLI. Every command below
is a bounded read through the API under the user's token — there is no database
connection and no SQL here. Add `--json` to any of them for one envelope
`{"v": 1, "kind", "data", "error"}`; a workspace read's `data` is
`{"workspaces": [{"workspace_id", "rows"}]}`.

Exit codes: 0 ok · 1 not found · 2 refused · 3 not authorized · 4 API
unreachable or not well · 5 Railway unreachable · 64 usage.

## 0. Is there a token?

```bash
storydump whoami
```

It prints the principal and the workspaces the token can read. Exit 3 means no
usable token: stop and say so. Minting one is the user's (the web's Settings ›
API tokens, then `storydump login`, or `STORYDUMP_TOKEN` in the environment) —
do not ask for the secret in the conversation.

## 1. The API and its database

```bash
storydump health --json
```

Needs no token. From `data.api`: `target_database` (false means every data
route answers 503), `db_role` (the login the API holds, and whether it bypasses
RLS), `pool` (`size`, `checked_out`, `checked_out_peak`; read them from the
JSON — the table view does not print the last two). `data.verdicts` judges `api`,
`scheduling`, `posting` and `webhook` with the fleet monitors' own rules; exit 4
when one is not well — the report is still printed, so read it.

## 2. The database's posture

```bash
storydump posture --json
```

`data.ledger` is `present`, `absent` or `unreadable`; `data.migrations` is the
runner's ledger (`version`, `status`, `applied_at`); `data.role` the connected
login and `bypassrls`; `data.rls` every tenant table with `enabled`/`forced`;
`data.doors` the `SECURITY DEFINER` functions and their owners.

Compare the highest applied version with the tree:

```bash
ls scripts/migrations/ | tail -5
grep -l "^-- runner:manual" scripts/migrations/*.sql
```

A file the second command lists (079, 080) is GATED: the deploy owes it and does
not apply it; the owner applies it in a window
(`documentation/operations/legacy-window-close.md`). Its absence from the ledger
is expected, not drift. `storydump doctor` does not know this — it reports those
files as "not applied" with the fix "deploy main", which does not apply to them.
Any OTHER file in the tree that the ledger lacks is a deploy that has not
happened or a predeploy that failed: report it.

## 3. The job queue

```bash
storydump jobs --since 24h
```

One row per kind × lane × state, with the oldest runnable and failed samples
(a publish job's sample carries its story's `last_error`). What is still owed
(`ready`, `leased`) is listed at any age. Worth reporting: a `failed` or
`review_required` group, and a `ready` group whose `oldest_run_at` is long past
— nothing is claiming that lane. System jobs (no workspace) are not shown.

## 4. Stories in flight

```bash
storydump floating
```

Approved stories carrying a debit, waiting between attempts: the step, the
retry job and its next run, the last wait's class and rung. A row whose job is
`failed` is a story nobody is retrying.

If the user names an Instagram account, add `storydump account <handle>`: the
cap per day, the zone, the next slot, today's count and the last twenty
outcomes.

## What no verb answers

The media pool's totals and a credential's expiry have no read verb. If the user
needs them, that is the read-only `psql` escape hatch in
`documentation/operations/reading-the-ledger.md` — ask before opening
production, SELECT only, and do not select `oauth_credentials.encrypted_payload`.

## Report Format

```
## Ledger Status

### API
| Surface | Verdict | Facts |
|---------|---------|-------|

### Posture
role … · bypassrls … · ledger … · applied through NNN · gated and owed: …

### Jobs (24h)
| Kind | Lane | State | Count | Oldest run_at |
|------|------|-------|-------|---------------|

### Floating
| Story | Step | Job | Next run | Last wait |
|-------|------|-----|----------|-----------|
```

**REMINDER**: every command here reads. Do not follow one with a write verb —
`CLAUDE.md`'s safety block governs those, and the decision is the user's.
