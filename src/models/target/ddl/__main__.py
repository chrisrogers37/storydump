"""Build the schema on a database: ``python -m src.models.target.ddl COMMAND``.

``build`` creates the schema on an empty database and refuses any other.
``init`` is what ``make init-db`` runs first: it builds an empty database,
leaves a built one (the ledger records version 0) for the migration runner,
and refuses anything else. The runner's ``adopt`` then records the initial
marker on a database this built, and its ``apply`` runs the newer files.

The database is addressed by ``DATABASE_URL`` or ``--database-url``, the
runner's own convention; the role needs CREATEROLE and must own the database.
A URL that cannot be read is refused by that name and never quoted
(``src.config.db_url``), and exits 2 as no URL does.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ArgumentError

from src.config.db_url import DATABASE_URL_NAME, UnreadableUrl, is_ambiguous_url
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


def _engine(url: str):
    """The engine on *url*, or ``UnreadableUrl`` for one that carries a raw
    ``@`` besides the one before the host (``db_url.is_ambiguous_url``), part
    of whose password SQLAlchemy would read as the rest of the URL and libpq
    could quote; one SQLAlchemy cannot parse, whose error quotes what it could
    not read; and one it reads into a database name carrying an ``@``, as
    after a raw ``/`` in the user, where the name carries the password."""
    try:
        engine = None if is_ambiguous_url(url) else create_engine(url)
    except (ArgumentError, ValueError):
        engine = None
    if engine is None or "@" in (engine.url.database or ""):
        # Past the `except`: SQLAlchemy's error is not even this one's context.
        raise UnreadableUrl(DATABASE_URL_NAME)
    return engine


def run(command: str, url: str) -> int:
    """*command* on the database *url* names, answered as `main` exits;
    ``UnreadableUrl`` for a URL that cannot be read."""
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            if command == "init":
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
    try:
        return run(args.command, args.database_url)
    except UnreadableUrl as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
