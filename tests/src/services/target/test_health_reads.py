"""`health_reads`: the statement cap a health read sets before it reads, and
the refusal a cancelled statement leaves the read as."""

import logging
from contextlib import asynccontextmanager

import pytest
from asyncpg.exceptions import (
    AdminShutdownError,
    DeadlockDetectedError,
    LockNotAvailableError,
    OperatorInterventionError,
    PostgresError,
    QueryCanceledError,
)
from sqlalchemy.exc import DBAPIError

from src.services.target import health_reads


class _Recorder:
    """Records each statement and its parameters; answers nothing."""

    def __init__(self):
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))


class _Engine:
    """`connect()` checks out one recorder."""

    def __init__(self):
        self.conn = _Recorder()

    @asynccontextmanager
    async def connect(self):
        yield self.conn


async def test_it_caps_the_rest_of_the_transaction_and_nothing_longer():
    executor = _Recorder()
    await health_reads.bound(executor)
    ((sql, params),) = executor.statements
    # Transaction-local (the third argument), so the cap ends with the read.
    assert sql == "SELECT set_config('statement_timeout', :ms, true)"
    assert params == {"ms": str(health_reads.STATEMENT_TIMEOUT_MS)}


async def test_a_cancelled_statement_leaves_the_read_as_the_refusal(caplog):
    cancelled = DBAPIError(
        "SELECT 1",
        None,
        QueryCanceledError("canceling statement due to statement timeout"),
    )
    with caplog.at_level(logging.WARNING, logger=health_reads.__name__):
        with pytest.raises(health_reads.StatementTimedOut) as refused:
            async with health_reads.connect(_Engine()):
                raise cancelled
    assert refused.value.__cause__ is cancelled
    # Logged where it is translated, naming the cap and the server's reason.
    (record,) = [r for r in caplog.records if r.name == health_reads.__name__]
    assert record.levelno == logging.WARNING
    cap = health_reads.STATEMENT_TIMEOUT_MS
    assert record.getMessage() == (
        f"health read cancelled (statement cap {cap} ms):"
        " canceling statement due to statement timeout"
    )


class _Adapter(Exception):
    """SQLAlchemy's asyncpg adapter error in the one respect the translation
    reads: the driver's error is its ``__cause__``."""


def _as_orig(driver_error):
    return DBAPIError("SELECT 1", None, driver_error)


def _as_cause(driver_error):
    adapter = _Adapter(str(driver_error))
    adapter.__cause__ = driver_error
    return DBAPIError("SELECT 1", None, adapter)


@pytest.mark.parametrize("shape", [_as_orig, _as_cause], ids=["orig", "cause"])
@pytest.mark.parametrize(
    "driver_error",
    [
        LockNotAvailableError("not the cancel"),
        DeadlockDetectedError("not the cancel"),
        AdminShutdownError("not the cancel"),
        OperatorInterventionError("not the cancel"),
        PostgresError("not the cancel"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_any_other_server_error_leaves_the_read_unchanged(
    shape, driver_error, caplog
):
    """Only the cancel's own class is the refusal: its parent class, a sibling
    in its SQLSTATE class, two lock errors and the base of every server error
    leave the read unchanged, in both shapes the translation reads, and log
    nothing."""
    broken = shape(driver_error)
    with caplog.at_level(logging.WARNING, logger=health_reads.__name__):
        with pytest.raises(DBAPIError) as raised:
            async with health_reads.connect(_Engine()):
                raise broken
    assert raised.value is broken
    assert [r for r in caplog.records if r.name == health_reads.__name__] == []
