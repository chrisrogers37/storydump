"""M.3 step 3f, proven the way it will run: every legacy table copied into
`archive` by one runner file (078), owned by `svc_maintenance`, with the row
counts the file's own postconditions assert (the tear-out, phase 03; fork F4).

Three layers. A UNIT PIN of the file itself — its thirty-two statements and
thirty-two postconditions generated from `LEGACY_TABLES` and compared exactly,
so a copy made `WITH NO DATA`, a dropped `OWNER TO`, a stray `GRANT`, a swapped
row-count or a mixed date is red without a database. Then two DB worlds,
because production's actor and the runbook's differ: production's runner
connects as the database owner (`railway.toml`'s `DATABASE_URL`; the tear-out's
probe measured `current_user = neondb_owner` for every ledger row since the
runner was armed, holding `svc_maintenance` with SET), so the first world
applies the corpus AS THE OWNER ACTOR after the window bootstrap — the shape
078 meets on the next deploy. The second applies it as `svc_migration`, the
actor the M.3 runbook named for 3b–3g, after the bootstrap and `runner adopt`
as 3a ran. (The lineage lane itself, `run_lane`, applies the corpus as the test
admin; the privilege proof is this file's.) Both actors must be able to hand a
snapshot to `svc_maintenance`; a file that only works for one is a predeploy
failure waiting for the other.

The DB worlds seed what they can cheaply — the hand-made table with one row;
`schema_version` holds the lineage's own INSERTs — so two row-count
postconditions compare something other than 0 = 0 at runtime; the other
fourteen are 0 = 0 there, and the unit pin is what closes that hole.
"""

from __future__ import annotations

import pytest
from scripts.migration_runner import (
    MIGRATIONS_DIR,
    adopt,
    apply_pending,
    discover_migrations,
)
from tests.scripts.conftest import (
    BY_HAND_SQL,
    LEGACY_STANDUP,
    SETUP_SQL,
    as_user,
    execute,
    fetch_all,
    fetch_ledger,
    fetch_one,
    migration_files,
    psql_apply,
    run_bootstrap,
    window_actor,
)
from tests.scripts.legacy_inventory import HAND_MADE, LEGACY_TABLES

SNAPSHOT_VERSION = 78


def _snapshot_file():
    [m] = [
        m for m in discover_migrations(MIGRATIONS_DIR) if m.version == SNAPSHOT_VERSION
    ]
    return m


def _date_suffix() -> str:
    """The `<ymd>` the file names its snapshots with — read off the file, so
    the test cannot pin a date the file does not carry."""
    import re

    found = set(re.findall(r"_pre_cutover_(\d{8})", _snapshot_file().sql))
    assert len(found) == 1, f"078 names more than one date: {sorted(found)}"
    return found.pop()


def _stand_up_to_077(dsn: str) -> None:
    """The legacy world as production has it, then the corpus up to 077."""
    psql_apply(dsn, [SETUP_SQL, BY_HAND_SQL])
    apply_pending(dsn, MIGRATIONS_DIR, SNAPSHOT_VERSION - 1)


def _seed(dsn: str) -> None:
    for table in HAND_MADE:
        execute(
            dsn,
            f"INSERT INTO legacy.{table} (id, status, archived_at)"
            " VALUES (gen_random_uuid(), 'posted', now())",
        )


def _snapshots(dsn: str) -> dict:
    rows = fetch_all(
        dsn,
        "SELECT c.relname, r.rolname FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " JOIN pg_roles r ON r.oid = c.relowner"
        " WHERE n.nspname = 'archive' AND c.relkind = 'r'"
        "   AND c.relname LIKE '%\\_pre\\_cutover\\_%' ORDER BY 1",
    )
    return {row["relname"]: row["rolname"] for row in rows}


def _count(dsn: str, schema: str, table: str) -> int:
    return fetch_one(dsn, f'SELECT count(*) FROM {schema}."{table}"')[0]


def _assert_snapshotted(dsn: str) -> None:
    ymd = _date_suffix()
    snapshots = _snapshots(dsn)
    expected = {f"{t}_pre_cutover_{ymd}" for t in LEGACY_TABLES}
    assert set(snapshots) == expected, (
        f"missing: {sorted(expected - set(snapshots))},"
        f" unexpected: {sorted(set(snapshots) - expected)}"
    )
    assert set(snapshots.values()) == {"svc_maintenance"}, snapshots
    for table in LEGACY_TABLES:
        assert _count(dsn, "archive", f"{table}_pre_cutover_{ymd}") == _count(
            dsn, "legacy", table
        ), table
    # the seeded tables carried rows, so two of the equalities above were not
    # 0 = 0 (the unit pin covers the other fourteen): the lineage's own INSERTs
    # (measured: 48 rows on this corpus, which is closed) and the hand-made row
    assert _count(dsn, "legacy", "schema_version") == 48
    for table in HAND_MADE:
        assert _count(dsn, "legacy", table) == 1
    # a snapshot is not a table in use: svc_ingress cannot read any of them
    for table in LEGACY_TABLES:
        denied = fetch_one(
            dsn,
            "SELECT has_table_privilege('svc_ingress',"
            f" 'archive.{table}_pre_cutover_{ymd}', 'SELECT')",
        )[0]
        assert denied is False, table


class TestTheSnapshotFile:
    """The unit pin: the file IS the sixteen-table copy, statement for
    statement, generated here from the inventory — no database needed."""

    def test_it_is_the_seventy_eighth_file_and_says_it_is_not_advertised(self):
        m = _snapshot_file()
        assert m.path.name == "078_legacy_snapshots_pre_cutover.sql"
        assert m.unadvertised is True
        assert m.execution_mode == "wrapped", (
            "one file, one transaction: no partial archive can survive a failure"
        )

    def test_the_file_is_exactly_the_sixteen_copies_in_inventory_order(self):
        from scripts.advertised_ddl import normalize_statements

        ymd = _date_suffix()
        expected = []
        for t in LEGACY_TABLES:
            snap = f"archive.{t}_pre_cutover_{ymd}"
            expected.append(f"CREATE TABLE {snap} AS TABLE legacy.{t}")
            expected.append(f"ALTER TABLE {snap} OWNER TO svc_maintenance")
        assert normalize_statements(_snapshot_file().sql) == expected

    def test_the_postconditions_are_exactly_two_per_table(self):
        ymd = _date_suffix()
        expected = []
        for t in LEGACY_TABLES:
            snap = f"archive.{t}_pre_cutover_{ymd}"
            expected.append(f"SELECT to_regclass('{snap}') IS NOT NULL")
            expected.append(
                f"SELECT (SELECT count(*) FROM {snap}) = (SELECT count(*) FROM legacy.{t})"
            )
        assert list(_snapshot_file().postconditions) == expected


@pytest.mark.integration
@pytest.mark.slow
class TestAsTheOwnerActor:
    """Production's shape: the bootstrap ran (the probe measured its
    memberships), and the runner connects as the database owner."""

    def test_the_owner_snapshots_every_table_hands_them_to_maintenance_and_the_next_apply_is_a_no_op(
        self, admin_conn, owner_actor, owner_window_db
    ):
        as_owner = as_user(owner_window_db, owner_actor)
        run_bootstrap(admin_conn, as_owner)
        _stand_up_to_077(as_owner)
        _seed(as_owner)
        apply_pending(as_owner, MIGRATIONS_DIR)
        assert [row[0] for row in fetch_ledger(as_owner)][-1] == SNAPSHOT_VERSION
        _assert_snapshotted(as_owner)
        # the second deploy: a no-op, the snapshots untouched
        before = _snapshots(as_owner)
        report = apply_pending(as_owner, MIGRATIONS_DIR)
        assert report.applied == []
        assert _snapshots(as_owner) == before


@pytest.mark.integration
@pytest.mark.slow
class TestAsTheWindowActor:
    """The M.3 runbook's shape, faithfully: production history through 049 was
    built by hand as the owner; the window bootstrap ran over those tables
    (its `GRANT SELECT ON ALL TABLES IN SCHEMA public TO svc_migration` covers
    what exists at that moment — which is why the seed comes FIRST); `runner
    adopt` entered the ledger; and the window's files, 050 onward, ran as
    `svc_migration`. 078 as that actor needs the bootstrap's SELECT on the
    legacy tables and its membership of `svc_maintenance` — both transient,
    both revoked by the stand-down phase 04 ships."""

    def test_svc_migration_snapshots_every_table_too(
        self, admin_conn, owner_actor, owner_window_db
    ):
        as_owner = as_user(owner_window_db, owner_actor)
        psql_apply(as_owner, LEGACY_STANDUP + migration_files(49) + [BY_HAND_SQL])
        as_svc = window_actor(owner_window_db, owner_actor, admin_conn)
        # the ledger is the window actor's to create (the bootstrap grants it
        # CREATE on the database): adopt as svc_migration, like 3a
        adopt(as_svc, MIGRATIONS_DIR, MIGRATIONS_DIR / "adoption_manifest.json", 49)
        apply_pending(as_svc, MIGRATIONS_DIR, SNAPSHOT_VERSION - 1)
        _seed(as_owner)
        apply_pending(as_svc, MIGRATIONS_DIR)
        assert [row[0] for row in fetch_ledger(as_owner)][-1] == SNAPSHOT_VERSION
        _assert_snapshotted(as_owner)


@pytest.mark.integration
@pytest.mark.slow
class TestTheRefusals:
    def test_a_missing_legacy_table_fails_the_file_and_leaves_no_partial_archive(
        self, admin_conn, owner_actor, owner_window_db
    ):
        """One file, one transaction: an inventory error in production must
        abort 078 whole — never a half-set of snapshots the next deploy then
        refuses to re-create."""
        from scripts.migration_runner import MigrationRunnerError

        as_owner = as_user(owner_window_db, owner_actor)
        run_bootstrap(admin_conn, as_owner)
        _stand_up_to_077(as_owner)
        execute(as_owner, "DROP TABLE legacy.users CASCADE")
        with pytest.raises(MigrationRunnerError, match="078"):
            apply_pending(as_owner, MIGRATIONS_DIR)
        assert _snapshots(as_owner) == {}
        assert [row[0] for row in fetch_ledger(as_owner)][-1] == SNAPSHOT_VERSION - 1
