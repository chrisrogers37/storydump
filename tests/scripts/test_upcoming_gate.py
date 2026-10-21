"""The calendar's upcoming read, as `svc_ingress` on the replayed schema.

What only a database can say about `upcoming.upcoming`: that a predicted slot
is one the clock will mint. The clock's side is measured apart from the read:
`_clock_walk` applies `fn_next_slot` to its own answer from the account's
cursor, as the tick's cursor advance does, under settings the test names
itself. The read is right where it returns that walk's slots.

Covered: accounts in different time zones, a day the clocks change, an account
with no posting hours of its own, a planned story and predicted slots on one
day, the accounts and workspaces the clock does not post for, a cursor that is
overdue, one far behind the range, and one off the grid, a repeating series'
later occurrences, another workspace's rows, and the bounds.

Most ranges are fixed days in 2031, so no result depends on the day the suite
runs: the read leaves out a slot whose time has come, and none of these has.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import pytest
from sqlalchemy import text

from src.services.target import upcoming, vocabulary, workspaces
from tests.scripts.conftest import (
    _dsn,
    _scratch,
    as_user,
    in_tenant,
    ingress_engine,
    build_world,
    seed_workspace_chain,
)
from tests.scripts.test_ops_views_gate import _sql

pytestmark = [pytest.mark.integration, pytest.mark.slow]

UTC = timezone.utc
NY = "America/New_York"
TOKYO = "Asia/Tokyo"
LONDON = "Europe/London"
LORD_HOWE = "Australia/Lord_Howe"

#: Three days with no clock change near them in any zone used here.
JUNE = date(2031, 6, 10)
#: New York's clocks go forward: 02:00 does not exist on this day.
SPRING = date(2031, 3, 9)
#: New York's clocks go back: 01:00 comes twice on this day.
AUTUMN = date(2031, 11, 2)
#: An instant on the day before JUNE; a cursor is stood on the first slot after it.
EVE = datetime(2031, 6, 9, 1, tzinfo=UTC)

#: A weekly rule's four columns, as `_story` takes them: Monday and Thursday at
#: 16:07.
MON_THU = {
    "repeat_unit": "week",
    "repeat_every": 1,
    "repeat_days": [1, 4],
    "repeat_at": time(16, 7),
}


def _daily(at: time) -> dict:
    """A daily rule at *at*, as `_story` takes it."""
    return {
        "repeat_unit": "day",
        "repeat_every": 1,
        "repeat_days": None,
        "repeat_at": at,
    }


#: A cadence as `fn_next_slot` takes it: (zone, start hour, end hour, posts per day).
WS_DEFAULT = (
    NY,
    14,
    2,
    3,
)  # what a workspace is born with (`identity_and_tenancy.py`), read in New York
FOUR_A_DAY = (NY, 9, 21, 4)
HOURLY = (NY, 0, 0, 24)


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """The replayed schema and three logins: the owner's, to seed; the API's
    production role, to read; and one the policies do not filter."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = build_world(db, owner_actor, admin_conn)
        yield {
            "stream": stream,
            "ingress": as_user(db, "svc_ingress"),
            "bypass": _dsn(db.rsplit("/", 1)[-1]),
        }
    finally:
        gen.close()


# --- seeding ------------------------------------------------------------------


def _schedule(settings) -> dict:
    """*settings* as the schedule columns of a workspace or an account."""
    zone, start, end, ppd = settings
    return {
        "tz": zone,
        "posts_per_day": ppd,
        "posting_hours_start": start,
        "posting_hours_end": end,
    }


def _workspace(world, name: str, **columns) -> dict:
    """A workspace of its own for one test, with *columns* set on it. Its
    chain's account has no cursor, so the clock does not post for it."""
    conn = psycopg2.connect(world["stream"])
    try:
        ids = seed_workspace_chain(conn, f"upcoming-{name}")
        if columns:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE workspaces SET {', '.join(f'{c} = %s' for c in columns)}"
                    " WHERE id = %s",
                    (*columns.values(), ids["ws"]),
                )
            conn.commit()
        return ids
    finally:
        conn.close()


def _account(world, ws: dict, name: str, *, cursor=None, state="active", **overrides):
    """An account of *ws* with its cursor at *cursor*. *overrides* are its own
    schedule columns; one left out is NULL, which inherits the workspace's."""
    columns = {
        "workspace_id": ws["ws"],
        "provider_account_ref": f"ref-{name}",
        "handle": name,
        "display_name": name.title(),
        "state": state,
        "next_slot_at": cursor,
        **overrides,
    }
    ((account_id,),) = _sql(
        world["stream"],
        f"INSERT INTO ig_accounts ({', '.join(columns)})"
        f" VALUES ({', '.join(['%s'] * len(columns))}) RETURNING id",
        tuple(columns.values()),
    )
    return {"id": str(account_id), "ref": f"ref-{name}", "cursor": cursor}


def _story(
    world, ws: dict, account: dict, name: str, slot_at, *, origin="planned", **columns
) -> str:
    """A story for *account* at *slot_at* with a media item of its own, born
    with *origin* (`post_intents.origin` fixes it at birth). Returns its id."""
    ((media,),) = _sql(
        world["stream"],
        "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
        " media_kind, provider_file_ref) VALUES (%s, %s, %s, %s, 'image', %s)"
        " RETURNING id",
        (ws["ws"], ws["src"], f"hash-{name}", f"{name}.jpg", f"file-{name}"),
    )
    columns = {
        "workspace_id": ws["ws"],
        "ig_account_id": account["id"],
        "media_item_id": media,
        "provider_account_ref": account["ref"],
        "approval_mode": "manual",
        "schedule_slot_at": slot_at,
        "origin": origin,
        **columns,
    }
    ((intent,),) = _sql(
        world["stream"],
        f"INSERT INTO post_intents ({', '.join(columns)})"
        f" VALUES ({', '.join(['%s'] * len(columns))}) RETURNING id",
        tuple(columns.values()),
    )
    return str(intent)


def _daily_story(
    world,
    ws: dict,
    account: dict,
    name: str,
    day: date,
    hour: int,
    minute: int = 0,
    zone: str = NY,
    **columns,
) -> str:
    """A planned story at *hour*:*minute* on *day* on *zone*'s wall clock,
    repeating daily at that time."""
    slot = _at(day, hour, minute, zone)
    return _story(
        world, ws, account, name, slot, **_daily(time(hour, minute)), **columns
    )


def _served(world, name: str) -> dict:
    """A workspace on New York time with the schedule a workspace is born
    with, one account the clock posts for, and one story planned for 10:30 on
    JUNE, which repeats daily."""
    ws = _workspace(world, name, tz=NY)
    account = _account(
        world, ws, f"acct-{name}", cursor=_next_slot(world, EVE, WS_DEFAULT)
    )
    story = _daily_story(world, ws, account, f"story-{name}", JUNE, 10, 30)
    return {"ws": ws, "account": account, "story": story}


# --- the clock's side, measured apart from the read ------------------------------


def _at(day: date, hour: int, minute: int, zone: str) -> datetime:
    """A wall-clock time in *zone* as an instant, worked out here rather than
    asked of the database."""
    return datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(zone))


def _midnight(day: date, zone: str) -> datetime:
    return _at(day, 0, 0, zone)


def _next_slot(world, after, settings):
    """`fn_next_slot` for *after* under *settings*: a slot of that grid, to
    stand a cursor on."""
    ((slot,),) = _sql(
        world["stream"],
        "SELECT fn_next_slot(%s, %s, %s, %s, %s)",
        (after, *settings),
        actor=False,
    )
    return slot


def _clock_walk(
    world, cursor, settings, first: date, days: int = 1, ws_zone: str = NY
) -> list:
    """The slots the clock mints from *cursor* under *settings*, those on the
    *days* local days of *ws_zone* from *first*: `fn_next_slot` applied to its
    own answer, as the tick's cursor advance is. It walks from the cursor
    itself however far back that is, and uses nothing of the read's."""
    lo = _midnight(first, ws_zone)
    hi = _midnight(first + timedelta(days=days), ws_zone)
    rows = _sql(
        world["stream"],
        "WITH RECURSIVE walk (slot) AS ("
        "  SELECT %s::timestamptz"
        "  UNION ALL"
        "  SELECT fn_next_slot(slot, %s, %s, %s, %s) FROM walk WHERE slot < %s"
        ") SELECT slot FROM walk WHERE slot >= %s AND slot < %s ORDER BY slot",
        (cursor, *settings, hi, lo, hi),
        actor=False,
    )
    return [slot for (slot,) in rows]


def _now(world) -> datetime:
    ((now,),) = _sql(world["stream"], "SELECT clock_timestamp()", actor=False)
    return now


# --- the read, as the API runs it -------------------------------------------------


def _as_ingress(world, ws: dict, fn, **kwargs):
    """*fn* as the web's member read runs it: `svc_ingress`, which `in_tenant`
    asserts, the workspace as the tenant, a person on the web."""
    return asyncio.run(
        in_tenant(
            world["ingress"],
            ws["ws"],
            ws["user"],
            lambda session: fn(session, workspace_id=str(ws["ws"]), **kwargs),
        )
    )


def _read(world, ws: dict, first: date, days: int = 1, **kwargs) -> dict:
    return _as_ingress(
        world,
        ws,
        upcoming.upcoming,
        from_date=first,
        to_date=first + timedelta(days=days),
        **kwargs,
    )


def _of(rows, account: dict) -> list[dict]:
    """The rows of *account*. Ids are compared as text: the seeding driver and
    the read's hand the same id back as different types."""
    return [row for row in rows if str(row["ig_account_id"]) == account["id"]]


def _slots(rows, account: dict | None = None) -> list:
    return [
        row["schedule_slot_at"]
        for row in (rows if account is None else _of(rows, account))
    ]


def _projected_from(rows) -> set[str]:
    """The stories a series' rows were projected from, as text."""
    return {str(row["intent_id"]) for row in rows}


def _days(first: date, days: int) -> list[date]:
    return [first + timedelta(days=n) for n in range(days)]


def _walls(slots, zone: str) -> list[str]:
    """Each slot's time of day on the wall clock of *zone*."""
    return [slot.astimezone(ZoneInfo(zone)).strftime("%H:%M") for slot in slots]


def _assert_days_are_dates_in(rows, zone: str) -> None:
    """Every row's ``day`` is its slot's date in *zone*, the workspace's."""
    assert rows
    for row in rows:
        local = row["schedule_slot_at"].astimezone(ZoneInfo(zone))
        assert row["day"] == local.date().isoformat(), row


# --- time zones ---------------------------------------------------------------------


class TestTimeZones:
    def test_each_account_is_spread_across_its_hours_in_its_own_zone(self, world):
        """Four posts a day across 09:00 to 21:00 is every three hours from
        nine, on the wall clock of the account's zone: its own, else the
        workspace's. The day a slot sits on is the workspace's."""
        in_tokyo = (TOKYO, 9, 21, 4)
        ws = _workspace(world, "zones", **_schedule(FOUR_A_DAY))
        home = _account(world, ws, "home", cursor=_next_slot(world, EVE, FOUR_A_DAY))
        tokyo = _account(
            world, ws, "tokyo", tz=TOKYO, cursor=_next_slot(world, EVE, in_tokyo)
        )
        out = _read(world, ws, JUNE, days=3)

        for account, settings in ((home, FOUR_A_DAY), (tokyo, in_tokyo)):
            zone = settings[0]
            slots = _slots(out["predicted"], account)
            assert slots == _clock_walk(
                world, account["cursor"], settings, JUNE, days=3
            )
            assert len(slots) == 12  # three of the workspace's days, four a day
            assert set(_walls(slots, zone)) == {"09:00", "12:00", "15:00", "18:00"}
            assert {row["tz"] for row in _of(out["predicted"], account)} == {zone}

        _assert_days_are_dates_in(out["predicted"], NY)
        # Positive control that the two zones tell days apart: one of Tokyo's
        # slots is on a later date there than the day it sits on.
        assert any(
            row["schedule_slot_at"].astimezone(ZoneInfo(TOKYO)).date().isoformat()
            > row["day"]
            for row in _of(out["predicted"], tokyo)
        )
        assert _slots(out["predicted"]) == sorted(_slots(out["predicted"]))
        assert out["planned"] == [] and not out["predicted_truncated"]


# --- a day the clocks change -----------------------------------------------------------


class TestADayTheClocksChange:
    @pytest.mark.parametrize(
        "day, walls",
        [
            (SPRING, [f"{h:02d}:00" for h in range(24) if h != 2]),
            (AUTUMN, [f"{h:02d}:00" for h in range(24)]),
        ],
        ids=[
            "the hour that does not exist holds no slot",
            "the hour that comes twice holds one slot",
        ],
    )
    def test_an_hourly_account_has_the_slots_the_days_wall_clock_has(
        self, world, day, walls
    ):
        """An account that posts on every hour of the day, so a missing hour
        and a repeated one each show."""
        ws = _workspace(world, "hourly", **_schedule(HOURLY))
        eve = _at(day - timedelta(days=1), 12, 0, NY)
        account = _account(world, ws, "hourly", cursor=_next_slot(world, eve, HOURLY))
        out = _read(world, ws, day)
        slots = _slots(out["predicted"])
        assert slots == _clock_walk(world, account["cursor"], HOURLY, day)
        assert _walls(slots, NY) == walls
        _assert_days_are_dates_in(out["predicted"], NY)


# --- an account with no posting hours of its own ------------------------------------------


class TestAnAccountWithNoPostingHours:
    def test_what_an_account_does_not_set_is_the_workspaces(self, world):
        """The workspace posts three times across 22:00 to 04:00. One account
        sets nothing, one only its posts per day, one only its hours."""
        ws = _workspace(world, "inherit", **_schedule((NY, 22, 4, 3)))
        cases = [
            ("bare", {}, (NY, 22, 4, 3), {"22:00", "00:00", "02:00"}),
            ("twice", {"posts_per_day": 2}, (NY, 22, 4, 2), {"22:00", "01:00"}),
            (
                "mornings",
                {"posting_hours_start": 6, "posting_hours_end": 12},
                (NY, 6, 12, 3),
                {"06:00", "08:00", "10:00"},
            ),
        ]
        accounts = [
            (
                _account(
                    world,
                    ws,
                    name,
                    cursor=_next_slot(world, EVE, settings),
                    **overrides,
                ),
                settings,
                walls,
            )
            for name, overrides, settings, walls in cases
        ]
        out = _read(world, ws, JUNE, days=2)
        for account, settings, walls in accounts:
            slots = _slots(out["predicted"], account)
            assert slots == _clock_walk(
                world, account["cursor"], settings, JUNE, days=2
            )
            assert set(_walls(slots, NY)) == walls
            assert len(slots) == 2 * settings[3]
        # A slot after midnight sits on the day it falls on, not the day its
        # posting hours opened.
        _assert_days_are_dates_in(out["predicted"], NY)


# --- planned and predicted on one day -------------------------------------------------------


class TestPlannedAndPredictedOnOneDay:
    DAY = date(2031, 6, 11)

    @pytest.fixture(scope="class")
    def day(self, world):
        ws = _workspace(world, "one-day", **_schedule(FOUR_A_DAY))
        account = _account(
            world,
            ws,
            "both",
            cursor=_next_slot(world, EVE + timedelta(days=1), FOUR_A_DAY),
        )
        day = self.DAY
        stories = {
            # the range's first instant is inside it, its end is not
            "midnight": _story(world, ws, account, "midnight", _midnight(day, NY)),
            "morning": _story(world, ws, account, "morning", _at(day, 10, 30, NY)),
            # at the very time of a cadence slot
            "noon": _story(world, ws, account, "noon", _at(day, 12, 0, NY)),
            "tomorrow": _story(
                world, ws, account, "tomorrow", _midnight(day + timedelta(days=1), NY)
            ),
            # no longer `scheduled`
            "prompted": _story(
                world,
                ws,
                account,
                "prompted",
                _at(day, 11, 0, NY),
                state="awaiting_approval",
            ),
            # the cadence's own story, not a planned one
            "cadence": _story(
                world, ws, account, "cadence", _at(day, 13, 0, NY), origin="cadence"
            ),
        }
        return {"ws": ws, "account": account, "stories": stories}

    def test_the_day_holds_its_planned_stories_and_its_predicted_slots(
        self, world, day
    ):
        stories = day["stories"]
        out = _read(world, day["ws"], self.DAY)
        assert [str(row["id"]) for row in out["planned"]] == [
            stories["midnight"],
            stories["morning"],
            stories["noon"],
        ]
        assert not out["planned_truncated"]
        for row in out["planned"]:
            assert row["origin"] == "planned" and row["state"] == "scheduled"
        slots = _slots(out["predicted"])
        assert slots == _clock_walk(
            world, day["account"]["cursor"], FOUR_A_DAY, self.DAY
        )
        assert _walls(slots, NY) == ["09:00", "12:00", "15:00", "18:00"]
        # A planned story takes no slot: noon holds the story and the slot.
        assert _at(self.DAY, 12, 0, NY) in slots
        assert {row["day"] for row in out["planned"] + out["predicted"]} == {
            "2031-06-11"
        }

    def test_a_planned_row_is_the_intents_reads_row_and_its_day(self, world, day):
        """The read's planned statement is its own, on the intents read's
        column list. This holds the two to one row."""
        out = _read(world, day["ws"], self.DAY)
        intents = {
            str(row["id"]): row
            for row in _as_ingress(
                world,
                day["ws"],
                workspaces.list_intents,
                states=["scheduled"],
                origin="planned",
            )
        }
        assert out["planned"]
        for row in out["planned"]:
            assert {k: v for k, v in row.items() if k != "day"} == intents[
                str(row["id"])
            ]

    def test_a_predicted_row_says_it_is_one_and_shares_its_keys_with_a_story(
        self, world, day
    ):
        out = _read(world, day["ws"], self.DAY)
        assert out["predicted"]
        for row in out["predicted"]:
            # the whole row: a kind, never a state
            assert {**row, "ig_account_id": str(row["ig_account_id"])} == {
                "kind": "predicted",
                "schedule_slot_at": row["schedule_slot_at"],
                "day": "2031-06-11",
                "tz": NY,
                "ig_account_id": day["account"]["id"],
                "account_handle": "both",
                "account_display_name": "Both",
            }
            assert set(row) - {"kind"} <= set(out["planned"][0])


# --- who has no predicted slot ------------------------------------------------------------------


class TestWhoHasNoPredictedSlot:
    def test_only_an_account_the_clock_posts_for_is_projected(self, world):
        ws = _workspace(world, "unserved", tz=NY)
        cursor = _next_slot(world, EVE, WS_DEFAULT)
        served = _account(world, ws, "served", cursor=cursor)
        _account(world, ws, "never-scheduled")
        _account(world, ws, "reauth", cursor=cursor, state="reauth_required")
        _account(world, ws, "disabled", cursor=cursor, state="disabled")
        rows = _read(world, ws, JUNE)["predicted"]
        assert {str(row["ig_account_id"]) for row in rows} == {served["id"]}

    @pytest.mark.parametrize(
        "name, stop",
        [
            ("paused", "is_paused = true"),
            ("suspended", "state = 'suspended'"),
            ("offboarding", "state = 'offboarding'"),
        ],
    )
    def test_a_workspace_the_clock_does_not_serve_has_none(self, world, name, stop):
        served = _served(world, name)
        ws = served["ws"]
        assert _read(world, ws, JUNE)["predicted"], (
            "positive control: it is projected while the clock serves it"
        )
        _sql(
            world["stream"], f"UPDATE workspaces SET {stop} WHERE id = %s", (ws["ws"],)
        )
        out = _read(world, ws, JUNE)
        assert out["predicted"] == []
        # Its planned story is still a row in the ledger, and still shown.
        assert [str(row["id"]) for row in out["planned"]] == [served["story"]]


# --- where a walk starts: at the cursor, or short of the range ------------------------------


class TestASlotWhoseTimeHasCome:
    @pytest.mark.parametrize(
        "overdue",
        [timedelta(hours=30), timedelta(days=10)],
        ids=[
            "a cursor thirty hours overdue, which the walk starts at",
            "a cursor ten days overdue, which the walk starts ahead of",
        ],
    )
    def test_an_overdue_cursors_past_slots_are_not_predicted(self, world, overdue):
        """The clock's walk from an overdue cursor passes through slots already
        due. The read leaves those out and keeps the rest, whether it walks
        from the cursor or, the cursor being further back than the lead, from
        two days before now. The database's clock moves between the
        statements, so the read is held between the walk as of before it and
        as of after."""
        ws = _workspace(world, "overdue", **_schedule(HOURLY))
        start = _now(world)
        cursor = _next_slot(world, start - overdue, HOURLY)
        account = _account(world, ws, "late", cursor=cursor)
        first = start.astimezone(ZoneInfo(NY)).date() - timedelta(days=2)
        walk = _clock_walk(world, cursor, HOURLY, first, days=5)
        assert any(slot <= start for slot in walk), (
            "positive control: the walk holds slots whose time has come"
        )
        before = _now(world)
        slots = _slots(_read(world, ws, first, days=5)["predicted"], account)
        after = _now(world)
        assert slots == sorted(slots) and slots
        assert {s for s in walk if s > after} <= set(slots)
        assert set(slots) <= {s for s in walk if s > before}


def _behind(year: int, month: int, day: int) -> datetime:
    """Where a far-behind cursor stands on that day."""
    return datetime(year, month, day, 3, 17, 11, tzinfo=UTC)


#: A cursor far behind the range, by case: the workspace's zone, the account's
#: settings, the range's first day and its days, where the cursor stands, and
#: whether it stands on the grid (True) or off it.
FAR_BEHIND = {
    "hourly, across the missing hour": (
        NY,
        HOURLY,
        SPRING,
        2,
        _behind(2031, 1, 20),
        True,
    ),
    "forty-minute steps, the day after the missing hour": (
        NY,
        (NY, 0, 0, 36),
        SPRING + timedelta(days=1),
        2,
        _behind(2031, 2, 1),
        False,
    ),
    "forty-minute steps, two days after the missing hour": (
        NY,
        (NY, 0, 0, 36),
        SPRING + timedelta(days=2),
        1,
        _behind(2031, 2, 1),
        True,
    ),
    "ninety-minute steps, across the missing hour": (
        NY,
        (NY, 0, 0, 16),
        SPRING - timedelta(days=1),
        3,
        _behind(2031, 2, 1),
        False,
    ),
    "posting hours that are the missing hour": (
        NY,
        (NY, 2, 3, 50),
        SPRING - timedelta(days=1),
        4,
        _behind(2031, 2, 20),
        True,
    ),
    "forty-minute steps, across the repeated hour": (
        NY,
        (NY, 0, 0, 36),
        AUTUMN - timedelta(days=1),
        3,
        _behind(2031, 9, 28),
        False,
    ),
    "another zone's clock change on a New York calendar": (
        NY,
        (LONDON, 0, 0, 36),
        date(2031, 3, 29),
        4,
        _behind(2031, 2, 1),
        False,
    ),
    "a half-hour clock change": (
        "UTC",
        (LORD_HOWE, 22, 6, 13),
        date(2031, 10, 4),
        3,
        _behind(2031, 9, 1),
        True,
    ),
    "a year behind, no clock change": (
        NY,
        (TOKYO, 9, 21, 5),
        JUNE,
        3,
        _behind(2030, 6, 1),
        True,
    ),
}


class TestACursorFarBehind:
    """The read does not walk from a cursor far behind its range: it starts at
    the slot `fn_next_slot` answers for an instant two days before the range.
    These are the cases where that could differ from the clock's own walk, a
    clock change inside those two days or just before them among them, and in
    each the read is the clock's walk from the cursor.

    A read that walked from these cursors would not reach the range: its step
    bound is sized for the range and those two days."""

    @pytest.mark.parametrize(
        "ws_zone, settings, first, days, behind, on_grid",
        list(FAR_BEHIND.values()),
        ids=list(FAR_BEHIND),
    )
    def test_it_predicts_what_the_clock_will_mint(
        self, world, ws_zone, settings, first, days, behind, on_grid
    ):
        ws = _workspace(world, "behind", tz=ws_zone)
        cursor = _next_slot(world, behind, settings) if on_grid else behind
        account = _account(world, ws, "behind", cursor=cursor, **_schedule(settings))
        lead = timedelta(days=upcoming.WALK_LEAD_DAYS + 5)
        assert cursor < _midnight(first, ws_zone) - lead, (
            "positive control: the cursor is well behind the walk's start"
        )
        walk = _clock_walk(world, cursor, settings, first, days, ws_zone)
        assert walk, "positive control: the range holds slots"
        out = _read(world, ws, first, days=days)
        assert _slots(out["predicted"], account) == walk
        assert not out["predicted_truncated"]
        _assert_days_are_dates_in(out["predicted"], ws_zone)

    def test_the_walk_starts_short_of_the_range_so_its_first_slot_is_kept(self, world):
        """Why the walk has a lead. `fn_next_slot` answers a slot with the next
        one, never itself, so a walk begun at the range's first instant would
        hold no slot there. The read starts short of the range and keeps it."""
        ws = _workspace(world, "first-slot", **_schedule(HOURLY))
        cursor = _next_slot(world, _behind(2031, 5, 1), HOURLY)
        account = _account(world, ws, "hourly", cursor=cursor)
        midnight = _midnight(JUNE, NY)
        walk = _clock_walk(world, cursor, HOURLY, JUNE)
        assert len(walk) == 24 and walk[0] == midnight
        assert _next_slot(world, midnight, HOURLY) == walk[1]
        assert _slots(_read(world, ws, JUNE)["predicted"], account) == walk


class TestACursorOffTheGrid:
    def test_the_cursor_is_itself_the_next_slot_then_the_grid_resumes(self, world):
        """A cursor set under other settings stands off today's grid. The clock
        mints it as it stands and advances from it, and so does the read."""
        ws = _workspace(world, "off-grid", **_schedule(FOUR_A_DAY))
        cursor = _at(JUNE, 10, 17, NY)
        account = _account(world, ws, "moved", cursor=cursor)
        slots = _slots(_read(world, ws, JUNE, days=2)["predicted"], account)
        assert slots == _clock_walk(world, cursor, FOUR_A_DAY, JUNE, days=2)
        assert slots[0] == cursor
        assert _walls(slots, NY) == [
            "10:17",
            "12:00",
            "15:00",
            "18:00",
            "09:00",
            "12:00",
            "15:00",
            "18:00",
        ]


# --- one workspace only ------------------------------------------------------------------------


class TestOneWorkspaceOnly:
    @pytest.fixture(scope="class")
    def two(self, world):
        return {name: _served(world, name) for name in ("mine", "theirs")}

    def _assert_only(self, out: dict, own: dict) -> None:
        assert [str(row["id"]) for row in out["planned"]] == [own["story"]]
        assert out["predicted"]
        assert {str(row["ig_account_id"]) for row in out["predicted"]} == {
            own["account"]["id"]
        }
        assert out["repeating"]
        assert {str(row["intent_id"]) for row in out["repeating"]} == {own["story"]}

    def test_each_workspace_reads_its_own_stories_and_slots(self, world, two):
        # Two days: the story's next occurrence is the second's.
        for own in two.values():
            self._assert_only(_read(world, own["ws"], JUNE, days=2), own)

    def test_the_predicates_confine_the_read_without_the_policies(self, world, two):
        """The same read as a login the policies do not filter, with no tenant
        set: its own `workspace_id` predicates alone keep it to one workspace
        (the policies are not the only fence)."""
        mine = two["mine"]

        async def main():
            async with (
                ingress_engine(world["bypass"]) as engine,
                engine.connect() as conn,
            ):
                unfiltered = (
                    await conn.execute(
                        text(
                            "SELECT rolbypassrls OR rolsuper FROM pg_roles"
                            " WHERE rolname = current_user"
                        )
                    )
                ).scalar()
                assert unfiltered, "this arm must not be filtered by the policies"
                return await upcoming.upcoming(
                    conn,
                    workspace_id=str(mine["ws"]["ws"]),
                    from_date=JUNE,
                    to_date=JUNE + timedelta(days=2),
                )

        self._assert_only(asyncio.run(main()), mine)


# --- a series' later occurrences ----------------------------------------------------


class TestARepeatingSeries:
    """A live repeating story's later occurrences, projected from its rule by
    `fn_repeat_times` as its successors will be made. The times expected are
    worked out here from the calendar, not asked of the database."""

    #: The waiting story: Thursday 5 June 2031 at 16:07 in New York.
    STORY_DAY = date(2031, 6, 5)

    @pytest.fixture(scope="class")
    def series(self, world):
        ws = _workspace(world, "series", tz=NY)
        account = _account(world, ws, "series")
        story = _story(
            world, ws, account, "weekly", _at(self.STORY_DAY, 16, 7, NY), **MON_THU
        )
        return {"ws": ws, "account": account, "story": story}

    def _mondays_and_thursdays(self, first: date, days: int) -> list[datetime]:
        return [
            _at(day, 16, 7, NY)
            for day in _days(first, days)
            if day > self.STORY_DAY and day.isoweekday() in (1, 4)
        ]

    @pytest.mark.parametrize(
        "first, days",
        [(date(2031, 6, 1), 30), (date(2031, 8, 1), 31)],
        ids=["the story's own month", "two months ahead"],
    )
    def test_a_weekly_series_comes_each_chosen_weekday_after_its_story(
        self, world, series, first, days
    ):
        out = _read(world, series["ws"], first, days=days)
        assert _slots(out["repeating"]), "positive control: the range holds occurrences"
        assert _slots(out["repeating"]) == self._mondays_and_thursdays(first, days)
        assert _projected_from(out["repeating"]) == {series["story"]}
        assert not out["repeating_truncated"]

    def test_the_storys_own_time_is_its_own_row(self, world, series):
        out = _read(world, series["ws"], self.STORY_DAY)
        assert [str(row["id"]) for row in out["planned"]] == [series["story"]]
        assert out["repeating"] == []

    def test_a_row_is_a_place_that_names_its_story_with_its_picture_and_rule(
        self, world, series
    ):
        # Thursday the 5th to Monday the 9th: the story, then its next occurrence.
        out = _read(world, series["ws"], self.STORY_DAY, days=5)
        (story,) = out["planned"]
        (row,) = out["repeating"]
        shared = (
            "tz",
            "ig_account_id",
            "media_item_id",
            "file_name",
            "media_kind",
            "category",
            "has_thumbnail",
            "thumbnail_version",
            "repeat",
            # the Queue's projection, as on the planned list: None for
            # a Drive story, the past-posts source's origin for a past-posts source one
            "provenance",
        )
        # the whole row: a kind, never a state
        assert set(row) == {"kind", "schedule_slot_at", "day", "intent_id", *shared}
        assert {key: row[key] for key in shared} == {key: story[key] for key in shared}
        assert row["kind"] == "repeat" and str(row["intent_id"]) == series["story"]
        assert row["schedule_slot_at"] == _at(date(2031, 6, 9), 16, 7, NY)
        assert row["day"] == "2031-06-09"
        assert row["repeat"] == {
            "unit": "week",
            "every": 1,
            "days": [1, 4],
            "at": "16:07",
        }

    def test_across_a_clock_change_each_occurrence_keeps_its_wall_time(self, world):
        """New York's clocks go back on 2 November 2031, and a daily
        series still comes at 16:07 on the wall clock."""
        ws = _workspace(world, "series-autumn", tz=NY)
        account = _account(world, ws, "autumn")
        _daily_story(world, ws, account, "autumn", date(2031, 10, 30), 16, 7)
        first = date(2031, 10, 31)
        slots = _slots(_read(world, ws, first, days=5)["repeating"])
        assert slots == [_at(day, 16, 7, NY) for day in _days(first, 5)]
        assert _walls(slots, NY) == ["16:07"] * 5
        offsets = {slot.astimezone(ZoneInfo(NY)).utcoffset() for slot in slots}
        assert len(offsets) == 2, "positive control: the clocks change in the range"

    def test_a_series_comes_at_its_time_in_its_accounts_zone(self, world):
        """The rule's time is its story's own, in the account's zone where it
        has one. A row's day is still the workspace's."""
        ws = _workspace(world, "series-tokyo", tz=NY)
        account = _account(world, ws, "series-tokyo", tz=TOKYO)
        _daily_story(world, ws, account, "tokyo", JUNE, 9, zone=TOKYO)
        out = _read(world, ws, JUNE, days=3)
        # 09:00 in Tokyo is 20:00 the evening before in New York.
        assert _slots(out["repeating"]) == [
            _at(day, 9, 0, TOKYO) for day in _days(JUNE + timedelta(days=1), 3)
        ]
        assert {row["tz"] for row in out["repeating"]} == {TOKYO}
        _assert_days_are_dates_in(out["repeating"], NY)

    def test_two_series_interleave_soonest_first(self, world):
        ws = _workspace(world, "series-two", tz=NY)
        account = _account(world, ws, "two")
        nine = _daily_story(world, ws, account, "nine", JUNE, 9)
        ten = _daily_story(world, ws, account, "ten", JUNE, 10)
        out = _read(world, ws, JUNE + timedelta(days=1), days=2)
        order = [str(row["intent_id"]) for row in out["repeating"]]
        assert order == [nine, ten, nine, ten]

    def test_an_occurrence_at_the_ranges_first_instant_is_in_it(self, world):
        ws = _workspace(world, "series-midnight", tz=NY)
        account = _account(world, ws, "midnight")
        _daily_story(world, ws, account, "midnight", JUNE, 0)
        out = _read(world, ws, JUNE + timedelta(days=1))
        assert _slots(out["repeating"]) == [_midnight(JUNE + timedelta(days=1), NY)]

    def test_skip_this_one_still_projects_and_a_stopped_series_does_not(self, world):
        """A story flagged to skip keeps its rule, and the series goes on after
        it. A stopped series has no rule."""
        ws = _workspace(world, "series-skip", tz=NY)
        account = _account(world, ws, "skip")
        skipping = _daily_story(
            world, ws, account, "skipping", JUNE, 9, cancel_requested=True
        )
        _story(world, ws, account, "stopped", _at(JUNE, 10, 0, NY))
        out = _read(world, ws, JUNE, days=3)
        assert _slots(out["repeating"]) == [
            _at(day, 9, 0, NY) for day in _days(JUNE + timedelta(days=1), 2)
        ]
        assert _projected_from(out["repeating"]) == {skipping}

    def test_an_ended_occurrence_projects_nothing(self, world):
        """Only a live story projects. An ended one's series goes on in its
        successor, a story of its own."""
        ws = _workspace(world, "series-ended", tz=NY)
        account = _account(world, ws, "ended")
        _daily_story(world, ws, account, "ended", JUNE, 9, state="cancelled")
        live = _daily_story(world, ws, account, "live", JUNE, 10)
        assert _projected_from(_read(world, ws, JUNE, days=3)["repeating"]) == {live}

    @pytest.mark.parametrize(
        "home, change",
        [
            (
                "offboarding",
                "UPDATE workspaces SET state = 'offboarding' WHERE id = %s",
            ),
            ("disabled", "UPDATE ig_accounts SET state = 'disabled' WHERE id = %s"),
        ],
        ids=["its workspace offboarding", "its account no longer live"],
    )
    def test_a_series_the_trigger_would_not_carry_on_projects_nothing(
        self, world, home, change
    ):
        ws = _workspace(world, f"series-{home}", tz=NY)
        account = _account(world, ws, f"home-{home}")
        _daily_story(world, ws, account, f"home-{home}", JUNE, 9)
        assert _read(world, ws, JUNE, days=3)["repeating"], (
            "positive control: it is projected while the trigger would carry it on"
        )
        target = ws["ws"] if home == "offboarding" else account["id"]
        _sql(world["stream"], change, (target,))
        out = _read(world, ws, JUNE, days=3)
        assert out["repeating"] == []
        # Its story is still a row in the ledger, and still shown.
        assert len(out["planned"]) == 1

    @pytest.mark.parametrize(
        "held, change",
        [
            ("paused", "UPDATE workspaces SET is_paused = true WHERE id = %s"),
            ("suspended", "UPDATE workspaces SET state = 'suspended' WHERE id = %s"),
        ],
        ids=["its workspace paused", "its workspace suspended"],
    )
    def test_a_series_whose_workspace_is_not_posting_projects_nothing(
        self, world, held, change
    ):
        """The miss sweep would end each occurrence as `paused`, and the
        cadence's predicted slots are hidden then too. The series comes back
        when the workspace posts again."""
        ws = _workspace(world, f"series-{held}", tz=NY)
        account = _account(world, ws, f"held-{held}")
        _daily_story(world, ws, account, f"held-{held}", JUNE, 9)
        assert _read(world, ws, JUNE, days=3)["repeating"], (
            "positive control: it is projected while the workspace posts"
        )
        _sql(world["stream"], change, (ws["ws"],))
        out = _read(world, ws, JUNE, days=3)
        assert out["repeating"] == []
        assert len(out["planned"]) == 1, "its story is still shown"
        _sql(
            world["stream"],
            "UPDATE workspaces SET is_paused = false, state = 'active' WHERE id = %s",
            (ws["ws"],),
        )
        assert _read(world, ws, JUNE, days=3)["repeating"], "and it comes back"

    def test_a_series_parked_for_review_projects_nothing(self, world):
        """Its next days hang on how the review ends: given up later, the
        trigger makes the next occurrence from the parked slot, born late and
        missed. A series beside it still projects."""
        ws = _workspace(world, "series-parked", tz=NY)
        account = _account(world, ws, "parked")
        _daily_story(world, ws, account, "parked", JUNE, 9, state="review_required")
        beside = _daily_story(world, ws, account, "beside", JUNE, 10)
        out = _read(world, ws, JUNE, days=3)
        assert _projected_from(out["repeating"]) == {beside}

    def test_a_time_that_has_come_is_not_projected(self, world):
        """A live story past its slot, waiting on a person, projects from now:
        a time that has come is the clock's."""
        ws = _workspace(world, "series-late", tz=NY)
        account = _account(world, ws, "late")
        now = _now(world)
        slot = (now - timedelta(days=2)).replace(second=0, microsecond=0)
        wall = slot.astimezone(ZoneInfo(NY)).time()
        _story(
            world, ws, account, "late", slot, state="awaiting_approval", **_daily(wall)
        )
        today = now.astimezone(ZoneInfo(NY)).date()
        slots = _slots(_read(world, ws, today - timedelta(days=3), days=6)["repeating"])
        assert slots, "positive control: the days after now hold occurrences"
        assert all(slot > now for slot in slots)
        # the first is the series' next, within a day of now (an hour more on a clock change)
        assert slots[0] - now <= timedelta(days=1, hours=1)

    def test_a_list_past_its_limit_keeps_its_soonest_and_says_it_was_cut(
        self, world, series
    ):
        first = date(2031, 6, 1)
        cut = _read(world, series["ws"], first, days=30, repeating_limit=2)
        assert _slots(cut["repeating"]) == self._mondays_and_thursdays(first, 30)[:2]
        assert cut["repeating_truncated"]


# --- the bounds ---------------------------------------------------------------------------------


class TestTheBounds:
    @pytest.mark.parametrize(
        "behind",
        [timedelta(hours=20), timedelta(days=40)],
        ids=["a cursor just before the range", "a cursor forty days behind"],
    )
    def test_the_widest_range_at_the_most_posts_a_day_is_whole(self, world, behind):
        """The step bound cuts no walk: the widest range the read takes, at the
        schema's fifty posts a day, comes back whole."""
        busiest = (NY, 0, 0, 50)
        ws = _workspace(world, "widest", **_schedule(busiest))
        first, days = date(2031, 6, 1), vocabulary.RANGE_MAX_DAYS
        cursor = _next_slot(world, _midnight(first, NY) - behind, busiest)
        account = _account(world, ws, "busiest", cursor=cursor)
        walk = _clock_walk(world, cursor, busiest, first, days)
        assert len(walk) == days * 50
        out = _read(world, ws, first, days=days)
        assert _slots(out["predicted"], account) == walk
        assert not out["predicted_truncated"]

    def test_a_list_past_its_limit_keeps_its_soonest_and_says_it_was_cut(self, world):
        ws = _workspace(world, "limits", **_schedule(FOUR_A_DAY))
        account = _account(
            world, ws, "limited", cursor=_next_slot(world, EVE, FOUR_A_DAY)
        )
        first = _story(world, ws, account, "first", _at(JUNE, 10, 30, NY))
        _story(world, ws, account, "second", _at(JUNE, 16, 30, NY))
        walk = _clock_walk(world, account["cursor"], FOUR_A_DAY, JUNE, days=2)
        assert len(walk) == 8

        cut = _read(world, ws, JUNE, days=2, planned_limit=1, predicted_limit=5)
        assert [str(row["id"]) for row in cut["planned"]] == [first]
        assert cut["planned_truncated"]
        assert _slots(cut["predicted"]) == walk[:5] and cut["predicted_truncated"]
