"""`upcoming.upcoming` against a scripted executor: its two statements and what
they bind, its bounds, and the pins that hold the projection to the clock's
own expressions in the migration.

That the walk gives the slots the clock will mint, per account and across a
clock change, is a database's to say: `tests/scripts/test_upcoming_gate.py`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from src.services.target import content_runway, upcoming, workspaces
from tests.src.services.target.test_content_runway import (
    CLOCK_TICK_MIGRATION,
    _Executor,
    _plan_slot_columns,
)

WS = "33333333-3333-3333-3333-333333333333"
FROM, TO = date(2026, 10, 26), date(2026, 12, 7)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


async def _read(ex, **kwargs):
    kwargs = {"workspace_id": WS, "from_date": FROM, "to_date": TO, **kwargs}
    return await upcoming.upcoming(ex, **kwargs)


class TestTheTwoStatements:
    async def test_planned_then_predicted_each_bound_to_the_workspace_and_the_range(
        self,
    ):
        ex = _Executor()
        await _read(ex)
        (planned, planned_params), (predicted, predicted_params) = ex.sent
        assert "i.origin = 'planned' AND i.state = 'scheduled'" in planned
        assert planned_params == {
            "ws": WS,
            "from_date": FROM,
            "to_date": TO,
            "lim": upcoming.PLANNED_MAX + 1,
        }
        assert predicted.startswith("WITH RECURSIVE acct AS (")
        assert predicted_params == {
            "ws": WS,
            "from_date": FROM,
            "to_date": TO,
            "lead": upcoming.WALK_LEAD_DAYS,
            "span_days": (TO - FROM).days + upcoming.WALK_LEAD_DAYS + 2,
            "lim": upcoming.PREDICTED_MAX + 1,
        }

    def test_every_table_is_read_under_the_workspace(self):
        """The predicate is the first fence (`.claude/rules/database.md`): a
        login that bypasses the policies still reads one workspace."""
        planned, predicted = (
            _flat(upcoming._PLANNED_SQL),
            _flat(upcoming._PREDICTED_SQL),
        )
        for fence in (
            "WHERE i.workspace_id = :ws",
            "m.workspace_id = i.workspace_id",
            "a.workspace_id = i.workspace_id",
            "w.id = i.workspace_id",
        ):
            assert fence in planned, fence
        assert "WHERE a.workspace_id = :ws AND w.id = :ws AND" in predicted
        zone = "(SELECT tz FROM workspaces WHERE id = :ws)"
        assert planned.count(zone) == 2 and predicted.count(zone) == 2

    def test_the_range_is_the_workspaces_local_days_half_open(self):
        start, end = upcoming._RANGE_START, upcoming._RANGE_END
        assert f"i.schedule_slot_at >= {start}" in upcoming._PLANNED_SQL
        assert f"i.schedule_slot_at < {end}" in upcoming._PLANNED_SQL
        assert f"(SELECT {start} AS lo, {end} AS hi)" in upcoming._PREDICTED_SQL
        assert "s.slot >= b.lo AND s.slot > now() AND s.slot < b.hi" in _flat(
            upcoming._PREDICTED_SQL
        )


class TestTheRows:
    def test_a_planned_row_is_the_intents_reads_row_and_its_day(self):
        """One column list for an intent row, so what the queue's row gains
        the calendar's gains."""
        assert upcoming._PLANNED_SQL.startswith(
            f"SELECT {workspaces._INTENT_COLUMNS},"
            " to_char(i.schedule_slot_at AT TIME ZONE w.tz, 'YYYY-MM-DD') AS day"
            f"{workspaces._INTENT_FROM} WHERE"
        )

    def test_a_predicted_row_says_it_is_one_and_carries_no_state(self):
        select = _flat(upcoming._PREDICTED_SQL).rsplit(") SELECT ", 1)[1]
        columns = select.split(" FROM ", 1)[0]
        assert columns.startswith("'predicted' AS kind,")
        names = [
            re.split(r" AS |\.", column)[-1]
            for column in re.split(r",\s*(?![^()]*\))", columns)
        ]
        assert names == [
            "kind",
            "schedule_slot_at",
            "day",
            "tz",
            "ig_account_id",
            "account_handle",
            "account_display_name",
        ]

    async def test_the_rows_come_back_as_read_each_list_its_own(self):
        planned = [{"id": "i-1", "state": "scheduled", "day": "2026-11-03"}]
        predicted = [{"kind": "predicted", "day": "2026-11-03"}]
        assert await _read(_Executor(planned, predicted)) == {
            "planned": planned,
            "planned_truncated": False,
            "predicted": predicted,
            "predicted_truncated": False,
        }


class TestTheBounds:
    async def test_each_list_asks_for_one_over_its_limit_and_says_when_cut(self):
        ex = _Executor([{"n": n} for n in range(3)], [{"n": n} for n in range(4)])
        out = await _read(ex, planned_limit=2, predicted_limit=3)
        assert [params["lim"] for _, params in ex.sent] == [3, 4]
        assert out["planned"] == [{"n": 0}, {"n": 1}] and out["planned_truncated"]
        assert len(out["predicted"]) == 3 and out["predicted_truncated"]

    async def test_a_list_exactly_at_its_limit_is_not_cut(self):
        ex = _Executor([{"n": n} for n in range(2)], [{"n": n} for n in range(3)])
        out = await _read(ex, planned_limit=2, predicted_limit=3)
        assert len(out["planned"]) == 2 and not out["planned_truncated"]
        assert len(out["predicted"]) == 3 and not out["predicted_truncated"]

    async def test_a_wider_range_is_cut_to_the_maximum_for_both_statements(self):
        ex = _Executor()
        await _read(ex, to_date=FROM + timedelta(days=400))
        cut = FROM + timedelta(days=upcoming.RANGE_MAX_DAYS)
        assert [params["to_date"] for _, params in ex.sent] == [cut, cut]
        assert ex.sent[1][1]["span_days"] == (
            upcoming.RANGE_MAX_DAYS + upcoming.WALK_LEAD_DAYS + 2
        )

    async def test_the_last_dates_are_served_without_overflow(self):
        ex = _Executor()
        await _read(ex, from_date=date(9999, 12, 1), to_date=date.max)
        assert [params["to_date"] for _, params in ex.sent] == [date.max, date.max]

    def test_a_walk_is_bounded_by_the_slots_its_days_hold(self):
        """The recursion's own bound, beside the range's end: the account's
        posts per day times the days the walk spans."""
        assert "WHERE s.slot < b.hi AND s.step <= a.eff_ppd * :span_days)" in _flat(
            upcoming._PREDICTED_SQL
        )


class TestTheClocksOwnExpressions:
    """The projection is the clock's, read from the migration rather than
    trusted to a comment: the accounts it walks are the ones the tick mints
    for, under the settings the tick advances their cursors by, one step of
    the walk being the tick's own advance."""

    def test_the_accounts_are_the_ones_the_clock_posts_for(self):
        """`_POSTING_SQL` is the `plan_slot` leg's WHERE less its due-now
        conjunct, which `test_content_runway.py` pins to the migration."""
        assert (
            f"AND w.id = :ws AND {content_runway._POSTING_SQL})"
            in upcoming._PREDICTED_SQL
        )

    def test_the_settings_are_the_legs_own(self):
        columns = _plan_slot_columns(CLOCK_TICK_MIGRATION)
        assert set(upcoming._EFFECTIVE_SQL) == {
            "eff_tz",
            "eff_ppd",
            "eff_start",
            "eff_end",
        }
        for name, sql in upcoming._EFFECTIVE_SQL.items():
            assert columns[name] == _flat(sql), name
            assert f"{sql} AS {name}" in upcoming._PREDICTED_SQL, name

    def test_a_step_is_the_legs_cursor_advance(self):
        """The same function over the same settings in the same order, from
        the slot before instead of the cursor."""
        from scripts.migration_runner import MIGRATIONS_DIR

        ddl = (MIGRATIONS_DIR / CLOCK_TICK_MIGRATION).read_text()
        advances = re.findall(r"SET next_slot_at = (fn_next_slot\([^)]*\))", ddl)
        assert len(advances) == 1, "positive control: the leg's advance was found"
        ours = advances[0].replace("d.next_slot_at", "{after}").replace("d.", "a.")
        assert ours == upcoming._NEXT_SLOT
        assert upcoming._NEXT_SLOT.format(after="s.slot") in upcoming._PREDICTED_SQL
        assert upcoming._NEXT_SLOT.format(after="b.walk_from") in (
            upcoming._PREDICTED_SQL
        )
