# Closing the legacy window — 3g and the stand-down (the owner's runbook)

The last two M.3 steps ship as runner files the deploy cannot run: `079_drop_legacy_schema.sql`
drops `legacy` behind an in-file precondition, `080_window_stand_down.sql` closes the window (fork
F8 (a): the `window_ddl` door dropped, `CREATE ON DATABASE` revoked from `svc_migration`, every
membership a door file needs KEPT). Both carry `-- runner:manual`: every deploy's predeploy lists
them as `owed (manual)` and applies nothing of theirs; only `apply --manual <version>` applies
one, by name. The owner runs the window (fork F7); this is the sequence, every command as printed.

**Irreversible.** 079 destroys the legacy schema. Its backout is a Neon point-in-time restore to a
marker taken just before, which discards every write after the marker — the worker's and the
API's. The window is seconds long; the marker exists for a drop that succeeded and then broke
the target, not for a refusal (a refusal leaves the database exactly as it was: each file is one
transaction).

## Before the window

- 078 applied in production (the tear-out's phase 03, 2026-09-18): sixteen
  `archive.<t>_pre_cutover_20260917` tables owned by `svc_maintenance`.
- Phase 04 merged; the next predeploy log of either service shows
  `owed (manual) 079 (079_drop_legacy_schema.sql)` and `owed (manual) 080 (080_window_stand_down.sql)`.
- #1202 closed on its "or" leg (the owner's ruling of 2026-09-16: the target tier is armed and
  serving with a connected destination). 079 is not that guard; it guards against an accidental
  deploy-time drop.
- The Neon project's history retention is what the backout rests on. Measured on 2026-09-18:
  **24 hours** (`history_retention_seconds = 86400`), not the ≥ 7 days `05` §DR states. The
  marker BRANCH below is the backout, not the retention: by Neon's documented branch model a
  child branch pins its parent's branch-point and is a durable copy (documented, not measured
  here — measure it once by restoring from a rehearsal branch older than a day); the retention
  matters only for a restore to an arbitrary timestamp.
- The rehearsal below has run green on a fresh PITR branch, end to end, with wall-clock recorded.

## The rehearsal on a Neon PITR branch

A branch off production is a copy; nothing here touches production. The Neon CLI is signed in
to the account that owns the project (`npx --yes neonctl@latest me`); every call takes the
organization and project ids.

```bash
P=ancient-grass-50759240; O=org-ancient-bush-46337162; PROD=br-square-frog-ai37r0qg
NAME=claude/window-rehearsal-$(date -u +%Y%m%d-%H%M)

# 1. the branch, at production's head
npx --yes neonctl@latest branches create --project-id $P --org-id $O --parent $PROD --name $NAME

# 2. the branch's OWNER connection string, into a variable — never echoed, never pasted
URL=$(npx --yes neonctl@latest connection-string $NAME --project-id $P --org-id $O \
      --role-name neondb_owner --database-name neondb)
# refuse — stop here — unless the host is a Neon endpoint that is NOT production's
printf %s "$URL" | sed -E 's#^[^@]*@##; s#[:/?].*##' | grep -E '^ep-' | grep -v ep-hidden-shadow-aify76h5 \
  || { echo "REFUSED: not a Neon branch endpoint, or production's"; unset URL; exit 1; }

# 3. the branch's ledger: head 078, the pair owed
DATABASE_URL="$URL" python -m scripts.migration_runner status | tail -4

# 4. 3g, timed
time DATABASE_URL="$URL" python -m scripts.migration_runner apply --manual 79

# 5. the stand-down, timed
time DATABASE_URL="$URL" python -m scripts.migration_runner apply --manual 80

# 6. the gate: the load-bearing lines of the eleven 080 prints, run as printed (the gate test
#    runs all eleven in CI); answers compared
psql "$URL" -At -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'legacy'"   # 0
psql "$URL" -At -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_namespace WHERE nspname = 'window_ddl'"   # 0
psql "$URL" -At -v ON_ERROR_STOP=1 -c "SELECT NOT has_database_privilege('svc_migration', current_database(), 'CREATE')"   # t
psql "$URL" -At -v ON_ERROR_STOP=1 -c "SELECT pg_has_role(current_user, 'svc_maintenance', 'SET')"   # t — the chain door files need
psql "$URL" -At -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'archive' AND c.relkind = 'r' AND c.relname LIKE '%\_pre\_cutover\_%'"   # 16

# 7. F8's positive control: 076's hand-off, bracket and all, still lands as the owner
psql "$URL" -At -v ON_ERROR_STOP=1 \
  -c "GRANT CREATE ON SCHEMA public TO svc_clock; ALTER FUNCTION fn_reaper_stale_approved(interval, int) OWNER TO svc_clock; REVOKE CREATE ON SCHEMA public FROM svc_clock" \
  -c "GRANT CREATE ON SCHEMA public TO svc_maintenance; ALTER FUNCTION fn_reaper_stale_approved(interval, int) OWNER TO svc_maintenance; REVOKE CREATE ON SCHEMA public FROM svc_maintenance"

# 8. a plain apply owes nothing now
DATABASE_URL="$URL" python -m scripts.migration_runner apply   # 0 applied

# 9. retire the branch
npx --yes neonctl@latest branches delete $NAME --project-id $P --org-id $O
```

Record the two wall-clocks and the gate's answers in the PR. Any red: retire the branch, fix
forward in the tree, rehearse again on a fresh branch. Never re-run in place.

## Production — in this order and no other

0. **The Railway link.** `railway redeploy` acts on the LINKED environment (it takes no
   `--environment`), and another session's `railway login` silently drops the link. Check it
   before step 1 and again before step 8:
   ```bash
   railway whoami && railway status        # the storydump project, environment production
   railway environment production          # re-link if it is not
   ```
1. **Stop the worker.** The backout restores to the marker; a running worker would keep writing
   past it.
   ```bash
   railway down --service worker --environment production --yes
   storydump deploys        # the worker's latest row reads REMOVED
   ```
2. **Announce** the window (the approval group, or the channel the team watches). The API keeps
   serving; taps and web approvals land in the ledger as usual and would be lost by a backout.
3. **Take the marker** — a branch off production at this moment, kept until the window is
   verified and the worker has run a day:
   ```bash
   P=ancient-grass-50759240; O=org-ancient-bush-46337162; PROD=br-square-frog-ai37r0qg
   MARKER=pre-3g-$(date -u +%Y%m%d-%H%M)
   npx --yes neonctl@latest branches create --project-id $P --org-id $O --parent $PROD --name $MARKER
   ```
4. **Before:** head 078, the pair owed — and the two facts 080 changes, recorded so the window
   shows before → after (phase 03's probes never read the database privilege).
   ```bash
   railway run --service worker --environment production -- python -m scripts.migration_runner status | tail -4
   railway run --service worker --environment production -- sh -c 'psql "$TARGET_DATABASE_URL" -At -F " | " -f /dev/stdin' <<'SQL'
   SELECT 'window_ddl_schemas', count(*) FROM pg_namespace WHERE nspname = 'window_ddl';
   SELECT 'svc_migration_create_on_db', has_database_privilege('svc_migration', current_database(), 'CREATE');
   SQL
   ```
5. **3g.** The runner connects as `DATABASE_URL`'s login on the worker service — the database
   owner, the actor 078 measured. A refusal here is the guard working: read the message, do not
   edit 079, find the missing or changed snapshot.
   ```bash
   railway run --service worker --environment production -- python -m scripts.migration_runner apply --manual 79
   ```
6. **The stand-down.**
   ```bash
   railway run --service worker --environment production -- python -m scripts.migration_runner apply --manual 80
   ```
7. **The gate**, read-only through the runtime login (the same read path every probe of this
   plan used), the answers as 080 prints them:
   ```bash
   railway run --service worker --environment production -- sh -c 'psql "$TARGET_DATABASE_URL" -At -F " | " -f /dev/stdin' <<'SQL'
   SELECT 'legacy_schemas', count(*) FROM pg_namespace WHERE nspname = 'legacy';                                   -- 0
   SELECT 'window_ddl_schemas', count(*) FROM pg_namespace WHERE nspname = 'window_ddl';                           -- 0
   SELECT 'svc_migration_create_revoked', NOT has_database_privilege('svc_migration', current_database(), 'CREATE'); -- t
   SELECT 'owner_set_on_maintenance', pg_has_role(current_user, 'svc_maintenance', 'SET');                        -- t
   SELECT 'svc_migration_memberships', array_agg(r.rolname ORDER BY r.rolname) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles g ON g.oid = m.member WHERE g.rolname = 'svc_migration';  -- {svc_claim,svc_clock,svc_maintenance,svc_membership}
   SELECT 'snapshots', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'archive' AND c.relkind = 'r' AND c.relname LIKE '%\_pre\_cutover\_%';  -- 16
   SELECT 'ledger_head', max(version), count(*) FROM runner.schema_migrations;                                    -- 80 | 80
   SQL
   ```
   `storydump posture --json` (a signed-in CLI) shows 079 and 080 `applied`. Its `doors` and RLS
   lists read `public` only — the step-0 door lived in its own schema precisely to stay out of
   that census, and `legacy` never appeared in it — so they say nothing about the window; the
   `pg_namespace` lines above are the evidence.
8. **Restart the worker** — the link checked again (step 0). `redeploy` re-runs the latest
   deployment; if Railway refuses because the latest is the REMOVED one, the fallback is the
   dashboard's Redeploy on the worker's last SUCCESS deployment, or an empty commit to `main`
   (`git commit --allow-empty -m "redeploy" && git push`), which deploys both services and runs
   the predeploy — which now owes nothing.
   ```bash
   railway redeploy --service worker --yes
   storydump deploys --watch --timeout 900     # both services SUCCESS
   storydump health                            # every surface ok; the worker's last success age falls
   ```
9. **Close the issues** with this PR and the gate's output: #1216 (the epic), #941 (the
   fifteenth table's disposition), #739 (the Facebook Login credential path — its tables are
   gone), and #1046/#1113 where they concern `legacy`-schema instruments.

## The backout

Only for a drop that succeeded and broke the target — a refusal needs none. With the worker
still stopped (step 1), restore production to the marker; every write after the marker is
discarded, the API's included, so announce before restoring.

```bash
P=ancient-grass-50759240; O=org-ancient-bush-46337162; PROD=br-square-frog-ai37r0qg
npx --yes neonctl@latest branches restore $PROD $MARKER --project-id $P --org-id $O \
  --preserve-under-name failed-3g-$(date -u +%Y%m%d-%H%M)
railway run --service worker --environment production -- python -m scripts.migration_runner status | tail -4   # head 078, the pair owed again
railway environment production && railway redeploy --service worker --yes
```

The preserved branch holds the failed state for forensics. Retire the marker branch a day after
a verified window; retire the preserved one after the forensics.

## What NOT to do

- 079 and 080 are the owner's to apply (fork F7); an agent session does not apply them, and a
  plain `apply` is no substitute — it owes them by design. `CLAUDE.md`'s never-run list names
  the door.
- Never edit 079 to make a refusal pass: the runner checksums the file, and a refusal names a
  snapshot that is missing or no longer matches its source.
- Never open the window with the worker running, and never restore to the marker with it
  running.
- Never drop `archive` or a snapshot; never touch `public`.
- Never revoke a membership 080 keeps (fork F8 (a)): the owner's `OWNER TO svc_*` in the next
  door file runs through them.
