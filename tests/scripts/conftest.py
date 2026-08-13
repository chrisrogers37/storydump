"""Shared fixtures and helpers for the migration-runner suites.

Real-PostgreSQL scratch databases, one per test, dropped on teardown — never
the shared per-session application database. Connection parameters come from
the same settings the rest of the suite uses (``.env.test`` via the root
conftest), so a credential or port move breaks every suite the same way
instead of this one differently.

Host model (#758/#763/#768 — several bots share this cluster): the DATABASE
half of isolation is per-session naming (#763's session suffix, reused here
so one run has one identity); the ROLE half cannot be namespaced — the seven
``svc_*`` roles are cluster-scoped spec names — so this suite SERIALIZES
host-wide on the cluster's own advisory lock and QUEUES behind concurrent
runs instead of corrupting them. Every cleanup path is scoped to this
suite's own database prefixes; nothing here can touch a database it did not
name.
"""

import fcntl
import functools
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from scripts.migration_runner import legacy_lineage_max
from src.config.settings import settings
from src.utils.validators import MIGRATIONS_DIR
from tests.conftest import SESSION_DB_SUFFIX as SESSION_TOKEN

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
#: every process on the host, whichever bot owns it.
SUITE_CLUSTER_LOCK_KEY = 753_2026
SUITE_LOCK_WAIT_SECONDS = 1200

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SQL = REPO_ROOT / "scripts" / "setup_database.sql"
BOOTSTRAP_SQL = REPO_ROOT / "scripts" / "window" / "step0_bootstrap.sql"

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
    conn = psycopg2.connect(_dsn("postgres"))
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            waited = 0
            while True:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s)", (SUITE_CLUSTER_LOCK_KEY,)
                )
                if cur.fetchone()[0]:
                    break
                cur.execute(
                    "SELECT a.pid, a.usename, a.application_name"
                    " FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
                    " WHERE l.locktype = 'advisory' AND l.objid = %s AND l.granted",
                    (SUITE_CLUSTER_LOCK_KEY,),
                )
                holder = cur.fetchone()
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


def window_actor(db_dsn: str, owner_actor: str, admin_conn) -> str:
    """Stand the window up the way M.3 does: bootstrap as the owner, then
    act as ``svc_migration``. Returns the window actor's DSN. The canonical
    actor hand-off — suites call this, never inline the sequence."""
    run_bootstrap(as_user(db_dsn, owner_actor))
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
    psql_apply(as_user(_dsn(name), owner_actor), [SETUP_SQL] + migration_files(45))
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
    psql_apply(as_owner, [SETUP_SQL])
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


#: How long a session waits for the service-role lock before giving up. The
#: wait is bounded rather than infinite because an indefinite block in CI is a
#: job that hangs to its global timeout with nothing explaining why; a bounded
#: wait fails with the lock path in the message.
#:
#: UNION NOTE (#753 PR-A rebase over #772): this flock and the session-wide
#: advisory lock admin_conn holds BOTH serialise the same cluster-scoped svc_*
#: provisioning — independently and redundantly. They are kept side by side
#: deliberately: #772 shipped merged + tested, PR-A was written before it, and
#: folding one into the other during a rebase would put unreviewed structure
#: behind rajan's completed review of PR-A. The redundancy is harmless (both
#: serialise; neither breaks the other) and the consolidation to a single lock
#: is tracked as its own issue.
ROLE_LOCK_TIMEOUT_S = float(os.environ.get("SVC_ROLE_LOCK_TIMEOUT_S", "300"))


def service_role_lock_path() -> Path:
    """The lock file guarding this CLUSTER's service roles.

    Keyed on host:port, derived rather than declared: roles are cluster-scoped,
    so the cluster is exactly the right mutual-exclusion scope. Two checkouts
    pointing at the same PostgreSQL must serialise; two pointing at different
    ones must not, and a single hardcoded path would make them queue for no
    reason. The name is readable on purpose — an operator finding it in the
    temp dir should be able to tell what it guards.
    """
    key = re.sub(r"[^A-Za-z0-9]+", "-", f"{settings.DB_HOST}-{settings.DB_PORT}")
    return Path(tempfile.gettempdir()) / f"storydump-svc-roles-{key}.lock"


@contextmanager
def service_role_lock():
    """Exclusive, host-wide, for the whole role lifecycle (#758 part 3).

    The seven service roles are CLUSTER-scoped, so a per-session database name
    cannot isolate them the way #763 isolated the database. Two concurrent
    sessions collide **setup against in-use**, in both directions: one
    session's `roleless_db` setup drops roles the other is actively using, and
    a drop can equally fail with `DependentObjectsStillExist` because the other
    session's database still holds grants. Measured: two concurrent runs of the
    role suites both return rc=1 while the same suite alone is green (#768).

    `flock` is released by the kernel when the holder exits, so a crashed or
    killed session cannot leave a stale lock behind — which is the property a
    lock file with hand-rolled cleanup would not have.
    """
    path = service_role_lock_path()
    # 0o666 so bots running as different users can share the same lock file;
    # umask may narrow it, which is a real bound rather than a guarantee.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
    deadline = time.monotonic() + ROLE_LOCK_TIMEOUT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"timed out after {ROLE_LOCK_TIMEOUT_S:.0f}s waiting for"
                        f" the service-role lock at {path}. Another test session"
                        " on this host is provisioning the cluster-scoped svc_*"
                        " roles. If nothing is running, a holder was killed"
                        " without releasing — flock clears on process exit, so"
                        " check for a live holder rather than deleting the file."
                    ) from None
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _drop_roles_hardened(cur, roles: tuple) -> None:
    """Drop roles; on DependentObjectsStillExist, drop exactly the SUITE
    databases actually blocking them (asked of pg_shdepend) and retry once.

    THE OWNERSHIP PREDICATE IS ABSOLUTE: `_is_suite_db` gates every drop.
    The step-0 bootstrap is a deployment artifact — a dev or staging
    database it was legitimately run against carries these same grants and
    must never be touched by a test sweep, live backends or not. A non-suite
    blocker is therefore named and the failure re-raised, loudly.
    """

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
    """
    with admin_conn.cursor() as cur:
        _drop_roles_hardened(cur, tuple(extra) + SERVICE_ROLES)


def run_bootstrap(dsn: str) -> None:
    """Apply the step-0 artifact as the current (owner) actor.

    Executed through psycopg2 rather than `psql_apply` so a guard's RAISE
    surfaces as an exception the caller can assert the *message* of — the
    precondition is the point of the artifact, not an incidental failure mode.
    """
    conn = psycopg2.connect(dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP_SQL.read_text())
    finally:
        conn.close()


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
    # UNION (PR-A over #772): PR-A's `_scratch(roles=)` lifecycle — which owns
    # the drop-database-then-roles order and the pg_shdepend/ownership-predicate
    # hardening — run INSIDE #772's flock, which spans the whole role lifecycle
    # (the collision is this fixture's SETUP against another session's in-use
    # roles, so a teardown-only lock would leave the measured #768 failure as
    # is). The flock is redundant with admin_conn's session-wide advisory lock
    # and consolidation is tracked separately; both are kept so neither review
    # is silently undone.
    with service_role_lock():
        extra: list[str] = []
        gen = _scratch(admin_conn, roles=extra)
        dsn = next(gen)
        try:
            yield dsn, extra
        finally:
            gen.close()


@pytest.fixture()
def bootstrapped_db(roleless_db):
    """A fresh database with the seven service roles provisioned.

    What F.2.2 onward needs: a policy naming `svc_ingress` is uncreatable until
    the role exists, so every policy-carrying table PR replays through here.

    Built on `roleless_db` so the capability skip and the drop-database-then-
    drop-roles teardown order have exactly one home — the same reason the
    scratch fixtures above share `_scratch`.
    """
    dsn, _ = roleless_db
    run_bootstrap(dsn)
    return dsn


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

    The subprocess gets a MINIMAL environment, not the inherited shell
    (fleet practice, 2026-08-13): external repo code under an ambient bot
    environment is how credential names collide into error output. psql
    needs PATH and its own options; everything else it needs is in the DSN.
    """
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
