"""Phase 3 gate: a planned story is served on time or missed out loud (089;
#1413, plan PR #1414, `03_serve-and-misses.md`).

A planned story (`origin = 'planned'`) is served by the prompt sweep, not
minted and prompted in one transaction as a cadence story is, so the only
things that may end one unserved are the miss leg, which says why, and the
reaper's cancel leg, for a cancel its person asked for. Driven here against
the replayed advertised stream, every sweep as `svc_worker` with no tenant,
exactly as the worker's prompt sweeper runs it:

- served on time, its card naming who scheduled it, and never while flagged;
- each miss reason ends the story `expired` with its reason and a notice in
  every bound chat, and a miss nobody can hear is counted, not dropped;
- a pause that ends inside the late window serves the story late, and says so;
- the two doors partition the due planned rows: served, missed, or waiting out
  a pause inside the window, and nothing else;
- the reaper's slot expiry leaves a planned row to the miss door;
- removing a destination tells the chats about its planned stories not yet
  served, once, and about nothing else.

The world is module-scoped (one replay); each test seeds its own workspace and
asserts on its own rows, since the sweeps are estate-wide by design.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import uuid

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.services.target import prompts, provisioning, unit_of_work
from src.services.target.work_loop import WorkerConfig
from tests.scripts.conftest import (
    _scratch,
    as_user,
    in_tenant,
    ingress_engine,
    replay_advertised_stream,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

LATE = WorkerConfig().planned_late_seconds  # the worker's own window
BLOCKING_LOCKS = ("reject", "unsupported", "hold", "seasonal")


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"owner": dsn}
    finally:
        gen.close()


def _exec(world, sql, params=None, fetch=False):
    conn = psycopg2.connect(world["owner"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(sql, params)
            if fetch:
                return cur.fetchall()
            return cur.rowcount
    finally:
        conn.close()


def _workspace(world, name, *, bound=True, paused=False, tz="America/New_York"):
    """A workspace, its owner (who has a Telegram display name, the name a
    shared chat sees), and one active push binding unless *bound* is False."""
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
            user = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_identities (user_id, provider, external_id,"
                " display_name) VALUES (%s, 'telegram', %s, %s)",
                (user, f"tg-{uuid.uuid4()}", f"Dana {name}"),
            )
            cur.execute(
                "INSERT INTO workspaces (name, tz, is_paused) VALUES (%s, %s, %s)"
                " RETURNING id",
                (name, tz, paused),
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
            binding = None
            if bound:
                binding = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO channel_bindings (id, workspace_id, channel,"
                    " external_ref) VALUES (%s, %s, 'telegram_group', %s)",
                    (binding, ws, f"-100{uuid.uuid4().int % 10**9}"),
                )
        conn.commit()
    finally:
        conn.close()
    return {
        "ws": str(ws),
        "user": str(user),
        "src": str(src),
        "binding": binding,
        "name": f"Dana {name}",
    }


def _story(
    world,
    w,
    *,
    origin="planned",
    slot_in_s=-5,
    media_state="available",
    account_state="active",
    flagged=False,
    lock=None,
    state="scheduled",
    scheduled_by=True,
    on=None,
):
    """One story on its own item and, unless *on* names another story whose
    account it shares, its own account, so no key or lock of another test's
    story can reach it. *lock* is ``(kind, scope, expires_in_s)``: scope
    ``'workspace'`` or ``'account'`` (only a `recent` lock may be the
    account's: ``ck_locks_recent_scope``); an ``expires_in_s``
    of None is a permanent lock."""
    tag = uuid.uuid4().hex[:10]
    acct_tag = on["acct_tag"] if on else tag
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            if on:
                acct = on["acct"]
            else:
                cur.execute(
                    "INSERT INTO ig_accounts (workspace_id, provider_account_ref,"
                    " handle) VALUES (%s, %s, %s) RETURNING id",
                    (w["ws"], f"acct-{tag}", f"h_{tag}"),
                )
                acct = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO media_items (workspace_id, source_id, content_hash,"
                " file_name, media_kind, provider_file_ref)"
                " VALUES (%s, %s, %s, %s, 'image', %s) RETURNING id",
                (w["ws"], w["src"], f"hash-{tag}", f"drop-{tag}.jpg", f"ref-{tag}"),
            )
            media = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
                " provider_account_ref, approval_mode, schedule_slot_at, state,"
                " origin, scheduled_by_user_id, cancel_requested)"
                " VALUES (%s, %s, %s, %s, 'manual',"
                "         now() + make_interval(secs => %s), %s, %s, %s, %s)"
                " RETURNING id",
                (
                    w["ws"],
                    acct,
                    media,
                    f"acct-{acct_tag}",
                    slot_in_s,
                    state,
                    origin,
                    w["user"] if (scheduled_by and origin == "planned") else None,
                    flagged,
                ),
            )
            intent = cur.fetchone()[0]
            if media_state != "available":
                cur.execute(
                    "UPDATE media_items SET state = %s WHERE id = %s",
                    (media_state, media),
                )
            if account_state != "active":
                cur.execute(
                    "UPDATE ig_accounts SET state = %s WHERE id = %s",
                    (account_state, acct),
                )
            if lock is not None:
                kind, scope, expires_in_s = lock
                # `ck_locks_recent_scope`: a `recent` lock is the account's,
                # every other kind the workspace's.
                scoped = {"workspace": None, "account": acct}[scope]
                cur.execute(
                    "INSERT INTO post_locks (workspace_id, media_item_id,"
                    " ig_account_id, kind, expires_at)"
                    " VALUES (%s, %s, %s, %s, CASE WHEN %s::int IS NULL THEN NULL"
                    "         ELSE now() + make_interval(secs => %s::int) END)",
                    (w["ws"], media, scoped, kind, expires_in_s, expires_in_s),
                )
        conn.commit()
    finally:
        conn.close()
    return {
        "intent": str(intent),
        "acct": str(acct),
        "acct_tag": acct_tag,
        "media": str(media),
        "tag": tag,
    }


def _as_worker(world, sql, params=None):
    """One read straight through a door, connected as `svc_worker`."""
    conn = psycopg2.connect(as_user(world["owner"], "svc_worker"))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == "svc_worker"
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _worker_sweep(world, leg, *, late=LATE):
    """One leg of the prompt sweeper exactly as `worker.PromptSweeper` runs it:
    its own transaction, `svc_worker`, no tenant, the system actor."""

    async def go():
        async with ingress_engine(as_user(world["owner"], "svc_worker")) as engine:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                async with session.begin():
                    who = (await session.execute(text("SELECT current_user"))).scalar()
                    assert who == "svc_worker", who
                    await unit_of_work.apply_gucs(
                        session, tenant_id="", actor_kind="system"
                    )
                    return await leg(session, limit=500, late_seconds=late)

    return asyncio.run(go())


def _serve(world, *, late=LATE):
    return _worker_sweep(world, prompts.sweep_due_prompts, late=late)


def _miss(world, *, late=LATE):
    return _worker_sweep(world, prompts.sweep_planned_misses, late=late)


def _pass(world, *, late=LATE):
    """One beat of the prompt sweeper: the serve leg, then the miss leg."""
    return _serve(world, late=late), _miss(world, late=late)


def _reap(world):
    """One `reap_expired` run as the worker's registry runs it, as `svc_worker`."""
    from src.services.target.work_loop import WorkerDeps, build_registry

    async def go():
        async with ingress_engine(as_user(world["owner"], "svc_worker")) as engine:
            registry = build_registry(WorkerDeps(engine=engine, config=WorkerConfig()))
            async with engine.begin() as conn:
                await unit_of_work.apply_gucs(conn, tenant_id="", actor_kind="system")
                await registry["reap_expired"](conn, {"kind": "reap_expired"})

    asyncio.run(go())


def _row(world, intent):
    (state, last_error, flagged) = _exec(
        world,
        "SELECT state, last_error, cancel_requested FROM post_intents WHERE id = %s",
        (intent,),
        fetch=True,
    )[0]
    if isinstance(last_error, str):
        last_error = json.loads(last_error)
    return {"state": state, "last_error": last_error, "flagged": flagged}


def _outbox(world, intent, kind):
    rows = _exec(
        world,
        "SELECT binding_id::text, payload FROM channel_outbox"
        " WHERE intent_id = %s AND kind = %s ORDER BY created_at",
        (intent, kind),
        fetch=True,
    )
    return [
        (binding, payload if isinstance(payload, dict) else json.loads(payload))
        for binding, payload in rows
    ]


def _audited(world, intent):
    return _exec(
        world,
        "SELECT from_state, to_state, actor_kind FROM audit_events"
        " WHERE entity_id = %s ORDER BY created_at",
        (intent,),
        fetch=True,
    )


def _missed(world, w, story, reason):
    """*story* ended `expired` with *reason*, audited as the system's, and its
    notice is queued once on the workspace's binding."""
    row = _row(world, story["intent"])
    assert row["state"] == "expired", row
    assert row["last_error"] == {
        "v": 1,
        "class": "planned_missed",
        "message": reason,
    }, row
    assert ("scheduled", "expired", "system") in _audited(world, story["intent"])
    notices = _outbox(world, story["intent"], "notification")
    assert [b for b, _ in notices] == [w["binding"]], notices
    said = notices[0][1]["text"]
    assert said.startswith("🗓 Not served: "), said
    assert f"drop-{story['tag']}.jpg for @h_{story['acct_tag']}" in said, said
    assert f"by {w['name']}: {prompts.MISS_REASONS[reason]}." in said, said
    assert said.endswith("Nothing was posted."), said


class TestServedOnTime:
    def test_a_due_planned_story_is_served_and_its_card_names_who_scheduled_it(
        self, world
    ):
        w = _workspace(world, "p3-on-time")
        story = _story(world, w)
        _pass(world)
        assert _row(world, story["intent"])["state"] == "awaiting_approval"
        ((binding, card),) = _outbox(world, story["intent"], "approval_prompt")
        assert binding == w["binding"]
        assert f"🗓 Scheduled by {w['name']} · " in card["text"], card["text"]
        assert "served late" not in card["text"], card["text"]
        assert f"🗓 Scheduled by {w['name']} · " in card["caption"], card["caption"]
        assert ("scheduled", "prompt_pending", "system") in _audited(
            world, story["intent"]
        ), "the serve is audited"
        assert _outbox(world, story["intent"], "notification") == []

    def test_the_card_rendered_again_at_send_time_still_names_the_scheduler(
        self, world
    ):
        """The sender renders every card again at claim time
        (`outbox._claim_current`), so the card that actually goes out is
        `rerender_prompt`'s: it reads the origin and the scheduler too."""
        w = _workspace(world, "p3-rerender")
        story = _story(world, w)
        _serve(world)

        async def go():
            async with ingress_engine(as_user(world["owner"], "svc_worker")) as e:
                async with e.begin() as conn:
                    await unit_of_work.apply_gucs(
                        conn, tenant_id=w["ws"], actor_kind="system"
                    )
                    return await prompts.rerender_prompt(
                        conn, intent_id=story["intent"]
                    )

        card = asyncio.run(go())
        assert f"🗓 Scheduled by {w['name']} · " in card["text"], card["text"]

    def test_a_scheduler_who_is_gone_leaves_the_line_without_a_name(self, world):
        w = _workspace(world, "p3-noname")
        story = _story(world, w, scheduled_by=False)
        _serve(world)
        ((_, card),) = _outbox(world, story["intent"], "approval_prompt")
        assert "🗓 Scheduled · " in card["text"], card["text"]

    @pytest.mark.parametrize(
        "variant",
        [
            {"account_state": "reauth_required"},
            {"lock": ("skip", "workspace", None)},
            {"lock": ("recent", "account", 3600)},
            {"lock": ("reject", "workspace", -60)},
        ],
        ids=[
            "an-account-awaiting-reconnection",
            "a-skip-lock",
            "a-recent-lock",
            "an-expired-reject-lock",
        ],
    )
    def test_what_only_warns_never_blocks_the_serve(self, world, variant):
        """F7 (a): only a blocker causes a miss. An account awaiting
        reconnection is served (Posted myself works), a `skip` or `recent`
        lock never blocks a planned story, and neither does a blocking lock
        that has expired."""
        w = _workspace(world, f"p3-warn-{uuid.uuid4().hex[:6]}")
        story = _story(world, w, **variant)
        _pass(world)
        assert _row(world, story["intent"])["state"] == "awaiting_approval"
        assert _outbox(world, story["intent"], "notification") == []


class TestNeverServedWhileFlagged:
    def test_a_cancelled_planned_story_is_never_served_nor_missed_and_ends_cancelled(
        self, world
    ):
        """G2: the flag outranks everything. Not served, not a miss (its person
        already knows), and the reaper's cancel leg ends it `cancelled` — even
        long past its window."""
        w = _workspace(world, "p3-cancelled")
        on_time = _story(world, w, flagged=True)
        past_window = _story(world, w, flagged=True, slot_in_s=-2 * LATE)
        _pass(world)
        for story in (on_time, past_window):
            assert _row(world, story["intent"])["state"] == "scheduled"
            assert _outbox(world, story["intent"], "approval_prompt") == []
        _reap(world)
        for story in (on_time, past_window):
            assert _row(world, story["intent"])["state"] == "cancelled"
            assert _outbox(world, story["intent"], "notification") == [], (
                "a cancelled story gets no miss notice"
            )

    def test_a_cancelled_cadence_story_is_not_served_either(self, world):
        """The flag is honoured for every origin: a flagged cadence story due
        now is left for the cancel leg, not carded."""
        w = _workspace(world, "p3-cancel-cadence")
        story = _story(world, w, origin="cadence", flagged=True)
        _serve(world)
        assert _row(world, story["intent"])["state"] == "scheduled"
        assert _outbox(world, story["intent"], "approval_prompt") == []


class TestEachMissSaysWhy:
    @pytest.mark.parametrize(
        "reason, variant",
        [
            ("item_removed", {"media_state": "removed"}),
            ("item_unsupported", {"media_state": "unsupported"}),
            *[
                ("item_locked", {"lock": (kind, "workspace", None)})
                for kind in BLOCKING_LOCKS
            ],
            ("item_locked", {"lock": ("reject", "workspace", 3600)}),
            ("account_removed", {"account_state": "disabled"}),
            ("account_removed", {"account_state": "moved"}),
            ("late", {"slot_in_s": -LATE - 60}),
        ],
        ids=[
            "media-removed",
            "media-unsupported",
            *[f"{kind}-lock" for kind in BLOCKING_LOCKS],
            "a-live-timed-reject-lock",
            "account-disabled",
            "account-moved",
            "past-the-window",
        ],
    )
    def test_each_reason_ends_the_story_expired_and_tells_the_chats(
        self, world, reason, variant
    ):
        w = _workspace(world, f"p3-{reason}-{uuid.uuid4().hex[:6]}")
        story = _story(world, w, **variant)
        _pass(world)
        _missed(world, w, story, reason)
        again = _pass(world)
        assert len(_outbox(world, story["intent"], "notification")) == 1, (
            "one notice, however many beats pass",
            again,
        )

    def test_a_workspace_paused_through_the_window_is_the_reason(self, world):
        w = _workspace(world, "p3-paused", paused=True)
        story = _story(world, w, slot_in_s=-LATE - 60)
        _pass(world)
        _missed(world, w, story, "paused")

    def test_the_reasons_come_in_their_precedence(self, world):
        """One reason per story: the item first, then the lock, then the
        account, then the window."""
        w = _workspace(world, "p3-precedence", paused=True)
        story = _story(
            world,
            w,
            slot_in_s=-LATE - 60,
            media_state="removed",
            account_state="moved",
            lock=("reject", "workspace", None),
        )
        _pass(world)
        _missed(world, w, story, "item_removed")
        locked = _story(
            world,
            w,
            slot_in_s=-LATE - 60,
            account_state="moved",
            lock=("hold", "workspace", None),
        )
        _pass(world)
        _missed(world, w, locked, "item_locked")
        gone = _story(world, w, slot_in_s=-LATE - 60, account_state="moved")
        _pass(world)
        _missed(world, w, gone, "account_removed")

    def test_a_miss_nobody_can_hear_is_counted_not_dropped(self, world):
        """No push binding: the story still ends `expired` with its reason (the
        web's Queue shows it), and the sweep counts it `unheard` — never a
        quiet zero."""
        w = _workspace(world, "p3-unheard", bound=False)
        story = _story(world, w, media_state="removed")
        _, missed = _pass(world)
        row = _row(world, story["intent"])
        assert row["state"] == "expired"
        assert row["last_error"]["message"] == "item_removed"
        assert _outbox(world, story["intent"], "notification") == []
        assert missed["unheard"] >= 1 and missed["missed"] >= missed["unheard"]

    def test_a_story_not_yet_due_is_never_missed(self, world):
        """Unservable today is not a miss before its time: the person may yet
        fix it (reconnect, unlock)."""
        w = _workspace(world, "p3-future")
        story = _story(world, w, slot_in_s=3600, media_state="removed")
        _pass(world)
        assert _row(world, story["intent"])["state"] == "scheduled"
        assert _outbox(world, story["intent"], "notification") == []


class TestAPauseInsideTheWindowServesLate:
    def test_the_story_waits_out_the_pause_and_is_served_late_when_it_ends(self, world):
        """F9 (i): paused at its time, the story is neither served nor missed;
        the pause ends inside the window and the next beat serves it, marked
        late on its card."""
        w = _workspace(world, "p3-pause-late", paused=True)
        story = _story(world, w, slot_in_s=-600)
        _pass(world)
        assert _row(world, story["intent"])["state"] == "scheduled"
        assert _outbox(world, story["intent"], "notification") == []
        _exec(
            world,
            "UPDATE workspaces SET is_paused = false WHERE id = %s",
            (w["ws"],),
        )
        _pass(world)
        assert _row(world, story["intent"])["state"] == "awaiting_approval"
        ((_, card),) = _outbox(world, story["intent"], "approval_prompt")
        assert f"🗓 Scheduled by {w['name']} · " in card["text"], card["text"]
        assert card["text"].endswith(" · served late"), card["text"]


class TestTheDoorsPartitionTheDuePlannedRows:
    """The serve door and the miss door are each other's complement: after
    one beat, every due, unflagged planned story is served, missed with the
    reason its facts give, or — only if it can be served, is inside its window
    and its workspace is paused — still waiting. Nothing is left unaccounted,
    and nothing is both."""

    MEDIA = ("available", "unsupported", "removed")
    ACCOUNTS = ("active", "reauth_required", "disabled", "moved")
    LOCKS = (None, ("reject", "workspace", None), ("skip", "workspace", None))
    PAUSED = (False, True)
    SLOTS = (-60, -LATE - 60)

    @staticmethod
    def _expected(media, account, lock, paused, slot_in_s):
        if media == "removed":
            return "item_removed"
        if media == "unsupported":
            return "item_unsupported"
        if lock is not None and lock[0] in BLOCKING_LOCKS:
            return "item_locked"
        if account not in ("active", "reauth_required"):
            return "account_removed"
        if slot_in_s > -LATE:
            return "waiting" if paused else "served"
        return "paused" if paused else "late"

    def test_every_combination_lands_in_exactly_its_bucket(self, world):
        spaces = {
            paused: _workspace(world, f"p3-grid-{int(paused)}", paused=paused)
            for paused in self.PAUSED
        }
        cells = []
        for media, account, lock, paused, slot in itertools.product(
            self.MEDIA, self.ACCOUNTS, self.LOCKS, self.PAUSED, self.SLOTS
        ):
            story = _story(
                world,
                spaces[paused],
                media_state=media,
                account_state=account,
                lock=lock,
                slot_in_s=slot,
            )
            cells.append((story, self._expected(media, account, lock, paused, slot)))
        _pass(world)
        wrong = []
        for story, expected in cells:
            row = _row(world, story["intent"])
            notices = _outbox(world, story["intent"], "notification")
            cards = _outbox(world, story["intent"], "approval_prompt")
            if expected == "served":
                got = row["state"] == "awaiting_approval" and cards and not notices
            elif expected == "waiting":
                got = row["state"] == "scheduled" and not cards and not notices
            else:
                got = (
                    row["state"] == "expired"
                    and row["last_error"]["message"] == expected
                    and len(notices) == 1
                    and not cards
                )
            if not got:
                wrong.append((expected, row, len(cards), len(notices)))
        assert not wrong, wrong


class TestTheLateWindowIsTheCallersNumber:
    def test_a_caller_that_names_no_window_is_served_no_planned_story(self, world):
        """`p_late` defaults to NULL: the one-argument call a worker still
        draining across the deploy makes resolves, serves the cadence story
        as before, and serves no planned one."""
        w = _workspace(world, "p3-null-window")
        planned = _story(world, w)
        cadence = _story(world, w, origin="cadence")
        ids = {
            r[0]
            for r in _as_worker(world, "SELECT o_id::text FROM fn_prompts_due(500)")
        }
        assert cadence["intent"] in ids
        assert planned["intent"] not in ids
        named = {
            r[0]
            for r in _as_worker(
                world,
                "SELECT o_id::text FROM fn_prompts_due(500, make_interval(secs => %s))",
                (LATE,),
            )
        }
        assert {cadence["intent"], planned["intent"]} <= named, (
            "the positive control: named, the window serves the planned story"
        )

    def test_a_null_window_lists_no_miss(self, world):
        """`fn_planned_misses` is STRICT: a missing number can never expire a
        row, even one far past any window."""
        w = _workspace(world, "p3-null-miss")
        late = _story(world, w, slot_in_s=-10 * LATE)
        rows = _as_worker(
            world, "SELECT o_id::text FROM fn_planned_misses(500, NULL::interval)"
        )
        assert rows == []
        named = _as_worker(
            world,
            "SELECT o_id::text, o_reason FROM fn_planned_misses(500,"
            " make_interval(secs => %s))",
            (LATE,),
        )
        assert (late["intent"], "late") in named, (
            "the positive control: named, the window lists the late story"
        )

    def test_the_window_is_the_callers(self, world):
        """The same story is served under a wide window and missed under a
        narrow one: the number is the worker's, not the door's."""
        w = _workspace(world, "p3-window")
        wide = _story(world, w, slot_in_s=-600)
        _serve(world, late=1200)
        assert _row(world, wide["intent"])["state"] == "awaiting_approval"
        narrow = _story(world, w, slot_in_s=-600)
        _pass(world, late=300)
        _missed(world, w, narrow, "late")


class TestTheReaperLeavesPlannedStoriesToTheMissDoor:
    def test_the_slot_expiry_ends_a_cadence_story_and_not_a_planned_one(self, world):
        """G1: the reaper's slot expiry had no grace and no voice. A past-due
        cadence story still ends there; a past-due planned story does not,
        and the miss leg ends it with its reason and a notice."""
        w = _workspace(world, "p3-reaper")
        planned = _story(world, w, slot_in_s=-LATE - 60)
        cadence = _story(world, w, origin="cadence", slot_in_s=-LATE - 60)
        _reap(world)
        assert _row(world, cadence["intent"])["state"] == "expired"
        assert _row(world, planned["intent"])["state"] == "scheduled"
        _miss(world)
        _missed(world, w, planned, "late")

    def test_a_planned_story_waiting_on_its_advance_is_not_expired(self, world):
        """`prompt_pending` is the other state the slot expiry reads. A planned
        story there has been served; the prompt sweep advances it."""
        w = _workspace(world, "p3-reaper-pending")
        story = _story(world, w, state="prompt_pending", slot_in_s=-LATE - 60)
        _reap(world)
        assert _row(world, story["intent"])["state"] == "prompt_pending"
        _serve(world)
        assert _row(world, story["intent"])["state"] == "awaiting_approval"


class TestRemovingADestinationTellsItsPlannedStories:
    def test_one_notice_for_each_planned_story_not_yet_served_and_none_else(
        self, world
    ):
        """`disable_destination` flags every live story of the account. The
        planned ones not yet served will never reach the miss door (it lists
        no flagged row), so they are told about now, in the removal's own
        transaction. A served planned story, a cadence story and one its
        person had already cancelled get no notice."""
        w = _workspace(world, "p3-removal")
        acct_story = _story(world, w, slot_in_s=3600)
        acct = acct_story["acct"]
        served = _story(world, w, on=acct_story, state="awaiting_approval")
        cadence = _story(world, w, on=acct_story, origin="cadence", slot_in_s=3600)
        cancelled_before = _story(world, w, on=acct_story, slot_in_s=7200, flagged=True)

        async def remove(session):
            return await provisioning.disable_destination(
                session, workspace_id=w["ws"], ig_account_id=acct
            )

        effects = asyncio.run(
            in_tenant(
                as_user(world["owner"], "svc_ingress"), w["ws"], w["user"], remove
            )
        )
        assert effects["intents_flagged"] == 3
        (notice,) = _outbox(world, acct_story["intent"], "notification")
        assert notice[0] == w["binding"]
        said = notice[1]["text"]
        assert said.startswith("🗓 Not served: "), said
        assert f"by {w['name']}: {prompts.MISS_REASONS['account_removed']}." in said
        for other in (served, cadence, cancelled_before):
            assert _outbox(world, other["intent"], "notification") == []
        assert _row(world, acct_story["intent"])["flagged"] is True
        _reap(world)
        assert _row(world, acct_story["intent"])["state"] == "cancelled"
        _pass(world)
        assert len(_outbox(world, acct_story["intent"], "notification")) == 1, (
            "the removal's notice is its only one"
        )
