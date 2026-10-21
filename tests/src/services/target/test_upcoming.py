"""`upcoming.upcoming` against a scripted executor: its three statements and
what they bind, its bounds, and the pins that hold the projections to the
clock's own expressions and the successor trigger's own grid in the
schema code.

What the rows are (the range's edges, a predicted row's keys, the slots the
clock will mint, per account and across a clock change) is a database's to
say: `tests/scripts/test_upcoming_gate.py`.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from src.services.target import (
    content_runway,
    intent_ledger,
    upcoming,
    vocabulary,
    workspaces,
)
from tests.src.services.target.schema_code import function_body, function_definitions
from tests.src.services.target.test_content_runway import (
    _Executor,
    _plan_slot_columns,
)

WS = "33333333-3333-3333-3333-333333333333"
#: A past-posts item is stored under this Instagram id, which no read may give; a
#: stored name of the form `Instagram <id>` carries it too.
GRAPH_ID = "17841400000000771"
POSTED_AT = datetime(2020, 1, 1, 12, 30, tzinfo=timezone.utc)
PROVENANCE_COLUMNS = (
    "source_provider",
    "origin_handle",
    "original_at",
    "slide_post_ref",
    "slide_position",
    "slide_count",
)
POST_REF = "11111111-2222-4333-8444-555555555555"
#: A November drawn in whole weeks: 42 days.
FROM, TO = date(2026, 10, 26), date(2026, 12, 7)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def _planned(n, provider="gdrive", **over):
    """A planned story as the database answers it, in the columns the past-posts
    projection reads: a Drive story has no origin account."""
    past_posts = provider == "instagram_past_posts"
    return {
        "id": f"i-{n}",
        "file_name": vocabulary.PAST_POSTS_ITEM_NAME if past_posts else "f.jpg",
        "source_provider": provider,
        "origin_handle": "example_handle" if past_posts else None,
        "original_at": POSTED_AT if past_posts else None,
        "slide_post_ref": None,
        "slide_position": None,
        "slide_count": None,
        **over,
    }


def _shown(row, provenance=None, **over):
    """That story as the page is handed it: the provenance nested, the columns
    it came from gone."""
    shown = {k: v for k, v in row.items() if k not in PROVENANCE_COLUMNS}
    return {**shown, "provenance": provenance, **over}


async def _read(ex, **kwargs):
    kwargs = {"workspace_id": WS, "from_date": FROM, "to_date": TO, **kwargs}
    return await upcoming.upcoming(ex, **kwargs)


class TestTheThreeStatements:
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
        assert len(ex.sent) == 3
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

    async def test_the_series_are_read_for_the_range_and_bounded(self):
        ex = _Executor()
        await _read(ex)
        repeating, params = ex.sent[2]
        assert repeating.startswith("SELECT 'repeat' AS kind,")
        assert repeating.endswith("ORDER BY t.slot, i.id LIMIT :lim")
        assert params == {
            "ws": WS,
            "from_date": FROM,
            "to_date": TO,
            "lim": upcoming.REPEATING_MAX + 1,
        }

    def test_a_series_is_projected_from_a_live_planned_story_with_a_rule(self):
        repeating = _flat(upcoming._REPEATING_SQL)
        assert "i.origin = 'planned' AND i.repeat_unit IS NOT NULL" in repeating
        assert f"AND i.{intent_ledger.NOT_TERMINAL}" in repeating
        assert f"{intent_ledger.repeat_sql('i.')} AS repeat" in repeating
        # a time that has come is the clock's, as a predicted slot's is
        assert "AND t.slot > now()" in repeating
        # a story at or after the range's end has no later occurrence in it
        assert f"AND i.schedule_slot_at < {upcoming._SERIES_TO}" in repeating

    def test_the_grid_is_bounded_in_the_joined_zone_so_it_can_be_inlined(self):
        """No sub-select among `fn_repeat_times`'s arguments: one keeps the
        planner from inlining the function, and the grid would be planned once
        per series. `w.tz` is the value the sub-select reads."""
        repeating = _flat(upcoming._REPEATING_SQL)
        call = repeating.split("fn_repeat_times(", 1)[1].split(") AS t (slot)", 1)[0]
        assert "SELECT" not in call
        assert f"GREATEST({upcoming._SERIES_FROM}," in call
        assert call.endswith(upcoming._SERIES_TO)

    def test_every_table_is_read_under_the_workspace(self):
        """The predicate is the first fence: a login that bypasses the policies
        still reads one workspace."""
        planned, predicted, repeating = (
            _flat(upcoming._PLANNED_SQL),
            _flat(upcoming._PREDICTED_SQL),
            _flat(upcoming._REPEATING_SQL),
        )
        for fence in (
            "WHERE i.workspace_id = :ws",
            "m.workspace_id = i.workspace_id",
            "a.workspace_id = i.workspace_id",
            "w.id = i.workspace_id",
        ):
            assert fence in planned, fence
            assert fence in repeating, fence
        assert "WHERE a.workspace_id = :ws AND w.id = :ws AND" in predicted
        zone = "(SELECT tz FROM workspaces WHERE id = :ws)"
        assert planned.count(zone) == 2 and predicted.count(zone) == 2
        # the series' bounds read the joined workspace's zone instead
        assert repeating.count(zone) == 0


class TestTheBounds:
    async def test_each_list_asks_for_one_over_its_limit_and_says_when_cut(self):
        ex = _Executor(
            [_planned(n) for n in range(3)],
            [{"n": n} for n in range(4)],
            [_planned(n, kind="repeat") for n in range(5)],
        )
        out = await _read(ex, planned_limit=2, predicted_limit=3, repeating_limit=4)
        assert [params["lim"] for _, params in ex.sent] == [3, 4, 5]
        assert out["planned"] == [_shown(_planned(n)) for n in range(2)]
        assert out["planned_truncated"]
        assert len(out["predicted"]) == 3 and out["predicted_truncated"]
        assert len(out["repeating"]) == 4 and out["repeating_truncated"]

    async def test_lists_at_their_limits_come_back_whole_as_read(self):
        planned = [_planned(1), _planned(2)]
        predicted = [{"kind": "predicted", "n": n} for n in range(3)]
        repeating = [_planned(n, kind="repeat") for n in range(4)]
        ex = _Executor(planned, predicted, repeating)
        assert await _read(
            ex, planned_limit=2, predicted_limit=3, repeating_limit=4
        ) == {
            "planned": [_shown(row) for row in planned],
            "planned_truncated": False,
            "predicted": predicted,
            "predicted_truncated": False,
            "repeating": [_shown(row) for row in repeating],
            "repeating_truncated": False,
        }

    async def test_a_wider_range_is_cut_to_the_maximum_for_every_statement(self):
        ex = _Executor()
        await _read(ex, to_date=FROM + timedelta(days=400))
        cut = FROM + timedelta(days=vocabulary.RANGE_MAX_DAYS)
        assert [params["to_date"] for _, params in ex.sent] == [cut, cut, cut]
        # the maximum's 45 days, the lead's 2, and a part of a day at each end
        assert ex.sent[1][1]["span_days"] == 49

    async def test_the_last_dates_are_served_without_overflow(self):
        ex = _Executor()
        await _read(ex, from_date=date(9999, 12, 1), to_date=date.max)
        assert [params["to_date"] for _, params in ex.sent] == [date.max] * 3

    def test_a_walk_is_bounded_by_the_slots_its_days_hold(self):
        """The recursion's own bound, beside the range's end: the account's
        posts per day times the days the walk spans. No row can pin it, since
        it cuts no walk."""
        assert "WHERE s.slot < b.hi AND s.step <= a.eff_ppd * :span_days)" in _flat(
            upcoming._PREDICTED_SQL
        )


class TestAPlannedPastPostsStory:
    """`planned` rows are the intents read's, read through the same projection
    as the Queue's (`workspaces._past_posts_row`): the calendar tells a past-posts
    story's picture from a Drive picture the way the Queue does, and names the
    story for where it came from, never by Instagram's id."""

    async def test_it_reads_under_its_own_name_with_its_provenance(self):
        past_posts, drive = _planned(1, "instagram_past_posts"), _planned(2)
        out = await _read(_Executor([past_posts, drive], []))
        assert out["planned"] == [
            _shown(
                past_posts,
                provenance={
                    "provider": "instagram_past_posts",
                    "origin_handle": "example_handle",
                    "original_at": POSTED_AT,
                    "slide": None,
                },
                file_name="Instagram post",
            ),
            # The positive control: a Drive story beside it is as it was.
            _shown(drive),
        ]
        assert GRAPH_ID not in repr(out)

    async def test_a_repeating_past_posts_story_reads_the_same_way(self):
        """The calendar's repeating list (#485) is read through the same
        projection: a past-posts story's series is named for where it came from,
        and a name stored as `Instagram <Graph id>` never shows that id."""
        legacy = _planned(
            1, "instagram_past_posts", kind="repeat", file_name=f"Instagram {GRAPH_ID}"
        )
        out = await _read(_Executor([], [], [legacy]))
        assert out["repeating"] == [
            _shown(
                legacy,
                provenance={
                    "provider": "instagram_past_posts",
                    "origin_handle": "example_handle",
                    "original_at": POSTED_AT,
                    "slide": None,
                },
                file_name="Instagram post",
            )
        ]
        assert GRAPH_ID not in repr(out)

    @pytest.mark.parametrize("count", [4, None])
    async def test_planned_slide_has_its_server_name_and_public_provenance(self, count):
        slide = _planned(
            1,
            "instagram_past_posts",
            slide_post_ref=POST_REF,
            slide_position=2,
            slide_count=count,
        )
        out = await _read(_Executor([slide], []))
        shown = out["planned"][0]
        assert shown["file_name"] == (
            "Instagram post, slide 2 of 4" if count == 4 else "Instagram post, slide 2"
        )
        assert shown["provenance"] == {
            "provider": "instagram_past_posts",
            "origin_handle": "example_handle",
            "original_at": POSTED_AT,
            "slide": {"post_ref": POST_REF, "position": 2, "count": count},
        }
        assert not set(PROVENANCE_COLUMNS) & set(shown)
        assert GRAPH_ID not in repr(out)

    async def test_an_instagram_graph_id_name_reads_as_the_neutral_one(self):
        """A past-posts item stored as `Instagram <Graph id>` reads under the
        neutral name: the projection hides the Graph id."""
        legacy = _planned(1, "instagram_past_posts", file_name=f"Instagram {GRAPH_ID}")
        out = await _read(_Executor([legacy], []))
        assert [p["file_name"] for p in out["planned"]] == [
            vocabulary.PAST_POSTS_ITEM_NAME
        ]
        assert GRAPH_ID not in repr(out)

    async def test_the_statement_selects_the_intents_picture_columns_and_joins(self):
        ex = _Executor()
        await _read(ex)
        planned, _ = ex.sent[0]
        assert workspaces._picture_sql("m.") in planned
        assert workspaces.picture_joins("m.") in planned
        assert "LEFT JOIN instagram_media_assets ma" in planned
        assert "mi.parent_media_id = ma.parent_media_id" in planned
        for column in PROVENANCE_COLUMNS:
            assert f"AS {column}" in planned

    async def test_a_row_that_lacks_the_provenance_columns_is_refused(self):
        """A statement that forgot to select them would read every past-posts story
        as a Drive story, and the calendar would draw it on the Drive path."""
        row = _planned(1, "instagram_past_posts")
        del row["source_provider"]
        with pytest.raises(KeyError):
            await _read(_Executor([row], []))


class TestTheClocksOwnExpressions:
    """The projection is the clock's, read from the schema code rather than
    trusted to a comment: the accounts it walks are the ones the tick mints
    for, under the settings the tick advances their cursors by, one step of
    the walk being the tick's own advance. The gate cannot see this: its
    measure of the clock is `fn_next_slot` under settings the test names."""

    def test_the_accounts_are_the_ones_the_clock_posts_for(self):
        """`_POSTING_SQL` is the `plan_slot` leg's WHERE less its due-now
        conjunct, which `test_content_runway.py` pins to the schema code."""
        assert f" AND {content_runway._POSTING_SQL})" in upcoming._PREDICTED_SQL

    def test_the_settings_are_the_legs_own(self):
        """Each of the four, as the leg's own select list writes it and under
        the leg's own name."""
        columns = _plan_slot_columns()
        predicted = _flat(upcoming._PREDICTED_SQL)
        for name in ("eff_tz", "eff_ppd", "eff_start", "eff_end"):
            assert f"{columns[name]} AS {name}" in predicted, name

    def test_a_step_is_the_legs_cursor_advance(self):
        """The same function over the same settings in the same order, from
        the slot before instead of the cursor. Both overloads of the tick
        advance their cursor, each once and by the same expression."""
        advances = [
            re.findall(r"SET next_slot_at = (fn_next_slot\([^)]*\))", body)
            for body in function_definitions("fn_clock_tick").values()
        ]
        assert [len(found) for found in advances] == [1, 1], (
            "both clock arities advance their cursor"
        )
        assert len({found[0] for found in advances}) == 1, (
            "both arities use the identical expression"
        )
        ours = advances[0][0].replace("d.next_slot_at", "{after}").replace("d.", "a.")
        assert ours == upcoming._NEXT_SLOT
        assert upcoming._NEXT_SLOT.format(after="s.slot") in upcoming._PREDICTED_SQL
        assert upcoming._NEXT_SLOT.format(after="b.walk_from") in (
            upcoming._PREDICTED_SQL
        )


class TestTheTriggersOwnGrid:
    """A series is projected as its successors are made, read from the
    trigger's body in the schema code rather than trusted to a comment."""

    TRIGGER = _flat(function_body("trg_intent_repeat"))

    def test_the_grid_is_asked_as_the_trigger_asks_it(self):
        """The story's own rule and anchor, its zone (the account's, else the
        workspace's, which the grid reads through `fn_safe_tz` either way),
        from just after its own time. After a post the trigger asks from the
        post's next local day, which a series has no other time before: it has
        one time a day, and a story posted at its own time was that day's. A
        change to that rule fails this until the read answers it."""
        assert (
            "fn_repeat_times(NEW.repeat_unit, NEW.repeat_every, NEW.repeat_days,"
            " NEW.repeat_at, NEW.schedule_slot_at, v_tz, v_lo, NULL)"
        ) in self.TRIGGER
        assert (
            "v_lo := CASE WHEN NEW.last_error->>'class' = 'planned_missed' THEN now()"
            " ELSE NEW.schedule_slot_at END + interval '1 microsecond';"
            " IF NEW.state = 'posted' THEN"
            " v_day := (now() AT TIME ZONE v_tz)::date + 1;"
            " v_lo := GREATEST(v_lo, COALESCE(fn_wall_instant(v_day, v_tz),"
            " v_day AT TIME ZONE v_tz)); END IF;"
        ) in self.TRIGGER
        assert "SELECT w.state, fn_safe_tz(COALESCE(a.tz, w.tz))," in self.TRIGGER
        repeating = _flat(upcoming._REPEATING_SQL)
        assert (
            "fn_repeat_times(i.repeat_unit, i.repeat_every, i.repeat_days,"
            " i.repeat_at, i.schedule_slot_at, COALESCE(a.tz, w.tz), GREATEST("
        ) in repeating
        assert "i.schedule_slot_at + interval '1 microsecond')" in repeating

    def test_only_where_the_trigger_makes_a_successor(self):
        """The trigger leaves a series without a successor in two places: a
        miss the item or account caused, which no read can see coming, and a
        home gone (an offboarding workspace, an account no longer live), which
        the read answers. A third exit fails this until the read answers it
        too. The account's predicate is pinned against the trigger by the
        schedule gate (`test_schedule_verbs_gate`).

        The read is stricter than the trigger in two places. It asks for a
        posting workspace, not only one not offboarding, as the predicted slots
        do. And it leaves out a story parked for review, whose next days hang
        on how the review ends."""
        assert self.TRIGGER.count("RETURN NULL") == 3  # the two exits, and its end
        assert "IF NOT FOUND OR v_ws = 'offboarding' THEN RETURN NULL" in self.TRIGGER
        repeating = _flat(upcoming._REPEATING_SQL)
        assert (
            f"AND {content_runway._WORKSPACE_POSTING_SQL}"
            f" AND {intent_ledger.LIVE_ACCOUNT}"
        ) in repeating
        assert "'offboarding'" not in content_runway._WORKSPACE_POSTING_SQL
        assert "w.state = 'active'" in content_runway._WORKSPACE_POSTING_SQL, (
            "an offboarding workspace is not active, so it is still left out"
        )
        assert "AND i.state <> 'review_required'" in repeating
