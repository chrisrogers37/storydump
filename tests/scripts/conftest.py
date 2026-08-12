"""Shared fixtures and helpers for the migration-runner suites.

Real-PostgreSQL scratch databases, one per test, dropped on teardown — never
the shared storyline_test database. Connection parameters come from the same
settings the rest of the suite uses (``.env.test`` via the root conftest), so
a credential or port move breaks every suite the same way instead of this one
differently.
"""

import json
import os
import subprocess
import uuid
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from scripts.migration_runner import legacy_lineage_max
from src.config.settings import settings
from src.utils.validators import MIGRATIONS_DIR

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SQL = REPO_ROOT / "scripts" / "setup_database.sql"
BOOTSTRAP_SQL = REPO_ROOT / "scripts" / "window" / "step0_bootstrap.sql"

#: The last version of the LEGACY lineage — everything numbered below the 3c
#: schema move. **Derived from the move file's own marker, never written down
#: here**: a bound stated as a literal is a second enumeration of the corpus,
#: and it would be right on the day it was typed and silently wrong the first
#: time anyone renumbered anything. Read it, do not replace it with an int.
#:
#: What it is FOR: from 051 on the corpus holds two lineages in one directory,
#: and the suites below guard the legacy one — the schema the running
#: application is built on. Replaying past the boundary renames `public` out
#: from under them, so every legacy-lineage replay passes this bound. The lane
#: that replays *across* the boundary is `test_lineage_lane.py`, deliberately
#: a separate suite rather than a flag on these.
LEGACY_LINEAGE_MAX = legacy_lineage_max(MIGRATIONS_DIR)

#: The seven service roles, split as `02` §7 declares them and the `04` step-0
#: bootstrap provisions them. Held here rather than re-listed per test so the
#: inventory has one home on the test side too.
LOGIN_ROLES = ("svc_ingress", "svc_worker", "svc_migration")
NOLOGIN_ROLES = ("svc_claim", "svc_clock", "svc_maintenance", "svc_membership")
SERVICE_ROLES = LOGIN_ROLES + NOLOGIN_ROLES


def _dsn(database: str) -> str:
    auth = settings.DB_USER
    if settings.DB_PASSWORD:
        auth = f"{settings.DB_USER}:{settings.DB_PASSWORD}"
    return f"postgresql://{auth}@{settings.DB_HOST}:{settings.DB_PORT}/{database}"


@pytest.fixture(scope="session")
def admin_conn():
    """One session-wide connection to the maintenance database for creating
    and dropping scratch databases."""
    conn = psycopg2.connect(_dsn("postgres"))
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    yield conn
    conn.close()


def _create_db(admin_conn, name: str, template: str | None = None) -> str:
    clause = f' TEMPLATE "{template}"' if template else ""
    with admin_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"{clause}')
    return _dsn(name)


def _drop_db(admin_conn, name: str) -> None:
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _scratch(admin_conn, template: str | None = None):
    name = f"runner_test_{uuid.uuid4().hex[:10]}"
    dsn = _create_db(admin_conn, name, template)
    try:
        yield dsn
    finally:
        _drop_db(admin_conn, name)


@pytest.fixture()
def scratch_db(admin_conn):
    """A fresh empty database."""
    yield from _scratch(admin_conn)


@pytest.fixture()
def second_scratch_db(admin_conn):
    """An independent second database for two-schema comparisons."""
    yield from _scratch(admin_conn)


@pytest.fixture(scope="session")
def at49_template(admin_conn):
    """The production-shaped at-49 database (setup + 001–049 hand-applied,
    no ledger), built once per session as a template; tests copy it via
    CREATE DATABASE ... TEMPLATE instead of re-executing the corpus."""
    name = f"runner_tpl_at49_{uuid.uuid4().hex[:8]}"
    dsn = _create_db(admin_conn, name)
    psql_apply(dsn, [SETUP_SQL] + migration_files(49))
    yield name
    _drop_db(admin_conn, name)


@pytest.fixture()
def at49_db(admin_conn, at49_template):
    """A private copy of the at-49 shape, safe to mutate."""
    yield from _scratch(admin_conn, template=at49_template)


# --- the 04 step-0 window bootstrap (F.2.1, #746) ---------------------------
#
# Service roles are CLUSTER-scoped, so they outlive the per-test scratch
# databases every other fixture here relies on. That is not an inconvenience to
# work around — it is why role provisioning cannot be a runner migration in the
# first place (`02` §7-DDL: "ROLES ARE NOT CREATED HERE"). The helpers below
# own the lifecycle explicitly instead of leaning on database teardown.


def actor_lacks_createrole(admin_conn) -> str | None:
    """The reason the current actor cannot run the bootstrap, or None.

    Returned as a reason string rather than a bool so the skip names the
    missing privilege. CI's `postgres:15` service makes `POSTGRES_USER` the
    cluster superuser, so the gate genuinely runs there; a local PostgreSQL
    whose test role was created by hand usually has neither flag, and a silent
    pass would read as coverage this suite did not have.
    """
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user"
        )
        row = cur.fetchone()
    if row and (row[0] or row[1]):
        return None
    return (
        "actor cannot provision roles: needs SUPERUSER or CREATEROLE. "
        "`04` 0.2 declares the bootstrap actor as non-superuser + CREATEROLE; "
        "grant it with: ALTER ROLE <test role> CREATEROLE;"
    )


def drop_service_roles(admin_conn, extra=()) -> None:
    """Remove the seven roles (plus any test-local ones) from the cluster.

    ORDER IS LOAD-BEARING and cost a round of red tests to learn: a privilege
    granted *inside* a database — `GRANT CREATE ON DATABASE`, a schema grant, a
    table grant — is a catalog dependency on the role, so PostgreSQL refuses
    the drop with `DependentObjectsStillExist` while that database is still
    there. Dropping the database first discards the dependencies with it.
    Callers must not hand-roll this order; use `roleless_db`/`bootstrapped_db`,
    whose teardown sequences it correctly.
    """
    with admin_conn.cursor() as cur:
        for role in tuple(extra) + SERVICE_ROLES:
            cur.execute(f'DROP ROLE IF EXISTS "{role}"')


def run_bootstrap(dsn: str) -> None:
    """Apply the step-0 artifact as the current (owner) actor.

    Executed through psycopg2 rather than `psql_apply` so a guard's RAISE
    surfaces as an exception the caller can assert the *message* of — the
    precondition is the point of the artifact, not an incidental failure mode.
    """
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP_SQL.read_text())
    finally:
        conn.close()


@pytest.fixture()
def roleless_db(admin_conn):
    """A fresh database with the seven service roles guaranteed ABSENT.

    Yields ``(dsn, extra_roles)`` — append any test-local role name to
    ``extra_roles`` and teardown drops it in the right order too. Roles are
    cluster-scoped, so "absent" has to be established at setup rather than
    assumed from a fresh database.
    """
    reason = actor_lacks_createrole(admin_conn)
    if reason:
        pytest.skip(reason)
    drop_service_roles(admin_conn)
    extra: list[str] = []
    name = f"runner_test_{uuid.uuid4().hex[:10]}"
    dsn = _create_db(admin_conn, name)
    try:
        yield dsn, extra
    finally:
        _drop_db(admin_conn, name)
        drop_service_roles(admin_conn, extra)


@pytest.fixture()
def bootstrapped_db(roleless_db):
    """A fresh database with the seven service roles provisioned.

    What F.2.2 onward needs: a policy naming `svc_ingress` is uncreatable until
    the role exists, so every policy-carrying table PR replays through here.

    Built on `roleless_db` so the capability skip and the drop-database-then-
    drop-roles teardown order have exactly one home — the same reason the
    scratch fixtures above share `_scratch`.
    """
    dsn, _ = roleless_db
    run_bootstrap(dsn)
    return dsn


def migration_files(limit: int):
    """The real corpus up to ``limit``, selected by the runner's own
    discovery so the gate cannot drift from production's file set."""
    from scripts.migration_runner import discover_migrations

    files = [m.path for m in discover_migrations(MIGRATIONS_DIR, limit)]
    assert files, "no migration files found"
    return files


def psql_apply(dsn: str, files) -> None:
    """Hand-apply SQL files the way production history was actually built."""
    cmd = ["psql", dsn, "-q", "-v", "ON_ERROR_STOP=1"]
    for f in files:
        cmd += ["-f", str(f)]
    env = dict(os.environ, PGOPTIONS="-c client_min_messages=warning")
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"hand-apply failed: {result.stderr[-2000:]}"


# --- plain helpers shared across the runner suites ---


def write_migration(migrations_dir, version, body, name="m"):
    path = migrations_dir / f"{version:03d}_{name}.sql"
    path.write_text(body)
    return path


def fetch_ledger(dsn):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT version, checksum, applied_by, execution_ms, status"
            " FROM runner.schema_migrations ORDER BY version"
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def table_exists(dsn, table, schema="public"):
    row = fetch_one(
        dsn,
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = %s AND table_name = %s)",
        (schema, table),
    )
    return row[0]


def execute(dsn, sql, params=None):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
    conn.close()


def fetch_one(dsn, sql, params=None):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.close()
    return row


def probe_table(name):
    return (
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
        f" WHERE table_schema = 'public' AND table_name = '{name}')"
    )


def write_manifest(tmp_path, required_through, entries):
    path = tmp_path / "adoption_manifest.json"
    path.write_text(
        json.dumps({"required_through": required_through, "entries": entries})
    )
    return path
