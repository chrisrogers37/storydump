"""L.0 unit-of-work gates (#857, `04` §L.0).

Four of the five gate clauses live here; the floor proofs are in
`test_egress_floor.py`. The pool-reuse clause is DB-backed and marked
integration — it is the one that cannot be faked, because GUC leakage is a
property of a real connection being handed to a second borrower.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from src.config.settings import settings
from src.services.target.unit_of_work import (
    MAX_OVERFLOW_SEAM,
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

    def test_the_seam_is_NOT_read_from_settings(self):
        """R4's finding, pinned. `settings.DB_MAX_OVERFLOW` is 20 on this repo,
        which silently makes the true ceiling (10+20)×5 = 150 rather than 50.
        The async engine must ignore it — if this ever starts matching, someone
        has wired the seam to the config and re-opened the defect."""
        assert settings.DB_MAX_OVERFLOW != MAX_OVERFLOW_SEAM, (
            "settings.DB_MAX_OVERFLOW now equals the seam, so this test can no "
            "longer tell 'pinned to the seam' from 'read from settings'. Pin the "
            "assertion to the literal instead of deleting it."
        )
        engine = create_engine("postgresql+asyncpg://u:p@localhost:5432/none")
        assert engine.pool._max_overflow != settings.DB_MAX_OVERFLOW

    def test_the_invariant_arithmetic_matches_05(self):
        """`05`: Σ(replica × (pool + overflow)) = 3×(10+0) + 2×(10+0) = 50."""
        per_replica = settings.DB_POOL_SIZE + MAX_OVERFLOW_SEAM
        assert 3 * per_replica + 2 * per_replica == 50


class TestAUoWWithoutTenantContextIsUnconstructible:
    """Gate: *a UoW without tenant context is unconstructible.*

    Unconstructible, not "raises on use" — the object must not come into
    existence, so no code path can hold one and reach a query with it.
    """

    @pytest.mark.parametrize("bad", [None, "", 0, [], {}])
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

    @pytest.fixture()
    def engine(self):
        eng = create_engine(async_database_url())
        yield eng
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            eng.dispose()
        )

    @pytest.mark.asyncio
    async def test_each_uow_sees_only_its_own_tenant_across_recycled_connections(self):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(async_database_url(), pool_size=1, max_overflow=0)
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

        engine = create_async_engine(async_database_url(), pool_size=1, max_overflow=0)
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

        engine = create_async_engine(async_database_url(), pool_size=1, max_overflow=0)
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
    async def test_the_discipline_flag_is_set_inside_and_clear_outside(self):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(async_database_url(), pool_size=1, max_overflow=0)
        try:
            assert in_transaction() is False
            async with unit_of_work(engine, TENANT_A).begin():
                assert in_transaction() is True
            assert in_transaction() is False
        finally:
            await engine.dispose()
