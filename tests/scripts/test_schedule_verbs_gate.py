"""The two verbs that make a planned story (#1413 phase 5), EXECUTED — as
`svc_ingress`, through the real port, on the replayed target schema.

`schedule_item` creates one: this item, to this account, at a wall time in
the account's zone. `reschedule_item` moves its time in place. `cancel` ends
it, as it ends any story, and now leaves an audit row naming the person on
every story (the flag is not a state change, so the intent's trigger never
wrote one).

What each class pins, and why it is here rather than in a unit test:

- **The wall time is the database's reading of the zone.** A skipped time is
  refused, an ambiguous one is its first occurrence, and a zone only Postgres
  knows (`fn_safe_tz` admits POSIX zones) converts as the clock converts. The
  conversion is SQL, so only a real Postgres can say it is right.
- **The database refuses the duplicate.** `uq_intent_live_subject` is the
  INSERT's to raise; nothing reads first.
- **F7, the lock and item rule.** Four lock kinds and an item that cannot be
  posted refuse outright; `skip` and `recent` refuse until overridden, and the
  override is audited. The kinds, and the account states a story is served
  on, are one spelling with the serve and miss doors (pinned against them).
- **Every write names the person** in `audit_events`.
- **Each refusal says why in facts**, so a front end never parses its prose:
  what is in the way (`locked`), what is missing (`not_found`), which time
  rule broke (`invalid_args`).
- **Another workspace's ids are refused by the verbs' own SQL.** The
  policies are not the only fence (`.claude/rules/database.md`): a login
  that owns the tables reads straight through them, so the `workspace_id`
  each read names is what refuses, and only a session as that owner shows
  it. Under the policies a dropped predicate would go unseen.

Every workspace here is its own, so a zone or a lock set by one test cannot
reach another's answer.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from src.services.target import commands  # noqa: I001 — the port first: the registry cycle
from src.services.target import (
    command_executors,
    intent_ledger,
    vocabulary,
    workspaces,
)
from src.services.target.commands import Command, CommandRefused
from src.utils.datetime_utils import parse_iso_timestamp
from tests.scripts.conftest import (
    _scratch,
    as_user,
    in_tenant,
    ingress_engine,
    reap_as_worker,
    replay_advertised_stream,
    set_test_passwords,
)
from tests.scripts.test_ops_views_gate import _sql as owner_sql
from tests.scripts.test_planned_serve_gate import _miss, _owner_cursor, _serve, _worker

pytestmark = [pytest.mark.integration, pytest.mark.slow]

UTC = timezone.utc


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"owner": owner, "ingress": as_user(owner, "svc_ingress")}
    finally:
        gen.close()


# --- seeding, as the migration actor ------------------------------------------


def _sql(world, sql, params=()):
    return owner_sql(world["owner"], sql, params)


def _workspace(world, name, *, tz="UTC", bound=True):
    """A workspace, its owner (named on Telegram), one account in no zone of
    its own, one media source, and a push binding unless *bound* is False."""
    with _owner_cursor(world) as cur:
        cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
        user = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO user_identities (user_id, provider, external_id,"
            " display_name) VALUES (%s, 'telegram', %s, %s)",
            (user, f"tg-{uuid.uuid4()}", f"Ines {name}"),
        )
        cur.execute(
            "INSERT INTO workspaces (name, tz) VALUES (%s, %s) RETURNING id",
            (name, tz),
        )
        ws = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'owner')",
            (ws, user),
        )
        cur.execute(
            "INSERT INTO media_sources (workspace_id, provider, config)"
            " VALUES (%s, 'gdrive', '{\"v\": 1}') RETURNING id",
            (ws,),
        )
        src = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
            " VALUES (%s, %s) RETURNING id",
            (ws, f"acct-{name}-{uuid.uuid4().hex[:6]}"),
        )
        account = cur.fetchone()[0]
        if bound:
            cur.execute(
                "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                " VALUES (%s, 'telegram_group', %s)",
                (ws, f"-100{uuid.uuid4().int % 10**9}"),
            )
    return {
        "ws": str(ws),
        "user": str(user),
        "src": str(src),
        "account": str(account),
        "name": f"Ines {name}",
    }


def _item(world, w, *, state="available") -> str:
    ((item,),) = _sql(
        world,
        "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name,"
        " media_kind, provider_file_ref, state)"
        " VALUES (%s, %s, %s, 'f.jpg', 'image', %s, %s) RETURNING id",
        (w["ws"], w["src"], f"h-{uuid.uuid4().hex}", f"r-{uuid.uuid4().hex}", state),
    )
    return str(item)


def _account(world, w, *, state="active", tz=None) -> str:
    ((account,),) = _sql(
        world,
        "INSERT INTO ig_accounts (workspace_id, provider_account_ref, state, tz)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (w["ws"], f"acct-{uuid.uuid4().hex[:8]}", state, tz),
    )
    return str(account)


def _cadence(
    world, w, *, item=None, hours=24, state="scheduled", last_error=None
) -> str:
    """A cadence story on *item* (a fresh one unless given), *hours* ahead."""
    ((intent,),) = _sql(
        world,
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at, state, last_error)"
        " VALUES (%s, %s, %s, 'r', 'manual', now() + make_interval(hours => %s),"
        "         %s, %s) RETURNING id",
        (w["ws"], w["account"], item or _item(world, w), hours, state, last_error),
    )
    return str(intent)


def _lock(world, w, item, kind, *, account=None, expires_in=None):
    """A lock on *item*: workspace-wide unless *account* names one (only a
    `recent` lock may, `ck_locks_recent_scope`); permanent unless it
    *expires_in* (negative: already expired)."""
    _sql(
        world,
        "INSERT INTO post_locks (workspace_id, media_item_id, ig_account_id,"
        " kind, expires_at)"
        " VALUES (%s, %s, %s, %s, CASE WHEN %s::float IS NULL THEN NULL"
        "         ELSE now() + make_interval(secs => %s::float) END)",
        (w["ws"], item, account, kind, expires_in, expires_in),
    )


def _member(world, w, role="member") -> str:
    """A second person in *w*, below the owner."""
    with _owner_cursor(world) as cur:
        cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
        user = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, %s)",
            (w["ws"], user, role),
        )
    return str(user)


def _row(world, intent_id):
    ((row,),) = _sql(
        world, "SELECT to_jsonb(i) FROM post_intents i WHERE i.id = %s", (intent_id,)
    )
    return row


def _audit(world, intent_id, event):
    return _sql(
        world,
        "SELECT actor_kind, actor_user_id::text, channel, from_state, to_state,"
        "       detail FROM audit_events"
        " WHERE entity_kind = 'post_intent' AND entity_id = %s"
        "   AND detail->>'event' = %s ORDER BY id",
        (intent_id, event),
    )


# --- the port, as the production role ------------------------------------------


def _command(w, kind, user, args) -> Command:
    """*kind* as a person of *w* sends it over the CLI's channel."""
    return Command(
        kind=kind, workspace_id=w["ws"], actor_user_id=user, channel="cli", args=args
    )


def run(world, w, kind, *, user=None, **args):
    """*kind* through the real registry, as a person over the CLI's channel."""
    user = user or w["user"]
    return asyncio.run(
        in_tenant(
            world["ingress"],
            w["ws"],
            user,
            lambda session: commands.execute(session, _command(w, kind, user, args)),
            channel="cli",
        )
    )


def refusal(world, w, kind, **args) -> CommandRefused:
    with pytest.raises(CommandRefused) as err:
        run(world, w, kind, **args)
    return err.value


def _local(days=1, hour=12, minute=0, tz="UTC") -> str:
    """A wall time *days* from today in *tz*, as a person types it."""
    day = datetime.now(ZoneInfo(tz)).date() + timedelta(days=days)
    return f"{day.isoformat()} {hour:02d}:{minute:02d}"


def _instant(local_at, tz) -> datetime:
    """What *local_at* in *tz* means, by Python's reading (the first
    occurrence of an ambiguous time) — the expectation, never the code."""
    return datetime.fromisoformat(local_at).replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)


def _schedule_args(w, item, local_at, extra):
    return {
        "ig_account_id": extra.pop("account", w["account"]),
        "media_item_id": item,
        "local_at": local_at,
        **extra,
    }


def schedule(world, w, item, local_at, *, user=None, **extra):
    return run(
        world, w, "schedule_item", user=user, **_schedule_args(w, item, local_at, extra)
    )


def schedule_refused(world, w, item, local_at, **extra) -> CommandRefused:
    return refusal(
        world, w, "schedule_item", **_schedule_args(w, item, local_at, extra)
    )


def _planned(world, w, days=2) -> str:
    """A planned story on a fresh item, *days* ahead: its id."""
    return schedule(world, w, _item(world, w), _local(days=days)).data["intent_id"]


def _door(world, name) -> str:
    ((body,),) = _sql(world, "SELECT prosrc FROM pg_proc WHERE proname = %s", (name,))
    return body


# --- the wall time --------------------------------------------------------------


def _reading(world, local_at, tz):
    async def go():
        async with ingress_engine(world["ingress"]) as engine:
            async with engine.connect() as conn:
                found = (
                    (
                        await conn.execute(
                            text(command_executors._INSTANT),
                            {
                                "local_at": local_at,
                                "tz": tz,
                                "horizon": vocabulary.PLAN_HORIZON_DAYS,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                return found["at"]

    return asyncio.run(go())


class TestTheWallTimeIsTheDatabasesReading:
    """Fixed dates past the horizon, read through the conversion alone."""

    def test_an_ordinary_time_in_a_zone(self, world):
        at = _reading(world, "2030-07-01 12:00:00", "America/New_York")
        assert at == datetime(2030, 7, 1, 16, 0, tzinfo=UTC)

    def test_a_time_the_clocks_skip_has_no_instant(self, world):
        # 2030-03-10: New York's clocks jump from 02:00 to 03:00
        assert _reading(world, "2030-03-10 02:30:00", "America/New_York") is None
        # Lord Howe's jump is half an hour: 02:00 to 02:30 on 2030-10-06
        assert _reading(world, "2030-10-06 02:15:00", "Australia/Lord_Howe") is None

    def test_an_ambiguous_time_is_its_first_occurrence(self, world):
        # 2030-11-03: New York's 01:00-02:00 happens twice, EDT then EST
        assert _reading(world, "2030-11-03 01:30:00", "America/New_York") == datetime(
            2030, 11, 3, 5, 30, tzinfo=UTC
        )
        # 2030-04-07: Lord Howe's 01:30-02:00 happens twice, +11:00 then +10:30
        assert _reading(
            world, "2030-04-07 01:45:00", "Australia/Lord_Howe"
        ) == datetime(2030, 4, 6, 14, 45, tzinfo=UTC)

    def test_the_edges_of_a_jump_exist(self, world):
        """The minute before a skip and the minute it lands on are real."""
        assert _reading(world, "2030-03-10 01:59:00", "America/New_York") == datetime(
            2030, 3, 10, 6, 59, tzinfo=UTC
        )
        assert _reading(world, "2030-03-10 03:00:00", "America/New_York") == datetime(
            2030, 3, 10, 7, 0, tzinfo=UTC
        )

    def test_a_zone_only_postgres_knows_converts_as_the_clock_does(self, world):
        """`fn_safe_tz`, the CHECK on every stored zone, admits a POSIX zone
        the IANA database does not name, and the clock (`fn_next_slot`) reads
        a wall time in it as `AT TIME ZONE fn_safe_tz(tz)`: the planned
        reading is that one."""
        ((admitted, expected),) = _sql(
            world,
            "SELECT fn_safe_tz('UTC+5'),"
            " CAST('2030-07-01 12:00' AS timestamp) AT TIME ZONE fn_safe_tz('UTC+5')",
        )
        assert admitted == "UTC+5"
        assert _reading(world, "2030-07-01 12:00:00", "UTC+5") == expected


def _next_ambiguous_or_skipped(kind):
    """The first wall time within the horizon that the clocks repeat
    (*kind* "ambiguous") or skip ("skipped"), in one of three zones — a year
    always holds one of each in at least one of them."""
    now = datetime.now(UTC)
    for tz in ("America/New_York", "Europe/London", "Australia/Sydney"):
        zone = ZoneInfo(tz)
        for days in range(1, 360):
            for minute in range(0, 24 * 60, 15):
                wall = (now.astimezone(zone) + timedelta(days=days)).replace(
                    hour=minute // 60,
                    minute=minute % 60,
                    second=0,
                    microsecond=0,
                    tzinfo=None,
                )
                first = wall.replace(tzinfo=zone, fold=0)
                second = wall.replace(tzinfo=zone, fold=1)
                if first.utcoffset() == second.utcoffset():
                    continue
                exists = (
                    first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall
                )
                if (kind == "ambiguous") == exists:
                    return tz, wall.strftime("%Y-%m-%d %H:%M"), first.astimezone(UTC)
    pytest.fail(f"no {kind} wall time within the horizon")


class TestScheduleItem:
    def test_it_is_born_planned_by_the_person_and_manual(self, world):
        w = _workspace(world, "born")
        item = _item(world, w)
        local_at = _local(days=2, hour=15)
        out = schedule(world, w, item, local_at)
        assert out.outcome == "executed"
        data = out.data
        assert data["state"] == "scheduled" and data["tz"] == "UTC"
        assert data["local_at"] == f"{local_at}:00"
        assert parse_iso_timestamp(data["schedule_slot_at"]) == _instant(
            local_at, "UTC"
        )
        assert data["warnings"] == [] and data["overridden"] == []
        row = _row(world, data["intent_id"])
        assert row["origin"] == "planned" and row["state"] == "scheduled"
        assert row["approval_mode"] == "manual"
        assert row["scheduled_by_user_id"] == w["user"]
        assert row["media_item_id"] == item and row["ig_account_id"] == w["account"]
        ((ref,),) = _sql(
            world,
            "SELECT provider_account_ref FROM ig_accounts WHERE id = %s",
            (w["account"],),
        )
        assert row["provider_account_ref"] == ref

    def test_a_t_and_seconds_are_the_same_wall_time(self, world):
        w = _workspace(world, "shape")
        local_at = f"{_local(days=2, hour=15)}:30"
        out = schedule(world, w, _item(world, w), local_at.replace(" ", "T"))
        assert out.data["local_at"] == local_at
        assert parse_iso_timestamp(out.data["schedule_slot_at"]) == _instant(
            local_at, "UTC"
        )

    def test_the_conflict_arbiter_is_the_live_subject_index(self, world):
        """`schedule_item` names `uq_intent_live_subject` by its predicate. A
        terminal state the index had and the predicate did not would fail
        every schedule; the reverse would let the duplicate's read name
        nothing. So the index's own definition is read back."""
        ((indexdef,),) = _sql(
            world,
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_intent_live_subject'",
        )
        columns, predicate = indexdef.split(" WHERE ", 1)
        assert columns.endswith("(workspace_id, media_item_id, ig_account_id)")
        assert "<> ALL" in predicate, predicate
        assert set(re.findall(r"'(\w+)'", predicate)) == set(
            intent_ledger.TERMINAL_STATES
        )

    def test_the_audit_row_names_the_person(self, world):
        w = _workspace(world, "audit")
        local_at = _local(days=3)
        out = schedule(world, w, _item(world, w), local_at)
        ((actor, by, channel, from_state, to_state, detail),) = _audit(
            world, out.data["intent_id"], "scheduled"
        )
        assert (actor, by, channel) == ("user", w["user"], "cli")
        assert (from_state, to_state) == (None, "scheduled")
        assert detail == {
            "v": 1,
            "event": "scheduled",
            "at": out.data["schedule_slot_at"],
            "tz": "UTC",
            "local_at": f"{local_at}:00",
        }

    def test_the_zone_is_the_accounts_then_the_workspaces(self, world):
        w = _workspace(world, "zones", tz="America/New_York")
        local_at = _local(days=5, hour=9, tz="America/New_York")
        out = schedule(world, w, _item(world, w), local_at)
        assert out.data["tz"] == "America/New_York"
        assert parse_iso_timestamp(out.data["schedule_slot_at"]) == _instant(
            local_at, "America/New_York"
        )
        tokyo = _account(world, w, tz="Asia/Tokyo")
        local_at = _local(days=5, hour=9, tz="Asia/Tokyo")
        out = schedule(world, w, _item(world, w), local_at, account=tokyo)
        assert out.data["tz"] == "Asia/Tokyo"
        assert parse_iso_timestamp(out.data["schedule_slot_at"]) == _instant(
            local_at, "Asia/Tokyo"
        )

    def test_an_ambiguous_wall_time_is_its_first_occurrence(self, world):
        tz, local_at, expected = _next_ambiguous_or_skipped("ambiguous")
        w = _workspace(world, "ambiguous", tz=tz)
        out = schedule(world, w, _item(world, w), local_at)
        assert parse_iso_timestamp(out.data["schedule_slot_at"]) == expected

    def test_a_skipped_wall_time_is_refused(self, world):
        tz, local_at, _ = _next_ambiguous_or_skipped("skipped")
        w = _workspace(world, "skipped", tz=tz)
        item = _item(world, w)
        refused = schedule_refused(world, w, item, local_at)
        assert refused.reason == "invalid_args"
        assert refused.facts == {"at_rule": "skipped"}
        assert not _sql(
            world, "SELECT 1 FROM post_intents WHERE media_item_id = %s", (item,)
        )

    def test_an_offset_is_refused_not_dropped(self, world):
        """Postgres reads a `timestamp` and silently drops an offset, so one
        inside the window would schedule the wrong instant."""
        w = _workspace(world, "offset")
        refused = schedule_refused(world, w, _item(world, w), _local(days=3) + "+02:00")
        assert refused.reason == "invalid_args"
        assert refused.facts == {"at_rule": "shape"}

    @pytest.mark.parametrize(
        "local_at, rule",
        [
            ("tomorrow at noon", "shape"),
            ("2031-01-01", "shape"),  # a date is not a time
            ("2031-01-01T12:00+02:00", "shape"),  # an offset Postgres would drop
            ("2031-01-01 12:00Z", "shape"),
            ("2031-13-01 12:00", "not_a_date"),  # the shape, not a date
            ("2031-02-30 12:00", "not_a_date"),
            ("", None),  # no time at all: the arguments are refused by name
        ],
    )
    def test_a_malformed_time_is_refused(self, world, local_at, rule):
        w = _workspace(world, "malformed")
        refused = schedule_refused(world, w, _item(world, w), local_at)
        assert refused.reason == "invalid_args"
        assert refused.facts == ({"at_rule": rule} if rule else {})

    def test_the_past_and_now_are_refused_and_the_horizon_holds(self, world):
        w = _workspace(world, "window")
        for local_at in (
            _local(days=-1),
            datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),  # this minute: gone
        ):
            refused = schedule_refused(world, w, _item(world, w), local_at)
            assert refused.reason == "invalid_args"
            assert refused.facts == {"at_rule": "past"}
        horizon = vocabulary.PLAN_HORIZON_DAYS
        refused = schedule_refused(world, w, _item(world, w), _local(days=horizon + 1))
        assert refused.reason == "invalid_args"
        assert refused.facts == {"at_rule": "horizon"}
        out = schedule(world, w, _item(world, w), _local(days=horizon - 1))
        assert out.outcome == "executed"

    def test_ids_must_be_ids(self, world):
        w = _workspace(world, "ids")
        for args in (
            {"ig_account_id": "not-an-id", "media_item_id": _item(world, w)},
            {"ig_account_id": w["account"], "media_item_id": "not-an-id"},
            {"media_item_id": _item(world, w)},
        ):
            assert (
                refusal(world, w, "schedule_item", local_at=_local(), **args).reason
                == "invalid_args"
            )
        refused = schedule_refused(
            world, w, _item(world, w), _local(), override_locks="yes"
        )
        assert refused.reason == "invalid_args"

    def test_only_a_live_account_of_this_workspace_is_found(self, world):
        w = _workspace(world, "accounts")
        other = _workspace(world, "accounts-other")
        item = _item(world, w)
        for account in (
            _account(world, w, state="disabled"),
            _account(world, w, state="moved"),
            other["account"],  # another workspace's
            str(uuid.uuid4()),
        ):
            refused = schedule_refused(world, w, item, _local(), account=account)
            assert refused.reason == "not_found"
            assert refused.facts == {"missing": "account"}
        waiting = _account(world, w, state="reauth_required")
        assert schedule(world, w, item, _local(), account=waiting).outcome == "executed"

    def test_only_an_item_of_this_workspace_is_found(self, world):
        w = _workspace(world, "items")
        other = _workspace(world, "items-other")
        for item in (_item(world, other), str(uuid.uuid4())):
            refused = schedule_refused(world, w, item, _local())
            assert refused.reason == "not_found"
            assert refused.facts == {"missing": "item"}

    def test_the_same_item_waiting_on_the_account_is_the_databases_refusal(self, world):
        w = _workspace(world, "twice")
        item = _item(world, w)
        first = schedule(world, w, item, _local(days=1))
        refused = schedule_refused(world, w, item, _local(days=2))
        assert refused.reason == "illegal_transition"
        # the story in the way, named, so a person can find it
        assert refused.facts == {
            "existing": {
                "intent_id": first.data["intent_id"],
                "state": "scheduled",
                "origin": "planned",
                "cancel_requested": False,
            }
        }
        assert _row(world, first.data["intent_id"])["state"] == "scheduled"
        ((count,),) = _sql(
            world, "SELECT count(*) FROM post_intents WHERE media_item_id = %s", (item,)
        )
        assert count == 1
        # the same item on ANOTHER account is another story
        second = _account(world, w)
        assert (
            schedule(world, w, item, _local(days=2), account=second).outcome
            == "executed"
        )

    def test_a_cadence_story_waiting_with_the_item_refuses_it_too(self, world):
        w = _workspace(world, "cadence")
        item = _item(world, w)
        cadence = _cadence(world, w, item=item, hours=1)
        refused = schedule_refused(world, w, item, _local())
        assert refused.reason == "illegal_transition"
        assert refused.facts["existing"] == {
            "intent_id": cadence,
            "state": "scheduled",
            "origin": "cadence",
            "cancel_requested": False,
        }

    def test_the_story_in_the_way_is_the_live_one_never_an_ended_one(self, world):
        w = _workspace(world, "ended-then-live")
        item = _item(world, w)
        ended = schedule(world, w, item, _local(days=1)).data["intent_id"]
        run(world, w, "cancel", intent_id=ended)
        asyncio.run(reap_as_worker(world["owner"]))
        assert _row(world, ended)["state"] == "cancelled"
        live = schedule(world, w, item, _local(days=2)).data["intent_id"]
        refused = schedule_refused(world, w, item, _local(days=3))
        assert refused.facts["existing"]["intent_id"] == live

    def test_a_story_being_cancelled_still_holds_its_item_and_says_so(self, world):
        """A cancel is a flag the reaper honours later: until it does, the
        story still holds its item, and the refusal says it is on its way out."""
        w = _workspace(world, "recancel")
        item = _item(world, w)
        first = schedule(world, w, item, _local(days=1)).data["intent_id"]
        run(world, w, "cancel", intent_id=first)
        refused = schedule_refused(world, w, item, _local(days=2))
        assert refused.facts["existing"]["intent_id"] == first
        assert refused.facts["existing"]["cancel_requested"] is True

    def test_no_bound_chat_is_said_not_refused(self, world):
        w = _workspace(world, "unbound", bound=False)
        out = schedule(world, w, _item(world, w), _local())
        assert out.outcome == "executed"
        assert out.data["warnings"] == [vocabulary.NO_PUSH_BINDING]


class TestTheLockAndItemRule:
    """F7 at the moment of scheduling."""

    @pytest.mark.parametrize("state", ["removed", "unsupported"])
    def test_an_item_that_cannot_post_blocks_even_with_the_override(self, world, state):
        w = _workspace(world, f"item-{state}")
        item = _item(world, w, state=state)
        for override in (False, True):
            refused = schedule_refused(
                world, w, item, _local(), override_locks=override
            )
            assert refused.reason == "locked"
            assert refused.facts == {
                "in_the_way": [f"item_{state}"],
                "overridable": False,
            }

    @pytest.mark.parametrize("kind", vocabulary.BLOCKING_LOCKS)
    def test_a_blocking_lock_blocks_even_with_the_override(self, world, kind):
        w = _workspace(world, f"lock-{kind}")
        item = _item(world, w)
        _lock(world, w, item, kind)
        for override in (False, True):
            refused = schedule_refused(
                world, w, item, _local(), override_locks=override
            )
            assert refused.reason == "locked"
            assert refused.facts == {"in_the_way": [kind], "overridable": False}

    @pytest.mark.parametrize("kind", vocabulary.WARNING_LOCKS)
    def test_a_warning_lock_holds_until_overridden_and_the_override_is_audited(
        self, world, kind
    ):
        w = _workspace(world, f"warn-{kind}")
        item = _item(world, w)
        _lock(world, w, item, kind, account=w["account"] if kind == "recent" else None)
        refused = schedule_refused(world, w, item, _local())
        assert refused.reason == "locked"
        assert refused.facts == {"in_the_way": [kind], "overridable": True}
        out = schedule(world, w, item, _local(), override_locks=True)
        assert out.outcome == "executed" and out.data["overridden"] == [kind]
        ((_, by, _, _, _, detail),) = _audit(world, out.data["intent_id"], "scheduled")
        assert by == w["user"] and detail["override"] == [kind]

    def test_a_blocker_and_a_warning_are_both_named_and_not_overridable(self, world):
        w = _workspace(world, "both")
        item = _item(world, w)
        _lock(world, w, item, "skip")
        _lock(world, w, item, "reject")
        refused = schedule_refused(world, w, item, _local(), override_locks=True)
        # the blocker first, then what an override would have got past
        assert refused.facts == {"in_the_way": ["reject", "skip"], "overridable": False}

    def test_another_accounts_recent_lock_and_an_expired_lock_do_not_count(self, world):
        w = _workspace(world, "scope")
        item = _item(world, w)
        _lock(world, w, item, "recent", account=_account(world, w))
        _lock(world, w, item, "reject", expires_in=-60)
        out = schedule(world, w, item, _local())
        assert out.outcome == "executed" and out.data["overridden"] == []

    def test_a_lock_on_another_item_does_not_count(self, world):
        w = _workspace(world, "other-item")
        _lock(world, w, _item(world, w), "reject")
        assert schedule(world, w, _item(world, w), _local()).outcome == "executed"

    def test_the_rule_is_one_spelling_with_the_serve_and_miss_doors(self, world):
        """A new lock kind must be filed as a blocker or a warning; the
        blockers, the postable item and the live account states are the ones
        that decide, at the story's time, whether it is served or missed."""
        ((definition,),) = _sql(
            world,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'ck_locks_kind'",
        )
        kinds = set(definition.split("'")[1::2])
        assert kinds == set(vocabulary.BLOCKING_LOCKS) | set(vocabulary.WARNING_LOCKS)
        blockers = "(" + ", ".join(f"'{k}'" for k in vocabulary.BLOCKING_LOCKS) + ")"
        for door in ("fn_prompts_due", "fn_planned_misses"):
            assert f"l.kind IN {blockers}" in _door(world, door), door
        # the serve door's allowlist is the schedule's; the miss door writes
        # the same rule as its complement, one reason per CASE branch
        serve = _door(world, "fn_prompts_due")
        assert command_executors._LIVE_ACCOUNT in serve
        assert "m.state = 'available'" in serve


class TestRescheduleItem:
    def test_the_time_moves_in_place_and_the_move_is_audited(self, world):
        w = _workspace(world, "move")
        intent = _planned(world, w)
        before = _row(world, intent)["schedule_slot_at"]
        local_at = _local(days=4, hour=8, minute=30)
        member = _member(world, w)
        out = run(
            world,
            w,
            "reschedule_item",
            user=member,
            intent_id=intent,
            local_at=local_at,
        )
        assert out.outcome == "executed" and out.data["intent_id"] == intent
        assert parse_iso_timestamp(out.data["schedule_slot_at"]) == _instant(
            local_at, "UTC"
        )
        assert parse_iso_timestamp(out.data["previous_slot_at"]) == parse_iso_timestamp(
            before
        )
        row = _row(world, intent)
        assert row["state"] == "scheduled"
        assert parse_iso_timestamp(row["schedule_slot_at"]) == _instant(local_at, "UTC")
        ((actor, by, channel, from_state, to_state, detail),) = _audit(
            world, intent, "rescheduled"
        )
        assert (actor, by, channel, from_state, to_state) == (
            "user",
            member,
            "cli",
            "scheduled",
            "scheduled",
        )
        assert parse_iso_timestamp(detail["from"]) == parse_iso_timestamp(before)
        assert detail["to"] == out.data["schedule_slot_at"]
        assert detail["tz"] == "UTC" and detail["local_at"] == f"{local_at}:00"

    def test_a_served_story_no_longer_moves(self, world):
        w = _workspace(world, "served")
        intent = _planned(world, w)
        _sql(
            world,
            "UPDATE post_intents SET state = 'prompt_pending' WHERE id = %s",
            (intent,),
        )
        before = _row(world, intent)["schedule_slot_at"]
        refused = refusal(
            world, w, "reschedule_item", intent_id=intent, local_at=_local(days=3)
        )
        assert refused.reason == "illegal_transition"
        assert _row(world, intent)["schedule_slot_at"] == before
        assert not _audit(world, intent, "rescheduled")

    def test_a_cadence_story_does_not_move(self, world):
        w = _workspace(world, "cadence-move")
        intent = _cadence(world, w)
        refused = refusal(
            world, w, "reschedule_item", intent_id=intent, local_at=_local(days=3)
        )
        assert refused.reason == "illegal_transition" and "cadence" in str(refused)
        # the story is judged before the time: a bad time does not hide it
        refused = refusal(
            world, w, "reschedule_item", intent_id=intent, local_at="soon"
        )
        assert refused.reason == "illegal_transition"

    def test_an_id_in_any_spelling_uuid_reads_is_that_id(self, world):
        w = _workspace(world, "move-braced")
        intent = _planned(world, w)
        out = run(
            world,
            w,
            "reschedule_item",
            intent_id="{" + intent.upper() + "}",
            local_at=_local(days=3),
        )
        assert out.data["intent_id"] == intent

    def test_a_story_being_cancelled_says_so(self, world):
        w = _workspace(world, "flagged")
        intent = _planned(world, w)
        run(world, w, "cancel", intent_id=intent)
        refused = refusal(
            world, w, "reschedule_item", intent_id=intent, local_at=_local(days=3)
        )
        assert refused.reason == "cancelling"

    def test_the_time_rules_are_schedules(self, world):
        w = _workspace(world, "move-rules")
        intent = _planned(world, w)
        horizon = vocabulary.PLAN_HORIZON_DAYS
        for local_at, rule in (
            (_local(days=-1), "past"),
            (_local(days=horizon + 1), "horizon"),
            ("2031-01-01T12:00+02:00", "shape"),
        ):
            refused = refusal(
                world, w, "reschedule_item", intent_id=intent, local_at=local_at
            )
            assert refused.reason == "invalid_args"
            assert refused.facts == {"at_rule": rule}

    def test_a_malformed_or_foreign_story_id(self, world):
        w = _workspace(world, "move-mine")
        other = _workspace(world, "move-theirs")
        theirs = _planned(world, other)
        before = _row(world, theirs)["schedule_slot_at"]
        refused = refusal(
            world, w, "reschedule_item", intent_id=theirs, local_at=_local(days=3)
        )
        assert refused.reason == "not_found"
        assert _row(world, theirs)["schedule_slot_at"] == before
        # a string that is not an id is refused by name, not by a failed cast
        refused = refusal(
            world, w, "reschedule_item", intent_id="not-an-id", local_at=_local(days=3)
        )
        assert refused.reason == "invalid_args"


class TestCancel:
    def test_every_cancel_leaves_an_audit_row_naming_the_person(self, world):
        """Cadence as much as planned: the flag was never audited (G8)."""
        w = _workspace(world, "cancel-audit")
        cadence = _cadence(world, w, hours=0, state="awaiting_approval")
        member = _member(world, w)
        for intent, state in (
            (cadence, "awaiting_approval"),
            (_planned(world, w), "scheduled"),
        ):
            out = run(world, w, "cancel", user=member, intent_id=intent)
            assert out.data["cancel_requested"] is True
            ((actor, by, channel, from_state, to_state, detail),) = _audit(
                world, intent, "cancel_requested"
            )
            assert (actor, by, channel) == ("user", member, "cli")
            assert (from_state, to_state) == (state, state)
            assert detail == {"v": 1, "event": "cancel_requested"}

    def test_a_cancelled_planned_story_is_ended_and_never_served(self, world):
        w = _workspace(world, "cancel-planned")
        intent = _planned(world, w)
        run(world, w, "cancel", intent_id=intent)
        # its time comes while the flag waits for the reaper
        _sql(
            world,
            "UPDATE post_intents SET schedule_slot_at = now() - interval '1 minute'"
            " WHERE id = %s",
            (intent,),
        )

        async def due(session):
            return (
                (
                    await session.execute(
                        text("SELECT o_id FROM fn_prompts_due(500, interval '1 hour')")
                    )
                )
                .scalars()
                .all()
            )

        # the door is not empty: an unflagged story due beside it IS served
        control = _planned(world, w, days=2)
        _sql(
            world,
            "UPDATE post_intents SET schedule_slot_at = now() - interval '1 minute'"
            " WHERE id = %s",
            (control,),
        )
        listed = _worker(world, due)
        assert uuid.UUID(control) in listed
        assert uuid.UUID(intent) not in listed
        # the control goes back to the future: no due row left for later sweeps
        _sql(
            world,
            "UPDATE post_intents SET schedule_slot_at = now() + interval '2 days'"
            " WHERE id = %s",
            (control,),
        )
        asyncio.run(reap_as_worker(world["owner"]))
        assert _row(world, intent)["state"] == "cancelled"
        # and the item is free for that account again
        ((item,),) = _sql(
            world, "SELECT media_item_id FROM post_intents WHERE id = %s", (intent,)
        )
        assert schedule(world, w, str(item), _local(days=3)).outcome == "executed"


class TestFloors:
    def test_a_member_schedules_and_moves(self, world):
        w = _workspace(world, "floors")
        member = _member(world, w)
        out = schedule(world, w, _item(world, w), _local(), user=member)
        assert _row(world, out.data["intent_id"])["scheduled_by_user_id"] == member
        run(
            world,
            w,
            "reschedule_item",
            user=member,
            intent_id=out.data["intent_id"],
            local_at=_local(days=2),
        )

    def test_a_stranger_is_not_a_member(self, world):
        from src.exceptions.tenancy import TenantResolutionError

        w = _workspace(world, "floors-stranger")
        stranger = _workspace(world, "floors-elsewhere")["user"]
        with pytest.raises(TenantResolutionError) as err:
            schedule(world, w, _item(world, w), _local(), user=stranger)
        assert err.value.reason == "not_a_member"


# --- another workspace's ids, as the tables' owner ------------------------------

#: Where a write through the port leaves rows: the story, its audit trail,
#: its admission, its cards and their jobs. A refusal changes no count.
WRITTEN = ("post_intents", "audit_events", "command_dedup", "channel_outbox", "jobs")


def _counts(world) -> dict:
    """Each of those tables' row count, across every workspace: the owner's
    reads are not filtered."""
    (counts,) = _sql(
        world, "SELECT " + ", ".join(f"(SELECT count(*) FROM {t})" for t in WRITTEN)
    )
    return dict(zip(WRITTEN, counts))


def _rows_of(world, w) -> dict:
    """Every account, item and story of *w*'s, each row whole, as its text."""
    return {
        table: _sql(
            world,
            f"SELECT to_jsonb(t)::text FROM {table} t"
            " WHERE t.workspace_id = %s ORDER BY t.id",
            (w["ws"],),
        )
        for table in ("ig_accounts", "media_items", "post_intents")
    }


def _refused_as_owner(world, w, other, kind, foreign, **args) -> CommandRefused:
    """*kind*, sent as `run` sends it by a person of *w*, but connected as the
    tables' owner and naming *foreign*: rows of *other*'s, as (table, id).

    The session that is refused reads each of them first. The policies do
    not filter the owner, so the refusal is the executor's own SQL, which
    must name *w*; as `svc_ingress` the rows would be hidden whatever the
    SQL said. Nothing is written: every `WRITTEN` table keeps its count, and
    *other*'s rows are as they were, to the byte."""
    counts, held = _counts(world), _rows_of(world, other)
    seen = []

    async def go(session):
        for table, row_id in foreign:
            found = await session.execute(
                text(f"SELECT 1 FROM {table} WHERE id = :id"), {"id": row_id}
            )
            if found.first():
                seen.append((table, row_id))
        return await commands.execute(session, _command(w, kind, w["user"], args))

    with pytest.raises(CommandRefused) as err:
        asyncio.run(
            in_tenant(
                world["owner"],
                w["ws"],
                w["user"],
                go,
                channel="cli",
                # the login that ran the schema's DDL, so the tables' owner
                role="svc_migration",
            )
        )
    assert seen == foreign, "the policies hid them: this proves no more than ingress"
    assert _counts(world) == counts
    assert _rows_of(world, other) == held
    return err.value


class TestAnotherWorkspacesIdsAsTheTablesOwner:
    """Each verb run as the tables' owner, by a person of one workspace,
    naming another's rows. The session sees them, and the verb still finds
    nothing: what refuses them is the `workspace_id` its SQL names."""

    @pytest.mark.parametrize(
        "their_account, their_item",
        [(True, False), (False, True), (True, True)],
        ids=["account", "item", "both"],
    )
    def test_schedule_finds_neither_their_account_nor_their_item(
        self, world, their_account, their_item
    ):
        w = _workspace(world, "owner-schedule")
        other = _workspace(world, "owner-schedule-theirs")
        account = (other if their_account else w)["account"]
        item = _item(world, other if their_item else w)
        foreign = [("ig_accounts", account)] if their_account else []
        foreign += [("media_items", item)] if their_item else []
        refused = _refused_as_owner(
            world,
            w,
            other,
            "schedule_item",
            foreign,
            ig_account_id=account,
            media_item_id=item,
            local_at=_local(),
        )
        assert refused.reason == "not_found"
        # the account is read first, so of two foreign ids it is the one named
        assert refused.facts == {"missing": "account" if their_account else "item"}

    @pytest.mark.parametrize("origin", ["planned", "cadence"])
    @pytest.mark.parametrize("kind", ["reschedule_item", "cancel"])
    def test_their_story_is_not_found(self, world, kind, origin):
        w = _workspace(world, f"owner-{kind}-{origin}")
        other = _workspace(world, f"owner-{kind}-{origin}-theirs")
        story = (
            _planned(world, other) if origin == "planned" else _cadence(world, other)
        )
        # a time it can move to: a story the verb found would move
        when = {"local_at": _local(days=3)} if kind == "reschedule_item" else {}
        refused = _refused_as_owner(
            world, w, other, kind, [("post_intents", story)], intent_id=story, **when
        )
        assert refused.reason == "not_found"


def _read(world, w, **kw):
    """The Queue read of *w*, as the web's route runs it."""
    return asyncio.run(
        in_tenant(
            world["ingress"],
            w["ws"],
            w["user"],
            lambda session: workspaces.list_intents(
                session, workspace_id=w["ws"], **kw
            ),
        )
    )


class TestTheQueueRead:
    def test_the_origin_filter_and_the_schedulers_name(self, world):
        w = _workspace(world, "queue")
        planned = schedule(world, w, _item(world, w), _local(days=2)).data["intent_id"]
        _cadence(world, w)
        everything = _read(world, w)
        assert {r["origin"] for r in everything} == {"cadence", "planned"}
        rows = _read(world, w, origin="planned")
        assert [str(r["id"]) for r in rows] == [planned]
        assert str(rows[0]["scheduled_by_user_id"]) == w["user"]
        assert rows[0]["scheduled_by"] == w["name"]
        (cadence,) = [r for r in everything if r["origin"] == "cadence"]
        assert (
            cadence["scheduled_by"] is None and cadence["scheduled_by_user_id"] is None
        )
        assert _read(world, w, origin="planned", states=["awaiting_approval"]) == []

    def test_the_schedulers_telegram_name_comes_first(self, world):
        """The card's rule: the name the group already sees."""
        w = _workspace(world, "queue-names")
        member = _member(world, w)
        for provider, name in (("google", "Mo on Google"), ("telegram", "Mo")):
            _sql(
                world,
                "INSERT INTO user_identities (user_id, provider, external_id,"
                " display_name) VALUES (%s, %s, %s, %s)",
                (member, provider, f"{provider}-{uuid.uuid4().hex[:6]}", name),
            )
        schedule(world, w, _item(world, w), _local(), user=member)
        (row,) = _read(world, w, origin="planned")
        assert row["scheduled_by"] == "Mo"

    def test_a_scheduler_with_no_name_is_a_teammate_never_an_address(self, world):
        w = _workspace(world, "queue-nameless")
        member = _member(world, w)
        for provider, name in (("google", None), ("telegram", "")):
            _sql(
                world,
                "INSERT INTO user_identities (user_id, provider, external_id,"
                " display_name) VALUES (%s, %s, %s, %s)",
                (member, provider, f"someone-{uuid.uuid4().hex[:6]}@example.com", name),
            )
        schedule(world, w, _item(world, w), _local(), user=member)
        (row,) = _read(world, w, origin="planned")
        assert row["scheduled_by"] == "a teammate"

    def test_each_row_carries_the_zone_its_times_read_in(self, world):
        w = _workspace(world, "queue-zones", tz="Europe/Lisbon")
        schedule(world, w, _item(world, w), _local(days=1))
        tokyo = _account(world, w, tz="Asia/Tokyo")
        schedule(world, w, _item(world, w), _local(days=2), account=tokyo)
        by_account = {str(r["ig_account_id"]): r["tz"] for r in _read(world, w)}
        assert by_account == {w["account"]: "Europe/Lisbon", tokyo: "Asia/Tokyo"}

    def test_a_missed_planned_story_says_why_and_nothing_else_does(self, world):
        w = _workspace(world, "queue-misses")
        _cadence(world, w, hours=-3, state="expired")
        failed = json.dumps({"v": 1, "class": "provider", "message": "an error"})
        _cadence(world, w, hours=-2, state="failed", last_error=failed)
        missed = _planned(world, w)
        _sql(
            world,
            "UPDATE post_intents SET state = 'expired', last_error = %s WHERE id = %s",
            (
                json.dumps(
                    {"v": 1, "class": vocabulary.PLANNED_MISSED, "message": "late"}
                ),
                missed,
            ),
        )
        reasons = {str(r["id"]): r["miss_reason"] for r in _read(world, w)}
        assert reasons.pop(missed) == "late"
        assert set(reasons.values()) == {None}

    def test_newest_first_is_the_latest_not_the_oldest(self, world):
        w = _workspace(world, "queue-order")
        ids = [_cadence(world, w, hours=h) for h in (1, 2, 3)]
        assert [str(r["id"]) for r in _read(world, w, limit=2)] == ids[:2]
        assert [str(r["id"]) for r in _read(world, w, newest_first=True, limit=2)] == [
            ids[2],
            ids[1],
        ]

    def test_one_instant_orders_by_id_in_the_reads_direction(self, world):
        """Stories planned for one instant keep one order from page to page:
        the id breaks the tie, running the way the read runs."""
        w = _workspace(world, "queue-tie")
        at = _local(days=2)
        ids = sorted(
            schedule(world, w, _item(world, w), at).data["intent_id"] for _ in range(3)
        )
        assert [str(r["id"]) for r in _read(world, w, origin="planned")] == ids
        newest = _read(world, w, origin="planned", newest_first=True)
        assert [str(r["id"]) for r in newest] == ids[::-1]


class TestWhoLastMovedIt:
    def test_is_never_who_only_asked_for_a_cancel(self, world):
        """A card that answers a late tap names who last MOVED the story; a
        cancel request moves nothing (the reaper ends the story), so it must
        not name the person who asked."""
        w = _workspace(world, "settle")
        intent = _planned(world, w)
        member = _member(world, w)
        run(world, w, "cancel", user=member, intent_id=intent)
        found = asyncio.run(
            in_tenant(
                world["ingress"],
                w["ws"],
                w["user"],
                lambda session: intent_ledger.settlement(
                    session, workspace_id=w["ws"], intent_id=intent
                ),
            )
        )
        assert str(found["by_user_id"]) == w["user"], "the scheduler, not the canceller"


def _await_lock_wait(cur, xid, thread, done, deadline_s=20.0):
    """Until a session waits on the transaction *xid*, which holds the story's
    row, or fail: a race test whose loser never waited on that row proves
    nothing, and a wait on any other transaction is not this one. Read inside
    that transaction: `pg_locks` is the lock table as it stands, not a
    snapshot. A sweep that ended first never waited: say how it ended."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if not thread.is_alive():
            pytest.fail(f"the sweep ended without waiting on the row: {done}")
        cur.execute(
            "SELECT count(*) FROM pg_locks WHERE NOT granted"
            " AND locktype = 'transactionid' AND transactionid::text = %s",
            (xid,),
        )
        if cur.fetchone()[0]:
            return
        time.sleep(0.05)
    pytest.fail("the sweep never waited on the row")


def _sweep_while_changed(world, intent, sweep, *changes):
    """The race a write can win against a sweep: a transaction holds the
    story's row, as `reschedule_item` and `cancel` do under `_intent_row`'s
    lock, and changes it (*changes*, SET lists applied in order); the sweep
    (`_serve` or `_miss`) reads its door, an unlocked read that still sees
    the row as it was, then waits on the row; the change commits and the
    sweep goes on. A sweep that raised fails the test."""
    done = {}

    def go():
        try:
            done["out"] = sweep(world)
        except Exception as exc:  # reported in the test's own thread, below
            done["error"] = exc

    thread = threading.Thread(target=go)
    with _owner_cursor(world) as cur:
        for change in changes:
            cur.execute(f"UPDATE post_intents SET {change} WHERE id = %s", (intent,))
        cur.execute("SELECT pg_current_xact_id()::xid::text")
        ((xid,),) = cur.fetchall()
        thread.start()
        _await_lock_wait(cur, xid, thread, done)
    thread.join(timeout=60)
    assert not thread.is_alive(), "the sweep never finished"
    assert "error" not in done, done.get("error")
    return done["out"]


SERVED = ("prompt_pending", "awaiting_approval")


class TestAWriteAtTheDueInstant:
    """A story changed while a sweep that read it as due waits on its row is
    served or missed as it now is, never as the sweep read it. Each test's
    control, due the same way and left alone, is served or missed by the
    same sweep: a sweep that skipped everything would pass them otherwise."""

    def _due(self, world, name, ago):
        w = _workspace(world, name)
        intent = _planned(world, w)
        _sql(
            world,
            "UPDATE post_intents SET schedule_slot_at = now() - CAST(%s AS interval)"
            " WHERE id = %s",
            (ago, intent),
        )
        return intent

    def test_the_serve_sweep_leaves_a_story_moved_under_it(self, world):
        intent = self._due(world, "race-serve", "10 seconds")
        control = self._due(world, "race-serve-control", "10 seconds")
        _sweep_while_changed(
            world, intent, _serve, "schedule_slot_at = now() + interval '1 day'"
        )
        row = _row(world, intent)
        assert row["state"] == "scheduled", "served at the time it no longer holds"
        assert parse_iso_timestamp(row["schedule_slot_at"]) > datetime.now(UTC)
        assert _row(world, control)["state"] in SERVED

    def test_the_serve_sweep_leaves_a_story_flagged_under_it(self, world):
        intent = self._due(world, "race-flag", "10 seconds")
        control = self._due(world, "race-flag-control", "10 seconds")
        _sweep_while_changed(world, intent, _serve, "cancel_requested = true")
        assert _row(world, intent)["state"] == "scheduled", "served while cancelling"
        assert _row(world, control)["state"] in SERVED

    @pytest.mark.parametrize("winner_ended", SERVED)
    def test_the_serve_sweep_skips_a_story_another_sweep_served(
        self, world, winner_ended
    ):
        """The worker's sweep and `plan_slot`'s fast path can read one due row.
        The second reads it served and goes on; its transition would be
        refused (a same-state write, 061, or an edge the guard does not
        allow) and would take the whole beat with it."""
        intent = self._due(world, f"race-twice-{winner_ended}", "10 seconds")
        control = self._due(world, f"race-twice-{winner_ended}-ctl", "10 seconds")
        served = ["state = 'prompt_pending'"]
        if winner_ended == "awaiting_approval":
            served.append("state = 'awaiting_approval'")
        _sweep_while_changed(world, intent, _serve, *served)
        # served once, by the winner (the same sweep's pending leg may then
        # advance a `prompt_pending` row): no error above is the loser's skip
        assert _row(world, intent)["state"] in SERVED
        assert _row(world, control)["state"] in SERVED

    def test_the_miss_sweep_leaves_a_story_moved_under_it(self, world):
        intent = self._due(world, "race-miss", "2 hours")
        control = self._due(world, "race-miss-control", "2 hours")
        _sweep_while_changed(
            world, intent, _miss, "schedule_slot_at = now() + interval '1 day'"
        )
        row = _row(world, intent)
        assert row["state"] == "scheduled", "missed at the time it no longer holds"
        assert row["last_error"] is None
        assert _row(world, control)["state"] == "expired"
