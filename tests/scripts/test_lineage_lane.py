"""F.2.1b — the lineage lane (#746): the corpus replayed ACROSS the boundary.

`04` §0.2's gate, in its own words: *"legacy setup + 001–050, then the 3c
schema-move file, then the F.2 target files … in one run"*. That run is what
this file is. `test_migration_gate.py` replays the legacy lineage **up to** the
boundary and asserts against the schema the running application uses; this
suite replays **through** it and asserts the move's own postconditions plus the
state the F.2 target files will land into.

Three things are asserted here that no other suite can see:

1. **The move happened, and `public` came back empty.** That emptiness is the
   F.2 files' stated precondition — in CI by replay-from-empty, in production
   by this same file at M.3 step 3c, so the gate and the window are the same
   act against the same precondition.
2. **`legacy` holds the inventory the running application declares.** Derived
   from the legacy models rather than from a written-down table list — the
   assertion the migration's own postconditions deliberately do not make,
   because an inventory comparison across `ALTER SCHEMA … RENAME` cannot fail.
3. **The bound the legacy suites pass is load-bearing, not tidy.** Stated as an
   executable fact rather than a comment: bounded, `public` still holds the
   legacy schema; unbounded, it does not.

Scope, named rather than implied: this lane runs as the **test actor**, not as
`svc_migration` after an owner-actor bootstrap. The actor-faithful replay is a
pre-existing 0.2 gap tracked on #753 along with `advertised_ddl_replay`. This
suite does not close it and must not widen it — it proves the SQL replays, not
that it replays under the declared actor.
"""

import pytest

from scripts.migration_runner import (
    SCHEMA_MOVE_MARKER,
    MigrationRunnerError,
    apply_pending,
    discover_migrations,
    legacy_lineage_max,
    schema_move_migration,
)
from scripts.schema_parity import schema_diff, schema_signature
from src.utils.validators import MIGRATIONS_DIR
from tests.scripts.conftest import (
    as_user,
    LEGACY_LINEAGE_MAX,
    SETUP_SQL,
    execute,
    fetch_ledger,
    fetch_one,
    psql_apply,
    table_exists,
    write_migration,
)


def run_lane(dsn):
    """The §0.2 lineage run: legacy setup applied by hand the way production's
    actually was, then ONE unbounded runner invocation across the boundary —
    001–050, the move, and the F.2 target files as they land."""
    psql_apply(dsn, [SETUP_SQL])
    return apply_pending(dsn, MIGRATIONS_DIR)


def _relnames(dsn, schema, kinds=None):
    row = fetch_one(
        dsn,
        "SELECT coalesce(array_agg(c.relname ORDER BY c.relname), '{}')"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %(schema)s"
        " AND (%(kinds)s::text[] IS NULL OR c.relkind::text = ANY(%(kinds)s))",
        {"schema": schema, "kinds": None if kinds is None else list(kinds)},
    )
    return list(row[0])


def relations_in(dsn, schema):
    """Every relation of every kind — the plan's postcondition wording."""
    return _relnames(dsn, schema)


def tables_in(dsn, schema):
    """Ordinary tables only, for comparison against model table names."""
    return _relnames(dsn, schema, kinds=["r", "p"])


def functions_in(dsn, schema):
    """Function names in a schema.

    Deliberately NOT `relations_in`: functions live in `pg_proc`, not
    `pg_class`, so no relation-based probe can see them. That is exactly why
    migration 052 lands without moving any of the relation-shaped assertions
    in this file, and why the lane needs its own eyes on the target leg.
    """
    row = fetch_one(
        dsn,
        "SELECT coalesce(array_agg(p.proname ORDER BY p.proname), '{}')"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " WHERE n.nspname = %(schema)s",
        {"schema": schema},
    )
    return list(row[0])


def legacy_declared_tables():
    """The inventory the running application declares, read off the models."""
    import src.models  # noqa: F401 - registers every legacy model on Base
    from src.config.database import Base

    return set(Base.metadata.tables)


class TestTheBoundaryIsDerivedAndLoud:
    """The boundary is located by the move file's own marker. No suite, fixture
    or constant names a version — see `legacy_lineage_max`'s docstring for why
    that decides the whole shape."""

    def _corpus(self, directory, move_versions):
        directory.mkdir(parents=True, exist_ok=True)
        for version in (1, 2, 3, 4):
            marker = f"{SCHEMA_MOVE_MARKER}\n" if version in move_versions else ""
            write_migration(directory, version, f"{marker}SELECT 1;\n")
        return directory

    def test_the_bound_is_the_version_below_the_move(self, tmp_path):
        corpus = self._corpus(tmp_path, {3})

        assert legacy_lineage_max(corpus) == 2
        assert [m.version for m in discover_migrations(corpus, 2)] == [1, 2]
        assert [m.version for m in discover_migrations(corpus)] == [1, 2, 3, 4]

    def test_the_marker_is_what_locates_it_so_renumbering_moves_it(self, tmp_path):
        """The property that made a derived boundary beat a declared one: the
        same corpus with the move one file later reports a later boundary, with
        nothing edited anywhere else."""
        assert legacy_lineage_max(self._corpus(tmp_path / "a", {3})) == 2
        assert legacy_lineage_max(self._corpus(tmp_path / "b", {4})) == 3

    def test_a_corpus_with_no_move_file_refuses(self, tmp_path):
        """THE DIRECTION THAT MATTERS. Every caller is asking 'replay the
        legacy lineage only'; answering 'then replay all of it' when the
        boundary cannot be found hands back the target schema under the legacy
        one's name — absence of evidence read as evidence of absence, on a door
        that licenses a replay rather than merely reporting."""
        corpus = tmp_path
        write_migration(corpus, 1, "SELECT 1;\n")

        with pytest.raises(MigrationRunnerError, match="schema-move"):
            legacy_lineage_max(corpus)

    def test_two_move_files_refuse_and_name_both(self, tmp_path):
        corpus = self._corpus(tmp_path, {2, 3})

        with pytest.raises(MigrationRunnerError) as excinfo:
            legacy_lineage_max(corpus)

        message = str(excinfo.value)
        assert "002" in message and "003" in message, message

    def test_the_real_corpus_has_exactly_one_boundary_below_which_the_bound_sits(self):
        """Positive control: the two refusals above prove the predicate can
        fail, and a predicate that has only ever failed proves nothing about
        the corpus it is pointed at."""
        move = schema_move_migration(MIGRATIONS_DIR)

        bounded = discover_migrations(MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        assert bounded, "the bound emptied the legacy lineage"
        assert max(m.version for m in bounded) == move.version - 1

    def test_the_target_lineage_is_the_shared_functions_file(self):
        """THE LANE'S TARGET LEG IS LOAD-BEARING FROM HERE (#806).

        This replaces `test_the_target_lineage_is_currently_empty_and_says_so`,
        which disclosed that the leg replayed an empty set and named what it
        expected to arrive: `02` §0's two shared trigger functions, F.2.1's one
        surviving item. Migration 052 is exactly that, so the disclosure is
        retired on its own terms rather than deleted.

        The other half of what it asked for — assertions about what those
        files CREATE — is `test_the_lane_installs_the_shared_functions` below,
        which needs a database and so cannot live in this class.

        The boundary is still derived from the runner's own move-marker, not
        from the number 51.
        """
        move = schema_move_migration(MIGRATIONS_DIR)
        above = [
            m.path.name
            for m in discover_migrations(MIGRATIONS_DIR)
            if m.version > move.version
        ]
        assert above == ["052_shared_trigger_functions.sql"], (
            f"the target lineage is {above}, expected exactly the shared"
            " functions file — 052 is the head of the advertised stream and"
            " nothing may be numbered above the move before it"
        )


@pytest.mark.integration
@pytest.mark.slow
class TestTheLaneReplaysAcrossTheBoundary:
    def test_one_run_applies_the_whole_corpus_and_leaves_public_empty(
        self, bootstrapped_db
    ):
        report = run_lane(bootstrapped_db)

        applied = [m.version for m in report.applied]
        assert applied == [m.version for m in discover_migrations(MIGRATIONS_DIR)]
        assert relations_in(bootstrapped_db, "public") == [], (
            "the F.2 files' stated precondition is an EMPTY public — the move"
            " is what makes it true, here and at M.3 step 3c alike"
        )

    def test_legacy_holds_the_inventory_the_running_application_declares(
        self, bootstrapped_db
    ):
        """THE TEETH the migration's own postconditions deliberately decline to
        grow. `ALTER SCHEMA … RENAME` moves the namespace and not the objects
        in it, so an in-file before/after inventory comparison cannot fail; the
        assertion that CAN fail is against an independent source of truth, and
        the models are one — derived, so there is no list to drift."""
        run_lane(bootstrapped_db)
        declared = legacy_declared_tables()

        assert declared, "positive control: the legacy models registered nothing"
        missing = sorted(declared - set(tables_in(bootstrapped_db, "legacy")))
        assert missing == [], f"legacy is missing declared tables: {missing}"
        assert declared & set(tables_in(bootstrapped_db, "public")) == set()

        # THE MUTATION, in the same test on the same database — the shape
        # `test_tenancy_gate.py` uses, and the reason it uses it: a comparator
        # that cannot fail proves nothing, and mutating a dict cannot tell you
        # the query reads the right catalog in the right schema.
        victim = sorted(declared)[0]
        execute(bootstrapped_db, f'DROP TABLE legacy."{victim}" CASCADE')

        missing = sorted(declared - set(tables_in(bootstrapped_db, "legacy")))
        assert missing == [victim], missing

    def test_the_ledger_survives_the_move_because_it_is_not_in_public(
        self, bootstrapped_db
    ):
        """The `runner` schema's whole reason for existing (`04` §0.2): a
        ledger in `public` would ride into `legacy` mid-run, severing the
        migration history from the invocation writing it."""
        report = run_lane(bootstrapped_db)

        rows = fetch_ledger(bootstrapped_db)
        assert [r[0] for r in rows] == [m.version for m in report.applied]
        assert all(r[4] == "applied" for r in rows)
        assert not table_exists(bootstrapped_db, "schema_migrations", "legacy"), (
            "the ledger rode the rename into legacy — it must live in the"
            " runner schema, which is invariant across schema-level operations"
        )

    def test_the_legacy_bound_is_load_bearing_not_tidy(
        self, owner_actor, owner_db, second_scratch_db
    ):
        """Why every replay in `test_migration_gate.py` passes the bound, as an
        executable fact rather than a comment. Same corpus, same runner, one
        difference: the bounded run stops below the move and still has a schema
        to assert against; the unbounded run does not.

        Neither arm is bootstrapped, deliberately — measured, no migration and
        no `setup_database.sql` references a service role, so the bootstrap
        buys this contrast nothing and its per-test role teardown is not free.
        The lane tests that assert the LANE keep it, because that is where the
        first policy-carrying table will need it.
        """
        # The bounded arm runs as the OWNER ACTOR (#753): the legacy-lineage
        # replay's declared seed identity, so no superuser replay path
        # survives anywhere in these suites.
        as_owner = as_user(owner_db, owner_actor)
        psql_apply(as_owner, [SETUP_SQL])
        apply_pending(as_owner, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        run_lane(second_scratch_db)

        assert tables_in(owner_db, "public"), (
            "bounded: the legacy schema is still in public, which is what the"
            " legacy-lineage suites assert against"
        )
        assert relations_in(second_scratch_db, "public") == []

    def test_the_lane_installs_the_shared_functions(self, bootstrapped_db):
        """WHAT THE TARGET LEG CREATES (#806) — the second half of what the
        retired emptiness disclosure asked for.

        The lane replays across the boundary in one run, so this asserts the
        end state of that run rather than of `psql` against the file alone:
        after the move re-creates an empty `public`, 052 lands its two shared
        functions INTO it.

        `fn_safe_tz` is exercised rather than merely counted. Its whole reason
        for existing is the fallback — a zone the server does not recognize
        degrades that row to UTC instead of raising — and a presence check
        cannot tell a working function from a stub that returns its argument.
        """
        run_lane(bootstrapped_db)

        assert functions_in(bootstrapped_db, "public") == [
            "fn_safe_tz",
            "trg_touch_updated_at",
        ]

        good, bad = fetch_one(
            bootstrapped_db,
            "SELECT fn_safe_tz('America/New_York'), fn_safe_tz('Not/AZone')",
        )
        assert (good, bad) == ("America/New_York", "UTC"), (
            "fn_safe_tz does not both pass a real zone through and fall back"
            " to UTC — asserted as a PAIR, because a function that always"
            " returns its argument passes the first half alone, and one that"
            " always returns 'UTC' passes the second"
        )

    def test_lane_parity_holds_and_discloses_that_it_is_currently_empty(
        self, bootstrapped_db, second_scratch_db
    ):
        """Lane parity under fork (a): the replayed `public` compared against
        `create_all` on the TARGET base — the same comparator the legacy parity
        gate uses, pointed at the other lineage.

        STILL VACUOUS AFTER 052, and for a reason worth stating rather than
        inferring from a green: the target leg is no longer empty — the lane
        now installs two functions — but this comparator is relation-scoped on
        both sides (`schema_signature` over `pg_class`, `create_all` over
        declared tables), and a function is not a relation. So 052 moves
        neither side and the arithmetic is still on two empty sets.

        The disclosure therefore stands as written, and the assertions below
        are unchanged. F.2.2's first table is what switches this on.
        """
        from sqlalchemy import create_engine

        from src.models.target import TargetBase

        run_lane(bootstrapped_db)

        engine = create_engine(second_scratch_db)
        TargetBase.metadata.create_all(engine)
        engine.dispose()

        diffs = schema_diff(
            schema_signature(bootstrapped_db), schema_signature(second_scratch_db)
        )
        assert diffs == [], "lane parity drift:\n" + "\n".join(diffs)

        # NON-VACUITY DISCLOSURE — both halves, because either one becoming
        # non-empty alone is the drift this gate exists to catch.
        assert list(TargetBase.metadata.tables) == [], (
            "target models now exist. Lane parity is load-bearing from here —"
            " update this disclosure deliberately."
        )
        assert relations_in(bootstrapped_db, "public") == [], (
            "the lane now creates relations in public. Lane parity is"
            " load-bearing from here — update this disclosure deliberately."
        )

    def test_the_moves_probe_refuses_every_shape_of_unmoved_database(
        self, scratch_db, second_scratch_db
    ):
        """The postconditions are the adoption probe — what `runner adopt`
        derives "has this file been applied here" from at M.3 step 3a, since
        the move carries no manifest entry. A probe that reads APPLIED on a
        database where the move has not run is the worst failure available to
        it: adopt records it, the runner skips it, and at 3d the F.2 files meet
        a populated `public` — the exact collision the move exists to prevent.

        So the probe is driven against THREE un-moved shapes, not one. Each
        exists because it is the shape a smaller probe would get wrong, and
        together they leave no predicate here that can be deleted without a
        test going red:

        1. a brand-new empty database — `public` is already empty, so "public
           holds zero relations" ALONE reads applied here;
        2. the populated legacy database 3a actually runs against;
        3. a populated legacy database that already has a NON-EMPTY `legacy`
           schema for some unrelated reason (a hand-made archive, an abandoned
           window) — "legacy holds relations" ALONE reads applied here. It has
           to hold relations, not merely exist: an empty `legacy` schema is
           refused by that predicate anyway, so it discriminates nothing.

        Then the moved database, where it must finally read applied. The
        conjunction is what adopt evaluates, so the conjunction is what is
        asserted; the per-shape split is what gives each half of it teeth.
        """
        move = schema_move_migration(MIGRATIONS_DIR)
        assert move.postconditions, (
            "the move carries no manifest entry, so postconditions are the only"
            " thing adopt can derive its probe from"
        )

        def reads_applied(dsn):
            return all(fetch_one(dsn, sql)[0] for sql in move.postconditions)

        assert not reads_applied(second_scratch_db), (
            "the probe reads APPLIED on an EMPTY database — adopt would skip"
            " the move on any database whose public happens to be empty"
        )

        psql_apply(scratch_db, [SETUP_SQL])
        assert not reads_applied(scratch_db), (
            "the probe reads APPLIED on the populated, un-moved database that"
            " M.3 step 3a actually runs against"
        )

        psql_apply(second_scratch_db, [SETUP_SQL])
        execute(
            second_scratch_db,
            "CREATE SCHEMA legacy; CREATE TABLE legacy.leftover (id int)",
        )
        assert not reads_applied(second_scratch_db), (
            "a pre-existing `legacy` schema was mistaken for a completed move"
            " — the probe must also observe that public was emptied"
        )

        psql_apply(scratch_db, [move.path])
        assert reads_applied(scratch_db), "the probe fails to see a real move"

    def test_the_target_base_is_a_separate_metadata_from_the_legacy_one(self):
        """Fork (a)'s mechanism, asserted rather than assumed: if the two bases
        ever shared `MetaData`, `create_all` on the target would emit the
        legacy schema and lane parity would compare a lineage against itself —
        green, and meaningless."""
        from src.config.database import Base
        from src.models.target import TargetBase

        assert TargetBase.metadata is not Base.metadata
        assert legacy_declared_tables(), "positive control: legacy base is loaded"
