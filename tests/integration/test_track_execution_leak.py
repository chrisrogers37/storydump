"""#907 — `create_run()` must not leave a connection idle-in-transaction.

`BaseService.track_execution` is the universal instrumentation wrapper: every
tracked service call app-wide flows through it, and its first act is
`ServiceRunRepository.create_run()`, which does `add → commit → refresh`. The
`refresh()` emits a SELECT that opens a NEW read transaction the method never
ends, so the connection sits **idle-in-transaction** for the full duration of
the call's own work. Survivable while the pool could grow; an outage risk the
moment L.0 pinned `max_overflow=0` + `pool_timeout=3.0` (this morning) — zero
burst room, callers fail after 3s once the pool saturates.

Proven the way rajan diagnosed it — on **actual backend state**, not a
call-count. A unit test asserting `create_run` was invoked proves nothing here:
the whole defect is that the connection's transaction state OUTLIVES the call.
Born RED against main: `create_run` leaves an idle-in-transaction backend whose
last statement touched `service_runs`.

The production `SessionLocal` is rebound at the current-schema test DB (the
`_route_repos_to_test_db` pattern), so `create_run` runs its real path; the
`pg_stat_activity` probe runs on a SEPARATE connection of the same engine, a
different backend, so it can see the leaked one.
"""

from __future__ import annotations


import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from src.repositories.service_run_repository import ServiceRunRepository
from src.services.base_service import BaseService

pytestmark = [pytest.mark.integration]


@pytest.fixture
def routed_engine(setup_test_database, monkeypatch):
    """Rebind production `SessionLocal` at the test engine; yield the engine.

    Every repo constructed under this fixture opens its session on the test
    DB, so `create_run` exercises its real code against a database we can
    also inspect via `pg_stat_activity`.
    """
    if setup_test_database is None:
        pytest.skip("Integration test requires a database")

    import src.config.database as db_module

    monkeypatch.setattr(
        db_module,
        "SessionLocal",
        sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=setup_test_database,
            expire_on_commit=False,
        ),
    )
    yield setup_test_database


def _idle_in_transaction_on_service_runs(engine) -> set[int]:
    """Backend PIDs on this DB that are idle-in-transaction with a last
    statement touching `service_runs` — the leaked-refresh signature.

    Runs on its own connection (a different backend), so it never reports
    itself.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT pid FROM pg_stat_activity"
                " WHERE datname = current_database()"
                "   AND pid <> pg_backend_pid()"
                "   AND state = 'idle in transaction'"
                "   AND query ILIKE '%service_runs%'"
            )
        ).fetchall()
    return {r[0] for r in rows}


class _TrackedService(BaseService):
    """A minimal real BaseService, to exercise the whole track_execution path
    (create_run → yield → complete_run) rather than create_run alone."""

    def __init__(self):
        super().__init__()
        self.service_name = "leak_probe"


class TestCreateRunEndsItsReadTransaction:
    def test_create_run_leaves_no_idle_in_transaction_backend(self, routed_engine):
        """The subject defect, at the single highest-value site."""
        before = _idle_in_transaction_on_service_runs(routed_engine)
        repo = ServiceRunRepository()
        try:
            repo.create_run(service_name="leak_probe", method_name="unit")
            leaked = _idle_in_transaction_on_service_runs(routed_engine) - before
            assert not leaked, (
                f"create_run left {len(leaked)} backend(s) idle-in-transaction"
                f" on service_runs (#907): {leaked}"
            )
        finally:
            repo.close()

    def test_no_backend_is_parked_for_the_full_duration_of_a_tracked_call(
        self, routed_engine
    ):
        """The universal-wrapper impact, measured WHERE it bites: INSIDE the
        tracked body. create_run's leaked read transaction is held from
        create_run until complete_run's commit — the whole call — so a
        concurrent caller under L.0's `max_overflow=0` pool is starved for
        that entire window. complete_run happens to close it at exit, so the
        honest proof checks mid-call, not after."""
        before = _idle_in_transaction_on_service_runs(routed_engine)
        service = _TrackedService()
        try:
            with service.track_execution("do_work", triggered_by="test") as run_id:
                assert run_id
                # Mid-call: the exact window a concurrent request contends for.
                leaked = _idle_in_transaction_on_service_runs(routed_engine) - before
            assert not leaked, (
                f"a tracked call parked {len(leaked)} backend(s)"
                f" idle-in-transaction on service_runs for its full duration"
                f" (#907): {leaked}"
            )
        finally:
            service.close()
