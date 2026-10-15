"""A planned story made and moved through the command port (#1413),
at the SQL seam: the statements `schedule_item`, `reschedule_item` and
`cancel` send, with their parameters; the refusals decided in Python before
the database is asked (an id, the time's shape, the override's type, the lock
and item rule); and the Queue read's filter and order.

What only PostgreSQL can say (the zone's reading, the live-subject key, the
tenant's policies) is the gate's: `tests/scripts/test_schedule_verbs_gate.py`.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from src.services.target.commands import Command, CommandRefused  # noqa: I001 — the port first: the registry cycle
from src.services.target import (
    command_executors,
    intent_ledger,
    prompts,
    vocabulary,
    workspaces,
)
from tests.src.services.target.test_provisioning import _ScriptedExecutor

WS = "7a1c3d5e-0000-4000-8000-000000000001"
USER = "7a1c3d5e-0000-4000-8000-000000000002"
ACCOUNT = "7a1c3d5e-0000-4000-8000-00000000000a"
ITEM = "7a1c3d5e-0000-4000-8000-00000000000b"
STORY = "7a1c3d5e-0000-4000-8000-00000000000c"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
AT = datetime(2026, 10, 12, 22, 30, tzinfo=timezone.utc)
HORIZON = NOW + timedelta(days=vocabulary.PLAN_HORIZON_DAYS)


def _command(kind: str, **args) -> Command:
    return Command(
        kind=kind, workspace_id=WS, actor_user_id=USER, channel="cli", args=args
    )


def _schedule(**args) -> Command:
    return _command(
        "schedule_item",
        **{
            "ig_account_id": ACCOUNT,
            "media_item_id": ITEM,
            "local_at": "2026-10-12 18:30",
            **args,
        },
    )


def _reschedule(**args) -> Command:
    return _command(
        "reschedule_item",
        **{"intent_id": STORY, "local_at": "2026-10-13 09:00", **args},
    )


def _account(tz="America/New_York"):
    return (1, {"provider_account_ref": "ref-1", "eff_tz": tz})


def _item(state="available", locks=()):
    return (1, {"state": state, "locks": list(locks)})


def _instant(at=AT):
    return (1, {"at": at, "now": NOW, "horizon": HORIZON})


def _born(repeat=None):
    return (1, {"id": STORY, "workspace_id": WS, "repeat": repeat})


def _moved(repeat=None):
    """A move's UPDATE answering with the story's rule, in its one shape."""
    return (1, {"repeat": repeat})


def _story(**over):
    """The row `_intent_row` reads: a planned story still waiting."""
    return (
        1,
        {
            "id": STORY,
            "workspace_id": WS,
            "state": "scheduled",
            "origin": "planned",
            "cancel_requested": False,
            "eff_tz": "Europe/London",
            "schedule_slot_at": AT,
            **over,
        },
    )


class TestScheduleItem:
    async def test_a_planned_story_is_inserted_under_the_person_and_audited(self):
        ex = _ScriptedExecutor(
            _account(), _item(), _instant(), _born(), (1, None), (1, [{"id": "b1"}])
        )
        result = await command_executors.schedule_item(ex, _schedule())
        assert (result.outcome, result.data) == (
            "executed",
            {
                "intent_id": STORY,
                "state": "scheduled",
                "schedule_slot_at": AT.isoformat(),
                "tz": "America/New_York",
                "local_at": "2026-10-12 18:30:00",
                "repeat": None,
                "overridden": [],
                "warnings": [],
            },
        )
        account, item, instant, insert, audit, bindings = ex.statements
        # each read names its tenant: the gates run under the policies, which
        # would hide another workspace's row whether or not the SQL says so
        assert " WHERE a.id = :acct AND a.workspace_id = :ws" in account[0]
        assert "WHERE m.id = :media AND m.workspace_id = :ws" in item[0]
        # the account is held until the story is in, so a removal cannot slip
        # between this read and the INSERT
        assert " FOR SHARE OF a" in account[0]
        assert account[1] == {"acct": ACCOUNT, "ws": WS}
        assert item[1] == {"acct": ACCOUNT, "media": ITEM, "ws": WS}
        assert instant[1] == {
            "local_at": "2026-10-12 18:30:00",
            "tz": "America/New_York",
            "horizon": vocabulary.PLAN_HORIZON_DAYS,
        }
        values = " ".join(insert[0].split())
        assert "VALUES (:ws, :acct, :media, :ref, 'manual', :at, 'scheduled'," in values
        assert "'scheduled', 'planned', :by, :repeat_unit, :repeat_every," in values
        assert f"WHERE {intent_ledger.NOT_TERMINAL} DO NOTHING" in insert[0]
        assert insert[1] == {
            "ws": WS,
            "acct": ACCOUNT,
            "media": ITEM,
            "ref": "ref-1",
            "at": AT,
            "by": USER,
            **command_executors._NO_REPEAT,
        }, "no rule: the four columns are written NULL"
        assert (audit[1]["from_state"], audit[1]["to_state"]) == (None, "scheduled")
        assert audit[1]["intent"] == STORY
        assert json.loads(audit[1]["detail"]) == {
            "v": 1,
            "event": "scheduled",
            "at": AT.isoformat(),
            "tz": "America/New_York",
            "local_at": "2026-10-12 18:30:00",
        }
        assert bindings[1] == {"ws": WS}

    async def test_a_duplicate_is_the_databases_and_names_the_story_in_the_way(self):
        ex = _ScriptedExecutor(
            _account(),
            _item(),
            _instant(),
            (0, None),  # the INSERT did nothing: the live-subject key holds
            (
                1,
                {
                    "id": STORY,
                    "state": "approved",
                    "origin": "cadence",
                    "cancel_requested": True,
                },
            ),
        )
        with pytest.raises(CommandRefused) as refused:
            await command_executors.schedule_item(ex, _schedule())
        assert refused.value.reason == "illegal_transition"
        assert refused.value.facts == {
            "existing": {
                "intent_id": STORY,
                "state": "approved",
                "origin": "cadence",
                "cancel_requested": True,
            }
        }
        assert len(ex.statements) == 5, "no audit row, nothing else"
        sql, params = ex.statements[-1]
        assert " WHERE workspace_id = :ws AND media_item_id = :media" in sql
        assert f"AND {intent_ledger.NOT_TERMINAL}" in sql
        assert params == {"ws": WS, "media": ITEM, "acct": ACCOUNT}

    async def test_a_duplicate_that_ended_before_it_was_read_names_nothing(self):
        ex = _ScriptedExecutor(_account(), _item(), _instant(), (0, None), (0, None))
        with pytest.raises(CommandRefused) as refused:
            await command_executors.schedule_item(ex, _schedule())
        assert (refused.value.reason, refused.value.facts) == ("illegal_transition", {})

    @pytest.mark.parametrize(
        "state, locks, in_the_way",
        [
            ("removed", [], ["item_removed"]),
            ("unsupported", ["skip"], ["item_unsupported", "skip"]),
            ("available", ["hold", "recent"], ["hold", "recent"]),
            ("available", ["reject", "seasonal"], ["reject", "seasonal"]),
        ],
    )
    async def test_what_blocks_is_refused_even_with_the_override(
        self, state, locks, in_the_way
    ):
        """An item that cannot post, or a lock that would miss it at its
        time, refuses whatever the person sends; the warnings ride along, so
        the refusal names everything in the way."""
        ex = _ScriptedExecutor(_account(), _item(state, locks), _instant())
        with pytest.raises(CommandRefused) as refused:
            await command_executors.schedule_item(ex, _schedule(override_locks=True))
        assert refused.value.reason == "locked"
        assert refused.value.facts == {"in_the_way": in_the_way, "overridable": False}
        assert len(ex.statements) == 3, "nothing is written"

    async def test_a_warning_refuses_until_the_override_and_says_it_would_pass(self):
        ex = _ScriptedExecutor(_account(), _item(locks=["recent", "skip"]), _instant())
        with pytest.raises(CommandRefused) as refused:
            await command_executors.schedule_item(ex, _schedule())
        assert refused.value.facts == {
            "in_the_way": ["recent", "skip"],
            "overridable": True,
        }

    async def test_the_override_gets_past_the_warnings_and_is_audited(self):
        ex = _ScriptedExecutor(
            _account(),
            _item(locks=["recent", "skip"]),
            _instant(),
            _born(),
            (1, None),
            (1, []),
        )
        result = await command_executors.schedule_item(
            ex, _schedule(override_locks=True)
        )
        assert result.data["overridden"] == ["recent", "skip"]
        assert result.data["warnings"] == [vocabulary.NO_PUSH_BINDING], "no chat bound"
        detail = json.loads(ex.statements[4][1]["detail"])
        assert detail["override"] == ["recent", "skip"]

    @pytest.mark.parametrize("override", ["yes", 1, None])
    async def test_the_override_is_true_or_false_before_anything_is_read(
        self, override
    ):
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.schedule_item(
                ex, _schedule(override_locks=override)
            )
        assert refused.value.reason == "invalid_args" and ex.statements == []

    @pytest.mark.parametrize("field", ["ig_account_id", "media_item_id"])
    async def test_an_id_that_is_not_one_is_refused_by_name_before_any_read(
        self, field
    ):
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.schedule_item(ex, _schedule(**{field: "not-an-id"}))
        assert refused.value.reason == "invalid_args"
        assert f"{field} is not an id" in str(refused.value)
        assert ex.statements == []

    async def test_an_id_in_another_spelling_is_sent_in_its_canonical_form(self):
        ex = _ScriptedExecutor(
            _account(), _item(), _instant(), _born(), (1, None), (1, [])
        )
        await command_executors.schedule_item(
            ex,
            _schedule(
                ig_account_id="{" + ACCOUNT.upper() + "}",
                media_item_id=f"urn:uuid:{ITEM}",
            ),
        )
        insert = ex.statements[3][1]
        assert (insert["acct"], insert["media"]) == (ACCOUNT, ITEM)

    @pytest.mark.parametrize("missing", ["account", "item"])
    async def test_what_is_not_found_is_named(self, missing):
        found = (_account(), (0, None)) if missing == "item" else ((0, None),)
        ex = _ScriptedExecutor(*found)
        with pytest.raises(CommandRefused) as refused:
            await command_executors.schedule_item(ex, _schedule())
        assert refused.value.reason == "not_found"
        assert refused.value.facts == {"missing": missing}


class TestTheWallTime:
    @pytest.mark.parametrize(
        "given, read",
        [
            ("2026-10-12 18:30", "2026-10-12 18:30:00"),
            ("2026-10-12T18:30", "2026-10-12 18:30:00"),
            ("2026-10-12 18:30:05", "2026-10-12 18:30:05"),
            (" 2026-10-12 18:30 ", "2026-10-12 18:30:00"),
        ],
    )
    def test_a_date_and_a_clock_time_is_read_as_given(self, given, read):
        assert command_executors._local_at(_schedule(local_at=given)) == read

    @pytest.mark.parametrize(
        "given, rule",
        [
            ("2026-10-12 18:30+02:00", "shape"),
            ("2026-10-12 18:30Z", "shape"),
            ("2026-10-12", "shape"),
            ("tomorrow 6pm", "shape"),
            # digits of another script are not the shape, whatever `\d` says
            ("٢٠٢٦-١٠-١٢ ١٨:٣٠", "shape"),
            ("２０２６-１０-１２ １８:３０", "shape"),
            ("2027-02-29 10:00", "not_a_date"),
            ("2026-10-12 24:00", "not_a_date"),
            ("2026-10-12 18:60", "not_a_date"),
        ],
    )
    def test_anything_else_names_the_rule_it_broke(self, given, rule):
        with pytest.raises(CommandRefused) as refused:
            command_executors._local_at(_schedule(local_at=given))
        assert refused.value.reason == "invalid_args"
        assert refused.value.facts == {"at_rule": rule}

    @pytest.mark.parametrize(
        "at, rule",
        [
            (None, "skipped"),
            (NOW, "past"),
            (NOW - timedelta(minutes=1), "past"),
            (HORIZON + timedelta(seconds=1), "horizon"),
        ],
    )
    async def test_the_databases_reading_names_the_rule_it_broke(self, at, rule):
        ex = _ScriptedExecutor(_instant(at))
        with pytest.raises(CommandRefused) as refused:
            await command_executors._planned_instant(
                ex, local_at="2026-10-12 18:30:00", tz="UTC"
            )
        assert refused.value.facts == {"at_rule": rule}

    async def test_the_horizons_last_instant_is_inside_it(self):
        ex = _ScriptedExecutor(_instant(HORIZON))
        assert (
            await command_executors._planned_instant(
                ex, local_at="2027-10-01 12:00:00", tz="UTC"
            )
            == HORIZON
        )


class TestRescheduleItem:
    async def test_the_story_moves_in_place_and_the_move_is_audited(self):
        later = AT + timedelta(hours=10)
        ex = _ScriptedExecutor(_story(), _instant(later), _moved(), (1, None))
        result = await command_executors.reschedule_item(ex, _reschedule())
        assert result.data == {
            "intent_id": STORY,
            "state": "scheduled",
            "schedule_slot_at": later.isoformat(),
            "tz": "Europe/London",
            "repeat": None,
            "previous_slot_at": AT.isoformat(),
            "local_at": "2026-10-13 09:00:00",
        }
        row, instant, update, audit = ex.statements
        assert "FOR UPDATE OF i" in row[0]
        assert (row[1]["id"], row[1]["ws"]) == (STORY, WS)
        assert instant[1]["tz"] == "Europe/London", "the story's own zone"
        assert "WHERE id = :id AND workspace_id = :ws" in update[0]
        assert update[1] == {
            "at": later,
            "wall": time(9, 0),
            "days": None,
            "id": STORY,
            "ws": WS,
        }
        assert (audit[1]["from_state"], audit[1]["to_state"]) == (
            "scheduled",
            "scheduled",
        )
        assert json.loads(audit[1]["detail"]) == {
            "v": 1,
            "event": "rescheduled",
            "from": AT.isoformat(),
            "to": later.isoformat(),
            "tz": "Europe/London",
            "local_at": "2026-10-13 09:00:00",
        }

    @pytest.mark.parametrize(
        "over, reason",
        [
            ({"origin": "cadence"}, "illegal_transition"),
            *[
                ({"state": state}, "illegal_transition")
                for state in vocabulary.INTENT_STATES
                if state != "scheduled"
            ],
            ({"cancel_requested": True}, "cancelling"),
        ],
    )
    async def test_the_story_is_judged_before_its_new_time(self, over, reason):
        """A story that cannot move says so whatever time it was given: its
        time is never read, so a bad one cannot hide the real answer."""
        ex = _ScriptedExecutor(_story(**over))
        with pytest.raises(CommandRefused) as refused:
            await command_executors.reschedule_item(ex, _reschedule(local_at="soon"))
        assert refused.value.reason == reason
        assert len(ex.statements) == 1, "only the row was read"

    async def test_an_id_in_another_spelling_reads_the_row_by_its_canonical_form(
        self,
    ):
        ex = _ScriptedExecutor(_story(), _instant(), _moved(), (1, None))
        await command_executors.reschedule_item(
            ex, _reschedule(intent_id="{" + STORY.upper() + "}")
        )
        assert ex.statements[0][1]["id"] == STORY

    async def test_an_id_that_is_not_one_is_refused_before_the_row_is_read(self):
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.reschedule_item(ex, _reschedule(intent_id="12"))
        assert refused.value.reason == "invalid_args" and ex.statements == []


class TestCancel:
    async def test_the_flag_is_bound_to_the_tenant_and_audited_as_no_move(
        self, monkeypatch
    ):
        """The flag is not a state change, so the intent's trigger writes no
        row for it: the executor's own row names the person, and its from and
        to states are equal, so a card never reads it as a move."""
        said = []

        async def _record_outcome(session, intent, command, state, *, series=None):
            assert series is None, "a plain cancel ends no series"
            said.append(state)

        monkeypatch.setattr(command_executors, "_record_outcome", _record_outcome)
        ex = _ScriptedExecutor(_story(), (1, None), (1, None))
        result = await command_executors.cancel(ex, _command("cancel", intent_id=STORY))
        assert result.data == {
            "intent_id": STORY,
            "state": "scheduled",
            "cancel_requested": True,
        }
        _, update, audit = ex.statements
        assert "WHERE id = :id AND workspace_id = :ws" in update[0]
        assert update[1] == {"id": STORY, "ws": WS}
        assert (audit[1]["from_state"], audit[1]["to_state"]) == (
            "scheduled",
            "scheduled",
        )
        assert json.loads(audit[1]["detail"]) == {"v": 1, "event": "cancel_requested"}
        assert said == ["cancelled"]


WEEKLY = {"unit": "week", "every": 1, "days": [1, 4], "at": "16:07"}
LIVE = [s for s in vocabulary.INTENT_STATES if s not in vocabulary.TERMINAL_STATES]


class TestTheRepeatArgument:
    """`_repeat_arg` checks types and fills defaults; bounds and combinations
    are the CHECKs' (#485). 2026-10-15 is a Thursday."""

    WALL = datetime(2026, 10, 15, 16, 7)

    def _arg(self, repeat):
        return command_executors._repeat_arg(_schedule(repeat=repeat), wall=self.WALL)

    def test_no_rule_is_none(self):
        assert self._arg(None) is None
        assert command_executors._repeat_arg(_schedule(), wall=self.WALL) is None

    def test_a_weekly_rule_defaults_to_the_chosen_weekday_and_every_one(self):
        assert self._arg({"unit": "week"}) == {
            "repeat_unit": "week",
            "repeat_every": 1,
            "repeat_days": [4],
            "repeat_at": time(16, 7),
        }

    def test_a_daily_rule_has_no_days_and_the_chosen_time_of_day(self):
        assert self._arg({"unit": "day", "every": 3}) == {
            "repeat_unit": "day",
            "repeat_every": 3,
            "repeat_days": None,
            "repeat_at": time(16, 7),
        }

    def test_days_are_kept_in_order_once_each(self):
        assert self._arg({"unit": "week", "days": [4, 1, 4]})["repeat_days"] == [1, 4]

    def test_a_rules_time_of_day_is_to_the_minute(self):
        """As every surface shows it: a rule set at 16:07:30 repeats at 16:07."""
        arg = command_executors._repeat_arg(
            _schedule(repeat={"unit": "day"}), wall=self.WALL.replace(second=30)
        )
        assert arg["repeat_at"] == time(16, 7)

    @pytest.mark.parametrize("days", [[1, 2], [1]], ids=repr)
    def test_named_days_must_include_the_storys_own(self, days):
        """Off them, the series would post on the story's day and on its own.
        Days a person named are never carried, even one."""
        with pytest.raises(CommandRefused) as refused:
            self._arg({"unit": "week", "days": days})
        assert refused.value.reason == "invalid_args"
        assert refused.value.facts == {"repeat": "off_day"}
        assert refused.value.facts["repeat"] in vocabulary.REPEAT_SENTENCES

    @pytest.mark.parametrize(
        "kept, days", [([1, 4], [1, 4]), ([1], [4]), (None, [4])], ids=repr
    )
    def test_a_weekly_rule_without_days_keeps_a_weekly_series_days(self, kept, days):
        """A series of one day takes the story's; none kept, the story's day."""
        arg = command_executors._repeat_arg(
            _schedule(repeat={"unit": "week", "every": 2}),
            wall=self.WALL,
            kept_days=kept,
        )
        assert arg["repeat_days"] == days

    def test_kept_days_off_the_storys_own_are_refused(self):
        with pytest.raises(CommandRefused) as refused:
            command_executors._repeat_arg(
                _schedule(repeat={"unit": "week"}), wall=self.WALL, kept_days=[1, 2]
            )
        assert refused.value.facts == {"repeat": "off_day"}

    @pytest.mark.parametrize(
        "rule",
        [
            {"unit": "day", "every": 0},
            {"unit": "week", "days": [9]},
            {"unit": "day", "days": [1]},
            {"unit": "month"},
        ],
        ids=repr,
    )
    def test_bounds_are_left_to_the_checks(self, rule):
        """A rule the CHECKs refuse is still well-typed here: it reaches the
        database, which judges it."""
        assert self._arg(rule)["repeat_unit"] == rule["unit"]

    @pytest.mark.parametrize(
        "repeat",
        [
            "week",
            ["week"],
            {"unit": "week", "evry": 2},
            {"unit": 7},
            {"unit": "week", "every": "2"},
            {"unit": "week", "every": True},
            {"unit": "week", "every": 2.0},
            {"unit": "day", "every": 40000},
            {"unit": "week", "days": "mon"},
            {"unit": "week", "days": [1, True]},
            {"unit": "week", "days": [70000]},
        ],
        ids=repr,
    )
    def test_anything_ill_typed_is_refused_as_the_repeat(self, repeat):
        """A number too large for its smallint column is a type error here: the
        driver would refuse it outside any CHECK, and answer a 500."""
        with pytest.raises(CommandRefused) as refused:
            self._arg(repeat)
        assert refused.value.reason == "invalid_args"
        assert refused.value.facts == {"repeat": "invalid"}
        assert refused.value.facts["repeat"] in vocabulary.REPEAT_SENTENCES, (
            "the CLI has words for the fact"
        )


class TestTheRuleChecksAreNamed:
    """A CHECK refusal is matched by the constraint's name. The terminal guard
    raises the same SQLSTATE and names none, and must not read as a bad rule."""

    async def _write(self, constraint_name):
        class _Failing:
            async def execute(self, statement, params=None):
                raise DBAPIError(
                    "UPDATE", {}, SimpleNamespace(constraint_name=constraint_name)
                )

        return await command_executors._write_rule(_Failing(), "UPDATE post_intents")

    @pytest.mark.parametrize("name", command_executors._REPEAT_CHECKS)
    async def test_a_rule_check_is_the_callers_rule(self, name):
        with pytest.raises(CommandRefused) as refused:
            await self._write(name)
        assert refused.value.reason == "invalid_args"
        assert refused.value.facts == {"repeat": "invalid"}
        assert name in str(refused.value)

    @pytest.mark.parametrize("name", [None, "ck_intent_state"])
    async def test_any_other_violation_is_not(self, name):
        with pytest.raises(DBAPIError):
            await self._write(name)


def _rule(**over):
    return {
        "repeat_unit": "week",
        "repeat_every": 1,
        "repeat_days": [1, 4],
        "repeat_at": time(16, 7),
        **over,
    }


class TestRescheduleARepeat:
    """`reschedule_item`'s branches (#485): a move, a new rule, both, and a
    clear, each with the statements it sends."""

    async def test_a_move_carries_the_series_time_of_day(self):
        later = AT + timedelta(hours=10)
        moved = {**WEEKLY, "at": "09:00"}
        ex = _ScriptedExecutor(
            _story(repeat_unit="week"), _instant(later), _moved(moved), (1, None)
        )
        result = await command_executors.reschedule_item(ex, _reschedule())
        assert result.data["repeat"] == moved
        _, _, update, audit = ex.statements
        sql = " ".join(update[0].split())
        assert "repeat_at = CASE WHEN repeat_unit IS NULL THEN NULL" in sql
        assert update[1]["wall"] == time(9, 0)
        assert json.loads(audit[1]["detail"])["repeat_at"] == "09:00"

    @pytest.mark.parametrize(
        "days, written",
        [([1], [2]), ([2, 4], [2, 4])],
        ids=["its_one_day_moves", "one_of_its_days"],
    )
    async def test_a_weekly_series_moves_to_its_new_day(self, days, written):
        """2026-10-13 is a Tuesday."""
        later = AT + timedelta(hours=10)
        ex = _ScriptedExecutor(
            _story(repeat_unit="week", repeat_days=days),
            _instant(later),
            _moved({**WEEKLY, "days": written, "at": "09:00"}),
            (1, None),
        )
        await command_executors.reschedule_item(ex, _reschedule())
        _, _, update, audit = ex.statements
        assert update[1]["days"] == written
        detail = json.loads(audit[1]["detail"])
        assert detail.get("repeat_days") == (written if written != days else None)

    async def test_a_weekly_series_does_not_move_off_its_days(self):
        """Mon and Thu, moved to a Tuesday: refused before anything is written."""
        ex = _ScriptedExecutor(
            _story(repeat_unit="week", repeat_days=[1, 4]), _instant(AT)
        )
        with pytest.raises(CommandRefused) as refused:
            await command_executors.reschedule_item(ex, _reschedule())
        assert refused.value.facts == {"repeat": "off_day"}
        assert len(ex.statements) == 2, "the row and the instant: nothing written"

    async def test_a_rule_alone_reads_the_storys_own_wall_time(self):
        """No new time: the rule repeats at the story's time of day in its zone,
        and a weekly rule's default day is that date's weekday."""
        set_to = {**WEEKLY, "days": [4]}
        ex = _ScriptedExecutor(
            _story(),
            (1, {"wall": datetime(2026, 10, 15, 16, 7)}),
            (1, {"repeat": set_to}),
            (1, None),
        )
        result = await command_executors.reschedule_item(
            ex, _command("reschedule_item", intent_id=STORY, repeat={"unit": "week"})
        )
        _, wall, write, audit = ex.statements
        assert wall[1] == {"at": AT, "tz": "Europe/London"}
        assert write[0] == command_executors._WRITE_REPEAT
        assert write[1] == {**_rule(repeat_days=[4]), "id": STORY, "ws": WS}
        assert json.loads(audit[1]["detail"]) == {
            "v": 1,
            "event": "repeat_set",
            "repeat": set_to,
        }
        assert result.data["repeat"] == set_to
        assert "previous_slot_at" not in result.data, "the story did not move"

    async def test_a_move_and_a_rule_take_the_new_time(self):
        later = AT + timedelta(hours=10)
        ex = _ScriptedExecutor(
            _story(),
            _instant(later),
            _moved(),
            (1, None),
            (1, {"repeat": {**WEEKLY, "unit": "day", "days": None, "at": "09:00"}}),
            (1, None),
        )
        await command_executors.reschedule_item(ex, _reschedule(repeat={"unit": "day"}))
        write = ex.statements[4]
        assert write[0] == command_executors._WRITE_REPEAT
        assert write[1]["repeat_at"] == time(9, 0), "the new local time, not the old"

    @pytest.mark.parametrize("state", LIVE)
    async def test_a_clear_takes_any_live_story_where_it_stands(self, state):
        ex = _ScriptedExecutor(
            _story(state=state, repeat_unit="day"), (1, None), (1, None)
        )
        result = await command_executors.reschedule_item(
            ex, _command("reschedule_item", intent_id=STORY, repeat=None)
        )
        _, write, audit = ex.statements
        assert write[0] == command_executors._WRITE_REPEAT
        assert write[1] == {**command_executors._NO_REPEAT, "id": STORY, "ws": WS}
        assert json.loads(audit[1]["detail"]) == {"v": 1, "event": "repeat_cleared"}
        assert (result.data["state"], result.data["repeat"]) == (state, None)
        assert result.data["repeat_stopped"] is True

    async def test_a_clear_takes_a_story_being_cancelled(self):
        """Clearing a rule is no lever on the story: the cancel lands, and makes
        no next occurrence."""
        ex = _ScriptedExecutor(
            _story(repeat_unit="day", cancel_requested=True), (1, None), (1, None)
        )
        result = await command_executors.reschedule_item(
            ex, _command("reschedule_item", intent_id=STORY, repeat=None)
        )
        assert ex.statements[1][0] == command_executors._WRITE_REPEAT
        assert result.data["repeat_stopped"] is True

    async def test_a_clear_of_a_story_with_no_rule_writes_nothing(self):
        ex = _ScriptedExecutor(_story())
        result = await command_executors.reschedule_item(
            ex, _command("reschedule_item", intent_id=STORY, repeat=None)
        )
        assert len(ex.statements) == 1, "only the row was read"
        assert result.data["repeat_stopped"] is False

    @pytest.mark.parametrize(
        "over, args",
        [
            ({"state": "awaiting_approval"}, {"repeat": {"unit": "day"}}),
            ({"state": "posted"}, {"repeat": None}),
            ({"state": "expired"}, {"repeat": None}),
            ({"origin": "cadence"}, {"repeat": None}),
            ({"origin": "cadence"}, {"repeat": {"unit": "day"}}),
        ],
    )
    async def test_where_a_rule_cannot_change_it_is_refused_unread(self, over, args):
        ex = _ScriptedExecutor(_story(**over))
        with pytest.raises(CommandRefused) as refused:
            await command_executors.reschedule_item(
                ex, _command("reschedule_item", intent_id=STORY, **args)
            )
        assert refused.value.reason == "illegal_transition"
        assert len(ex.statements) == 1, "only the row was read"

    async def test_neither_a_time_nor_a_rule_is_refused_before_any_read(self):
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.reschedule_item(
                ex, _command("reschedule_item", intent_id=STORY)
            )
        assert refused.value.reason == "invalid_args" and ex.statements == []


class TestCancelStopsTheRepeat:
    """`stop_repeat` clears the rule in the cancel's own transaction, on both
    of its branches, so the ending that follows makes no successor (#485)."""

    @pytest.fixture(autouse=True)
    def outcomes(self, monkeypatch):
        """The outcome lines the cancel asked for: its state, and what it says
        became of the series (#485)."""
        said = []

        async def _record_outcome(session, intent, command, state, *, series=None):
            said.append((state, series))

        monkeypatch.setattr(command_executors, "_record_outcome", _record_outcome)
        return said

    async def test_the_flag_branch_clears_the_rule_and_audits_it(self, outcomes):
        ex = _ScriptedExecutor(
            _story(repeat_unit="day"), (1, None), (1, None), (1, None), (1, None)
        )
        result = await command_executors.cancel(
            ex, _command("cancel", intent_id=STORY, stop_repeat=True)
        )
        assert result.data == {
            "intent_id": STORY,
            "state": "scheduled",
            "cancel_requested": True,
            "repeat_stopped": True,
        }
        _, clear, cleared, flag, flagged = ex.statements
        assert clear[0] == command_executors._WRITE_REPEAT
        assert clear[1] == {**command_executors._NO_REPEAT, "id": STORY, "ws": WS}
        assert json.loads(cleared[1]["detail"]) == {
            "v": 1,
            "event": "repeat_cleared",
            "stop_repeat": True,
        }
        assert "cancel_requested = true" in flag[0]
        assert json.loads(flagged[1]["detail"]) == {"v": 1, "event": "cancel_requested"}
        assert outcomes == [("cancelled", prompts.SERIES_STOPPED)], (
            "the card says the series stopped, since its header still repeats"
        )

    async def test_the_give_up_branch_clears_the_rule_before_the_ending(
        self, monkeypatch
    ):
        order = []

        async def _give_up(session, intent, command, op, *, series=None):
            order.append(("give_up", len(session.statements), series))
            return command_executors._result(intent, "cancelled")

        monkeypatch.setattr(command_executors, "_give_up", _give_up)
        ex = _ScriptedExecutor(
            _story(state="review_required", repeat_unit="week"),
            (1, None),
            (1, None),
            (1, None),
        )
        result = await command_executors.cancel(
            ex, _command("cancel", intent_id=STORY, stop_repeat=True)
        )
        assert result.data == {
            "intent_id": STORY,
            "state": "cancelled",
            "repeat_stopped": True,
        }
        assert ex.statements[1][0] == command_executors._WRITE_REPEAT
        assert order == [("give_up", 4, prompts.SERIES_STOPPED)], (
            "the rule is cleared before the ending, which says the series stopped"
        )

    async def test_a_story_being_cancelled_still_stops_its_series(
        self, monkeypatch, outcomes
    ):
        """The first cancel stands; the series is all that is left to stop,
        and the card the first cancel superseded says it stopped (#485)."""
        restated = []

        async def _restate_outcome(session, intent, command, state, *, series=None):
            restated.append((state, series))

        monkeypatch.setattr(command_executors, "_restate_outcome", _restate_outcome)
        ex = _ScriptedExecutor(
            _story(repeat_unit="day", cancel_requested=True), (1, None), (1, None)
        )
        result = await command_executors.cancel(
            ex, _command("cancel", intent_id=STORY, stop_repeat=True)
        )
        assert restated == [("cancelled", prompts.SERIES_STOPPED)]
        assert outcomes == [], "the first cancel's supersede is not repeated"
        assert result.data == {
            "intent_id": STORY,
            "state": "scheduled",
            "cancel_requested": True,
            "repeat_stopped": True,
        }
        _, clear, cleared = ex.statements
        assert clear[0] == command_executors._WRITE_REPEAT
        assert json.loads(cleared[1]["detail"])["event"] == "repeat_cleared"

    @pytest.mark.parametrize("stop_repeat", [True, False])
    async def test_a_story_being_cancelled_with_nothing_to_stop_is_refused(
        self, stop_repeat
    ):
        ex = _ScriptedExecutor(_story(cancel_requested=True))
        with pytest.raises(CommandRefused) as refused:
            await command_executors.cancel(
                ex, _command("cancel", intent_id=STORY, stop_repeat=stop_repeat)
            )
        assert refused.value.reason == "cancelling"

    async def test_a_story_with_no_rule_has_nothing_to_stop(self):
        ex = _ScriptedExecutor(_story(), (1, None), (1, None))
        result = await command_executors.cancel(
            ex, _command("cancel", intent_id=STORY, stop_repeat=True)
        )
        assert result.data["repeat_stopped"] is False
        assert len(ex.statements) == 3, "the row, the flag and its audit: no clear"

    async def test_without_it_the_rule_stands_and_the_answer_is_unchanged(self):
        ex = _ScriptedExecutor(_story(repeat_unit="day"), (1, None), (1, None))
        result = await command_executors.cancel(ex, _command("cancel", intent_id=STORY))
        assert "repeat_stopped" not in result.data
        assert not any(
            sql == command_executors._WRITE_REPEAT for sql, _ in ex.statements
        )

    async def test_stop_repeat_is_true_or_false_before_anything_is_read(self):
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.cancel(
                ex, _command("cancel", intent_id=STORY, stop_repeat="yes")
            )
        assert refused.value.reason == "invalid_args" and ex.statements == []


class TestTheQueueRead:
    async def test_the_origin_the_states_and_the_order_are_bound(self):
        ex = _ScriptedExecutor((1, []))
        await workspaces.list_intents(
            ex,
            workspace_id=WS,
            states=["scheduled", "expired"],
            origin="planned",
            newest_first=True,
            limit=10,
        )
        ((sql, params),) = ex.statements
        assert (
            "AND i.state = ANY(CAST(:states AS text[])) AND i.origin = :origin" in sql
        )
        assert sql.endswith("ORDER BY i.schedule_slot_at DESC, i.id DESC LIMIT :lim")
        assert params == {
            "ws": WS,
            "lim": 10,
            "states": ["scheduled", "expired"],
            "origin": "planned",
        }

    async def test_by_default_every_origin_soonest_first(self):
        ex = _ScriptedExecutor((1, []))
        await workspaces.list_intents(ex, workspace_id=WS)
        ((sql, params),) = ex.statements
        assert ":origin" not in sql
        assert sql.endswith("ORDER BY i.schedule_slot_at ASC, i.id ASC LIMIT :lim")
        assert params == {"ws": WS, "lim": 50}


@pytest.fixture(autouse=True)
def trusted_planning_facts(monkeypatch):
    """Supply DB facts to these scripted SQL/message tests; runtime gates remain unmocked."""
    from src.services.target import source_policy, readers
    from unittest.mock import AsyncMock

    original = readers.row

    async def track(executor, *args, **kwargs):
        row = await original(executor, *args, **kwargs)
        if row and "state" in row and "locks" in row:
            executor.policy_item = row
        return row

    async def verdict(executor, **kwargs):
        item = executor.policy_item
        return [
            dict(
                provider="gdrive",
                base_suitable=item["state"] == "available",
                reason=None if item["state"] == "available" else "unavailable",
                blocking_locks=[
                    k for k in item["locks"] if k in vocabulary.BLOCKING_LOCKS
                ],
                warning_locks=[
                    k for k in item["locks"] if k in vocabulary.WARNING_LOCKS
                ],
            )
        ]

    monkeypatch.setattr(readers, "row", track)
    monkeypatch.setattr(
        source_policy, "suitability_context", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(source_policy, "intent_context", AsyncMock(return_value=None))
    monkeypatch.setattr(source_policy, "suitability", verdict)
    monkeypatch.setattr(
        source_policy,
        "intent_suitability",
        AsyncMock(
            return_value=dict(
                suitable=True,
                base_suitable=True,
                live_intent_conflict=False,
                blocking_locks=[],
                warning_locks=[],
                reason=None,
            )
        ),
    )
