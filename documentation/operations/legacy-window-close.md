# Closing the legacy window — 3g and the stand-down (the owner's runbook)

> **This window ran on 2026-09-19 at 21:34 UTC** (the tear-out's ledger,
> [`RUN_LOG.md`](../planning/2026-09-16-legacy-tear-out/RUN_LOG.md)): 079 applied in 1.341 s,
> 080 in 1.019 s, the gate green on every line, the database 43.8 MB smaller, the schemas left
> `archive`, `public`, `runner`; the marker branch `pre-3g-20260919-2134` was retired on
> 2026-09-20. **Nothing below is to be run again** — `apply --manual` refuses a version the
> ledger already records. The page stays as the record of the sequence, of the rehearsal's shape
> (`backup-restore.md` reuses it) and of the backout; step 0's checkout-and-link check and step
> 8's "never `railway redeploy` after a `down`" are the parts still live for any production work.

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
- The rehearsal below has run green on a fresh PITR branch, end to end, with wall-clock recorded —
  it did, on 2026-09-19 (the tear-out's ledger, phase 04): 079 in 1.6 s, 080 in 0.5 s, every gate
  line as printed, the positive control landed, the database 82.8 MB → 38.9 MB.

## The rehearsal on a Neon PITR branch

A branch off production is a copy; nothing here touches production — with ONE way to get it
wrong, closed below: `neonctl connection-string` with an EMPTY branch name resolves to the
project's default branch, which is production. So the branch name is demanded (`${NAME:?}`)
wherever it is used, and the host guard fails closed. The guard's first arm is production's
endpoint id as the repository records it (`scripts/observed_use.py`, `EXPECTED_HOST`; read
2026-09-18) — if the Neon console shows another compute endpoint on the `production` branch,
fix the arm before running. The Neon CLI is signed in to the account
that owns the project (`npx --yes neonctl@latest me`); every call takes the organization and
project ids. Save the block as `rehearse.sh` and run it with `bash -eu rehearse.sh`, top to
bottom: under `-e` a failed substitution ends the script, and the connection string never
enters an interactive shell.

```bash
P=ancient-grass-50759240; O=org-ancient-bush-46337162; PROD=br-square-frog-ai37r0qg
NAME=claude/window-rehearsal-$(date -u +%Y%m%d-%H%M)

# 1. the branch, at production's head. neonctl PRINTS the new branch's connection string on create —
#    the same role password as production's — so the output goes through the redaction, always
npx --yes neonctl@latest branches create --project-id $P --org-id $O --parent $PROD --name "${NAME:?}" \
  2>&1 | sed -E 's#postgres(ql)?://[^ ]+#postgres://<redacted>#g'

# 2. the branch's OWNER connection string, into a variable — never echoed, never pasted.
#    an unset name fails the substitution (`${NAME:?}`) — under `bash -eu` that ends the script;
#    in a bare shell it leaves URL empty, which the guard below refuses.
URL=$(npx --yes neonctl@latest connection-string "${NAME:?set NAME first — an empty name is production}" \
      --project-id $P --org-id $O --role-name neondb_owner --database-name neondb)
# the host guard, FAIL-CLOSED: anything but a Neon branch endpoint unsets URL and ends the script
case "$(printf %s "$URL" | sed -E 's#^[^@]*@##; s#[:/?].*##')" in
  ep-hidden-shadow-aify76h5*|"") echo "REFUSED: production's endpoint, or none"; unset URL; exit 1 ;;
  ep-*.neon.tech) echo "branch endpoint ok" ;;
  *) echo "REFUSED: not a Neon endpoint"; unset URL; exit 1 ;;
esac

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
# the PG16+ shape only the rehearsal (17) and production (17) can show — CI's cluster is 15:
psql "$URL" -At -v ON_ERROR_STOP=1 -c "SELECT count(*) = 0 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid WHERE r.rolname LIKE 'svc\_%' AND NOT (m.member = current_user::regrole AND (r.rolname = 'svc_migration' OR (m.admin_option AND m.grantor = 10))) AND NOT (m.member = 'svc_migration'::regrole AND r.rolname IN ('svc_claim','svc_clock','svc_maintenance','svc_membership'))"   # t
psql "$URL" -At -v ON_ERROR_STOP=1 -c "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'"   # neondb_owner

# 7. F8's positive control: 076's hand-off, bracket and all, still lands as the owner
psql "$URL" -At -v ON_ERROR_STOP=1 \
  -c "GRANT CREATE ON SCHEMA public TO svc_clock; ALTER FUNCTION fn_reaper_stale_approved(interval, int) OWNER TO svc_clock; REVOKE CREATE ON SCHEMA public FROM svc_clock" \
  -c "GRANT CREATE ON SCHEMA public TO svc_maintenance; ALTER FUNCTION fn_reaper_stale_approved(interval, int) OWNER TO svc_maintenance; REVOKE CREATE ON SCHEMA public FROM svc_maintenance"

# 8. a plain apply owes nothing now
DATABASE_URL="$URL" python -m scripts.migration_runner apply   # 0 applied

# 9. retire the branch
npx --yes neonctl@latest branches delete "${NAME:?}" --project-id $P --org-id $O
```

Record the two wall-clocks and the gate's answers in the PR. Any red: retire the branch, fix
forward in the tree, rehearse again on a fresh branch. Never re-run in place.

## Production — in this order and no other

0. **The checkout and the link.** `railway run` executes the LOCAL checkout with the worker's
   environment: the 079 that runs is the text on disk, and the ledger records its checksum, so a
   checkout that is not the deployed commit fails every later deploy's integrity check on both
   services. And `railway redeploy` acts on the LINKED environment (it takes no
   `--environment`); another session's `railway login` silently drops the link. Check both
   before step 1 and the link again before step 8:
   ```bash
   git status --porcelain                  # empty
   git rev-parse --short HEAD              # the commit `storydump deploys` shows for both services
   shasum -a 256 scripts/migrations/079_drop_legacy_schema.sql scripts/migrations/080_window_stand_down.sql   # paste into the PR
   railway whoami && railway status        # the storydump project, environment production
   railway environment production          # re-link if it is not
   ```
1. **Stop the worker.** The backout restores to the marker; a running worker would keep writing
   past it.
   ```bash
   railway down --service worker --environment production --yes
   storydump deploys        # the worker's latest row reads REMOVED
   ```
   The CLI can time out against Railway's API and the `down` still take effect (2026-09-19): if
   it errors, read `storydump deploys` before sending it again.
2. **Announce** the window (the approval group, or the channel the team watches). The API keeps
   serving; taps and web approvals land in the ledger as usual and would be lost by a backout.
3. **Take the marker** — a branch off production at this moment, kept until the window is
   verified and the worker has run a day:
   ```bash
   P=ancient-grass-50759240; O=org-ancient-bush-46337162; PROD=br-square-frog-ai37r0qg
   MARKER=pre-3g-$(date -u +%Y%m%d-%H%M)
   npx --yes neonctl@latest branches create --project-id $P --org-id $O --parent $PROD --name $MARKER \
     2>&1 | sed -E 's#postgres(ql)?://[^ ]+#postgres://<redacted>#g'   # create prints a connection string
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
   edit 079, find the missing snapshot, the changed table or the relation nobody snapshotted.
   What the guard cannot see, and the drop takes: the legacy indexes (77), constraints, defaults
   and sequence values — a snapshot is rows only, by design (078).
   ```bash
   railway run --service worker --environment production -- python -m scripts.migration_runner apply --manual 79
   ```
6. **The stand-down.**
   ```bash
   railway run --service worker --environment production -- python -m scripts.migration_runner apply --manual 80
   ```
7. **The gate**, read-only, as the login 080's `current_user` lines are written for — the
   runner's (`DATABASE_URL`, the database owner), not the runtime one: today both are the owner,
   but the F.4 posture increment changes the runtime login, and then the `pg_has_role` and the
   ledger lines would answer for the wrong subject. The answers as 080 prints them:
   ```bash
   railway run --service worker --environment production -- sh -c 'psql "$DATABASE_URL" -At -F " | " -f /dev/stdin' <<'SQL'
   SELECT 'legacy_schemas', count(*) FROM pg_namespace WHERE nspname = 'legacy';                                   -- 0
   SELECT 'window_ddl_schemas', count(*) FROM pg_namespace WHERE nspname = 'window_ddl';                           -- 0
   SELECT 'svc_migration_create_revoked', NOT has_database_privilege('svc_migration', current_database(), 'CREATE'); -- t
   SELECT 'owner_set_on_maintenance', pg_has_role(current_user, 'svc_maintenance', 'SET');                        -- t
   SELECT 'svc_migration_memberships', array_agg(r.rolname ORDER BY r.rolname) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles g ON g.oid = m.member WHERE g.rolname = 'svc_migration';  -- {svc_claim,svc_clock,svc_maintenance,svc_membership}
   SELECT 'snapshots', count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'archive' AND c.relkind = 'r' AND c.relname LIKE '%\_pre\_cutover\_%';  -- 16
   SELECT 'roleid_side_shape', count(*) = 0 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid WHERE r.rolname LIKE 'svc\_%' AND NOT (m.member = current_user::regrole AND (r.rolname = 'svc_migration' OR (m.admin_option AND m.grantor = 10))) AND NOT (m.member = 'svc_migration'::regrole AND r.rolname IN ('svc_claim','svc_clock','svc_maintenance','svc_membership'));  -- t
   SELECT 'public_owner', nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public';                    -- neondb_owner
   SELECT 'ledger_head', max(version), count(*) FROM runner.schema_migrations;                                    -- 80 | 80
   SQL
   ```
   `storydump posture --json` (a signed-in CLI) shows 079 and 080 `applied`. Its `doors` and RLS
   lists read `public` only — the step-0 door lived in its own schema precisely to stay out of
   that census, and `legacy` never appeared in it — so they say nothing about the window; the
   `pg_namespace` lines above are the evidence.
8. **Restart the worker** — NOT with `railway redeploy`. In the window of 2026-09-19 it did not
   refuse and did not re-run the removed deployment: it re-ran an OLD one, a commit of 2026-09-03,
   and the worker came back on stale code (it booted the target root only because the retired
   tier switch was still set to the target on the service — the pin in `tests/test_agent_docs.py`
   keeps that variable's name off this page; the tear-out's ledger has it). The way back is a push to `main` — an empty commit — which
   deploys both services through the normal path and runs the predeploy, which now owes nothing.
   The alternative is the dashboard's Redeploy on the worker's last SUCCESS deployment *at the
   deployed commit*, chosen by hand. Then read the commit `storydump deploys` shows for the
   worker: it must be `main`'s head.
   ```bash
   git commit --allow-empty -m "redeploy: the worker after the window" && git push origin main
   storydump deploys                           # both services SUCCESS at main's head (the API after CI)
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
git commit --allow-empty -m "redeploy: the worker" && git push origin main   (never `railway redeploy` after a `down` — step 8)
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
