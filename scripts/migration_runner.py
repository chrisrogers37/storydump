"""Numbered-SQL migration runner — ledger, apply, adopt, repair.

Standalone by design: a Railway predeploy step runs this before the app code
boots, so it must not depend on the application's import closure or its
configuration. It depends on the standard library, psycopg2, and, from
``src``, one standard-library module alone: ``src.config.db_url``, the
pooled-host predicate the worker shares (``tests/test_deploy_guardrails.py``
pins what importing the runner loads). The database is addressed by
``DATABASE_URL`` or ``--database-url``, through its direct host: every writing
verb holds a session-scoped advisory lock, and refuses a connection libpq made
to a pooled host (``_locked_connection``).

The ledger lives in the dedicated `runner` schema, separate from application
objects in `public`. Schema operations therefore preserve the ledger and the
history that the current runner invocation records.

Conventions a migration file may carry (this list is the contract; the ops
runbook cites it):

- ``-- runner:no-transaction`` — the file runs statement-by-statement in
  autocommit instead of one transaction (required for CREATE INDEX
  CONCURRENTLY, which refuses to run inside any transaction block, including
  the implicit block a multi-statement simple query creates). Such files must
  be idempotent: a mid-file crash leaves them partially applied and unrecorded,
  so the retry re-executes them from the top.
- ``-- runner:postcondition <SQL returning bool>`` — executed after the file
  applies; anything but a single true is a failed migration. A file carrying
  postconditions also needs no adoption-manifest entry: its adoption probe
  derives from them (one predicate, one home).
- ``-- runner:reapply-safe`` — for idempotent data migrations whose applied
  state is undecidable in place: adopt may leave them pending
  below an adopted head without calling the chain incoherent, and apply
  re-runs them there.
``000_initial.sql`` is empty and is never executed. A database built from the
schema code (``src.models.target.ddl``) enters the ledger at it through
``adopt``, whose probe for it is in the adoption manifest; every later file
applies on top of it.

The three doors that WRITE from the corpus — ``discover_migrations``,
``apply_pending`` and ``adopt`` — take an optional ``max_version``, a bound for
a caller that replays the tree only up to a version. ``status`` deliberately
does not: an operator asking what is pending wants the whole answer.
Production never passes a bound.

A file's execution mode is a declared discovery-time fact — ``wrapped`` (the
runner owns one transaction, ledger row inside it), ``self-managed`` (the file
carries its own BEGIN/COMMIT, the legacy corpus shape, run with psql
semantics), or ``no-transaction`` — visible in ``status`` output. New files
use ``wrapped`` unless their SQL requires another mode.

A wrapped file's lock waits are bounded, and a failure on one is tried again
(``LOCK_TIMEOUT``, ``LOCK_RETRY_DELAYS_S``). Self-managed and
no-transaction files run without the bound: their statements and their
transactions are their own. The run's own wait for another run's lock is
bounded too (``RUNNER_LOCK_WAIT``), and polls with no transaction open, so it
never holds a snapshot the holder's ``CREATE INDEX CONCURRENTLY`` would wait for.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import psycopg2

from scripts.sql_lexer import CODE, COMMENTS, segments
from src.config.db_url import is_pooled_host, pooled_endpoint_refusal


# One fixed key for every runner invocation against a given database: the
# whole run holds it (`_locked_connection`), so concurrent deploys serialize
# and the loser finds the versions applied and no-ops. The cutover runbook's
# rollback SQL takes the same key by its number.
RUNNER_LOCK_KEY = 712_050_2026

#: The longest a run waits, in seconds, for another run's lock before it
#: fails. Both services' predeploys run on every deploy, and the second waits
#: out the first, whose apply takes seconds; five minutes is far past that. A
#: lock still held then is not a run finishing but a session nobody uses (one
#: a connection pooler kept, or a run that hung), and the deploy fails with
#: the previous version serving instead of waiting on it for good.
RUNNER_LOCK_WAIT = 300

#: The pause, in seconds, between two tries for a held runner lock. Each try
#: is one autocommit statement, so the waiting run holds no transaction and no
#: snapshot between tries (`_acquire_lock`).
RUNNER_LOCK_POLL_S = 0.5

#: The corpus, relative to this file — the home for the path the suites import.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: The initial marker's version: ``000_initial.sql``, empty, recorded by
#: ``adopt`` on a database built from the schema code and never executed.
INITIAL_VERSION = 0
RETIRED_CHAIN_LAST_VERSION = 149
# The retired files that overlap the two pending versions. Their identities
# remain evidence of retired history even when a current initial row is mixed in.
RETIRED_COLLIDING_CHECKSUMS = {
    1: "f75f8a5b999673786d5bc79fca214a6bc7cc7360104fc6ab7703d440f03767c1",
    2: "8e2d5502c1f1ba613c598982824f9588d94f8a3a7fc167df36f310d8cfc136bc",
}

#: The longest a wrapped file waits for any one lock. A file that
#: must wait for ACCESS EXCLUSIVE holds its place in the table's lock queue,
#: and every later statement on the table, readers included, queues behind
#: it. Bounded, the file fails with SQLSTATE 55P03 instead, having stalled the
#: table for at most this long: under the app's own lock bound (the tap
#: path's 2 s), so a statement queued behind it does not fail on its own. A
#: failed apply aborts the deploy with the old version serving, so a lock
#: timeout preserves the running service.
LOCK_TIMEOUT = "1s"

#: The pauses before each new attempt at a wrapped file that failed on its
#: lock wait alone, so a transient holder costs seconds, not a deploy. A file
#: is one transaction, so every attempt starts clean and nothing waits on
#: the table between attempts. About 20 s in all before a holder that never
#: lets go fails the apply.
LOCK_RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0)

NO_TRANSACTION_MARKER = "-- runner:no-transaction"
POSTCONDITION_MARKER = "-- runner:postcondition"
REAPPLY_SAFE_MARKER = "-- runner:reapply-safe"
#: A file the deploy must never run by itself: `apply` skips it where it stands
#: and reports it as OWED, exit 0; `apply --manual <version>` applies it by
#: name, below the head if need be, exactly like any file — ledger row,
#: postconditions, lock.
MANUAL_MARKER = "-- runner:manual"

#: Every `-- runner:<word>` the runner knows. A line that looks like a marker
#: and is not one of these is a HARD FAILURE at discovery: a misspelt marker
#: would otherwise be an ordinary file, applied at the next deploy — for a
#: file that was meant to wait for an operator, that is the whole hazard.
KNOWN_MARKERS = (
    NO_TRANSACTION_MARKER,
    POSTCONDITION_MARKER,
    REAPPLY_SAFE_MARKER,
    MANUAL_MARKER,
)
#: The marker grammar, matched ONCE per line and case-insensitively on the
#: `runner` word so that `-- Runner:manual`, `--runner:manual` and
#: `-- runner: manual` (a space after the colon) all reach the known-set check
#: instead of passing as prose. Only the word after the colon is looked up.
_MARKER_RE = re.compile(r"^--\s*runner\s*:\s*(\S+)(.*)$", re.IGNORECASE)
_KNOWN_WORDS = {m.split(":", 1)[1] for m in KNOWN_MARKERS}
#: A NEAR MISS: a comment opener followed straight by `runner` and a known word
#: in a frame the grammar does not read — three dashes, a doubled `-- --` (an
#: editor's "comment this line" on a comment), a `/* */` or `#` frame, a
#: missing colon or a dash or `=` in its place, a marker after code on the
#: same line. Discovery refuses these forms because treating a malformed
#: `manual` marker as prose would let a predeploy apply a gated file.
#: A mention mid-sentence
#: (`-- the runner:manual marker is what …`) is not a near miss: the
#: opener is followed by a word, not by `runner`.
_NEAR_MISS_RE = re.compile(
    r"(?:^|\s)(?:-{2,}|/\*|#)\s*runner\b\s*[:=\-]?\s*("
    + "|".join(re.escape(w) for w in sorted(_KNOWN_WORDS))
    + r")\b",
    re.IGNORECASE,
)

_FILENAME_RE = re.compile(r"^(\d+)_.+\.sql$")

LEDGER_DDL = """
CREATE SCHEMA IF NOT EXISTS runner;
CREATE TABLE IF NOT EXISTS runner.schema_migrations (
    version INT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by TEXT NOT NULL,
    execution_ms INT,
    status TEXT CHECK (status IN ('applied', 'repaired', 'adopted'))
);
"""


class MigrationRunnerError(Exception):
    """Hard failure: the run stops and the process exits non-zero."""


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path
    checksum: str
    sql: str
    statements: tuple
    no_transaction: bool
    reapply_safe: bool
    postconditions: tuple
    execution_mode: str  # wrapped | self-managed | no-transaction
    manual: bool

    @property
    def label(self) -> str:
        return f"{self.version:03d} ({self.path.name})"


@dataclass
class ApplyReport:
    applied: list = field(default_factory=list)
    #: `runner:manual` files the run left where they stand, for an operator
    owed: list = field(default_factory=list)


def _parse_markers(text: str):
    """The file's markers, and a refusal for a marker the runner does not know.

    One grammar, one dispatch: any line that reads as `-- runner:<word>` is
    looked up by its word in the known set and refused otherwise — a misspelt
    word, a stray space after the colon, a capital R — because the failure
    mode of a marker that quietly reads as prose is a file the next deploy
    applies (for a `manual` file, the whole hazard). A flag marker takes no
    argument; the postcondition marker takes the SQL that follows it and
    refuses to be bare. Raises ``MigrationRunnerError`` naming the offending
    line; ``discover_migrations`` adds the file.
    """
    flags = {
        "no-transaction": False,
        "reapply-safe": False,
        "manual": False,
    }
    postconditions = []
    for line in text.splitlines():
        match = _MARKER_RE.match(line.strip())
        if not match:
            if _NEAR_MISS_RE.search(line):
                raise MigrationRunnerError(
                    f"{line.strip()!r} reads like a marker but is not one — a"
                    " marker is `-- runner:<word>` alone at the start of its line;"
                    " a near miss would silently make this an ordinary file"
                )
            continue
        word, rest = match.group(1), match.group(2).strip()
        if word not in _KNOWN_WORDS:
            raise MigrationRunnerError(
                f"unknown marker {line.strip()!r} — the runner knows"
                f" {', '.join(sorted(_KNOWN_WORDS))}; a misspelt marker is an"
                " ordinary file applied at the next deploy, so it is refused here"
            )
        if word == "postcondition":
            if not rest:
                raise MigrationRunnerError(
                    f"bare marker {line.strip()!r} — a postcondition carries the"
                    " SQL it asserts"
                )
            postconditions.append(rest)
        elif rest:
            raise MigrationRunnerError(
                f"marker {line.strip()!r} carries text after the word; a flag"
                " marker takes no argument"
            )
        else:
            flags[word] = True
    return (
        flags["no-transaction"],
        flags["reapply-safe"],
        flags["manual"],
        tuple(postconditions),
    )


def _execution_mode(no_transaction: bool, statements) -> str:
    """Legacy corpus files carry their own BEGIN/COMMIT (and may follow the
    block with CREATE INDEX CONCURRENTLY); those run with psql semantics —
    statement by statement — because wrapping them in a second transaction
    both nests their BEGIN and breaks the post-commit CONCURRENTLY."""
    if no_transaction:
        return "no-transaction"
    for statement in statements:
        code = "".join(
            " " if s.kind in COMMENTS else s.text for s in segments(statement)
        )
        if code.strip().upper() == "BEGIN":
            return "self-managed"
    return "wrapped"


def discover_migrations(migrations_dir, max_version: int | None = None) -> list:
    """Numbered ``NNN_*.sql`` files in numeric order; anything else ignored.

    Reads, checksums, splits, and mode-classifies each file exactly once —
    every later stage works from the returned records.

    ``max_version`` bounds the result to files at or below that version;
    production never passes it.

    ``000_initial.sql`` must be empty: it marks the schema built from code, so
    a byte in it is refused here, before any caller can apply it.
    """
    migrations_dir = Path(migrations_dir)
    by_version = {}
    for path in sorted(migrations_dir.iterdir()):
        match = _FILENAME_RE.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in by_version:
            raise MigrationRunnerError(
                f"duplicate migration version {version:03d}:"
                f" {by_version[version].path.name} and {path.name}"
            )
        raw = path.read_bytes()
        if version == INITIAL_VERSION and raw:
            raise MigrationRunnerError(
                f"{version:03d} ({path.name}) must be empty: it marks the schema"
                " built from code and holds no SQL"
            )
        try:
            # `utf-8-sig`: a byte-order mark at the start of the file is dropped,
            # so a marker on line 1 is still at the start of its line; the
            # checksum is of the raw bytes and does not change.
            sql = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MigrationRunnerError(
                f"{version:03d} ({path.name}): not UTF-8 ({exc.reason} at byte"
                f" {exc.start}) — the runner reads nothing it cannot decode"
            ) from None
        if "\x00" in sql:
            # UTF-16 WITHOUT a byte-order mark is valid UTF-8 — ASCII with a NUL
            # after every character — so the decode passes and every marker in
            # it reads as prose. psycopg2 refuses a NUL at apply, so nothing
            # applies; the refusal belongs here, by name, beside the other.
            raise MigrationRunnerError(
                f"{version:03d} ({path.name}): holds a NUL byte (at character"
                f" {sql.index(chr(0))}) — not a text file the runner reads"
            )
        try:
            (
                no_transaction,
                reapply_safe,
                manual,
                postconditions,
            ) = _parse_markers(sql)
        except MigrationRunnerError as exc:
            raise MigrationRunnerError(f"{version:03d} ({path.name}): {exc}") from None
        statements = tuple(split_statements(sql))
        by_version[version] = Migration(
            version=version,
            path=path,
            checksum=hashlib.sha256(raw).hexdigest(),
            sql=sql,
            statements=statements,
            no_transaction=no_transaction,
            reapply_safe=reapply_safe,
            postconditions=postconditions,
            execution_mode=_execution_mode(no_transaction, statements),
            manual=manual,
        )
    return _within([by_version[v] for v in sorted(by_version)], max_version)


def _within(migrations, max_version: int | None):
    """The replay window: files at or below ``max_version``, all of them if
    unbounded. One home for the bound so a caller that already holds the whole
    corpus derives the same window `discover_migrations` would have."""
    if max_version is None:
        return list(migrations)
    return [m for m in migrations if m.version <= max_version]


def split_statements(sql: str) -> list:
    """Split a file into single statements, where PostgreSQL would: at a `;` in
    code, never inside a literal, a quoted identifier, a dollar-quoted body or
    a comment. `scripts/sql_lexer.py` owns those bounds.

    Comments stay in the statement they sit in, because the text is what the
    runner executes and reads its execution mode from. A fragment of nothing
    but comments and whitespace is dropped.
    """
    statements, pieces, has_code = [], [], False
    for seg in segments(sql):
        parts = seg.text.split(";") if seg.kind == CODE else [seg.text]
        for n, part in enumerate(parts):
            if n:  # a `;` in code ended the statement before this part
                if has_code:
                    statements.append("".join(pieces))
                pieces, has_code = [], False
            pieces.append(part)
            has_code = has_code or (seg.kind not in COMMENTS and bool(part.strip()))
    if has_code:
        statements.append("".join(pieces))
    return statements


def _connect(dsn: str):
    # libpq quotes the part of the string it could not parse, which can be the
    # password: the refusal names the fixes, never the value, and is raised
    # outside the `except` so that libpq's error is not even its context. The
    # same error covers an unencoded character in the userinfo, a misspelt or
    # `=`-less query parameter and an unknown key/value option, so the hint
    # names all of them rather than guess which.
    unreadable = False
    try:
        conn = psycopg2.connect(dsn)
    except psycopg2.ProgrammingError:
        unreadable = True
    if unreadable:
        raise MigrationRunnerError(
            "the runner's database URL is not one libpq can read; percent-encode"
            " @ / ? # % in the user or password, and check each query parameter's"
            " (or key/value option's) name and its ="
        )
    conn.autocommit = True
    return conn


#: Who holds the runner lock in THIS database (advisory keys are per database,
#: and a cluster can carry many): its pid, its state, and the seconds since
#: that state began. A run applying reads `active` or `idle in transaction`
#: for seconds; a session idle for minutes is one nothing is using. The key is
#: matched whole: it is above 2**32, so `classid` carries its high half.
RUNNER_LOCK_HOLDER_SQL = (
    "SELECT a.pid, a.state,"
    " round(extract(epoch FROM now() - a.state_change))::int"
    " FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
    " WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1"
    " AND l.database = (SELECT oid FROM pg_database"
    " WHERE datname = current_database())"
    " AND ((l.classid::bigint << 32) | l.objid::bigint) = %s"
)


def _describe_holder(row) -> str:
    if row is None:
        return "a session that has just let it go"
    pid, state, seconds = row
    since = "" if seconds is None else f" for {seconds}s"
    return f"pid {pid} ({state or 'state unknown'}{since})"


def _lock_holder(conn) -> str:
    """The runner lock's holder, described. Never raises, so a failed read
    cannot replace the message it was meant to explain."""
    try:
        with conn.cursor() as cur:
            cur.execute(RUNNER_LOCK_HOLDER_SQL, (RUNNER_LOCK_KEY,))
            return _describe_holder(cur.fetchone())
    except psycopg2.Error:
        return "a session the runner could not read"


def _try_lock(conn) -> bool:
    """One try for the runner lock, in autocommit: it never waits."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (RUNNER_LOCK_KEY,))
        return bool(cur.fetchone()[0])


def _acquire_lock(conn) -> None:
    """Take the runner lock, waiting at most `RUNNER_LOCK_WAIT` seconds for
    another run's. A free lock costs one statement. A held one is named before
    the wait, so a deploy's log says what it waits on, and again when the wait
    runs out, so the error names the holder as it stands then.

    The wait polls, one `_try_lock` every `RUNNER_LOCK_POLL_S`, so between
    tries this connection holds no statement, transaction or snapshot. A
    blocking `pg_advisory_lock` would hold its statement's snapshot for the
    whole wait, and the holder's `CREATE INDEX CONCURRENTLY` waits for every
    older snapshot: each would wait on the other until the server ended one of
    them as a deadlock, the build leaving an INVALID index.
    """
    wait = RUNNER_LOCK_WAIT  # read here, not bound as a default: tests patch it
    if _try_lock(conn):
        return
    print(
        f"the runner lock is held by {_lock_holder(conn)}; waiting up to {wait:g}s",
        file=sys.stderr,
    )
    deadline = time.monotonic() + wait
    while True:
        time.sleep(max(0.0, min(RUNNER_LOCK_POLL_S, deadline - time.monotonic())))
        if _try_lock(conn):
            return
        if time.monotonic() >= deadline:
            raise MigrationRunnerError(
                f"the runner lock was still held after {wait:g}s, by"
                f" {_lock_holder(conn)}; nothing was applied. Another run may"
                " still be applying: redeploy once it ends. If nothing is"
                " running, the lock is stranded on a session nobody uses, which"
                " a connection pooler between a runner and the database leaves"
                " behind: the runner needs the database's direct host."
            )


def _release_lock(conn) -> None:
    """Give the runner lock back before the connection closes.

    Closing ends the session and frees the lock, on a direct connection. The
    explicit unlock is for a session that outlives its connection, which is
    what a connection pooler keeps. An unlock that finds nothing held is
    reported, because a run whose session no longer holds its lock ran its
    statements on more than one server session. Never raises: it runs in a
    `finally`, where an error would replace the run's own.
    """
    try:
        with conn.cursor() as cur:
            status = conn.info.transaction_status
            if status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                # A self-managed file can leave its own block open, failed or
                # with no COMMIT, and the server refuses the unlock inside an
                # aborted one.
                cur.execute("ROLLBACK")
            cur.execute("SELECT pg_advisory_unlock(%s)", (RUNNER_LOCK_KEY,))
            released = cur.fetchone()[0]
    except psycopg2.Error as exc:
        print(
            "the runner lock was not given back explicitly"
            f" ({type(exc).__name__}); closing the connection frees it",
            file=sys.stderr,
        )
        return
    if not released:
        print(
            "the runner lock was not held by this session when the run ended:"
            " its statements reached more than one server session, as through a"
            " connection pooler, and the lock may outlive the run. The runner"
            " needs the database's direct host.",
            file=sys.stderr,
        )


@contextmanager
def _locked_connection(dsn: str):
    """The connection a writing verb runs on, holding the runner lock. It is
    the one door to that lock, so every writer is refused a pooled host, waits
    a bounded time and gives the lock back the same way.

    The host judged is the one libpq connected to (`conn.info.host`). libpq
    has already resolved a URL, a key/value string, a `host` parameter, a host
    list, PGHOST or a service file, so no reading here can differ from its own.
    The refusal comes before any statement: through a transaction pooler the
    session lock this would take stays on a pooled server session after the
    run, and the next deploy's runner waits on it.
    """
    conn = _connect(dsn)
    try:
        if is_pooled_host(conn.info.host):
            raise MigrationRunnerError(
                pooled_endpoint_refusal(
                    "the runner's database (DATABASE_URL, --database-url or PGHOST)"
                )
            )
        _acquire_lock(conn)
        try:
            yield conn
            # The backstop behind `_apply_one`'s per-file check: the verb
            # returned, so nothing failed, yet the connection is still inside
            # a transaction, which the release's rollback would discard
            # unseen. No verb reaches this today; on a failure that rollback
            # is the cleanup.
            status = conn.info.transaction_status
            if status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                raise MigrationRunnerError(
                    "the run left a transaction open when it ended; nothing in"
                    " it was committed"
                )
        finally:
            _release_lock(conn)
    finally:
        conn.close()


#: The API reads the ledger for `storydump posture`. The runner creates the
#: ledger independently of the application schema and grants read access on
#: every run when `svc_ingress` exists. Schema construction creates the role
#: in `ddl/01_roles.sql`; the conditional also supports an earlier ledger run.
LEDGER_GRANT_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_ingress') THEN
        GRANT USAGE ON SCHEMA runner TO svc_ingress;
        GRANT SELECT ON runner.schema_migrations TO svc_ingress;
    END IF;
END
$$;
"""


def _ensure_ledger(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(LEDGER_DDL)
        cur.execute(LEDGER_GRANT_SQL)


def _ledger_rows(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum, status FROM runner.schema_migrations")
        return {version: (checksum, status) for version, checksum, status in cur}


def _retired_history_issue(ledger, migrations):
    """Keep retired or mixed history out of the replacement version namespace."""
    by_version = {m.version: m for m in migrations}
    if not ledger or INITIAL_VERSION not in by_version:
        return None
    has_unknown = any(version not in by_version for version in ledger)
    lacks_initial = INITIAL_VERSION not in ledger
    has_collision = any(
        version in by_version and checksum != by_version[version].checksum
        for version, (checksum, _status) in ledger.items()
    )
    known_retired_collision = any(
        RETIRED_COLLIDING_CHECKSUMS.get(version) == checksum
        for version, (checksum, _status) in ledger.items()
    )
    replacement_corpus = {
        "000_initial.sql",
        "001_catalog_text.sql",
        "002_drop_archive_snapshots.sql",
    } <= {migration.path.name for migration in migrations}
    if known_retired_collision or (
        replacement_corpus and lacks_initial and (has_unknown or has_collision)
    ):
        return (
            "the ledger holds retired or mixed migration history; run"
            " `migration_runner convert-ledger --confirm-legacy-ledger` as the"
            " database owner; conversion refuses current chain rows and"
            f" versions above {RETIRED_CHAIN_LAST_VERSION}"
        )
    return None


def _refuse_retired_history(conn, migrations):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('runner.schema_migrations')")
        if cur.fetchone()[0] is None:
            return
    issue = _retired_history_issue(_ledger_rows(conn), migrations)
    if issue:
        raise MigrationRunnerError(issue)


@contextmanager
def _transaction(conn):
    """One explicit transaction on an otherwise-autocommit connection."""
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def ledger_discrepancies(ledger: dict, migrations: list) -> list:
    """The single home of the ledger-vs-tree integrity rules: every recorded
    version must still exist in-tree with its recorded checksum. ``status``
    reports this list; ``apply``/``adopt`` refuse on it — the two doors
    cannot disagree because they share it."""
    retired = _retired_history_issue(ledger, migrations)
    if retired:
        return [(min(ledger), retired)]
    by_version = {m.version: m for m in migrations}
    issues = []
    for version, (checksum, _status) in sorted(ledger.items()):
        migration = by_version.get(version)
        if migration is None:
            issues.append(
                (
                    version,
                    "recorded in ledger but no such file exists in-tree —"
                    " history must never be deleted",
                )
            )
        elif migration.checksum != checksum:
            issues.append(
                (
                    version,
                    f"checksum mismatch for {migration.path.name} — applied"
                    " files are immutable; fix forward with a new migration,"
                    " or record a deliberate exception with: runner repair"
                    f" --version {version} --reason ...",
                )
            )
    return issues


def _verify_integrity(ledger: dict, migrations: list) -> None:
    issues = ledger_discrepancies(ledger, migrations)
    if issues:
        raise MigrationRunnerError(
            "; ".join(f"migration {v:03d}: {detail}" for v, detail in issues)
        )


def _run_postconditions(cursor, migration) -> None:
    for sql in migration.postconditions:
        cursor.execute(sql)
        row = cursor.fetchone()
        if row is None or row[0] is not True:
            raise MigrationRunnerError(
                f"migration {migration.label} postcondition returned"
                f" {row!r}, expected true: {sql}"
            )


def _record(cursor, migration, execution_ms, status: str) -> None:
    cursor.execute(
        "INSERT INTO runner.schema_migrations"
        " (version, checksum, applied_by, execution_ms, status)"
        " VALUES (%s, %s, current_user, %s, %s)",
        (migration.version, migration.checksum, execution_ms, status),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _apply_one(conn, migration, *, confirm_archive_backup: bool = False) -> None:
    started = time.monotonic()
    if migration.execution_mode == "wrapped":
        # One migration = one transaction, ledger row included: applied and
        # recorded are the same fact or neither happens.
        with _transaction(conn) as cur:
            # Bounded lock waits; `true` keeps the setting local to this
            # transaction (SET LOCAL), as the tap path's is.
            cur.execute("SELECT set_config('lock_timeout', %s, true)", (LOCK_TIMEOUT,))
            # Set inside this transaction on the runner's own connection. An
            # ambient session setting cannot replace the operator's flag.
            cur.execute(
                "SELECT set_config('app.archive_snapshot_backup_confirmed', %s, true)",
                ("on" if confirm_archive_backup else "off",),
            )
            cur.execute(migration.sql)
            _run_postconditions(cur, migration)
            _record(cur, migration, _elapsed_ms(started), "applied")
    else:
        # Statement-by-statement in autocommit: a multi-statement simple
        # query runs as one implicit transaction block, which both CREATE
        # INDEX CONCURRENTLY and a file's own BEGIN/COMMIT refuse. Atomicity
        # here is the file's own responsibility. The rollback below sends
        # nothing under autocommit, so a self-managed file that fails inside
        # its own BEGIN leaves that block open on the session; `_release_lock`
        # ends it before it unlocks.
        try:
            with conn.cursor() as cur:
                for statement in migration.statements:
                    cur.execute(statement)
                # A BEGIN with no COMMIT leaves the block open, and the next
                # file would run inside it: a wrapped one's COMMIT would commit
                # both. So the file that left it open fails here, before its
                # ledger row and before anything after it runs. What it
                # committed before that BEGIN stays committed: only the open
                # transaction is rolled back (`_release_lock`).
                status = conn.info.transaction_status
                if status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                    raise MigrationRunnerError(
                        f"migration {migration.label}: a {migration.execution_mode}"
                        " file left its transaction open; the open transaction was"
                        " rolled back and the file is not recorded"
                    )
                _run_postconditions(cur, migration)
                _record(cur, migration, _elapsed_ms(started), "applied")
        except Exception:
            conn.rollback()
            raise


def _apply_guarded(conn, migration, *, confirm_archive_backup: bool = False) -> None:
    """`_apply_one`, with a database failure named by the file — the one
    wrap both doors share. A wrapped file is one transaction, so one that
    failed on its lock wait alone is tried again after each pause in
    `LOCK_RETRY_DELAYS_S`; the last attempt's failure is final."""
    pauses = LOCK_RETRY_DELAYS_S if migration.execution_mode == "wrapped" else ()
    confirmation = {"confirm_archive_backup": True} if confirm_archive_backup else {}
    try:
        for pause in pauses:
            try:
                _apply_one(conn, migration, **confirmation)
                return
            except psycopg2.errors.LockNotAvailable:
                print(
                    f"migration {migration.label}: a lock was held past"
                    f" {LOCK_TIMEOUT}; trying again in {pause:g}s",
                    file=sys.stderr,
                )
                time.sleep(pause)
        _apply_one(conn, migration, **confirmation)
    except MigrationRunnerError:
        raise
    except Exception as exc:
        raise MigrationRunnerError(
            f"migration {migration.label} failed: {exc}"
        ) from exc


def apply_pending(
    dsn: str, migrations_dir, max_version: int | None = None
) -> ApplyReport:
    """Apply every pending migration in version order.

    ``max_version`` stops the run at that version; production leaves it unset.

    ``000_initial.sql`` is never applied: an unrecorded ``000`` is refused,
    because the database it stands for is built from the schema code and
    recorded with ``adopt``.

    A pending ``runner:manual`` file is OWED, not applied: it is left where it
    stands, reported on ``owed``, and exempt from the below-head rule — an
    ordinary file numbered above it applies as if it were not there. Its door
    is :func:`apply_manual`.
    """
    corpus = discover_migrations(migrations_dir)
    migrations = _within(corpus, max_version)
    report = ApplyReport()
    with _locked_connection(dsn) as conn:
        _refuse_retired_history(conn, corpus)
        _ensure_ledger(conn)
        ledger = _ledger_rows(conn)
        _verify_integrity(ledger, migrations)

        applied_head = max(ledger, default=0)
        pending = [m for m in migrations if m.version not in ledger]
        for migration in pending:
            if migration.version == INITIAL_VERSION:
                raise MigrationRunnerError(
                    f"{migration.label} is not recorded: build the schema from"
                    " code and record it with `adopt`; it is never applied"
                )
        report.owed = [m for m in pending if m.manual]
        pending = [m for m in pending if not m.manual]
        for migration in pending:
            if migration.version < applied_head and not migration.reapply_safe:
                raise MigrationRunnerError(
                    f"pending migration {migration.label} is numbered below"
                    f" the applied head ({applied_head:03d}) — a file"
                    " inserted under already-applied history breaks the"
                    " chain; renumber it above the head"
                )

        for migration in pending:
            _apply_guarded(conn, migration)
            report.applied.append(migration)
    return report


def apply_manual(
    dsn: str, migrations_dir, version: int, *, confirm_archive_backup: bool = False
) -> ApplyReport:
    """Apply ONE gated file by version — the operator's door, never a deploy's.

    The file must carry ``runner:manual`` (an ordinary pending file is
    ``apply``'s to run in order; running it by name would bypass that order),
    must exist in the tree, and must not already be recorded — a gated file
    runs once. It applies exactly like any other file: under the advisory
    lock, after the integrity check, in one transaction with its
    postconditions and its ledger row; and it applies BELOW the head, because
    the below-head rule is for files inserted under history by mistake, and a
    gated file is under it by design. No other pending file is touched — and
    an ORDINARY file still pending below it refuses the door: the tree the
    gated file was written against is the one with everything before it
    applied, so `apply` runs first.

    ``confirm_archive_backup`` acknowledges that the sixteen snapshot tables
    have been exported or backed up. It is accepted only for wrapped 002 and
    reaches its SQL as a transaction-local setting on this runner connection.
    """
    if confirm_archive_backup and version != 2:
        raise MigrationRunnerError("archive backup confirmation requires --manual 002")
    migrations = discover_migrations(migrations_dir)
    by_version = {m.version: m for m in migrations}
    migration = by_version.get(version)
    if migration is None:
        raise MigrationRunnerError(
            f"no migration {version:03d} in {Path(migrations_dir)}"
        )
    if not migration.manual:
        raise MigrationRunnerError(
            f"{migration.label} carries no runner:manual directive — it is"
            " `apply`'s to run in order; --manual is the door for gated files only"
        )
    if confirm_archive_backup and migration.execution_mode != "wrapped":
        raise MigrationRunnerError("archive backup confirmation requires wrapped 002")
    report = ApplyReport()
    with _locked_connection(dsn) as conn:
        _refuse_retired_history(conn, migrations)
        _ensure_ledger(conn)
        ledger = _ledger_rows(conn)
        _verify_integrity(ledger, migrations)
        if version in ledger:
            raise MigrationRunnerError(
                f"{migration.label} is already recorded in the ledger"
                f" ({ledger[version][1]}); a gated file runs once"
            )
        below = [
            m.label
            for m in migrations
            if m.version < version and not m.manual and m.version not in ledger
        ]
        if below:
            raise MigrationRunnerError(
                f"{migration.label} cannot run over a pending ordinary file below"
                f" it ({', '.join(below)}); run `apply` first"
            )
        _apply_guarded(conn, migration, confirm_archive_backup=confirm_archive_backup)
        report.applied.append(migration)
    return report


@dataclass
class AdoptReport:
    adopted: list = field(default_factory=list)
    asserted: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    already: list = field(default_factory=list)


def _load_manifest(manifest_path, window, corpus):
    """The adoption manifest is a contract kept by hand.

    Every file in the replay window is paired with adoption evidence, one of:

    - an explicit ``probe`` entry (SQL returning bool);
    - an explicit ``asserted`` entry (data-only files with no structural
      delta — legal only at or below ``required_through``, because above the
      floor trust is never the mechanism);
    - no entry at all, iff the file carries ``runner:postcondition`` lines —
      the probe derives from them, so the predicate has one home.

    **The two checks below ask different questions and take different lists,
    and conflating them makes a bounded adopt lie.** One list serving both
    looks harmless until a bound exists:

    - *unpaired* is a **window** question — "does everything I am about to
      decide on have a probe?" Adopt decides nothing about files outside the
      window, so demanding evidence for them would fail a bounded adopt
      because a file above the bound lacks a probe: true, and about the wrong
      files.
    - *orphans* is a **corpus** question — "does every manifest key name a
      real file?" Asked against the window instead, every entry above the
      bound is reported as having *no migration file* when the file is right
      there and was merely filtered out. That message sends an operator
      looking for files that exist, which is worse than no message: it answers
      a question nobody asked, confidently, mid-incident.
    """
    data = json.loads(Path(manifest_path).read_text())
    required_through = int(data["required_through"])
    entries = {}
    for entry in data["entries"]:
        version = int(entry["version"])
        if version in entries:
            raise MigrationRunnerError(
                f"adoption manifest lists version {version:03d} twice"
            )
        if entry.get("asserted"):
            if "probe" in entry:
                raise MigrationRunnerError(
                    f"manifest entry {version:03d} carries both a probe and"
                    " an assertion — pick one"
                )
            if version > required_through:
                raise MigrationRunnerError(
                    f"manifest entry {version:03d} is asserted but sits above"
                    f" the required floor ({required_through:03d}) — above"
                    " the floor, evidence is the mechanism; assertion is not"
                    " evidence"
                )
            entries[version] = ("asserted", (entry.get("reason", ""),))
        else:
            entries[version] = ("probe", (entry["probe"],))

    in_corpus = {m.version for m in corpus}
    unpaired = []
    for migration in window:
        if migration.version in entries:
            continue
        if migration.postconditions:
            entries[migration.version] = ("probe", migration.postconditions)
        else:
            unpaired.append(migration.version)
    if unpaired:
        raise MigrationRunnerError(
            "migrations with neither an adoption-manifest entry nor"
            " runner:postcondition lines to derive one from: "
            + ", ".join(f"{v:03d}" for v in unpaired)
        )
    orphans = sorted(set(entries) - in_corpus)
    if orphans:
        raise MigrationRunnerError(
            "adoption manifest names versions with no migration file: "
            + ", ".join(f"{v:03d}" for v in orphans)
        )
    return required_through, entries


def _adoption_plan(conn, migrations, required_through, entries):
    """Probe the current chain on a caller-owned connection before recording it."""
    report = AdoptReport()
    ledger = _ledger_rows(conn)
    _verify_integrity(ledger, migrations)

    report.already = [m for m in migrations if m.version in ledger]
    candidates = [m for m in migrations if m.version not in ledger]

    results = {}
    asserted_versions = set()
    with conn.cursor() as cur:
        for migration in candidates:
            kind, payload = entries[migration.version]
            if kind == "asserted":
                results[migration.version] = True
                asserted_versions.add(migration.version)
                continue
            verdict = True
            for probe in payload:
                try:
                    cur.execute(probe)
                    row = cur.fetchone()
                except Exception as exc:
                    raise MigrationRunnerError(
                        f"adoption probe for migration {migration.label}"
                        f" errored — a broken probe is not evidence of"
                        f" anything: {probe}: {exc}"
                    ) from exc
                if row is None or row[0] is not True:
                    verdict = False
                    break
            results[migration.version] = verdict

    for migration in candidates:
        if migration.version <= required_through and not results[migration.version]:
            _kind, payload = entries[migration.version]
            raise MigrationRunnerError(
                f"migration {migration.label} is required applied"
                f" (floor {required_through:03d}) but its probe returned"
                f" false: {'; '.join(payload)}"
            )

    head = max((v for v, ok in results.items() if ok), default=0)
    for migration in candidates:
        if (
            migration.version < head
            and not results[migration.version]
            and not migration.reapply_safe
        ):
            raise MigrationRunnerError(
                f"incoherent chain: migration {migration.label} probes"
                f" unapplied while later migration {head:03d} probes"
                " applied — a gap under applied history needs a human"
                " ruling (runner repair), not a guess"
            )

    to_record = [m for m in candidates if results[m.version]]
    report.pending = [m for m in candidates if not results[m.version]]

    report.adopted = [m for m in to_record if m.version not in asserted_versions]
    report.asserted = [m for m in to_record if m.version in asserted_versions]
    return report, to_record


def adopt(
    dsn: str, migrations_dir, manifest_path, max_version: int | None = None
) -> AdoptReport:
    """Enter a live database that predates the ledger into it.

    Probe-decided per file, never trusted: a probe returning true adopts the
    file; a false probe leaves it pending **only** when every later probe is
    also false (a contiguous unapplied tail). A false at or
    below the manifest's required floor, a false below a true (an incoherent
    chain), or a probe that errors is a hard failure naming the version — and
    a failed adopt writes nothing, so there is no partial adoption to reason
    about. Adoption reads schema probes and never trusts the legacy
    `schema_version` table as evidence that a migration is present.
    """
    corpus = discover_migrations(migrations_dir)
    migrations = _within(corpus, max_version)
    required_through, entries = _load_manifest(manifest_path, migrations, corpus)
    with _locked_connection(dsn) as conn:
        _refuse_retired_history(conn, corpus)
        _ensure_ledger(conn)
        report, to_record = _adoption_plan(conn, migrations, required_through, entries)
        # All-or-nothing: every adoption row lands in one transaction.
        with _transaction(conn) as cur:
            for migration in to_record:
                _record(cur, migration, None, "adopted")
    return report


def convert_ledger(
    dsn: str,
    migrations_dir,
    manifest_path,
    *,
    confirm_legacy_ledger: bool = False,
) -> AdoptReport:
    """Preserve the retired ledger and adopt the current chain atomically.

    Only the database owner may confirm this conversion. The retired table
    keeps its complete rows, ownership, and grants under its new name; it is
    never dropped. The replacement ledger is populated only by adoption
    probes, and a failed adoption rolls the rename back.
    """
    if not confirm_legacy_ledger:
        raise MigrationRunnerError("convert-ledger requires --confirm-legacy-ledger")
    migrations = discover_migrations(migrations_dir)
    required_through, entries = _load_manifest(manifest_path, migrations, migrations)
    checksums = {migration.checksum for migration in migrations}
    filenames = {migration.path.name for migration in migrations}
    with _locked_connection(dsn) as conn:
        with _transaction(conn) as cur:
            # A writer may commit while the table lock waits. Read its rows
            # after that wait even when the DSN defaults to repeatable read.
            cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cur.execute(
                "SELECT current_user = pg_get_userbyid(datdba) FROM pg_database"
                " WHERE datname = current_database()"
            )
            if not cur.fetchone()[0]:
                raise MigrationRunnerError(
                    "convert-ledger must run as the database owner"
                )
            cur.execute("SELECT to_regclass('runner.schema_migrations_legacy')")
            if cur.fetchone()[0] is not None:
                raise MigrationRunnerError(
                    "runner.schema_migrations_legacy already exists"
                )
            cur.execute("SELECT to_regclass('runner.schema_migrations')")
            if cur.fetchone()[0] is None:
                raise MigrationRunnerError(
                    "convert-ledger requires an existing retired ledger"
                )
            cur.execute("LOCK TABLE runner.schema_migrations IN ACCESS EXCLUSIVE MODE")
            cur.execute("SELECT * FROM runner.schema_migrations LIMIT 0")
            columns = {column.name for column in cur.description}
            required = {
                "version",
                "checksum",
                "applied_at",
                "applied_by",
                "execution_ms",
                "status",
            }
            if not required <= columns:
                raise MigrationRunnerError(
                    "convert-ledger refuses an unsupported ledger layout"
                )
            cur.execute("SELECT to_jsonb(m) FROM runner.schema_migrations m")
            rows = [row[0] for row in cur.fetchall()]
            if not rows:
                raise MigrationRunnerError(
                    "convert-ledger requires nonempty retired history"
                )
            if any(row["version"] > RETIRED_CHAIN_LAST_VERSION for row in rows):
                raise MigrationRunnerError("convert-ledger refuses versions above 149")
            if any(
                row["version"] == INITIAL_VERSION
                or row["checksum"] in checksums
                or any(
                    value in filenames
                    for value in row.values()
                    if isinstance(value, str)
                )
                for row in rows
            ):
                raise MigrationRunnerError(
                    "convert-ledger refuses any row from the current chain"
                )
            if any(row["version"] < 1 for row in rows):
                raise MigrationRunnerError(
                    "convert-ledger requires retired versions 1 through 149"
                )
            cur.execute(
                "ALTER TABLE runner.schema_migrations RENAME TO schema_migrations_legacy"
            )
            _ensure_ledger(conn)
            report, to_record = _adoption_plan(
                conn, migrations, required_through, entries
            )
            for migration in to_record:
                _record(cur, migration, None, "adopted")
    return report


def repair(dsn: str, migrations_dir, version: int, reason: str) -> None:
    """Record a deliberate checksum exception: the ledger row takes the
    in-tree file's current checksum with ``status='repaired'``. The reason is
    mandatory — it forces the exception to be stated — and lands in the run
    log (the ledger schema is spec-fixed and carries no reason column)."""
    if not reason or not reason.strip():
        raise MigrationRunnerError("repair requires a non-empty --reason")
    migrations = {m.version: m for m in discover_migrations(migrations_dir)}
    migration = migrations.get(version)
    if migration is None:
        raise MigrationRunnerError(
            f"repair: no migration file for version {version:03d}"
        )
    with _locked_connection(dsn) as conn:
        _refuse_retired_history(conn, list(migrations.values()))
        _ensure_ledger(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runner.schema_migrations"
                " SET checksum = %s, status = 'repaired'"
                " WHERE version = %s",
                (migration.checksum, version),
            )
            if cur.rowcount != 1:
                raise MigrationRunnerError(
                    f"repair: ledger has no row for version {version:03d}"
                )
        print(f"repaired {migration.label}: {reason.strip()}")


@dataclass
class StatusReport:
    ledger_present: bool
    applied: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    #: `runner:manual` files not yet recorded — owed to an operator's hand
    owed: list = field(default_factory=list)
    discrepancies: list = field(default_factory=list)  # (version, detail)


def status(dsn: str, migrations_dir) -> StatusReport:
    """Read-only report of ledger vs tree — creates nothing, raises on
    nothing it can merely report. Shares the integrity rules with apply, so
    the two doors cannot disagree."""
    migrations = discover_migrations(migrations_dir)
    conn = _connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
                " WHERE table_schema = 'runner'"
                " AND table_name = 'schema_migrations')"
            )
            ledger_present = cur.fetchone()[0]
        if not ledger_present:
            return StatusReport(
                ledger_present=False,
                pending=[m for m in migrations if not m.manual],
                owed=[m for m in migrations if m.manual],
            )

        ledger = _ledger_rows(conn)
        report = StatusReport(ledger_present=True)
        report.discrepancies = ledger_discrepancies(ledger, migrations)
        flagged = {v for v, _ in report.discrepancies}
        by_version = {m.version: m for m in migrations}
        report.applied = [
            (by_version[v], row_status)
            for v, (_checksum, row_status) in sorted(ledger.items())
            if v in by_version and v not in flagged
        ]
        unrecorded = [m for m in migrations if m.version not in ledger]
        report.pending = [m for m in unrecorded if not m.manual]
        report.owed = [m for m in unrecorded if m.manual]
        return report
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="migration_runner")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="connection string; defaults to DATABASE_URL",
    )
    parser.add_argument(
        "--migrations-dir",
        default=str(MIGRATIONS_DIR),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser(
        "apply",
        help="apply every pending migration (a runner:manual file is owed, not applied)",
    )
    apply_parser.add_argument(
        "--manual",
        type=int,
        default=None,
        metavar="VERSION",
        help="apply ONE gated (runner:manual) file by version, below the head if"
        " need be — the operator's door, never a deploy's",
    )
    apply_parser.add_argument(
        "--confirm-archive-backup",
        action="store_true",
        help="confirm the sixteen archive snapshot tables were exported or backed up;"
        " requires --manual 002",
    )
    adopt_parser = subparsers.add_parser(
        "adopt",
        help="enter a pre-ledger live database into the ledger, probe-decided",
    )
    adopt_parser.add_argument(
        "--manifest",
        default=None,
        help="adoption manifest path; defaults to"
        " <migrations-dir>/adoption_manifest.json",
    )
    convert_parser = subparsers.add_parser(
        "convert-ledger",
        help="preserve the retired ledger and adopt the current chain as database owner",
    )
    convert_parser.add_argument(
        "--confirm-legacy-ledger",
        action="store_true",
        help="confirm the existing ledger belongs to the retired migration chain",
    )
    convert_parser.add_argument(
        "--manifest",
        default=None,
        help="adoption manifest path; defaults to <migrations-dir>/adoption_manifest.json",
    )
    repair_parser = subparsers.add_parser(
        "repair", help="record a deliberate checksum exception"
    )
    repair_parser.add_argument("--version", type=int, required=True)
    repair_parser.add_argument("--reason", required=True)
    subparsers.add_parser("status", help="read-only ledger vs tree report")
    parity_parser = subparsers.add_parser(
        "parity",
        help="compare the full catalog against a database built from schema code",
    )
    parity_parser.add_argument(
        "--against", required=True, help="connection string to compare against"
    )

    args = parser.parse_args(argv)
    if args.command == "apply" and args.confirm_archive_backup and args.manual != 2:
        parser.error("--confirm-archive-backup requires --manual 002")
    if not args.database_url:
        print("no database: set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2

    try:
        if args.command == "apply" and args.manual is not None:
            report = apply_manual(
                args.database_url,
                args.migrations_dir,
                args.manual,
                confirm_archive_backup=args.confirm_archive_backup,
            )
            for migration in report.applied:
                print(f"applied {migration.label}")
            print(f"{len(report.applied)} applied (--manual)")
        elif args.command == "apply":
            report = apply_pending(args.database_url, args.migrations_dir)
            for migration in report.applied:
                print(f"applied {migration.label}")
            for migration in report.owed:
                print(f"owed (manual) {migration.label}")
            print(f"{len(report.applied)} applied")
            if report.owed:
                print(
                    f"{len(report.owed)} owed (manual): waiting for an operator's"
                    " `apply --manual <version>`; a deploy never runs them"
                )
        elif args.command in ("adopt", "convert-ledger"):
            manifest = args.manifest or str(
                Path(args.migrations_dir) / "adoption_manifest.json"
            )
            if args.command == "convert-ledger":
                adopt_report = convert_ledger(
                    args.database_url,
                    args.migrations_dir,
                    manifest,
                    confirm_legacy_ledger=args.confirm_legacy_ledger,
                )
                print(
                    "preserved runner.schema_migrations as runner.schema_migrations_legacy"
                )
            else:
                adopt_report = adopt(args.database_url, args.migrations_dir, manifest)
            for migration in adopt_report.adopted:
                print(f"adopted {migration.label}")
            for migration in adopt_report.asserted:
                print(f"asserted {migration.label} (declared, not probed)")
            for migration in adopt_report.pending:
                print(
                    f"pending {migration.label} (unapplied tail — a gated"
                    " apply runs it)"
                )
            print(
                f"{len(adopt_report.adopted)} adopted,"
                f" {len(adopt_report.asserted)} asserted,"
                f" {len(adopt_report.already)} already recorded,"
                f" {len(adopt_report.pending)} pending"
            )
        elif args.command == "repair":
            repair(
                args.database_url,
                args.migrations_dir,
                version=args.version,
                reason=args.reason,
            )
        elif args.command == "status":
            status_report = status(args.database_url, args.migrations_dir)
            if not status_report.ledger_present:
                print(
                    "ledger absent — this database predates the runner"
                    " (runner adopt is how it enters)"
                )
            for migration, row_status in status_report.applied:
                print(f"{row_status} {migration.label}")
            for migration in status_report.pending:
                print(f"pending {migration.label} [{migration.execution_mode}]")
            for migration in status_report.owed:
                print(f"owed (manual) {migration.label}")
            for version, detail in status_report.discrepancies:
                print(f"DISCREPANCY {version:03d}: {detail}")
            if status_report.discrepancies:
                return 1
        elif args.command == "parity":
            from scripts.schema_parity import print_comparison, schema_signature

            return print_comparison(
                schema_signature(args.database_url, read_only=True),
                schema_signature(args.against, read_only=True),
            )
    except MigrationRunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
