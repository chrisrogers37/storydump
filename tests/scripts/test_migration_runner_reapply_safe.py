"""``-- runner:reapply-safe`` — an idempotent data migration whose applied
state is undecidable in place (048's class): adopt may leave it pending below
an adopted head without calling the chain incoherent, and apply may re-run it
below the head. Files without the marker keep the strict refusals.
"""

import pytest

from scripts.migration_runner import (
    MigrationRunnerError,
    adopt,
    apply_pending,
    discover_migrations,
)
from tests.scripts.conftest import fetch_ledger, write_migration
from tests.scripts.conftest import execute, probe_table, write_manifest

pytestmark = pytest.mark.integration


@pytest.fixture()
def tree_with_reapply_safe_gap(tmp_path):
    """001 DDL, 002 reapply-safe data, 003 DDL — the 048 shape."""
    write_migration(tmp_path, 1, "CREATE TABLE rs_one (id INT);")
    write_migration(
        tmp_path,
        2,
        "-- runner:reapply-safe\n"
        "CREATE TABLE IF NOT EXISTS rs_data (id INT);\n"
        "INSERT INTO rs_data VALUES (1) ON CONFLICT DO NOTHING;",
        name="data_backfill",
    )
    write_migration(tmp_path, 3, "CREATE TABLE rs_three (id INT);")
    return tmp_path


def manifest_with_false_two(tmp_path):
    return write_manifest(
        tmp_path,
        1,
        [
            {"version": 1, "probe": probe_table("rs_one")},
            # Undecidable-in-place: the probe deliberately reads false.
            {"version": 2, "probe": "SELECT false"},
            {"version": 3, "probe": probe_table("rs_three")},
        ],
    )


class TestMarkerParsing:
    def test_marker_is_parsed(self, tmp_path):
        write_migration(tmp_path, 1, "-- runner:reapply-safe\nSELECT 1;", name="m")
        write_migration(tmp_path, 2, "SELECT 1;", name="n")

        migrations = discover_migrations(tmp_path)

        assert migrations[0].reapply_safe is True
        assert migrations[1].reapply_safe is False


class TestAdoptExemption:
    def test_reapply_safe_false_below_head_is_pending_not_incoherent(
        self, scratch_db, tree_with_reapply_safe_gap
    ):
        """The at-49 world with 048 undecidable: 001 and 003 probe true, the
        marked 002 probes false — pending, not a hard failure."""
        execute(scratch_db, "CREATE TABLE rs_one (id INT)")
        execute(scratch_db, "CREATE TABLE rs_three (id INT)")
        manifest = manifest_with_false_two(tree_with_reapply_safe_gap)

        report = adopt(scratch_db, tree_with_reapply_safe_gap, manifest)

        assert [m.version for m in report.adopted] == [1, 3]
        assert [m.version for m in report.pending] == [2]

    def test_unmarked_false_below_head_still_hard_fails(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE rs_a (id INT);")
        write_migration(tmp_path, 2, "CREATE TABLE rs_b (id INT);", name="ddl")
        write_migration(tmp_path, 3, "CREATE TABLE rs_c (id INT);")
        execute(scratch_db, "CREATE TABLE rs_a (id INT)")
        execute(scratch_db, "CREATE TABLE rs_c (id INT)")
        manifest = write_manifest(
            tmp_path,
            1,
            [
                {"version": 1, "probe": probe_table("rs_a")},
                {"version": 2, "probe": probe_table("rs_b")},
                {"version": 3, "probe": probe_table("rs_c")},
            ],
        )

        with pytest.raises(MigrationRunnerError, match="002"):
            adopt(scratch_db, tmp_path, manifest)


class TestApplyAllowance:
    def test_apply_runs_reapply_safe_pending_below_head(
        self, scratch_db, tree_with_reapply_safe_gap
    ):
        execute(scratch_db, "CREATE TABLE rs_one (id INT)")
        execute(scratch_db, "CREATE TABLE rs_three (id INT)")
        manifest = manifest_with_false_two(tree_with_reapply_safe_gap)
        adopt(scratch_db, tree_with_reapply_safe_gap, manifest)

        report = apply_pending(scratch_db, tree_with_reapply_safe_gap)

        assert [m.version for m in report.applied] == [2]
        rows = fetch_ledger(scratch_db)
        assert [(r[0], r[4]) for r in rows] == [
            (1, "adopted"),
            (2, "applied"),
            (3, "adopted"),
        ]

    def test_apply_still_refuses_unmarked_file_below_head(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE rs_p (id INT);")
        write_migration(tmp_path, 3, "CREATE TABLE rs_q (id INT);")
        apply_pending(scratch_db, tmp_path)
        write_migration(tmp_path, 2, "CREATE TABLE rs_r (id INT);")

        with pytest.raises(MigrationRunnerError, match="002"):
            apply_pending(scratch_db, tmp_path)
