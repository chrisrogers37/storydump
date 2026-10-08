"""The calendar's reads over the ledger (#1634), over a scripted executor.

What PostgreSQL decides (the local day in the workspace's zone, the index, the
tenant) is the gate's, `tests/scripts/test_intent_history_index_gate.py`. This
pins the statements: the range clauses and their parameters, and the month
read's states spelled into the SQL for the partial index to prove.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.services.target import workspaces

WS = "8d1d3a3e-0000-4000-8000-000000000001"

FROM_CLAUSE = (
    "i.schedule_slot_at >= (CAST(:from_date AS date)::timestamp AT TIME ZONE"
    " (SELECT tz FROM workspaces WHERE id = :ws))"
)
TO_CLAUSE = (
    "i.schedule_slot_at < (CAST(:to_date AS date)::timestamp AT TIME ZONE"
    " (SELECT tz FROM workspaces WHERE id = :ws))"
)


class _Rows:
    def __init__(self, *rows):
        self._rows = list(rows)

    def mappings(self):
        return iter(self._rows)


class _Executor:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _Rows(*self.rows)


class TestTheDayRange:
    async def test_a_range_bounds_the_slot_to_the_workspaces_local_days(self):
        ex = _Executor()
        await workspaces.list_intents(
            ex, workspace_id=WS, from_date=date(2026, 10, 3), to_date=date(2026, 10, 4)
        )
        ((sql, params),) = ex.statements
        assert FROM_CLAUSE in sql and TO_CLAUSE in sql
        assert params["from_date"] == date(2026, 10, 3)
        assert params["to_date"] == date(2026, 10, 4)
        assert params["ws"] == WS

    async def test_no_range_reads_as_before(self):
        ex = _Executor()
        await workspaces.list_intents(ex, workspace_id=WS)
        ((sql, params),) = ex.statements
        assert "schedule_slot_at >=" not in sql and "schedule_slot_at <" not in sql
        assert "from_date" not in params and "to_date" not in params


class TestTheMonthRead:
    def _row(self, day, total, name, slot):
        return {
            "day": day,
            "total": total,
            "id": f"id-{name}",
            "state": "posted",
            "schedule_slot_at": slot,
            "file_name": name,
            "category": "memes",
        }

    async def _days(self, ex, states=("posted",)):
        return await workspaces.intent_days(
            ex,
            workspace_id=WS,
            states=list(states),
            from_date=date(2026, 9, 28),
            to_date=date(2026, 11, 2),
            per_day=3,
        )

    @pytest.mark.parametrize("states", [(), ("posted", "frobnicated")])
    async def test_states_must_be_named_and_known(self, states):
        ex = _Executor()
        with pytest.raises(ValueError):
            await self._days(ex, states)
        assert ex.statements == [], "refused before any SQL ran"

    async def test_the_states_are_spelled_into_the_statement_not_bound(self):
        """A partial index proves its predicate from the query's literals; a
        bound array would hide them from a generic plan (107)."""
        ex = _Executor()
        await self._days(ex, ("posted", "skipped"))
        ((sql, params),) = ex.statements
        assert "i.state IN ('posted', 'skipped')" in sql
        assert "states" not in params
        assert FROM_CLAUSE in sql and TO_CLAUSE in sql
        assert "AT TIME ZONE w.tz" in sql, "the day is the workspace's local day"
        assert params == {
            "ws": WS,
            "per_day": 3,
            "from_date": date(2026, 9, 28),
            "to_date": date(2026, 11, 2),
        }

    async def test_rows_become_days_with_the_counts_the_read_counted(self):
        ex = _Executor(
            self._row("2026-10-03", 15, "c.jpg", "2026-10-03T20:00:00+00:00"),
            self._row("2026-10-03", 15, "b.jpg", "2026-10-03T19:00:00+00:00"),
            self._row("2026-10-03", 15, "a.jpg", "2026-10-03T18:00:00+00:00"),
            self._row("2026-10-04", 1, "d.jpg", "2026-10-04T09:00:00+00:00"),
        )
        days = await self._days(ex)
        assert [(d["date"], d["count"]) for d in days] == [
            ("2026-10-03", 15),
            ("2026-10-04", 1),
        ]
        assert [n["file_name"] for n in days[0]["newest"]] == [
            "c.jpg",
            "b.jpg",
            "a.jpg",
        ]
        assert days[0]["newest"][0] == {
            "id": "id-c.jpg",
            "state": "posted",
            "schedule_slot_at": "2026-10-03T20:00:00+00:00",
            "file_name": "c.jpg",
            "category": "memes",
        }
