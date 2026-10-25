---
paths:
  - "scripts/migrations/**"
  - "scripts/migration_runner.py"
  - "scripts/schema_consistency.py"
  - "scripts/schema_consistency_base.txt"
  - "src/models/target/**"
---

# Schema changes

The schema is code: the models in `src/models/target/` and the ordered SQL files
in `src/models/target/ddl/` (`ddl/__init__.py:27`-`:35`), which `build()` creates
on an empty database in one transaction (`python -m src.models.target.ddl
build|init`). Numbered files in `scripts/migrations/` bring an existing database
to that code through `scripts/migration_runner.py`. Every deploy runs `apply` as
its predeploy (`railway.toml:8`), so **merged is applied**. The ledger is
`runner.schema_migrations`; how production came to this shape is
`documentation/operations/schema-cutover.md`.

- **A schema change is two edits in one PR**: the schema code, edited in place,
  and a new `scripts/migrations/<NNN>_<what>.sql` with the next number. The
  Schema consistency job (`python -m scripts.schema_consistency`) builds the
  current code on one cluster; on another it builds the code at the pinned base
  (`scripts/schema_consistency_base.txt`), adopts it and replays every migration
  absent there. The catalogs must match (`scripts/schema_parity.py`) and the
  runner must apply exactly that tail (`scripts/schema_consistency.py:197`), so
  code alone, or a migration alone, fails. The replay runs the tail in version
  order, each manual file by `apply --manual` when its turn comes (`:166`-`:187`),
  so **a new file runs after `001` and `002`**, as production applied them.
  `003_instagram_past_posts.sql` needs them and refuses to run until both are in
  the ledger, so a predeploy over a database that still owes them fails and
  changes nothing.
- **Its probe must be true on a fresh build.** A database built from code (the
  test worlds, `make init-db`) is entered by `adopt`, which records each file
  whose probe is true. Carry `-- runner:postcondition <SQL returning bool>`
  lines, or an `adoption_manifest.json` entry; a file with neither fails
  `_load_manifest` (`scripts/migration_runner.py:918`). A false probe leaves it
  pending, and `apply` then runs it over a schema that already has it.
- **A probe is permanent**: a false probe below a true one is an incoherent
  chain and fails adopt (`migration_runner.py:1047`). So the code keeps 001's
  postconditions true: no catalog comments on `public`'s functions and relations
  (describe in `--` lines, as `ddl/__init__.py` says) and a pinned `search_path`
  on every `SECURITY DEFINER` function.
- `000_initial.sql` is empty and never executed: a byte in it is refused at
  discovery (`migration_runner.py:309`), and `apply` refuses a database that has
  not adopted it.
- No `BEGIN`/`COMMIT`: the runner wraps the file and its ledger row in one
  transaction, bounds its lock waits and retries a lock-wait failure
  (`LOCK_TIMEOUT`, `LOCK_RETRY_DELAYS_S`, `migration_runner.py:117`-`:124`); the
  file sets no `lock_timeout`. A retry re-runs the whole file, so take the
  contended lock before heavy work.
- The run holds the runner's session lock (`RUNNER_LOCK_KEY`) through
  `_locked_connection`, the one door every writing verb opens. It refuses to
  run where libpq connected to a pooled host (`conn.info.host`, judged by
  `src/config/db_url.py`, the one module the runner imports from `src`, pinned
  by `tests/test_deploy_guardrails.py`), waits a bounded time
  (`RUNNER_LOCK_WAIT`) for another run's lock, naming the holder, then fails,
  and unlocks before it closes. A writing verb that connects any other way
  skips all three, so `TestAPooledEndpointIsRefused` fails when anything but
  `_locked_connection` takes the lock, anything but it and the read-only
  `status` calls `_connect`, or anything but `_connect` calls
  `psycopg2.connect`.
- The wait polls: one `pg_try_advisory_lock` in autocommit every
  `RUNNER_LOCK_POLL_S`, so the waiting run holds no transaction or snapshot.
  Do not turn it back into a blocking `pg_advisory_lock`: its statement holds
  a snapshot for the whole wait, the holder's `CREATE INDEX CONCURRENTLY`
  waits for every older snapshot, and the server ends one of the two as a
  deadlock (`test_a_waiting_run_lets_the_holder_build_an_index_concurrently`).
- **An applied file is immutable** — the ledger stores its SHA-256, and a changed
  or deleted file fails every later apply (`ledger_discrepancies`,
  `migration_runner.py:654`); a file at the base is immutable to the consistency
  job too (`schema_consistency.py:55`). Fix forward with a new number, and never
  remove `001` or `002`: production's ledger records them.
- Markers are a closed grammar (`KNOWN_MARKERS`, `migration_runner.py:139`):
  `postcondition`, `no-transaction`, `reapply-safe`, `manual`. A misspelt one is a
  hard failure at discovery, and a comment must not open with `runner` plus a
  marker word. A `-- runner:manual` file is owed by the deploy, never applied by
  it; `apply --manual <version>` is the owner's door and is in `CLAUDE.md`'s
  safety block. A database built from code adopts `001` and `002`; only one
  converted in place owes them.
- Tables come from `TargetBase.metadata`: use `columns.py`'s `TZ`, `NOW`,
  `GEN_UUID` and `pk()`. The CHECK lists the adapters share (`INTENT_STATES`,
  `PUBLISH_STEPS`, the audit kinds and channels, `TOKEN_ROLES`) are copied in
  `vocabulary.py` and pinned to the models
  (`tests/src/services/target/test_vocabulary.py:57`): change both together. A
  new table or function moves the counts at
  `tests/scripts/test_schema_builder.py:38` and `:47`.
- **Name a trigger `tg_<table>_<what>` and its function `trg_<table>_<what>`.**
  The prefix means trigger, not Telegram. Postgres fires a table's `BEFORE`
  triggers in name order, and on `post_intents` the edge guard
  `tg_intent_guard` (`ddl/06_triggers.sql:62`) sorts first; keep it first.
- Handing a function to a NOLOGIN door role is bracketed: `GRANT CREATE ON
  SCHEMA public TO svc_x; ALTER FUNCTION … OWNER TO svc_x; REVOKE CREATE ON
  SCHEMA public FROM svc_x` (`ddl/08_ownership.sql:4`-`:118`). A migration that
  adds a door brackets it the same way.
- The schema, its RLS posture and how services query it are
  `.claude/rules/database.md`. A new tenant table ships its `ENABLE ROW LEVEL
  SECURITY` and its policy (`ddl/07_row_security.sql`) in the same PR.
