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
  override is audited.
- **Every write names the person** in `audit_events`.

Every workspace here is its own, so a zone or a lock set by one test cannot
reach another's answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import pytest
from sqlalchemy import text

from src.services.target import commands  # noqa: I001 — the port first: the registry cycle
from src.services.target import command_executors, unit_of_work, vocabulary, workspaces
from src.services.target.commands import Command, CommandRefused
from tests.scripts.conftest import (
    _scratch,
    as_user,
    ingress_engine,
    reap_as_worker,
    replay_advertised_stream,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

UTC = timezone.utc


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {
            "owner": owner,
            "ingress": as_user(owner, "svc_ingress"),
            "worker": as_user(owner, "svc_worker"),
        }
    finally:
        gen.close()


# --- seeding, as the migration actor ------------------------------------------


@contextlib.contextmanager
def _owner_cursor(world):
    conn = psycopg2.connect(world["owner"])
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            yield cur
    finally:
        conn.close()


def _sql(world, sql, params=None):
    with _owner_cursor(world) as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else None


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
        world,
        "SELECT to_jsonb(i) FROM post_intents i WHERE i.id = %s",
        (intent_id,),
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


async def _execute(dsn, ws, user, kind, args):
    async with ingress_engine(dsn) as engine:
        uow = unit_of_work.unit_of_work(
            engine, ws, actor_kind="user", actor_user_id=user, channel="cli"
        )
        async with uow.begin() as session:
            who = (await session.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_ingress", who
            return await commands.execute(
                session,
                Command(
                    kind=kind,
                    workspace_id=ws,
                    actor_user_id=user,
                    channel="cli",
                    args=args,
                ),
            )


def run(world, w, kind, *, user=None, **args):
    return asyncio.run(
        _execute(world["ingress"], w["ws"], user or w["user"], kind, args)
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


def schedule(world, w, item, local_at, **extra):
    return run(
        world,
        w,
        "schedule_item",
        ig_account_id=extra.pop("account", w["account"]),
        media_item_id=item,
        local_at=local_at,
        **extra,
    )


def _planned(world, w, days=2) -> str:
    """A planned story on a fresh item, *days* ahead: its id."""
    return schedule(world, w, _item(world, w), _local(days=days)).data["intent_id"]


# --- the wall time --------------------------------------------------------------


def _reading(world, local_at, tz):
    async def go():
        async with ingress_engine(world["ingress"]) as engine:
            async with engine.connect() as conn:
                found = (
                    (
                        await conn.execute(
                            text(command_executors._INSTANT),
                            {"local_at": local_at, "tz": tz, "horizon": 365},
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
        """`fn_safe_tz` admits a POSIX zone the IANA database does not name;
        the conversion is Postgres's own reading of it."""
        ((expected,),) = _sql(
            world,
            "SELECT CAST('2030-07-01 12:00' AS timestamp) AT TIME ZONE 'UTC+5'",
        )
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
        assert datetime.fromisoformat(data["schedule_slot_at"]) == _instant(
            local_at, "UTC"
        )
        assert data["warnings"] == [] and data["overridden"] == []
        row = _row(world, data["intent_id"])
        assert row["origin"] == "planned" and row["state"] == "scheduled"
        assert row["approval_mode"] == "manual"
        assert row["scheduled_by_user_id"] == w["user"]
        assert row["media_item_id"] == item and row["ig_account_id"] == w["account"]
        (ref,) = _sql(
            world,
            "SELECT provider_account_ref FROM ig_accounts WHERE id = %s",
            (w["account"],),
        )[0]
        assert row["provider_account_ref"] == ref

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
        assert datetime.fromisoformat(out.data["schedule_slot_at"]) == _instant(
            local_at, "America/New_York"
        )
        tokyo = _account(world, w, tz="Asia/Tokyo")
        local_at = _local(days=5, hour=9, tz="Asia/Tokyo")
        out = schedule(world, w, _item(world, w), local_at, account=tokyo)
        assert out.data["tz"] == "Asia/Tokyo"
        assert datetime.fromisoformat(out.data["schedule_slot_at"]) == _instant(
            local_at, "Asia/Tokyo"
        )

    def test_an_ambiguous_wall_time_is_its_first_occurrence(self, world):
        tz, local_at, expected = _next_ambiguous_or_skipped("ambiguous")
        w = _workspace(world, "ambiguous", tz=tz)
        out = schedule(world, w, _item(world, w), local_at)
        assert datetime.fromisoformat(out.data["schedule_slot_at"]) == expected

    def test_a_skipped_wall_time_is_refused(self, world):
        tz, local_at, _ = _next_ambiguous_or_skipped("skipped")
        w = _workspace(world, "skipped", tz=tz)
        item = _item(world, w)
        refused = refusal(
            world,
            w,
            "schedule_item",
            ig_account_id=w["account"],
            media_item_id=item,
            local_at=local_at,
        )
        assert refused.reason == "invalid_args" and "skip" in str(refused)
        assert not _sql(
            world, "SELECT 1 FROM post_intents WHERE media_item_id = %s", (item,)
        )

    def test_an_offset_is_refused_not_dropped(self, world):
        """Postgres reads a `timestamp` and silently drops an offset, so one
        inside the window would schedule the wrong instant."""
        w = _workspace(world, "offset")
        refused = refusal(
            world,
            w,
            "schedule_item",
            ig_account_id=w["account"],
            media_item_id=_item(world, w),
            local_at=_local(days=3) + "+02:00",
        )
        assert refused.reason == "invalid_args" and "offset" in str(refused)

    @pytest.mark.parametrize(
        "local_at",
        [
            "tomorrow at noon",
            "2031-01-01",  # a date is not a time
            "2031-01-01T12:00+02:00",  # an offset Postgres would silently drop
            "2031-01-01 12:00Z",
            "2031-13-01 12:00",  # the shape, not a date
            "2031-02-30 12:00",
            "",
        ],
    )
    def test_a_malformed_time_is_refused(self, world, local_at):
        w = _workspace(world, "malformed")
        assert (
            refusal(
                world,
                w,
                "schedule_item",
                ig_account_id=w["account"],
                media_item_id=_item(world, w),
                local_at=local_at,
            ).reason
            == "invalid_args"
        )

    def test_the_past_and_now_are_refused_and_the_horizon_holds(self, world):
        w = _workspace(world, "window")
        for local_at in (
            _local(days=-1),
            datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),  # this minute: gone
        ):
            refused = refusal(
                world,
                w,
                "schedule_item",
                ig_account_id=w["account"],
                media_item_id=_item(world, w),
                local_at=local_at,
            )
            assert refused.reason == "invalid_args" and "future" in str(refused)
        refused = refusal(
            world,
            w,
            "schedule_item",
            ig_account_id=w["account"],
            media_item_id=_item(world, w),
            local_at=_local(days=366),
        )
        assert refused.reason == "invalid_args" and "365 days" in str(refused)
        assert (
            schedule(world, w, _item(world, w), _local(days=364)).outcome == "executed"
        )

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
        assert (
            refusal(
                world,
                w,
                "schedule_item",
                ig_account_id=w["account"],
                media_item_id=_item(world, w),
                local_at=_local(),
                override_locks="yes",
            ).reason
            == "invalid_args"
        )

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
            assert (
                refusal(
                    world,
                    w,
                    "schedule_item",
                    ig_account_id=account,
                    media_item_id=item,
                    local_at=_local(),
                ).reason
                == "not_found"
            )
        waiting = _account(world, w, state="reauth_required")
        assert schedule(world, w, item, _local(), account=waiting).outcome == "executed"

    def test_only_an_item_of_this_workspace_is_found(self, world):
        w = _workspace(world, "items")
        other = _workspace(world, "items-other")
        for item in (_item(world, other), str(uuid.uuid4())):
            assert (
                refusal(
                    world,
                    w,
                    "schedule_item",
                    ig_account_id=w["account"],
                    media_item_id=item,
                    local_at=_local(),
                ).reason
                == "not_found"
            )

    def test_the_same_item_waiting_on_the_account_is_the_databases_refusal(self, world):
        w = _workspace(world, "twice")
        item = _item(world, w)
        first = schedule(world, w, item, _local(days=1))
        refused = refusal(
            world,
            w,
            "schedule_item",
            ig_account_id=w["account"],
            media_item_id=item,
            local_at=_local(days=2),
        )
        assert refused.reason == "illegal_transition"
        assert _row(world, first.data["intent_id"])["schedule_slot_at"] is not None
        (count,) = _sql(
            world, "SELECT count(*) FROM post_intents WHERE media_item_id = %s", (item,)
        )[0]
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
        _sql(
            world,
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state)"
            " VALUES (%s, %s, %s, 'r', 'manual', now() + interval '1 hour',"
            "         'scheduled')",
            (w["ws"], w["account"], item),
        )
        assert (
            refusal(
                world,
                w,
                "schedule_item",
                ig_account_id=w["account"],
                media_item_id=item,
                local_at=_local(),
            ).reason
            == "illegal_transition"
        )

    def test_no_bound_chat_is_said_not_refused(self, world):
        w = _workspace(world, "unbound", bound=False)
        out = schedule(world, w, _item(world, w), _local())
        assert out.outcome == "executed" and out.data["warnings"] == ["no_push_binding"]


class TestTheLockAndItemRule:
    """F7 at the moment of scheduling."""

    @pytest.mark.parametrize("state", ["removed", "unsupported"])
    def test_an_item_that_cannot_post_blocks_even_with_the_override(self, world, state):
        w = _workspace(world, f"item-{state}")
        item = _item(world, w, state=state)
        for override in (False, True):
            refused = refusal(
                world,
                w,
                "schedule_item",
                ig_account_id=w["account"],
                media_item_id=item,
                local_at=_local(),
                override_locks=override,
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
            refused = refusal(
                world,
                w,
                "schedule_item",
                ig_account_id=w["account"],
                media_item_id=item,
                local_at=_local(),
                override_locks=override,
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
        refused = refusal(
            world,
            w,
            "schedule_item",
            ig_account_id=w["account"],
            media_item_id=item,
            local_at=_local(),
        )
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
        refused = refusal(
            world,
            w,
            "schedule_item",
            ig_account_id=w["account"],
            media_item_id=item,
            local_at=_local(),
            override_locks=True,
        )
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

    def test_the_two_sets_are_the_lock_kinds_and_the_serve_doors_blockers(self, world):
        """A new lock kind must be filed as a blocker or a warning, and the
        blockers are the kinds that turn a due planned story into a miss."""
        ((definition,),) = _sql(
            world,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'ck_locks_kind'",
        )
        kinds = {k for k in definition.split("'")[1::2]}
        assert kinds == set(vocabulary.BLOCKING_LOCKS) | set(vocabulary.WARNING_LOCKS)
        for door in ("fn_prompts_due", "fn_planned_misses"):
            ((body,),) = _sql(
                world, "SELECT prosrc FROM pg_proc WHERE proname = %s", (door,)
            )
            listed = "(" + ", ".join(f"'{k}'" for k in vocabulary.BLOCKING_LOCKS) + ")"
            assert f"l.kind IN {listed}" in body, door


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
        assert datetime.fromisoformat(out.data["schedule_slot_at"]) == _instant(
            local_at, "UTC"
        )
        row = _row(world, intent)
        assert row["state"] == "scheduled"
        assert datetime.fromisoformat(row["schedule_slot_at"]) == _instant(
            local_at, "UTC"
        )
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
        assert datetime.fromisoformat(detail["from"]) == datetime.fromisoformat(before)
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
        assert (
            refusal(
                world, w, "reschedule_item", intent_id=intent, local_at=_local(days=3)
            ).reason
            == "illegal_transition"
        )
        assert _row(world, intent)["schedule_slot_at"] == before
        assert not _audit(world, intent, "rescheduled")

    def test_a_cadence_story_does_not_move(self, world):
        w = _workspace(world, "cadence-move")
        ((intent,),) = _sql(
            world,
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state)"
            " VALUES (%s, %s, %s, 'r', 'manual', now() + interval '1 day',"
            "         'scheduled') RETURNING id",
            (w["ws"], w["account"], _item(world, w)),
        )
        refused = refusal(
            world, w, "reschedule_item", intent_id=str(intent), local_at=_local(days=3)
        )
        assert refused.reason == "illegal_transition" and "cadence" in str(refused)

    def test_a_story_being_cancelled_says_so(self, world):
        w = _workspace(world, "flagged")
        intent = _planned(world, w)
        run(world, w, "cancel", intent_id=intent)
        assert (
            refusal(
                world, w, "reschedule_item", intent_id=intent, local_at=_local(days=3)
            ).reason
            == "cancelling"
        )

    def test_the_time_rules_are_schedules(self, world):
        w = _workspace(world, "move-rules")
        intent = _planned(world, w)
        for local_at in (_local(days=-1), _local(days=366), "2031-01-01T12:00+02:00"):
            assert (
                refusal(
                    world, w, "reschedule_item", intent_id=intent, local_at=local_at
                ).reason
                == "invalid_args"
            )

    def test_another_workspaces_story_is_not_found(self, world):
        w = _workspace(world, "move-mine")
        other = _workspace(world, "move-theirs")
        theirs = _planned(world, other)
        before = _row(world, theirs)["schedule_slot_at"]
        assert (
            refusal(
                world, w, "reschedule_item", intent_id=theirs, local_at=_local(days=3)
            ).reason
            == "not_found"
        )
        assert _row(world, theirs)["schedule_slot_at"] == before


class TestCancel:
    def test_every_cancel_leaves_an_audit_row_naming_the_person(self, world):
        """Cadence as much as planned: the flag was never audited (G8)."""
        w = _workspace(world, "cancel-audit")
        ((cadence,),) = _sql(
            world,
            "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
            " provider_account_ref, approval_mode, schedule_slot_at, state)"
            " VALUES (%s, %s, %s, 'r', 'manual', now(), 'awaiting_approval')"
            " RETURNING id",
            (w["ws"], w["account"], _item(world, w)),
        )
        member = _member(world, w)
        for intent, state in (
            (str(cadence), "awaiting_approval"),
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

        async def serve_door():
            async with ingress_engine(world["worker"]) as engine:
                sessions = unit_of_work.make_session_for(engine)
                async with sessions({}) as session:
                    return await due(session)

        assert uuid.UUID(intent) not in asyncio.run(serve_door())
        asyncio.run(reap_as_worker(world["owner"]))
        assert _row(world, intent)["state"] == "cancelled"
        # and the item is free for that account again
        (item,) = _sql(
            world, "SELECT media_item_id FROM post_intents WHERE id = %s", (intent,)
        )[0]
        assert schedule(world, w, str(item), _local(days=3)).outcome == "executed"


class TestFloors:
    def test_a_member_schedules_and_moves(self, world):
        w = _workspace(world, "floors")
        member = _member(world, w)
        out = run(
            world,
            w,
            "schedule_item",
            user=member,
            ig_account_id=w["account"],
            media_item_id=_item(world, w),
            local_at=_local(),
        )
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
            run(
                world,
                w,
                "schedule_item",
                user=stranger,
                ig_account_id=w["account"],
                media_item_id=_item(world, w),
                local_at=_local(),
            )
        assert err.value.reason == "not_a_member"


def _read(world, w, **kw):
    """The Queue read of *w*, as the web's route runs it."""

    async def go():
        async with ingress_engine(world["ingress"]) as engine:
            uow = unit_of_work.unit_of_work(
                engine,
                w["ws"],
                actor_kind="user",
                actor_user_id=w["user"],
                channel="web",
            )
            async with uow.begin() as session:
                return await workspaces.list_intents(
                    session, workspace_id=w["ws"], **kw
                )

    return asyncio.run(go())


def _cadence(world, w, *, hours=24, state="scheduled", last_error=None) -> str:
    ((intent,),) = _sql(
        world,
        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
        " provider_account_ref, approval_mode, schedule_slot_at, state, last_error)"
        " VALUES (%s, %s, %s, 'r', 'manual', now() + make_interval(hours => %s),"
        "         %s, %s) RETURNING id",
        (w["ws"], w["account"], _item(world, w), hours, state, last_error),
    )
    return str(intent)


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
        run(
            world,
            w,
            "schedule_item",
            user=member,
            ig_account_id=w["account"],
            media_item_id=_item(world, w),
            local_at=_local(),
        )
        (row,) = _read(world, w, origin="planned")
        assert row["scheduled_by"] == "Mo"

    def test_a_scheduler_with_no_name_is_a_teammate_never_an_address(self, world):
        w = _workspace(world, "queue-nameless")
        member = _member(world, w)
        _sql(
            world,
            "INSERT INTO user_identities (user_id, provider, external_id, display_name)"
            " VALUES (%s, 'google', %s, NULL)",
            (member, f"someone-{uuid.uuid4().hex[:6]}@example.com"),
        )
        run(
            world,
            w,
            "schedule_item",
            user=member,
            ig_account_id=w["account"],
            media_item_id=_item(world, w),
            local_at=_local(),
        )
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
        failed = json.dumps(
            {"v": 1, "class": "provider", "message": "a secret-free error"}
        )
        _cadence(world, w, hours=-2, state="failed", last_error=failed)
        missed = _planned(world, w)
        _sql(
            world,
            "UPDATE post_intents SET state = 'expired', last_error = %s WHERE id = %s",
            (
                json.dumps({"v": 1, "class": "planned_missed", "message": "late"}),
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
