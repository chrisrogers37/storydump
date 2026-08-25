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

**The flag is per-task, and that is the bound.** Measured: a call made directly
inside the transaction, from a task spawned inside it, or via
``asyncio.to_thread`` is refused — those are the shapes `02` §5 is about, where
the connection is genuinely held across the call. A task spawned *before* the
transaction opened, or work handed to ``run_in_executor`` with a fresh event
loop, carries its own context and is not seen. Those are not violations in the
same sense (such a task holds no connection of yours), but they are the edge,
and "ContextVar, therefore per-task, therefore blind across an executor
boundary" is obvious now and expensive to rediscover at L.6.
"""

from __future__ import annotations

import contextvars
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config.settings import settings
from src.exceptions.base import StorydumpError

#: `05` seam value. Pinned, not configurable, not read from settings — see the
#: module docstring for why `settings.DB_MAX_OVERFLOW` is deliberately ignored.
MAX_OVERFLOW_SEAM = 0

#: `05` seam value: 10 per replica, both lanes (`3×10 workers + 2×10 ingress`).
#: Pinned for the same reason the overflow is. Leaving HALF the inequality
#: configurable was the gap: with `pool_size` read from settings, a production
#: `DB_POOL_SIZE` override silently breaks `Σ(replica × (pool + overflow)) = 50`
#: and no gate can see it, because the test would read the same overridden
#: value it is meant to be checking.
POOL_SIZE_SEAM = 10

#: How long a caller waits for a connection before being told there is none.
#:
#: **This is the saturation policy, and pinning `max_overflow` to 0 is what
#: made it one.** With burst capacity the SQLAlchemy default (30 s) was rarely
#: reached; with overflow at 0 the pool cannot burst, so every saturation
#: episode lands here. The number did not change — its meaning did.
#:
#: 30 s is pile-up, not the `01` H5 behaviour the plan specifies
#: ("visible backpressure, slip-a-slot rather than pile-up"): on a bulk replica
#: with 50 task slots one episode parks up to 40 tasks for 30 s each, all still
#: holding their slots. It is also >= `EgressPolicy.total_budget_s`, so a job
#: could burn its entire provider budget waiting for a connection it never got
#: — the substrate outlasting its own callers' timeouts.
#:
#: 3 s is derived, not picked: it sits **below the shortest egress timeout
#: class** (`fast` = 5.0 s), so the substrate can never be the slowest thing in
#: a call; and it is far above the expected wait, since `05`'s model puts
#: DB-active demand at ~2.5 against a pool of 10 — a wait of any length is
#: already the tail, so failing fast there defers work rather than losing it.
POOL_TIMEOUT_SEAM = 3.0

#: Carried across from the legacy sync engine (`src/config/database.py`), which
#: sets it explicitly to 300 alongside pre-ping. `pool_pre_ping` catches a dead
#: connection, but the legacy author judged recycling necessary IN ADDITION,
#: and silently dropping to -1 (never) in the target engine would be a
#: regression nobody chose.
POOL_RECYCLE_SEAM = 300

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


def asyncpg_url(url: str) -> str:
    """A platform-shaped `postgresql://` URL made asyncpg-safe — the deploy door
    both roots share (worker and api), so the rewrite lives once.

    Rewrites what the asyncpg driver refuses: the dialect suffix, and the
    libpq-only `sslmode`/`channel_binding` query params (Neon appends both;
    asyncpg takes `ssl=`).
    """
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    sslmode = params.pop("sslmode", None)
    params.pop("channel_binding", None)
    if sslmode and "ssl" not in params:
        params["ssl"] = sslmode
    return urlunsplit(parts._replace(query=urlencode(params)))


def engine_url_from_env(env) -> Optional[str]:
    """`TARGET_DATABASE_URL` from *env*, asyncpg-safe, or None when unset so
    :func:`create_engine` falls back to the settings-built URL."""
    url = env.get("TARGET_DATABASE_URL")
    if not url:
        return None
    return asyncpg_url(url)


def create_engine(url: Optional[str] = None) -> AsyncEngine:
    """The async engine, with `max_overflow` pinned to the `05` seam."""
    return create_async_engine(
        url or async_database_url(),
        pool_size=POOL_SIZE_SEAM,
        max_overflow=MAX_OVERFLOW_SEAM,
        pool_timeout=POOL_TIMEOUT_SEAM,
        pool_recycle=POOL_RECYCLE_SEAM,
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
        if not tenant_id or not isinstance(tenant_id, str) or not tenant_id.strip():
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
        await apply_gucs(
            session,
            tenant_id=self._tenant_id,
            actor_kind=self._actor_kind,
            actor_user_id=self._actor_user_id,
            channel=self._channel,
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


async def apply_gucs(
    executor,
    *,
    tenant_id: str,
    actor_kind: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    channel: Optional[str] = None,
) -> None:
    """`SET LOCAL` the tenancy/actor GUCs — the ONE spelling of the security
    invariant, shared by the UoW and by raw-connection transactions (the §6
    permit path). *executor* is anything with ``.execute`` (AsyncSession or
    AsyncConnection).

    LOCAL (`is_local=true`) rather than SESSION is what makes pool reuse safe:
    the value dies with the transaction, so the next task to borrow the
    connection cannot inherit another tenant's scope. Two hand-rolled copies
    of this call is how a third ships `is_local=false` and leaks tenant scope
    with no gate able to see it — hence one home.

    One statement, not one per GUC: a tenant-scoped request pays this on
    every unit of work, and four round trips for four `set_config` calls was
    measured as the largest avoidable cost on the read path (#1028). Only the
    non-None pairs are sent — `set_config(x, NULL, true)` stores the empty
    string, and the audit triggers must keep seeing an UNSET actor as unset.

    Known coverage note: a raw-connection transaction (the permit path) never
    sets `_IN_TRANSACTION`, so the §5 discipline tripwire does not cover the
    permit window. Inert today — `acquire_permit` commits before returning,
    so no provider call can happen inside it — named here because this is
    where the next reader will look.
    """
    pairs = [("app.tenant_id", tenant_id)] + [
        (name, value)
        for name, value in (
            ("app.actor_kind", actor_kind),
            ("app.actor_user_id", actor_user_id),
            ("app.channel", channel),
        )
        if value is not None
    ]
    calls = ", ".join(
        f"set_config('{name}', :v{i}, true)" for i, (name, _) in enumerate(pairs)
    )
    await executor.execute(
        text(f"SELECT {calls}"),
        {f"v{i}": value for i, (_, value) in enumerate(pairs)},
    )


def unit_of_work(engine: AsyncEngine, tenant_id: str, **gucs) -> UnitOfWork:
    """Factory. Present so call sites read as intent rather than construction."""
    return UnitOfWork(engine, tenant_id, **gucs)
