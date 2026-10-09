"""The calendar's upcoming read (#1634), as `svc_ingress` on the replayed schema.

What only a database can say about `upcoming.upcoming`: that a predicted slot
is one the clock will mint. The clock's side is measured apart from the read:
`_clock_walk` applies `fn_next_slot` to its own answer from the account's
cursor, as the tick's cursor advance does, under settings the test names
itself. The read is right where it returns that walk's slots.

Covered: accounts in different time zones, a day the clocks change, an account
with no posting hours of its own, a planned story and predicted slots on one
day, the accounts and workspaces the clock does not post for, a cursor that is
overdue, one far behind the range, and one off the grid, another workspace's
rows, and the bounds.

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
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import upcoming, workspaces
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.scripts.conftest import (
    _dsn,
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)
from tests.scripts.test_ops_views_gate import _sql

pytestmark = [pytest.mark.integration, pytest.mark.slow]

UTC = timezone.utc
NY = "America/New_York"
TOKYO = "Asia/Tokyo"
LONDON = "Europe/London"

#: Three days with no clock change near them in any zone used here.
JUNE = date(2031, 6, 10)
#: New York's clocks go forward: 02:00 does not exist on this day.
SPRING = date(2031, 3, 9)
#: New York's clocks go back: 01:00 comes twice on this day.
AUTUMN = date(2031, 11, 2)


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """The replayed schema and three logins: the owner's, to seed; the API's
    production role, to read; and one the policies do not filter."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {
            "stream": stream,
            "ingress": as_user(db, "svc_ingress"),
            "bypass": _dsn(db.rsplit("/", 1)[-1]),
        }
    finally:
        gen.close()


# --- seeding ------------------------------------------------------------------


def _workspace(world, name: str, **settings) -> dict:
    """A workspace of its own for one test, with *settings* as its columns. Its
    chain's account has no cursor, so the clock does not post for it."""
    conn = psycopg2.connect(world["stream"])
    try:
        ids = seed_workspace_chain(conn, f"upcoming-{name}")
        if settings:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE workspaces SET {', '.join(f'{c} = %s' for c in settings)}"
                    " WHERE id = %s",
                    (*settings.values(), ids["ws"]),
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
    with *origin* (088 fixes it at birth). Returns its id."""
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


# --- the clock's side, measured apart from the read ------------------------------


def _next_slot(world, after, settings):
    """`fn_next_slot` for *after* under *settings* (zone, start hour, end hour,
    posts per day): a slot of that grid, to stand a cursor on."""
    ((slot,),) = _sql(
        world["stream"],
        "SELECT fn_next_slot(%s, %s, %s, %s, %s)",
        (after, *settings),
        actor=False,
    )
    return slot


def _clock_walk(world, cursor, settings, lo, hi) -> list:
    """The slots the clock mints from *cursor* under *settings*, those inside
    ``[lo, hi)``: `fn_next_slot` applied to its own answer, as the tick's
    cursor advance is. It walks from the cursor itself however far back that
    is, and uses nothing of the read's."""
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


def _midnight(day: date, zone: str) -> datetime:
    """Midnight of *day* in *zone* as an instant, worked out here rather than
    asked of the database."""
    return datetime.combine(day, time(0), tzinfo=ZoneInfo(zone))


def _at(day: date, hour: int, minute: int, zone: str) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(zone))


def _now(world) -> datetime:
    ((now,),) = _sql(world["stream"], "SELECT clock_timestamp()", actor=False)
    return now


# --- the read, as the API runs it -------------------------------------------------


def _as_ingress(world, ws: dict, fn, **kwargs):
    """*fn* as the web's member read runs it: `svc_ingress`, the workspace as
    the tenant, a person on the web as the actor."""

    async def main():
        engine = create_async_engine(asyncpg_url(world["ingress"]), poolclass=NullPool)
        try:
            uow = unit_of_work(
                engine,
                str(ws["ws"]),
                actor_kind="user",
                actor_user_id=str(ws["user"]),
                channel="web",
            )
            async with uow.begin() as session:
                return await fn(session, workspace_id=str(ws["ws"]), **kwargs)
        finally:
            await engine.dispose()

    return asyncio.run(main())


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


def _account_ids(rows) -> set[str]:
    return {str(row["ig_account_id"]) for row in rows}


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
        hours = {
            "posts_per_day": 4,
            "posting_hours_start": 9,
            "posting_hours_end": 21,
        }
        ws = _workspace(world, "zones", tz=NY, **hours)
        eve = datetime(2031, 6, 9, 1, tzinfo=UTC)
        home = _account(
            world, ws, "home", cursor=_next_slot(world, eve, (NY, 9, 21, 4))
        )
        tokyo = _account(
            world,
            ws,
            "tokyo",
            tz=TOKYO,
            cursor=_next_slot(world, eve, (TOKYO, 9, 21, 4)),
        )
        out = _read(world, ws, JUNE, days=3)
        lo, hi = _midnight(JUNE, NY), _midnight(JUNE + timedelta(days=3), NY)

        for account, zone in ((home, NY), (tokyo, TOKYO)):
            slots = _slots(out["predicted"], account)
            assert slots == _clock_walk(
                world, account["cursor"], (zone, 9, 21, 4), lo, hi
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

    def test_a_predicted_row_says_it_is_one_and_names_its_account(self, world):
        ws = _workspace(world, "labels", tz=NY)
        eve = datetime(2031, 6, 9, 1, tzinfo=UTC)
        account = _account(
            world, ws, "labelled", cursor=_next_slot(world, eve, (NY, 14, 2, 3))
        )
        rows = _read(world, ws, JUNE)["predicted"]
        assert rows
        for row in rows:
            assert {**row, "ig_account_id": str(row["ig_account_id"])} == {
                "kind": "predicted",
                "schedule_slot_at": row["schedule_slot_at"],
                "day": "2031-06-10",
                "tz": NY,
                "ig_account_id": account["id"],
                "account_handle": "labelled",
                "account_display_name": "Labelled",
            }


# --- a day the clocks change -----------------------------------------------------------


class TestADayTheClocksChange:
    """An account that posts on every hour of the day, so a missing hour and a
    repeated one each show."""

    HOURLY = (NY, 0, 0, 24)

    def _hourly(self, world, name: str, day: date) -> tuple[dict, dict]:
        ws = _workspace(
            world,
            name,
            tz=NY,
            posts_per_day=24,
            posting_hours_start=0,
            posting_hours_end=0,
        )
        eve = _at(day - timedelta(days=1), 12, 0, NY)
        account = _account(
            world, ws, "hourly", cursor=_next_slot(world, eve, self.HOURLY)
        )
        return ws, account

    def test_the_hour_that_does_not_exist_holds_no_slot(self, world):
        ws, account = self._hourly(world, "spring", SPRING)
        out = _read(world, ws, SPRING)
        slots = _slots(out["predicted"])
        assert slots == _clock_walk(
            world,
            account["cursor"],
            self.HOURLY,
            _midnight(SPRING, NY),
            _midnight(SPRING + timedelta(days=1), NY),
        )
        # 23 hours in the day, and a slot on each: none at 02:00.
        assert _walls(slots, NY) == [f"{h:02d}:00" for h in range(24) if h != 2]
        _assert_days_are_dates_in(out["predicted"], NY)

    def test_the_hour_that_comes_twice_holds_one_slot(self, world):
        ws, account = self._hourly(world, "autumn", AUTUMN)
        out = _read(world, ws, AUTUMN)
        slots = _slots(out["predicted"])
        assert slots == _clock_walk(
            world,
            account["cursor"],
            self.HOURLY,
            _midnight(AUTUMN, NY),
            _midnight(AUTUMN + timedelta(days=1), NY),
        )
        assert _walls(slots, NY) == [f"{h:02d}:00" for h in range(24)]
        _assert_days_are_dates_in(out["predicted"], NY)


# --- an account with no posting hours of its own ------------------------------------------


class TestAnAccountWithNoPostingHours:
    def test_what_an_account_does_not_set_is_the_workspaces(self, world):
        """The workspace posts three times across 22:00 to 04:00. One account
        sets nothing, one only its posts per day, one only its hours."""
        ws = _workspace(
            world,
            "inherit",
            tz=NY,
            posts_per_day=3,
            posting_hours_start=22,
            posting_hours_end=4,
        )
        eve = datetime(2031, 6, 9, 1, tzinfo=UTC)
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
                    cursor=_next_slot(world, eve, settings),
                    **overrides,
                ),
                settings,
                walls,
            )
            for name, overrides, settings, walls in cases
        ]
        out = _read(world, ws, JUNE, days=2)
        lo, hi = _midnight(JUNE, NY), _midnight(JUNE + timedelta(days=2), NY)
        for account, settings, walls in accounts:
            slots = _slots(out["predicted"], account)
            assert slots == _clock_walk(world, account["cursor"], settings, lo, hi)
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
        ws = _workspace(
            world,
            "one-day",
            tz=NY,
            posts_per_day=4,
            posting_hours_start=9,
            posting_hours_end=21,
        )
        eve = datetime(2031, 6, 10, 1, tzinfo=UTC)
        account = _account(
            world, ws, "both", cursor=_next_slot(world, eve, (NY, 9, 21, 4))
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
        slots = _slots(out["predicted"])
        assert slots == _clock_walk(
            world,
            day["account"]["cursor"],
            (NY, 9, 21, 4),
            _midnight(self.DAY, NY),
            _midnight(self.DAY + timedelta(days=1), NY),
        )
        assert _walls(slots, NY) == ["09:00", "12:00", "15:00", "18:00"]
        # A planned story takes no slot: noon holds the story and the slot.
        assert _at(self.DAY, 12, 0, NY) in slots
        assert {row["day"] for row in out["planned"] + out["predicted"]} == {
            "2031-06-11"
        }

    def test_a_planned_row_is_the_intents_reads_row_and_its_day(self, world, day):
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
            assert row["origin"] == "planned" and row["state"] == "scheduled"
            assert {k: v for k, v in row.items() if k != "day"} == intents[
                str(row["id"])
            ]

    def test_a_predicted_row_carries_no_state_and_the_keys_the_two_share(
        self, world, day
    ):
        out = _read(world, day["ws"], self.DAY)
        assert out["predicted"]
        for row in out["predicted"]:
            assert row["kind"] == "predicted" and "state" not in row
            assert set(row) - {"kind"} <= set(out["planned"][0])


# --- who has no predicted slot ------------------------------------------------------------------


class TestWhoHasNoPredictedSlot:
    def test_only_an_account_the_clock_posts_for_is_projected(self, world):
        ws = _workspace(world, "unserved", tz=NY)
        cursor = _next_slot(world, datetime(2031, 6, 9, 1, tzinfo=UTC), (NY, 14, 2, 3))
        served = _account(world, ws, "served", cursor=cursor)
        _account(world, ws, "never-scheduled")
        _account(world, ws, "reauth", cursor=cursor, state="reauth_required")
        _account(world, ws, "disabled", cursor=cursor, state="disabled")
        rows = _read(world, ws, JUNE)["predicted"]
        assert _account_ids(rows) == {served["id"]}

    @pytest.mark.parametrize(
        "name, stop",
        [
            ("paused", "is_paused = true"),
            ("suspended", "state = 'suspended'"),
            ("offboarding", "state = 'offboarding'"),
        ],
    )
    def test_a_workspace_the_clock_does_not_serve_has_none(self, world, name, stop):
        ws = _workspace(world, name, tz=NY)
        cursor = _next_slot(world, datetime(2031, 6, 9, 1, tzinfo=UTC), (NY, 14, 2, 3))
        account = _account(world, ws, f"acct-{name}", cursor=cursor)
        story = _story(world, ws, account, f"story-{name}", _at(JUNE, 10, 30, NY))
        assert _read(world, ws, JUNE)["predicted"], (
            "positive control: it is projected while the clock serves it"
        )
        _sql(
            world["stream"], f"UPDATE workspaces SET {stop} WHERE id = %s", (ws["ws"],)
        )
        out = _read(world, ws, JUNE)
        assert out["predicted"] == []
        # Its planned story is still a row in the ledger, and still shown.
        assert [str(row["id"]) for row in out["planned"]] == [story]


# --- cursors: overdue, far behind, off the grid ----------------------------------------------


class TestASlotWhoseTimeHasCome:
    def test_an_overdue_cursors_past_slots_are_not_predicted(self, world):
        """The cursor is more than a day overdue, so the clock's walk from it
        passes through slots already due. The read leaves those out and keeps
        the rest. The database's clock moves between the statements, so the
        read is held between the walk as of before it and as of after."""
        hourly = (NY, 0, 0, 24)
        ws = _workspace(
            world,
            "overdue",
            tz=NY,
            posts_per_day=24,
            posting_hours_start=0,
            posting_hours_end=0,
        )
        start = _now(world)
        cursor = _next_slot(world, start - timedelta(hours=30), hourly)
        account = _account(world, ws, "late", cursor=cursor)
        today = start.astimezone(ZoneInfo(NY)).date()
        first = today - timedelta(days=2)
        walk = _clock_walk(
            world,
            cursor,
            hourly,
            _midnight(first, NY),
            _midnight(first + timedelta(days=5), NY),
        )
        assert any(slot <= start for slot in walk), (
            "positive control: the walk holds slots whose time has come"
        )
        before = _now(world)
        slots = _slots(_read(world, ws, first, days=5)["predicted"], account)
        after = _now(world)
        assert slots == sorted(slots) and slots
        assert {s for s in walk if s > after} <= set(slots)
        assert set(slots) <= {s for s in walk if s > before}


#: A cursor far behind the range: (case, the workspace's zone, the account's
#: effective settings, the range's first day, its days, where the cursor
#: stands, and whether it stands on the grid or off it).
FAR_BEHIND = [
    (
        "hourly, across the missing hour",
        NY,
        (NY, 0, 0, 24),
        SPRING,
        2,
        datetime(2031, 1, 20, 3, 17, 11, tzinfo=UTC),
        True,
    ),
    (
        "forty-minute steps, the day after the missing hour",
        NY,
        (NY, 0, 0, 36),
        SPRING + timedelta(days=1),
        2,
        datetime(2031, 2, 1, 3, 17, 11, tzinfo=UTC),
        False,
    ),
    (
        "forty-minute steps, two days after the missing hour",
        NY,
        (NY, 0, 0, 36),
        SPRING + timedelta(days=2),
        1,
        datetime(2031, 2, 1, 3, 17, 11, tzinfo=UTC),
        True,
    ),
    (
        "ninety-minute steps, across the missing hour",
        NY,
        (NY, 0, 0, 16),
        SPRING - timedelta(days=1),
        3,
        datetime(2031, 2, 1, 3, 17, 11, tzinfo=UTC),
        False,
    ),
    (
        "posting hours that are the missing hour",
        NY,
        (NY, 2, 3, 50),
        SPRING - timedelta(days=1),
        4,
        datetime(2031, 2, 20, 3, 17, 11, tzinfo=UTC),
        True,
    ),
    (
        "forty-minute steps, across the repeated hour",
        NY,
        (NY, 0, 0, 36),
        AUTUMN - timedelta(days=1),
        3,
        datetime(2031, 9, 28, 3, 17, 11, tzinfo=UTC),
        False,
    ),
    (
        "another zone's clock change on a New York calendar",
        NY,
        (LONDON, 0, 0, 36),
        date(2031, 3, 29),
        4,
        datetime(2031, 2, 1, 3, 17, 11, tzinfo=UTC),
        False,
    ),
    (
        "a half-hour clock change",
        "UTC",
        ("Australia/Lord_Howe", 22, 6, 13),
        date(2031, 10, 4),
        3,
        datetime(2031, 9, 1, 3, 17, 11, tzinfo=UTC),
        True,
    ),
    (
        "a year behind, no clock change",
        NY,
        (TOKYO, 9, 21, 5),
        JUNE,
        3,
        datetime(2030, 6, 1, 3, 17, 11, tzinfo=UTC),
        True,
    ),
]


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
        [case[1:] for case in FAR_BEHIND],
        ids=[case[0] for case in FAR_BEHIND],
    )
    def test_it_predicts_what_the_clock_will_mint(
        self, world, request, ws_zone, settings, first, days, behind, on_grid
    ):
        zone, start, end, ppd = settings
        ws = _workspace(world, f"behind-{request.node.callspec.index}", tz=ws_zone)
        cursor = _next_slot(world, behind, settings) if on_grid else behind
        account = _account(
            world,
            ws,
            "behind",
            cursor=cursor,
            tz=zone,
            posts_per_day=ppd,
            posting_hours_start=start,
            posting_hours_end=end,
        )
        lo = _midnight(first, ws_zone)
        hi = _midnight(first + timedelta(days=days), ws_zone)
        assert cursor < lo - timedelta(days=upcoming.WALK_LEAD_DAYS + 5), (
            "positive control: the cursor is well behind the walk's start"
        )
        walk = _clock_walk(world, cursor, settings, lo, hi)
        assert walk, "positive control: the range holds slots"
        out = _read(world, ws, first, days=days)
        assert _slots(out["predicted"], account) == walk
        assert not out["predicted_truncated"]
        _assert_days_are_dates_in(out["predicted"], ws_zone)

    def test_a_walk_begun_at_the_range_itself_would_lose_its_first_slot(
        self, world, monkeypatch
    ):
        """Why the walk starts short of the range. `fn_next_slot` answers with
        a slot after the instant it is given, never one at it, so a walk begun
        at the range's first instant holds no slot there. Every case above
        rests on the lead, so it is taken away here to show it bears load."""
        hourly = (NY, 0, 0, 24)
        ws = _workspace(
            world,
            "no-lead",
            tz=NY,
            posts_per_day=24,
            posting_hours_start=0,
            posting_hours_end=0,
        )
        cursor = _next_slot(world, datetime(2031, 5, 1, 3, 17, 11, tzinfo=UTC), hourly)
        account = _account(world, ws, "hourly", cursor=cursor)
        midnight = _midnight(JUNE, NY)
        walk = _clock_walk(
            world, cursor, hourly, midnight, _midnight(JUNE + timedelta(days=1), NY)
        )
        assert len(walk) == 24 and walk[0] == midnight
        assert _slots(_read(world, ws, JUNE)["predicted"], account) == walk

        monkeypatch.setattr(upcoming, "WALK_LEAD_DAYS", 0)
        assert _slots(_read(world, ws, JUNE)["predicted"], account) == walk[1:]


class TestACursorOffTheGrid:
    def test_the_cursor_is_itself_the_next_slot_then_the_grid_resumes(self, world):
        """A cursor set under other settings stands off today's grid. The clock
        mints it as it stands and advances from it, and so does the read."""
        settings = (NY, 9, 21, 4)
        ws = _workspace(
            world,
            "off-grid",
            tz=NY,
            posts_per_day=4,
            posting_hours_start=9,
            posting_hours_end=21,
        )
        cursor = _at(JUNE, 10, 17, NY)
        account = _account(world, ws, "moved", cursor=cursor)
        slots = _slots(_read(world, ws, JUNE, days=2)["predicted"], account)
        assert slots == _clock_walk(
            world,
            cursor,
            settings,
            _midnight(JUNE, NY),
            _midnight(JUNE + timedelta(days=2), NY),
        )
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
        seeded = {}
        eve = datetime(2031, 6, 9, 1, tzinfo=UTC)
        for name in ("mine", "theirs"):
            ws = _workspace(world, name, tz=NY)
            account = _account(
                world, ws, f"acct-{name}", cursor=_next_slot(world, eve, (NY, 14, 2, 3))
            )
            story = _story(world, ws, account, f"story-{name}", _at(JUNE, 10, 30, NY))
            seeded[name] = {"ws": ws, "account": account, "story": story}
        return seeded

    def _assert_only(self, out: dict, own: dict) -> None:
        assert [str(row["id"]) for row in out["planned"]] == [own["story"]]
        assert out["predicted"]
        assert _account_ids(out["predicted"]) == {own["account"]["id"]}

    def test_each_workspace_reads_its_own_stories_and_slots(self, world, two):
        for own in two.values():
            self._assert_only(_read(world, own["ws"], JUNE), own)

    def test_the_predicates_confine_the_read_without_the_policies(self, world, two):
        """The same read as a login the policies do not filter, with no tenant
        set: its own `workspace_id` predicates alone keep it to one workspace
        (`.claude/rules/database.md`, "Policies are not the only fence")."""
        mine = two["mine"]

        async def main():
            engine = create_async_engine(
                asyncpg_url(world["bypass"]), poolclass=NullPool
            )
            try:
                async with engine.connect() as conn:
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
                        to_date=JUNE + timedelta(days=1),
                    )
            finally:
                await engine.dispose()

        self._assert_only(asyncio.run(main()), mine)


# --- the bounds ---------------------------------------------------------------------------------


class TestTheBounds:
    @pytest.mark.parametrize(
        "behind",
        [timedelta(hours=20), timedelta(days=40)],
        ids=["a cursor just before the range", "a cursor forty days behind"],
    )
    def test_the_widest_range_at_the_most_posts_a_day_is_whole(
        self, world, request, behind
    ):
        """The step bound cuts no walk: the widest range the read takes, at the
        schema's fifty posts a day, comes back whole."""
        busiest = (NY, 0, 0, 50)
        ws = _workspace(
            world,
            f"widest-{request.node.callspec.index}",
            tz=NY,
            posts_per_day=50,
            posting_hours_start=0,
            posting_hours_end=0,
        )
        first = date(2031, 6, 1)
        days = upcoming.RANGE_MAX_DAYS
        lo, hi = _midnight(first, NY), _midnight(first + timedelta(days=days), NY)
        cursor = _next_slot(world, lo - behind, busiest)
        account = _account(world, ws, "busiest", cursor=cursor)
        walk = _clock_walk(world, cursor, busiest, lo, hi)
        assert len(walk) == days * 50
        out = _read(world, ws, first, days=days)
        assert _slots(out["predicted"], account) == walk
        assert not out["predicted_truncated"]

    def test_a_list_past_its_limit_is_cut_and_says_so(self, world):
        settings = (NY, 9, 21, 4)
        ws = _workspace(
            world,
            "limits",
            tz=NY,
            posts_per_day=4,
            posting_hours_start=9,
            posting_hours_end=21,
        )
        eve = datetime(2031, 6, 9, 1, tzinfo=UTC)
        account = _account(
            world, ws, "limited", cursor=_next_slot(world, eve, settings)
        )
        first = _story(world, ws, account, "first", _at(JUNE, 10, 30, NY))
        _story(world, ws, account, "second", _at(JUNE, 16, 30, NY))
        walk = _clock_walk(
            world,
            account["cursor"],
            settings,
            _midnight(JUNE, NY),
            _midnight(JUNE + timedelta(days=2), NY),
        )
        assert len(walk) == 8

        cut = _read(world, ws, JUNE, days=2, planned_limit=1, predicted_limit=5)
        assert [str(row["id"]) for row in cut["planned"]] == [first]
        assert cut["planned_truncated"]
        assert _slots(cut["predicted"]) == walk[:5] and cut["predicted_truncated"]

        whole = _read(world, ws, JUNE, days=2, planned_limit=2, predicted_limit=8)
        assert len(whole["planned"]) == 2 and not whole["planned_truncated"]
        assert _slots(whole["predicted"]) == walk and not whole["predicted_truncated"]

    def test_a_wider_range_than_the_maximum_is_cut_to_it(self, world):
        """A direct caller is bounded too: the read cuts the range itself."""
        settings = (NY, 9, 21, 4)
        ws = _workspace(
            world,
            "wide",
            tz=NY,
            posts_per_day=4,
            posting_hours_start=9,
            posting_hours_end=21,
        )
        eve = datetime(2031, 6, 9, 1, tzinfo=UTC)
        account = _account(world, ws, "wide", cursor=_next_slot(world, eve, settings))
        days = upcoming.RANGE_MAX_DAYS
        walk = _clock_walk(
            world,
            account["cursor"],
            settings,
            _midnight(JUNE, NY),
            _midnight(JUNE + timedelta(days=days), NY),
        )
        slots = _slots(_read(world, ws, JUNE, days=400)["predicted"], account)
        assert slots == walk and len(walk) == days * 4
