"""F.2.1b — the lineage lane (#746): the corpus replayed ACROSS the boundary.

`04` §0.2's gate, in its own words: *"legacy setup + 001–050, then the 3c
schema-move file, then the F.2 target files … in one run"*. That run is what
this file is. `test_migration_gate.py` replays the legacy lineage **up to** the
boundary and asserts against the schema the running application uses; this
suite replays **through** it and asserts the move's own postconditions plus the
state the F.2 target files will land into.

Three things are asserted here that no other suite can see:

1. **The move happened, and what is in `public` afterwards came from the TARGET
   lineage.** The move leaves `public` empty and that emptiness is the F.2
   files' stated precondition — in CI by replay-from-empty, in production by
   this same file at M.3 step 3c, so the gate and the window are the same act
   against the same precondition. From migration 053 the emptiness is no longer
   observable at the END of the lane, because F.2 builds into it; what is
   asserted instead is that `public` holds exactly the tables the target
   lineage implies, which fails on a legacy leftover the same way and on an
   undeclared target table as well.
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

import functools
import re

import pytest

from scripts.migration_runner import (
    SCHEMA_MOVE_MARKER,
    MigrationRunnerError,
    apply_pending,
    discover_migrations,
    legacy_lineage_max,
    schema_move_migration,
)
from scripts.advertised_ddl import (
    normalize_statements,
    target_lineage_files,
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
    advertised_stream,
    as_user,
    LEGACY_LINEAGE_MAX,
    LEGACY_STANDUP,
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
    001–050, the move, and the F.2 target files as they land.

    It took an overridable `migrations_dir` while the target lineage was empty,
    for the one control that staged a future increment into a throwaway tree.
    053 landed that increment for real, so the control and the parameter went
    with it — the lane now replays the thing itself.
    """
    psql_apply(dsn, [SETUP_SQL])
    return apply_pending(dsn, MIGRATIONS_DIR)


@functools.lru_cache(maxsize=1)
def _lineage_length():
    """How many statements the target lineage carries — the length that decides
    which prefix of the advertised stream it is supposed to equal.

    Cached alongside the stream itself: nothing in the suite writes to the real
    migrations directory (every `write_migration` call targets `tmp_path`), so
    there is no invalidation to miss.
    """
    return len(target_lineage_statements(MIGRATIONS_DIR))


@functools.lru_cache(maxsize=1)
def implied_tenancy():
    """The tenancy state the target lineage IMPLIES — `expected_tenancy` over
    the advertised stream's prefix of the lineage's own statement length.

    ONE HOME FOR THE PREFIX RULE. `target_lineage_statements`' own docstring
    warns that deriving the lineage twice lets the validated list and the list
    driving the slice diverge the first time the lineage needs any filtering,
    with only one site updated. This is that one site.

    It is the plan's answer, not the database's, so a test comparing a replayed
    catalog against it is comparing two independent objects rather than
    restating one.
    """
    return expected_tenancy(advertised_stream()[: _lineage_length()])


def implied_target_tables():
    """The table names `implied_tenancy` implies.

    DERIVED, never a written-down list, and that is the whole point: a list of
    F.2.2's seven tables would be a second enumeration of the increment, right
    on the day it is typed and silently wrong the first time an increment lands
    without someone remembering to edit it. Read this way, the next increment
    updates every caller by existing.

    A `frozenset` because the source is memoized — handing callers a shared
    mutable set would let one test's set arithmetic land in another's.
    """
    return frozenset(implied_tenancy())


#: `CREATE FUNCTION`, with the `OR REPLACE` the plan does not currently use but
#: is not forbidden from using. Anchored at the start, because these are whole
#: normalized statements and so a match can only be the statement's own name.
_CREATE_FUNCTION = re.compile(r"CREATE (?:OR REPLACE )?FUNCTION (\w+)")


def implied_target_functions():
    """The functions the target lineage implies, sorted the way `pg_proc`
    reports them.

    DERIVED FOR THE SAME REASON THE TABLES ARE, and not doing so costs more
    here than it looks. The stream holds 18 `CREATE FUNCTION`s and four have
    landed; the remaining 14 are not appends but INSERTIONS INTO A SORTED LIST,
    since the assertion compares against `proname` order — so a literal list
    would need re-sorting by hand at each of the five remaining increments.

    This does not make the assertion vacuous. The two sides are plan TEXT and a
    live `pg_proc`, the same independent-objects shape as the tenancy
    comparison; what it stops being is a transcription exercise.
    """
    names = {
        match.group(1)
        for statement in advertised_stream()[: _lineage_length()]
        if (match := _CREATE_FUNCTION.match(statement))
    }
    return sorted(names)


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

    def test_the_target_lineage_is_the_increments_that_have_been_ratified(self):
        """THE TARGET LINEAGE'S FILE LIST, enumerated deliberately.

        An explicit list rather than a rule, and it is meant to need editing:
        adding a file above the move is how F.2 advances, so each increment
        should have to say so here once. That is the same shape as the
        disclosure this replaced — `..._is_the_shared_functions_file`, retired
        by 053 the way 052 retired the emptiness disclosure before it.

        WHAT THIS SEES THAT ARM (b) CANNOT. The prefix diff in
        `test_advertised_ddl.py` compares STATEMENTS, so a file above the move
        carrying only comments contributes nothing and leaves it green. Only a
        file-level assertion can see that file at all.

        ORDER IS THE POINT, not membership: arm (b) is an ordered prefix, so
        052's shared functions must precede 053's tables — every touch trigger
        and `ck_ws_tz_valid` calls a function 052 creates.

        The boundary is still derived from the runner's own move-marker, not
        from the number 51.
        """
        move = schema_move_migration(MIGRATIONS_DIR)
        above = [
            m.path.name
            for m in discover_migrations(MIGRATIONS_DIR)
            if m.version > move.version
        ]
        assert above == [
            "052_shared_trigger_functions.sql",
            "053_identity_and_tenancy_tables.sql",
            "054_accounts_sources_media_tables.sql",
            "055_intent_ledger_tables.sql",
            "056_machinery_tables.sql",
            "057_grant_matrix_and_archive_schema.sql",
            "058_rls_and_policies.sql",
            "059_security_definer_doors.sql",
            "060_auth_plane_tables.sql",
            # 061 is the first file PAST F.2: 060 completed the lineage, and
            # this one extends the advertised stream rather than consuming a
            # remaining prefix of it (#883).
            "061_intent_self_transition_guard.sql",
            # 062 extends the stream the same way: the W5e reauth-prompt clock
            # leg (#942), advertised in 07 §9 before landing here.
            "062_reauth_prompt_leg.sql",
            # 063 replaces 062's fn_clock_tick to add the refresh leg's provider
            # guard (#982 prerequisite, #978 disclosure). A REPLACEMENT rather
            # than an edit: the runner keys on file-byte checksums, so an applied
            # file that changes is a hard failure — fix forward. 062 set the
            # precedent by dropping and recreating what 059 created.
            "063_refresh_leg_provider_guard.sql",
            # 064 extends the stream as 061 and 062 did: the memberships door
            # (#1037), advertised as 07 §10 before landing here — appended,
            # because the 02 §7-DDL block that prints the first nine doors is
            # content-addressed and arm (b) never amends it.
            "064_memberships_for_caller_door.sql",
            # 065 extends the stream as 061-064 did: the alert_stranded_sources job
            # kind (#1061), advertised as 07 §11 before landing here. Appended for
            # the same reason 064 was — the 02 §5 machinery block that prints these
            # constraints is content-addressed, and arm (b) never amends.
            "065_alert_stranded_sources_kind.sql",
            # 066 extends the stream as 061-065 did: the "no media available"
            # notice marker (#1090 D3), advertised as 07 section 12 before
            # landing here. Appended for the same reason 064 and 065 were --
            # the 02 section 2 block that prints ig_accounts is
            # content-addressed, and arm (b) never amends.
            "066_no_media_notice_marker.sql",
        ], (
            f"the target lineage is {above}. If you are landing the next F.2"
            " increment, add it here — deliberately, and at the end: arm (b)"
            " is an ordered prefix of the advertised stream, so a file may"
            " only ever be appended."
        )

        # The hole the list exists for, closed mechanically rather than by
        # enumeration: a lineage file contributing zero statements is invisible
        # to arm (b) AND to every length-derived expectation in this file.
        empty = [
            f.name
            for f in target_lineage_files(MIGRATIONS_DIR)
            if not normalize_statements(f.read_text())
        ]
        assert empty == [], f"lineage files contributing no statements: {empty}"


@pytest.mark.integration
@pytest.mark.slow
class TestTheLaneReplaysAcrossTheBoundary:
    def test_one_run_applies_the_whole_corpus_and_public_holds_only_the_target(
        self, bootstrapped_db
    ):
        """The lane's end state, restated now that the target lineage builds
        into `public` rather than leaving it empty.

        THE PRECONDITION DID NOT CHANGE AND THE ASSERTION DID. "Public is
        empty" is what the MOVE establishes and what the F.2 files then consume
        — it was only ever observable at the end of the lane because nothing
        above the move created a relation. 053 does, so an emptiness assertion
        here would now be asserting that F.2 has not started.

        What survives, and is the part that was always load-bearing: whatever is
        in `public` at the end got there from the TARGET lineage. A legacy
        leftover would still fail this, which is the failure the emptiness check
        was really guarding — it is now expressed against the target's own
        implied table set rather than against zero.
        """
        report = run_lane(bootstrapped_db)

        applied = [m.version for m in report.applied]
        assert applied == [m.version for m in discover_migrations(MIGRATIONS_DIR)]

        implied = sorted(implied_target_tables())
        assert sorted(tables_in(bootstrapped_db, "public")) == implied, (
            "public does not hold exactly the target lineage's tables — either"
            " the move left legacy relations behind (the precondition the F.2"
            " files consume, and what this assertion has always been for), or"
            " a target file installed something the advertised stream does not"
            " declare at this point"
        )

    def test_legacy_holds_the_inventory_the_running_application_declares(
        self, bootstrapped_db
    ):
        """THE TEETH the migration's own postconditions deliberately decline to
        grow. `ALTER SCHEMA … RENAME` moves the namespace and not the objects
        in it, so an in-file before/after inventory comparison cannot fail; the
        assertion that CAN fail is against an independent source of truth, and
        the models are one — derived, so there is no list to drift.

        THE PUBLIC-SIDE HALF IS SCOPED TO NAMES THE TARGET DOES NOT REUSE, and
        that scoping is forced rather than convenient. The target schema
        deliberately re-uses `users` and `onboarding_sessions` — same concepts,
        rebuilt — so from F.2.2 on those names exist in BOTH schemas and a bare
        `declared & public == set()` reports a legacy leftover where there is
        none. Comparing by name cannot distinguish a legacy table that failed to
        move from a target table that was just created, so the assertion is
        narrowed to the legacy-only names, where a hit still means exactly one
        thing. The tables it can no longer speak for are covered by identity
        rather than name in the parity gate, which compares their columns.
        """
        run_lane(bootstrapped_db)
        declared = legacy_declared_tables()

        assert declared, "positive control: the legacy models registered nothing"
        missing = sorted(declared - set(tables_in(bootstrapped_db, "legacy")))
        assert missing == [], f"legacy is missing declared tables: {missing}"

        legacy_only = declared - implied_target_tables()
        assert legacy_only, (
            "every legacy table name is also a target table name — this check"
            " has nothing left to discriminate on and needs rethinking"
        )
        stranded = sorted(legacy_only & set(tables_in(bootstrapped_db, "public")))
        assert stranded == [], f"legacy tables stranded in public: {stranded}"

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
        self, owner_actor, owner_db, bootstrapped_db
    ):
        """Why every replay in `test_migration_gate.py` passes the bound, as an
        executable fact rather than a comment. Same corpus, same runner, one
        difference: the bounded run stops below the move and still has a schema
        to assert against; the unbounded run does not.

        THE UNBOUNDED ARM IS NOW BOOTSTRAPPED, and the premise that said it
        need not be has expired. That premise was measured rather than assumed
        — "no migration and no `setup_database.sql` references a service role"
        — and migration 057 (F.2.6) is the first one that does: its grant
        matrix names all six service roles, so an unbootstrapped replay fails
        with `role "svc_ingress" does not exist` before it can draw any
        contrast. This is the moment the note above anticipated when it said
        the lane tests keep the bootstrap "because that is where the first
        policy-carrying table will need it"; grants got there before policies.

        The BOUNDED arm is deliberately still unbootstrapped. It stops below
        the move and never reaches 057, so it needs no role and paying for the
        teardown would buy nothing — and keeping it roleless preserves the
        contrast this test exists to draw.
        """
        # The bounded arm runs as the OWNER ACTOR (#753): the legacy-lineage
        # replay's declared seed identity, so no superuser replay path
        # survives anywhere in these suites.
        as_owner = as_user(owner_db, owner_actor)
        # The bounded arm replays through 050, which routes its owner-DDL
        # through the step-0 door (#787). The unbounded arm below gets the door
        # from `bootstrapped_db`'s own bootstrap, so `run_lane` must NOT apply
        # it a second time — a re-apply as a different actor would fail on
        # `must be owner of function`.
        psql_apply(as_owner, LEGACY_STANDUP)
        apply_pending(as_owner, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        run_lane(bootstrapped_db)

        # The contrast is drawn on LEGACY-ONLY table names, not on emptiness:
        # from 053 the unbounded arm's `public` is populated — by the target
        # schema — so "public is empty" no longer separates the two arms, while
        # "the legacy schema is in public" still separates them exactly.
        legacy_only = legacy_declared_tables() - implied_target_tables()
        assert legacy_only, "no legacy-only name left to draw the contrast on"

        assert legacy_only & set(tables_in(owner_db, "public")), (
            "bounded: the legacy schema is still in public, which is what the"
            " legacy-lineage suites assert against"
        )
        assert legacy_only & set(tables_in(bootstrapped_db, "public")) == set(), (
            "unbounded: the move ran, so the legacy schema must be out of"
            " public — what is there instead is the target lineage"
        )

    def test_the_lane_installs_the_target_functions(self, bootstrapped_db):
        """WHAT THE TARGET LEG CREATES (#806) — the second half of what the
        retired emptiness disclosure asked for.

        The lane replays across the boundary in one run, so this asserts the
        end state of that run rather than of `psql` against the file alone:
        after the move re-creates an empty `public`, 052 lands its two shared
        functions INTO it and 053 adds the two owner-invariant ones.

        FUNCTIONS ARE THE LANE'S OWN EYES HERE. The parity gate is
        relation-scoped and reads `pg_class`, so it cannot see a function at
        all; 053's constraint triggers are the "at least one owner" half of an
        invariant whose "at most one" half is an index, and only the index side
        is visible to any other check in the suite. Losing a trigger function
        would leave every other assertion in this file green.

        `fn_safe_tz` is exercised rather than merely counted. Its whole reason
        for existing is the fallback — a zone the server does not recognize
        degrades that row to UTC instead of raising — and a presence check
        cannot tell a working function from a stub that returns its argument.
        """
        run_lane(bootstrapped_db)

        implied = implied_target_functions()
        assert implied, "positive control: the stream prefix implies no functions"
        assert functions_in(bootstrapped_db, "public") == implied

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
        expected = implied_tenancy()
        assert sig == expected, (
            f"the replayed target lineage does not carry the tenancy state its"
            f" own stream prefix implies. Lineage is {_lineage_length()} statements;"
            f" expected {len(expected)} tables"
            f" ({sorted(tenant_keyed_tables(expected))} tenant-keyed), observed"
            f" {len(sig)} ({sorted(tenant_keyed_tables(sig))} tenant-keyed)."
            f" This is NOT the mid-stream RLS window — that is accounted for on"
            f" both sides. It means a migration installed something the plan"
            f" does not declare at this point, or failed to install something it"
            f" does.\n  expected: {expected}\n  observed: {sig}"
        )

        # THE COMPARISON IS NO LONGER VACUOUS, and that is asserted rather than
        # assumed. The xfail tripwire that used to sit here fired on this
        # increment exactly as designed and was removed with it (#806); what it
        # was protecting — that nobody reads an empty comparison as coverage —
        # is now a live requirement instead of a warning about a future one.
        assert expected, (
            "the prefix comparison is vacuous again — the target lineage"
            " reports zero tables, so `sig == expected` above compared `{}` to"
            " `{}` and passed without looking at anything"
        )

        # THE MID-STREAM WINDOW HAS CLOSED, AND THE INVARIANT IS NOW LIVE.
        # A pin sat here through F.2.2-F.2.6 asserting the OLD invariant shape
        # was red on this exact catalog — every tenant-keyed table reported as
        # a violation because the stream does not enable RLS until index 126.
        # It carried its own expiry ("if RLS has now landed, the mid-stream
        # window has closed and this pin should go with it") and F.2.7 landed
        # statements 126..201, so it went with it.
        #
        # Replaced by the invariant itself rather than by nothing. This is the
        # first increment at which "every tenant-keyed table carries RLS and a
        # policy" is TRUE of the target lineage, which is the whole point of
        # Fork 1 ruling (a)'s trade: the structural guarantee was given up for a
        # detected one, and this is the detector finally reading clean. Deleting
        # the pin without asserting what replaced it would leave the closing
        # unobserved.
        #
        # ONE CORRECTION TO THE RETIRED PIN, recorded because the next tripwire
        # will be read the same way: its INSTRUCTION was right and its SIDE
        # PREDICTION was not. It said the crossing increment "turns these
        # messages from 'RLS is not enabled' to 'no policy'". It does not —
        # F.2.7 lands the 23 ENABLE and the 53 CREATE POLICY together, so no
        # table is ever left enabled-but-unpolicied and the list goes straight
        # to empty. Observed 0 violations, not 17 reworded ones.
        baseline_violations = tenancy_violations(sig)
        assert baseline_violations == [], (
            f"the tenancy invariant is live from F.2.7 and does not hold:"
            f" {baseline_violations}"
        )
        assert tenant_keyed_tables(sig), (
            "positive control: no tenant-keyed table in the replayed lineage,"
            " so the assertion above passed by having nothing to examine"
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
        probed = tenancy_signature(bootstrapped_db)
        assert probed.get("lane_tenancy_probe", {}).get("tenant_keyed") is True, (
            "the gate cannot see this replay's catalog at all — the signature"
            " above was measuring some other database"
        )

        # THE DIRECTION THE PREFIX FORM UNIQUELY ADDS, on one object so the
        # contrast is legible: the probe is a tenant-keyed table the stream does
        # not declare at this position, born PERFECTLY CORRECTLY. The invariant
        # check is blind to it by construction — it has RLS and a policy, so it
        # adds nothing to the list — while the prefix comparison sees it at
        # once. An undeclared table is exactly what a detector-based guarantee
        # has to catch now that grouping no longer carries it (#806 Fork 1).
        assert tenancy_violations(probed) == baseline_violations, (
            "the probe changed the invariant check's answer — it was meant to"
            " be invisible to it, so this no longer isolates the direction the"
            " prefix comparison uniquely covers"
        )
        assert probed != expected, (
            "a correctly-born table the stream does not declare at this"
            " position left the prefix comparison green"
        )

        # THE MUTATION, on the replayed database itself: same table, policy
        # gone. `test_tenancy_gate.py` proves the predicate discriminates; what
        # can only be proved HERE is that it discriminates on THIS schema.
        execute(bootstrapped_db, "DROP POLICY p_lane_probe ON lane_tenancy_probe")
        violations = tenancy_violations(tenancy_signature(bootstrapped_db))
        assert any(
            "lane_tenancy_probe" in v and "no policy" in v for v in violations
        ), violations

    def test_lane_parity_holds_against_the_target_models(
        self, bootstrapped_db, second_scratch_db
    ):
        """LANE PARITY IS LOAD-BEARING FROM HERE (#806, migration 053).

        `04` §0.2's parity arm: the replayed `public` compared against
        `create_all` on the TARGET base — the same comparator the legacy gate
        uses, pointed at the other lineage. It was vacuous through 052 and said
        so in its own name, because the comparator is relation-scoped on both
        sides and 052 creates only functions. 053's seven tables are what switch
        it on, so that disclosure is retired the way it asked to be: by becoming
        a real comparison rather than by being deleted.

        This is also what settles "do target models land per increment or in one
        pass" — the question left open at scoping. It is not a preference:
        tables on the migration side with no models behind them IS the drift
        this gate reports, so a one-pass reading means running it knowingly red
        for five increments.

        THE MODELS SIDE NEEDS 052 FIRST, and that is a real dependency rather
        than a convenience. `ck_ws_tz_valid` calls `fn_safe_tz`, so `create_all`
        cannot execute at all against a database lacking it — a CHECK naming a
        missing function is a hard error, not a skipped constraint. Applying the
        head of the target lineage supplies exactly that, and that it cannot
        contaminate the comparison is asserted below rather than argued: it
        creates no relations, so it moves neither side of a relation-scoped
        diff.
        """
        from sqlalchemy import create_engine

        from src.models.target import TargetBase

        run_lane(bootstrapped_db)

        # The function prerequisite, taken from the lineage's OWN HEAD rather
        # than named by filename — `test_advertised_ddl.py` independently pins
        # that head to the two shared functions and to carrying no table.
        psql_apply(second_scratch_db, [target_lineage_files(MIGRATIONS_DIR)[0]])
        assert relations_in(second_scratch_db, "public") == [], (
            "the prerequisite created a relation — it is meant to supply"
            " functions only, and anything else it creates lands on the models"
            " side of a comparison that must be `create_all` alone"
        )

        engine = create_engine(second_scratch_db)
        TargetBase.metadata.create_all(engine)
        engine.dispose()

        diffs = schema_diff(
            schema_signature(bootstrapped_db), schema_signature(second_scratch_db)
        )
        assert diffs == [], "lane parity drift:\n" + "\n".join(diffs)

        # NON-VACUITY, both halves — what the retired disclosure was holding the
        # place for. Either side going empty makes the diff above pass by
        # comparing nothing, which is the failure it named.
        #
        # DERIVED FROM THE STREAM, never a written-down table list. The list
        # would be a second enumeration of the increment — right the day it is
        # typed, and silently wrong the first time an increment lands without
        # someone remembering to edit it. Reading it off the advertised stream's
        # prefix of the lineage's own length means the next increment updates
        # this assertion by existing, and a models module that fails to keep up
        # is what goes red.
        implied = sorted(implied_target_tables())
        assert implied, "positive control: the stream prefix implies no tables"

        declared = sorted(TargetBase.metadata.tables)
        assert declared == implied, (
            f"the target models declare {declared}, the target lineage implies"
            f" {implied}. Landing an increment's migration without its models —"
            " or the reverse — is exactly the drift this gate reports."
        )
        # The replayed side needs no assertion of its own here: `diffs == []`
        # reports table-set differences in BOTH directions, so an empty diff plus
        # a models side pinned to `implied` pins the replayed side too. It is
        # asserted directly, once, in
        # `test_one_run_applies_the_whole_corpus_and_public_holds_only_the_target`.

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
