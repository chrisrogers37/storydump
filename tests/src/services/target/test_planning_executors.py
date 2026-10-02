"""A planned story made and moved through the command port (#1413 phase 5),
at the SQL seam: the statements `schedule_item`, `reschedule_item` and
`cancel` send, with their parameters; the refusals decided in Python before
the database is asked (an id, the time's shape, the override's type, the lock
and item rule); and the Queue read's filter and order.

What only PostgreSQL can say (the zone's reading, the live-subject key, the
tenant's policies) is the gate's: `tests/scripts/test_schedule_verbs_gate.py`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.services.target.commands import Command, CommandRefused  # noqa: I001 — the port first: the registry cycle
from src.services.target import command_executors, intent_ledger, vocabulary, workspaces
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


def _born():
    return (1, {"id": STORY, "workspace_id": WS})


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
        assert "'scheduled', 'planned', :by)" in values
        assert f"WHERE {intent_ledger.NOT_TERMINAL} DO NOTHING" in insert[0]
        assert insert[1] == {
            "ws": WS,
            "acct": ACCOUNT,
            "media": ITEM,
            "ref": "ref-1",
            "at": AT,
            "by": USER,
        }
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
        """F7: an item that cannot post, or a lock that would miss it at its
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
        ex = _ScriptedExecutor(_story(), _instant(later), (1, None), (1, None))
        result = await command_executors.reschedule_item(ex, _reschedule())
        assert result.data == {
            "intent_id": STORY,
            "state": "scheduled",
            "schedule_slot_at": later.isoformat(),
            "previous_slot_at": AT.isoformat(),
            "tz": "Europe/London",
            "local_at": "2026-10-13 09:00:00",
        }
        row, instant, update, audit = ex.statements
        assert "FOR UPDATE OF i" in row[0]
        assert (row[1]["id"], row[1]["ws"]) == (STORY, WS)
        assert instant[1]["tz"] == "Europe/London", "the story's own zone"
        assert "WHERE id = :id AND workspace_id = :ws" in update[0]
        assert update[1] == {"at": later, "id": STORY, "ws": WS}
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
        ex = _ScriptedExecutor(_story(), _instant(), (1, None), (1, None))
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

        async def _record_outcome(session, intent, command, state):
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
