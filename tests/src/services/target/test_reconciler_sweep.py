"""`reconciler.sweep_due` says WHICH rows are due; `reconciler.checks_so_far`
says how far ONE row's ladder has climbed — read per row, under the row's
tenant, by the caller that claimed it (#1276 review; #1349 review: one read
for the whole sweep before any claim saw nothing under `svc_worker`, and the
ladder never exhausted)."""

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


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _Conn:
    def __init__(self, door_rows, evidence=None):
        self.door_rows, self.evidence = door_rows, evidence
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        if "fn_reconciler_sweep" in str(statement):
            return _Rows(self.door_rows)
        return _Scalar(self.evidence)


async def test_the_sweep_is_one_statement_and_carries_no_ladder_position():
    a, b, ws = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn = _Conn(
        door_rows=[
            {"intent_id": a, "workspace_id": ws, "reason": "ladder_due"},
            {"intent_id": b, "workspace_id": ws, "reason": "notify_window"},
        ]
    )
    rows = await reconciler.sweep_due(conn, limit=50, notify_after_seconds=60)
    assert [r["reason"] for r in rows] == ["ladder_due", "notify_window"]
    assert all("checks" not in r for r in rows), (
        "the position is read under the row's tenant, after the claim — not here"
    )
    assert len(conn.statements) == 1


async def test_checks_so_far_reads_the_evidence_on_the_row():
    a = uuid.uuid4()
    conn = _Conn(door_rows=[], evidence=3)
    assert await reconciler.checks_so_far(conn, intent_id=a) == 3, (
        "the ladder step knows how far it has climbed"
    )
    sql, params = conn.statements[0]
    assert "last_error->'evidence'->>'checks'" in sql and params["intent"] == str(a)


async def test_an_intent_without_evidence_starts_at_zero():
    conn = _Conn(door_rows=[], evidence=None)
    assert await reconciler.checks_so_far(conn, intent_id=uuid.uuid4()) == 0
