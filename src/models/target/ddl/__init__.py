"""The database schema as code.

The SQLAlchemy models in `src.models.target` declare the tables, columns,
defaults, constraints and indexes. The SQL files in this package declare what
the models cannot: the service roles and their memberships, schema settings,
functions, reference rows, triggers, row-level security and its policies,
grants, and function ownership. `build()` creates all of it on an empty
database, in order, in one transaction.

Descriptions live in these files as `--` lines, never as catalog comments, so
what the database stores is the code alone.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.models.target import TargetBase

HERE = Path(__file__).parent

#: The files that run before the tables exist, then the models' tables, then the
#: files that run after. The numeric prefixes give the order of application.
BEFORE_TABLES = ("01_roles.sql", "02_schemas.sql", "03_table_functions.sql")
AFTER_TABLES = (
    "04_functions.sql",
    "05_reference_rows.sql",
    "06_triggers.sql",
    "07_row_security.sql",
    "08_ownership.sql",
    "09_grants.sql",
)


class NotEmpty(RuntimeError):
    """The database already holds a schema; `build()` changed nothing."""


def contents(conn: Connection) -> list[str]:
    """What makes a database not empty for `build()`: a relation or a function
    in `public`, or the migration ledger. Empty means an empty list."""
    found = []
    relations = conn.execute(
        text(
            "SELECT count(*) FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public'"
        )
    ).scalar_one()
    if relations:
        found.append(f"{relations} relations in public")
    functions = conn.execute(
        text(
            "SELECT count(*) FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'public'"
        )
    ).scalar_one()
    if functions:
        found.append(f"{functions} functions in public")
    if conn.execute(text("SELECT to_regclass('runner.schema_migrations')")).scalar():
        found.append("the migration ledger")
    return found


def build(conn: Connection) -> None:
    """Create the whole schema on an empty database, in one transaction.

    `conn` must have no transaction in progress: the transaction is this
    function's own, so a failure anywhere leaves the database as it was. A
    database holding anything `contents` names is refused with `NotEmpty`,
    and nothing is changed. The connecting role needs CREATEROLE (the service
    roles are created if missing) and must own the database.
    """
    with conn.begin():
        found = contents(conn)
        if found:
            raise NotEmpty("the database is not empty: " + ", ".join(found))
        for name in BEFORE_TABLES:
            _run_file(conn, name)
        TargetBase.metadata.create_all(conn)
        for name in AFTER_TABLES:
            _run_file(conn, name)


def _run_file(conn: Connection, name: str) -> None:
    # The DBAPI cursor sends the file as one query with no parameters, so a
    # `%` in it is SQL, never a placeholder.
    sql = (HERE / name).read_text(encoding="utf-8")
    with conn.connection.driver_connection.cursor() as cursor:
        cursor.execute(sql)
