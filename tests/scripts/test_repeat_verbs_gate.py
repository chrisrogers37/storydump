"""A repeating story through the command port, EXECUTED as
`svc_ingress` on the replayed target schema, with the worker's sweeps and the
reaper as the roles that run them.

What each class pins, and why it is here rather than in a unit test:

- **`schedule_item` stores the rule and computes nothing.** The first
  occurrence is the chosen time, on one of the rule's days; the ledger's
  trigger (repeat rules) makes every later one. A rule's time is kept to the minute.
- **The CHECKs judge a rule, named.** A malformed rule is `invalid_args` with
  the `repeat` fact, and nothing is written; only the database can raise the
  constraint the refusal is matched on.
- **`reschedule_item` edits a series while it waits, and clears it while it
  lives.** A moved story's series follows it to the new time of day, and a
  weekly series to the new day: its only day carried along, or one of its
  days, else refused. A rule set without a move repeats at the story's own
  wall time, read by the database in the account's zone, and a weekly rule
  set without days keeps the series' days. A story being cancelled can still
  have its rule cleared.
- **`cancel` with `stop_repeat` ends the series on both of its branches**, and
  on a story already being cancelled; without it the series goes on. The
  flag's ending comes from the reaper, the give-up's from the port.
- **A miss tells the truth about the series**, read from the ledger after the
  miss, and a repeat's occurrence born after its time ends with no notice.
- **The card and the Queue read say the rule**, and a card says when an act
  ended its series.
- **A second workspace sees none of it.**
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.services.target import prompts, vocabulary
from tests.scripts import test_intent_repeat_gate as repeat_gate
from tests.scripts.test_intent_repeat_gate import (
    DAILY,
    TZ,
    _at,
    _one_successor,
    _successors,
    _ws,
)
from tests.scripts.test_planned_serve_gate import (
    LATE,
    _exec,
    _miss,
    _outbox,
    _reap,
    _serve,
    _story,
)
from tests.scripts.test_schedule_verbs_gate import (
    _account,
    _audit,
    _instant,
    _item,
    _read,
    refusal,
    run,
)
from tests.scripts.test_schedule_verbs_gate import _row as _intent

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: The repeat gate's world, re-registered as this module's own: a replayed
#: schema, as its owner and as `svc_ingress`.
world = repeat_gate.world

LIVE = [s for s in vocabulary.INTENT_STATES if s not in vocabulary.TERMINAL_STATES]


def _wednesday() -> date:
    """A Wednesday at least a week ahead in the workspaces' zone."""
    today = datetime.now(ZoneInfo(TZ)).date()
    return today + timedelta(days=(2 - today.weekday()) % 7 + 7)


def _rule_of(row) -> tuple:
    return (
        row["repeat_unit"],
        row["repeat_every"],
        row["repeat_days"],
        row["repeat_at"],
    )


def _schedule(world, w, local_at, **extra):
    """A planned story on a fresh item and account of *w*, through the port."""
    return run(
        world,
        w,
        "schedule_item",
        ig_account_id=_account(world, w),
        media_item_id=_item(world, w),
        local_at=local_at,
        **extra,
    )


def _serve_it(world, intent):
    """`scheduled → prompt_pending → awaiting_approval`, the two edges the
    prompt sweep takes, as the migration actor."""
    for state in ("prompt_pending", "awaiting_approval"):
        _exec(
            world, "UPDATE post_intents SET state = %s WHERE id = %s", (state, intent)
        )


# --- schedule_item ------------------------------------------------------------------


class TestScheduleARepeat:
    def test_the_first_occurrence_is_the_chosen_time_and_skip_makes_the_next(
        self, world
    ):
        """A Thursday chosen for a rule of Mondays and Thursdays: the story is
        the Thursday, and its skip makes the Monday at the same wall time."""
        w = _ws(world, "weekly")
        thursday = _wednesday() + timedelta(days=1)
        local_at = f"{thursday} 16:07"
        result = _schedule(world, w, local_at, repeat={"unit": "week", "days": [4, 1]})
        intent = result.data["intent_id"]
        assert _at(result.data) == _instant(local_at, TZ)
        assert result.data["repeat"] == {
            "unit": "week",
            "every": 1,
            "days": [1, 4],
            "at": "16:07",
        }
        assert _rule_of(_intent(world, intent)) == ("week", 1, [1, 4], "16:07:00")
        (scheduled,) = _audit(world, intent, "scheduled")
        assert scheduled[5]["repeat"] == result.data["repeat"]

        _serve_it(world, intent)
        run(world, w, "skip", intent_id=intent)
        successor = _one_successor(world, _seeded(world, intent))
        monday = thursday + timedelta(days=4)
        assert _at(successor) == _instant(f"{monday} 16:07", TZ)
        assert _rule_of(successor) == ("week", 1, [1, 4], "16:07:00")

    def test_named_days_must_include_the_chosen_one(self, world):
        """A Wednesday for a rule of Mondays and Thursdays would post on the
        Wednesday and on the Thursday: refused, and nothing is written."""
        w = _ws(world, "off-day")
        item = _item(world, w)
        refused = refusal(
            world,
            w,
            "schedule_item",
            ig_account_id=_account(world, w),
            media_item_id=item,
            local_at=f"{_wednesday()} 16:07",
            repeat={"unit": "week", "days": [1, 4]},
        )
        assert (refused.reason, refused.facts) == (
            "invalid_args",
            {"repeat": "off_day"},
        )
        assert (
            _exec(
                world,
                "SELECT count(*) FROM post_intents WHERE media_item_id = %s",
                (item,),
                fetch=True,
            )[0][0]
            == 0
        )

    def test_a_rules_time_is_to_the_minute_as_every_surface_shows_it(self, world):
        """The story keeps the seconds it was given; its series, set and moved,
        repeats at the minute."""
        w = _ws(world, "minute")
        wednesday = _wednesday()
        result = _schedule(world, w, f"{wednesday} 16:07:30", repeat={"unit": "day"})
        intent = result.data["intent_id"]
        assert _at(result.data) == _instant(f"{wednesday} 16:07:30", TZ)
        assert _rule_of(_intent(world, intent)) == ("day", 1, None, "16:07:00")
        run(
            world,
            w,
            "reschedule_item",
            intent_id=intent,
            local_at=f"{wednesday} 11:30:45",
        )
        assert _rule_of(_intent(world, intent)) == ("day", 1, None, "11:30:00")

    def test_a_weekly_rule_defaults_to_the_chosen_day(self, world):
        w = _ws(world, "default-day")
        result = _schedule(world, w, f"{_wednesday()} 09:30", repeat={"unit": "week"})
        assert result.data["repeat"]["days"] == [3]

    @pytest.mark.parametrize(
        "rule, constraint",
        [
            ({"unit": "day", "every": 0}, "ck_intent_repeat"),
            ({"unit": "day", "days": [1]}, "ck_intent_repeat"),
            ({"unit": "week", "every": 53}, "ck_intent_repeat"),
            ({"unit": "week", "days": [8]}, "ck_intent_repeat"),
            ({"unit": "month"}, "ck_intent_repeat_unit"),
        ],
        ids=lambda v: str(v),
    )
    def test_a_malformed_rule_is_the_checks_to_refuse_and_nothing_is_written(
        self, world, rule, constraint
    ):
        w = _ws(world, "malformed")
        item, account = _item(world, w), _account(world, w)
        refused = refusal(
            world,
            w,
            "schedule_item",
            ig_account_id=account,
            media_item_id=item,
            local_at=f"{_wednesday()} 10:00",
            repeat=rule,
        )
        assert refused.reason == "invalid_args"
        assert refused.facts == {"repeat": "invalid"}
        assert f"({constraint})" in str(refused)
        assert (
            _exec(
                world,
                "SELECT count(*) FROM post_intents WHERE media_item_id = %s",
                (item,),
                fetch=True,
            )[0][0]
            == 0
        )


def _seeded(world, intent) -> dict:
    """A port-made story in the shape the repeat gate's helpers read."""
    row = _intent(world, intent)
    return {
        "intent": intent,
        "media": row["media_item_id"],
        "acct": row["ig_account_id"],
    }


# --- reschedule_item ------------------------------------------------------------------


class TestRescheduleARepeat:
    def test_a_moved_series_follows_its_story_to_the_new_time_of_day(self, world):
        w = _ws(world, "move")
        wednesday = _wednesday()
        intent = _schedule(world, w, f"{wednesday} 09:00", repeat={"unit": "day"}).data[
            "intent_id"
        ]
        moved = run(
            world, w, "reschedule_item", intent_id=intent, local_at=f"{wednesday} 11:30"
        )
        row = _intent(world, intent)
        assert _at(row) == _instant(f"{wednesday} 11:30", TZ)
        assert _rule_of(row) == ("day", 1, None, "11:30:00")
        assert moved.data["repeat"]["at"] == "11:30"
        (audited,) = _audit(world, intent, "rescheduled")
        assert audited[5]["repeat_at"] == "11:30"

    def test_a_weekly_series_of_one_day_moves_its_day_with_its_story(self, world):
        """A Wednesday series moved to the Thursday repeats on Thursdays: its
        skip makes the next Thursday, and no Wednesday."""
        w = _ws(world, "move-day")
        wednesday = _wednesday()
        thursday = wednesday + timedelta(days=1)
        intent = _schedule(
            world, w, f"{wednesday} 09:00", repeat={"unit": "week"}
        ).data["intent_id"]
        moved = run(
            world, w, "reschedule_item", intent_id=intent, local_at=f"{thursday} 09:00"
        )
        assert moved.data["repeat"]["days"] == [4]
        assert _rule_of(_intent(world, intent)) == ("week", 1, [4], "09:00:00")
        (audited,) = _audit(world, intent, "rescheduled")
        assert audited[5]["repeat_days"] == [4]
        _serve_it(world, intent)
        run(world, w, "skip", intent_id=intent)
        successor = _one_successor(world, _seeded(world, intent))
        assert _at(successor) == _instant(f"{thursday + timedelta(days=7)} 09:00", TZ)

    def test_a_weekly_series_of_several_days_moves_only_to_one_of_them(self, world):
        w = _ws(world, "move-days")
        thursday = _wednesday() + timedelta(days=1)
        intent = _schedule(
            world, w, f"{thursday} 09:00", repeat={"unit": "week", "days": [1, 4]}
        ).data["intent_id"]
        refused = refusal(
            world,
            w,
            "reschedule_item",
            intent_id=intent,
            local_at=f"{thursday - timedelta(days=1)} 09:00",
        )
        assert (refused.reason, refused.facts) == (
            "invalid_args",
            {"repeat": "off_day"},
        )
        assert _at(_intent(world, intent)) == _instant(f"{thursday} 09:00", TZ)
        monday = thursday + timedelta(days=4)
        run(world, w, "reschedule_item", intent_id=intent, local_at=f"{monday} 10:00")
        assert _rule_of(_intent(world, intent)) == ("week", 1, [1, 4], "10:00:00")

    def test_a_weekly_rule_without_days_keeps_the_series_days(self, world):
        """`--repeat week --every 2` with no `--on` changes the interval only."""
        w = _ws(world, "keep-days")
        thursday = _wednesday() + timedelta(days=1)
        intent = _schedule(
            world, w, f"{thursday} 09:00", repeat={"unit": "week", "days": [1, 4]}
        ).data["intent_id"]
        run(
            world,
            w,
            "reschedule_item",
            intent_id=intent,
            repeat={"unit": "week", "every": 2},
        )
        assert _rule_of(_intent(world, intent)) == ("week", 2, [1, 4], "09:00:00")

    def test_a_rule_alone_repeats_at_the_storys_own_wall_time(self, world):
        """No new time: the database reads the story's instant in its zone, so
        the rule's time and weekday are the ones the person chose."""
        w = _ws(world, "set")
        wednesday = _wednesday()
        intent = _schedule(world, w, f"{wednesday} 16:07").data["intent_id"]
        before = _intent(world, intent)["schedule_slot_at"]
        result = run(
            world, w, "reschedule_item", intent_id=intent, repeat={"unit": "week"}
        )
        row = _intent(world, intent)
        assert _rule_of(row) == ("week", 1, [3], "16:07:00")
        assert row["schedule_slot_at"] == before, "the story stays where it is"
        assert "previous_slot_at" not in result.data
        (audited,) = _audit(world, intent, "repeat_set")
        assert audited[5]["repeat"] == result.data["repeat"]

    @pytest.mark.parametrize(
        "args",
        [
            {"repeat": {"unit": "day", "every": 2}},
            {"local_at": f"{_wednesday()} 10:00"},
        ],
        ids=["a new rule", "a new time"],
    )
    def test_once_sent_a_story_takes_no_new_rule_and_no_new_time(self, world, args):
        w = _ws(world, "sent")
        story = _story(world, w, state="awaiting_approval", rule=DAILY)
        refused = refusal(
            world, w, "reschedule_item", intent_id=story["intent"], **args
        )
        assert refused.reason == "illegal_transition"
        assert _rule_of(_intent(world, story["intent"]))[0] == "day"

    @pytest.mark.parametrize("state", LIVE)
    def test_a_clear_takes_any_live_story_where_it_stands(self, world, state):
        w = _ws(world, "clear")
        ambiguous = state == "publishing_ambiguous"
        story = _story(
            world,
            w,
            state="publishing" if ambiguous else state,
            rule=DAILY,
            slot_in_s=3600,
            cap=state.startswith("publishing"),
        )
        if ambiguous:
            # `ck_ambiguous_called`: only a publish call on record is ambiguous
            _exec(
                world,
                "UPDATE post_intents SET state = 'publishing_ambiguous',"
                " publish_step = 'publish_called' WHERE id = %s",
                (story["intent"],),
            )
        result = run(
            world, w, "reschedule_item", intent_id=story["intent"], repeat=None
        )
        row = _intent(world, story["intent"])
        assert _rule_of(row) == (None, None, None, None)
        assert (row["state"], result.data["repeat"]) == (state, None)
        assert result.data["repeat_stopped"] is True
        assert len(_audit(world, story["intent"], "repeat_cleared")) == 1

    def test_a_clear_takes_a_story_being_cancelled(self, world):
        """Clearing a rule is no lever on the story: the cancel lands at the
        reaper, and makes no next occurrence."""
        w = _ws(world, "clear-cancelling")
        story = _story(world, w, rule=DAILY, slot_in_s=86400)
        run(world, w, "cancel", intent_id=story["intent"])
        result = run(
            world, w, "reschedule_item", intent_id=story["intent"], repeat=None
        )
        assert result.data["repeat_stopped"] is True
        _reap(world)
        assert _intent(world, story["intent"])["state"] == "cancelled"
        assert _successors(world, story) == []

    @pytest.mark.parametrize(
        "over, args",
        [
            ({"origin": "cadence"}, {"repeat": {"unit": "day"}}),
            ({"state": "skipped", "slot_in_s": -60}, {"repeat": None}),
        ],
        ids=["a rule on a cadence story", "a clear on an ended story"],
    )
    def test_where_a_rule_cannot_change_it_is_refused(self, world, over, args):
        w = _ws(world, "refused")
        story = _story(world, w, **over)
        refused = refusal(
            world, w, "reschedule_item", intent_id=story["intent"], **args
        )
        assert refused.reason == "illegal_transition"


# --- cancel ---------------------------------------------------------------------------


class TestCancelAndTheSeries:
    @pytest.mark.parametrize("stop", [True, False], ids=["stop_repeat", "plain"])
    def test_the_flags_ending_makes_a_successor_only_without_stop_repeat(
        self, world, stop
    ):
        w = _ws(world, "flag")
        story = _story(world, w, rule=DAILY, slot_in_s=86400)
        args = {"stop_repeat": True} if stop else {}
        result = run(world, w, "cancel", intent_id=story["intent"], **args)
        assert result.data.get("repeat_stopped") is (True if stop else None)
        _reap(world)
        assert _intent(world, story["intent"])["state"] == "cancelled"
        assert len(_successors(world, story)) == (0 if stop else 1)
        cleared = _audit(world, story["intent"], "repeat_cleared")
        assert [r[5].get("stop_repeat") for r in cleared] == ([True] if stop else [])

    def test_a_story_being_cancelled_still_stops_its_series(self, world):
        """A plain cancel, then `cancel --stop-repeat` before the reaper lands
        it: the first cancel stands, and its ending makes no successor."""
        w = _ws(world, "flag-then-stop")
        story = _story(world, w, rule=DAILY, slot_in_s=86400)
        run(world, w, "cancel", intent_id=story["intent"])
        result = run(world, w, "cancel", intent_id=story["intent"], stop_repeat=True)
        assert result.data["repeat_stopped"] is True
        assert result.data["cancel_requested"] is True
        _reap(world)
        assert _intent(world, story["intent"])["state"] == "cancelled"
        assert _successors(world, story) == []

    def test_a_stop_while_the_cancel_lands_says_so_on_the_sent_card(self, world):
        """A plain cancel of a sent card, then a stop before the reaper lands
        it: the line the first cancel wrote is restated with the series
        stopped."""
        w = _ws(world, "card-flag-then-stop")
        story = _story(world, w, rule=DAILY, slot_in_s=-5)
        _serve(world)
        _exec(
            world,
            "UPDATE channel_outbox SET state = 'sent', external_message_ref = %s"
            " WHERE intent_id = %s AND kind = 'approval_prompt'",
            ("4242", story["intent"]),
        )
        run(world, w, "cancel", intent_id=story["intent"])
        run(world, w, "cancel", intent_id=story["intent"], stop_repeat=True)
        edits = [
            p["outcome_text"]
            for _, p in _outbox(world, story["intent"], "prompt_supersede")
        ]
        assert len(edits) == 2, edits
        assert "series" not in edits[0], edits
        assert edits[1].endswith(" · the series stopped"), edits
        ((_, card),) = _outbox(world, story["intent"], "approval_prompt")
        assert card["outcome_text"] == edits[1]

    @pytest.mark.parametrize("stop", [True, False], ids=["stop_repeat", "plain"])
    def test_the_give_up_ends_the_series_only_with_stop_repeat(self, world, stop):
        """A parked story's cancel is the give-up, ended by the port itself:
        the rule must be gone before that ending, in the same transaction."""
        w = _ws(world, "give-up")
        story = _story(world, w, state="review_required", rule=DAILY, slot_in_s=-60)
        args = {"stop_repeat": True} if stop else {}
        result = run(world, w, "cancel", intent_id=story["intent"], **args)
        assert result.data["state"] == "cancelled"
        assert len(_successors(world, story)) == (0 if stop else 1)


# --- the miss sweep -------------------------------------------------------------------


def _waited(world, story) -> dict:
    """*story* as one planned a day before its time, which every story but a
    late-born repeat is: the seeder writes it now, after a past time."""
    _exec(
        world,
        "UPDATE post_intents SET created_at = schedule_slot_at - interval '1 day'"
        " WHERE id = %s",
        (story["intent"],),
    )
    return story


def _notice(world, intent):
    notices = _outbox(world, intent, "notification")
    return [payload["text"] for _, payload in notices]


class TestAMissAndTheSeries:
    @pytest.mark.parametrize(
        "over, goes_on",
        [
            ({"slot_in_s": -(LATE + 60)}, True),
            ({"lock": ("hold", None)}, False),
        ],
        ids=[
            "late_miss_says_the_series_goes_on",
            "a_miss_the_item_caused_says_the_series_stopped",
        ],
    )
    def test_a_miss_says_what_became_of_its_series(self, world, over, goes_on):
        w = _ws(world, "miss")
        story = _waited(world, _story(world, w, rule=DAILY, **over))
        _miss(world)
        assert _intent(world, story["intent"])["state"] == "expired"
        (said,) = _notice(world, story["intent"])
        assert said.endswith(f"Nothing was posted. {prompts.SERIES_WORDS[goes_on]}"), (
            said
        )
        assert len(_successors(world, story)) == (1 if goes_on else 0)

    def test_a_late_miss_whose_next_occurrence_failed_says_the_series_stopped(
        self, world
    ):
        """The sentence is the ledger's outcome, never the reason's promise: a
        late miss continues a series, but here the successor's INSERT is
        refused (`repeat_failed`), and the notice says the series stopped."""
        w = _ws(world, "failed")
        story = _waited(world, _story(world, w, rule=DAILY, slot_in_s=-(LATE + 60)))
        _exec(world, "REVOKE INSERT ON post_intents FROM svc_worker")
        try:
            _miss(world)
        finally:
            _exec(world, "GRANT INSERT ON post_intents TO svc_worker")
        assert _intent(world, story["intent"])["last_error"]["message"] == "late"
        assert len(_audit(world, story["intent"], "repeat_failed")) == 1
        (said,) = _notice(world, story["intent"])
        assert said.endswith(f"Nothing was posted. {prompts.SERIES_WORDS[False]}"), said
        assert _successors(world, story) == []

    def test_a_repeat_born_after_its_time_whose_series_goes_on_is_unannounced(
        self, world
    ):
        """A daily story served three days ago and skipped only now: its next
        occurrence is born two days late, while its card was still out. The
        miss sweep ends it with its reason and says nothing, and the one after
        it follows now (the repeat trigger resumes a late miss from now)."""
        w = _ws(world, "born-late")
        story = _story(
            world, w, state="awaiting_approval", rule=DAILY, slot_in_s=-(3 * 86400)
        )
        run(world, w, "skip", intent_id=story["intent"])
        late = _one_successor(world, story)
        now = datetime.now(timezone.utc)
        assert _at(late) < now - timedelta(days=1), "born after its time"
        _miss(world)
        assert _intent(world, late["id"])["state"] == "expired"
        assert _intent(world, late["id"])["last_error"]["message"] == "late"
        assert _notice(world, late["id"]) == [], "nobody was waiting for it"
        (after,) = [s for s in _successors(world, story) if s["id"] != late["id"]]
        assert now < _at(after) <= now + timedelta(days=1)

    def test_a_repeat_born_after_its_time_whose_series_stops_says_so(self, world):
        """Born late, nobody waited for this occurrence; but the series' end is
        news to its person, so it is said."""
        w = _ws(world, "born-late-stop")
        story = _story(
            world,
            w,
            state="awaiting_approval",
            rule=DAILY,
            slot_in_s=-(3 * 86400),
            lock=("hold", None),
        )
        run(world, w, "skip", intent_id=story["intent"])
        late = _one_successor(world, story)
        _miss(world)
        assert _intent(world, late["id"])["last_error"]["message"] == "item_locked"
        (said,) = _notice(world, late["id"])
        assert said.endswith(f"Nothing was posted. {prompts.SERIES_WORDS[False]}"), said
        assert [s["id"] for s in _successors(world, story)] == [late["id"]], (
            "the series has ended"
        )


# --- the card and the read --------------------------------------------------------------


class TestTheCardAndTheReadSayTheRule:
    def test_a_served_repeating_story_says_its_rule_on_its_card(self, world):
        w = _ws(world, "card")
        story = _story(world, w, rule=("week", 2, [1, 4]), slot_in_s=-5)
        _serve(world)
        ((_, card),) = _outbox(world, story["intent"], "approval_prompt")
        clock = _at(_intent(world, story["intent"])).astimezone(ZoneInfo(TZ))
        clock = clock.strftime("%H:%M")
        assert f" · repeats every 2 weeks on Mon and Thu at {clock}" in card["text"]

    @pytest.mark.parametrize(
        "verb, args, tail",
        [
            ("cancel", {"stop_repeat": True}, " · the series stopped"),
            ("reject", {}, " · the series ended"),
            ("cancel", {}, None),
        ],
        ids=["stop_repeat", "reject", "plain-cancel"],
    )
    def test_a_served_card_says_when_its_series_ended(self, world, verb, args, tail):
        """The card's header keeps its "repeats every …" words, so the line
        written under it says when the act ended the series. A plain
        cancel ends this story alone, and its series goes on."""
        w = _ws(world, f"ended-{verb}")
        story = _story(world, w, rule=DAILY, slot_in_s=-5)
        _serve(world)
        run(world, w, verb, intent_id=story["intent"], **args)
        ((_, card),) = _outbox(world, story["intent"], "approval_prompt")
        line = card["outcome_text"]
        if tail:
            assert line.endswith(tail), line
        else:
            assert "series" not in line, line

    def test_the_queue_read_carries_the_rule_as_one_value(self, world):
        w = _ws(world, "read")
        repeating = _story(world, w, rule=("day", 3, None), slot_in_s=86400)
        once = _story(world, w, slot_in_s=86400)
        rows = {str(r["id"]): r for r in _read(world, w, origin="planned")}
        at = _at(_intent(world, repeating["intent"]))
        assert rows[repeating["intent"]]["repeat"] == {
            "unit": "day",
            "every": 3,
            "days": None,
            "at": at.astimezone(ZoneInfo(TZ)).strftime("%H:%M"),
        }
        assert rows[once["intent"]]["repeat"] is None


# --- isolation ---------------------------------------------------------------------------


class TestASecondWorkspaceSeesNoneOfIt:
    def test_another_workspaces_series_is_not_its_to_read_or_change(self, world):
        w, other = _ws(world, "mine"), _ws(world, "theirs")
        story = _story(world, w, state="awaiting_approval", rule=DAILY, slot_in_s=-60)
        run(world, w, "skip", intent_id=story["intent"])
        successor = _one_successor(world, story)
        mine = {str(r["id"]) for r in _read(world, w, origin="planned", states=())}
        assert {story["intent"], successor["id"]} <= mine, "a positive control"
        theirs = {
            str(r["id"]) for r in _read(world, other, origin="planned", states=())
        }
        assert not theirs & mine
        refused = refusal(
            world, other, "reschedule_item", intent_id=successor["id"], repeat=None
        )
        assert refused.reason == "not_found"
        assert _rule_of(_intent(world, successor["id"]))[0] == "day"
