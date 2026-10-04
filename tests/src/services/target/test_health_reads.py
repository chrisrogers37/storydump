"""`health_reads`: the statement cap a health read sets before it reads, and
the refusal a cancelled statement leaves the read as."""

import logging
from contextlib import asynccontextmanager

import pytest
from asyncpg.exceptions import QueryCanceledError
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


async def test_any_other_database_error_leaves_the_read_unchanged():
    broken = DBAPIError("SELECT 1", None, Exception("connection reset"))
    with pytest.raises(DBAPIError) as raised:
        async with health_reads.connect(_Engine()):
            raise broken
    assert raised.value is broken
