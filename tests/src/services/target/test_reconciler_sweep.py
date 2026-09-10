"""`reconciler.sweep_due` (#1276 review): the door says WHICH rows are due;
the ladder also needs HOW FAR each has climbed, which lives on the row."""

from __future__ import annotations

import uuid

from src.services.target import reconciler


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Conn:
    def __init__(self, door_rows, counted_rows):
        self.door_rows, self.counted_rows = door_rows, counted_rows
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        if "fn_reconciler_sweep" in str(statement):
            return _Rows(self.door_rows)
        return _Rows(self.counted_rows)


async def test_ladder_rows_carry_the_checks_recorded_on_the_intent():
    a, b, ws = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn = _Conn(
        door_rows=[
            {"intent_id": a, "workspace_id": ws, "reason": "ladder_due"},
            {"intent_id": b, "workspace_id": ws, "reason": "notify_window"},
        ],
        counted_rows=[{"id": a, "checks": 3}],
    )
    rows = await reconciler.sweep_due(conn, limit=50, notify_after_seconds=60)
    assert rows[0]["checks"] == 3, "the ladder step knows how far it has climbed"
    assert "checks" not in rows[1], "a notify row needs no ladder position"
    sql, params = conn.statements[1]
    assert "last_error->'evidence'->>'checks'" in sql and params["ids"] == [a]


async def test_an_intent_without_evidence_starts_at_zero():
    a, ws = uuid.uuid4(), uuid.uuid4()
    conn = _Conn(
        door_rows=[{"intent_id": a, "workspace_id": ws, "reason": "ladder_due"}],
        counted_rows=[],
    )
    rows = await reconciler.sweep_due(conn, limit=50, notify_after_seconds=60)
    assert rows[0]["checks"] == 0


async def test_no_ladder_rows_means_no_second_query():
    b, ws = uuid.uuid4(), uuid.uuid4()
    conn = _Conn(
        door_rows=[{"intent_id": b, "workspace_id": ws, "reason": "notify_window"}],
        counted_rows=[],
    )
    await reconciler.sweep_due(conn, limit=50, notify_after_seconds=60)
    assert len(conn.statements) == 1
