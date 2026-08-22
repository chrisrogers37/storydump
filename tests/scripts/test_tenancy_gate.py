"""F.2.0 — the gate that can see RLS (#746).

The parity comparator compares the runner-replayed schema against
``Base.metadata.create_all``. Measured: create_all emits 24 foreign keys but
**zero** policies, zero RLS-enabled tables, zero triggers and zero functions.
So RLS cannot be checked by comparison — the models side can never produce it,
and adding it to the parity signature would make that gate permanently red.

RLS is therefore checked as an **invariant over the replayed schema alone**:
every workspace-keyed table is RLS-enabled and carries at least one policy.
FKs, which both sides do emit, belong in the parity signature instead.

`test_migration_gate.py:180` already states the principle this file exists to
satisfy — *"A comparator that cannot fail proves nothing."* Every check below
is paired with a proof that it CAN fail.
"""

import pytest

from tests.scripts.conftest import F2_2_END, F2_6_END, advertised_stream
from scripts.tenancy_gate import (
    _tenancy_entry,
    expected_tenancy,
    tenancy_signature,
    tenancy_violations,
    tenant_keyed_tables,
    TENANT_KEY,
)


#: The advertised stream, built once per process — now `conftest`'s, not this
#: file's. It was defined here with the measurement that justified caching it
#: (~33 ms a build, six calls in the class below); `test_lineage_lane.py` then
#: grew a byte-identical copy WITHOUT the cache, which is the ordinary end of
#: two files owning one derivation. Promoted rather than re-copied, so the next
#: suite that needs it finds a cached spelling instead of writing a third.
_real_stream = advertised_stream


def _sig(**tables):
    """Synthetic signature: {table: (tenant_keyed, rls_enabled, policies[, forced])}."""
    out = {}
    for name, spec in tables.items():
        t, r, p = spec[0], spec[1], spec[2]
        forced = spec[3] if len(spec) > 3 else False
        out[name] = {
            "tenant_keyed": t,
            "rls_enabled": r,
            "policies": p,
            "rls_forced": forced,
        }
    return out


class TestTheGateCanFail:
    """The comparator-that-cannot-fail check, applied to this comparator."""

    def test_flags_a_tenant_keyed_table_with_rls_off(self):
        v = tenancy_violations(_sig(media_items=(True, False, 0)))
        assert any("media_items" in x and "RLS" in x for x in v), v

    def test_flags_rls_enabled_but_no_policy(self):
        """The subtler half: ENABLE without a policy denies everything to the
        logins and reads as 'secured' to anyone eyeballing relrowsecurity."""
        v = tenancy_violations(_sig(media_items=(True, True, 0)))
        assert any("media_items" in x and "policy" in x.lower() for x in v), v

    def test_passes_a_correctly_born_table(self):
        assert tenancy_violations(_sig(media_items=(True, True, 2))) == []

    def test_ignores_a_table_with_no_tenant_key(self):
        """user-plane and auth-plane tables legitimately carry no workspace key
        (02 §7-DDL Class 3/4) — the gate must not demand tenancy of them."""
        assert tenancy_violations(_sig(users=(False, False, 0))) == []


class TestAgainstTheLegacyReplay:
    """THE LEGACY LINEAGE ONLY — named, because it used to read as "the real
    replay" and there are two (#806).

    Every replay below is bounded to `LEGACY_LINEAGE_MAX`, which stops BELOW
    the 052 move. F.2's files are numbered ABOVE it, so nothing this class
    examines can ever contain one. The target-lineage half is
    `test_lineage_lane.py::test_the_tenancy_gate_runs_against_this_replay_and_can_see_into_it`.
    """

    @pytest.mark.integration
    def test_replayed_corpus_has_no_tenancy_violations(self, scratch_db):
        from tests.scripts.conftest import LEGACY_STANDUP
        from tests.scripts.test_migration_gate import (
            LEGACY_LINEAGE_MAX,
            MIGRATIONS_DIR,
            apply_pending,
            psql_apply,
        )

        # LEGACY_STANDUP, not SETUP_SQL: this replay reaches 051, which routes
        # its owner-DDL through the step-0 door (#787). Imported from conftest
        # rather than re-exported through test_migration_gate — the symbol has
        # one home, and leaning on another test module to forward it is what
        # broke here when that module's import list changed.
        psql_apply(scratch_db, LEGACY_STANDUP)
        apply_pending(scratch_db, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        sig = tenancy_signature(scratch_db)
        assert tenancy_violations(sig) == []

    @pytest.mark.integration
    def test_states_its_own_coverage_rather_than_passing_silently(self, scratch_db):
        """NON-VACUITY DISCLOSURE, and a CORRECTION to the one it replaces
        (#806).

        This used to say the count would move "when F.2.2 lands the first
        workspace-keyed table." It would not. F.2's tables are numbered above
        the move and this replay stops below it, so every table F.2 ever lands
        leaves this green — measured in an exported tree, where a tenant-keyed
        RLS-less `054` left this whole class passing.

        A disclosure that names a trip condition it cannot reach is worse than
        none: it is read as coverage. What is actually disclosed is that the
        LEGACY corpus carries no workspace-keyed table, and the condition that
        moves this number is a LEGACY one gaining a tenant key — which would
        mean the pre-move schema had grown a tenancy obligation nothing else
        checks.

        The F.2 tripwire lives where the F.2 replay is; see the class
        docstring for the pointer.
        """
        from tests.scripts.conftest import LEGACY_STANDUP
        from tests.scripts.test_migration_gate import (
            LEGACY_LINEAGE_MAX,
            MIGRATIONS_DIR,
            apply_pending,
            psql_apply,
        )

        # LEGACY_STANDUP, not SETUP_SQL: this replay reaches 051, which routes
        # its owner-DDL through the step-0 door (#787). Imported from conftest
        # rather than re-exported through test_migration_gate — the symbol has
        # one home, and leaning on another test module to forward it is what
        # broke here when that module's import list changed.
        psql_apply(scratch_db, LEGACY_STANDUP)
        apply_pending(scratch_db, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
        sig = tenancy_signature(scratch_db)
        keyed = [t for t, e in sig.items() if e["tenant_keyed"]]
        assert keyed == [], (
            f"{len(keyed)} tenant-keyed tables now exist ({keyed}). The corpus "
            f"gate is now load-bearing — update this disclosure deliberately."
        )
        assert TENANT_KEY == "workspace_id"


class TestParitySeesForeignKeys:
    """F.2.0's other half. FKs differ from RLS: both sides DO emit them
    (measured — create_all produced 24), so they belong in the parity
    comparison rather than in the invariant gate above."""

    def test_a_missing_fk_is_a_diff(self):
        from scripts.schema_parity import schema_diff

        base = {"columns": {}, "checks": {}, "uniques": set()}
        replayed = {
            "media_items": {
                **base,
                "fks": {"FOREIGN KEY (workspace_id) REFERENCES workspaces(id)"},
            }
        }
        models = {"media_items": {**base, "fks": set()}}
        diffs = schema_diff(replayed, models)
        assert any(d.startswith("fk media_items") for d in diffs), diffs

    def test_identical_fks_are_not_a_diff(self):
        from scripts.schema_parity import schema_diff

        fk = {"FOREIGN KEY (workspace_id) REFERENCES workspaces(id)"}
        base = {"columns": {}, "checks": {}, "uniques": set()}
        assert schema_diff({"t": {**base, "fks": fk}}, {"t": {**base, "fks": fk}}) == []


@pytest.mark.integration
class TestTheGateFiresOnRealPostgres:
    """The synthetic checks above prove the PREDICATE can fail. This proves the
    whole path can — signature read from a live catalog, not a hand-built dict.

    A predicate that fails on a dict but never on a database is still a
    comparator that cannot fail; the dict cannot tell you that
    `tenancy_signature` queries the wrong catalog or spells the column wrong.
    """

    def _exec(self, dsn, sql):
        import psycopg2

        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.close()

    def test_born_correctly_passes_then_dropping_the_policy_turns_it_red(
        self, scratch_db
    ):
        self._exec(
            scratch_db,
            """
            CREATE TABLE gate_probe (
              id UUID PRIMARY KEY,
              workspace_id UUID NOT NULL
            );
            ALTER TABLE gate_probe ENABLE ROW LEVEL SECURITY;
            CREATE POLICY p_probe ON gate_probe FOR ALL
              USING (workspace_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
            """,
        )
        sig = tenancy_signature(scratch_db)
        assert sig["gate_probe"] == {
            "tenant_keyed": True,
            "rls_enabled": True,
            "rls_forced": False,  # the ratified posture: ENABLE without FORCE
            "policies": 1,
        }
        assert tenancy_violations(sig) == [], "a correctly born table was flagged"

        # THE MUTATION: drop the policy, keep everything else.
        self._exec(scratch_db, "DROP POLICY p_probe ON gate_probe;")
        v = tenancy_violations(tenancy_signature(scratch_db))
        assert any("gate_probe" in x and "no policy" in x for x in v), v

        # And the other half: RLS off entirely.
        self._exec(scratch_db, "ALTER TABLE gate_probe DISABLE ROW LEVEL SECURITY;")
        v = tenancy_violations(tenancy_signature(scratch_db))
        assert any("gate_probe" in x and "RLS is not enabled" in x for x in v), v

        self._exec(scratch_db, "DROP TABLE gate_probe;")

    def test_a_table_without_a_tenant_key_is_not_demanded_of(self, scratch_db):
        """Class 3/4 tables (user-plane, machinery) carry no workspace key by
        design. The gate must stay silent on them or it blocks the auth plane."""
        self._exec(scratch_db, "CREATE TABLE gate_probe_global (id UUID PRIMARY KEY);")
        sig = tenancy_signature(scratch_db)
        assert sig["gate_probe_global"]["tenant_keyed"] is False
        assert tenancy_violations(sig) == []
        self._exec(scratch_db, "DROP TABLE gate_probe_global;")


class TestOwnerBypassPosture:
    """rajan's #750 finding. Policies do NOT apply to a table's OWNER unless
    FORCE is set — so ENABLE + policies + no FORCE is owner-readable wholesale.

    Measured on real Postgres before choosing what to gate:
      ENABLE only, deny-all policy  -> owner reads the row   (bypass is real)
      + FORCE                       -> owner reads nothing   (FORCE works)
      + one `NO FORCE` statement    -> owner reads it again  (owner can undo it)

    `02` §7-DDL rules ENABLE-without-FORCE deliberately, because owner-bypass is
    what lets the M.3 transform run without blanket migration policies. So the
    gate does NOT require FORCE — that would fail every table built to spec.
    It gates AGREEMENT, because a mixed estate is the state nobody can see.
    """

    def test_a_table_deviating_from_the_posture_is_flagged(self):
        v = tenancy_violations(_sig(media_items=(True, True, 2, True)))
        assert any("media_items" in x and "posture" in x for x in v), v

    def test_the_posture_message_is_distinguishable_from_the_other_two(self):
        """Three invariants, three messages — an operator reading red has to
        know WHICH one broke, and 'RLS' appears in all three descriptions."""
        no_rls = tenancy_violations(_sig(a=(True, False, 0)))[0]
        no_pol = tenancy_violations(_sig(b=(True, True, 0)))[0]
        posture = tenancy_violations(_sig(c=(True, True, 2, True)))[0]
        assert len({no_rls, no_pol, posture}) == 3
        assert "not enabled" in no_rls
        assert "no policy" in no_pol
        assert "posture deviates" in posture

    def test_conforming_tables_are_silent(self):
        assert tenancy_violations(_sig(a=(True, True, 1, False))) == []


@pytest.mark.integration
class TestPostureFiresOnRealPostgres:
    def _exec(self, dsn, sql):
        import psycopg2

        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.close()

    def test_setting_force_on_one_table_turns_the_gate_red(self, scratch_db):
        self._exec(
            scratch_db,
            "CREATE TABLE posture_probe (id UUID PRIMARY KEY, workspace_id UUID NOT NULL)",
        )
        self._exec(scratch_db, "ALTER TABLE posture_probe ENABLE ROW LEVEL SECURITY")
        self._exec(
            scratch_db,
            "CREATE POLICY p ON posture_probe FOR ALL USING (workspace_id IS NOT NULL)",
        )
        assert tenancy_violations(tenancy_signature(scratch_db)) == []

        # THE MUTATION: one table deviates from the ratified posture.
        self._exec(scratch_db, "ALTER TABLE posture_probe FORCE ROW LEVEL SECURITY")
        sig = tenancy_signature(scratch_db)
        assert sig["posture_probe"]["rls_forced"] is True
        v = tenancy_violations(sig)
        assert any("posture_probe" in x and "posture deviates" in x for x in v), v

        self._exec(scratch_db, "DROP TABLE posture_probe")


class TestAddColumnBoundaryIsBoundedAtBothEnds:
    """#978 review (virgil): `re.match` anchors only the START, so a compound
    ALTER — `ADD COLUMN foo, DROP COLUMN workspace_id` — matched as a prefix
    and rode the continue past the refusal. The bound is a TOP-LEVEL comma:
    a single ADD COLUMN never carries one outside parentheses, while a
    paren'd type (`numeric(10,2)`) legitimately carries one inside. A comma
    at depth zero means a second action — whatever it is, including the
    fact-MOVING ones a verb denylist would have to keep chasing — and falls
    through to the loud refusal. Control-run established the regression on
    the pre-fix SHA before this pin existed; these keep it dead.
    """

    BASE = "CREATE TABLE t ( id uuid, workspace_id uuid )"

    ADMITTED = {
        "intended_062": "ALTER TABLE t ADD COLUMN last_reauth_prompt_at TIMESTAMPTZ NULL",
        "tenant_key_add": "ALTER TABLE t ADD COLUMN workspace_id uuid",
        "paren_type": "ALTER TABLE t ADD COLUMN amount numeric(10,2)",
    }
    REFUSED = {
        "bare_drop": "ALTER TABLE t DROP COLUMN workspace_id",
        "compound_drop": "ALTER TABLE t ADD COLUMN foo text, DROP COLUMN workspace_id",
        "compound_rename": "ALTER TABLE t ADD COLUMN foo text, RENAME TO other",
        "compound_add_key": "ALTER TABLE t ADD COLUMN foo text, ADD COLUMN workspace_id uuid",
        "compound_force_rls": "ALTER TABLE t ADD COLUMN foo text, FORCE ROW LEVEL SECURITY",
    }

    @pytest.mark.parametrize("name", sorted(ADMITTED))
    def test_single_action_add_column_is_admitted(self, name):
        expected_tenancy([self.BASE, self.ADMITTED[name]])

    @pytest.mark.parametrize("name", sorted(REFUSED))
    def test_everything_else_falls_through_to_the_refusal(self, name):
        with pytest.raises(AssertionError):
            expected_tenancy([self.BASE, self.REFUSED[name]])

    def test_adding_the_tenant_key_flips_the_fact(self):
        sig = expected_tenancy(
            ["CREATE TABLE t ( id uuid )", "ALTER TABLE t ADD COLUMN workspace_id uuid"]
        )
        assert sig["t"]["tenant_keyed"] is True


class TestExpectedTenancyDerivation:
    """`expected_tenancy` — the static half of the prefix-aware lane check.

    The lane test compares a live catalog against this derivation, so a
    derivation that quietly returned `{}` would make that comparison pass on
    everything. These are the positive controls that stop it.
    """

    @staticmethod
    def _stream():
        return _real_stream()

    def test_the_full_stream_derives_the_counts_a_live_replay_measured(self):
        """CALIBRATION, and the reason this is not circular reasoning.

        `test_advertised_ddl_replay` executes the whole stream into a real
        database and observes 26 tables, 19 of them tenant-keyed. This parses
        the same stream as TEXT and must land on the same two numbers. Agreement
        between a catalog read and a text parse is what licenses using the parse
        as an expectation elsewhere; without it the derivation would only ever
        be self-consistent.
        """
        sig = expected_tenancy(self._stream())
        assert len(sig) == 26
        assert len(tenant_keyed_tables(sig)) == 19

    def test_the_completed_stream_satisfies_the_invariant_it_will_be_judged_by(self):
        """At the end of the stream — and only there — the plan's own tenancy
        invariant holds. If this ever fails, the PLAN is wrong, not a migration.
        """
        assert tenancy_violations(expected_tenancy(self._stream())) == []

    def test_the_f2_2_boundary_implies_tables_without_rls_or_policies(self):
        """The F.2.2 segment ends at stream index 22: seven tables land, four of
        them tenant-keyed, and NOT ONE of them has RLS or a policy yet. This is
        the state the old `tenant_keyed_tables(sig) == []` assertion could not
        express without going red.
        """
        sig = expected_tenancy(self._stream()[:F2_2_END])
        assert len(sig) == 7
        assert len(tenant_keyed_tables(sig)) == 4
        assert all(not e["rls_enabled"] for e in sig.values())
        assert all(e["policies"] == 0 for e in sig.values())

    def test_the_mid_stream_window_is_represented_rather_than_assumed_away(self):
        """THE LOAD-BEARING ONE. The derivation must faithfully describe a state
        the tenancy invariant REJECTS — otherwise it would be smuggling the
        invariant into the expectation, and the lane comparison could never
        catch a migration that skipped a policy.

        Measured at the F.2.6 boundary: tenant-keyed tables exist, none has a
        policy, and `tenancy_violations` over the derived state is non-empty.
        The lane test asserts EQUALITY with this, never that it is clean.
        """
        sig = expected_tenancy(self._stream()[:F2_6_END])
        assert tenant_keyed_tables(sig), "no tenant-keyed tables to be wrong about"
        violations = tenancy_violations(sig)
        assert violations, (
            "the derived mid-stream state satisfies the tenancy invariant, which"
            " means the derivation is asserting the invariant instead of"
            " describing the prefix"
        )

    def test_it_emits_the_same_keys_tenancy_signature_does(self):
        """The two producers are compared with `==`, so a shape drift on either
        side would make every lane run red for a reason unrelated to tenancy.

        Pinned to `_tenancy_entry` rather than to a literal key set, and the
        difference matters: a literal is a THIRD copy of the shape, so adding a
        field to the catalog reader alone would leave this green while every
        lane run went red. Both producers build through the constructor, so this
        asserts the routing rather than re-spelling the answer.
        """
        entry = next(iter(expected_tenancy(self._stream()).values()))
        assert set(entry) == set(_tenancy_entry(tenant_keyed=False))

    def test_an_empty_prefix_derives_nothing(self):
        assert expected_tenancy([]) == {}

    def test_the_tenant_key_column_is_what_marks_a_table(self):
        keyed = expected_tenancy([f"CREATE TABLE t ( id UUID, {TENANT_KEY} UUID )"])
        plain = expected_tenancy(["CREATE TABLE t ( id UUID, name TEXT )"])
        assert keyed["t"]["tenant_keyed"] is True
        assert plain["t"]["tenant_keyed"] is False

    def test_the_tenant_root_is_keyed_without_carrying_the_column(self):
        """`workspaces` IS the tenant — it keys on `id`. The catalog side has
        the same special case, so the derivation must too or the two disagree on
        the single most important table in the schema.
        """
        sig = expected_tenancy(["CREATE TABLE workspaces ( id UUID, name TEXT )"])
        assert sig["workspaces"]["tenant_keyed"] is True

    def test_rls_and_policies_accumulate_onto_the_table(self):
        sig = expected_tenancy(
            [
                f"CREATE TABLE t ( id UUID, {TENANT_KEY} UUID )",
                "ALTER TABLE t ENABLE ROW LEVEL SECURITY",
                "CREATE POLICY p_one ON t FOR ALL TO svc_ingress USING (true)",
                "CREATE POLICY p_two ON t FOR SELECT TO svc_ingress USING (true)",
            ]
        )
        assert sig["t"]["rls_enabled"] is True
        assert sig["t"]["policies"] == 2
        assert tenancy_violations(sig) == []

    def test_statements_about_a_table_the_prefix_has_not_created_are_ignored(self):
        """A prefix can legitimately mention nothing about a later table. What it
        must never do is invent one — a phantom entry would diverge from the
        catalog and redden the lane for a table that does not exist yet.
        """
        assert (
            expected_tenancy(["CREATE POLICY p ON not_yet FOR ALL USING (true)"]) == {}
        )

    @pytest.mark.parametrize(
        "reducing",
        [
            "DROP POLICY p_one ON t",
            "DROP TABLE t",
            f"ALTER TABLE t DROP COLUMN {TENANT_KEY}",
            "ALTER TABLE t RENAME TO t_old",
            "DROP SCHEMA public CASCADE",
            "ALTER TABLE t DISABLE ROW LEVEL SECURITY",
        ],
    )
    def test_a_state_reducing_statement_refuses_rather_than_deriving_a_wrong_state(
        self, reducing
    ):
        """The derivation only ever ADDS. A statement that takes something away
        would leave it claiming a table or policy is present that the replay has
        since dropped — the quiet direction — so it refuses.

        Parametrized deliberately: an earlier version named `DROP POLICY` and
        `DISABLE ROW LEVEL SECURITY` by regex and let every other reducing form
        fall through to a silent ignore, which is a claim about today's corpus
        wearing the shape of an enforced rule. None of these six is special; they
        are all just "not on the allowlist".
        """
        with pytest.raises(AssertionError, match="does not classify"):
            expected_tenancy(
                [f"CREATE TABLE t ( id UUID, {TENANT_KEY} UUID )", reducing]
            )

    def test_the_real_stream_is_fully_classified(self):
        """POSITIVE CONTROL for the refusal above. A gate that refuses everything
        is as useless as one that refuses nothing — this asserts the allowlist
        actually covers the corpus it has to run against, so the refusal is
        discriminating rather than merely strict.
        """
        assert len(expected_tenancy(self._stream())) == 26

    def test_an_unclassified_statement_kind_refuses(self):
        """The allowlist's other direction: a statement kind nobody has judged
        is a review event, not a silent skip. `CREATE SEQUENCE` cannot move the
        four facts today — but that is a conclusion someone has to reach and
        record, which is what adding it to `_TENANCY_IRRELEVANT` means.
        """
        with pytest.raises(AssertionError, match="does not classify"):
            expected_tenancy(["CREATE SEQUENCE s START 1"])
