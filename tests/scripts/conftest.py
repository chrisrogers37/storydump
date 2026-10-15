"""Shared fixtures and helpers for the migration-runner suites.

Real-PostgreSQL scratch databases, one per test, dropped on teardown — never
the shared per-session application database. Connection parameters come from
the same settings the rest of the suite uses (``.env.test`` via the root
conftest), so a credential or port move breaks every suite the same way
instead of this one differently.

Host model (#758/#763/#768 — several runs share this cluster): the DATABASE
half of isolation is per-session naming (#763's session suffix, reused here
so one run has one identity); the ROLE half cannot be namespaced — the seven
``svc_*`` roles are cluster-scoped spec names — so this suite SERIALIZES on
the cluster's own advisory lock and QUEUES behind concurrent runs instead of
corrupting them. Every cleanup path is scoped to this suite's own database
prefixes; nothing here can touch a database it did not name.

ONE lock, not two (#785). The mutex is ``SUITE_CLUSTER_LOCK_KEY``, held
session-wide by ``admin_conn``. It reaches every process that can reach the
cluster, which is the scope the guarded resource actually has.
"""

import contextlib
import json
import time
import uuid
from pathlib import Path

import psycopg2
import psycopg2.sql
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from psycopg2.extras import RealDictCursor, UUID_adapter
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.config.db_url import userinfo
from src.config.settings import settings
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.conftest import SESSION_DB_SUFFIX as SESSION_TOKEN


# psycopg2 adapts a `uuid.UUID` parameter (and asyncpg's subclass of it, which
# every gate's asyncpg fetch hands back) only once an adapter is registered.
# The adapter alone is registered here, deliberately NOT `register_uuid()`
# whole: its typecaster half would make every gate's psycopg2 connection return
# `uuid.UUID` for uuid columns where the gates read `str` (23 of them compare
# ids as text).
# pytest imports this conftest before any gate connects, which is the one
# ordering fact the registration relies on.
psycopg2.extensions.register_adapter(uuid.UUID, UUID_adapter)


def pytest_configure(config):
    """Refuse this directory under pytest-xdist (#809).

    WHAT THIS IS AND IS NOT. It makes an unsafe invocation fail in under a
    second instead of producing a twenty-minute silent queue; it does NOT make
    this directory parallel-safe, and nothing here moves it toward that. The
    seven ``svc_*`` roles are cluster-scoped spec names that cannot be
    namespaced per session (#768), which is why the suite serializes on
    ``SUITE_CLUSTER_LOCK_KEY`` in the first place. Under ``-n`` each worker is a
    separate process with its own session, so workers 2..N queue on a lock the
    first one holds until the session ends — they do not corrupt each other,
    they simply wait out ``SUITE_LOCK_WAIT_SECONDS`` and then raise. Read this
    as "the door is locked", never as "the room is now safe for a crowd".

    WHY THE WAIT IS INVISIBLE, which is what makes the refusal worth having: the
    wait loop reports by ``print`` from session-scoped fixture SETUP, and
    ``pytest.ini`` sets no ``-s``/``--capture=no``, so the message is captured
    and never reaches the terminal unless the fixture fails. The first thing a
    developer sees is a `RuntimeError` twenty minutes in.

    WHY ``hasattr(config, "workerinput")`` AND NOT ``is_xdist_worker()``: it is
    not a workaround for the public helper, it *is* the public helper's own
    implementation — xdist defines `is_xdist_worker` as exactly this hasattr
    check. The helper takes a request/session wrapper, and this hook is handed
    only ``config``, so the direct form is the correct spelling here rather than
    a shortcut. Do not "fix" it into an import; xdist is deliberately not a
    dependency of this project, and importing it would make the guard require
    the very plugin it exists to refuse.

    IT FIRES IN THE WORKER, AND NOTHING ELSE COULD. A run scoped away from here
    (`pytest --ignore=tests/scripts -n auto`) never loads this file and sees
    nothing. Refusing on the CONTROLLER instead — keying on
    `config.option.numprocesses` to fail before workers spawn — would make the
    second row below tidy, and does not work. Measured rather than reasoned: in
    the descent form this hook is called exactly TWICE, both times with
    ``workerinput`` present. The controller never loads this conftest at all,
    and on the workers ``numprocesses`` is ``None`` and ``dist`` is ``no``,
    because xdist resets them for the worker's own serial run. A controller-side
    check placed here would not trade one behaviour for another; it would be
    dead code that never fires.

    HOW THE REFUSAL PRESENTS DEPENDS ON HOW THIS DIRECTORY IS REACHED, and both
    forms are measured (pytest 9.0.3, pytest-xdist 3.8.0) because claiming only
    the tidy one would be true of half the invocations:

    - **Reached by DESCENT** — `pytest -n auto`, where `testpaths = tests` and
      collection walks into this directory. `ERROR tests/scripts` in the short
      summary, ~16s, no execnet noise, and **the rest of the suite still runs**.
    - **Named as an INITIAL ARGUMENT** — `pytest tests/scripts -n auto`. A
      conftest belonging to an initial arg is loaded at worker STARTUP rather
      than during collection, so the same refusal surfaces as a worker-boot
      failure: `no tests ran`, and one execnet traceback per worker. Still ~12s
      with the whole message present, so it is loud rather than silent — just
      ugly. Do not read the first row as a promise about the second.
    """
    if hasattr(config, "workerinput"):
        raise pytest.UsageError(
            "tests/scripts/ serializes every session on one cluster-wide"
            " advisory lock (SUITE_CLUSTER_LOCK_KEY) because the seven svc_*"
            " roles are cluster-scoped and cannot be namespaced per session, so"
            " it is not safe under pytest-xdist. Refusing now rather than"
            " queueing silently for SUITE_LOCK_WAIT_SECONDS. To keep"
            " parallelism for the rest of the suite, run them separately:"
            "  pytest --ignore=tests/scripts -n auto  &&  pytest tests/scripts"
        )


#: Scratch-database naming: ONE home for the convention every sweep predicate
#: derives from. The session token is #763's — one pytest run, one identity,
#: correlatable across the root suite's databases and these.
TEST_DB_PREFIX = "runner_test_"
SUITE_DB_PREFIXES = (TEST_DB_PREFIX, "runner_tpl_")

#: Advisory-lock key for "a tests/scripts suite is using this cluster's
#: shared objects (service roles, scratch namespaces)". Distinct from the
#: runner's own RUNNER_LOCK_KEY; same idea one level up: PostgreSQL is the
#: shared resource, so PostgreSQL's lock is the mutex that actually reaches
#: every process that can reach it, whichever bot or host owns it.
#:
#: THE SINGLE PRIMITIVE (#785). This lock alone guards the role lifecycle, with
#: no `flock` keyed on the same cluster alongside it, for reasons that are
#: properties of the two mechanisms and not of any one deployment:
#:
#: - **Scope.** The roles are CLUSTER-scoped; a `flock` is HOST-scoped. Two
#:   containers or hosts pointing at one cluster do not share a temp directory,
#:   so the file lock silently fails to serialise them while this one still
#:   does. `tempfile.gettempdir()` also follows `TMPDIR`, so the same divergence
#:   is reachable on a single host.
#: - **Coverage by container, not by enumeration.** This lock is held for the
#:   whole session, so it covers `_sweep_leftovers` — which drops OTHER
#:   sessions' suite-prefixed databases and is the most destructive thing here.
#:   Widening a per-fixture lock means maintaining a list of fixture names that
#:   goes stale the next time one is added.
#: - **Crash safety is not a discriminator.** Measured on this cluster: SIGKILL
#:   the holder with no cleanup path and the lock clears anyway (the backend
#:   sees EOF and exits). Both mechanisms have the property.
#: - **It can name its holder.** The wait below prints pid/user/application
#:   from `pg_locks`; a file lock cannot say who holds it.
SUITE_CLUSTER_LOCK_KEY = 753_2026
SUITE_LOCK_WAIT_SECONDS = 1200

#: A key nothing ever really takes, declared HERE rather than in the test that
#: uses it so key allocation has one home — the next author adding a real key
#: sees this one. The lock tests need a key they can contend over; the suite
#: key is held session-wide by `admin_conn`, so testing exclusion against it
#: would assert against the session's own hold.
SCRATCH_LOCK_KEY = 785_2026

REPO_ROOT = Path(__file__).resolve().parents[2]
ROLES_SQL = REPO_ROOT / "src" / "models" / "target" / "ddl" / "01_roles.sql"

#: The seven service roles declared by the schema code.
#: Held here rather than re-listed per test so the inventory has one home on
#: the test side too.
LOGIN_ROLES = ("svc_ingress", "svc_worker", "svc_migration")
NOLOGIN_ROLES = ("svc_claim", "svc_clock", "svc_maintenance", "svc_membership")
SERVICE_ROLES = LOGIN_ROLES + NOLOGIN_ROLES

#: The owner-actor role-name family (one per session; crashed sessions leave
#: strays the startup sweep reclaims).
OWNER_ROLE_PREFIX = "lineage_owner_"

#: Test-side stand-in for the passwords deployment sets out-of-band — never
#: DDL in the bootstrap itself.
TEST_ACTOR_PASSWORD = "test_actor_password"


def _dsn(database: str, user: str | None = None, password: str | None = None) -> str:
    """THE one home for connection-string construction — actors included."""
    user = user or settings.DB_USER
    password = (
        settings.DB_PASSWORD
        if password is None and user == settings.DB_USER
        else (password if password is not None else TEST_ACTOR_PASSWORD)
    )
    # The user and password encoded (`src.config.db_url`): pasted raw, one
    # carrying `@`, `/` or `%` is misread by libpq.
    auth = userinfo(user, password)
    return f"postgresql://{auth}{settings.DB_HOST}:{settings.DB_PORT}/{database}"


def as_user(dsn: str, user: str) -> str:
    """The same database, connected as a different actor (test password)."""
    database = dsn.rsplit("/", 1)[-1]
    return _dsn(database, user=user, password=TEST_ACTOR_PASSWORD)


def sweep_as_worker(dsn: str) -> int:
    """`work_loop.ensure_sender_jobs` the way the worker runs it: as the login
    *dsn* names (the worker's), in its own session with the EMPTY tenant and
    the `system` actor — the sweep's cross-tenant read is the door's (082).
    Run it AFTER the transaction that enqueued the cards has committed: a
    second session cannot see an uncommitted row."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from src.services.target import unit_of_work, work_loop

    async def go() -> int:
        engine = create_async_engine(async_url(dsn))
        try:
            async with engine.begin() as conn:
                await unit_of_work.apply_gucs(conn, tenant_id="", actor_kind="system")
                return await work_loop.ensure_sender_jobs(conn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def run_as_worker(
    owner_dsn: str, kind: str, *, payload: dict | None = None, config=None
) -> None:
    """One run of the system kind *kind* exactly as the worker's registry runs
    it, at the worker's own numbers unless *config* says otherwise — connected
    as `svc_worker`, with no tenant and the `system` actor. An
    `own_transactions` executor gets no session, as the loop gives it none.
    ONE spelling for every gate that runs a system job (its subject check
    included)."""
    from src.services.target.unit_of_work import apply_gucs
    from src.services.target.work_loop import WorkerConfig, WorkerDeps, build_registry

    job = {"kind": kind, "payload": payload or {}}
    async with ingress_engine(as_user(owner_dsn, "svc_worker")) as engine:
        registry = build_registry(
            WorkerDeps(engine=engine, config=config or WorkerConfig())
        )
        executor = registry[kind]
        async with engine.begin() as conn:
            who = (await conn.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_worker", who
            await apply_gucs(conn, tenant_id="", actor_kind="system")
            owns = getattr(executor, "owns_transactions", False)
            await executor(None if owns else conn, job)


async def reap_as_worker(owner_dsn: str) -> None:
    """One `reap_expired` run — the reaper door, the stale-approved leg, the
    settled-card sweep — through :func:`run_as_worker`."""
    await run_as_worker(owner_dsn, "reap_expired")


def async_url(dsn: str) -> str:
    """The asyncpg URL for *dsn* — the APPLICATION's rewrite, not a second one.

    `unit_of_work.asyncpg_url` is the deploy door both roots share, and its own
    docstring says why it exists: "so the rewrite lives once". A test that
    hand-rolls what the application does is a test that can go on agreeing
    with itself while the application changes underneath it.

    It also strips the libpq-only `sslmode`/`channel_binding` params asyncpg
    refuses. Inert for every DSN this suite builds (`_dsn` above emits no query
    string at all), and correct rather than inert if one ever carries them.
    """
    return asyncpg_url(dsn)


def _scratch_name(prefix: str = TEST_DB_PREFIX, tag: str = "") -> str:
    return f"{prefix}{tag}{SESSION_TOKEN}_{uuid.uuid4().hex[:10]}"


def _is_suite_db(datname: str) -> bool:
    """Is this a database THIS SUITE'S convention could have created (any
    session)? Every destructive sweep filters on this — a database this
    predicate rejects is someone else's data and untouchable, no matter what
    grants it holds."""
    return datname.startswith(SUITE_DB_PREFIXES)


def _is_mine(datname: str) -> bool:
    return _is_suite_db(datname) and SESSION_TOKEN in datname


def maintenance_conn():
    """A fresh autocommit connection to the maintenance database.

    One spelling, so `admin_conn` and anything that has to reach the cluster
    from OUTSIDE it — the lock tests, which need a connection that is not the
    lock holder — cannot disagree about what "the server" means.
    """
    conn = psycopg2.connect(_dsn("postgres"))
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def lock_holder(cur, key: int):
    """Who holds advisory lock ``key``: ``(pid, usename, application_name)``.

    THE point of the advisory lock over a file lock is that this question has
    an answer at all, so it lives in one place and the test asserts against
    THIS function rather than re-typing the join — a second copy could drift
    and leave the wait loop printing ``held by None`` with everything green.

    Matched on the full identity, not on ``objid`` alone. Measured: a
    two-argument ``pg_advisory_lock(0, 7532026)`` lands on the same ``objid``
    as our one-argument bigint key and is distinguished only by
    ``objsubid`` (1 for bigint, 2 for the int pair), so the loose form can
    name an unrelated lock's holder.
    """
    cur.execute(
        "SELECT a.pid, a.usename, a.application_name"
        " FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
        " WHERE l.locktype = 'advisory' AND l.granted"
        "   AND l.classid = 0 AND l.objid = %s AND l.objsubid = 1",
        (key,),
    )
    return cur.fetchone()


def assert_holds_cluster_lock(admin_conn, door: str) -> None:
    """Refuse a cluster-scoped door unless the connection handed in IS a holder
    of ``SUITE_CLUSTER_LOCK_KEY``.

    WHY THE PARAMETER IS CHECKED RATHER THAN DECORATIVE (#808). The cheap fix
    for a door that does not declare its mutex is to put the connection in the
    signature and never use it, so the fixture graph forces the dependency. That
    is a marker: nothing goes red when a later author deletes an argument the
    body ignores.

    The alternative is impossible for a helper holding only a DSN (#808), in
    both of the forms available to it. Measured on this cluster:

    - ``pg_try_advisory_lock(SUITE_CLUSTER_LOCK_KEY)`` returns True when called
      from the holding session. Advisory locks are RE-ENTRANT, so the obvious
      probe detects nothing.
    - The un-scoped ``pg_locks`` form answers "somebody holds it" — which is
      True precisely when a foreign session holds it, i.e. in the collision.

    What makes the question answerable is having the connection at all: run the
    backend-scoped form ON THE HANDED CONNECTION and it reports on that backend
    rather than on the cluster. Measured, same key, same cluster: 1 from the
    holder, 0 from an equally valid non-holding connection to the same database.
    So the argument is load-bearing, and a caller that reaches these roles from
    outside the mutex fails HERE instead of corrupting a concurrent run.

    Asked THROUGH ``lock_holder`` rather than as a second copy of its join, for
    the reason that function's own docstring gives: the ``objsubid`` clause tells
    our bigint key from an unrelated two-argument lock on the same ``objid``, and
    a divergent second spelling would go on matching that lock in silence.
    Reusing it also means the refusal can NAME who is inside the mutex — which
    the module header calls the whole advantage of the advisory lock over a
    `flock`, and a ``count(*)`` of our own backend would have thrown away.
    """
    with admin_conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        mine = cur.fetchone()[0]
        holder = lock_holder(cur, SUITE_CLUSTER_LOCK_KEY)
        if holder is None or holder[0] != mine:
            whose = (
                "nothing holds it"
                if holder is None
                else f"held by pid {holder[0]} ({holder[1]}/{holder[2]})"
            )
            raise RuntimeError(
                f"{door}: the connection handed in does not hold"
                f" SUITE_CLUSTER_LOCK_KEY ({SUITE_CLUSTER_LOCK_KEY}) — {whose}."
                " The seven svc_* roles are CLUSTER-scoped, so reaching them"
                " outside the suite mutex corrupts any concurrent run (#768)"
                " rather than failing. Route this through a fixture that"
                " depends on `admin_conn`."
            )


@pytest.fixture(scope="session")
def admin_conn(setup_test_database):
    """One session-wide connection to the maintenance database.

    Serializes the whole session on ``SUITE_CLUSTER_LOCK_KEY`` — visibly
    (each wait names the holder) and boundedly — then sweeps leftovers:
    suite-prefixed databases with zero backends (a crashed run's strays;
    a live one is named and left alone) and stray ``lineage_owner_*`` roles.
    Sweeping is opportunistic hygiene and must never fail the suite; the
    lock is released by the connection closing, which the ``finally``
    guarantees even if setup dies after acquisition.
    """
    if setup_test_database is None:
        pytest.skip("Database not available - skipping integration test")
    conn = maintenance_conn()
    try:
        with conn.cursor() as cur:
            waited = 0
            while True:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s)", (SUITE_CLUSTER_LOCK_KEY,)
                )
                if cur.fetchone()[0]:
                    break
                holder = lock_holder(cur, SUITE_CLUSTER_LOCK_KEY)
                print(
                    f"conftest: waiting for the suite cluster lock"
                    f" (held by {holder}) — queued, not corrupted"
                )
                if waited >= SUITE_LOCK_WAIT_SECONDS:
                    raise RuntimeError(
                        "suite cluster lock not acquired within"
                        f" {SUITE_LOCK_WAIT_SECONDS}s; holder: {holder}"
                    )
                time.sleep(5)
                waited += 5
            try:
                _sweep_leftovers(cur)
            except Exception as exc:  # noqa: BLE001 - hygiene must not fail the run
                print(f"conftest: leftover sweep skipped on error: {exc}")
        yield conn
    finally:
        conn.close()


def _sweep_leftovers(cur) -> None:
    """Drop crashed runs' strays: suite-prefixed databases with zero live
    backends, then orphaned owner-actor roles. Scoped by `_is_suite_db` —
    never anything this suite's convention did not name."""
    cur.execute(
        "SELECT d.datname, count(a.pid) FROM pg_database d"
        " LEFT JOIN pg_stat_activity a ON a.datname = d.datname"
        " WHERE d.datname LIKE %s OR d.datname LIKE %s"
        " GROUP BY d.datname",
        tuple(f"{p}%" for p in SUITE_DB_PREFIXES),
    )
    for stale, live in cur.fetchall():
        if not _is_suite_db(stale):
            continue
        if live:
            print(
                f"conftest: leaving {stale} alone — {live} live backend(s);"
                " a concurrent pre-lock run may own it"
            )
            continue
        cur.execute(f'DROP DATABASE IF EXISTS "{stale}"')
    cur.execute(
        "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
        (f"{OWNER_ROLE_PREFIX}%",),
    )
    strays = [r[0] for r in cur.fetchall()]
    if strays:
        _drop_roles_hardened(cur, tuple(strays))


def _create_db(admin_conn, name: str, owner: str | None = None) -> str:
    clause = f' OWNER "{owner}"' if owner else ""
    with admin_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"{clause}')
    return _dsn(name)


def _drop_db(admin_conn, name: str, timeout_s: float = 20.0) -> None:
    """Drop a scratch database, tolerating backends we are not allowed to kill.

    **A failure here is not local** (#758): teardown drops the database
    *before* the roles, because a grant inside a database is a catalog
    dependency on the role. So a drop that raises leaves the database standing,
    which leaves the roles undroppable, which fails the SETUP of every
    subsequent test in the session. Measured: one teardown error produced five
    cascaded `DependentObjectsStillExist` setup errors behind it.

    Two things make the naive form fail, and only the first is obvious:

    - **`pg_terminate_backend` can refuse.** It raises
      `InsufficientPrivilege: must be a superuser to terminate superuser
      process` when a superuser-owned backend — an autovacuum worker is the one
      that actually happens — is attached to the database. That is a normal,
      transient condition, and it becomes *likely* rather than rare when two
      sessions are churning databases at once. Terminating is best-effort:
      being unable to kill one backend is not a reason to abandon the drop.
    - **The backend may not be gone yet.** Termination is asynchronous, and
      autovacuum detaches on its own, so `DROP DATABASE` is retried within a
      bound instead of being attempted exactly once.

    Raises if the database still stands at the deadline — the one thing this
    must not do is return quietly having left it behind.
    """
    deadline = time.monotonic() + timeout_s
    last_error = None
    while True:
        with admin_conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
            except psycopg2.errors.InsufficientPrivilege:
                pass  # a superuser backend (autovacuum); it will detach itself
        with admin_conn.cursor() as cur:
            try:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
                return
            except psycopg2.errors.ObjectInUse as exc:
                last_error = exc
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"could not drop scratch database {name} within {timeout_s:.0f}s"
                f" — {last_error}. Leaving it would block every later role drop"
                " in this session, so this fails loudly rather than silently."
            )
        time.sleep(0.25)


def _scratch(
    admin_conn,
    owner: str | None = None,
    roles: list | None = None,
):
    """The one scratch-database lifecycle. ``roles`` is the service-role
    bracket: not None means the seven spec roles (plus any names the test
    appends to the list) are guaranteed absent before and dropped after —
    database first, roles second, the order `drop_service_roles` documents."""
    if roles is not None:
        drop_service_roles(admin_conn)
    name = _scratch_name()
    dsn = _create_db(admin_conn, name, owner)
    try:
        yield dsn
    finally:
        _drop_db(admin_conn, name)
        if roles is not None:
            drop_service_roles(admin_conn, roles)


@pytest.fixture()
def scratch_db(admin_conn):
    """A fresh empty database."""
    yield from _scratch(admin_conn)


@pytest.fixture()
def second_scratch_db(admin_conn):
    """An independent second database for two-schema comparisons."""
    yield from _scratch(admin_conn)


# The database owner applies schema code and ledger changes. Runtime services
# connect through their restricted service logins.


@pytest.fixture(scope="session")
def owner_actor(admin_conn):
    """The database-owner-shaped actor: non-superuser, CREATEROLE, LOGIN.

    Session-scoped like the service roles (roles are cluster objects); the
    admin is granted membership so it can create databases OWNED by it.
    Teardown goes through the hardened drop so a leaked owner-owned database
    cannot strand the role (#768's class, for the role this suite adds).
    """
    reason = actor_lacks_createrole(admin_conn)
    if reason:
        pytest.skip(reason)
    name = f"{OWNER_ROLE_PREFIX}{SESSION_TOKEN}"
    with admin_conn.cursor() as cur:
        cur.execute(
            f'CREATE ROLE "{name}" LOGIN CREATEROLE PASSWORD %s',
            (TEST_ACTOR_PASSWORD,),
        )
        cur.execute(f'GRANT "{name}" TO current_user')
    yield name
    with admin_conn.cursor() as cur:
        _drop_roles_hardened(cur, (name,))


@pytest.fixture()
def owner_db(admin_conn, owner_actor):
    """A fresh database OWNED by the owner actor."""
    yield from _scratch(admin_conn, owner=owner_actor)


@pytest.fixture()
def owner_world_db(admin_conn, owner_actor):
    """An empty owner database with service roles absent before and after."""
    yield from _scratch(admin_conn, owner=owner_actor, roles=[])


def set_test_passwords(admin_conn) -> None:
    """Set the service login passwords supplied by the test environment."""
    with admin_conn.cursor() as cur:
        for role in LOGIN_ROLES:
            cur.execute(f'ALTER ROLE "{role}" PASSWORD %s', (TEST_ACTOR_PASSWORD,))


def build_world(db_dsn: str, owner_actor: str, admin_conn) -> str:
    """Build a complete test database as its owner and adopt the base marker.

    The caller brackets the database and service roles with ``_scratch``.
    Role creation uses the schema's roles step under the suite cluster lock;
    the schema and ledger use the same non-superuser owner as deployment.
    """
    from scripts.migration_runner import MIGRATIONS_DIR, adopt
    from src.models.target.ddl import build

    assert_holds_cluster_lock(admin_conn, "build_world")
    owner = as_user(db_dsn, owner_actor)
    provision_roles(admin_conn, owner)
    engine = create_engine(owner)
    try:
        with engine.connect() as conn:
            build(conn)
        adopt(owner, MIGRATIONS_DIR, MIGRATIONS_DIR / "adoption_manifest.json")
        set_test_passwords(admin_conn)
    finally:
        engine.dispose()
    return owner


@pytest.fixture(scope="module")
def ingress_world(admin_conn, owner_actor):
    """A schema built once per module, addressed as owner and API login."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner = build_world(db, owner_actor, admin_conn)
        yield {"owner": owner, "ingress": as_user(owner, "svc_ingress")}
    finally:
        gen.close()


def seed_workspace_chain(conn, name: str) -> dict:
    """The full parent chain a post_intent requires, in ONE transaction.

    One transaction and committed because ``ct_workspaces_owner_at_insert``
    is a CONSTRAINT TRIGGER INITIALLY DEFERRED — a workspace and its owner
    member row must both exist by commit. ``app.actor_kind`` is set at
    session scope because the guards and governance audit triggers forbid
    anonymous writes. Returns the chain ids; the connection is left committed
    with the GUC still set. ONE spelling for the whole suite — satellite rows
    belong in the caller, not here."""
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
        user = cur.fetchone()[0]
        cur.execute("INSERT INTO workspaces (name) VALUES (%s) RETURNING id", (name,))
        ws = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'owner')",
            (ws, user),
        )
        chain = seed_intent_chain(cur, ws, name)
    conn.commit()
    return {"user": user, "ws": ws, **chain}


def seed_intent(dsn: str, workspace_id, name: str, *, state: str | None = None) -> dict:
    """`seed_intent_chain` on its own owner connection, as the migration actor,
    committed — for a gate whose module-scoped world's one intent is other
    tests' fixture and that needs an intent of its own in *state*."""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            chain = seed_intent_chain(cur, workspace_id, name, state=state)
        conn.commit()
        return chain
    finally:
        conn.close()


def seed_intent_chain(
    cur,
    workspace_id,
    name: str,
    *,
    state: str | None = None,
    origin: str | None = None,
) -> dict:
    """The satellite chain one post_intent needs — media source, account, media
    item, the intent — into an EXISTING workspace, on the caller's cursor and
    inside the caller's transaction (the caller commits, and has set
    ``app.actor_kind``). ``state`` overrides the intent's column default
    (``'scheduled'``) for a suite that needs, say, ``'awaiting_approval'``;
    ``origin`` likewise (``'cadence'``) for a planned story, which must be
    BORN planned: the ledger refuses changing it afterwards (088, 110).
    Split out of :func:`seed_workspace_chain` so an API-created workspace can
    be seeded with the same spelling rather than a copy."""
    cur.execute(
        "INSERT INTO media_sources (workspace_id, provider, config)"
        " VALUES (%s, 'gdrive', '{\"v\": 1}') RETURNING id",
        (workspace_id,),
    )
    src = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
        " VALUES (%s, %s) RETURNING id",
        (workspace_id, f"acct-{name}"),
    )
    iga = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO media_items"
        " (workspace_id, source_id, content_hash, file_name, media_kind,"
        "  provider_file_ref)"
        " VALUES (%s, %s, %s, 'f.jpg', 'image', %s) RETURNING id",
        (workspace_id, src, f"hash-{name}", f"ref-{name}"),
    )
    mi = cur.fetchone()[0]
    columns = "workspace_id, ig_account_id, media_item_id, provider_account_ref, approval_mode, schedule_slot_at"
    values = "%s, %s, %s, %s, 'manual', now()"
    params: tuple = (workspace_id, iga, mi, f"acct-{name}")
    if state is not None:
        columns += ", state"
        values += ", %s"
        params += (state,)
    if origin is not None:
        columns += ", origin"
        values += ", %s"
        params += (origin,)
    cur.execute(
        f"INSERT INTO post_intents ({columns}) VALUES ({values}) RETURNING id", params
    )
    intent = cur.fetchone()[0]
    return {"src": src, "iga": iga, "media": mi, "intent": intent}


def actor_lacks_createrole(admin_conn) -> str | None:
    """The reason the current actor cannot provision roles, or None.

    Returned as a reason string rather than a bool so the skip names the
    missing privilege. CI's PostgreSQL service makes `POSTGRES_USER` the
    cluster superuser, so the gate genuinely runs there; a local PostgreSQL
    whose test role was created by hand usually has neither flag, and a silent
    pass would read as coverage this suite did not have.
    """
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user"
        )
        row = cur.fetchone()
    if row and (row[0] or row[1]):
        return None
    return (
        "actor cannot provision roles: needs SUPERUSER or CREATEROLE. "
        "The bootstrap actor is non-superuser + CREATEROLE; "
        "grant it with: ALTER ROLE <test role> CREATEROLE;"
    )


def _drop_roles_hardened(cur, roles: tuple) -> None:
    """Drop roles; on DependentObjectsStillExist, drop exactly the SUITE
    databases actually blocking them (asked of pg_shdepend) and retry once.

    THE OWNERSHIP PREDICATE IS ABSOLUTE: `_is_suite_db` gates every drop.
    The step-0 bootstrap is a deployment artifact — a dev or staging
    database it was legitimately run against carries these same grants and
    must never be touched by a test sweep, live backends or not. A non-suite
    blocker is therefore named and the failure re-raised, loudly.

    THE MUTEX IS CHECKED HERE rather than at `drop_service_roles` (#808) because
    this is the primitive all role dropping funnels through, and the other two
    callers are the ones worth covering: `_sweep_leftovers`, which this file's
    own header calls the most destructive thing here, and the `owner_actor`
    teardown. Guarding the public door instead would cover one of three and
    leave the destructive path on call-path coverage, exactly the #808 shape.
    Enumerating doors is the shape `SUITE_CLUSTER_LOCK_KEY`'s own docstring
    warns against — coverage by container, not by enumeration.
    """
    assert_holds_cluster_lock(cur.connection, "_drop_roles_hardened")

    def _drop_all():
        for role in roles:
            cur.execute(f'DROP ROLE IF EXISTS "{role}"')

    try:
        _drop_all()
    except psycopg2.errors.DependentObjectsStillExist:
        # Two dependency shapes: grants on objects INSIDE a database
        # (dbid = that database) and ACLs on the database object itself
        # (dbid = 0, classid = pg_database, objid = the database).
        cur.execute(
            "SELECT DISTINCT d.datname,"
            "       (SELECT count(*) FROM pg_stat_activity a"
            "         WHERE a.datname = d.datname) AS live"
            " FROM pg_shdepend sd"
            " JOIN pg_roles r ON r.oid = sd.refobjid"
            " JOIN pg_database d ON ("
            "   d.oid = sd.dbid"
            "   OR (sd.dbid = 0 AND sd.classid = 'pg_database'::regclass"
            "       AND d.oid = sd.objid))"
            " WHERE r.rolname = ANY(%s)",
            (list(roles),),
        )
        for stale, live in cur.fetchall():
            if not _is_suite_db(stale):
                print(
                    f"conftest: role drop blocked by NON-SUITE database"
                    f" {stale} — not ours to touch; investigate by hand"
                )
                continue
            if live:
                print(
                    f"conftest: NOT dropping {stale} — {live} live"
                    " backend(s) hold it; is another suite running"
                    " without the lock?"
                )
                continue
            cur.execute(f'DROP DATABASE IF EXISTS "{stale}"')
        _drop_all()


def drop_service_roles(admin_conn, extra=()) -> None:
    """Remove the seven roles (plus any test-local ones) from the cluster.

    ORDER IS LOAD-BEARING: a privilege granted *inside* a database — `GRANT
    CREATE ON DATABASE`, a schema grant, a table grant — is a catalog
    dependency on the role, so PostgreSQL refuses the drop with
    `DependentObjectsStillExist` while that database is still there. Dropping
    the database first discards the dependencies with it.
    Callers must not hand-roll this order; use the role-bracketed fixtures
    (`owner_world_db`, `world_db`, ...), whose shared
    `_scratch` lifecycle sequences it correctly.

    Having ``admin_conn`` in the signature made this door DECLARE the mutex, not
    hold one; it is verified in `_drop_roles_hardened`, below (#808).
    """
    with admin_conn.cursor() as cur:
        _drop_roles_hardened(cur, tuple(extra) + SERVICE_ROLES)


def provision_roles(admin_conn, dsn: str) -> None:
    """Create the schema's service roles as the database owner under the lock."""
    assert_holds_cluster_lock(admin_conn, "provision_roles")
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(ROLES_SQL.read_text())
    finally:
        conn.close()


@pytest.fixture()
def world_db(admin_conn, owner_actor):
    """A complete schema and adopted base marker in a fresh owner database."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        yield build_world(db, owner_actor, admin_conn)
    finally:
        gen.close()


@pytest.fixture()
def sync_conn(world_db):
    """A plain psycopg2 connection to the built world, closed on teardown."""
    conn = psycopg2.connect(world_db)
    yield conn
    conn.close()


# --- plain helpers shared across the runner suites ---


def write_migration(migrations_dir, version, body, name="m"):
    path = migrations_dir / f"{version:03d}_{name}.sql"
    path.write_text(body)
    return path


def fetch_ledger(dsn):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT version, checksum, applied_by, execution_ms, status"
            " FROM runner.schema_migrations ORDER BY version"
        )
        rows = cur.fetchall()
    conn.close()
    return rows


def table_exists(dsn, table, schema="public"):
    row = fetch_one(
        dsn,
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
        " WHERE table_schema = %s AND table_name = %s)",
        (schema, table),
    )
    return row[0]


def execute(dsn, sql, params=None):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
    conn.close()


def fetch_one(dsn, sql, params=None):
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.close()
    return row


def rows_holding(dsn, needle):
    """Every base table whose rows, read as text, contain *needle*, as
    ``{"schema.table": rows}``. A scan rather than a list of known writers, so
    it also catches the next one: the invitation gates hold a raw token to
    "found nowhere" with it, since only its hash may be stored."""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_schema, table_name FROM information_schema.tables"
                " WHERE table_type = 'BASE TABLE'"
                " AND table_schema NOT IN ('pg_catalog', 'information_schema')"
            )
            hits = {}
            for schema, table in cur.fetchall():
                cur.execute(
                    psycopg2.sql.SQL(
                        "SELECT count(*) FROM {}.{} AS t WHERE strpos(t::text, %s) > 0"
                    ).format(
                        psycopg2.sql.Identifier(schema), psycopg2.sql.Identifier(table)
                    ),
                    (needle,),
                )
                rows = cur.fetchone()[0]
                if rows:
                    hits[f"{schema}.{table}"] = rows
            return hits
    finally:
        conn.close()


def fetch_all(dsn, sql, params=None):
    """Every matching row, by column NAME. A multi-column row assertion should
    read by name: a reordered SELECT cannot then silently shift which column
    an index meant."""
    with (
        psycopg2.connect(dsn) as conn,
        conn.cursor(cursor_factory=RealDictCursor) as cur,
    ):
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def probe_table(name):
    return (
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables"
        f" WHERE table_schema = 'public' AND table_name = '{name}')"
    )


def write_manifest(tmp_path, required_through, entries):
    path = tmp_path / "adoption_manifest.json"
    path.write_text(
        json.dumps({"required_through": required_through, "entries": entries})
    )
    return path


@contextlib.contextmanager
def txn(dsn, expect_user=None):
    """A NON-autocommit connection (SET LOCAL needs a transaction block),
    subject-gated when a login is named.

    ONE spelling for the whole suite — the #852 subject discipline every
    target-tier suite cites lives in this assertion, and two copies of it is
    how one file learns a stricter version and the other silently does not.
    Rolled back on exit unless the test committed.
    """
    conn = psycopg2.connect(dsn)
    try:
        if expect_user is not None:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                who = cur.fetchone()[0]
                assert who == expect_user, f"wrong subject: {who}"
        yield conn
        conn.rollback()
    finally:
        conn.close()


@contextlib.asynccontextmanager
async def ingress_engine(dsn):
    """A fresh NullPool async engine on *dsn*, disposed on exit — the shape an
    asyncio.run-per-call driver needs, since a pooled engine cannot outlive the
    loop that made it."""
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def in_tenant(dsn, ws, user, fn, *, channel="web", expect_user="svc_ingress"):
    """Run *fn(session)* in one committed unit of work as *expect_user*, the
    tables' owner only where a gate reads through the policies, the person
    *user* acting over *channel* (`web`, or `cli` for a token's write).

    The role is ASSERTED rather than assumed: a driver that quietly connected
    as the owner would bypass RLS, and every isolation claim built on it would
    be vacuous while still reading green. ONE spelling for the suites that
    measure under RLS (the provisioning and Drive gates), for the reason `txn`
    is one spelling.
    """
    async with ingress_engine(dsn) as engine:
        uow = unit_of_work(
            engine, str(ws), actor_kind="user", actor_user_id=str(user), channel=channel
        )
        async with uow.begin() as session:
            who = (await session.execute(text("SELECT current_user"))).scalar()
            assert who == expect_user, who
            return await fn(session)


async def in_user_plane(dsn, fn, *, actor_user_id=None):
    """Run *fn(conn)* in one committed transaction as `svc_ingress`, with the
    USER-plane GUC only — no tenant.

    `in_tenant`'s sibling, for the writers that run BEFORE any workspace
    exists. Identity upsert and session mint are user/auth-plane
    (`058` class 3: role-scoped `USING (true)`), so there is no tenant to
    claim, and `unit_of_work` cannot be borrowed for them: its constructor
    raises `TenantContextRequired` on an empty tenant id by design (a
    tenant-less unit of work would be a widened query). Production opens
    exactly the shape below — `src/api/routes/auth.py`'s Google callback and
    `src/api/principal.py`'s session resolve both run
    ``async with engine.begin() as conn`` with no GUCs at all.

    The role is ASSERTED for the same reason it is asserted in `in_tenant`: a
    driver that quietly connected as the owner would bypass RLS and every
    isolation claim built on it would be vacuous while still reading green.

    *actor_user_id* claims the caller in `app.actor_user_id` — the user-plane
    GUC `workspaces.list_for_user` sets — for the reads that go through a
    door. Left unset by default, because the sign-in writers set nothing.
    """
    async with ingress_engine(dsn) as engine:
        async with engine.begin() as conn:
            who = (await conn.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_ingress", who
            if actor_user_id is not None:
                await conn.execute(
                    text("SELECT set_config('app.actor_user_id', :u, true)"),
                    {"u": str(actor_user_id)},
                )
            return await fn(conn)
