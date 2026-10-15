"""`health_reads.connect` on a real PostgreSQL through asyncpg, the API's driver.

The unit tests pin the statement, its parameter and the translation of a
hand-built cancel; only the server can show that the cap applies to the
statements after it in the same transaction, that one running past it is
cancelled as the SQLSTATE the translation reads, and that the cap is gone once
that transaction ends, before the pooled connection serves anything else.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import health_reads
from tests.scripts.conftest import _scratch, async_url

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def empty_db(admin_conn):
    """One empty database for the module: the cap needs no schema."""
    yield from _scratch(admin_conn)


@pytest.fixture
async def one_connection(empty_db):
    """An engine with ONE pooled connection, so every read reuses it."""
    engine = create_async_engine(async_url(empty_db), pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


async def _statement_timeout(conn) -> str:
    return (await conn.execute(text("SHOW statement_timeout"))).scalar_one()


async def test_the_cap_holds_for_the_read_and_ends_with_its_transaction(
    one_connection,
):
    """The read ends in a COMMIT, which keeps a session-wide setting where a
    rollback would undo it: so the default coming back shows the cap belonged
    to the transaction."""
    async with one_connection.connect() as conn:
        default = await _statement_timeout(conn)
    async with health_reads.connect(one_connection) as conn:
        assert await _statement_timeout(conn) == "3s"
        await conn.commit()
    # The same pooled connection, its next transaction: the cap went with the read.
    async with one_connection.connect() as conn:
        assert await _statement_timeout(conn) == default


async def test_a_statement_past_the_cap_is_cancelled(one_connection, monkeypatch):
    monkeypatch.setattr(health_reads, "STATEMENT_TIMEOUT_MS", 50)
    with pytest.raises(health_reads.StatementTimedOut, match="statement timeout"):
        async with health_reads.connect(one_connection) as conn:
            await conn.execute(text("SELECT pg_sleep(5)"))
