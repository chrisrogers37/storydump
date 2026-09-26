# Runtime database roles — moving the API and worker off the owner login (F.4, #751)

## Why this matters

Both services now connect to Neon as their own logins: the worker as
`svc_worker` since 2026-09-21 15:51 UTC, the API as `svc_ingress` since
2026-09-21 19:45 UTC (the steps below, run by the owner once 081 and 082 were
live). `neondb_owner` — the role that owns every table and holds `BYPASSRLS`
— is the migration runner's login alone, so the row-level-security policies
the target schema installs (`058` and `060`, 58 policies between them) are
live on every deployed path. Measured 2026-08-25 on #751 as the owner login on
both services; the worker's half closed 2026-09-21 15:51 UTC, the API's 19:45.

The plan's runtime posture (`02` §7, `04` F.4) is: the API connects as
`svc_ingress`, the worker as `svc_worker`, and only the migration runner uses
the owner. The roles exist in production (created by the step-0 bootstrap,
granted by `057`, exercised by the F.4 harness as those exact logins). What is
missing is operational: passwords, and the two services' connection strings.

`/health` reports which login the API actually holds — `"db_role": {"user":
..., "bypassrls": ...}` — and the worker logs the same at boot. That is how
each step below is verified rather than assumed.

## Preconditions

- The deploy that added `db_role` to `/health` is live:
  `curl -s https://api.storydump.app/health` shows the field (today it reads
  `neondb_owner` / `bypassrls: true`).
- Migration 081 is applied (`storydump doctor` reads the ledger head at 81 or
  above): the fleet health surfaces and the Meta deauthorize callback read the
  estate through its doors. Before it, `/health/scheduling` and
  `/health/posting` read the tenant tables directly and answered `no-signal` /
  `never-posted` under `svc_ingress` — the monitors went blind on the first
  switch (2026-09-20) and the API was rolled back the same hour — and the
  deauthorize callback's account lookup, which names no workspace, would have
  found nothing and kept a credential Meta had already invalidated. Those are
  the three tenant-less reads the API makes; everything else it reads names
  its tenant or goes through a door built before 081.
- Migration 082 is applied (ledger head 82 or above) before the WORKER's
  switch: the worker's tenant-less sweeps — the outbox sender sweep, the
  prompt sweep, the settled-card sweep, the stranded-source alert, the
  reconciler's container poll and its ladder count — read through its doors
  (or claim the row's workspace first) and write per workspace under that
  workspace's tenant, handing the caller's scope back. Under `svc_worker`
  without it the sender sweep mints no delivery job and the prompt sweep
  finds no due story: nothing is delivered and nothing asks for approval,
  silently.
- Both roles exist and can log in. In the Neon SQL editor, as the project
  owner:

  ```sql
  SELECT rolname, rolcanlogin, rolbypassrls
    FROM pg_roles WHERE rolname IN ('svc_ingress', 'svc_worker');
  ```

  Expect two rows, `rolbypassrls = false`. If `rolcanlogin` is false for
  either, step 1 fixes it.

## Steps — one service at a time, verify each

1. **Set passwords** (Neon SQL editor, as the owner). Generate two long random
   passwords yourself; they go into Neon and Railway and nowhere else.

   ```sql
   ALTER ROLE svc_ingress WITH LOGIN PASSWORD '<generated-1>';
   ALTER ROLE svc_worker  WITH LOGIN PASSWORD '<generated-2>';
   ```

2. **Build the two connection strings** from the API service's current
   `TARGET_DATABASE_URL`: same host, database and `sslmode=require`; only the
   user and password change. Do not switch between the pooled and direct Neon
   hosts while doing this — keep whichever shape the current URL has.

3. **Switch the API.** Railway → the API service → Variables →
   `TARGET_DATABASE_URL` = the `svc_ingress` string. Redeploy. Then:
   - `curl -s https://api.storydump.app/health` →
     `"db_role": {"user": "svc_ingress", "bypassrls": false}`.
   - `storydump health`: the `scheduling` and `posting` lines report the SAME
     verdicts and counts as before the switch (`accounts_active`,
     `posted_ever`, `intents_ever`). A `no-signal` or `never-posted` that was
     `healthy` / `posting` a minute earlier is the blind read — roll back.
   - Sign in at storydump.app and open Queue, Media Library and Settings.
   - **Rollback** if any page shows "Router unavailable" or a request 500s:
     put the previous value back and redeploy. Note which page failed —
     that is a grant the schema is missing for `svc_ingress`, and it becomes a
     migration, not a reason to stay on the owner login.

4. **Switch the worker.** Railway → the worker service → Variables →
   `TARGET_DATABASE_URL` = the `svc_worker` string. Redeploy. Then:
   - Railway logs show `worker database role: {'user': 'svc_worker',
     'bypassrls': False}` at boot.
   - `curl -s https://api.storydump.app/health/scheduling` still reports the
     clock advancing after its next tick (cadence is six hours; the
     `storydump-scheduling-monitor` alerts if it stops).
   - **Rollback** is the same: previous value back, redeploy. A job failing
     with `permission denied` in the logs names the missing grant.

5. **Leave `DATABASE_URL` alone on both services.** It is the owner
   connection the migration runner uses on every deploy (#1217); the runtime
   login must never hold DDL rights.

## Done when

- `/health` on production reads `svc_ingress` / `bypassrls: false` and the
  worker's boot line reads `svc_worker` / `False`. The worker's half: observed
  2026-09-21 15:52 UTC (deployment `c33ec782`). The API's: observed 2026-09-21
  19:47 UTC (deployment `0a554321`, `db_role user=svc_ingress bypassrls=no`).
- `/health/scheduling` and `/health/posting` report the estate — the same
  counts as under the owner login — and the fleet monitors' verdicts are
  unchanged across the switch. Observed at both switches: scheduling
  `healthy` (2 active accounts, 0 overdue), posting `posting` (119 posted,
  248 intents at the API's), identical before and after.
- After the worker's switch, the worker's log shows, within one sweep
  cycle, prompts and sender jobs minted at the rate the previous deployment's
  log showed (compare its last sweep lines with the new deployment's first),
  and a card reaches a bound group. Observed 2026-09-21: the verdicts
  unchanged, `reconcile_ambiguous` jobs succeeding, no permission denied; the
  estate was idle (no pending outbox row, no due story), so the first card as
  `svc_worker` is to be seen on the next slot.
- #751 is closed with those two observations quoted, and the plan README's
  scoreboard moves F.4 to built.

## What this does not do, stated so nobody reads it as more

The objects in `public` are still owned by `neondb_owner`, not `svc_migration`,
and the plan's end state of "no service role a member of anything" was never
established because the cutover ran by hand (`03` §Post-ratification rulings).
Those are hygiene residue. This runbook restores the property that carries the
security weight: the processes that serve tenants cannot bypass the tenant
policies.
