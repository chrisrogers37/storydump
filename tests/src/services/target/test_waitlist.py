"""`waitlist.join` refuses what the table cannot hold before any statement."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.services.target import waitlist


@pytest.mark.parametrize(
    "email", ["a" * 244 + "@example.co", "nul\x00@example.com", "lone\ud800@x.co"]
)
async def test_an_address_the_table_cannot_hold_runs_no_statement(email):
    conn = MagicMock()
    with pytest.raises(waitlist.InvalidWaitlistEmail):
        await waitlist.join(conn, email)
    assert conn.mock_calls == []
