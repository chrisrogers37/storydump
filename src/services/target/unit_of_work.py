"""L.0 — the final async unit of work (#857, `04` §L.0).

The transaction substrate every later Phase L increment runs on. Three things
live here because they are one contract, not three: the async engine, the
tenant-scoped unit of work, and the transaction-per-checkpoint discipline that
the egress floor enforces from the other side.

## `max_overflow` is pinned to 0, and the number is READ not chosen

`05`'s tasks-vs-connections paragraph carries the arithmetic and the R4
finding, and this module does not re-derive either. Verbatim: *"the inequality
counts `pool_size + max_overflow`, and `max_overflow` is pinned to 0 at L.0"* —
because a pool slot is an asyncio task rather than a connection, `pool_size` is
already sized for the spike case, so overflow un-bounds the invariant for
nothing. The invariant it protects:

    Σ(replica × (pool + overflow)) = 3×(10+0) + 2×(10+0) = 50  ≥  peak DB-active tasks

**This is deliberately NOT read from `settings.DB_MAX_OVERFLOW`.** That setting
is `20` on this repo today, which is exactly R4's finding: it silently makes
the true ceiling (10+20)×5 = 150 rather than 50. The legacy sync engine in
`src/config/database.py` still reads it and is **left alone** — retiring it is
M.3's, not L.0's. Pinning the async engine to the seam constant is what keeps
the target substrate correct while the legacy one is still running, and the
gate asserts the constant rather than trusting the config.

## The UoW is unconstructible without a tenant

Not "raises on use" — unconstructible. `02` §7's RLS reads `app.tenant_id`, so
a UoW that could exist without one would be a widened query waiting for a
caller, which is the failure F.1 retired at the repository layer and this
retires at the transaction layer.

**No role selection exists to make.** `04` §L.0 says so outright: system
operations go through the `02` §7 `SECURITY DEFINER` doors, not through a
privileged UoW variant. So there is no `SYSTEM_SCOPE` analogue here and none
should be added — a cross-tenant door on this surface would bypass the doors
that exist precisely to be the audited way through.

## Transaction-per-checkpoint, enforced rather than documented

`02` §5, normative: *"a database transaction never spans a provider call.
Pipelines run transaction-per-checkpoint — open, write the checkpoint/permit,
commit, then talk to the provider."* It is both the correctness seam (the §6
permit protocol depends on permit-commit-before-call) and the premise of the
connection arithmetic above.

Enforcement is a ContextVar flag set while a UoW transaction is open, which the
egress floor checks before any provider call. The flag is reset from a token in
a `finally`, never by assignment: a bare reset loses the previous value under
nesting, and a leaked flag would make every later provider call in the same
task raise — a wrong-way failure that looks like the discipline working.
"""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config.settings import settings
from src.exceptions.base import StorydumpError

#: `05` seam value. Pinned, not configurable, not read from settings — see the
#: module docstring for why `settings.DB_MAX_OVERFLOW` is deliberately ignored.
MAX_OVERFLOW_SEAM = 0

#: True while a UoW transaction is open in this task. Read by the egress floor.
_IN_TRANSACTION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "storydump_in_transaction", default=False
)


class TenantContextRequired(StorydumpError):
    """A unit of work was requested without a tenant. Never widened."""


class TransactionDisciplineError(StorydumpError):
    """A provider call was attempted inside an open transaction (`02` §5)."""


def in_transaction() -> bool:
    """Whether this task currently holds an open UoW transaction."""
    return _IN_TRANSACTION.get()


def async_database_url(database: Optional[str] = None) -> str:
    """The asyncpg URL, built from the same settings the sync engine uses.

    *database* overrides the database name only. It exists for the test
    harness, which runs against the session-scoped test database rather than
    `settings.DB_NAME` — CI provisions `TEST_DB_NAME` and has no `DB_NAME`
    database at all, so a URL hardcoded to the latter passes locally (where a
    dev database happens to exist) and fails there. Production passes nothing.
    """
    return (
        f"postgresql+asyncpg://{settings.DB_USER}:{settings.DB_PASSWORD}"
        f"@{settings.DB_HOST}:{settings.DB_PORT}/{database or settings.DB_NAME}"
    )


def create_engine(url: Optional[str] = None) -> AsyncEngine:
    """The async engine, with `max_overflow` pinned to the `05` seam."""
    return create_async_engine(
        url or async_database_url(),
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=MAX_OVERFLOW_SEAM,
        pool_pre_ping=True,
    )


class UnitOfWork:
    """A tenant-scoped async transaction.

    Construction requires a tenant id. `actor_kind` is optional at construction
    and required by the DATABASE for the `02` §7 covered tables — the triggers
    refuse an anonymous write, so this module does not duplicate that check.
    What it does is make setting the GUC easy and uniform, so a caller never
    has to remember the statement.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        tenant_id: str,
        *,
        actor_kind: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        channel: Optional[str] = None,
    ):
        if not tenant_id or not isinstance(tenant_id, str):
            raise TenantContextRequired(
                "a unit of work requires a tenant id — `02` §7 RLS reads "
                "app.tenant_id, so a tenant-less transaction would be a widened "
                "query. System operations go through the §7 doors (L.0/#857)."
            )
        self._engine = engine
        self._tenant_id = tenant_id
        self._actor_kind = actor_kind
        self._actor_user_id = actor_user_id
        self._channel = channel
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    async def _apply_gucs(self, session) -> None:
        """`SET LOCAL` every GUC this UoW carries — transaction-scoped.

        LOCAL rather than SESSION is what makes pool reuse safe: the value dies
        with the transaction, so the next task to borrow the connection cannot
        inherit another tenant's scope. The gate proves that under reuse rather
        than trusting it.
        """
        await session.execute(
            text("SELECT set_config('app.tenant_id', :v, true)"),
            {"v": self._tenant_id},
        )
        for name, value in (
            ("app.actor_kind", self._actor_kind),
            ("app.actor_user_id", self._actor_user_id),
            ("app.channel", self._channel),
        ):
            if value is not None:
                await session.execute(
                    text(f"SELECT set_config('{name}', :v, true)"), {"v": value}
                )

    @asynccontextmanager
    async def begin(self):
        """Open the transaction, apply the GUCs, and mark the discipline flag.

        The flag is reset from its token in `finally` — see the module
        docstring on why a bare reset is wrong here.
        """
        token = _IN_TRANSACTION.set(True)
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._apply_gucs(session)
                    yield session
        finally:
            _IN_TRANSACTION.reset(token)


def unit_of_work(engine: AsyncEngine, tenant_id: str, **gucs) -> UnitOfWork:
    """Factory. Present so call sites read as intent rather than construction."""
    return UnitOfWork(engine, tenant_id, **gucs)
