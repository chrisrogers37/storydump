"""F.2.1 — the step-0 window bootstrap, and why roles are not a migration (#746).

The F.2 split filed on #746 put role creation in a substrate *migration*. That
was wrong, and the plan says so in three places (`02` §7-DDL's opening line,
D40, `04`'s F.2 entry): the advertised stream runs as `svc_migration`, which
must not hold role DDL's cluster privileges. Roles come from the `04` step-0
bootstrap, run by the database-owner actor.

Measured on PostgreSQL 15.18 — the version CI pins — this is not a preference:

    CREATE POLICY p ON t TO svc_ingress USING (true);
      -> ERROR: role "svc_ingress" does not exist

RLS *enablement* needs no roles; a policy naming one does. Every table PR from
F.2.2 on lands its policies in the same increment by construction, so the seven
roles must exist before the first policy-carrying table can replay at all. That
is what this artifact delivers, and `test_a_policy_naming_a_service_role`
below is the two-sided proof of it.
"""

import psycopg2
import pytest

from tests.scripts.conftest import (
    LOGIN_ROLES,
    NOLOGIN_ROLES,
    SERVICE_ROLES,
    BOOTSTRAP_SQL,
    SUITE_CLUSTER_LOCK_KEY,
    bootstrap_via_psql,
    drop_service_roles,
    psql_apply,
    run_bootstrap,
)


def _roles(dsn):
    """{rolname: rolcanlogin} for the service roles actually present."""
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)",
            (list(SERVICE_ROLES),),
        )
        return dict(cur.fetchall())


def _memberships(dsn, member):
    """Roles `member` belongs to — the window transients D40 bounds."""
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_auth_members m"
            "  JOIN pg_roles r ON r.oid = m.roleid"
            "  JOIN pg_roles g ON g.oid = m.member"
            " WHERE g.rolname = %s",
            (member,),
        )
        return {row[0] for row in cur.fetchall()}


class TestRoleProvisioning:
    def test_provisions_the_seven_roles_with_the_declared_login_split(
        self, bootstrapped_db
    ):
        """The inventory is seven, and the split is load-bearing: a NOLOGIN door
        owner that could log in would be a cross-tenant capability with a
        password, which is the thing `02` §7's model exists to exclude."""
        present = _roles(bootstrapped_db)

        assert set(present) == set(SERVICE_ROLES), (
            f"expected the seven `02` §7 service roles, got {sorted(present)}"
        )
        assert [present[r] for r in LOGIN_ROLES] == [True, True, True]
        assert [present[r] for r in NOLOGIN_ROLES] == [False, False, False, False]

    def test_is_idempotent_across_reruns(self, admin_conn, bootstrapped_db):
        """The artifact is the one place guarded DDL is legal (`02` §0: it is
        not the byte-parity stream). A re-run is the M.3 retry path, and it must
        pass through pre-existing roles untouched rather than erroring."""
        before = _roles(bootstrapped_db)

        run_bootstrap(admin_conn, bootstrapped_db)
        run_bootstrap(admin_conn, bootstrapped_db)

        assert _roles(bootstrapped_db) == before

    def test_grants_svc_migration_the_window_transient_memberships(
        self, bootstrapped_db
    ):
        """D40's one bounded exception to the no-memberships invariant: the four
        NOLOGIN door owners, because `ALTER … OWNER TO` demands the executor be
        a member of the receiving role. Revoked at step 8 — which is M.3's
        filing, not F.2's, and is deliberately not asserted here."""
        held = _memberships(bootstrapped_db, "svc_migration")

        assert set(NOLOGIN_ROLES) <= held, (
            f"svc_migration missing door-owner memberships: {set(NOLOGIN_ROLES) - held}"
        )

    def test_applies_through_psql_the_way_the_window_will_run_it(
        self, admin_conn, roleless_db
    ):
        """The other tests execute the file through psycopg2 to read RAISE
        messages. This one proves the same bytes apply through `psql
        -v ON_ERROR_STOP=1`, which is how a runbook step actually invokes it."""
        dsn, _ = roleless_db

        bootstrap_via_psql(admin_conn, dsn)

        assert set(_roles(dsn)) == set(SERVICE_ROLES)


class TestPublicOwnerPrecondition:
    def test_halts_when_public_owner_is_not_what_the_abandon_variant_restores(
        self, admin_conn, roleless_db
    ):
        """R7's shape one level up: the abandon variant restores `public` to
        `pg_database_owner`, and a gate written from a constant cannot catch the
        constant being wrong. A customized owner must halt at window prep — on
        an M.2 branch — never mis-restore later.

        This is the artifact's only refusal, so it is also the proof that the
        artifact can fail at all.
        """
        dsn, extra = roleless_db
        extra.append("probe_odd_owner")
        with admin_conn.cursor() as cur:
            # Residue from an interrupted run must not fail this test for the
            # wrong reason — roles are cluster-scoped and outlive a crash.
            cur.execute("DROP ROLE IF EXISTS probe_odd_owner")
            cur.execute("CREATE ROLE probe_odd_owner NOLOGIN")
            cur.execute("GRANT probe_odd_owner TO CURRENT_USER")

        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("ALTER SCHEMA public OWNER TO probe_odd_owner")
        conn.close()

        with pytest.raises(psycopg2.errors.RaiseException) as caught:
            run_bootstrap(admin_conn, dsn)

        assert "probe_odd_owner" in str(caught.value)
        assert "not the pg_database_owner" in str(caught.value)


class TestTheReasonThisPrLandsFirst:
    def test_a_policy_naming_a_service_role_needs_the_bootstrap(
        self, admin_conn, roleless_db
    ):
        """Two-sided, in one test, because either half alone proves nothing.

        The negative half is what makes this non-inert: without it, a test that
        only creates a policy after the bootstrap would pass just as happily on
        a cluster where roles were never needed. The positive half is the F.2.2
        unblock. Deliberately the real role name — a synthetic one would
        demonstrate PostgreSQL's behaviour, not this artifact's effect.
        """
        dsn, _ = roleless_db

        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE t (id uuid PRIMARY KEY, workspace_id uuid NOT NULL)"
            )
            # Enablement alone is role-free — so a table CAN be born
            # RLS-enabled and policy-less, which is exactly the hole F.2.0's
            # tenancy gate exists to catch.
            cur.execute("ALTER TABLE t ENABLE ROW LEVEL SECURITY")

            with pytest.raises(psycopg2.errors.UndefinedObject) as caught:
                cur.execute("CREATE POLICY p ON t TO svc_ingress USING (true)")
            assert 'role "svc_ingress" does not exist' in str(caught.value)
        conn.close()

        run_bootstrap(admin_conn, dsn)

        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE POLICY p ON t TO svc_ingress USING (true)")
            cur.execute(
                "SELECT count(*) FROM pg_policies"
                " WHERE schemaname = 'public' AND tablename = 't'"
            )
            assert cur.fetchone()[0] == 1
        conn.close()


class TestTheClusterMutexIsVerifiedNotDeclared:
    """#808 — the CREATE side took a bare DSN, so it was covered by CALL PATH.

    Coverage held: every DSN in the suite came from `_create_db(admin_conn, ...)`,
    so every caller was already inside the mutex. A property held by call path
    holds until someone adds a caller, and nothing goes red when they do.

    Putting the connection in the signature only makes the dependency DECLARED.
    These assert it is CHECKED, which is the difference between a marker
    argument a later author can delete in silence and a door that refuses.
    """

    @pytest.mark.parametrize(
        "door", [run_bootstrap, bootstrap_via_psql], ids=lambda f: f.__name__
    )
    def test_a_non_holding_connection_is_refused_at_the_create_doors(
        self, outsider_conn, roleless_db, door
    ):
        """The failure #808 describes, reproduced: reach the cluster-scoped
        roles with a connection that is outside the mutex.

        Bound to the SPECIFIC failure, not to any failure. A bare `RuntimeError`
        is equally raised by a typo, a closed connection or a bad DSN — and
        `outsider_conn` is deliberately none of those — so the message must name
        both the door and the key before this counts as the lock refusing.
        """
        dsn, _ = roleless_db

        with pytest.raises(RuntimeError) as caught:
            door(outsider_conn, dsn)

        assert door.__name__ in str(caught.value)
        assert str(SUITE_CLUSTER_LOCK_KEY) in str(caught.value)
        present = _roles(dsn)
        assert present == {}, (
            f"{door.__name__} raised but still reached the cluster: {sorted(present)}"
        )

    def test_a_non_holding_connection_is_refused_at_the_drop_door(
        self, outsider_conn, bootstrapped_db
    ):
        """The DROP side, on a database where the roles EXIST.

        Deliberately not folded into the parametrize above: against
        `roleless_db` the seven roles are already absent, so "they are still
        absent afterwards" is true whatever the guard does — a vacuous
        assertion, and this is a PR about vacuous assertions. Starting from
        `bootstrapped_db` makes survival an observation.
        """
        before = _roles(bootstrapped_db)
        assert set(before) == set(SERVICE_ROLES), "positive control: roles present"

        with pytest.raises(RuntimeError) as caught:
            drop_service_roles(outsider_conn)

        assert "_drop_roles_hardened" in str(caught.value)
        assert str(SUITE_CLUSTER_LOCK_KEY) in str(caught.value)
        assert _roles(bootstrapped_db) == before, "the refusal did not hold"

    def test_psql_apply_turns_the_cluster_scoped_artifact_away(self, roleless_db):
        """The third transport. `psql_apply` holds a DSN, not a connection, so
        it cannot check the mutex — it refuses the artifact by name instead,
        which is what stops a future caller reopening the hole through it."""
        dsn, _ = roleless_db

        with pytest.raises(RuntimeError, match="bootstrap_via_psql"):
            psql_apply(dsn, [BOOTSTRAP_SQL])

        assert _roles(dsn) == {}

    def test_the_holding_connection_is_admitted(self, admin_conn, roleless_db):
        """The positive control, and it is not optional.

        Without it, every assertion above is equally satisfied by a guard that
        refuses EVERYTHING — including one that fires on `admin_conn` and takes
        the whole suite down. This is what says the discriminator is the lock
        rather than the parameter's existence.
        """
        dsn, _ = roleless_db

        run_bootstrap(admin_conn, dsn)

        assert set(_roles(dsn)) == set(SERVICE_ROLES)
