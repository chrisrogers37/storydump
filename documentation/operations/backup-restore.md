# Backup & Restore Procedures

## Overview — what holds state, and what backs it up

| State | Where it lives | Its backup |
|---|---|---|
| The ledger — every target table (`src/models/target/`), the runner's migration ledger (`runner.schema_migrations`) and the `archive` schema | one Neon Postgres database | Neon point-in-time restore (below). Nothing in the tree copies the database off Neon |
| The legacy tier's data | sixteen `archive.<table>_pre_cutover_20260917` tables in that same database (migration 078) | the snapshots **are** the backup — rows only, with a lifetime (below) |
| Media | each workspace's own Google Drive folders; the ledger holds references (`media_items`), never the files | the tenant's Drive. Storydump reads it under the workspace's grant and keeps no copy |
| Transit copies | Cloudinary, between upload and publish | none wanted: destroyed after the publish commits (`src/services/target/publish_pipeline.py:2103`), and swept by `reap_transit_assets` |
| Configuration and secrets | Railway variables, per service; Vercel for `landing/` | an export the owner holds (below) |
| API tokens | `service_tokens`, as SHA-256 hashes (`src/services/target/service_tokens.py`) | nothing to back up — a lost token is minted again under Settings › API tokens |

The legacy tier was retired in the tear-out (#1216, September 2026); its data survives as the
`archive.*_pre_cutover_20260917` snapshots.

---

## The database — Neon point-in-time restore

The target tier's backup is the one the plan names (`05-operational-numbers.md`, §Backup / DR):
Neon keeps the project's write-ahead history, and a restore is either a **branch** taken at a point
in that history or the production branch **restored in place** to one.

### What is measured, and what is the plan's

| Concern | The plan's number (`05` §DR) | Measured |
|---|---|---|
| History retention (the PITR window) | ≥ 7 days, "verified at 0.2's gate" | **24 hours** — the project's `history_retention_seconds` is 86400 (read 2026-09-18; the tear-out's `RUN_LOG.md`, and `legacy-window-close.md`). Raising it in Neon or amending the plan is the owner's open decision |
| RPO | Neon's continuous WAL, "~minutes", no additional mechanism | not measured here |
| RTO target | 1 h — restore, repoint, smoke suite | not measured here |
| Restore drill | quarterly: PITR branch → runner parity → smoke suite | none of that shape is recorded in the tree (`04`: M.2, which was to be the first, was not run). What is recorded on branches of production: the 078 rehearsal at production's head (2026-09-18) and the 079/080 rehearsal on a fresh PITR branch (2026-09-19), both in the tear-out's `RUN_LOG.md`; the window itself ran behind a marker branch (`pre-3g-20260919-2134`), which is the plan's drill shape without the smoke suite |
| Tenant-level recovery | a PITR branch, then a per-workspace copy keyed on `workspace_id` | no runbook or tool for it exists in the tree |

So: a restore to an **arbitrary** moment reaches back 24 hours and no further. A **marker branch**
is the way past that bound — by Neon's documented branch model a child branch pins its parent's
branch point and is a durable copy (documented, not measured here; `legacy-window-close.md` says
the same and asks for it to be measured once).

### Before a risky change: take a marker

The commands are the ones `legacy-window-close.md` prints, against the same project
(`npx --yes neonctl@latest me` must show the account that owns it):

```bash
P=ancient-grass-50759240; O=org-ancient-bush-46337162; PROD=br-square-frog-ai37r0qg
MARKER=pre-change-$(date -u +%Y%m%d-%H%M)
npx --yes neonctl@latest branches create --project-id $P --org-id $O --parent $PROD --name $MARKER
```

Retire it once the change is verified: `npx --yes neonctl@latest branches delete "${MARKER:?}" --project-id $P --org-id $O`.
Never pass an empty branch name to `neonctl connection-string` — it resolves to the project's
default branch, which is production (`legacy-window-close.md`, the rehearsal's guard).

### Restoring production in place

Irreversible for every write after the restore point — the worker's **and** the API's. It is the
owner's act; an agent does not run it.

1. **Stop the worker**, so nothing keeps writing past the point:
   `railway down --service worker --environment production --yes`, then `storydump deploys` shows
   the worker's latest row `REMOVED`. (`railway whoami && railway status` first — `down` acts on
   the linked project and environment.)
2. **Announce it.** The API keeps serving; taps and web approvals landing now are lost.
3. **Restore to the marker**, keeping the present state under a name for forensics:
   ```bash
   npx --yes neonctl@latest branches restore $PROD $MARKER --project-id $P --org-id $O \
     --preserve-under-name before-restore-$(date -u +%Y%m%d-%H%M)
   ```
   Neon also restores a branch to a timestamp inside the retention, rather than to a marker; this
   page prints no spelling for that because nobody here has run one — read Neon's own
   documentation at the time, and rehearse it on a branch first.
4. **Read the ledger back** before anything runs against it:
   ```bash
   railway run --service worker --environment production -- python -m scripts.migration_runner status | tail -4
   storydump posture            # the migration ledger as the API reads it
   ```
   A database restored to before a migration owes that migration again; the next deploy's
   predeploy applies it (`migration-runner.md`).
5. **Pause before the worker returns.** A restore rewinds the ledger, not Instagram or Telegram. A
   story published after the restore point is back in the state it held at that point, and one
   that was already `approved` then would be published a second time when the worker returns;
   pause state is rewound too. With the worker still down, `storydump pause --workspace <ws>` for each
   workspace that posted in the gap (the command port runs in the API), then reconcile each story
   by hand: `storydump story <id>` shows the state it is back in; one still awaiting approval that
   did go out is recorded with `storydump posted`, one already `approved` is stopped with
   `storydump cancel` (never-run list: the user's decision). Only then `storydump resume`.
   `posted` and `resume` write too — `resume` restarts posting — so during a restore every one
   of these is the owner's decision, not an agent's.
6. **Restart the worker** — NOT with `railway redeploy` after a `down`: on 2026-09-19 it re-ran an
   OLD deployment (a commit of 2026-09-03) and the worker came back on stale code
   (`legacy-window-close.md`, step 8). Push an empty commit to `main`, which deploys both services
   through the normal path and runs the predeploy (which applies whatever the restore owes):
   `git commit --allow-empty -m "redeploy: the worker after the restore" && git push origin main`,
   then `storydump deploys --watch --timeout 900` (the API lands after CI) and `storydump health`;
   read the commit `storydump deploys` shows for the worker — it must be `main`'s head. The
   alternative is the dashboard's Redeploy on the worker's last SUCCESS deployment *at the deployed
   commit*, chosen by hand.

An in-place restore keeps the branch and its endpoint, so `DATABASE_URL` and `TARGET_DATABASE_URL`
stay as they are. Cutting over to a *different* branch instead means repointing both variables on
both services: the runner reads `DATABASE_URL` (the owner's connection), the worker and the API
read `TARGET_DATABASE_URL` (`railway.toml`; `src/services/target/vocabulary.py:377`).

### Reading a branch without touching production

A branch is a copy; reading one is how a restore point is checked before it is used. Use the
rehearsal block of `legacy-window-close.md` as the template — the demanded branch name and the
host guard that refuses production's endpoint are the parts not to drop — then
`python -m scripts.migration_runner status` and `parity --against <dsn>` answer whether the branch
is the schema the tree expects.

### An off-Neon copy (the owner's option)

The plan adds no mechanism beyond Neon, and nothing in the tree schedules a dump. An owner who
wants a copy that survives the loss of the Neon project takes a logical dump as the owner login —
it owns the tables and bypasses row-level security, which a complete dump needs:

```bash
railway run --service worker --environment production -- \
  sh -c 'pg_dump "$DATABASE_URL" -F c -f storydump_$(date -u +%Y%m%d).dump'
```

`railway run` executes on the laptop with the service's variables, so the file lands locally and
the connection string is never printed. `pg_dump` must be the server's major version or newer
(production is PostgreSQL 17 — the tear-out's `RUN_LOG.md`). The dump holds every tenant's rows and the encrypted provider
credentials: store it as a secret. Restoring such a dump into a fresh project has never been
rehearsed here — the `svc_*` roles, their grants and their memberships are made by the window
bootstrap and the migrations, not carried by one database's dump — so treat that path as unproven.

**Scheduling it.** Nothing in the tree runs that dump on a schedule, and with Neon's history at
24 hours (above) an owner who schedules nothing has no copy older than a day except a marker
branch. A nightly copy is the same command under cron on the owner's machine, with its own
retention. The variable names are the script's own, not the application's:

```bash
#!/bin/sh
# ~/scripts/storydump_dump.sh — a nightly logical dump, thirty days kept
# `railway run` resolves the linked project from the working directory, and cron starts in $HOME:
cd /path/to/the/storydump/checkout || exit 1
DUMP_DIR="$HOME/backups/storydump"; KEEP_DAYS=30
mkdir -p "$DUMP_DIR"
railway run --service worker --environment production -- \
  sh -c "pg_dump \"\$DATABASE_URL\" -F c -f '$DUMP_DIR/storydump_$(date -u +%Y%m%d).dump'"
find "$DUMP_DIR" -name 'storydump_*.dump' -mtime +$KEEP_DAYS -delete
```

```
0 3 * * * ~/scripts/storydump_dump.sh >> ~/logs/storydump_dump.log 2>&1
```

The page this one replaced documented that script for the legacy database; a dump takes the whole
database, so a host that still runs it is still making a complete copy. Whether one does is not
something the tree can say — check the host.

**Reading a dump, and taking one table out of it.** Into a SCRATCH database (a Neon branch, or a
local one), never into production:

```bash
pg_restore -l storydump_YYYYMMDD.dump                                   # what the dump holds
pg_restore -d "$SCRATCH_URL" -t post_intents storydump_YYYYMMDD.dump    # one table
```

Rows wanted back in production are then copied deliberately, as the owner, with the worker
stopped — there is no rehearsed procedure for that here, so write it down before doing it.

`make db-backup` and `make db-restore` dump and load the **local** development database named by
`DB_*`. They never touch production.

---

## The legacy tier's backup — the `archive` snapshots

Migration 078 (`scripts/migrations/078_legacy_snapshots_pre_cutover.sql`, applied in production by
the deploy of 2026-09-18) copied every table of the `legacy` schema into
`archive.<table>_pre_cutover_20260917`: sixteen tables, owned by `svc_maintenance`, readable by
that role's members only, with no grant to anything else. Its postconditions compared each copy's
row count to its source in the same transaction.

- **Rows only.** `CREATE TABLE … AS` copies rows — not indexes, constraints, defaults or sequence
  values. The `legacy` schema itself, with its 77 indexes, was dropped by 079 in the owner's window
  on 2026-09-19 (`legacy-window-close.md`); what that drop took and no snapshot holds is exactly
  that list.
- **For reading, not for running.** The code that read those tables was deleted in the tear-out's
  phase 01. A snapshot answers a question about the past; nothing can be restored *into service*
  from it.
- **Lifetime (fork F9).** The snapshots carry the `archive_snapshots` retention class: 90 days
  from the date in their names, so they are eligible from **2026-12-16** — the clock is the date in
  their names, not the day 079 ran (2026-09-19) — once the `retention_sweep` executor exists. The export-or-lapse decision is #1326; the executor is #1327. It is unbuilt (`work_loop.UNBUILT_KINDS`), so
  nothing drops them today; the door it will call reads the date from the table's name
  (`fn_retention_batch`, `059_security_definer_doors.sql:440-456`).
- **The owner's export, before then.** An owner who wants them longer exports first:
  ```bash
  railway run --service worker --environment production -- \
    sh -c 'pg_dump "$DATABASE_URL" -n archive -F c -f archive_pre_cutover_20260917.dump'
  ```
  `pg_restore --no-owner -d <a database> <the file>` loads it anywhere `svc_maintenance` does not
  exist.

Reading one, read-only, as the owner login (the window's gate uses the same door):

```bash
railway run --service worker --environment production -- sh -c 'psql "$DATABASE_URL" -At -F " | " -f /dev/stdin' <<'SQL'
SELECT count(*) FROM archive.posting_history_pre_cutover_20260917;
SQL
```

Never drop `archive` or a snapshot by hand (`legacy-window-close.md`, *What NOT to do*).

---

## Media

Media is the tenant's. A workspace connects Google Drive folders under Settings › Integrations;
the worker lists them when it syncs and reads a file's bytes, under that workspace's grant, only to
render a card or to publish (`src/worker.py:118-134`, `756-790`). The ledger stores references,
not bytes. There is nothing of the
tenant's media for the operator to back up or restore: what a tenant deletes in Drive is the
tenant's to recover from Drive.

---

## Configuration and secrets

```bash
# The owner's act: this prints EVERY secret of the service. Never in a shared terminal, never by an agent.
railway variables --service worker --environment production --json > railway_worker_$(date -u +%Y%m%d).json
railway variables --service storydump --environment production --json > railway_api_$(date -u +%Y%m%d).json
chmod 600 railway_*.json
```

`.env.example` names every variable something reads. One of them cannot be re-issued:

- **`ENCRYPTION_KEYS` / `ENCRYPTION_KEY`** — the Fernet ring that encrypts every provider
  credential in `oauth_credentials` (`src/utils/encryption.py`). Lose it and every stored grant is
  unreadable. The code fails closed: the readers refuse the credential by name ("could not be
  decrypted by any ring entry" — `src/services/target/ig_credentials.py:102-107`,
  `drive_credentials.py:136-145`), and the Instagram refresh path flips it `expired` and its
  account `reauth_required` and commits that before it raises
  (`src/services/target/ig_login_oauth.py:437-464`); the Drive reader deliberately flips nothing.
  Each workspace then reconnects: Instagram under Settings › Accounts, Google Drive under
  Settings › Integrations. A database restore is only as good as the ring that goes with it.

Everything else is re-issued at its source: the database connection strings (Neon), the bot token
(BotFather; then `telegram-webhook.md`), the webhook secret (any new value on the API, whose
startup registration re-registers the webhook with it in production), the Meta and Google app
secrets (their consoles), the Cloudinary trio.

---

## Disaster recovery — the whole deployment

1. **Database.** The Neon project survives: restore as above. The Neon project is lost: only an
   off-Neon dump the owner chose to keep brings the ledger back, by a path nobody has rehearsed.
2. **Services.** A Railway project with two services from this repository — `worker`
   (`python -m src.main`) and the API, `storydump` (`uvicorn src.api.app:app`), per the
   `Procfile` — with `railway.toml` as committed: its predeploy runs
   `python -m scripts.migration_runner apply`. A *new* Railway project has a new id, and
   `storydump deploys` and `doctor` refuse any project but the one the tree names
   (`RAILWAY_PROJECT_ID`, `src/services/target/vocabulary.py:370`): that constant moves with it,
   and `api.storydump.app` must point at the new API service.
3. **Variables** from the owner's export, on **both** services: `DATABASE_URL` is the owner's
   connection for the runner; `TARGET_DATABASE_URL` is the runtime login. Without the latter the
   worker exits 2 and the API answers 503 on every data route.
4. **Deploy**, then verify:
   ```bash
   storydump deploys --watch --timeout 900
   storydump health          # the three surfaces and the webhook
   storydump posture         # the ledger at the head the checkout expects
   ```
5. **The webhook.** The API registers it at startup in Railway's `production` environment;
   `storydump webhook status` confirms (`telegram-webhook.md`).
6. **Grants.** If the key ring did not survive, every workspace reconnects (above).

---

## Verification checklist

Quarterly, or after any change to the Neon plan:

- [ ] The project's history retention read from Neon, and this page's table corrected to it
- [ ] A branch of production read back through the host guard: `migration_runner status` owes
      nothing (079 and 080 are applied; a future `runner:manual` file would show as owed, never
      missing)
- [ ] The sixteen snapshots still present until their export or their sweep:
      `SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'archive' AND c.relkind = 'r' AND c.relname LIKE '%\_pre\_cutover\_%'` → 16
- [ ] The owner's variable export is current, and the key ring is in it
- [ ] The branch retired
