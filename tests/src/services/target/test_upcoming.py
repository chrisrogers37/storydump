"""`upcoming.upcoming` against a scripted executor: its two statements and what
they bind, its bounds, and the pins that hold the projection to the clock's
own expressions in the migration.

What the rows are (the range's edges, a predicted row's keys, the slots the
clock will mint, per account and across a clock change) is a database's to
say: `tests/scripts/test_upcoming_gate.py`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from src.services.target import content_runway, upcoming, vocabulary
from tests.src.services.target.test_content_runway import (
    CLOCK_TICK_MIGRATION,
    _Executor,
    _plan_slot_columns,
)

WS = "33333333-3333-3333-3333-333333333333"
#: A November drawn in whole weeks: 42 days.
FROM, TO = date(2026, 10, 26), date(2026, 12, 7)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


async def _read(ex, **kwargs):
    kwargs = {"workspace_id": WS, "from_date": FROM, "to_date": TO, **kwargs}
    return await upcoming.upcoming(ex, **kwargs)


class TestTheTwoStatements:
    async def test_planned_is_the_planned_stories_still_scheduled_in_the_range(self):
        ex = _Executor()
        await _read(ex)
        planned, params = ex.sent[0]
        assert "i.origin = 'planned' AND i.state = 'scheduled'" in planned
        assert planned.endswith("ORDER BY i.schedule_slot_at, i.id LIMIT :lim")
        assert params == {
            "ws": WS,
            "from_date": FROM,
            "to_date": TO,
            "lim": upcoming.PLANNED_MAX + 1,
        }

    async def test_the_walk_is_bound_to_the_workspace_the_range_and_its_span(self):
        ex = _Executor()
        await _read(ex)
        predicted, params = ex.sent[1]
        assert len(ex.sent) == 2
        assert predicted.startswith("WITH RECURSIVE acct AS (")
        assert params == {
            "ws": WS,
            "from_date": FROM,
            "to_date": TO,
            "lead": upcoming.WALK_LEAD_DAYS,
            # the range's 42 days, the lead's 2, and a part of a day at each end
            "span_days": 46,
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


class TestTheBounds:
    async def test_each_list_asks_for_one_over_its_limit_and_says_when_cut(self):
        ex = _Executor([{"n": n} for n in range(3)], [{"n": n} for n in range(4)])
        out = await _read(ex, planned_limit=2, predicted_limit=3)
        assert [params["lim"] for _, params in ex.sent] == [3, 4]
        assert out["planned"] == [{"n": 0}, {"n": 1}] and out["planned_truncated"]
        assert len(out["predicted"]) == 3 and out["predicted_truncated"]

    async def test_lists_at_their_limits_come_back_whole_as_read(self):
        planned = [{"id": "i-1"}, {"id": "i-2"}]
        predicted = [{"kind": "predicted", "n": n} for n in range(3)]
        ex = _Executor(planned, predicted)
        assert await _read(ex, planned_limit=2, predicted_limit=3) == {
            "planned": planned,
            "planned_truncated": False,
            "predicted": predicted,
            "predicted_truncated": False,
        }

    async def test_a_wider_range_is_cut_to_the_maximum_for_both_statements(self):
        ex = _Executor()
        await _read(ex, to_date=FROM + timedelta(days=400))
        cut = FROM + timedelta(days=vocabulary.RANGE_MAX_DAYS)
        assert [params["to_date"] for _, params in ex.sent] == [cut, cut]
        # the maximum's 45 days, the lead's 2, and a part of a day at each end
        assert ex.sent[1][1]["span_days"] == 49

    async def test_the_last_dates_are_served_without_overflow(self):
        ex = _Executor()
        await _read(ex, from_date=date(9999, 12, 1), to_date=date.max)
        assert [params["to_date"] for _, params in ex.sent] == [date.max, date.max]

    def test_a_walk_is_bounded_by_the_slots_its_days_hold(self):
        """The recursion's own bound, beside the range's end: the account's
        posts per day times the days the walk spans. No row can pin it, since
        it cuts no walk."""
        assert "WHERE s.slot < b.hi AND s.step <= a.eff_ppd * :span_days)" in _flat(
            upcoming._PREDICTED_SQL
        )


class TestTheClocksOwnExpressions:
    """The projection is the clock's, read from the migration rather than
    trusted to a comment: the accounts it walks are the ones the tick mints
    for, under the settings the tick advances their cursors by, one step of
    the walk being the tick's own advance. The gate cannot see this: its
    measure of the clock is `fn_next_slot` under settings the test names."""

    def test_the_accounts_are_the_ones_the_clock_posts_for(self):
        """`_POSTING_SQL` is the `plan_slot` leg's WHERE less its due-now
        conjunct, which `test_content_runway.py` pins to the migration."""
        assert f" AND {content_runway._POSTING_SQL})" in upcoming._PREDICTED_SQL

    def test_the_settings_are_the_legs_own(self):
        """Each of the four, as the leg's own select list writes it and under
        the leg's own name."""
        columns = _plan_slot_columns(CLOCK_TICK_MIGRATION)
        predicted = _flat(upcoming._PREDICTED_SQL)
        for name in ("eff_tz", "eff_ppd", "eff_start", "eff_end"):
            assert f"{columns[name]} AS {name}" in predicted, name

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
