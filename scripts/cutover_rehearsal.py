"""Restore an owner-supplied dump and require zero catalog drift after cutover.

Both administrator DSNs must name independent disposable PostgreSQL clusters.
The restore cluster's owners and roles must already match the dump; the code
cluster starts without service roles. Only uniquely created databases and the
service roles created on the code cluster are removed on exit. A supplied
SCHEMA_DRIFT_DSN is only ever read, never restored, adopted, or migrated.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn, parse_dsn

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import migration_runner
from scripts.schema_parity import print_comparison, schema_signature

ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROLES = {
    "svc_ingress",
    "svc_worker",
    "svc_migration",
    "svc_claim",
    "svc_clock",
    "svc_maintenance",
    "svc_membership",
}


def require_parity(restored_dsn, code_dsn):
    """Use the runner's read-only parity command and refuse every difference."""
    result = migration_runner.main(
        ["--database-url", restored_dsn, "parity", "--against", code_dsn]
    )
    if result:
        raise RuntimeError(
            "catalog parity failed; zero differences are required before cutover"
        )


def require_restore_fidelity(restored_dsn, source_catalog):
    """Refuse a restore that lost database metadata, roles, or object grants."""
    source = json.loads(Path(source_catalog).read_text(encoding="utf-8"))
    if print_comparison(source, schema_signature(restored_dsn, read_only=True)):
        raise RuntimeError(
            "dump restore fidelity failed; preserve source database metadata, owners, and grants"
        )


def preserve_public_legacy_ledgers(dsn):
    """Preserve public history tables without bypassing runner ledger conversion.

    Counts and version numbers in old ledgers are not adoption evidence. All
    renames share one transaction, and an existing history destination refuses
    the conversion instead of overwriting it.
    """
    candidates = (
        ("public", "schema_migrations", "legacy_public_schema_migrations"),
        ("public", "schema_version", "legacy_schema_version"),
    )
    preserved = []
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS runner")
        for schema, source, destination in candidates:
            cur.execute("SELECT to_regclass(%s)", (f"{schema}.{source}",))
            if cur.fetchone()[0] is None:
                continue
            cur.execute("SELECT to_regclass(%s)", (f"runner.{destination}",))
            if cur.fetchone()[0] is not None:
                raise RuntimeError(
                    f"runner.{destination} already exists; legacy history was not overwritten"
                )
            cur.execute(
                sql.SQL("ALTER TABLE {}.{} RENAME TO {}").format(
                    sql.Identifier(schema),
                    sql.Identifier(source),
                    sql.Identifier(destination),
                )
            )
            if schema != "runner":
                cur.execute(
                    sql.SQL("ALTER TABLE {}.{} SET SCHEMA runner").format(
                        sql.Identifier(schema), sql.Identifier(destination)
                    )
                )
            preserved.append(f"runner.{destination}")
    return preserved


def require_cluster_separation(restore_admin, code_admin):
    identifiers = []
    for admin in (restore_admin, code_admin):
        with admin.cursor() as cur:
            cur.execute("SELECT system_identifier FROM pg_catalog.pg_control_system()")
            identifiers.append(cur.fetchone()[0])
    if identifiers[0] == identifiers[1]:
        raise ValueError(
            "restore and code databases must use independent PostgreSQL clusters"
        )


def _service_roles(admin):
    with admin.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_catalog.pg_roles WHERE left(rolname, 4) = 'svc_'"
        )
        return {row[0] for row in cur.fetchall()}


def _create_restore_database(admin, name, source):
    database = source["catalog"]["database"]["<database>"]
    properties = database["properties"]
    if not properties["datallowconn"] or properties["datistemplate"]:
        raise ValueError("the source must be a connectable, non-template database")
    providers = {"c": "libc", "i": "icu", "b": "builtin"}
    provider = providers.get(properties["datlocprovider"])
    if provider is None:
        raise ValueError("unsupported source locale provider")
    with admin.cursor() as cur:
        cur.execute("SELECT pg_encoding_to_char(%s)", (properties["encoding"],))
        options = {
            "ENCODING": cur.fetchone()[0],
            "LC_COLLATE": properties["datcollate"],
            "LC_CTYPE": properties["datctype"],
            "LOCALE_PROVIDER": provider,
            "TABLESPACE": database["tablespace"],
            "CONNECTION LIMIT": properties["datconnlimit"],
        }
        if provider in ("icu", "builtin"):
            options["ICU_LOCALE" if provider == "icu" else "BUILTIN_LOCALE"] = (
                properties["datlocale"]
            )
        if properties.get("daticurules") is not None:
            options["ICU_RULES"] = properties["daticurules"]
        cur.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0 ").format(
                sql.Identifier(name)
            )
            + sql.SQL(" ").join(
                sql.SQL("{} {}").format(sql.SQL(key), sql.Literal(value))
                for key, value in options.items()
            )
        )


def restore_database_metadata(admin, name, source):
    """Restore source ACLs/settings only on the newly created scratch database."""
    with admin:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s",
                (name,),
            )
            owner = cur.fetchone()[0]

            def role(name):
                return (
                    sql.SQL("PUBLIC")
                    if name == "PUBLIC"
                    else sql.Identifier(owner if name == "<owner>" else name)
                )

            acl = source["catalog"]["database"]["<database>"]["acl"]
            acl = acl + source.get("reported", {}).get("outside_roles", {}).get(
                "database.<database>", []
            )
            cur.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC, {}").format(
                    sql.Identifier(name), sql.Identifier(owner)
                )
            )
            privileges = {
                value: sql.SQL(value) for value in ("CONNECT", "CREATE", "TEMPORARY")
            }
            for entry in acl:
                if entry["privilege"] not in privileges:
                    raise ValueError("unsupported database privilege in source catalog")
                cur.execute(
                    sql.SQL("GRANT {} ON DATABASE {} TO {}{}").format(
                        privileges[entry["privilege"]],
                        sql.Identifier(name),
                        role(entry["grantee"]),
                        sql.SQL(" WITH GRANT OPTION")
                        if entry["grantable"]
                        else sql.SQL(""),
                    )
                )
            settings = list(source["catalog"]["settings"].values())
            settings += list(
                source.get("reported", {})
                .get("outside_roles", {})
                .get("settings", {})
                .values()
            )
            for setting in settings:
                if setting["database"] != "<database>":
                    continue
                for config in setting["config"]:
                    key, separator, value = config.partition("=")
                    if not separator:
                        raise ValueError("invalid source database setting")
                    prefix = (
                        sql.SQL("ALTER DATABASE {}").format(sql.Identifier(name))
                        if setting["role"] == "PUBLIC"
                        else sql.SQL("ALTER ROLE {} IN DATABASE {}").format(
                            role(setting["role"]), sql.Identifier(name)
                        )
                    )
                    cur.execute(
                        "SELECT pg_catalog.set_config(%s, %s, true)", (key, value)
                    )
                    cur.execute(
                        prefix
                        + sql.SQL(" SET {} FROM CURRENT").format(sql.Identifier(key))
                    )


def _restore_dump(dump, dsn):
    # A service file keeps credentials out of the child command's arguments.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="cutover-restore-") as temporary:
        service = Path(temporary) / "pg_service.conf"
        service.write_text(
            "[cutover_restore]\n"
            + "".join(f"{key}={value}\n" for key, value in parse_dsn(dsn).items()),
            encoding="utf-8",
        )
        service.chmod(0o600)
        result = subprocess.run(
            [
                "pg_restore",
                "--exit-on-error",
                "--dbname=service=cutover_restore",
                str(dump),
            ],
            env={**os.environ, "PGSERVICEFILE": str(service)},
            capture_output=True,
            text=True,
        )
    if result.returncode:
        raise RuntimeError(
            f"pg_restore failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def run(
    dump,
    restore_admin_url,
    code_admin_url,
    *,
    source_catalog,
    confirm_archive_backup=False,
    drift_dsn=None,
):
    """Restore, convert, adopt, manually migrate, validate, and compare."""
    from sqlalchemy import create_engine

    from src.models.target.ddl import build

    if not confirm_archive_backup:
        raise ValueError(
            "confirm the snapshot export or backup with --confirm-archive-backup"
        )
    if not Path(dump).is_file():
        raise ValueError("--dump must name an existing custom-format pg_dump file")
    if not Path(source_catalog).is_file():
        raise ValueError(
            "--source-catalog must name the source's read-only catalog extraction"
        )
    source = json.loads(Path(source_catalog).read_text(encoding="utf-8"))
    administrators, created = [], []
    code_roles_owned = False
    try:
        for dsn in (restore_admin_url, code_admin_url):
            admin = psycopg2.connect(dsn)
            admin.autocommit = True
            administrators.append(admin)
        require_cluster_separation(*administrators)
        missing = SERVICE_ROLES - _service_roles(administrators[0])
        if missing:
            raise ValueError(
                "prepare the dump's roles on the disposable restore cluster first: "
                + ", ".join(sorted(missing))
            )
        if _service_roles(administrators[1]):
            raise ValueError(
                "the disposable code cluster must start without svc_* roles"
            )
        code_roles_owned = True
        dsns = []
        for admin, base, kind in zip(
            administrators, (restore_admin_url, code_admin_url), ("restore", "code")
        ):
            name = f"cutover_{kind}_{uuid.uuid4().hex}"
            if kind == "restore":
                _create_restore_database(admin, name, source)
            else:
                with admin.cursor() as cur:
                    cur.execute(
                        sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                            sql.Identifier(name)
                        )
                    )
            created.append((admin, name))
            dsns.append(make_dsn(base, dbname=name))
        restored, expected = dsns
        _restore_dump(dump, restored)
        restore_database_metadata(administrators[0], created[0][1], source)
        print("checking restored dump fidelity before ledger conversion")
        require_restore_fidelity(restored, source_catalog)
        migrations = ROOT / "scripts/migrations"
        engine = create_engine(
            "postgresql+psycopg2://", connect_args=parse_dsn(expected)
        )
        try:
            with engine.connect() as conn:
                build(conn)
        finally:
            engine.dispose()
        migration_runner.adopt(
            expected, migrations, migrations / "adoption_manifest.json"
        )
        migration_runner.convert_ledger(
            restored,
            migrations,
            migrations / "adoption_manifest.json",
            confirm_legacy_ledger=True,
        )
        print("converted legacy runner ledger to runner.schema_migrations_legacy")
        preserved = preserve_public_legacy_ledgers(restored)
        print("preserved public history: " + (", ".join(preserved) or "none present"))
        pending = migration_runner.apply_pending(restored, migrations)
        if pending.applied or [m.version for m in pending.owed] != [1, 2]:
            raise RuntimeError(
                "pre-deploy must leave exactly 001 and 002 owed and apply neither"
            )
        print("pre-deploy: 0 applied; 001 and 002 owed (manual)")
        print("initial parity (diagnostic): pending owner-applied changes may differ")
        migration_runner.main(
            ["--database-url", restored, "parity", "--against", expected]
        )
        migration_runner.apply_manual(restored, migrations, 1)
        print(
            "applied owner step 001; validating constraints in a separate transaction"
        )
        # The first transaction has committed before validation scans the table.
        with psycopg2.connect(restored) as conn, conn.cursor() as cur:
            cur.execute(
                (ROOT / "scripts/validate_catalog_constraints.sql").read_text(
                    encoding="utf-8"
                )
            )
        migration_runner.apply_manual(
            restored, migrations, 2, confirm_archive_backup=True
        )
        print("applied owner step 002 after confirmed snapshot backup")
        require_parity(restored, expected)
        if drift_dsn:
            print("checking SCHEMA_DRIFT_DSN in read-only transactions")
            require_parity(drift_dsn, expected)
        print("cutover rehearsal passed: zero catalog differences")
        return 0
    finally:
        failures = []
        for admin, name in reversed(created):
            try:
                with admin.cursor() as cur:
                    cur.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                            sql.Identifier(name)
                        )
                    )
            except psycopg2.Error as exc:
                failures.append(str(exc))
        if code_roles_owned:
            try:
                with administrators[1].cursor() as cur:
                    for role in sorted(_service_roles(administrators[1])):
                        cur.execute(
                            sql.SQL("DROP ROLE {}").format(sql.Identifier(role))
                        )
            except psycopg2.Error as exc:
                failures.append(str(exc))
        for admin in administrators:
            admin.close()
        if failures:
            raise RuntimeError("rehearsal cleanup failed: " + "; ".join(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path)
    parser.add_argument("--source-catalog", type=Path)
    parser.add_argument(
        "--restore-admin-url", default=os.environ.get("REHEARSAL_RESTORE_ADMIN_DSN")
    )
    parser.add_argument(
        "--code-admin-url", default=os.environ.get("REHEARSAL_CODE_ADMIN_DSN")
    )
    parser.add_argument("--confirm-disposable-clusters", action="store_true")
    parser.add_argument("--confirm-archive-backup", action="store_true")
    parser.add_argument(
        "--check-drift",
        action="store_true",
        help="only compare SCHEMA_DRIFT_DSN read-only against --against",
    )
    parser.add_argument(
        "--against", help="a database already built from schema code, for --check-drift"
    )
    args = parser.parse_args(argv)
    drift = os.environ.get("SCHEMA_DRIFT_DSN", "").strip()
    try:
        if os.environ.get("REQUIRE_SCHEMA_DRIFT_CHECK") == "1" and not drift:
            raise ValueError(
                "REQUIRE_SCHEMA_DRIFT_CHECK=1 but SCHEMA_DRIFT_DSN is empty"
            )
        if args.check_drift:
            if not drift or not args.against:
                raise ValueError(
                    "--check-drift requires SCHEMA_DRIFT_DSN and --against"
                )
            require_parity(drift, args.against)
            return 0
        if not (
            args.dump
            and args.source_catalog
            and args.restore_admin_url
            and args.code_admin_url
            and args.confirm_disposable_clusters
        ):
            raise ValueError(
                "supply --dump, --source-catalog, both disposable administrator DSNs, and --confirm-disposable-clusters"
            )
        return run(
            args.dump,
            args.restore_admin_url,
            args.code_admin_url,
            source_catalog=args.source_catalog,
            confirm_archive_backup=args.confirm_archive_backup,
            drift_dsn=drift,
        )
    except (
        ValueError,
        RuntimeError,
        OSError,
        psycopg2.Error,
        migration_runner.MigrationRunnerError,
    ) as exc:
        print(f"cutover rehearsal failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
