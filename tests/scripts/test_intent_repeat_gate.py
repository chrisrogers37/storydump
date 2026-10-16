"""A planned story repeats according to its repeat rule.

`post_intents` carries a four-column repeat rule. `fn_repeat_times` gives the rule's grid, reading
local times through `fn_wall_instant`, and `tg_intent_repeat` makes the next occurrence in the
transaction that ends one. Each ending is driven against the code-built schema, under the
role that causes it in production:

- the port, as `svc_ingress`: skip, reject, posted by a person, and the cancel a person asks for;
- the worker, as `svc_worker`: the miss sweep with each reason, a publish ending, the offboarding
  drain, and stale-approved parking;
- the reaper, as `svc_maintenance` inside `fn_reaper_sweep`, called by `svc_worker` as
  production calls it: the cancel leg and the approval-timeout leg.

Then a series' one post a local day, after a card answered late and after its account moved
west; the grid itself, table-driven and across both clock changes in two zones; the CHECKs; the
trigger's totality, where a failure records `repeat_failed`, warns, and the sweep completes; a
successor that starts clean; and the pin that holds the trigger's terminal list to the
vocabulary. The live-account list is pinned with the doors that share it, in
`test_schedule_verbs_gate.py`.

Not simulated: a second live story for the same item and account when an occurrence ends.
`uq_intent_live_subject` holds the ending occurrence as the item's only live story until the
transaction that inserts its successor, so no other transaction can hold the key at that moment.

The world is module-scoped, with one replay. Each test seeds its own workspace and asserts on its
own rows, because the sweeps are estate-wide by design.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import pytest
from sqlalchemy import text

from src.services.target import offboarding, provisioning, vocabulary
from src.utils.datetime_utils import parse_iso_timestamp
from tests.scripts.conftest import (
    _scratch,
    as_user,
    in_tenant,
    build_world,
    txn,
)
from tests.scripts.test_planned_serve_gate import (
    LATE,
    _as_worker,
    _exec,
    _miss,
    _reap,
    _row,
    _story,
    _worker,
    _workspace,
)
from tests.scripts.test_schedule_verbs_gate import _audit, run

pytestmark = [pytest.mark.integration, pytest.mark.slow]

TZ = "America/New_York"  # every `_workspace` is in this zone, and its accounts carry none
DAILY = ("day", 1, None)
NAMED = {  # the columns tg_intent_repeat's INSERT names; every other one takes its default
    "workspace_id",
    "ig_account_id",
    "media_item_id",
    "provider_account_ref",
    "approval_mode",
    "schedule_slot_at",
    "state",
    "origin",
    "scheduled_by_user_id",
    "repeat_unit",
    "repeat_every",
    "repeat_days",
    "repeat_at",
}
CLOCKED = {
    "id",
    "created_at",
    "updated_at",
    "entered_state_at",
}  # defaults that read the clock


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = build_world(db, owner_actor, admin_conn)
        yield {"owner": dsn, "ingress": as_user(dsn, "svc_ingress")}
    finally:
        gen.close()


def _ws(world, name, **kw):
    return _workspace(world, f"{name}-{uuid.uuid4().hex[:6]}", **kw)


def _seed(world, w, *, rule=DAILY, **kw):
    """A planned story carrying *rule* (daily by default), its time of day its own slot's."""
    return _story(world, w, rule=rule, **kw)


def _successors(world, s):
    """Every other story on the seeded item and account: the series' later occurrences."""
    rows = _exec(
        world,
        "SELECT to_jsonb(i) FROM post_intents i"
        " WHERE i.media_item_id = %s AND i.ig_account_id = %s AND i.id <> %s"
        " ORDER BY i.created_at",
        (s["media"], s["acct"], s["intent"]),
        fetch=True,
    )
    return [r[0] for r in rows]


def _one_successor(world, s):
    found = _successors(world, s)
    assert len(found) == 1, f"expected exactly one successor, found {len(found)}"
    return found[0]


def _at(row) -> datetime:
    return parse_iso_timestamp(row["schedule_slot_at"])


def _local(instant: datetime):
    here = instant.astimezone(ZoneInfo(TZ))
    return here.date(), here.time()


def _as_worker_update(world, ws, sql, params):
    """One UPDATE as the publish pipeline opens its flip: `svc_worker`, the system actor,
    under *ws*'s tenant."""
    return _worker(world, lambda session: session.execute(text(sql), params), tenant=ws)


def _times(world, unit, every, days, at, anchor, tz, lo, hi):
    """`fn_repeat_times`, called as `svc_worker`: a plain function runs for the role that ends a
    story, with no grant beyond PUBLIC."""
    rows = _as_worker(
        world,
        "SELECT t FROM fn_repeat_times(%s, %s, %s::smallint[], %s::time,"
        " %s::timestamptz, %s, %s::timestamptz, %s::timestamptz) AS t",
        (unit, every, days, at, anchor, tz, lo, hi),
    )
    return [r[0] for r in rows]


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# --- the port, as svc_ingress -----------------------------------------------------------


class TestThePortEndsAnOccurrence:
    def test_skip_makes_the_next_occurrence_a_day_later_at_the_same_local_time(
        self, world
    ):
        w = _ws(world, "skip")
        s = _seed(world, w, state="awaiting_approval", slot_in_s=-600)
        run(world, w, "skip", intent_id=s["intent"])
        assert _row(world, s["intent"])["state"] == "skipped"
        nxt = _one_successor(world, s)
        assert nxt["state"] == "scheduled" and nxt["origin"] == "planned"
        assert (
            nxt["approval_mode"] == "manual"
            and nxt["scheduled_by_user_id"] == w["user"]
        )
        assert (nxt["repeat_unit"], nxt["repeat_every"]) == ("day", 1)
        day, clock = _local(_at(nxt))
        slot_day, slot_clock = _local(s["slot"])
        assert day == slot_day + timedelta(days=1) and clock == slot_clock
        ((actor, *_, detail),) = _audit(world, nxt["id"], "repeated")
        assert actor == "user" and detail["follows"] == s["intent"] and detail["v"] == 1

    def test_a_weekly_series_comes_back_a_week_later(self, world):
        w = _ws(world, "weekly")
        weekday = (datetime.now(timezone.utc) - timedelta(seconds=600)).astimezone(
            ZoneInfo(TZ)
        )
        s = _seed(
            world,
            w,
            state="awaiting_approval",
            slot_in_s=-600,
            rule=("week", 1, [weekday.isoweekday()]),
        )
        run(world, w, "skip", intent_id=s["intent"])
        day, clock = _local(_at(_one_successor(world, s)))
        slot_day, slot_clock = _local(s["slot"])
        assert day == slot_day + timedelta(days=7) and clock == slot_clock

    def test_posted_by_a_person_continues_the_series(self, world):
        w = _ws(world, "posted")
        s = _seed(world, w, state="awaiting_approval", slot_in_s=-600)
        run(world, w, "mark_posted", intent_id=s["intent"])
        assert _row(world, s["intent"])["state"] == "posted"
        assert _one_successor(world, s)["state"] == "scheduled"

    def test_reject_ends_the_series(self, world):
        w = _ws(world, "reject")
        s = _seed(world, w, state="awaiting_approval", slot_in_s=-600)
        run(world, w, "reject", intent_id=s["intent"])
        assert _row(world, s["intent"])["state"] == "rejected"
        assert _successors(world, s) == []

    def test_a_person_s_cancel_skips_this_one_when_the_reaper_ends_it(self, world):
        w = _ws(world, "cancel")
        s = _seed(world, w, state="scheduled", slot_in_s=2 * 86400)
        run(world, w, "cancel", intent_id=s["intent"])
        assert _successors(world, s) == [], "the flag alone ends nothing"
        _reap(world)
        assert _row(world, s["intent"])["state"] == "cancelled"
        nxt = _one_successor(world, s)
        assert _local(_at(nxt))[0] == _local(s["slot"])[0] + timedelta(days=1)
        ((actor, *_),) = _audit(world, nxt["id"], "repeated")
        assert actor == "reaper"


# --- the worker, as svc_worker ------------------------------------------------------------


class TestTheWorkerEndsAnOccurrence:
    @pytest.mark.parametrize("ending", ["posted", "failed"])
    def test_a_publish_ending_continues_the_series(self, world, ending):
        w = _ws(world, f"publish-{ending}")
        s = _seed(world, w, state="publishing", slot_in_s=-600, cap=True)
        extra = ", published_via = 'manual'" if ending == "posted" else ""
        _as_worker_update(
            world,
            w["ws"],
            f"UPDATE post_intents SET state = :state{extra} WHERE id = CAST(:id AS uuid)",
            {"state": ending, "id": s["intent"]},
        )
        assert _row(world, s["intent"])["state"] == ending
        ((actor, *_),) = _audit(world, _one_successor(world, s)["id"], "repeated")
        assert actor == "system"

    @pytest.mark.parametrize(
        "reason, seed",
        [
            ("item_removed", {"media_state": "removed"}),
            ("item_unsupported", {"media_state": "unsupported"}),
            ("item_missing", {"media_state": "missing"}),
            ("item_locked", {"lock": ("reject", None)}),
            ("account_removed", {"account_state": "disabled"}),
        ],
    )
    def test_a_miss_the_item_or_account_caused_ends_the_series(
        self, world, reason, seed
    ):
        w = _ws(world, f"miss-{reason}")
        s = _seed(world, w, state="scheduled", slot_in_s=-5, **seed)
        _miss(world)
        row = _row(world, s["intent"])
        assert row["state"] == "expired" and row["last_error"]["message"] == reason
        assert _successors(world, s) == []

    def test_a_late_miss_resumes_after_now_rather_than_cascading(self, world):
        w = _ws(world, "late")
        s = _seed(world, w, state="scheduled", slot_in_s=-(3 * 86400 + 2 * 3600))
        _miss(world)
        row = _row(world, s["intent"])
        assert row["state"] == "expired" and row["last_error"]["message"] == "late"
        nxt = _at(_one_successor(world, s))
        assert nxt > datetime.now(timezone.utc), (
            "from the ended time it would be days past"
        )
        assert nxt - s["slot"] >= timedelta(days=3)

    def test_a_paused_miss_continues_the_series(self, world):
        w = _ws(world, "paused", paused=True)
        s = _seed(world, w, state="scheduled", slot_in_s=-(LATE + 600))
        _miss(world)
        assert _row(world, s["intent"])["last_error"]["message"] == "paused"
        assert len(_successors(world, s)) == 1

    def test_the_offboarding_drain_makes_no_successor_and_finalizing_proceeds(
        self, world
    ):
        w = _ws(world, "offboard")
        s = _seed(world, w, state="scheduled", slot_in_s=86400)
        _exec(
            world,
            "UPDATE workspaces SET state = 'offboarding', offboarding_at = now() - interval '1 day'"
            " WHERE id = %s",
            (w["ws"],),
        )
        _worker(
            world,
            lambda session: offboarding.drain(session, w["ws"], limit=50),
            tenant=w["ws"],
        )
        assert _row(world, s["intent"])["state"] == "cancelled"
        assert _successors(world, s) == []
        _worker(
            world,
            lambda session: session.execute(
                text("SELECT fn_offboard_finalize(CAST(:ws AS uuid), interval '0')"),
                {"ws": w["ws"]},
            ),
        )
        assert (
            _exec(
                world, "SELECT 1 FROM workspaces WHERE id = %s", (w["ws"],), fetch=True
            )
            == []
        )

    def test_removing_the_account_stops_the_series(self, world):
        w = _ws(world, "remove")
        s = _seed(world, w, state="scheduled", slot_in_s=86400)

        async def remove(session):
            return await provisioning.disable_destination(
                session, workspace_id=w["ws"], ig_account_id=s["acct"]
            )

        asyncio.run(in_tenant(world["ingress"], w["ws"], w["user"], remove))
        _reap(world)
        assert _row(world, s["intent"])["state"] == "cancelled"
        assert _successors(world, s) == []


# --- the reaper, as svc_maintenance inside fn_reaper_sweep ---------------------------------


class TestTheReaperEndsAnOccurrence:
    def test_an_unanswered_card_s_timeout_still_gets_the_next_day(self, world):
        w = _ws(world, "timeout")
        # The card was served a day and ten seconds ago and is past the 24 h approval TTL,
        # so the rule's next time passed ten seconds ago. It is still the successor's time:
        # the serve sweep's late window decides from there.
        s = _seed(
            world,
            w,
            state="awaiting_approval",
            slot_in_s=-(86400 + 10),
            entered_ago_s=25 * 3600,
        )
        _reap(world)
        assert _row(world, s["intent"])["state"] == "expired"
        nxt = _one_successor(world, s)
        day, clock = _local(_at(nxt))
        slot_day, slot_clock = _local(s["slot"])
        assert day == slot_day + timedelta(days=1) and clock == slot_clock
        ((actor, *_),) = _audit(world, nxt["id"], "repeated")
        assert actor == "reaper"

    def test_a_stale_approved_story_parked_for_review_makes_no_successor_yet(
        self, world
    ):
        # Parking (approved → review_required) is not an ending: the series waits for the
        # person who resolves the review, whose ending then makes the successor.
        w = _ws(world, "parked")
        s = _seed(
            world, w, state="approved", slot_in_s=-(4 * 86400), entered_ago_s=4 * 86400
        )
        _reap(world)
        assert _row(world, s["intent"])["state"] == "review_required"
        assert _successors(world, s) == []

    def test_one_ending_makes_exactly_one_successor(self, world):
        w = _ws(world, "once")
        s = _seed(world, w, state="scheduled", slot_in_s=86400, flagged=True)
        before = len(_successors(world, s))
        _reap(world)
        _reap(world)
        assert before == 0 and len(_successors(world, s)) == 1


# --- once a local day ---------------------------------------------------------------------


def _zone_reading(hour: int) -> str:
    """A fixed-offset zone whose clock reads *hour* o'clock now, give or take the minutes, so a
    test that places a time later today has a day that cannot end under it, whenever it runs.
    Etc/GMT names count west as positive."""
    offset = (hour - datetime.now(timezone.utc).hour + 12) % 24 - 12
    return f"Etc/GMT{-offset:+d}" if offset else "Etc/GMT"


def _in_zone(world, s, zone, *, rule_later_h=0):
    """*s*'s account moved to *zone*, and its rule's time of day read there from its slot,
    *rule_later_h* hours later."""
    _exec(world, "UPDATE ig_accounts SET tz = %s WHERE id = %s", (zone, s["acct"]))
    _exec(
        world,
        "UPDATE post_intents SET repeat_at = (schedule_slot_at AT TIME ZONE %s)::time"
        " + make_interval(hours => %s) WHERE id = %s",
        (zone, rule_later_h, s["intent"]),
    )


class TestASeriesPostsOnceALocalDay:
    @pytest.mark.parametrize("verb, days_on", [("mark_posted", 1), ("skip", 0)])
    def test_a_card_answered_a_day_late(self, world, verb, days_on):
        """Yesterday's card is answered two hours before today's time. Posted, it is the item's
        post for today, so the series waits for tomorrow; skipped, nothing posted, so today's
        time stands."""
        zone = _zone_reading(12)
        w = _ws(world, f"late-{verb}")
        s = _seed(world, w, state="awaiting_approval", slot_in_s=-22 * 3600)
        _in_zone(world, s, zone)
        run(world, w, verb, intent_id=s["intent"])
        here = ZoneInfo(zone)
        nxt = _at(_one_successor(world, s)).astimezone(here)
        assert nxt.date() == datetime.now(here).date() + timedelta(days=days_on)
        assert nxt.time() == s["slot"].astimezone(here).time()

    def test_a_post_after_the_account_moved_west(self, world):
        """The rule was set in a zone three hours east of the account's new one, so the story
        posts three hours before the rule's time of day there. That time today would be the
        item's second post today."""
        zone = _zone_reading(6)
        w = _ws(world, "west")
        s = _seed(world, w, state="awaiting_approval", slot_in_s=-600)
        _in_zone(world, s, zone, rule_later_h=3)
        run(world, w, "mark_posted", intent_id=s["intent"])
        here = ZoneInfo(zone)
        nxt = _at(_one_successor(world, s)).astimezone(here)
        assert nxt.date() == datetime.now(here).date() + timedelta(days=1)
        assert nxt.time() == (s["slot"] + timedelta(hours=3)).astimezone(here).time()


# --- the grid ----------------------------------------------------------------------------

ANCHOR = utc(2026, 10, 15, 13, 0)  # Thursday 09:00 in New York
ANCHOR_PM = utc(2026, 10, 15, 20, 7)  # Thursday 16:07 in New York


class TestTheGrid:
    @pytest.mark.parametrize(
        "unit, every, days, at, anchor, lo, hi, want",
        [
            pytest.param(
                "day",
                1,
                None,
                "09:00",
                ANCHOR,
                ANCHOR,
                ANCHOR + timedelta(days=3),
                [ANCHOR + timedelta(days=d) for d in range(3)],
                id="every_day",
            ),
            pytest.param(
                "day",
                3,
                None,
                "09:00",
                ANCHOR,
                ANCHOR + timedelta(hours=1),
                ANCHOR + timedelta(days=8),
                [ANCHOR + timedelta(days=3), ANCHOR + timedelta(days=6)],
                id="every_third_day",
            ),
            # A window that opens five days in: a grid counted from the window's first day
            # would answer days 4 and 7; the rule's grid is days 6 and 9 from the anchor.
            pytest.param(
                "day",
                3,
                None,
                "09:00",
                ANCHOR,
                ANCHOR + timedelta(days=5),
                ANCHOR + timedelta(days=10),
                [ANCHOR + timedelta(days=6), ANCHOR + timedelta(days=9)],
                id="from_the_anchor_not_the_window",
            ),
            pytest.param(
                "week",
                1,
                [1, 4],
                "16:07",
                ANCHOR_PM,
                ANCHOR_PM,
                ANCHOR_PM + timedelta(days=14),
                [ANCHOR_PM + timedelta(days=d) for d in (0, 4, 7, 11)],
                id="weekly_on_mondays_and_thursdays",
            ),
            pytest.param(
                "week",
                2,
                [1, 4],
                "16:07",
                ANCHOR_PM,
                ANCHOR_PM,
                ANCHOR_PM + timedelta(days=28),
                [
                    ANCHOR_PM,
                    ANCHOR_PM + timedelta(days=11),
                    ANCHOR_PM + timedelta(days=14),
                    utc(
                        2026, 11, 9, 21, 7
                    ),  # past 1 November's clock change: 16:07 EST
                ],
                id="every_other_week",
            ),
            # A Wednesday anchor, not a chosen day: the next is the Thursday after it.
            pytest.param(
                "week",
                1,
                [1, 4],
                "16:07",
                ANCHOR_PM - timedelta(days=1),
                ANCHOR_PM - timedelta(days=1) + timedelta(microseconds=1),
                ANCHOR_PM + timedelta(days=4),
                [ANCHOR_PM],
                id="off_grid_anchor",
            ),
            pytest.param(
                "day",
                1,
                None,
                "09:00",
                ANCHOR,
                ANCHOR,
                ANCHOR + timedelta(days=1),
                [ANCHOR],
                id="lower_bound_inclusive_upper_exclusive",
            ),
            pytest.param(
                "day",
                0,
                None,
                "09:00",
                ANCHOR,
                ANCHOR,
                ANCHOR + timedelta(days=3),
                [],
                id="interval_zero_yields_nothing",
            ),
            # No upper bound: up to two local days past one period after the lower bound, a
            # window that always holds the next.
            pytest.param(
                "week",
                1,
                [4],
                "16:07",
                ANCHOR_PM,
                ANCHOR_PM + timedelta(microseconds=1),
                None,
                [ANCHOR_PM + timedelta(days=7)],
                id="next_within_one_period",
            ),
        ],
    )
    def test_the_grid(self, world, unit, every, days, at, anchor, lo, hi, want):
        assert _times(world, unit, every, days, at, anchor, TZ, lo, hi) == want

    def test_a_decayed_zone_reads_as_utc(self, world):
        anchor = utc(2026, 10, 15, 9, 0)
        got = _times(
            world,
            "day",
            1,
            None,
            "09:00",
            anchor,
            "Not/AZone",
            anchor,
            anchor + timedelta(days=2),
        )
        assert got == [anchor, anchor + timedelta(days=1)]

    @pytest.mark.parametrize(
        "tz, day, clock, reading",
        [
            # a repeated hour resolves to its first occurrence, as one-off planning reads it
            ("America/New_York", (2026, 11, 1), "01:30", utc(2026, 11, 1, 5, 30)),
            ("Europe/London", (2026, 10, 25), "01:30", utc(2026, 10, 25, 0, 30)),
            # a skipped hour takes the offset before the jump
            ("America/New_York", (2027, 3, 14), "02:30", utc(2027, 3, 14, 7, 30)),
            ("Europe/London", (2027, 3, 28), "01:30", utc(2027, 3, 28, 1, 30)),
        ],
        ids=[
            "repeated_hour_ny",
            "repeated_hour_london",
            "skipped_hour_ny",
            "skipped_hour_london",
        ],
    )
    def test_a_clock_change_day(self, world, tz, day, clock, reading):
        anchor = utc(*day) - timedelta(days=2)
        lo, hi = utc(*day) - timedelta(hours=6), utc(*day) + timedelta(hours=18)
        assert _times(world, "day", 1, None, clock, anchor, tz, lo, hi) == [reading]


# --- the CHECKs ------------------------------------------------------------------------------


@pytest.fixture(scope="class")
def unruled(world):
    """One planned story with no rule, which every refused UPDATE leaves as it was."""
    return _seed(world, _ws(world, "check"), rule=None)


class TestTheRuleIsTheDatabases:
    @pytest.mark.parametrize(
        "assignment",
        [
            "repeat_unit = 'day', repeat_at = '09:00'",
            "repeat_every = 1, repeat_at = '09:00'",
            "repeat_unit = 'day', repeat_every = 1",
            "repeat_unit = 'day', repeat_every = 1, repeat_at = '09:00', repeat_days = '{1}'",
            "repeat_unit = 'week', repeat_every = 1, repeat_at = '09:00'",
            "repeat_unit = 'week', repeat_every = 1, repeat_at = '09:00', repeat_days = '{}'",
            "repeat_unit = 'week', repeat_every = 1, repeat_at = '09:00', repeat_days = '{1,8}'",
            "repeat_unit = 'week', repeat_every = 1, repeat_at = '09:00', repeat_days = '{{1,4}}'",
            "repeat_unit = 'day', repeat_every = 0, repeat_at = '09:00'",
            "repeat_unit = 'week', repeat_every = 53, repeat_at = '09:00', repeat_days = '{1}'",
            "repeat_unit = 'day', repeat_every = 366, repeat_at = '09:00'",
        ],
    )
    def test_a_malformed_rule_is_refused_by_the_shape_check(
        self, world, unruled, assignment
    ):
        with pytest.raises(psycopg2.errors.CheckViolation) as err:
            _exec(
                world,
                f"UPDATE post_intents SET {assignment} WHERE id = %s",
                (unruled["intent"],),
            )
        assert err.value.diag.constraint_name == "ck_intent_repeat"

    def test_an_unknown_unit_is_refused_by_the_unit_list(self, world, unruled):
        with pytest.raises(psycopg2.errors.CheckViolation) as err:
            _exec(
                world,
                "UPDATE post_intents SET repeat_unit = 'month', repeat_every = 1,"
                " repeat_at = '09:00' WHERE id = %s",
                (unruled["intent"],),
            )
        assert err.value.diag.constraint_name == "ck_intent_repeat_unit"

    @pytest.mark.parametrize(
        "assignment",
        [
            "repeat_unit = 'day', repeat_every = 365, repeat_at = '09:00'",
            "repeat_unit = 'week', repeat_every = 52, repeat_at = '23:59',"
            " repeat_days = '{1,2,3,4,5,6,7}'",
        ],
    )
    def test_a_well_formed_rule_is_admitted(self, world, assignment):
        s = _seed(world, _ws(world, "check-ok"), rule=None)
        assert (
            _exec(
                world,
                f"UPDATE post_intents SET {assignment} WHERE id = %s",
                (s["intent"],),
            )
            == 1
        )

    def test_a_cadence_story_cannot_carry_a_rule(self, world):
        w = _ws(world, "cadence")
        cadence = _story(world, w, origin="cadence")
        with pytest.raises(psycopg2.errors.CheckViolation) as err:
            _exec(
                world,
                "UPDATE post_intents SET repeat_unit = 'day', repeat_every = 1,"
                " repeat_at = '09:00' WHERE id = %s",
                (cadence["intent"],),
            )
        assert err.value.diag.constraint_name == "ck_intent_repeat"


# --- totality, a clean start, and the pins ----------------------------------------------


class TestTheTriggerIsTotal:
    def test_a_failed_successor_records_repeat_failed_and_the_sweep_completes(
        self, world
    ):
        w = _ws(world, "total")
        s = _seed(
            world, w, state="awaiting_approval", slot_in_s=-600, entered_ago_s=25 * 3600
        )
        bystander = _seed(
            world, w, state="scheduled", slot_in_s=86400, flagged=True, rule=None
        )
        _exec(world, "REVOKE INSERT ON post_intents FROM svc_maintenance")
        try:
            _reap(world)
        finally:
            _exec(world, "GRANT INSERT ON post_intents TO svc_maintenance")
        assert _row(world, s["intent"])["state"] == "expired", "the ending stands"
        assert _row(world, bystander["intent"])["state"] == "cancelled", (
            "the other legs ran"
        )
        assert _successors(world, s) == []
        ((actor, *_, detail),) = _audit(world, s["intent"], "repeat_failed")
        assert actor == "reaper" and detail["sqlstate"] == "42501"

    def test_even_a_refused_failure_record_lets_the_ending_stand(self, world):
        w = _ws(world, "total-2")
        s = _seed(
            world, w, state="awaiting_approval", slot_in_s=-600, entered_ago_s=25 * 3600
        )
        _exec(world, "REVOKE INSERT ON post_intents FROM svc_maintenance")
        _exec(
            world,
            "ALTER TABLE audit_events ADD CONSTRAINT ck_probe_no_repeat_failed"
            " CHECK (detail->>'event' IS DISTINCT FROM 'repeat_failed') NOT VALID",
        )
        try:
            _reap(world)
        finally:
            _exec(
                world,
                "ALTER TABLE audit_events DROP CONSTRAINT ck_probe_no_repeat_failed",
            )
            _exec(world, "GRANT INSERT ON post_intents TO svc_maintenance")
        assert _row(world, s["intent"])["state"] == "expired"
        assert _audit(world, s["intent"], "repeat_failed") == []

    def test_a_failed_successor_warns_with_its_story_and_sqlstate(self, world):
        """The warning the server log gets. The door is called as `svc_worker` calls it, on a
        connection whose notices can be read."""
        w = _ws(world, "total-3")
        s = _seed(
            world, w, state="awaiting_approval", slot_in_s=-600, entered_ago_s=25 * 3600
        )
        _exec(world, "REVOKE INSERT ON post_intents FROM svc_maintenance")
        try:
            dsn = as_user(world["owner"], "svc_worker")
            with txn(dsn, expect_user="svc_worker") as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT fn_reaper_sweep(500, interval '24 hours',"
                        " interval '72 hours')"
                    )
                conn.commit()
                notices = list(conn.notices)
        finally:
            _exec(world, "GRANT INSERT ON post_intents TO svc_maintenance")
        assert _row(world, s["intent"])["state"] == "expired"
        warning = (
            f"post_intent {s['intent']}: its next occurrence was not created (42501)"
        )
        assert any(n.startswith("WARNING:") and warning in n for n in notices), notices


class TestASuccessorStartsClean:
    def test_a_successor_starts_clean(self, world):
        """Every column tg_intent_repeat's INSERT does not name holds its default on the
        successor's approval_rule among them, and nothing is carried over from
        the ended occurrence. `approval_mode` is named, and written as 'manual', as
        every insert writes it (approval rules)."""
        w = _ws(world, "clean")
        s = _seed(world, w, state="publishing", slot_in_s=-600, cap=True)
        _exec(
            world,
            "UPDATE post_intents SET transit_asset_ref = 'transit-x', ig_container_id = 'c-x',"
            " approval_mode = 'auto', last_error = '{\"class\": \"probe\"}' WHERE id = %s",
            (s["intent"],),
        )
        _as_worker_update(
            world,
            w["ws"],
            "UPDATE post_intents SET state = 'posted', published_via = 'manual'"
            " WHERE id = CAST(:id AS uuid)",
            {"id": s["intent"]},
        )
        successor = _one_successor(world, s)
        assert successor["approval_mode"] == "manual"
        unnamed = [
            (name, default)
            for name, default in _exec(
                world,
                "SELECT column_name, column_default FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'post_intents'",
                fetch=True,
            )
            if name not in NAMED and name not in CLOCKED
        ]
        defaults = ", ".join(
            f"'{name}', to_jsonb({default if default is not None else 'NULL::text'})"
            for name, default in unnamed
        )
        ((expected,),) = _exec(
            world, f"SELECT jsonb_build_object({defaults})", fetch=True
        )
        for name, _ in unnamed:
            assert successor[name] == expected[name], (
                f"{name} carried {successor[name]!r}"
            )
        assert len(unnamed) >= 10, (
            "the positive control: the unnamed columns were all compared"
        )


class TestTheTriggersTerminalListIsTheVocabularys:
    def test_it_fires_on_every_ending_but_reject(self, world):
        ((definition,),) = _exec(
            world,
            "SELECT pg_get_triggerdef(oid) FROM pg_trigger WHERE tgname = 'tg_intent_repeat'",
            fetch=True,
        )
        listed = set(
            re.findall(
                r"'([a-z_]+)'::text",
                re.split(r"new\.state = ANY", definition, flags=re.I)[1],
            )
        )
        assert listed == set(vocabulary.TERMINAL_STATES) - {"rejected"}

    def test_a_second_workspace_sees_none_of_it(self, world):
        w = _ws(world, "mine")
        theirs = _ws(world, "theirs")
        s = _seed(world, w, state="awaiting_approval", slot_in_s=-600)
        run(world, w, "skip", intent_id=s["intent"])
        assert len(_successors(world, s)) == 1

        async def count(session):
            return (
                await session.execute(
                    text("SELECT count(*) FROM post_intents WHERE media_item_id = :m"),
                    {"m": s["media"]},
                )
            ).scalar()

        assert (
            asyncio.run(
                in_tenant(world["ingress"], theirs["ws"], theirs["user"], count)
            )
            == 0
        )
