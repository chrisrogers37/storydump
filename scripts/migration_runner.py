"""Numbered-SQL migration runner (plan §0.2, C6) — ledger, apply, adopt, repair.

Standalone by design: a Railway predeploy step runs this before the app code
boots, so it imports nothing from ``src`` (whose settings module requires the
full runtime environment) and depends only on the standard library plus
psycopg2. The database is addressed by ``DATABASE_URL`` or ``--database-url``.

The ledger lives in a dedicated ``runner`` schema — never ``public`` — because
the M.3 cutover renames ``public`` wholesale, and a ledger living there would
ride into ``legacy`` mid-run, severing the migration history from the very
invocation writing it. The ``runner`` schema is invariant across every
schema-level operation, identically in CI and production.

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
  state is undecidable in place (048's class): adopt may leave them pending
  below an adopted head without calling the chain incoherent, and apply
  re-runs them there.
- ``-- runner:schema-move`` — the one file that renames ``public`` to
  ``legacy`` and re-creates an empty ``public`` (M.3 step 3c). It is the
  corpus's **lineage boundary**: below it is the legacy lineage, above it the
  target schema. Exactly one file may carry it. Callers needing the boundary
  call ``legacy_lineage_max`` and let the marker locate it — the boundary is
  never written down as a version, because a written-down number is a second
  enumeration that drifts from the file it describes (``04``'s own rollback
  leg names it "the 3c move file", not a number, for the same reason).

The three doors that WRITE from the corpus — ``discover_migrations``,
``apply_pending`` and ``adopt`` — take an optional ``max_version``: a bound
callers use to replay one *lineage* rather than the whole tree. ``status``
deliberately does not: an operator asking what is pending wants the whole
answer, target lineage included. Production never passes a bound either — the
M.3 window is one invocation in file order.

A file's execution mode is a declared discovery-time fact — ``wrapped`` (the
runner owns one transaction, ledger row inside it), ``self-managed`` (the file
carries its own BEGIN/COMMIT, the legacy corpus shape, run with psql
semantics), or ``no-transaction`` — visible in ``status`` output. New files
(052+) should be ``wrapped``.
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

# One fixed key for every runner invocation against a given database: the
# whole run holds pg_advisory_lock(RUNNER_LOCK_KEY), so concurrent deploys
# serialize and the loser finds the versions applied and no-ops.
RUNNER_LOCK_KEY = 712_050_2026

NO_TRANSACTION_MARKER = "-- runner:no-transaction"
POSTCONDITION_MARKER = "-- runner:postcondition"
REAPPLY_SAFE_MARKER = "-- runner:reapply-safe"
SCHEMA_MOVE_MARKER = "-- runner:schema-move"

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
    schema_move: bool

    @property
    def label(self) -> str:
        return f"{self.version:03d} ({self.path.name})"


@dataclass
class ApplyReport:
    applied: list = field(default_factory=list)


def _parse_markers(text: str):
    no_transaction = False
    reapply_safe = False
    schema_move = False
    postconditions = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == NO_TRANSACTION_MARKER:
            no_transaction = True
        elif stripped == REAPPLY_SAFE_MARKER:
            reapply_safe = True
        elif stripped == SCHEMA_MOVE_MARKER:
            schema_move = True
        elif stripped.startswith(POSTCONDITION_MARKER):
            sql = stripped[len(POSTCONDITION_MARKER) :].strip()
            if sql:
                postconditions.append(sql)
    return no_transaction, reapply_safe, schema_move, tuple(postconditions)


def _execution_mode(no_transaction: bool, statements) -> str:
    """Legacy corpus files carry their own BEGIN/COMMIT (and may follow the
    block with CREATE INDEX CONCURRENTLY); those run with psql semantics —
    statement by statement — because wrapping them in a second transaction
    both nests their BEGIN and breaks the post-commit CONCURRENTLY."""
    if no_transaction:
        return "no-transaction"
    for statement in statements:
        content = "\n".join(
            line
            for line in statement.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ).strip()
        if content.upper() == "BEGIN":
            return "self-managed"
    return "wrapped"


def discover_migrations(migrations_dir, max_version: int | None = None) -> list:
    """Numbered ``NNN_*.sql`` files in numeric order; anything else ignored.

    Reads, checksums, splits, and mode-classifies each file exactly once —
    every later stage works from the returned records.

    ``max_version`` bounds the result to files at or below that version. It is
    how a caller replays one *lineage* out of a tree that holds two (see
    ``legacy_lineage_max``); production never passes it.
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
        sql = raw.decode()
        no_transaction, reapply_safe, schema_move, postconditions = _parse_markers(sql)
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
            schema_move=schema_move,
        )
    return _within([by_version[v] for v in sorted(by_version)], max_version)


def _within(migrations, max_version: int | None):
    """The replay window: files at or below ``max_version``, all of them if
    unbounded. One home for the bound so a caller that already holds the whole
    corpus derives the same window `discover_migrations` would have."""
    if max_version is None:
        return list(migrations)
    return [m for m in migrations if m.version <= max_version]


def schema_move_migration(migrations_dir):
    """The corpus's one lineage-boundary file, located by its own marker.

    The single home of the "find the boundary" rule, so the three questions
    callers actually ask — which file is it, what is below it, what does it
    assert — cannot answer from three different scans.

    Loud in both directions. **No move file is a hard failure, not an
    unbounded pass**: callers are asking "where does the legacy lineage end",
    and answering "nowhere, take all of it" when the boundary cannot be found
    hands back the whole target schema under the legacy one's name — absence of
    evidence read as evidence of absence, on a door that licenses a replay.
    Two move files is a hard failure because the corpus then has no single
    boundary to derive.

    Cardinality is checked here rather than in ``discover_migrations`` so that
    a corpus is refused to the caller that needs a boundary, not to every
    caller that merely reads files. Moving it into discovery would be a
    stronger invariant; it is also a behavioural change to ``apply_pending``
    and ``adopt``, so it belongs to whoever decides that deliberately.
    """
    moves = [m for m in discover_migrations(migrations_dir) if m.schema_move]
    if not moves:
        raise MigrationRunnerError(
            f"no migration in {migrations_dir} carries {SCHEMA_MOVE_MARKER!r} —"
            " the legacy/target lineage boundary is derived from that marker"
            " and cannot be guessed"
        )
    if len(moves) > 1:
        names = ", ".join(m.path.name for m in moves)
        raise MigrationRunnerError(
            f"{len(moves)} migrations carry {SCHEMA_MOVE_MARKER!r} ({names}) —"
            " the corpus has exactly one lineage boundary"
        )
    return moves[0]


def legacy_lineage_max(migrations_dir) -> int:
    """The last version of the LEGACY lineage — one below the schema move.

    Derived from the marker, never declared: the move file says which file it
    is, so renumbering it moves the boundary with it and there is no second
    inventory to keep in step. That is the same property that decided the
    target-model fork (#746) — a check whose input is derived cannot drift from
    the artifact it describes, and a check whose input is written down will.
    """
    return schema_move_migration(migrations_dir).version - 1


def split_statements(sql: str) -> list:
    """Split a file into single statements.

    Aware of single quotes, double quotes, dollar-quoted bodies, and line
    comments — enough for the DDL migration files are restricted to.
    """
    statements = []
    buf = []
    i = 0
    in_single = in_double = False
    dollar_tag = None
    while i < len(sql):
        ch = sql[i]
        if dollar_tag:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
        elif in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = len(sql) if end == -1 else end
            buf.append(sql[i:end])
            i = end
            continue
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "$":
            match = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if match:
                dollar_tag = match.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
        elif ch == ";":
            statements.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    statements.append("".join(buf))
    return [s for s in statements if s.strip() and not _is_only_comments(s)]


def _is_only_comments(fragment: str) -> bool:
    return all(
        not line.strip() or line.strip().startswith("--")
        for line in fragment.splitlines()
    )


def _connect(dsn: str):
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def _acquire_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (RUNNER_LOCK_KEY,))


def _ensure_ledger(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(LEDGER_DDL)


def _ledger_rows(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum, status FROM runner.schema_migrations")
        return {version: (checksum, status) for version, checksum, status in cur}


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


def _apply_one(conn, migration) -> None:
    started = time.monotonic()
    if migration.execution_mode == "wrapped":
        # One migration = one transaction, ledger row included: applied and
        # recorded are the same fact or neither happens.
        with _transaction(conn) as cur:
            cur.execute(migration.sql)
            _run_postconditions(cur, migration)
            _record(cur, migration, _elapsed_ms(started), "applied")
    else:
        # Statement-by-statement in autocommit: a multi-statement simple
        # query runs as one implicit transaction block, which both CREATE
        # INDEX CONCURRENTLY and a file's own BEGIN/COMMIT refuse. The
        # rollback clears a self-managed file's dangling aborted block on
        # failure; atomicity here is the file's own responsibility.
        try:
            with conn.cursor() as cur:
                for statement in migration.statements:
                    cur.execute(statement)
                _run_postconditions(cur, migration)
                _record(cur, migration, _elapsed_ms(started), "applied")
        except Exception:
            conn.rollback()
            raise


def apply_pending(
    dsn: str, migrations_dir, max_version: int | None = None
) -> ApplyReport:
    """Apply every pending migration in version order.

    ``max_version`` replays one lineage out of the tree (``legacy_lineage_max``
    derives the legacy one). Production leaves it unset — the M.3 window is a
    single unbounded invocation in file order.
    """
    migrations = discover_migrations(migrations_dir, max_version)
    report = ApplyReport()
    conn = _connect(dsn)
    try:
        _acquire_lock(conn)
        _ensure_ledger(conn)
        ledger = _ledger_rows(conn)
        _verify_integrity(ledger, migrations)

        applied_head = max(ledger, default=0)
        pending = [m for m in migrations if m.version not in ledger]
        for migration in pending:
            if migration.version < applied_head and not migration.reapply_safe:
                raise MigrationRunnerError(
                    f"pending migration {migration.label} is numbered below"
                    f" the applied head ({applied_head:03d}) — a file"
                    " inserted under already-applied history breaks the"
                    " chain; renumber it above the head"
                )

        for migration in pending:
            try:
                _apply_one(conn, migration)
            except MigrationRunnerError:
                raise
            except Exception as exc:
                raise MigrationRunnerError(
                    f"migration {migration.label} failed: {exc}"
                ) from exc
            report.applied.append(migration)
    finally:
        conn.close()
    return report


@dataclass
class AdoptReport:
    adopted: list = field(default_factory=list)
    asserted: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    already: list = field(default_factory=list)


def _load_manifest(manifest_path, window, corpus):
    """The adoption manifest is a reviewed contract.

    Every file in the replay window is paired with adoption evidence, one of:

    - an explicit ``probe`` entry (SQL returning bool);
    - an explicit ``asserted`` entry (data-only files with no structural
      delta — legal only at or below ``required_through``, because above the
      floor trust is never the mechanism);
    - no entry at all, iff the file carries ``runner:postcondition`` lines —
      the probe derives from them, so the predicate has one home.

    **The two checks below ask different questions and take different lists,
    and conflating them is what made a bounded adopt lie** (#746 review). One
    list serving both looks harmless until a bound exists:

    - *unpaired* is a **window** question — "does everything I am about to
      decide on have a probe?" Adopt decides nothing about files outside the
      window, so demanding evidence for them would fail a legacy-lineage adopt
      because a *target* file lacks a probe: true, and about the wrong files.
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


def adopt(
    dsn: str, migrations_dir, manifest_path, max_version: int | None = None
) -> AdoptReport:
    """Enter a live database that predates the ledger into it.

    Probe-decided per file, never trusted: a probe returning true adopts the
    file; a false probe leaves it pending **only** when every later probe is
    also false (a contiguous unapplied tail — the at-45 world). A false at or
    below the manifest's required floor, a false below a true (an incoherent
    chain), or a probe that errors is a hard failure naming the version — and
    a failed adopt writes nothing, so there is no partial adoption to reason
    about. The legacy ``schema_version`` table is never read; its known
    010/034 gaps are exactly the hazard this replaces.
    """
    corpus = discover_migrations(migrations_dir)
    migrations = _within(corpus, max_version)
    required_through, entries = _load_manifest(manifest_path, migrations, corpus)
    report = AdoptReport()
    conn = _connect(dsn)
    try:
        _acquire_lock(conn)
        _ensure_ledger(conn)
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

        # All-or-nothing: every adoption row lands in one transaction.
        with _transaction(conn) as cur:
            for migration in to_record:
                _record(cur, migration, None, "adopted")
        report.adopted = [m for m in to_record if m.version not in asserted_versions]
        report.asserted = [m for m in to_record if m.version in asserted_versions]
    finally:
        conn.close()
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
    conn = _connect(dsn)
    try:
        _acquire_lock(conn)
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
    finally:
        conn.close()


@dataclass
class StatusReport:
    ledger_present: bool
    applied: list = field(default_factory=list)
    pending: list = field(default_factory=list)
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
            return StatusReport(ledger_present=False, pending=migrations)

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
        report.pending = [m for m in migrations if m.version not in ledger]
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
        default=str(Path(__file__).parent / "migrations"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("apply", help="apply every pending migration")
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
    repair_parser = subparsers.add_parser(
        "repair", help="record a deliberate checksum exception"
    )
    repair_parser.add_argument("--version", type=int, required=True)
    repair_parser.add_argument("--reason", required=True)
    subparsers.add_parser("status", help="read-only ledger vs tree report")
    parity_parser = subparsers.add_parser(
        "parity",
        help="compare this database's public schema against another's",
    )
    parity_parser.add_argument(
        "--against", required=True, help="connection string to compare against"
    )

    args = parser.parse_args(argv)
    if not args.database_url:
        print("no database: set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2

    try:
        if args.command == "apply":
            report = apply_pending(args.database_url, args.migrations_dir)
            for migration in report.applied:
                print(f"applied {migration.label}")
            print(f"{len(report.applied)} applied")
        elif args.command == "adopt":
            manifest = args.manifest or str(
                Path(args.migrations_dir) / "adoption_manifest.json"
            )
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
            for version, detail in status_report.discrepancies:
                print(f"DISCREPANCY {version:03d}: {detail}")
            if status_report.discrepancies:
                return 1
        elif args.command == "parity":
            from scripts.schema_parity import schema_diff, schema_signature

            diffs = schema_diff(
                schema_signature(args.database_url),
                schema_signature(args.against),
            )
            for diff in diffs:
                print(diff)
            print(f"{len(diffs)} difference(s)")
            if diffs:
                return 1
    except MigrationRunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
