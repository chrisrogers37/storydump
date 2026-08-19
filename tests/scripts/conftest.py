"""Shared fixtures and helpers for the migration-runner suites.

Real-PostgreSQL scratch databases, one per test, dropped on teardown — never
the shared per-session application database. Connection parameters come from
the same settings the rest of the suite uses (``.env.test`` via the root
conftest), so a credential or port move breaks every suite the same way
instead of this one differently.

Host model (#758/#763/#768 — several bots share this cluster): the DATABASE
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

import functools
import json
import subprocess
import time
import uuid
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from scripts.migration_runner import legacy_lineage_max
from src.config.settings import settings
from src.utils.validators import MIGRATIONS_DIR
from tests.conftest import SESSION_DB_SUFFIX as SESSION_TOKEN


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
TPL_DB_PREFIX = "runner_tpl_"
SUITE_DB_PREFIXES = (TEST_DB_PREFIX, TPL_DB_PREFIX)

#: Advisory-lock key for "a tests/scripts suite is using this cluster's
#: shared objects (service roles, scratch namespaces)". Distinct from the
#: runner's own RUNNER_LOCK_KEY; same idea one level up: PostgreSQL is the
#: shared resource, so PostgreSQL's lock is the mutex that actually reaches
#: every process that can reach it, whichever bot or host owns it.
#:
#: THE SINGLE PRIMITIVE (#785). A `flock` keyed on the same cluster used to
#: guard the role lifecycle redundantly alongside this. It was dropped rather
#: than widened, for reasons that are properties of the two mechanisms and not
#: of the current fleet:
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
SETUP_SQL = REPO_ROOT / "scripts" / "setup_database.sql"

#: Advertised-stream boundaries for the F.2 increments (#806, ruling (a)), used
#: by the derivation's unit tests. Held here because the stream is generated
#: from `02`/`07` plan text — one inserted statement moves every boundary, and a
#: bare literal at each use site lets consumers drift apart while each stays
#: green.
#:
#: DELIBERATELY LITERAL, NOT DERIVED. `F2_6_END` is "the index of the first
#: ENABLE ROW LEVEL SECURITY", and a test asserts exactly that property at that
#: bound; deriving it from the stream would make that assertion true by
#: construction. Re-derive them by hand when the plan changes — that is a review
#: event, which is the point.
#:
#: `F2_2_START` lived here too and went with migration 053: it existed to slice
#: the F.2.2 segment out of the stream for a control that STAGED that segment
#: while the target lineage was empty. The lineage carries it for real now, so
#: the slice has no consumer — and an unconsumed literal is the drift this
#: comment block exists to prevent, not an example of preventing it.
F2_2_END = 22  # F.2.2 = stream[2:22]: 7 tables, 4 tenant-keyed, no RLS
F2_6_END = 126  # end of F.2.6 — the last index before any RLS is enabled
BOOTSTRAP_SQL = REPO_ROOT / "scripts" / "window" / "step0_bootstrap.sql"

#: The step-0 companion artifact (#787): the window's legacy-DDL definer
#: door. Applied by the owner actor immediately after the bootstrap, and by
#: any owner-world stand-up that will replay 050 — 050 calls the door rather
#: than issuing owner DDL itself, so a world without it cannot apply 050 at
#: all. Deliberately a SECOND file rather than more of the bootstrap: the
#: bootstrap is cluster-scoped (it provisions the seven svc_* roles and so
#: needs the suite mutex), this is per-database and needs none, which is what
#: lets the owner-world sites apply it through `psql_apply`.
DOOR_SQL = REPO_ROOT / "scripts" / "window" / "step0_legacy_ddl_door.sql"

#: THE LEGACY LINEAGE'S STAND-UP IS TWO FILES SINCE #787, and this constant is
#: the one place that says so. 050 routes its owner-DDL through the step-0
#: definer door, so any world that will REPLAY 050 — not merely one that applies
#: `SETUP_SQL` — has to carry the door first. Window worlds get it from
#: `run_bootstrap`; owner worlds get it from here. Named rather than inlined
#: because the failure mode of forgetting it is a bare `schema "window_ddl" does
#: not exist` at migration 050, which reads as a corpus defect rather than as a
#: missing fixture file.
LEGACY_STANDUP = [SETUP_SQL, DOOR_SQL]

#: The last version of the LEGACY lineage — everything numbered below the 3c
#: schema move. **Derived from the move file's own marker, never written down
#: here**: a bound stated as a literal is a second enumeration of the corpus,
#: and it would be right on the day it was typed and silently wrong the first
#: time anyone renumbered anything. Read it, do not replace it with an int.
#:
#: What it is FOR: from 051 on the corpus holds two lineages in one directory,
#: and the suites below guard the legacy one — the schema the running
#: application is built on. Replaying past the boundary renames `public` out
#: from under them, so every legacy-lineage replay passes this bound. The lane
#: that replays *across* the boundary is `test_lineage_lane.py`, deliberately
#: a separate suite rather than a flag on these.
LEGACY_LINEAGE_MAX = legacy_lineage_max(MIGRATIONS_DIR)

#: The seven service roles, split as `02` §7 declares them and the `04` step-0
#: bootstrap provisions them. Held here rather than re-listed per test so the
#: inventory has one home on the test side too.
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
    auth = user if not password else f"{user}:{password}"
    return f"postgresql://{auth}@{settings.DB_HOST}:{settings.DB_PORT}/{database}"


def as_user(dsn: str, user: str) -> str:
    """The same database, connected as a different actor (test password)."""
    database = dsn.rsplit("/", 1)[-1]
    return _dsn(database, user=user, password=TEST_ACTOR_PASSWORD)


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
    body ignores, which is the same convention-not-construction shape the door
    had to begin with, moved one step along.

    #808 filed the alternative as impossible, and it is right about both forms
    available to a helper holding only a DSN. Measured on this cluster:

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
    the reason that function's own docstring gives: the ``objsubid`` clause cost
    a measurement to learn, and a divergent second spelling would go on matching
    an unrelated two-argument lock in silence. Reusing it also means the refusal
    can NAME who is inside the mutex — which the module header calls the whole
    advantage of the advisory lock over a `flock`, and a ``count(*)`` of our own
    backend would have thrown away.
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
def admin_conn():
    """One session-wide connection to the maintenance database.

    Serializes the whole session on ``SUITE_CLUSTER_LOCK_KEY`` — visibly
    (each wait names the holder) and boundedly — then sweeps leftovers:
    suite-prefixed databases with zero backends (a crashed run's strays;
    a live one is named and left alone) and stray ``lineage_owner_*`` roles.
    Sweeping is opportunistic hygiene and must never fail the suite; the
    lock is released by the connection closing, which the ``finally``
    guarantees even if setup dies after acquisition.
    """
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


@pytest.fixture()
def outsider_conn():
    """A connection valid in every way EXCEPT that it does not hold the mutex.

    Same cluster, database, user and process as ``admin_conn`` — both go
    through `maintenance_conn`, whose docstring already names this consumer:
    "anything that has to reach the cluster from OUTSIDE it". The single
    difference is the advisory lock, which is what makes a refusal asserted
    against this connection a controlled result rather than an unexplained one.
    """
    conn = maintenance_conn()
    try:
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


def _create_db(
    admin_conn, name: str, template: str | None = None, owner: str | None = None
) -> str:
    clause = f' TEMPLATE "{template}"' if template else ""
    clause += f' OWNER "{owner}"' if owner else ""
    with admin_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"{clause}')
    return _dsn(name)


def _drop_db(admin_conn, name: str, timeout_s: float = 20.0) -> None:
    """Drop a scratch database, tolerating backends we are not allowed to kill.

    **A failure here is not local** (#758 part 3): teardown drops the database
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
    template: str | None = None,
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
    dsn = _create_db(admin_conn, name, template, owner)
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


# --- the declared actors (#753: the `04` §0.2 actor split) -------------------
#
# Three actors, three jobs: the lineage seeds under a database-owner-shaped
# actor (non-superuser + CREATEROLE, so legacy tables carry real legacy
# ownership); the step-0 bootstrap runs as that owner; window steps run as
# svc_migration.


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
def owner_window_db(admin_conn, owner_actor):
    """An owner-owned database plus the service-role bracket — for tests
    that run the bootstrap and act as the window actor."""
    yield from _scratch(admin_conn, owner=owner_actor, roles=[])


def set_test_passwords(admin_conn) -> None:
    """Give the LOGIN service roles their test password — the step the
    bootstrap deliberately leaves to deployment env."""
    with admin_conn.cursor() as cur:
        for role in LOGIN_ROLES:
            cur.execute(f'ALTER ROLE "{role}" PASSWORD %s', (TEST_ACTOR_PASSWORD,))


def replay_advertised_stream(db_dsn: str, owner_actor: str, admin_conn) -> str:
    """Bootstrap as the owner, then replay the advertised stream as
    svc_migration into the empty public — the M.3 step-3 shape. Returns the
    window actor's DSN. The canonical full-schema stand-up: suites call this,
    never inline the sequence (promoted from test_advertised_ddl_replay,
    which now imports it, per the window_actor rule below)."""
    import psycopg2 as _psycopg2

    from scripts.advertised_ddl import (
        DEFAULT_DOCS,
        DEFAULT_MANIFEST,
        build_stream,
        load_manifest,
    )

    manifest = load_manifest(DEFAULT_MANIFEST)
    stream = build_stream(DEFAULT_DOCS, manifest)
    as_svc = window_actor(db_dsn, owner_actor, admin_conn)
    conn = _psycopg2.connect(as_svc)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(stream)
    finally:
        conn.close()
    return as_svc


def seed_workspace_chain(conn, name: str) -> dict:
    """The full parent chain a post_intent requires, in ONE transaction.

    One transaction and committed because ``ct_workspaces_owner_at_insert``
    is a CONSTRAINT TRIGGER INITIALLY DEFERRED — a workspace and its owner
    member row must both exist by commit. ``app.actor_kind`` is set at
    session scope because the §4 guards and governance audit triggers forbid
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
        cur.execute(
            "INSERT INTO media_sources (workspace_id, provider, config)"
            " VALUES (%s, 'gdrive', '{\"v\": 1}') RETURNING id",
            (ws,),
        )
        src = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
            " VALUES (%s, %s) RETURNING id",
            (ws, f"acct-{name}"),
        )
        iga = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO media_items"
            " (workspace_id, source_id, content_hash, file_name, media_kind,"
            "  provider_file_ref)"
            " VALUES (%s, %s, %s, 'f.jpg', 'image', %s) RETURNING id",
            (ws, src, f"hash-{name}", f"ref-{name}"),
        )
        mi = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO post_intents"
            " (workspace_id, ig_account_id, media_item_id, provider_account_ref,"
            "  approval_mode, schedule_slot_at)"
            " VALUES (%s, %s, %s, %s, 'manual', now()) RETURNING id",
            (ws, iga, mi, f"acct-{name}"),
        )
        intent = cur.fetchone()[0]
    conn.commit()
    return {
        "user": user,
        "ws": ws,
        "src": src,
        "iga": iga,
        "media": mi,
        "intent": intent,
    }


def window_actor(db_dsn: str, owner_actor: str, admin_conn) -> str:
    """Stand the window up the way M.3 does: bootstrap as the owner, then
    act as ``svc_migration``. Returns the window actor's DSN. The canonical
    actor hand-off — suites call this, never inline the sequence."""
    run_bootstrap(admin_conn, as_user(db_dsn, owner_actor))
    set_test_passwords(admin_conn)
    return as_user(db_dsn, "svc_migration")


# --- production-shaped templates (built once per session, copied per test) --


@pytest.fixture(scope="session")
def at45_template(admin_conn, owner_actor):
    """The at-45 world (setup + 001–045 hand-applied, no ledger), seeded
    CONNECTED AS THE OWNER ACTOR: production history was built by hand as
    the database owner, so the template's legacy tables carry real legacy
    ownership — the `04` §0.2 seed semantic. Table ownership rides into
    every TEMPLATE copy."""
    name = _scratch_name(TPL_DB_PREFIX, "at45_")
    _create_db(admin_conn, name, owner=owner_actor)
    psql_apply(as_user(_dsn(name), owner_actor), LEGACY_STANDUP + migration_files(45))
    yield name
    _drop_db(admin_conn, name)


@pytest.fixture(scope="session")
def at49_template(admin_conn, owner_actor, at45_template):
    """The at-49 world — built FROM the at-45 template plus 046–049, so the
    session pays for the shared prefix once."""
    name = _scratch_name(TPL_DB_PREFIX, "at49_")
    _create_db(admin_conn, name, template=at45_template, owner=owner_actor)
    files = [f for f in migration_files(49) if f not in set(migration_files(45))]
    psql_apply(as_user(_dsn(name), owner_actor), files)
    yield name
    _drop_db(admin_conn, name)


@pytest.fixture(scope="session")
def replayed_template(admin_conn, owner_actor):
    """The legacy lineage REPLAYED THROUGH THE RUNNER as the owner — for
    tests that assert on the resulting schema rather than on the replay
    report (the report-asserting test builds its own and stays the actual
    tightening battery)."""
    from scripts.migration_runner import apply_pending

    name = _scratch_name(TPL_DB_PREFIX, "replayed_")
    _create_db(admin_conn, name, owner=owner_actor)
    as_owner = as_user(_dsn(name), owner_actor)
    psql_apply(as_owner, LEGACY_STANDUP)
    apply_pending(as_owner, MIGRATIONS_DIR, LEGACY_LINEAGE_MAX)
    yield name
    _drop_db(admin_conn, name)


@pytest.fixture()
def at45_db(admin_conn, at45_template, owner_actor):
    """A private copy of the at-45 shape."""
    yield from _scratch(admin_conn, template=at45_template, owner=owner_actor)


@pytest.fixture()
def at45_window_db(admin_conn, at45_template, owner_actor):
    """The at-45 shape plus the service-role bracket (window-actor tests)."""
    yield from _scratch(admin_conn, template=at45_template, owner=owner_actor, roles=[])


@pytest.fixture()
def at49_db(admin_conn, at49_template, owner_actor):
    """A private copy of the at-49 shape, safe to mutate."""
    yield from _scratch(admin_conn, template=at49_template, owner=owner_actor)


@pytest.fixture()
def at49_window_db(admin_conn, at49_template, owner_actor):
    """The at-49 shape plus the service-role bracket (window-actor tests)."""
    yield from _scratch(admin_conn, template=at49_template, owner=owner_actor, roles=[])


@pytest.fixture()
def replayed_db(admin_conn, replayed_template, owner_actor):
    """A private copy of the runner-replayed legacy schema."""
    yield from _scratch(admin_conn, template=replayed_template, owner=owner_actor)


# --- the 04 step-0 window bootstrap (F.2.1, #746) ---------------------------
#
# Service roles are CLUSTER-scoped, so they outlive the per-test scratch
# databases every other fixture here relies on. That is not an inconvenience to
# work around — it is why role provisioning cannot be a runner migration in the
# first place (`02` §7-DDL: "ROLES ARE NOT CREATED HERE"). The helpers below
# own the lifecycle explicitly instead of leaning on database teardown.


def actor_lacks_createrole(admin_conn) -> str | None:
    """The reason the current actor cannot run the bootstrap, or None.

    Returned as a reason string rather than a bool so the skip names the
    missing privilege. CI's `postgres:15` service makes `POSTGRES_USER` the
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
        "`04` 0.2 declares the bootstrap actor as non-superuser + CREATEROLE; "
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
    leave the destructive path on exactly the call-path coverage #808 is about.
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
            if stale.startswith(TPL_DB_PREFIX) and _is_mine(stale):
                print(f"conftest: NOT dropping own template {stale}")
                continue
            cur.execute(f'DROP DATABASE IF EXISTS "{stale}"')
        _drop_all()


def drop_service_roles(admin_conn, extra=()) -> None:
    """Remove the seven roles (plus any test-local ones) from the cluster.

    ORDER IS LOAD-BEARING and cost a round of red tests to learn: a privilege
    granted *inside* a database — `GRANT CREATE ON DATABASE`, a schema grant, a
    table grant — is a catalog dependency on the role, so PostgreSQL refuses
    the drop with `DependentObjectsStillExist` while that database is still
    there. Dropping the database first discards the dependencies with it.
    Callers must not hand-roll this order; use the role-bracketed fixtures
    (`roleless_db`, `owner_window_db`, `at49_window_db`, ...), whose shared
    `_scratch` lifecycle sequences it correctly.

    Having ``admin_conn`` in the signature made this door DECLARE the mutex, not
    hold one; it is verified in `_drop_roles_hardened`, below (#808).
    """
    with admin_conn.cursor() as cur:
        _drop_roles_hardened(cur, tuple(extra) + SERVICE_ROLES)


def run_bootstrap(admin_conn, dsn: str) -> None:
    """Apply the step-0 artifact as the current (owner) actor.

    Executed through psycopg2 rather than `psql_apply` so a guard's RAISE
    surfaces as an exception the caller can assert the *message* of — the
    precondition is the point of the artifact, not an incidental failure mode.

    ``admin_conn`` is the CREATE side of the mutex declaration `drop_service_roles`
    has always had on the DROP side (#808), and it is checked rather than merely
    named — see `assert_holds_cluster_lock`. It is deliberately not the
    connection the bootstrap RUNS on: the artifact must execute as the owner
    actor, which is what ``dsn`` carries.
    """
    assert_holds_cluster_lock(admin_conn, "run_bootstrap")
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP_SQL.read_text())
            # The step-0 COMPANION (#787), in the order the runbook prints it:
            # the door's EXECUTE grant names svc_migration, which the bootstrap
            # above is what creates. Applied here rather than left to callers so
            # every window fixture stands the window up whole — a bootstrap
            # without its door is a window that still dies at 3b.
            cur.execute(DOOR_SQL.read_text())
    finally:
        conn.close()


def bootstrap_via_psql(admin_conn, dsn: str) -> None:
    """The same artifact through `psql -v ON_ERROR_STOP=1` — how a runbook step
    actually invokes it.

    A named door rather than a bare `psql_apply(dsn, [BOOTSTRAP_SQL])` call
    because BOTH routes onto the seven cluster-scoped roles have to verify the
    mutex or the one left bare is the whole hole (#808). This is the only route
    that may apply that artifact through psql — `psql_apply` turns it away.
    """
    assert_holds_cluster_lock(admin_conn, "bootstrap_via_psql")
    _psql_run(dsn, [BOOTSTRAP_SQL])


@pytest.fixture()
def roleless_db(admin_conn):
    """A fresh database with the seven service roles guaranteed ABSENT.

    Yields ``(dsn, extra_roles)`` — append any test-local role name to
    ``extra_roles`` and teardown drops it in the right order too. Roles are
    cluster-scoped, so "absent" has to be established at setup rather than
    assumed from a fresh database.
    """
    reason = actor_lacks_createrole(admin_conn)
    if reason:
        pytest.skip(reason)
    # No lock is taken HERE, and that is the point of #785 rather than an
    # omission: depending on `admin_conn` already places this fixture inside
    # the session-wide `SUITE_CLUSTER_LOCK_KEY`, which is held from before the
    # first fixture runs until the session ends. The collision #768 measured is
    # this fixture's SETUP against another session's in-use roles, and a
    # session-wide lock covers setup by construction — earlier and wider than
    # the per-fixture lock it replaces, not later or narrower.
    extra: list[str] = []
    gen = _scratch(admin_conn, roles=extra)
    dsn = next(gen)
    try:
        yield dsn, extra
    finally:
        gen.close()


@pytest.fixture()
def bootstrapped_db(admin_conn, roleless_db):
    """A fresh database with the seven service roles provisioned.

    What F.2.2 onward needs: a policy naming `svc_ingress` is uncreatable until
    the role exists, so every policy-carrying table PR replays through here.

    Built on `roleless_db` so the capability skip and the drop-database-then-
    drop-roles teardown order have exactly one home — the same reason the
    scratch fixtures above share `_scratch`.
    """
    dsn, _ = roleless_db
    run_bootstrap(admin_conn, dsn)
    return dsn


@functools.lru_cache(maxsize=1)
def advertised_stream():
    """The expanded 02+07 advertised stream, NORMALIZED, built once per process.

    ONE SPELLING for the whole suite. Two files had grown identical copies of
    this and only one of them was cached; the other rebuilt the 257-statement
    stream on every call. Measured on this host: ~36 ms per build, and the two
    suites between them ask for it roughly ten times a run.

    Cached because callers only ever slice it, and slicing copies — nothing in
    the suite writes to the plan docs or the manifest (every fixture that
    rewrites either targets `tmp_path`), so there is no invalidation to miss.

    NORMALIZED IS A COMPARISON KEY AND NOT EXECUTABLE SQL, which matters when
    writing the next increment's migration: normalization collapses whitespace
    and drops full-line comments, and `02` prints an inline `--` inside
    `CREATE TABLE onboarding_sessions`, so a normalized statement run as SQL
    comments out its own tail.
    """
    from scripts.advertised_ddl import (
        DEFAULT_DOCS,
        DEFAULT_MANIFEST,
        build_stream,
        load_manifest,
        normalize_statements,
    )

    return normalize_statements(
        build_stream(DEFAULT_DOCS, load_manifest(DEFAULT_MANIFEST))
    )


@functools.lru_cache(maxsize=None)
def migration_files(limit: int):
    """The real corpus up to ``limit``, selected by the runner's own
    discovery so the gate cannot drift from production's file set."""
    from scripts.migration_runner import discover_migrations

    files = [m.path for m in discover_migrations(MIGRATIONS_DIR, limit)]
    assert files, "no migration files found"
    return files


def psql_apply(dsn: str, files) -> None:
    """Hand-apply SQL files the way production history was actually built.

    REFUSES ``BOOTSTRAP_SQL`` (#808). That artifact provisions the seven
    cluster-scoped ``svc_*`` roles, and this door cannot check the suite mutex
    for the reason the door exists — it holds a DSN, not a connection, and a
    DSN cannot be asked what locks anyone holds. Rather than guard it (12 of
    its 13 callers apply `SETUP_SQL` or migration files INTO one scratch
    database, which is per-database and needs no cluster mutex), the one
    cluster-scoped file is turned away toward `bootstrap_via_psql`.

    Keying the refusal on the ARTIFACT rather than on a door is what stops the
    hole reopening: the inventory of ways to reach a file is unbounded and this
    is the second one already, so the check belongs where the file is named.

    The subprocess gets a MINIMAL environment, not the inherited shell
    (fleet practice, 2026-08-13): external repo code under an ambient bot
    environment is how credential names collide into error output. psql
    needs PATH and its own options; everything else it needs is in the DSN.
    """
    if any(Path(f) == BOOTSTRAP_SQL for f in files):
        raise RuntimeError(
            "psql_apply: BOOTSTRAP_SQL provisions the CLUSTER-scoped svc_*"
            " roles, so it must go through `bootstrap_via_psql`, which verifies"
            " SUITE_CLUSTER_LOCK_KEY (#808). This door only has a DSN, so it"
            " cannot check the mutex itself."
        )
    _psql_run(dsn, files)


def _psql_run(dsn: str, files) -> None:
    """The subprocess invocation itself — the one spelling, shared by the public
    `psql_apply` door and the guarded `bootstrap_via_psql` one, so the refusal
    above cannot be sidestepped by rebuilding the command somewhere else."""
    cmd = ["psql", dsn, "-q", "-v", "ON_ERROR_STOP=1"]
    for f in files:
        cmd += ["-f", str(f)]
    env = {
        "PATH": "/usr/bin:/bin",
        "PGOPTIONS": "-c client_min_messages=warning",
        "LC_ALL": "C.UTF-8",
    }
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"hand-apply failed: {result.stderr[-2000:]}"


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
