"""The ingress dispatcher (#1183) — the `/start` door, and what it refuses.

The load-bearing test here is `test_a_non_start_update_is_NAMED_not_dropped`.
Everything else is ordinary wiring; that one is what keeps the `/start`-only
bound observable from outside the process.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
import httpx
from sqlalchemy.exc import PendingRollbackError

from src.config.settings import settings
from src.services.target import (
    rate_counters,
    callback_tokens,
    commands,
    identity,
    past_posts_activation,
    past_posts_composition,
    signup_admissions,
    telegram_dispatch,
    tenant_resolution,
    unit_of_work,
    vocabulary,
)
from src.services.target.commands import CommandRefused, CommandResult
from src.services.target.instagram_sources import PastPostsCapability
from src.services.target.start_router import GREETED, UNROUTED
from src.services.target.tenant_resolution import ResolvedTenant, TenantResolutionError
from tests.src.services.target import test_past_posts_activation as activation


def start(text="/start", uid=7, cid=99):
    return {
        "message": {
            "text": text,
            "from": {"id": uid, "username": "ada"},
            "chat": {"id": cid, "type": "private"},
        }
    }


class TestWhatItRefusesIsSaidOutLoud:
    @pytest.mark.asyncio
    async def test_a_non_start_update_is_NAMED_not_dropped(self):
        """A silent drop is indistinguishable from a dispatcher that had
        nothing to do — which is exactly what would make the bound
        undetectable. Chat-inbound commands (#854) stay unserved; this must
        SAY so."""
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(
            None,
            {
                "message": {
                    "text": "hello",
                    "from": {"id": 1},
                    "chat": {"id": 2, "type": "private"},
                }
            },
        )
        assert r.outcome == telegram_dispatch.NOT_A_START
        assert r.handled is False

    @pytest.mark.asyncio
    async def test_a_non_message_update_is_also_named(self):
        """An edit, a channel post — none is a /start, and none may vanish
        silently either. (A callback query IS served: see TestTheTap.)"""
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, {"edited_message": {"text": "x"}})
        assert r.outcome == telegram_dispatch.NOT_A_START
        assert r.handled is False

    @pytest.mark.asyncio
    async def test_it_never_raises_on_an_unservable_update(self):
        """The delivery is already ADMITTED by the time dispatch runs, so
        raising would strand it in command_dedup with nothing done."""
        d = telegram_dispatch.TelegramDispatcher()
        for update in ({}, {"message": {}}, {"message": {"text": None}}):
            r = await d(None, update)
            assert r.handled is False


class TestTheStartDoorItself:
    @pytest.mark.asyncio
    async def test_a_bare_start_is_greeted(self):
        r = await telegram_dispatch.TelegramDispatcher()(None, start("/start"))
        assert r.outcome == GREETED and r.handled is True

    @pytest.mark.asyncio
    async def test_an_unknown_prefix_is_unrouted_not_not_a_start(self):
        """Three distinct facts — not-a-start, greeted, unrouted — and
        collapsing any pair loses a signal an operator needs."""
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, start("/start zzz-nope"))
        assert r.outcome == UNROUTED
        assert r.outcome != telegram_dispatch.NOT_A_START

    def test_the_router_has_link_registered_and_room_for_inv(self):
        router = telegram_dispatch.build_router()
        assert "link-" in router._handlers

        async def inv(conn, ctx): ...

        router.register("inv-", inv)  # `inv-` joins by registration, not a new door
        assert set(router._handlers) == {"link-", "inv-", "bind-"}


class TestTheCompositionRoot:
    def test_ingress_is_wired_when_an_engine_exists(self, monkeypatch):
        from src.api import app as app_module

        sentinel = object()

        class FakeEngine:
            def connect(self):
                return sentinel

        application = app_module.create_app(engine=FakeEngine())
        assert application.state.ingress is not None
        assert application.state.ingress.connect() is sentinel

    def test_ingress_stays_None_without_an_engine(self, monkeypatch):
        """Without an engine there is nothing to connect to, and a runtime
        whose connect fails would turn the route's honest 503 into a 500
        mid-delivery."""
        from src.api import app as app_module

        monkeypatch.setattr(app_module, "_engine_from_env", lambda env: None)
        application = app_module.create_app(env={})
        assert application.state.ingress is None

    def test_without_the_key_ring_the_dispatcher_carries_none(self):
        from src.api import app as app_module

        application = app_module.create_app(engine=_ConnectOnly(), env={})
        assert application.state.past_posts_capability is None
        assert application.state.ingress.dispatch.past_posts_capability is None

    @pytest.mark.parametrize("armed, said", [(True, "on"), (False, "off")])
    async def test_startup_says_whether_taps_read_past_posts(
        self, monkeypatch, armed, said
    ):
        """One fixed line on the shared logger, which the API emits: `on` or
        `off`, and no capability or id. After a deploy it shows, before anyone
        taps, whether a tap's command will read past posts."""
        from src.api import app as app_module
        from src.utils.logger import logger as shared

        # The line below is caught on whatever `app_module.logger` is: pinned
        # to the shared logger, so a module logger bound to that name fails.
        assert app_module.logger is shared

        told = []
        monkeypatch.setattr(
            app_module.logger, "info", lambda msg, *a: told.append(msg % a)
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as client:
            app_module.create_app(
                engine=_ConnectOnly(),
                env=_armed_env() if armed else {},
                past_posts_client=client,
            )
        assert [m for m in told if m.startswith("telegram ingress:")] == [
            f"telegram ingress: instagram past posts {said}"
        ]


class _Savepoints:
    """A connection that opens savepoints and nothing else: the membership
    step observes each person in one of their own (`conn.begin_nested()`)."""

    def __init__(self):
        self.opened = 0

    @asynccontextmanager
    async def begin_nested(self):
        self.opened += 1
        yield self


#: An enabled capability, as the key ring builds it (tests may build one;
#: `src/` may not, `test_past_posts_activation`'s scan).
ARMED = PastPostsCapability(enabled=True)


class _Pooled(_Savepoints):
    """A delivery's connection as the route hands it over: savepoints, and the
    pooled DBAPI connection's own `info` dict, which outlives the checkout."""

    def __init__(self, **info):
        super().__init__()
        self.info = dict(info)


class _InvalidatedAfterTheFirstRead(_Savepoints):
    """A delivery's connection that the pool hard-invalidates mid-dispatch: its
    first `info` read answers the pooled record's dict, and every later read
    raises, as SQLAlchemy's `Connection.info` does for an invalidated
    connection inside its transaction (`_revalidate_connection`)."""

    def __init__(self, **info):
        super().__init__()
        self.record_info = dict(info)
        self.info_reads = 0

    @property
    def info(self):
        self.info_reads += 1
        if self.info_reads > 1:
            raise PendingRollbackError(
                "Can't reconnect until invalid transaction is rolled back."
            )
        return self.record_info


class _ConnectOnly:
    """An engine the composition root can wire the ingress over."""

    def connect(self):
        return object()


def _refuse(request):
    return httpx.Response(599)


def _armed_env() -> dict:
    """The API's environment where the past-posts key ring loads (the
    activation tests' ring and release)."""
    return {
        past_posts_activation.KEYS_VAR: activation.RING,
        past_posts_composition.RELEASE_VAR: activation.API_SHA,
    }


class TestGroupMessagesReachTheMembershipStep:
    """A person speaking in a group is observed (#1242); a DM, a callback
    query or a senderless message is still NOT_A_START — said, not dropped."""

    @pytest.mark.asyncio
    async def test_a_group_message_is_observed(self, monkeypatch):
        seen = {}

        async def observe(conn, *, chat_type, external_ref, telegram_user_id):
            seen.update(
                chat_type=chat_type,
                external_ref=external_ref,
                telegram_user_id=telegram_user_id,
            )
            from src.services.target.start_router import StartResult

            return StartResult(outcome="joined", handled=True)

        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        d, conn = telegram_dispatch.TelegramDispatcher(), _Savepoints()
        r = await d(
            conn,
            {
                "message": {
                    "text": "hi",
                    "from": {"id": 42},
                    "chat": {"id": -100777, "type": "supergroup"},
                }
            },
        )
        assert r.outcome == "joined"
        assert seen == {
            "chat_type": "supergroup",
            "external_ref": "-100777",
            "telegram_user_id": "42",
        }

    @pytest.mark.asyncio
    async def test_a_dm_is_still_not_a_start(self, monkeypatch):
        async def observe(conn, **kw):
            raise AssertionError("a DM must not be observed as a group message")

        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(
            None,
            {
                "message": {
                    "text": "hello",
                    "from": {"id": 1},
                    "chat": {"id": 2, "type": "private"},
                }
            },
        )
        assert r.outcome == telegram_dispatch.NOT_A_START


class TestAMigrationNoticeIsFollowedNotObserved:
    """A group that became a supergroup (#743): the notice goes to the
    binding's move, not the join path — it carries a sender, so the membership
    step would otherwise take it for speech in a chat nothing holds yet."""

    @pytest.mark.asyncio
    async def test_the_notice_reaches_the_move_and_only_the_move(self, monkeypatch):
        from src.services.target.start_router import StartResult

        seen = {}

        async def follow(conn, *, old_ref, new_ref):
            seen.update(old_ref=old_ref, new_ref=new_ref)
            return StartResult(outcome="chat_followed", handled=True)

        async def observe(conn, **kw):
            raise AssertionError("a migration notice must not be observed as speech")

        monkeypatch.setattr(telegram_dispatch.chat_migration, "follow", follow)
        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        r = await telegram_dispatch.TelegramDispatcher()(
            None,
            {
                "message": {
                    "from": {"id": 42},
                    "chat": {"id": -1009876543210, "type": "supergroup"},
                    "migrate_from_chat_id": -4012345678,
                }
            },
        )
        assert r.outcome == "chat_followed"
        assert seen == {"old_ref": "-4012345678", "new_ref": "-1009876543210"}


class TestABareStartInAGroupIsSpeechNotAGreeting:
    @pytest.mark.asyncio
    async def test_bare_start_in_a_group_is_observed_and_never_greets(
        self, monkeypatch
    ):
        seen = []

        async def observe(conn, **kw):
            seen.append(kw["telegram_user_id"])
            from src.services.target.start_router import StartResult

            return StartResult(outcome="already_member", handled=False)

        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        d, conn = telegram_dispatch.TelegramDispatcher(), _Savepoints()
        r = await d(
            conn,
            {
                "message": {
                    "text": "/start@storydump_app_bot",
                    "from": {"id": 7},
                    "chat": {"id": -5, "type": "group"},
                }
            },
        )
        assert seen == ["7"] and r.reply is None

    @pytest.mark.asyncio
    async def test_people_added_by_a_service_message_are_observed(self, monkeypatch):
        seen = []

        async def observe(conn, **kw):
            seen.append(kw["telegram_user_id"])
            from src.services.target.start_router import StartResult

            return StartResult(outcome="joined", handled=True)

        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        d, conn = telegram_dispatch.TelegramDispatcher(), _Savepoints()
        r = await d(
            conn,
            {
                "message": {
                    "from": {"id": 1},
                    "chat": {"id": -5, "type": "supergroup"},
                    "new_chat_members": [
                        {"id": 2},
                        {"id": 3, "is_bot": True},
                        {"id": 4},
                    ],
                }
            },
        )
        assert seen == ["1", "2", "4"] and r.outcome == "joined"
        assert conn.opened == 3, "one savepoint per person"

    @pytest.mark.asyncio
    async def test_a_sync_error_is_named_and_never_raised(self, monkeypatch):
        async def observe(conn, **kw):
            raise RuntimeError("door missing")

        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        d, conn = telegram_dispatch.TelegramDispatcher(), _Savepoints()
        r = await d(
            conn,
            {
                "message": {
                    "text": "hi",
                    "from": {"id": 1},
                    "chat": {"id": -5, "type": "group"},
                }
            },
        )
        assert r.outcome == telegram_dispatch.MEMBERSHIP_SYNC_FAILED and not r.handled

    @pytest.mark.asyncio
    async def test_a_failed_join_does_not_stop_the_people_after_it(self, monkeypatch):
        """Each person rolls back alone, so the people listed after a failed
        one are still observed; the failure is still the message's outcome."""
        seen = []

        async def observe(conn, **kw):
            seen.append(kw["telegram_user_id"])
            if kw["telegram_user_id"] == "1":
                raise RuntimeError("refused")
            from src.services.target.start_router import StartResult

            return StartResult(outcome="joined", handled=True)

        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        d, conn = telegram_dispatch.TelegramDispatcher(), _Savepoints()
        r = await d(
            conn,
            {
                "message": {
                    "from": {"id": 1},
                    "chat": {"id": -5, "type": "supergroup"},
                    "new_chat_members": [{"id": 2}],
                }
            },
        )
        assert seen == ["1", "2"] and conn.opened == 2
        assert r.outcome == telegram_dispatch.MEMBERSHIP_SYNC_FAILED and not r.handled


# ---------------------------------------------------------------------------
# The tap: a callback_query is served.
# ---------------------------------------------------------------------------

INTENT = str(uuid.uuid4())


def tap(action="skip", *, data=None, with_message=True, uid=7, cid=-100):
    cq = {
        "id": "q1",
        "from": {"id": uid, "first_name": "Ada"},
        "data": data or f"v1:{action}:{INTENT}",
    }
    if with_message:
        cq["message"] = {"message_id": 555, "chat": {"id": cid, "type": "supergroup"}}
    return {"update_id": 1, "callback_query": cq}


@pytest.fixture
def seams(monkeypatch):
    """Everything `_tap` reaches through, scripted and recorded."""
    log = {
        "executed": [],
        "gucs": [],
        "tenant_at_execute": [],
        "capability_at_execute": [],
    }
    state = {
        "tenant": ResolvedTenant(
            workspace_id="ws", channel_binding_id="b1", via="chat"
        ),
        "user": "u1",
        "result": CommandResult(
            "executed", {"intent_id": INTENT, "state": "skipped", "lock_days": 7}
        ),
        "raise": None,
    }

    async def resolve_chat(executor, channel, external_ref):
        if isinstance(state["tenant"], Exception):
            raise state["tenant"]
        return state["tenant"]

    async def user_for_identity(executor, *, provider, external_id):
        return state["user"]

    async def tapper_for_identity(executor, *, provider, external_id):
        # The tapper and their display name in one read (#1286).
        if state["user"] is None:
            return None
        return state["user"], state.get("label", "Sam")

    async def apply_gucs(executor, **kw):
        log["gucs"].append(kw)

    async def execute(session, command):
        log["executed"].append(command)
        # The port sets no tenant: its gate reads under whichever the tap set.
        log["tenant_at_execute"].append(
            log["gucs"][-1]["tenant_id"] if log["gucs"] else None
        )
        # What the executors' readers would find on the connection
        # (`source_policy._read_context`): the dispatch's capability.
        log["capability_at_execute"].append(
            (getattr(session, "info", None) or {}).get("past_posts_capability")
        )
        if state["raise"] is not None:
            raise state["raise"]
        return state["result"]

    # Admission for taps: the window's count before the flip and the debit
    # inside it, scripted — `count` answers `state["window_count"]`, and
    # `increment` answers None when `state["exhausted"]`.
    state["window_count"] = 0
    state["exhausted"] = False
    log["debits"] = []

    async def count(executor, *, scope, key, window_start):
        log.setdefault("counts", []).append((scope, key))
        return state["window_count"]

    async def increment(executor, *, scope, key, window_start, limit):
        log["debits"].append((scope, key, limit))
        return None if state["exhausted"] else state["window_count"] + 1

    monkeypatch.setattr(tenant_resolution, "resolve_chat", resolve_chat)
    monkeypatch.setattr(identity, "user_for_identity", user_for_identity)
    monkeypatch.setattr(identity, "tapper_for_identity", tapper_for_identity)
    monkeypatch.setattr(unit_of_work, "apply_gucs", apply_gucs)
    monkeypatch.setattr(commands, "execute", execute)
    monkeypatch.setattr(rate_counters, "count", count)
    monkeypatch.setattr(rate_counters, "increment", increment)
    state["log"] = log
    return state


class TestTheConfigureMenu:
    """#1477: ⚙️ Configure opens a menu one level down, Back returns
    to the card's keyboard, and the toggle turns the item's Auto-approve flag.
    Every menu tap passes the gate the flag's command applies and spends the
    tap's admission; the menu edits the tapped message after the commit
    (`edit_markup`), never through the outbox; a rejected story's or a
    removed item's card heals on touch."""

    ROW = {
        "state": "awaiting_approval",
        "cancel_requested": False,
        "publish_step": "none",
        "media_item_id": "m-1",
        "item_state": "available",
        "auto_approve": False,
        "api_publishing_enabled": True,
    }

    #: The story's decision facts once its item is flagged: nothing holds it
    #: back.
    FACTS = dict(
        provider_account_ref="17841",
        cancel_requested=False,
        api_publishing_enabled=True,
        dry_run=False,
        has_login=True,
        has_link=False,
        flagged=True,
        position="manual",
        posted_via_api=False,
    )

    @pytest.fixture
    def menu(self, seams, monkeypatch):
        from src.services.target import auto_approval, prompts, readers

        seams["menu_row"] = dict(self.ROW)
        seams["facts"] = dict(self.FACTS)
        seams["gate"] = None
        seams["log"]["gated"] = []
        seams["log"]["facts_read_after"] = []

        async def authorize(session, kind, *, workspace_id, actor_user_id):
            seams["log"]["gated"].append((kind, workspace_id, actor_user_id))
            if seams["gate"] is not None:
                raise seams["gate"]

        async def row(session, sql, **params):
            assert sql == prompts._CARD_FACTS and params["ws"] == "ws"
            return seams["menu_row"]

        async def facts(session, *, workspace_id, intent_id):
            assert (workspace_id, intent_id) == ("ws", INTENT)
            seams["log"]["facts_read_after"].append(len(seams["log"]["executed"]))
            return auto_approval.Facts(**seams["facts"])

        monkeypatch.setattr(commands, "authorize", authorize)
        monkeypatch.setattr(readers, "row", row)
        monkeypatch.setattr(auto_approval, "facts", facts)
        monkeypatch.setattr(
            prompts, "settings_url", lambda: "https://app.example/settings"
        )
        return seams

    @pytest.mark.asyncio
    # A story in review whose item is still there opens the menu too: only a
    # card that cannot post again heals.
    @pytest.mark.parametrize("state", ["awaiting_approval", "review_required"])
    async def test_configure_opens_the_menu_and_runs_no_command(self, menu, state):
        from src.services.target import prompts

        menu["menu_row"]["state"] = state
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("cfg"))
        assert r.outcome == "menu" and r.answer_text == ""
        assert r.edit_markup == prompts.menu_keyboard(
            INTENT, on=False, settings_url="https://app.example/settings"
        )
        assert menu["log"]["executed"] == [], "navigation runs no command"
        assert menu["log"]["gated"] == [("set_item_auto_approve", "ws", "u1")]
        assert len(menu["log"]["debits"]) == 1, "a menu tap spends admission"

    @pytest.mark.asyncio
    async def test_a_flagged_item_opens_the_menu_with_its_toggle_on(self, menu):
        """The menu shows the item's flag as `card_facts` reads it: a flagged
        item's toggle says on, and a tap on it turns the flag off."""
        from src.services.target import prompts

        menu["menu_row"]["auto_approve"] = True
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("cfg"))
        assert r.outcome == "menu" and r.answer_text == ""
        (toggle,) = r.edit_markup["inline_keyboard"][0]
        assert toggle["text"] == "🔁 Auto-approve this item: on"
        assert toggle["callback_data"] == f"v1:aa0:{INTENT}"
        assert r.edit_markup == prompts.menu_keyboard(
            INTENT, on=True, settings_url="https://app.example/settings"
        )

    @pytest.mark.asyncio
    async def test_back_returns_the_cards_own_keyboard(self, menu):
        from src.services.target import prompts

        r = await telegram_dispatch.TelegramDispatcher()(None, tap("back"))
        assert r.edit_markup == prompts.card_keyboard(
            INTENT, state="awaiting_approval", api_publishing_enabled=True
        )
        assert menu["log"]["executed"] == []

    @pytest.mark.asyncio
    async def test_a_non_member_is_refused_and_spends_nothing(self, menu):
        menu["gate"] = TenantResolutionError("not_a_member", "no")
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("cfg"))
        assert r.outcome == "not_a_member" and r.edit_markup is None
        assert menu["log"]["debits"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "facts, tail",
        [
            ({}, "posts without a tap"),
            ({"api_publishing_enabled": False}, "(once Instagram API posting is on)"),
            (
                {"has_login": False},
                "(once Instagram is connected for this account)",
            ),
            ({"has_login": False, "dry_run": True}, "posts without a tap"),
            ({"has_link": True}, "(items with a link to add still ask)"),
            # The first block the serve step names, as a waiting card's note.
            (
                {"api_publishing_enabled": False, "has_link": True},
                "(once Instagram API posting is on)",
            ),
            # This story's pending cancel is no next serve's business, and
            # hides none of the next serve's blocks.
            ({"cancel_requested": True}, "posts without a tap"),
            (
                {"cancel_requested": True, "has_link": True},
                "(items with a link to add still ask)",
            ),
        ],
    )
    async def test_the_toggle_runs_the_flags_command_and_says_what_holds_it(
        self, menu, facts, tail
    ):
        """What holds the flag back is `auto_approval.decide`'s, read once the
        flag is written, so the answer never promises what the serve step
        would not do (a missing login was the case a hand-written check
        missed)."""
        from src.services.target import prompts

        menu["facts"].update(facts)
        menu["result"] = CommandResult("executed", {"auto_approve": True})
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("aa1"))
        (command,) = menu["log"]["executed"]
        assert command.kind == "set_item_auto_approve"
        assert command.args == {"media_item_id": "m-1", "auto_approve": True}
        assert r.outcome == "executed" and r.answer_text.startswith(
            "🔁 Auto-approve on"
        )
        assert r.answer_text.endswith(tail)
        assert menu["log"]["facts_read_after"] == [1], "read after the write"
        assert r.edit_markup == prompts.menu_keyboard(
            INTENT, on=True, settings_url="https://app.example/settings"
        )

    @pytest.mark.asyncio
    async def test_turning_it_off(self, menu):
        menu["menu_row"]["auto_approve"] = True
        menu["result"] = CommandResult("executed", {"auto_approve": False})
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("aa0"))
        (command,) = menu["log"]["executed"]
        assert command.args["auto_approve"] is False
        assert r.answer_text == "Auto-approve off"
        assert menu["log"]["facts_read_after"] == [], "nothing to hold back"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change", [{"state": "rejected"}, {"item_state": "removed"}]
    )
    @pytest.mark.parametrize("action", ["cfg", "back", "aa1"])
    async def test_a_card_that_cannot_post_again_heals_on_touch(
        self, menu, change, action
    ):
        menu["menu_row"].update(change)
        r = await telegram_dispatch.TelegramDispatcher()(None, tap(action))
        assert r.answer_text == "This item can't post again."
        assert r.edit_markup == {"inline_keyboard": []}, "its buttons go"
        assert menu["log"]["executed"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["cfg", "back", "aa1", "aa0"])
    async def test_a_removed_items_card_in_review_heals_to_give_up(self, menu, action):
        """The item cannot post again, so a touch heals its story's card to the
        story's own keyboard: Give up alone, the one resolution left, and no
        command runs."""
        menu["menu_row"].update(state="review_required", item_state="removed")
        r = await telegram_dispatch.TelegramDispatcher()(None, tap(action))
        assert r.answer_text == "This item can't post again."
        assert r.edit_markup == {
            "inline_keyboard": [
                [{"text": "🚫 Give up", "callback_data": f"v1:giveup:{INTENT}"}]
            ]
        }
        assert menu["log"]["executed"] == []

    @pytest.mark.asyncio
    async def test_at_the_cap_a_menu_tap_is_told(self, menu):
        menu["exhausted"] = True
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("cfg"))
        assert r.outcome == "too_many" and r.edit_markup is None


class TestTheTap:
    @pytest.mark.asyncio
    async def test_an_executed_tap_is_handled_and_answered(self, seams):
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap("skip"))
        assert isinstance(r, telegram_dispatch.TapResult)
        assert r.handled is True and r.outcome == "executed"
        assert r.callback_query_id == "q1"
        assert (r.chat_ref, r.message_ref) == ("-100", "555")
        assert "Skipped" in r.answer_text and r.show_alert is False
        cmd = seams["log"]["executed"][0]
        assert (cmd.kind, cmd.workspace_id, cmd.actor_user_id, cmd.channel) == (
            "skip",
            "ws",
            "u1",
            "telegram",
        )
        assert cmd.args == {"intent_id": INTENT}
        gucs = seams["log"]["gucs"][0]
        assert gucs["tenant_id"] == "ws" and gucs["actor_kind"] == "user"
        assert gucs["actor_user_id"] == "u1" and gucs["channel"] == "telegram"

    @pytest.mark.asyncio
    async def test_the_command_carries_the_card_the_tap_came_from(self, seams):
        """A settled story's answer adopts a message the ledger never learned
        (a resend's twin) and edits it — so the command names the binding it
        was tapped in and the message ref, on their own fields, never in
        `args`."""
        d = telegram_dispatch.TelegramDispatcher()
        await d(None, tap("skip"))
        command = seams["log"]["executed"][0]
        assert command.binding_id == "b1" and command.card_ref == "555"
        assert "card_ref" not in command.args and "binding_id" not in command.args

    async def test_an_answered_tap_that_adopted_a_card_spends_admission(self, seams):
        """An answered no-op spends nothing; an answered tap that
        ADOPTED a twin wrote a row and a paced edit, so it spends
        one (#1308)."""
        seams["result"] = CommandResult(
            "answered", {"intent_id": INTENT, "state": "posted", "adopted": False}
        )
        d = telegram_dispatch.TelegramDispatcher()
        await d(None, tap("skip"))
        assert seams["log"]["debits"] == [], "a plain answered tap spends nothing"
        seams["result"] = CommandResult(
            "answered", {"intent_id": INTENT, "state": "posted", "adopted": True}
        )
        await d(None, tap("skip"))
        assert len(seams["log"]["debits"]) == 1, "the adoption spent one"

    async def test_post_maps_to_approve_and_tells_the_truth_about_publishing(
        self, seams
    ):
        seams["result"] = CommandResult(
            "enqueued",
            {"intent_id": INTENT, "state": "approved", "job": "publish_pipeline"},
        )
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap("post"))
        assert seams["log"]["executed"][0].kind == "approve"
        assert r.outcome == "executed"
        assert "posting shortly" in r.answer_text.lower()

    @pytest.mark.asyncio
    async def test_an_answered_result_reads_back_the_cards_state(self, seams):
        seams["result"] = CommandResult(
            "answered",
            {
                "intent_id": INTENT,
                "state": "approved",
                "settled_by": "Sam",
                "settled_at": "2026-09-09 14:14 UTC",
            },
        )
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap("skip"))
        assert r.outcome == "answered" and r.handled is True
        assert "Already" in r.answer_text and "Sam" in r.answer_text

    @pytest.mark.asyncio
    async def test_an_unknown_token_is_answered_as_an_older_card(self, seams):
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap(data="v9:zap:" + INTENT))
        assert r.outcome == "older_card" and r.show_alert is True
        assert seams["log"]["executed"] == []

    @pytest.mark.asyncio
    async def test_an_unknown_chat_is_answered_not_connected(self, seams):
        seams["tenant"] = TenantResolutionError("unknown_binding")
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap())
        assert r.outcome == "unknown_binding" and r.show_alert is True
        assert seams["log"]["executed"] == []

    @pytest.mark.asyncio
    async def test_an_unlinked_tapper_is_told_where_to_link_and_nothing_flips(
        self, seams
    ):
        seams["user"] = None
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap())
        assert r.outcome == "unlinked" and r.show_alert is True
        assert "Settings" in r.answer_text
        assert seams["log"]["executed"] == []

    @pytest.mark.asyncio
    async def test_a_member_below_the_floor_is_answered(self, seams):
        seams["raise"] = TenantResolutionError("insufficient_role", "member < admin")
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap())
        assert r.outcome == "insufficient_role" and r.handled is True

    @pytest.mark.asyncio
    async def test_a_refusal_is_answered_by_its_reason(self, seams):
        seams["raise"] = CommandRefused("manual_mode", "posts by hand")
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap("post"))
        assert r.outcome == "manual_mode" and r.show_alert is True
        assert "Posted myself" in r.answer_text

    @pytest.mark.asyncio
    async def test_a_query_without_a_message_is_a_named_failure_never_a_raise(
        self, seams
    ):
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap(with_message=False))
        assert r.outcome == "no_message" and r.handled is True
        assert seams["log"]["executed"] == []

    @pytest.mark.asyncio
    async def test_a_bug_in_the_tap_is_tap_failed_never_a_raise(self, seams):
        seams["raise"] = RuntimeError("boom")
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap())
        assert r.outcome == "tap_failed" and r.handled is True
        assert r.callback_query_id == "q1"

    @pytest.mark.asyncio
    async def test_a_database_error_escapes_to_the_route(self, seams):
        from sqlalchemy.exc import DBAPIError

        seams["raise"] = DBAPIError("stmt", {}, Exception("down"))
        d = telegram_dispatch.TelegramDispatcher()
        with pytest.raises(DBAPIError):
            await d(None, tap())


class TestTheAppsPastPostsCapabilityRidesEveryDispatch:
    """A Post now tap on a past-posts card, and any command a delivery runs,
    reads Instagram past posts as the web's unit of work does
    (`principal.open_tenant`). The dispatcher reads the app's capability as
    the delivery runs, puts it on the delivery's connection for the dispatch,
    and puts back what the pooled connection carried before."""

    async def test_the_dispatcher_create_app_wires_runs_post_now_with_the_apps_capability(
        self, seams
    ):
        """The regression: before, the composition root built the dispatcher
        with no capability, and every past post read as not activated."""
        from src.api import app as app_module

        seams["result"] = CommandResult(
            "enqueued",
            {"intent_id": INTENT, "state": "approved", "job": "publish_pipeline"},
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(_refuse)) as client:
            application = app_module.create_app(
                engine=_ConnectOnly(), env=_armed_env(), past_posts_client=client
            )
            conn = _Pooled()
            r = await application.state.ingress.dispatch(conn, tap("post"))
        assert application.state.past_posts_capability == ARMED
        assert r.outcome == "executed"
        assert seams["log"]["executed"][0].kind == "approve"
        assert seams["log"]["capability_at_execute"] == [
            application.state.past_posts_capability
        ]
        assert "past_posts_capability" not in conn.info

    async def test_an_app_armed_after_create_app_arms_its_taps_too(self, seams):
        """The dispatcher reads `app.state` as each delivery runs, as the web
        reads `request.app.state` per request: a suite that arms a built app
        (five do) arms its Telegram path too, and a disarm is seen at once."""
        from src.api import app as app_module

        application = app_module.create_app(engine=_ConnectOnly(), env={})
        conn = _Pooled()
        application.state.past_posts_capability = ARMED
        await application.state.ingress.dispatch(conn, tap("skip"))
        application.state.past_posts_capability = None
        await application.state.ingress.dispatch(conn, tap("skip"))
        assert seams["log"]["capability_at_execute"] == [ARMED, None]
        assert "past_posts_capability" not in conn.info

    def test_the_dispatcher_takes_a_reader_never_the_value(self):
        """A value would be read once, at construction, and drift from
        `app.state`: the constructor refuses it."""
        # The message pins the guard: on `main` the unknown keyword raises a
        # TypeError too, which this must not mistake for it.
        with pytest.raises(TypeError, match="zero-argument reader"):
            telegram_dispatch.TelegramDispatcher(capability=ARMED)

    async def test_what_the_connection_carried_before_is_put_back(self, seams):
        before = object()
        conn = _Pooled(past_posts_capability=before)
        d = telegram_dispatch.TelegramDispatcher(capability=lambda: ARMED)
        await d(conn, tap("skip"))
        assert seams["log"]["capability_at_execute"] == [ARMED]
        assert conn.info["past_posts_capability"] is before

    async def test_an_unarmed_app_masks_what_a_pooled_connection_was_left_with(
        self, seams
    ):
        """Without the key ring the app's capability is None, and the dispatch
        reads None: never one a pooled connection still carries."""
        conn = _Pooled(past_posts_capability=ARMED)
        await telegram_dispatch.TelegramDispatcher()(conn, tap("skip"))
        assert seams["log"]["capability_at_execute"] == [None]
        assert conn.info["past_posts_capability"] is ARMED

    @pytest.mark.parametrize(
        "raised, outcome",
        [
            (
                CommandRefused(
                    "media_unsuitable",
                    "media admission refused",
                    facts={"source_reason": "validation_pending"},
                ),
                "media_unsuitable",
            ),
            (RuntimeError("boom"), "tap_failed"),
        ],
    )
    async def test_a_refused_or_failed_tap_puts_it_back(self, seams, raised, outcome):
        seams["raise"] = raised
        conn = _Pooled()
        d = telegram_dispatch.TelegramDispatcher(capability=lambda: ARMED)
        assert (await d(conn, tap("post"))).outcome == outcome
        assert "past_posts_capability" not in conn.info

    async def test_a_database_error_escapes_and_still_puts_it_back(self, seams):
        from sqlalchemy.exc import DBAPIError

        seams["raise"] = DBAPIError("stmt", {}, Exception("down"))
        conn = _Pooled()
        d = telegram_dispatch.TelegramDispatcher(capability=lambda: ARMED)
        with pytest.raises(DBAPIError):
            await d(conn, tap("post"))
        assert "past_posts_capability" not in conn.info

    async def test_the_restore_writes_the_dict_it_read_and_never_reads_info_again(
        self, seams, monkeypatch
    ):
        """An invalidated connection's `info` raises inside its transaction, so
        a restore that read `conn.info` again would raise from the `finally`
        and hide the tap's own outcome. The dispatch reads `info` once and puts
        the prior value back into that same dict."""

        async def refused(session, command):
            # Refused without touching the connection: its `info` has been
            # read once already, by the dispatch.
            raise CommandRefused(
                "media_unsuitable",
                "media admission refused",
                facts={"source_reason": "validation_pending"},
            )

        monkeypatch.setattr(commands, "execute", refused)
        before = object()
        conn = _InvalidatedAfterTheFirstRead(past_posts_capability=before)
        d = telegram_dispatch.TelegramDispatcher(capability=lambda: ARMED)
        r = await d(conn, tap("post"))
        assert (r.outcome, r.source_reason) == (
            "media_unsuitable",
            "validation_pending",
        )
        assert conn.record_info == {"past_posts_capability": before}
        assert conn.info_reads == 1

    async def test_every_lane_of_a_delivery_carries_it(self, monkeypatch):
        """The migration notice, the join path, `/start` and the link prompt
        run under the same capability as a card tap, so a command any lane
        comes to run reads past posts as the web does."""
        from src.services.target import identity_link
        from src.services.target.start_router import StartResult, StartRouter

        seen = []

        def record(lane, conn):
            seen.append((lane, conn.info.get("past_posts_capability")))

        async def follow(conn, *, old_ref, new_ref):
            record("migration", conn)
            return StartResult(outcome="chat_followed", handled=True)

        async def observe(conn, **kw):
            record("group", conn)
            return StartResult(outcome="joined", handled=True)

        async def probe(conn, ctx):
            record("start", conn)
            return StartResult(outcome="probed", handled=False)

        async def handle_tap(conn, link_tap, **kw):
            record("link", conn)
            return identity_link.LinkTapOutcome("linked", "Linked.")

        monkeypatch.setattr(telegram_dispatch.chat_migration, "follow", follow)
        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        monkeypatch.setattr(identity_link, "handle_tap", handle_tap)
        router = StartRouter()
        router.register("probe-", probe)
        d = telegram_dispatch.TelegramDispatcher(
            router=router, capability=lambda: ARMED
        )
        conn = _Pooled()
        for update in (
            {
                "message": {
                    "from": {"id": 42},
                    "chat": {"id": -1009876543210, "type": "supergroup"},
                    "migrate_from_chat_id": -4012345678,
                }
            },
            {
                "message": {
                    "text": "hi",
                    "from": {"id": 42},
                    "chat": {"id": -100777, "type": "supergroup"},
                }
            },
            start("/start probe-x"),
            link_tap_update(),
        ):
            await d(conn, update)
            assert "past_posts_capability" not in conn.info
        assert seen == [
            ("migration", ARMED),
            ("group", ARMED),
            ("start", ARMED),
            ("link", ARMED),
        ]

    async def test_a_connection_without_an_info_dict_is_dispatched_as_it_is(
        self, seams
    ):
        """A test's double, or None: nothing to carry, and nothing raises."""
        d = telegram_dispatch.TelegramDispatcher(capability=lambda: ARMED)
        assert (await d(None, tap("skip"))).outcome == "executed"
        assert (await d(_Savepoints(), tap("skip"))).outcome == "executed"
        assert seams["log"]["capability_at_execute"] == [None, None]


class TestARefusalNamesItsSuitabilityReason:
    """A Post now tap refused for its media names the refusal's closed
    suitability reason on the tap's log lines (`TapResult.source_reason`); the
    tapper's answer is unchanged."""

    async def test_a_media_refusal_carries_its_reason_to_the_result_and_the_log(
        self, seams, caplog
    ):
        caplog.set_level("INFO")
        seams["raise"] = CommandRefused(
            "media_unsuitable",
            "media admission refused",
            facts={"source_reason": "past_posts_not_activated"},
        )
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("post"))
        assert (r.outcome, r.source_reason) == (
            "media_unsuitable",
            "past_posts_not_activated",
        )
        assert (r.answer_text, r.show_alert) == telegram_dispatch.ANSWERS[
            "media_unsuitable"
        ], "the tapper's answer is the reason's, unchanged"
        (line,) = [
            m
            for m in (rec.getMessage() for rec in caplog.records)
            if m.startswith("tap update_id=")
        ]
        assert " outcome=media_unsuitable " in line
        assert line.endswith(" source_reason=past_posts_not_activated")

    async def test_an_executed_tap_carries_none(self, seams):
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("skip"))
        assert r.outcome == "executed" and r.source_reason is None

    @pytest.mark.parametrize(
        "exc, logged",
        [
            (
                CommandRefused(
                    "media_unsuitable",
                    "x",
                    facts={"source_reason": "validation_pending"},
                ),
                "validation_pending",
            ),
            (
                CommandRefused(
                    "media_unsuitable", "x", facts={"source_reason": "not_found"}
                ),
                "not_found",
            ),
            (CommandRefused("manual_mode", "by hand"), None),
            (
                CommandRefused(
                    "locked",
                    "x",
                    facts={"in_the_way": ["item_removed"], "overridable": False},
                ),
                None,
            ),
            (
                CommandRefused(
                    "media_unsuitable",
                    "x",
                    facts={"source_reason": "cannot identify image file '/tmp/a.jpg'"},
                ),
                "unlisted",
            ),
            (
                CommandRefused("media_unsuitable", "x", facts={"source_reason": 7}),
                "unlisted",
            ),
            (TenantResolutionError("not_a_member", "no"), None),
        ],
    )
    def test_only_a_closed_code_reaches_the_log(self, exc, logged):
        assert telegram_dispatch._source_reason(exc) == logged


class TestTheKeyIsTheReadersOwn:
    """The dispatcher writes the key every past-posts reader asks its executor
    for. The readers spell it inline, so a rename on their side (as `ac36bc2`
    renamed `archive_capability`) must fail here, on any OS, and not only in
    the Linux-only source gate. A reader moved to a shared constant fails here
    too: then pin the constant instead."""

    @pytest.mark.parametrize(
        "module, function",
        [
            ("source_policy", "_read_context"),
            ("category_mix", "destination_projection"),
            ("versioned_mix", "resource"),
            ("versioned_mix", "preview"),
            ("command_executors", "sync_now"),
        ],
    )
    def test_each_reader_asks_for_the_key_the_dispatcher_writes(self, module, function):
        import importlib
        import inspect
        import re

        reader = getattr(
            importlib.import_module(f"src.services.target.{module}"), function
        )
        asked = set(
            re.findall(
                r"\.info\.get\(\s*[\"']([^\"']+)[\"']", inspect.getsource(reader)
            )
        )
        assert telegram_dispatch.PAST_POSTS_CAPABILITY_KEY in asked, (
            module,
            function,
            asked,
        )


class TestEveryReasonHasAnAnswer:
    def test_every_command_refusal_reason_has_an_entry(self):
        for reason in commands.REASONS:
            text, alert = telegram_dispatch.ANSWERS[reason]
            assert text

    @pytest.mark.parametrize(
        "reason",
        [
            "not_a_member",
            "insufficient_role",
            "unknown_binding",
            "revoked_binding",
            "unknown_channel",
        ],
    )
    def test_every_resolver_refusal_has_an_entry(self, reason):
        text, _ = telegram_dispatch.ANSWERS[reason]
        assert text

    @pytest.mark.parametrize("outcome", telegram_dispatch.TAP_OUTCOMES)
    def test_every_tap_outcome_has_an_entry(self, outcome):
        """Its own entry, never the web's fallback, which has words too; and
        every outcome but navigation's says something."""
        text, _ = telegram_dispatch.ANSWERS[outcome]
        if outcome == "menu":
            assert text == "", "navigation answers with no toast"
        else:
            assert text

    def test_an_unknown_reason_falls_back_to_the_web(self):
        text, alert = telegram_dispatch.answer_for("something_new")
        assert "web" in text.lower() and alert is True


class TestTapAdmission:
    """Admission for taps: DEBITED only for a flip that ran,
    inside the flip's savepoint; the increment's own guard is the check."""

    @pytest.mark.asyncio
    async def test_an_executed_flip_debits_the_workspace_once(self, seams, monkeypatch):
        monkeypatch.setattr(settings, "TARGET_TAP_ADMISSION_PER_MINUTE", 120)
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("skip"))
        assert r.outcome == "executed"
        assert seams["log"]["debits"] == [("ws_admission", "ws", 120)]
        assert "counts" not in seams["log"], "no read before the flip"

    @pytest.mark.asyncio
    async def test_an_enqueued_post_debits_too(self, seams):
        seams["result"] = CommandResult(
            "enqueued",
            {"intent_id": INTENT, "state": "approved", "job": "publish_pipeline"},
        )
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("post"))
        assert r.outcome == "executed" and len(seams["log"]["debits"]) == 1

    @pytest.mark.asyncio
    async def test_an_answered_repeat_spends_nothing_even_at_the_cap(self, seams):
        """At the cap: `_settle` answers before any write, so a repeat on a
        settled card is never told 'too many'."""
        seams["exhausted"] = True
        seams["result"] = CommandResult(
            "answered",
            {
                "intent_id": INTENT,
                "state": "skipped",
                "settled_by": "Ada",
                "settled_at": "2026-09-10T12:00:00+00:00",
                "outcome_text": "⏭️ Skipped by Ada",
            },
        )
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("skip"))
        assert r.outcome == "answered" and seams["log"]["debits"] == []

    @pytest.mark.asyncio
    async def test_a_refusal_spends_nothing(self, seams):
        seams["raise"] = CommandRefused("manual_mode", "by hand")
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("post"))
        assert r.outcome == "manual_mode" and seams["log"]["debits"] == []

    @pytest.mark.asyncio
    async def test_at_the_limit_the_flip_rolls_back_and_the_tap_is_told(self, seams):
        """The debit's guard said "full": the flip rolls back with the
        savepoint and the tap is told — an alert, never a 503."""
        seams["exhausted"] = True
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("skip"))
        assert r.outcome == "too_many" and r.show_alert is True and r.handled is True
        assert "Too many" in r.answer_text
        assert len(seams["log"]["executed"]) == 1, "the flip ran, then rolled back"
        assert seams["log"]["debits"] == [("ws_admission", "ws", 120)]


class TestThePostAnswerSaysWhatHappensNext:
    def test_a_dry_run_and_a_paused_workspace_are_named(self):
        from src.services.target.telegram_dispatch import _executed_text

        dry = CommandResult("enqueued", {"dry_run": True, "paused": False})
        paused = CommandResult("enqueued", {"dry_run": False, "paused": True})
        live = CommandResult("enqueued", {"dry_run": False, "paused": False})
        assert "dry run" in _executed_text("post", dry)
        assert "paused" in _executed_text("post", paused)
        assert "posting shortly" in _executed_text("post", live)


def test_a_repeat_tap_on_a_dry_run_row_hears_dry_run_not_posted():
    from src.services.target.telegram_dispatch import _answered_text

    r = CommandResult(
        "answered",
        {"state": "posted", "published_via": "dry_run", "settled_by": None},
    )
    assert "Dry run" in _answered_text(r) and "Posted" not in _answered_text(r)


def test_a_repeat_tap_hears_the_cards_own_line():
    """The toast is the line the executor wrote for the card (`_settle`'s
    `outcome_text`), so the two never disagree — the rule that approved the
    story included (#1477)."""
    from src.services.target.telegram_dispatch import _answered_text

    r = CommandResult(
        "answered",
        {
            "state": "posted",
            "settled_by": None,
            "outcome_text": "✅ Posted by YOLO · 2026-10-12 14:01 UTC",
        },
    )
    assert _answered_text(r) == "Already: ✅ Posted by YOLO · 2026-10-12 14:01 UTC"


class TestATapIsCheap:
    """#1286: the tap's own statements, counted where the dispatch spends
    them — one GUC statement carrying the lock timeout, the tapper and their
    name in one read, and the command port run under that statement's
    tenant."""

    @pytest.mark.asyncio
    async def test_the_lock_timeout_rides_the_one_guc_statement(self, seams):
        d = telegram_dispatch.TelegramDispatcher()
        await d(None, tap("skip"))
        (gucs,) = seams["log"]["gucs"]
        assert gucs["lock_timeout"] == telegram_dispatch.TAP_LOCK_TIMEOUT == "2s"
        assert gucs["tenant_id"] == "ws" and gucs["actor_user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_the_tapper_s_name_rides_the_command(self, seams):
        seams["label"] = "Dana"
        d = telegram_dispatch.TelegramDispatcher()
        await d(None, tap("skip"))
        (command,) = seams["log"]["executed"]
        assert command.actor_label == "Dana"
        assert "actor_label" not in command.args, "never in args (the web body's)"

    @pytest.mark.asyncio
    async def test_a_tapper_without_a_name_still_taps(self, seams):
        seams["label"] = None
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap("skip"))
        assert r.outcome == "executed"
        (command,) = seams["log"]["executed"]
        assert command.actor_label is None

    @pytest.mark.asyncio
    async def test_the_command_port_runs_under_the_taps_tenant(self, seams):
        d = telegram_dispatch.TelegramDispatcher()
        await d(None, tap("skip"))
        assert seams["log"]["tenant_at_execute"] == ["ws"]


class TestTheReviewTaps:
    """The review card's three buttons are one command — `resolve_review` —
    with the resolution in `args`; the answer names what happens next."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("action", "resolution"),
        [("notposted", "retry"), ("itposted", "posted"), ("giveup", "cancel")],
    )
    async def test_a_review_tap_carries_its_resolution(self, seams, action, resolution):
        seams["result"] = CommandResult(
            "executed", {"intent_id": INTENT, "state": "approved"}
        )
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap(action))
        assert r.outcome == "executed"
        cmd = seams["log"]["executed"][0]
        assert cmd.kind == "resolve_review"
        expected = {"intent_id": INTENT, "resolution": resolution}
        if action == "notposted":
            # The button's label IS the member's verdict: "not on my story".
            expected["verdict"] = "not_posted"
        assert cmd.args == expected

    def test_the_answers_name_what_happens_next(self):
        from src.services.target.telegram_dispatch import _executed_text

        retry = CommandResult("enqueued", {"state": "approved", "dry_run": False})
        assert "again" in _executed_text("notposted", retry).lower()
        dry = CommandResult("enqueued", {"state": "approved", "dry_run": True})
        assert "dry run" in _executed_text("notposted", dry)
        paused = CommandResult("enqueued", {"state": "approved", "paused": True})
        assert "paused" in _executed_text("notposted", paused)
        assert (
            "posted"
            in _executed_text("itposted", CommandResult("executed", {})).lower()
        )
        assert "Cancelled" in _executed_text("giveup", CommandResult("executed", {}))

    def test_a_posted_claim_with_nothing_to_confirm_is_told_why(self):
        text, alert = telegram_dispatch.answer_for("nothing_to_confirm")
        assert alert is True and "post again" in text.lower()

    def test_a_retry_on_a_lost_answer_is_told_to_look_first(self):
        text, alert = telegram_dispatch.answer_for("may_have_posted")
        assert alert is True and "It posted" in text

    def test_manual_mode_on_the_review_card_names_the_review_cards_lever(self):
        """The approval card's manual-mode answer says "tap ✅ Posted myself";
        the review card has no such button."""
        text, _ = telegram_dispatch.answer_for("manual_mode", action="notposted")
        assert "Give up" in text and "Posted myself" not in text
        text, _ = telegram_dispatch.answer_for("manual_mode", action="post")
        assert "Posted myself" in text


# ---------------------------------------------------------------------------
# The identity link's Confirm / Cancel: a callback_query beside the cards'.
# ---------------------------------------------------------------------------


def link_tap_update(action="linkok", *, uid=4242, cid=4242, chat_type="private"):
    from src.services.target import callback_tokens

    return {
        "update_id": 3,
        "callback_query": {
            "id": "q9",
            "from": {"id": uid, "username": "ada"},
            "data": callback_tokens.link_token(action, "St4te_x-", "4242"),
            "message": {"message_id": 77, "chat": {"id": cid, "type": chat_type}},
        },
    }


class TestTheLinkConfirmationTap:
    @pytest.mark.asyncio
    async def test_it_reaches_the_link_lane_not_the_command_port(
        self, seams, monkeypatch
    ):
        from src.services.target import identity_link

        seen = {}

        async def handle_tap(conn, tap, **kw):
            seen.update(tap=tap, **kw)
            return identity_link.LinkTapOutcome("linked", "Linked.", edit_text="done")

        monkeypatch.setattr(identity_link, "handle_tap", handle_tap)
        r = await telegram_dispatch.TelegramDispatcher()(None, link_tap_update())
        assert isinstance(r, telegram_dispatch.TapResult)
        assert (r.outcome, r.answer_text, r.edit_text) == ("linked", "Linked.", "done")
        assert (r.callback_query_id, r.chat_ref, r.message_ref) == ("q9", "4242", "77")
        assert seen["tap"].state == "St4te_x-" and seen["tap"].action == "linkok"
        assert (seen["from_user_id"], seen["chat_ref"], seen["chat_type"]) == (
            "4242",
            "4242",
            "private",
        )
        assert seams["log"]["executed"] == [], "a link tap ran a command"
        assert seams["log"]["debits"] == [] and seams["log"]["gucs"] == []

    @pytest.mark.asyncio
    async def test_a_lane_crash_is_a_named_tap_failed(self, monkeypatch):
        from src.services.target import identity_link

        async def handle_tap(conn, tap, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(identity_link, "handle_tap", handle_tap)
        r = await telegram_dispatch.TelegramDispatcher()(None, link_tap_update())
        assert r.outcome == "tap_failed" and r.edit_text is None

    @pytest.mark.asyncio
    async def test_another_users_tap_is_refused_end_to_end(self, monkeypatch):
        """Through the real lane: the button was offered to 4242."""
        from src.services.target import identity_link

        async def never(*a, **kw):
            raise AssertionError("the state was touched for the wrong tapper")

        monkeypatch.setattr(identity_link.oauth_states, "consume_state", never)
        monkeypatch.setattr(identity_link.identity, "link_identity", never)
        r = await telegram_dispatch.TelegramDispatcher()(
            None, link_tap_update(uid=5555, cid=5555)
        )
        assert r.outcome == "tapper_mismatch" and r.show_alert is True


# ---------------------------------------------------------------------------
# The waitlist ping's ✅ Admit (#1723): user-plane, for the operators.
# ---------------------------------------------------------------------------

OPERATOR = "00000000-0000-4000-8000-0000000000a1"
ADDRESS = "new.person@example.com"
#: The tapper's Telegram id, apart from the chat's, so the lookup's key is pinned.
TAPPER_ID = 777
PING = f"New waitlist signup!\n\nEmail: {ADDRESS}\nTime: 2026-10-13T09:00:00Z"


def admit_update(*, text=PING):
    """A tap on the ping's ✅ Admit, in the operator's private chat."""
    message = {"message_id": 88, "chat": {"id": 4242, "type": "private"}}
    if text is not None:
        message["text"] = text
    return {
        "update_id": 5,
        "callback_query": {
            "id": "q5",
            "from": {"id": TAPPER_ID, "first_name": "Ada"},
            "data": callback_tokens.admit_token(signup_admissions.ref(ADDRESS)),
            "message": message,
        },
    }


@pytest.fixture
def admit(seams, monkeypatch):
    """`_admit_tap`'s seams over the card tap's: the tapper is an operator,
    the ref finds ADDRESS, and `admit` answers `admit["admit"]` (or raises
    it). Every call is recorded, a chat resolution included."""
    log = seams["log"]
    log.update(waitlist=[], admitted=[], resolved=[], named=[], tappers=[], active=[])
    seams.update(user=OPERATOR, admit="admitted", rows=[{"email": ADDRESS}])
    monkeypatch.setattr(settings, "OPS_USER_IDS", OPERATOR)
    monkeypatch.setattr(settings, "WEB_APP_URL", "https://app.example/")

    async def tapper_for_identity(executor, *, provider, external_id):
        log["tappers"].append((provider, external_id))
        if seams["user"] is None:
            return None
        return seams["user"], seams.get("label", "Sam")

    async def telegram_ids_for(executor, user_ids):
        # The ping's recipients read: an active account's linked Telegram ids.
        log["active"].append(list(user_ids))
        return [] if seams.get("disabled") else [str(TAPPER_ID)]

    async def waitlist(conn, *, ref=None):
        log["waitlist"].append(ref)
        return seams["rows"]

    async def admit_(conn, *, email, claim_base, resend=False):
        log["admitted"].append((email, claim_base, resend))
        if isinstance(seams["admit"], BaseException):
            raise seams["admit"]
        return seams["admit"]

    async def resolve_chat(executor, channel, external_ref):
        log["resolved"].append(external_ref)
        return seams["tenant"]

    async def display_name_for(executor, *, user_id):
        log["named"].append(user_id)
        return "a teammate"

    monkeypatch.setattr(identity, "tapper_for_identity", tapper_for_identity)
    monkeypatch.setattr(identity, "telegram_ids_for", telegram_ids_for)
    monkeypatch.setattr(signup_admissions, "waitlist", waitlist)
    monkeypatch.setattr(signup_admissions, "admit", admit_)
    monkeypatch.setattr(tenant_resolution, "resolve_chat", resolve_chat)
    monkeypatch.setattr(identity, "display_name_for", display_name_for)
    return seams


class TestTheAdmitTap:
    @pytest.mark.asyncio
    async def test_it_reaches_the_admit_lane_before_any_chat_resolution(self, admit):
        """User-plane, like the link's Confirm: no chat resolved, no tenant,
        no command port, no admission debit; the ref finds the entry, and the
        admission runs on the delivery's own connection."""
        conn = _Savepoints()
        r = await telegram_dispatch.TelegramDispatcher()(conn, admit_update())
        assert isinstance(r, telegram_dispatch.TapResult)
        assert (r.outcome, r.callback_query_id, r.chat_ref, r.message_ref) == (
            "admitted",
            "q5",
            "4242",
            "88",
        )
        log = admit["log"]
        assert log["resolved"] == [], "an admit tap resolved a chat"
        assert log["executed"] == [] and log["gucs"] == [] and log["debits"] == []
        assert log["waitlist"] == [signup_admissions.ref(ADDRESS)]
        assert log["admitted"] == [(ADDRESS, "https://app.example/claim/", False)]
        # The tapper is looked up by the tap's sender, never the chat, and must be
        # active; the admission and its job share one savepoint.
        assert log["tappers"] == [("telegram", str(TAPPER_ID))]
        assert log["active"] == [[OPERATOR]]
        assert conn.opened == 1

    @pytest.mark.asyncio
    async def test_an_admission_edits_the_ping_naming_who_admitted_it(self, admit):
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert (r.answer_text, r.show_alert) == (
            "✅ Admitted — the email is queued",
            False,
        )
        assert r.edit_text == PING + "\n✅ Admitted by Sam"
        assert admit["log"]["named"] == [], "the tapper's name came with the tapper"

    @pytest.mark.asyncio
    async def test_a_tapper_without_a_name_is_named_the_long_way(self, admit):
        admit["label"] = None
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert r.edit_text == PING + "\n✅ Admitted by a teammate"
        assert admit["log"]["named"] == [OPERATOR]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome, answer",
        [("admitted_before", "Already admitted"), ("signed_up", "Already signed up")],
    )
    async def test_an_entry_admitted_before_says_so_on_the_ping(
        self, admit, outcome, answer
    ):
        # The ping's line is the answer: someone who signed up by invitation was
        # never admitted.
        admit["admit"] = outcome
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert (r.outcome, r.answer_text, r.show_alert) == (outcome, answer, False)
        assert r.edit_text == PING + "\n" + answer

    @pytest.mark.asyncio
    async def test_a_disabled_operator_gets_the_one_answer_and_nothing_runs(
        self, admit
    ):
        """Named in `OPS_USER_IDS` and linked, but no longer active: the ping's
        recipients read leaves them out, and so does the tap."""
        admit["disabled"] = True
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert (r.outcome, r.show_alert, r.edit_text) == ("not_operator", True, None)
        assert admit["log"]["waitlist"] == [] and admit["log"]["admitted"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "user, operators",
        [
            (None, OPERATOR),  # an unlinked tapper
            ("00000000-0000-4000-8000-0000000000b2", OPERATOR),  # not an operator
            (OPERATOR, ""),  # no operator at all
        ],
    )
    async def test_anyone_but_a_linked_operator_gets_one_answer_and_nothing_runs(
        self, admit, monkeypatch, user, operators
    ):
        admit["user"] = user
        monkeypatch.setattr(settings, "OPS_USER_IDS", operators)
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert (r.outcome, r.answer_text, r.show_alert) == (
            "not_operator",
            "Only the site's operators can admit",
            True,
        )
        assert r.edit_text is None
        assert admit["log"]["waitlist"] == [] and admit["log"]["admitted"] == []

    @pytest.mark.asyncio
    async def test_the_operator_check_ignores_case_as_require_ops_does(self, admit):
        admit["user"] = OPERATOR.upper()
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert r.outcome == "admitted"

    @pytest.mark.asyncio
    async def test_an_entry_removed_by_hand_is_not_found(self, admit):
        admit["rows"] = []
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert (r.outcome, r.show_alert, r.edit_text) == ("not_found", True, None)
        assert r.answer_text == "This signup is no longer on the waitlist"
        assert admit["log"]["admitted"] == []

    @pytest.mark.asyncio
    async def test_no_web_origin_is_not_configured(self, admit, monkeypatch):
        monkeypatch.setattr(settings, "WEB_APP_URL", "")
        admit["admit"] = "not_configured"
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert admit["log"]["admitted"] == [(ADDRESS, None, False)]
        assert (r.outcome, r.show_alert, r.edit_text) == ("not_configured", True, None)
        assert "WEB_APP_URL" in r.answer_text

    @pytest.mark.asyncio
    async def test_a_ping_telegram_sent_no_text_for_is_left_alone(self, admit):
        r = await telegram_dispatch.TelegramDispatcher()(
            _Savepoints(), admit_update(text=None)
        )
        assert r.outcome == "admitted" and r.edit_text is None

    @pytest.mark.asyncio
    async def test_a_bug_is_a_named_tap_failed(self, admit):
        admit["admit"] = RuntimeError("boom")
        r = await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert (r.outcome, r.handled, r.show_alert, r.edit_text) == (
            "tap_failed",
            True,
            True,
            None,
        )
        assert r.answer_text == telegram_dispatch.ADMIT_ANSWERS["tap_failed"][0]

    @pytest.mark.asyncio
    async def test_a_database_error_escapes_to_the_route(self, admit):
        from sqlalchemy.exc import DBAPIError

        admit["admit"] = DBAPIError("stmt", {}, Exception("down"))
        with pytest.raises(DBAPIError):
            await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())

    @pytest.mark.asyncio
    async def test_the_log_names_the_outcome_never_the_address_ref_or_chat(
        self, admit, caplog
    ):
        caplog.set_level("INFO")
        await telegram_dispatch.TelegramDispatcher()(_Savepoints(), admit_update())
        assert "admit tap update_id=5 outcome=admitted" in caplog.text
        for secret in (ADDRESS, signup_admissions.ref(ADDRESS), "4242"):
            assert secret not in caplog.text

    def test_every_outcome_the_tap_can_end_in_has_an_answer(self):
        """`admit`'s answers, less `resent` (the tap never resends), and the
        tap's own three."""
        tap_own = {"not_operator", "not_found", "tap_failed"}
        expected = (set(vocabulary.ADMIT_OUTCOMES) - {"resent"}) | tap_own
        assert set(telegram_dispatch.ADMIT_ANSWERS) == expected
        assert all(text for text, _ in telegram_dispatch.ADMIT_ANSWERS.values())
