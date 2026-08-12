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

from scripts.tenancy_gate import (
    tenancy_signature,
    tenancy_violations,
    TENANT_KEY,
)


def _sig(**tables):
    """Synthetic signature: {table: (has_tenant_key, rls_enabled, policy_count)}."""
    return {
        name: {"tenant_keyed": t, "rls_enabled": r, "policies": p}
        for name, (t, r, p) in tables.items()
    }


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


class TestAgainstTheRealReplay:
    @pytest.mark.integration
    def test_replayed_corpus_has_no_tenancy_violations(self, scratch_db):
        from tests.scripts.test_migration_gate import (
            MIGRATIONS_DIR,
            SETUP_SQL,
            apply_pending,
            psql_apply,
        )

        psql_apply(scratch_db, [SETUP_SQL])
        apply_pending(scratch_db, MIGRATIONS_DIR)
        sig = tenancy_signature(scratch_db)
        assert tenancy_violations(sig) == []

    @pytest.mark.integration
    def test_states_its_own_coverage_rather_than_passing_silently(self, scratch_db):
        """NON-VACUITY DISCLOSURE. Until F.2.2 lands the first workspace-keyed
        table, the corpus check above has nothing to examine and passes on an
        empty set. That is a true result and useless coverage, so the count is
        asserted rather than left implicit: when it reaches zero-plus-one this
        test is updated deliberately, and until then nobody can mistake the
        green above for enforcement.
        """
        from tests.scripts.test_migration_gate import (
            MIGRATIONS_DIR,
            SETUP_SQL,
            apply_pending,
            psql_apply,
        )

        psql_apply(scratch_db, [SETUP_SQL])
        apply_pending(scratch_db, MIGRATIONS_DIR)
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
