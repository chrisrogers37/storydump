# Runtime database roles — moving the API and worker off the owner login (F.4, #751)

## Why this matters

Production connects to Neon as `neondb_owner`. That role owns every table and
holds `BYPASSRLS`, so every row-level-security policy the target schema
installs (`058`, 58 policies) is inert on the deployed path: the database
enforces no tenant boundary at all today, the application code is the only
thing keeping one workspace's rows away from another's. Measured 2026-08-25
on #751.

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
  worker's boot line reads `svc_worker` / `False`.
- #751 is closed with those two observations quoted, and the plan README's
  scoreboard moves F.4 to built.

## What this does not do, stated so nobody reads it as more

The objects in `public` are still owned by `neondb_owner`, not `svc_migration`,
and the plan's end state of "no service role a member of anything" was never
established because the cutover ran by hand (`03` §Post-ratification rulings).
Those are hygiene residue. This runbook restores the property that carries the
security weight: the processes that serve tenants cannot bypass the tenant
policies.
