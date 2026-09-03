"""L.0 unit-of-work gates (#857, `04` §L.0).

Four of the five gate clauses live here; the floor proofs are in
`test_egress_floor.py`. The pool-reuse clause is DB-backed and marked
integration — it is the one that cannot be faked, because GUC leakage is a
property of a real connection being handed to a second borrower.
"""

from __future__ import annotations


import pytest
from sqlalchemy import text

from src.config.settings import settings
from src.services.target.unit_of_work import (
    CONNECTION_ROLE_SQL,
    connection_role,
    MAX_OVERFLOW_SEAM,
    POOL_RECYCLE_SEAM,
    POOL_SIZE_SEAM,
    POOL_TIMEOUT_SEAM,
    TenantContextRequired,
    UnitOfWork,
    async_database_url,
    create_engine,
    in_transaction,
    unit_of_work,
)

TENANT_A = "aaaaaaaa-0000-0000-0000-00000000000a"
TENANT_B = "bbbbbbbb-0000-0000-0000-00000000000b"


class TestTheEngineConfigAssertsTheSeam:
    """Gate: *engine config asserts `max_overflow` equals its `05` seam value.*"""

    def test_the_seam_constant_is_zero(self):
        assert MAX_OVERFLOW_SEAM == 0

    def test_the_engine_pins_max_overflow_to_the_seam(self):
        engine = create_engine("postgresql+asyncpg://u:p@localhost:5432/none")
        assert engine.pool._max_overflow == MAX_OVERFLOW_SEAM

    def test_the_seam_is_NOT_read_from_settings(self, monkeypatch):
        """R4's finding, pinned BEHAVIOURALLY rather than by proxy.

        The first version asserted `settings.DB_MAX_OVERFLOW != SEAM`, which
        inverts the day someone remediates R4's finding by setting the config
        to 0 — the test went red at exactly the moment the config became
        right, in an unrelated PR authored by someone with no L.0 context. A
        check that fails when the defect it guards is fixed is not pinning it.

        Driving the setting to an arbitrary value and asserting the engine
        still shows the literal proves "not read from settings" for ANY value,
        including 0, and can never invert."""
        monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 99)
        engine = create_engine("postgresql+asyncpg://u:p@localhost:5432/none")
        assert engine.pool._max_overflow == MAX_OVERFLOW_SEAM

    @pytest.mark.parametrize("setting_value", [0, 20, 99])
    def test_it_holds_for_every_setting_value_including_the_remediated_one(
        self, monkeypatch, setting_value
    ):
        """0 is in the set deliberately — it is the value that broke the old
        form, so it is the one worth pinning."""
        monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", setting_value)
        engine = create_engine("postgresql+asyncpg://u:p@localhost:5432/none")
        assert engine.pool._max_overflow == MAX_OVERFLOW_SEAM

    def test_the_saturation_policy_is_pinned_not_left_to_the_default(self):
        """branden's finding. With `max_overflow=0` the pool cannot burst, so
        `pool_timeout` IS the saturation policy — and SQLAlchemy's 30 s default
        is pile-up, not the `01` H5 "slip-a-slot" the plan specifies. It is also
        >= the egress floor's own budget, so the substrate could outlast the
        callers it serves."""
        engine = create_engine("postgresql+asyncpg://u:p@localhost:5432/none")
        assert engine.pool._timeout == POOL_TIMEOUT_SEAM
        assert POOL_TIMEOUT_SEAM < 10, "the seam must defer, not stall"

    def test_the_connection_wait_is_shorter_than_the_shortest_provider_timeout(self):
        """The coupling that makes 30 s wrong rather than merely large: a
        caller must never spend its provider budget waiting for a connection."""
        from src.services.target.egress import TIMEOUT_CLASSES

        assert POOL_TIMEOUT_SEAM < min(TIMEOUT_CLASSES.values())

    def test_pool_recycle_is_carried_across_from_the_legacy_engine(self):
        """`src/config/database.py` sets 300 explicitly ALONGSIDE pre-ping.
        Dropping to -1 (never) in the target engine would be a regression
        nobody chose."""
        engine = create_engine("postgresql+asyncpg://u:p@localhost:5432/none")
        assert engine.pool._recycle == POOL_RECYCLE_SEAM

    def test_pool_size_is_pinned_too_so_the_invariant_cannot_be_overridden(self):
        """Half a pinned inequality is not pinned. With `pool_size` read from
        settings, a production `DB_POOL_SIZE` override breaks
        `Σ(replica × (pool + overflow)) = 50` and no gate can see it — the test
        would read the same overridden value it is meant to be checking."""
        monkeypatch_free = create_engine("postgresql+asyncpg://u:p@localhost:5432/none")
        assert monkeypatch_free.pool.size() == POOL_SIZE_SEAM

    def test_the_invariant_arithmetic_matches_05(self):
        """`05`: Σ(replica × (pool + overflow)) = 3×(10+0) + 2×(10+0) = 50."""
        # From the SEAM literals, not from settings: reading the setting here
        # means CI only ever sees its default, so a production override would
        # break the invariant with the gate still green.
        per_replica = POOL_SIZE_SEAM + MAX_OVERFLOW_SEAM
        assert 3 * per_replica + 2 * per_replica == 50


class TestAUoWWithoutTenantContextIsUnconstructible:
    """Gate: *a UoW without tenant context is unconstructible.*

    Unconstructible, not "raises on use" — the object must not come into
    existence, so no code path can hold one and reach a query with it.
    """

    @pytest.mark.parametrize("bad", [None, "", 0, [], {}, "   ", "\n", "\t", " \n\t "])
    def test_construction_refuses_every_absent_tenant(self, bad):
        with pytest.raises(TenantContextRequired):
            UnitOfWork(engine=None, tenant_id=bad)

    def test_the_factory_refuses_too(self):
        with pytest.raises(TenantContextRequired):
            unit_of_work(None, "")

    def test_there_is_no_system_scope_door(self):
        """`04` §L.0: *no role selection exists to make — system operations go
        through the §7 doors.* A cross-tenant UoW variant would bypass the
        audited doors, so its absence is asserted rather than assumed."""
        import src.services.target.unit_of_work as mod

        surface = dir(mod)
        for forbidden in ("SYSTEM_SCOPE", "SystemScope", "system_unit_of_work"):
            assert forbidden not in surface, (
                f"{forbidden} appeared on the L.0 surface — system operations go "
                "through the `02` §7 doors, not through a privileged UoW"
            )

    def test_a_real_tenant_constructs(self):
        """Positive control: the refusals above are about absence."""
        assert UnitOfWork(engine=None, tenant_id=TENANT_A).tenant_id == TENANT_A


class TestTheDisciplineFlagDoesNotLeak:
    """The ContextVar is reset from its token, never by assignment.

    A leaked flag makes every later provider call in the task raise — a
    wrong-way failure that looks like the discipline working, which is why it
    is asserted rather than reasoned about. CI runs 3.10 and this repo has
    already had a ContextVar leak that only tripped there, so CI is the
    arbiter for this class, not a local pass.
    """

    def test_the_flag_is_false_at_rest(self):
        assert in_transaction() is False

    @pytest.mark.asyncio
    async def test_the_flag_is_false_again_after_a_failed_transaction(self):
        engine = create_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none")
        uow = UnitOfWork(engine=engine, tenant_id=TENANT_A)
        with pytest.raises(Exception):
            async with uow.begin():
                pass
        assert in_transaction() is False, "the discipline flag leaked past a failure"
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
class TestTenantScopingAndGucHygieneUnderPoolReuse:
    """Gate: *harness proves tenant-scoping + GUC hygiene under pool reuse.*

    The pool is deliberately tiny so connections are genuinely recycled — with
    a large pool each UoW could get a fresh connection and the test would pass
    without ever exercising reuse, which is the vacuity this gate is about.
    """

    @pytest.fixture(autouse=True)
    def _require_db(self, setup_test_database):
        """The suite's own session-DB fixture: creates `TEST_DB_NAME` and skips
        cleanly when no database is reachable. Depending on it is what makes
        these tests run against the database CI actually provisions — CI has no
        `DB_NAME` database, so building the URL from that name fails there
        while passing locally, which is exactly what it did on the first push."""
        if setup_test_database is None:
            pytest.skip("Database not available - skipping integration test")

    @pytest.mark.asyncio
    async def test_each_uow_sees_only_its_own_tenant_across_recycled_connections(self):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            async_database_url(settings.TEST_DB_NAME), pool_size=1, max_overflow=0
        )
        try:
            seen = []
            for tenant in (TENANT_A, TENANT_B, TENANT_A, TENANT_B):
                async with unit_of_work(engine, tenant).begin() as s:
                    got = (
                        await s.execute(text("SELECT current_setting('app.tenant_id')"))
                    ).scalar()
                    seen.append(got)
            assert seen == [TENANT_A, TENANT_B, TENANT_A, TENANT_B]
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_guc_does_not_survive_into_the_next_borrower(self):
        """The hygiene half: after a UoW commits, a plain connection off the
        SAME single-slot pool must see no tenant at all."""
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            async_database_url(settings.TEST_DB_NAME), pool_size=1, max_overflow=0
        )
        try:
            async with unit_of_work(engine, TENANT_A).begin() as s:
                assert (
                    await s.execute(text("SELECT current_setting('app.tenant_id')"))
                ).scalar() == TENANT_A
            async with engine.connect() as c:
                leaked = (
                    await c.execute(
                        text("SELECT current_setting('app.tenant_id', true)")
                    )
                ).scalar()
            assert not leaked, f"app.tenant_id leaked across pool reuse: {leaked!r}"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_actor_gucs_are_set_and_are_also_transaction_scoped(self):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            async_database_url(settings.TEST_DB_NAME), pool_size=1, max_overflow=0
        )
        try:
            uow = unit_of_work(
                engine, TENANT_A, actor_kind="system", actor_user_id=None, channel="web"
            )
            async with uow.begin() as s:
                assert (
                    await s.execute(text("SELECT current_setting('app.actor_kind')"))
                ).scalar() == "system"
                assert (
                    await s.execute(text("SELECT current_setting('app.channel')"))
                ).scalar() == "web"
            async with engine.connect() as c:
                for guc in ("app.actor_kind", "app.channel"):
                    assert not (
                        await c.execute(text(f"SELECT current_setting('{guc}', true)"))
                    ).scalar(), f"{guc} leaked across pool reuse"
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_COMPOSED_path_refuses_a_provider_call_inside_a_real_uow(self):
        """branden's finding: the two halves were each proven and the JOIN was
        not. `test_a_provider_call_inside_an_open_transaction_fails` sets the
        ContextVar by hand, and the flag test proves a UoW sets it — but no
        test ran the shape a caller actually writes. Each half can keep passing
        while the join breaks, so this runs a real UoW transaction against live
        Postgres with a real `egress.request` through it."""
        import httpx
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.egress import EgressPolicy
        from src.services.target.egress import request as egress_request
        from src.services.target.unit_of_work import TransactionDisciplineError

        engine = create_async_engine(
            async_database_url(settings.TEST_DB_NAME), pool_size=1, max_overflow=0
        )
        try:
            transport = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
            async with httpx.AsyncClient(transport=transport) as client:
                async with unit_of_work(engine, TENANT_A).begin():
                    with pytest.raises(TransactionDisciplineError):
                        await egress_request(
                            client,
                            "GET",
                            "https://graph.instagram.com/v1/me",
                            policy=EgressPolicy(),
                            resolver=lambda h: ["93.184.216.34"],
                        )
                # ... and the same call OUTSIDE the transaction goes out, so the
                # refusal above is the discipline rather than a broken call.
                resp = await egress_request(
                    client,
                    "GET",
                    "https://graph.instagram.com/v1/me",
                    policy=EgressPolicy(),
                    resolver=lambda h: ["93.184.216.34"],
                )
                assert resp.status_code == 200
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_a_task_spawned_INSIDE_the_transaction_is_also_refused(self):
        """The per-task bound, as an assertion rather than a docstring claim:
        a child task inherits the context, so it is caught too."""
        import asyncio as aio

        import httpx
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.services.target.egress import EgressPolicy
        from src.services.target.egress import request as egress_request
        from src.services.target.unit_of_work import TransactionDisciplineError

        engine = create_async_engine(
            async_database_url(settings.TEST_DB_NAME), pool_size=1, max_overflow=0
        )
        try:
            transport = httpx.MockTransport(lambda r: httpx.Response(200, text="ok"))
            async with httpx.AsyncClient(transport=transport) as client:
                async with unit_of_work(engine, TENANT_A).begin():

                    async def child():
                        return await egress_request(
                            client,
                            "GET",
                            "https://graph.instagram.com/v1/me",
                            policy=EgressPolicy(),
                            resolver=lambda h: ["93.184.216.34"],
                        )

                    with pytest.raises(TransactionDisciplineError):
                        await aio.create_task(child())
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_the_discipline_flag_is_set_inside_and_clear_outside(self):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(
            async_database_url(settings.TEST_DB_NAME), pool_size=1, max_overflow=0
        )
        try:
            assert in_transaction() is False
            async with unit_of_work(engine, TENANT_A).begin():
                assert in_transaction() is True
            assert in_transaction() is False
        finally:
            await engine.dispose()


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Conn:
    """A connection that answers the role query with a fixed row, or raises."""

    def __init__(self, row=None, raises=None):
        self._row, self._raises, self.statements = row, raises, []

    async def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        if self._raises is not None:
            raise self._raises
        return _Result(self._row)


class TestConnectionRole:
    """`connection_role` is the one place that asks a live connection who it is.

    It exists for the F.4 rollout (#751): production connects as the owner role
    with `BYPASSRLS`, which makes every tenant policy inert, and nothing in the
    estate could SAY so. The switch to the runtime logins is verified by reading
    this back, so it has to be honest and it has to be harmless.
    """

    async def test_it_reports_the_login_and_whether_it_bypasses_rls(self):
        conn = _Conn(row=("svc_ingress", False))
        assert await connection_role(conn) == {
            "user": "svc_ingress",
            "bypassrls": False,
        }

    async def test_it_asks_postgres_not_the_config(self):
        """The answer has to come from the catalog: a role name copied from the
        URL would report what was CONFIGURED, and the whole point is to detect
        a deployment where the two disagree."""
        conn = _Conn(row=("neondb_owner", True))
        await connection_role(conn)
        assert "current_user" in conn.statements[0]
        assert "rolbypassrls" in conn.statements[0]
        assert (
            "current_user" in CONNECTION_ROLE_SQL
            and "rolbypassrls" in CONNECTION_ROLE_SQL
        )

    async def test_a_failing_sample_is_none_never_an_exception(self):
        """A diagnostic must not take its caller down: the API samples this at
        startup and the worker on its election connection, and neither may die
        because a catalog read failed."""
        conn = _Conn(raises=RuntimeError("connection reset"))
        assert await connection_role(conn) is None

    async def test_an_empty_answer_is_none(self):
        assert await connection_role(_Conn(row=None)) is None
