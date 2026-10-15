"""Build disposable code and migration databases and compare their catalogs.

The pinned commit is the schema-code base. Only migration files absent at that
commit are replayed; edits or removals to its migration files are refused.
DATABASE_URL names an administrator on a disposable local PostgreSQL cluster.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.migration_runner import (
    MigrationRunnerError,
    adopt,
    apply_pending,
    discover_migrations,
)
from scripts.schema_parity import print_comparison, schema_signature

ROOT = Path(__file__).resolve().parents[1]
BASE_FILE = ROOT / "scripts/schema_consistency_base.txt"


def base_revision(root, pin):
    revision = pin.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("schema consistency base must be a full commit SHA")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, "HEAD"], cwd=root, check=True
    )
    return revision


def check_migration_history(base_dir, current_dir):
    """A base migration is immutable; its later tail is the replay input."""
    base = {m.version: m for m in discover_migrations(base_dir)}
    current = {m.version: m for m in discover_migrations(current_dir)}
    for version, migration in base.items():
        if (
            version not in current
            or current[version].checksum != migration.checksum
            or current[version].path.name != migration.path.name
        ):
            raise ValueError(f"base migration {version:03d} is removed or changed")
    return sorted(set(current) - set(base))


def run(database_url, *, root=ROOT, pin=BASE_FILE, output_dir=None):
    revision = base_revision(root, pin)
    archive = subprocess.check_output(["git", "archive", revision], cwd=root)
    names = ["parity_code_" + uuid.uuid4().hex, "parity_migrations_" + uuid.uuid4().hex]
    admin = psycopg2.connect(database_url)
    admin.autocommit = True
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix="schema-consistency-") as temporary:
            base_root = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(base_root)
            later = check_migration_history(
                base_root / "scripts/migrations", root / "scripts/migrations"
            )
            with admin.cursor() as cursor:
                for name in names:
                    cursor.execute(
                        sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                            sql.Identifier(name)
                        )
                    )
                    created.append(name)
            dsns = [make_dsn(database_url, dbname=name) for name in names]
            for directory, dsn in ((root, dsns[0]), (base_root, dsns[1])):
                # The module resolves from cwd, so the archived code really builds
                # the base side even with an editable installation on the host.
                env = {**os.environ, "DATABASE_URL": dsn}
                env.pop("PYTHONPATH", None)
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import os; from sqlalchemy import create_engine; "
                        "from psycopg2.extensions import parse_dsn; "
                        "from src.models.target.ddl import build; "
                        "engine=create_engine('postgresql+psycopg2://', "
                        "connect_args=parse_dsn(os.environ['DATABASE_URL'])); "
                        "\nwith engine.connect() as conn: build(conn)"
                        "\nengine.dispose()",
                    ],
                    cwd=directory,
                    env=env,
                    check=True,
                )
                adopt(
                    dsn,
                    directory / "scripts/migrations",
                    directory / "scripts/migrations/adoption_manifest.json",
                    max_version=0 if directory == root else None,
                )
            replay = apply_pending(dsns[1], root / "scripts/migrations")
            if replay.owed:
                raise ValueError("schema consistency cannot skip manual migrations")
            if sorted(m.version for m in replay.applied) != later:
                raise ValueError("the runner did not apply exactly the migration tail")
            first, second = [schema_signature(dsn, read_only=True) for dsn in dsns]
            if output_dir:
                output_dir.mkdir(parents=True, exist_ok=True)
                for name, snapshot in (("code", first), ("migrations", second)):
                    (output_dir / f"{name}.json").write_text(
                        json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
            print(f"base {revision}; {len(replay.applied)} migration(s) replayed")
            return print_comparison(first, second)
    finally:
        with admin.cursor() as cursor:
            for name in reversed(created):
                cursor.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
        admin.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("set DATABASE_URL or pass --database-url")
    try:
        return run(args.database_url, output_dir=args.out)
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        psycopg2.Error,
        MigrationRunnerError,
    ) as exc:
        print(f"schema consistency failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
