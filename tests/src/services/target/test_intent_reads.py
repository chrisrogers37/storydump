"""The calendar's reads over the ledger (#1634), over a scripted executor.

What PostgreSQL decides (the local day in the workspace's zone, the index, the
tenant) is the gate's, `tests/scripts/test_intent_slot_index_gate.py`. This
pins the statements: the range clauses, the states and their parameters.
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
            "media_item_id": f"media-{name}",
            "media_kind": "image",
            "has_thumbnail": True,
            "thumbnail_version": "0123456789abcdef",
            # A column the read never selects, so a link cannot ride along.
            "thumbnail_url": "https://drive.example/thumbnail",
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

    async def test_states_must_be_named(self):
        ex = _Executor()
        with pytest.raises(ValueError):
            await self._days(ex, ())
        assert ex.statements == [], "refused before any SQL ran"

    async def test_the_states_are_bound_as_the_intents_read_binds_them(self):
        ex = _Executor()
        await self._days(ex, ("posted", "skipped"))
        ((sql, params),) = ex.statements
        assert "i.state = ANY(CAST(:states AS text[]))" in sql
        assert FROM_CLAUSE in sql and TO_CLAUSE in sql
        assert "AT TIME ZONE w.tz" in sql, "the day is the workspace's local day"
        assert params == {
            "ws": WS,
            "per_day": 3,
            "states": ["posted", "skipped"],
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
            "media_item_id": "media-c.jpg",
            "media_kind": "image",
            "has_thumbnail": True,
            "thumbnail_version": "0123456789abcdef",
        }

    async def test_each_newest_carries_a_thumbnail_flag_never_its_link(self):
        """The calendar draws a thumbnail through the route (#1634 Phase 4),
        from the flag and version the intents read carries, never the link."""
        ex = _Executor()
        await self._days(ex)
        ((sql, _),) = ex.statements
        assert "m.thumbnail_url IS NOT NULL AS has_thumbnail" in sql
        assert "AS thumbnail_version" in sql
        assert "thumbnail_url" not in workspaces.INTENT_DAY_FIELDS
