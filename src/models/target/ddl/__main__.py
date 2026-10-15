"""Build the schema on a database: ``python -m src.models.target.ddl COMMAND``.

``build`` creates the schema on an empty database and refuses any other.
``init`` is what ``make init-db`` runs first: it builds an empty database,
leaves a built one (the ledger records version 0) for the migration runner,
and refuses anything else. The runner's ``adopt`` then records the initial
marker on a database this built, and its ``apply`` runs the newer files.

The database is addressed by ``DATABASE_URL`` or ``--database-url``, the
runner's own convention; the role needs CREATEROLE and must own the database.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text

from src.models.target.ddl import NotEmpty, build, contents


def _built(conn) -> bool:
    if not conn.execute(
        text("SELECT to_regclass('runner.schema_migrations')")
    ).scalar():
        return False
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM runner.schema_migrations WHERE version = 0)"
            )
        ).scalar()
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.models.target.ddl")
    parser.add_argument("command", choices=("build", "init"))
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="connection string; defaults to DATABASE_URL",
    )
    args = parser.parse_args(argv)
    if not args.database_url:
        print("no database: set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    engine = create_engine(args.database_url)
    try:
        with engine.connect() as conn:
            if args.command == "init":
                if _built(conn):
                    print(
                        "built already: the ledger records version 0; nothing to build"
                    )
                    return 0
                found = contents(conn)
                conn.rollback()
                if found:
                    print(
                        "refused: the database is neither empty nor built from code: "
                        + ", ".join(found),
                        file=sys.stderr,
                    )
                    return 2
            try:
                build(conn)
            except NotEmpty as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 2
            print(
                "built: the schema is in place; record the initial marker with the runner's adopt"
            )
            return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
