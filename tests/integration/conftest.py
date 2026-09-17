"""Shared fixtures for integration tests that inspect live connection and
transaction state.

The idle-in-transaction leak probe (#907/#908) lives here as one definition so
the leak tests cannot drift apart: if the `pg_stat_activity` query ever needs
changing, it changes once.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture
def idle_in_transaction_touching():
    """Return a probe `f(engine, table) -> set[int]`: the backend pids sitting
    idle-in-transaction whose last statement mentions *table*, excluding the
    probe's own connection. A leaked read transaction shows up here."""

    def _probe(engine, table: str) -> set[int]:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT pid FROM pg_stat_activity"
                    " WHERE datname = current_database()"
                    "   AND pid <> pg_backend_pid()"
                    "   AND state = 'idle in transaction'"
                    "   AND query ILIKE :pat"
                ),
                {"pat": f"%{table}%"},
            ).fetchall()
        return {r[0] for r in rows}

    return _probe
