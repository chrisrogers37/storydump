"""``runner adopt`` (plan §0.2) — a live database that predates the ledger
enters it, decided by per-file probes and never by trust.

The design requirement (dispatch, 2026-08-11): adopt must work whether the
production database is at migration 45 or 49 **without knowing which** —
probe-decided per file, hard-failing on incoherent shapes, and never reading
the legacy ``schema_version`` table.
"""

import pytest

from scripts.migration_runner import (
    MigrationRunnerError,
    adopt,
    apply_pending,
)
from tests.scripts.conftest import (
    execute,
    fetch_ledger,
    probe_table,
    write_manifest,
    write_migration,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def three_file_tree(tmp_path):
    """Migrations 001-003; the scratch DB decides which 'ran' via tables."""
    write_migration(tmp_path, 1, "CREATE TABLE adopted_one (id INT);")
    write_migration(tmp_path, 2, "CREATE TABLE adopted_two (id INT);")
    write_migration(tmp_path, 3, "CREATE TABLE adopted_three (id INT);")
    return tmp_path


def three_entry_manifest(tmp_path, required_through=1):
    return write_manifest(
        tmp_path,
        required_through,
        [
            {"version": 1, "probe": probe_table("adopted_one")},
            {"version": 2, "probe": probe_table("adopted_two")},
            {"version": 3, "probe": probe_table("adopted_three")},
        ],
    )


class TestAdoptDecidesByProbe:
    def test_all_probes_true_adopts_all(self, scratch_db, three_file_tree):
        for t in ("adopted_one", "adopted_two", "adopted_three"):
            execute(scratch_db, f"CREATE TABLE {t} (id INT)")
        manifest = three_entry_manifest(three_file_tree)

        report = adopt(scratch_db, three_file_tree, manifest)

        assert [m.version for m in report.adopted] == [1, 2, 3]
        assert report.pending == []
        rows = fetch_ledger(scratch_db)
        assert [(r[0], r[4]) for r in rows] == [
            (1, "adopted"),
            (2, "adopted"),
            (3, "adopted"),
        ]

    def test_contiguous_false_tail_is_pending_not_failure(
        self, scratch_db, three_file_tree
    ):
        """The at-45 world: earlier files applied, a tail never was."""
        execute(scratch_db, "CREATE TABLE adopted_one (id INT)")
        manifest = three_entry_manifest(three_file_tree)

        report = adopt(scratch_db, three_file_tree, manifest)

        assert [m.version for m in report.adopted] == [1]
        assert [m.version for m in report.pending] == [2, 3]
        assert [r[0] for r in fetch_ledger(scratch_db)] == [1]

    def test_apply_after_adopt_applies_only_the_pending_tail(
        self, scratch_db, three_file_tree
    ):
        execute(scratch_db, "CREATE TABLE adopted_one (id INT)")
        manifest = three_entry_manifest(three_file_tree)
        adopt(scratch_db, three_file_tree, manifest)

        report = apply_pending(scratch_db, three_file_tree)

        assert [m.version for m in report.applied] == [2, 3]
        rows = fetch_ledger(scratch_db)
        assert [(r[0], r[4]) for r in rows] == [
            (1, "adopted"),
            (2, "applied"),
            (3, "applied"),
        ]

    def test_rerun_skips_already_adopted(self, scratch_db, three_file_tree):
        for t in ("adopted_one", "adopted_two", "adopted_three"):
            execute(scratch_db, f"CREATE TABLE {t} (id INT)")
        manifest = three_entry_manifest(three_file_tree)
        adopt(scratch_db, three_file_tree, manifest)

        report = adopt(scratch_db, three_file_tree, manifest)

        assert report.adopted == []
        assert [m.version for m in report.already] == [1, 2, 3]
        assert len(fetch_ledger(scratch_db)) == 3


class TestAdoptHardFailures:
    def test_required_floor_probe_miss_fails_naming_version_and_probe(
        self, scratch_db, three_file_tree
    ):
        """A file the manifest requires applied, whose probe says otherwise,
        is a discrepancy — never silently 'pending'."""
        execute(scratch_db, "CREATE TABLE adopted_one (id INT)")
        manifest = three_entry_manifest(three_file_tree, required_through=2)

        with pytest.raises(MigrationRunnerError, match="002") as excinfo:
            adopt(scratch_db, three_file_tree, manifest)

        assert "adopted_two" in str(excinfo.value)
        assert fetch_ledger(scratch_db) == [], (
            "a failed adopt must leave the ledger untouched — no partial adoption"
        )

    def test_gap_below_a_true_probe_fails(self, scratch_db, three_file_tree):
        """False-then-true above the floor is an incoherent chain: 003
        present while 002 is absent needs a human, not a guess."""
        execute(scratch_db, "CREATE TABLE adopted_one (id INT)")
        execute(scratch_db, "CREATE TABLE adopted_three (id INT)")
        manifest = three_entry_manifest(three_file_tree)

        with pytest.raises(MigrationRunnerError, match="002"):
            adopt(scratch_db, three_file_tree, manifest)

        assert fetch_ledger(scratch_db) == []

    def test_probe_error_is_a_failure_not_a_false(self, scratch_db, three_file_tree):
        """A broken probe must not read as 'not applied' — erroring and
        returning false get opposite treatments, so they must be distinct."""
        for t in ("adopted_one", "adopted_two", "adopted_three"):
            execute(scratch_db, f"CREATE TABLE {t} (id INT)")
        manifest = write_manifest(
            three_file_tree,
            1,
            [
                {"version": 1, "probe": probe_table("adopted_one")},
                {"version": 2, "probe": "SELECT broken FROM does_not_exist"},
                {"version": 3, "probe": probe_table("adopted_three")},
            ],
        )

        with pytest.raises(MigrationRunnerError, match="002") as excinfo:
            adopt(scratch_db, three_file_tree, manifest)

        # The DISTINCT probe-error path, not the gap rule coincidentally
        # firing on the same fixture — a swallowed probe error must not be
        # able to hide behind another guard.
        assert "errored" in str(excinfo.value)
        assert fetch_ledger(scratch_db) == []

    def test_file_without_manifest_entry_fails(self, scratch_db, three_file_tree):
        manifest = write_manifest(
            three_file_tree,
            1,
            [
                {"version": 1, "probe": probe_table("adopted_one")},
                {"version": 2, "probe": probe_table("adopted_two")},
            ],
        )

        with pytest.raises(MigrationRunnerError, match="003"):
            adopt(scratch_db, three_file_tree, manifest)

    def test_manifest_entry_without_file_fails(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE adopted_one (id INT);")
        manifest = write_manifest(
            tmp_path,
            1,
            [
                {"version": 1, "probe": probe_table("adopted_one")},
                {"version": 9, "probe": "SELECT true"},
            ],
        )

        with pytest.raises(MigrationRunnerError, match="009"):
            adopt(scratch_db, tmp_path, manifest)


class TestAdoptUnderALineageBound:
    """`max_version` selects a replay WINDOW out of a corpus that holds two
    lineages (#746). The manifest is not windowed — it describes the whole
    corpus — and the two manifest checks therefore ask questions at different
    scopes. Answering both from the bounded list is what made adopt lie (#755).
    """

    def test_a_file_above_the_bound_is_not_reported_as_missing(
        self, scratch_db, three_file_tree
    ):
        """THE REGRESSION. Bounded, the manifest's 002 and 003 were reported as
        `no migration file` — while both files sat in the tree, filtered out by
        the bound. An operator reads that and goes looking for files that are
        already there, which costs more than no diagnostic: they find the files
        present and stop believing the tool, mid-incident.
        """
        manifest = three_entry_manifest(three_file_tree)
        execute(scratch_db, "CREATE TABLE adopted_one (id INT)")

        report = adopt(scratch_db, three_file_tree, manifest, 1)

        assert [m.version for m in report.adopted] == [1]
        assert [m.version for m in report.pending] == []

    def test_a_manifest_entry_with_no_file_is_still_reported_when_bounded(
        self, scratch_db, tmp_path
    ):
        """The half a narrower fix loses. Silencing the orphan check *at* the
        bound also silences it for an entry naming a file that genuinely does
        not exist — the check stops asking its own question on every bounded
        run. Pointing it at the corpus instead keeps the question intact and
        makes the answer true: 009 is absent from the tree, bound or no bound.
        """
        write_migration(tmp_path, 1, "CREATE TABLE adopted_one (id INT);")
        write_migration(tmp_path, 2, "CREATE TABLE adopted_two (id INT);")
        manifest = write_manifest(
            tmp_path,
            1,
            [
                {"version": 1, "probe": probe_table("adopted_one")},
                {"version": 9, "probe": "SELECT true"},
            ],
        )

        with pytest.raises(MigrationRunnerError, match="009"):
            adopt(scratch_db, tmp_path, manifest, 1)

    def test_the_probe_requirement_stays_scoped_to_the_window(
        self, scratch_db, three_file_tree
    ):
        """The other direction, and why the corpus is not simply handed to both
        checks. "Every file needs adoption evidence" is a question about what
        adopt is deciding on — it decides nothing above the bound. Widened to
        the corpus, a legacy-lineage adopt would fail because a TARGET file has
        no probe yet: true, and about the wrong files.

        The contrast is in one test because the two calls differ in exactly one
        argument.
        """
        manifest = write_manifest(
            three_file_tree, 1, [{"version": 1, "probe": probe_table("adopted_one")}]
        )
        execute(scratch_db, "CREATE TABLE adopted_one (id INT)")

        report = adopt(scratch_db, three_file_tree, manifest, 1)
        assert [m.version for m in report.adopted] == [1]

        with pytest.raises(MigrationRunnerError, match="002"):
            adopt(scratch_db, three_file_tree, manifest)


class TestAdoptTrustsProbesNotSchemaVersion:
    def test_legacy_schema_version_rows_are_ignored(self, scratch_db, three_file_tree):
        """The legacy self-stamp table contradicts the probes (its known
        010/034-gap hazard class); the probes must win."""
        execute(
            scratch_db,
            "CREATE TABLE schema_version (version INT, description TEXT,"
            " applied_at TIMESTAMPTZ)",
        )
        execute(scratch_db, "INSERT INTO schema_version VALUES (3, 'lies', now())")
        execute(scratch_db, "CREATE TABLE adopted_one (id INT)")
        execute(scratch_db, "CREATE TABLE adopted_two (id INT)")
        manifest = three_entry_manifest(three_file_tree)

        report = adopt(scratch_db, three_file_tree, manifest)

        assert [m.version for m in report.adopted] == [1, 2]
        assert [m.version for m in report.pending] == [3], (
            "schema_version claims 3 applied; the probe says otherwise and must win"
        )


class TestAssertedEntries:
    """Data-only files with no structural delta are declared, not disguised:
    a first-class ``asserted`` entry kind the mechanism can see."""

    def test_asserted_below_floor_adopts_and_reports_distinctly(
        self, scratch_db, tmp_path
    ):
        write_migration(tmp_path, 1, "SELECT 1;", name="data_only")
        write_migration(tmp_path, 2, "CREATE TABLE aa_two (id INT);")
        execute(scratch_db, "CREATE TABLE aa_two (id INT)")
        manifest = write_manifest(
            tmp_path,
            2,
            [
                {"version": 1, "asserted": True, "reason": "data-only"},
                {"version": 2, "probe": probe_table("aa_two")},
            ],
        )

        report = adopt(scratch_db, tmp_path, manifest)

        assert [m.version for m in report.asserted] == [1]
        assert [m.version for m in report.adopted] == [2]
        rows = fetch_ledger(scratch_db)
        assert [(r[0], r[4]) for r in rows] == [(1, "adopted"), (2, "adopted")]

    def test_asserted_above_floor_is_rejected(self, scratch_db, tmp_path):
        """Assertion is trust; above the floor the whole design is that trust
        is never the mechanism — an asserted window entry must refuse."""
        write_migration(tmp_path, 1, "CREATE TABLE aa_one (id INT);")
        write_migration(tmp_path, 2, "SELECT 1;", name="data_only")
        manifest = write_manifest(
            tmp_path,
            1,
            [
                {"version": 1, "probe": probe_table("aa_one")},
                {"version": 2, "asserted": True, "reason": "nope"},
            ],
        )

        with pytest.raises(MigrationRunnerError, match="002"):
            adopt(scratch_db, tmp_path, manifest)


class TestDerivedProbes:
    """A file carrying runner:postcondition lines needs no manifest entry —
    its adoption probe derives from them (one predicate, one home)."""

    def test_missing_entry_derives_from_postconditions(self, scratch_db, tmp_path):
        write_migration(tmp_path, 1, "CREATE TABLE dp_one (id INT);")
        write_migration(
            tmp_path,
            2,
            "-- runner:postcondition SELECT EXISTS (SELECT 1 FROM"
            " information_schema.tables WHERE table_name = 'dp_two')\n"
            "CREATE TABLE dp_two (id INT);",
        )
        execute(scratch_db, "CREATE TABLE dp_one (id INT)")
        execute(scratch_db, "CREATE TABLE dp_two (id INT)")
        manifest = write_manifest(
            tmp_path, 1, [{"version": 1, "probe": probe_table("dp_one")}]
        )

        report = adopt(scratch_db, tmp_path, manifest)

        assert [m.version for m in report.adopted] == [1, 2]

    def test_missing_entry_without_postconditions_still_fails(
        self, scratch_db, tmp_path
    ):
        write_migration(tmp_path, 1, "CREATE TABLE dp_a (id INT);")
        write_migration(tmp_path, 2, "CREATE TABLE dp_b (id INT);")
        manifest = write_manifest(
            tmp_path, 1, [{"version": 1, "probe": probe_table("dp_a")}]
        )

        with pytest.raises(MigrationRunnerError, match="002"):
            adopt(scratch_db, tmp_path, manifest)
