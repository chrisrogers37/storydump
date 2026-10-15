"""The compensating control the ENABLE-without-FORCE design depends on (#751).

`ENABLE` without `FORCE` is deliberate: ``svc_migration`` *"owns every object,
bypasses RLS as owner by design"*, because the migration transform needs
that. What makes it safe is the RLS harness, and specifically this clause:

    the control is that svc_migration's credential exists only in the runner's
    deploy context (the gate asserts runtime env carries exactly svc_ingress
    and svc_worker)

**The gap this module closes.** The measurement the design rests on — that an
owner really does read straight through its own table's policies — is otherwise
only PROSE, in the docstring of `TestOwnerBypassPosture` in
`test_tenancy_gate.py`, taken by hand. Without an executable check the premise
could stop being true — a server upgrade, a role attribute, a different
ownership arrangement — and nothing anywhere would go red. #750's gate would
stay green, because it checks the design's *consequence* (FORCE is absent)
and never its *premise* (that absence is safe).

**Why a schema-level gate can never cover this, structurally.** The tests below
build ONE table with ONE policy and connect to it twice. The owner reads every
tenant; the runtime login reads exactly one. Nothing in the schema differs
between those two cases — the entire tenant isolation of the system is a
property of *which credential connected*, not of anything a schema signature
can see. `TestTheSchemaGateIsStructurallyBlind` asserts that directly: the
tenancy gate reports zero violations on the very table an owner is reading
across tenants through. That is not a defect in the tenancy gate (#750); it is
the reason a separate, behavioural harness has to exist.

**Measured, not predicted.** Every expectation here is observed on the real
server (PostgreSQL 15.18). Where an error is expected, the SQLSTATE is
asserted rather than the message text.

**Deliberately NOT covered here, so the boundary is stated rather than
implied** — the full RLS gate also requires, against the full schema:
definer-door confinement (a door touches only the tables its body names),
transaction-reuse context leakage, zero-NULL gates, `fn_auth_plane_sweep` as
the only auth-plane maintenance path, and the credential-locality assertion
itself (runtime env carries exactly `svc_ingress` and `svc_worker`). Those
halves are out of this harness's scope.
"""

import uuid
from contextlib import contextmanager

import psycopg2
import pytest

from scripts.tenancy_gate import tenancy_signature, tenancy_violations
from tests.scripts.conftest import (
    TEST_ACTOR_PASSWORD,
    _dsn,
    as_user,
    execute,
    fetch_one,
)

pytestmark = [pytest.mark.integration]

WS_A = "11111111-1111-1111-1111-111111111111"
WS_B = "22222222-2222-2222-2222-222222222222"

# Observed with the standalone probe on PostgreSQL 15.18, asserted by SQLSTATE
# rather than message text so a wording change upstream does not read as a
# behaviour change.
UNDEFINED_OBJECT = "42704"  # current_setting on an unset GUC
INSUFFICIENT_PRIVILEGE = "42501"  # SET ROLE / DDL as the runtime login


def _sqlstate(exc_info) -> str:
    return exc_info.value.pgcode


@pytest.fixture()
def confined(admin_conn, owner_db, owner_actor):
    """One table, one tenant policy, two ways in.

    The table owner is the **non-superuser** owner actor, deliberately: if the
    owner were a superuser, A1's bypass would be explained by `rolsuper` and
    would prove nothing about ownership. The runtime login is a plain LOGIN
    role with no membership in anything, which is what makes `SET ROLE`
    structurally impossible rather than merely forbidden.

    Teardown drops the login's in-database privileges before the role, because
    a surviving grant makes the role undroppable (`DependentObjectsStillExist`)
    and this fixture cannot reorder `owner_db`'s teardown around its own.
    """
    login = f"rls_rt_{uuid.uuid4().hex[:10]}"
    with admin_conn.cursor() as cur:
        cur.execute(f'CREATE ROLE "{login}" LOGIN PASSWORD %s', (TEST_ACTOR_PASSWORD,))

    owner_dsn = as_user(owner_db, owner_actor)
    execute(
        owner_dsn,
        "CREATE TABLE rls_probe (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
        " workspace_id uuid NOT NULL, secret text NOT NULL)",
    )
    execute(
        owner_dsn,
        "INSERT INTO rls_probe (workspace_id, secret) VALUES (%s, 'A-secret'),"
        " (%s, 'B-secret')",
        (WS_A, WS_B),
    )
    execute(owner_dsn, "ALTER TABLE rls_probe ENABLE ROW LEVEL SECURITY")
    # The tenant policy's shape: constant-expression, scoped to
    # the runtime login, identical USING and WITH CHECK.
    execute(
        owner_dsn,
        f'CREATE POLICY tenant_isolation ON rls_probe FOR ALL TO "{login}"'
        " USING (workspace_id = current_setting('app.tenant_id')::uuid)"
        " WITH CHECK (workspace_id = current_setting('app.tenant_id')::uuid)",
    )
    execute(
        owner_dsn, f'GRANT SELECT, INSERT, UPDATE, DELETE ON rls_probe TO "{login}"'
    )

    database = owner_db.rsplit("/", 1)[-1]
    yield {
        "owner": owner_dsn,
        "login_dsn": _dsn(database, user=login, password=TEST_ACTOR_PASSWORD),
        "login": login,
        "owner_role": owner_actor,
        "db": owner_db,
    }

    # Drop the TABLE, not `DROP OWNED BY` — the latter needs membership in the
    # role being cleaned and the suite's connecting user has none, so it fails
    # `permission denied to drop objects`. The table is the only object that
    # references the login (its grant and its policy are both table-scoped), so
    # dropping it as the owner removes every dependency the role has.
    execute(owner_dsn, "DROP TABLE IF EXISTS rls_probe")
    with admin_conn.cursor() as cur:
        cur.execute(f'DROP ROLE IF EXISTS "{login}"')


@contextmanager
def _session(dsn):
    """One connection held open across several statements.

    `_as` opens and closes a connection per call, which is right for every
    other test here and makes this module's leakage case inexpressible: the
    defect is a property of GUC lifetime meeting CONNECTION REUSE, and a fresh
    connection per request cannot reuse anything. This is that missing seam —
    a helper that needs none of the schema's tables.

    A pooled connection is exactly this: handed to one request, returned, then
    handed to the next without being reset.
    """
    conn = psycopg2.connect(dsn)
    try:
        yield conn
    finally:
        conn.close()


def _read(conn):
    """One 'request' on an already-open connection."""
    with conn.cursor() as cur:
        cur.execute("SELECT secret FROM rls_probe ORDER BY secret")
        return [r[0] for r in cur.fetchall()]


def _as(dsn, statements, tenant=None):
    """Run statements on one connection, optionally with the tenant GUC set."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            if tenant is not None:
                cur.execute("SET app.tenant_id = %s", (tenant,))
            for sql in statements:
                cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


class TestOwnerBypassIsRealNotAssumed:
    """The premise of the design, made executable."""

    def test_the_owner_reads_every_tenant_under_enable_without_force(self, confined):
        """A1 — the load-bearing one. RLS is enabled, a policy exists, and the
        owner reads BOTH tenants anyway. This is the design's premise; if it
        ever stops being true, the reasoning behind ENABLE-without-FORCE
        changes and someone needs to know."""
        rows = _as(confined["owner"], ["SELECT secret FROM rls_probe ORDER BY secret"])
        assert [r[0] for r in rows] == ["A-secret", "B-secret"]

    def test_force_would_stop_it_which_is_why_its_absence_is_the_control_gap(
        self, confined
    ):
        """A2 — the bypass is specifically FORCE's absence, not a broken policy.
        Without this, A1 is equally consistent with 'the policy does nothing'."""
        execute(confined["owner"], "ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY")
        rows = _as(confined["owner"], ["SELECT secret FROM rls_probe"])
        assert rows == []

    def test_the_constrained_role_can_remove_force_itself(self, confined):
        """A3 — why FORCE is not defence-in-depth here (#751). The role FORCE
        constrains removes it in one statement, so requiring it would buy
        nothing against a privileged operator. Asserting this stops the
        argument being re-litigated from memory."""
        execute(confined["owner"], "ALTER TABLE rls_probe FORCE ROW LEVEL SECURITY")
        assert _as(confined["owner"], ["SELECT secret FROM rls_probe"]) == []

        execute(confined["owner"], "ALTER TABLE rls_probe NO FORCE ROW LEVEL SECURITY")
        rows = _as(confined["owner"], ["SELECT secret FROM rls_probe ORDER BY secret"])
        assert [r[0] for r in rows] == ["A-secret", "B-secret"]


class TestTheRuntimeLoginIsConfined:
    """The other half: the posture works for the roles it is meant to bind."""

    def test_the_login_is_neither_superuser_nor_bypassrls(self, confined):
        """Positive control. Every confinement assertion below is meaningless
        if the login had an attribute that explains the result instead."""
        row = fetch_one(
            confined["login_dsn"],
            "SELECT current_user, rolsuper, rolbypassrls"
            " FROM pg_roles WHERE rolname = current_user",
        )
        assert row[0] == confined["login"], f"wrong subject answered: {row[0]}"
        assert row[1:] == (False, False)

    def test_the_owner_is_also_neither_superuser_nor_bypassrls(
        self, confined, owner_db, owner_actor
    ):
        """Owner-side analogue (#789): the owner's read-through is
        the PREMISE here, so the control that its bypass comes from OWNERSHIP —
        not from a role attribute that would explain the result trivially —
        matters more on this side than on the login's.

        Subject-identity-gated (#852): the query runs AS the owner and asserts
        who answered before asserting anything about them. Reading
        ``owner_db`` directly authenticates as the ADMIN — a real assertion
        about the wrong role, green where the admin is not superuser and red
        in CI where stock POSTGRES_USER is. A gate must verify its own
        subject."""
        row = fetch_one(
            as_user(owner_db, owner_actor),
            "SELECT current_user, rolsuper, rolbypassrls"
            " FROM pg_roles WHERE rolname = current_user",
        )
        assert row[0] == owner_actor, f"wrong subject answered: {row[0]}"
        assert row[1:] == (False, False)

    def test_an_unset_tenant_guc_raises_rather_than_returning_rows(self, confined):
        """It fails closed by ERRORING, not by returning an empty set — and the
        difference matters operationally. An empty result is indistinguishable
        from 'this tenant has no rows' and can be logged and forgotten; 42704
        cannot be mistaken for data."""
        with pytest.raises(psycopg2.errors.UndefinedObject) as exc:
            _as(confined["login_dsn"], ["SELECT secret FROM rls_probe"])
        assert _sqlstate(exc) == UNDEFINED_OBJECT

    @pytest.mark.parametrize(
        "tenant,expected", [(WS_A, ["A-secret"]), (WS_B, ["B-secret"])]
    )
    def test_the_login_sees_exactly_its_own_tenant(self, confined, tenant, expected):
        rows = _as(
            confined["login_dsn"],
            ["SELECT secret FROM rls_probe ORDER BY secret"],
            tenant=tenant,
        )
        assert [r[0] for r in rows] == expected

    def test_the_login_cannot_set_role_to_the_owner(self, confined):
        """A login that can assume every personality IS every
        personality. No membership exists, so the assumption fails in the
        database rather than being forbidden by convention."""
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc:
            _as(confined["login_dsn"], [f'SET ROLE "{confined["owner_role"]}"'])
        assert _sqlstate(exc) == INSUFFICIENT_PRIVILEGE

    @pytest.mark.parametrize(
        "statement",
        [
            "DROP POLICY tenant_isolation ON rls_probe",
            "ALTER TABLE rls_probe DISABLE ROW LEVEL SECURITY",
        ],
    )
    def test_the_login_cannot_dismantle_its_own_confinement(self, confined, statement):
        """Confinement that its subject can switch off is not confinement.
        Both routes are refused because the login does not own the table —
        which is the same fact that makes the OWNER able to do it."""
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc:
            _as(confined["login_dsn"], [statement])
        assert _sqlstate(exc) == INSUFFICIENT_PRIVILEGE


class TestTheSchemaGateIsStructurallyBlind:
    """Why the RLS harness must exist on its own, asserted rather than argued.

    This is the gap proof: #750's tenancy gate is, by construction, incapable of
    seeing this failure, because the failure leaves no trace in a schema
    signature.
    """

    def test_the_tenancy_gate_is_green_on_a_table_the_owner_reads_across(
        self, confined
    ):
        """Same table, same instant: the gate reports no violation while an
        owner connection reads both tenants' rows through it.

        A schema-level check cannot distinguish the safe deployment from the
        unsafe one, because the schemas are byte-identical — the only
        difference is which credential the process holds. That is precisely
        why the design's compensating control is credential-locality and not
        anything expressible in DDL."""
        violations = tenancy_violations(tenancy_signature(confined["db"]))
        assert [v for v in violations if "rls_probe" in v] == []

        rows = _as(confined["owner"], ["SELECT secret FROM rls_probe ORDER BY secret"])
        assert [r[0] for r in rows] == ["A-secret", "B-secret"], (
            "the gate is green and the owner is reading every tenant — the two"
            " facts coexisting is the whole content of this test"
        )


class TestTenantContextDoesNotSurviveConnectionReuse:
    """Transaction-reuse leakage, which needs none of the schema's tables.

    Every test here runs as the **runtime LOGIN**, never the owner. That is
    load-bearing for this module: the whole premise of the design is that the
    owner bypasses RLS by design, so a harness acting as the owner would prove
    nothing. Here the policy is genuinely in force, and what leaks is the
    *tenant context*, not the row filter.

    **Measured on real PostgreSQL 15.18**: where a measurement and the obvious
    prediction differ, the test says so.
    """

    def test_a_session_SET_survives_into_the_next_request_on_a_reused_connection(
        self, confined
    ):
        """THE LEAK. `SET` is session-scoped, so on a pooled connection the
        tenant context outlives the request that set it: the next request
        reads the previous tenant's rows without setting anything at all.

        This is what the harness exists to make executable. It is not a
        PostgreSQL defect — it is the documented lifetime of `SET` — which is
        exactly why it needs a test rather than a fix: nothing is broken, and
        that is what makes it easy to write a pool that leaks.
        """
        with _session(confined["login_dsn"]) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET app.tenant_id = %s", (WS_A,))
            assert _read(conn) == ["A-secret"]

            # The next request. It sets NOTHING — a pool just handed it this
            # connection.
            assert _read(conn) == ["A-secret"], (
                "expected the tenant context to persist; if this is empty the "
                "GUC lifetime has changed and the pooling guidance built on it "
                "needs revisiting"
            )

    def test_SET_LOCAL_on_a_clean_connection_fails_CLOSED_after_commit(self, confined):
        """The remedy, and the SQLSTATE is measured rather than predicted.

        The obvious prediction is `42704` (undefined_object). Measured:
        **22P02**, invalid_text_representation. Once a custom GUC has been set
        even transaction-locally, the placeholder is registered, so after the
        transaction it reads back as an EMPTY STRING rather than being
        undefined — and it is the `::uuid` cast that fails, not the lookup.

        Either way it fails closed, which is the property that matters. The
        code is asserted because a reworded expectation is how a fail-closed
        test quietly becomes a fail-open one.
        """
        with _session(confined["login_dsn"]) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.tenant_id = %s", (WS_B,))
            assert _read(conn) == ["B-secret"]
            conn.commit()

            with pytest.raises(psycopg2.Error) as excinfo:
                _read(conn)
            assert excinfo.value.pgcode == "22P02", (
                f"expected a closed failure, got pgcode={excinfo.value.pgcode}"
            )
            conn.rollback()

    def test_SET_LOCAL_after_a_session_SET_reverts_to_the_PREVIOUS_TENANT(
        self, confined
    ):
        """THE CASE A CLEAN CONNECTION HIDES, and the dangerous one.

        `SET LOCAL` is described as the safe form, and on a clean connection it
        is. But it does not *unset* — it restores whatever the session had.
        So on a connection where any earlier request issued a plain `SET`,
        the transaction-scoped value reverts on commit to the EARLIER
        TENANT'S id, and the next read returns that tenant's rows with **no
        error at all**.

        That is strictly worse than the case on a clean connection: that one
        fails closed and is loud, this one succeeds and is silent. A pool
        that adopts `SET LOCAL` believing it is sufficient, without also
        guaranteeing no session-level `SET` ever touched the connection, is
        still cross-tenant leaky.
        """
        with _session(confined["login_dsn"]) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:  # tenant A's request, session-scoped
                cur.execute("SET app.tenant_id = %s", (WS_A,))

            conn.autocommit = False
            with conn.cursor() as cur:  # tenant B's request, transaction-scoped
                cur.execute("SET LOCAL app.tenant_id = %s", (WS_B,))
            assert _read(conn) == ["B-secret"]
            conn.commit()

            assert _read(conn) == ["A-secret"], (
                "SET LOCAL reverted to something other than the session value; "
                "the leak shape has changed and the guidance needs re-deriving"
            )

    def test_a_fresh_connection_carries_no_context_which_is_the_control(self, confined):
        """The control. If a brand-new connection already saw rows, these tests
        would be measuring something other than context reuse — and the two
        leak assertions above would pass for the wrong reason."""
        with _session(confined["login_dsn"]) as conn:
            with pytest.raises(psycopg2.Error):
                _read(conn)
