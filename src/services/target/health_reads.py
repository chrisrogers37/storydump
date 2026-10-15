"""The statement cap on the unauthenticated health routes' reads."""

import logging
from contextlib import asynccontextmanager

from asyncpg.exceptions import QueryCanceledError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.services.target._dbapi import driver_error_is

logger = logging.getLogger(__name__)

#: The ceiling on EACH statement of a health read, in milliseconds. The server
#: enforces it, so it bounds each statement while the database answers.
STATEMENT_TIMEOUT_MS = 3000


class StatementTimedOut(StorydumpError):
    """A health read's statement was cancelled (SQLSTATE 57014): load, as a
    pool wait is, which the app answers with a 503 to retry. The cap is what
    cancels one; an operator's cancel arrives as the same SQLSTATE, and the
    message, the server's own, says which."""


async def bound(executor) -> None:
    """Cap each statement that follows in the current transaction at
    :data:`STATEMENT_TIMEOUT_MS`. ``set_config(..., true)`` is
    transaction-local, as `unit_of_work.apply_gucs` sets its `lock_timeout`,
    so the cap ends with the read and never outlives the pooled connection."""
    await executor.execute(
        text("SELECT set_config('statement_timeout', :ms, true)"),
        {"ms": str(STATEMENT_TIMEOUT_MS)},
    )


@asynccontextmanager
async def connect(engine):
    """A direct connection for one health read, capped by :func:`bound` as the
    first statement of the read's transaction. A statement cancelled during the
    read leaves it as :class:`StatementTimedOut`; every other database error
    passes through unchanged and stays the server's 500."""
    async with engine.connect() as conn:
        await bound(conn)
        try:
            yield conn
        except DBAPIError as exc:
            cancelled = driver_error_is(exc, QueryCanceledError)
            if cancelled is None:
                raise
            # Logged here, once per read: the health cache answers every hit of
            # its window with this one refusal, and the app does not log it.
            logger.warning(
                "health read cancelled (statement cap %d ms): %s",
                STATEMENT_TIMEOUT_MS,
                cancelled,
            )
            raise StatementTimedOut(str(cancelled)) from exc
