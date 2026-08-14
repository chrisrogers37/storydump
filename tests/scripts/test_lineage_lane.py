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
4. **Tenancy over the TARGET lineage** (#806 Fork 1's paired obligation).
   `test_tenancy_gate.py` replays bounded to the legacy lineage, so it cannot
   see a file numbered above the move — measured, not inferred. This is the
   only replay in the suite that can.

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
    split_statements,
)
from scripts.advertised_ddl import (
    DEFAULT_DOCS,
    DEFAULT_MANIFEST,
    build_stream,
    load_manifest,
    normalize_statements,
    target_lineage_statements,
)
from scripts.schema_parity import schema_diff, schema_signature
from scripts.tenancy_gate import (
    expected_tenancy,
    tenancy_signature,
    tenancy_violations,
    tenant_keyed_tables,
)
from src.utils.validators import MIGRATIONS_DIR
from tests.scripts.conftest import (
    as_user,
    F2_2_END,
    F2_2_START,
    LEGACY_LINEAGE_MAX,
    SETUP_SQL,
    execute,
    fetch_ledger,
    fetch_one,
    psql_apply,
    table_exists,
    write_migration,
)


def run_lane(dsn, migrations_dir=MIGRATIONS_DIR):
    """The §0.2 lineage run: legacy setup applied by hand the way production's
    actually was, then ONE unbounded runner invocation across the boundary —
    001–050, the move, and the F.2 target files as they land.

    `migrations_dir` is overridable for ONE caller: the positive control that
    stages a future F.2 increment into a throwaway tree. It takes the parameter
    rather than re-implementing these two lines so that the control replays the
    same lane it certifies — a hand-rolled copy would keep passing after the
    real lane gained a step, which is the failure mode a control exists to
    rule out.
    """
    psql_apply(dsn, [SETUP_SQL])
    return apply_pending(dsn, migrations_dir)


def real_stream(normalized=True):
    """The advertised stream from the committed docs + manifest.

    One spelling, because the raw and normalized lists are NOT interchangeable
    and the difference has already bitten: normalized statements are a
    comparison key (whitespace collapsed, full-line comments dropped), while
    `02` prints an inline `--` inside `CREATE TABLE onboarding_sessions` — so a
    normalized statement executed as SQL comments out its own tail.
    """
    raw = build_stream(DEFAULT_DOCS, load_manifest(DEFAULT_MANIFEST))
    return normalize_statements(raw) if normalized else raw


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

    def test_the_tenancy_gate_runs_against_this_replay_and_can_see_into_it(
        self, bootstrapped_db
    ):
        """FORK 1'S PAIRED OBLIGATION (#806): aim `tenancy_gate` at the lineage
        F.2 actually lands in.

        Ruling (a) lets policies leave the table increments, trading a
        STRUCTURAL guarantee — a table and its policy in one PR — for a
        DETECTED one. That trade is only honest if the detector is pointed at
        the target lineage, and it was not. `test_tenancy_gate.py` replays
        bounded to `LEGACY_LINEAGE_MAX`, which stops BELOW the move, so no file
        numbered above it can ever appear in the schema it examines.

        Measured before writing this, in an exported tree: a tenant-keyed table
        with RLS off, landed as `053`, left that suite **fully green** while
        this lane's own replay observed it in `public`. So the gap was
        structural, not a matter of degree.

        The check belongs here, beside lane parity, for the same reason lane
        parity is here rather than in a parity suite: the lane owns the replay,
        and imports the predicates that judge it.

        THE PROBE IS NOT DECORATION. After the lane, `public` holds zero
        tables, so the violation check alone passes on an empty set — and would
        pass just as green if `tenancy_signature` were pointed at the wrong
        database entirely. That is the SAME failure this test exists to fix,
        one level down, so it must not be assertable by it. The probe is what
        separates "the gate looked and found nothing" from "the gate is not
        looking."

        It is born the way `02` §7 prints a table, `TO svc_ingress` — a role
        that does not exist without the bootstrap — so the probe also shows
        this replay can carry the policy shape F.2.2 will need.

        WHAT IS ASSERTED IS EQUALITY WITH THE STREAM'S OWN PREFIX, not the
        tenancy invariant itself. The invariant — every tenant-keyed table
        carries a policy — is FALSE mid-stream by the ratified order, so
        asserting it from F.2.2 would be asserting the plan is wrong. What holds
        at every increment, including the empty one, is that the replayed
        lineage carries exactly the tenancy state a prefix of that length
        implies. See the comment at the assertion for why the alternative —
        bounding the check to a complete lineage — is the same quiet window as
        deleting it.
        """
        run_lane(bootstrapped_db)
        sig = tenancy_signature(bootstrapped_db)

        # THE CHECK IS PREFIX-AWARE — the decision the previous revision of this
        # test deferred to whoever landed the first table, now taken.
        #
        # It used to assert `tenant_keyed_tables(sig) == []`, which is true only
        # until F.2.2 and then false for five increments: under Fork 1 ruling (a)
        # the increments are contiguous stream segments, and the stream creates
        # `02`'s 23 tables before the first ENABLE ROW LEVEL SECURITY (index 126)
        # or policy (149). A check written that way had two exits and both were
        # bad — go red on schedule for five increments, or get switched off.
        #
        # The two candidates it named were a prefix-aware check and one bounded
        # to a complete lineage. Bounding it is the quiet failure wearing a
        # different word: the gate would say nothing until F.2.9, which is the
        # same window as deleting it. So: compare against the tenancy state the
        # stream's own prefix OF THE SAME LENGTH implies.
        #
        # This is strictly stronger than the `violations == []` it replaces, in
        # both directions — it fails on a table that landed and should not have,
        # on RLS enabled where the prefix does not call for it, and on a policy
        # count that disagrees. And it needs no edit at F.2.7: the implied state
        # simply grows 23 RLS-enabled tables and 53 policies, and the observed
        # side has to match.
        #
        # NOT a tautology, because the two sides are different objects. `sig` is
        # read off a live catalog after replaying real migration files; the
        # expectation is parsed out of plan TEXT. A file that declares a table
        # the stream declares but fails to install it diverges here.
        #
        # The full target schema is separately checked at full strength today by
        # `test_advertised_ddl_replay.py::test_the_completed_target_schema_has_no_tenancy_violations`
        # — this lane check is the file-lineage half, never the only half.
        f2 = target_lineage_statements(MIGRATIONS_DIR)
        stream = real_stream()
        expected = expected_tenancy(stream[: len(f2)])
        assert sig == expected, (
            f"the replayed target lineage does not carry the tenancy state its"
            f" own stream prefix implies. Lineage is {len(f2)} statements;"
            f" expected {len(expected)} tables"
            f" ({sorted(tenant_keyed_tables(expected))} tenant-keyed), observed"
            f" {len(sig)} ({sorted(tenant_keyed_tables(sig))} tenant-keyed)."
            f" This is NOT the mid-stream RLS window — that is accounted for on"
            f" both sides. It means a migration installed something the plan"
            f" does not declare at this point, or failed to install something it"
            f" does.\n  expected: {expected}\n  observed: {sig}"
        )

        # NON-VACUITY DISCLOSURE, as an ASSERTION rather than a print.
        #
        # With an empty prefix both sides are `{}` and the equality above
        # compared nothing — which is legitimate today, and is exactly the state
        # a reader must not mistake for coverage. An earlier version of this
        # said so with `print()`; pytest captures stdout and surfaces it ONLY on
        # failure, so the message was invisible on precisely the green runs it
        # was written for. A disclosure that cannot be observed is not one.
        #
        # Asserted the way the sibling lane-parity disclosure asserts its own
        # emptiness, and it trips exactly once: at F.2.2, in the same run as the
        # first real comparison, forcing someone to delete it deliberately.
        # `expected_tenancy`'s own non-degeneracy is a positive control in
        # `test_tenancy_gate.py`, against the full 257-statement stream.
        assert not expected, (
            f"the target lineage now carries {len(expected)} tables, so the"
            f" prefix comparison above is load-bearing rather than vacuous."
            f" Delete this disclosure deliberately — it exists to make the"
            f" transition visible, not to bound the check."
        )

        execute(
            bootstrapped_db,
            "CREATE TABLE lane_tenancy_probe ("
            "  id UUID PRIMARY KEY,"
            "  workspace_id UUID NOT NULL);"
            "ALTER TABLE lane_tenancy_probe ENABLE ROW LEVEL SECURITY;"
            "CREATE POLICY p_lane_probe ON lane_tenancy_probe FOR ALL"
            "  TO svc_ingress USING (workspace_id IS NOT NULL)",
        )
        sig = tenancy_signature(bootstrapped_db)
        assert sig.get("lane_tenancy_probe", {}).get("tenant_keyed") is True, (
            "the gate cannot see this replay's catalog at all — the empty"
            " violation list above was measuring some other database"
        )
        assert tenancy_violations(sig) == [], "a correctly born table was flagged"

        # THE MUTATION, on the replayed database itself: same table, policy
        # gone. `test_tenancy_gate.py` proves the predicate discriminates; what
        # can only be proved HERE is that it discriminates on THIS schema.
        execute(bootstrapped_db, "DROP POLICY p_lane_probe ON lane_tenancy_probe")
        violations = tenancy_violations(tenancy_signature(bootstrapped_db))
        assert any(
            "lane_tenancy_probe" in v and "no policy" in v for v in violations
        ), violations

    def test_the_prefix_comparison_holds_with_real_tables_and_fails_on_an_extra_one(
        self, bootstrapped_db, tmp_path
    ):
        """POSITIVE CONTROL for the check above, which on today's lineage
        compares `{}` to `{}` and says so.

        An equality that has only ever been exercised on two empty dicts is not
        evidence that it works — it is the same vacuous green this whole lane
        exists to catch, one level up. So this replays a lineage that DOES carry
        tables: the real F.2.2 segment (stream indices 2..21, seven tables, four
        of them tenant-keyed) staged into a throwaway migrations directory, and
        asserts the comparison holds against a populated catalog.

        Then it mutates — one table the stream does not declare at that
        position — and requires the comparison to go RED. Without that half this
        would only prove the check passes, never that it can fail.

        Staged in `tmp_path`, never the repo's migrations directory: this must
        not become a way to land a file the ratified split has not approved.

        NOTE the statements are taken RAW rather than normalized. Normalized
        statements are a comparison key, not executable SQL — `02` prints an
        inline `--` comment inside `CREATE TABLE onboarding_sessions`, and
        collapsing that statement to one line comments out everything after it.

        IT REPLAYS THE WHOLE CORPUS THROUGH `run_lane`, AND THAT COST IS A
        DECISION. Measured: the corpus leg is ~5.6s of this test's ~6.5s, and
        staging only `052` plus the segment into an empty tree reaches the same
        seven tables — verified, not assumed, because 051 empties `public` and
        052 creates functions rather than relations. It is kept anyway. This is
        the control for a safety check, and its value is that it exercises the
        SAME lane the check runs after; a shortcut equal today rests on the move
        continuing to leave nothing behind, and would keep passing silently the
        day that stopped being true. Fidelity over 5.6s, on a file that already
        replays the corpus seven times.
        """
        import shutil

        raw = real_stream(normalized=False)
        stream = normalize_statements(raw)
        segment = split_statements(raw)[F2_2_START:F2_2_END]

        staged = tmp_path / "migrations"
        shutil.copytree(MIGRATIONS_DIR, staged)
        write_migration(
            staged, 53, ";\n\n".join(s.strip() for s in segment) + ";", name="f2_2"
        )

        run_lane(bootstrapped_db, staged)

        sig = tenancy_signature(bootstrapped_db)
        expected = expected_tenancy(stream[:F2_2_END])
        assert expected, "the control staged nothing — it is proving nothing"
        assert len(tenant_keyed_tables(expected)) == 4, sorted(
            tenant_keyed_tables(expected)
        )
        assert sig == expected, (
            f"the comparison fails on a lineage it should accept.\n"
            f"  expected: {expected}\n  observed: {sig}"
        )

        # THE COST OF THE OLD SHAPE, MEASURED HERE RATHER THAN ARGUED. On this
        # exact catalog the invariant check this test replaced is RED: four
        # tenant-keyed tables carry no RLS, because the ratified stream does not
        # enable it until index 126. They are correct — `sig == expected` above
        # just proved the lineage is exactly what the plan implies — and the old
        # check calls them violations anyway. That is the five-increment window
        # the prefix-aware form exists to survive, and asserting it non-empty
        # pins the reason so nobody reverts the shape thinking it was cosmetic.
        stale_shape = tenancy_violations(sig)
        assert len(stale_shape) == len(tenant_keyed_tables(expected)), stale_shape
        assert all("RLS is not enabled" in v for v in stale_shape), stale_shape

        # THE MUTATION: a tenant-keyed table the stream does not declare at this
        # position, born perfectly correctly. The invariant check cannot see it
        # BY CONSTRUCTION — it has RLS and a policy, so it adds nothing to the
        # list above. Only the prefix comparison can, and that is the direction
        # the new form adds rather than merely preserves.
        execute(
            bootstrapped_db,
            "CREATE TABLE undeclared_probe ("
            "  id UUID PRIMARY KEY,"
            "  workspace_id UUID NOT NULL);"
            "ALTER TABLE undeclared_probe ENABLE ROW LEVEL SECURITY;"
            "CREATE POLICY p_undeclared ON undeclared_probe FOR ALL"
            "  TO svc_ingress USING (workspace_id IS NOT NULL)",
        )
        mutated = tenancy_signature(bootstrapped_db)
        assert tenancy_violations(mutated) == stale_shape, (
            "the undeclared table changed the invariant check's answer — it was "
            "meant to be invisible to it, so this no longer isolates the "
            "direction the prefix comparison uniquely covers"
        )
        assert mutated != expected, (
            "a table the stream does not declare at this position left the "
            "prefix comparison green"
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
