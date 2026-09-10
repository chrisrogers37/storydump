"""The ingress dispatcher (#1183) — the `/start` door, and what it refuses.

The load-bearing test here is `test_a_non_start_update_is_NAMED_not_dropped`.
Everything else is ordinary wiring; that one is what keeps the `/start`-only
bound observable from outside the process.
"""

from __future__ import annotations

import uuid

import pytest

from src.config.settings import settings
from src.services.target import (
    rate_counters,
    commands,
    identity,
    telegram_dispatch,
    tenant_resolution,
    unit_of_work,
)
from src.services.target.commands import CommandRefused, CommandResult
from src.services.target.start_router import GREETED, UNROUTED
from src.services.target.tenant_resolution import ResolvedTenant, TenantResolutionError


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
        silently either. (A callback query IS served now: see TestTheTap.)"""
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

    def test_the_router_has_lane_a_registered_and_room_for_lane_c(self):
        router = telegram_dispatch.build_router()
        assert "link-" in router._handlers

        async def inv(conn, ctx): ...

        router.register("inv-", inv)  # lane C joins by registration, not a new door
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
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(
            None,
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
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(
            None,
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
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(
            None,
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

    @pytest.mark.asyncio
    async def test_a_sync_error_is_named_and_never_raised(self, monkeypatch):
        async def observe(conn, **kw):
            raise RuntimeError("door missing")

        monkeypatch.setattr(telegram_dispatch.membership_sync, "observe", observe)
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(
            None,
            {
                "message": {
                    "text": "hi",
                    "from": {"id": 1},
                    "chat": {"id": -5, "type": "group"},
                }
            },
        )
        assert r.outcome == telegram_dispatch.MEMBERSHIP_SYNC_FAILED and not r.handled


# ---------------------------------------------------------------------------
# The tap (phase 1 of the 2026-09-09 plan): a callback_query is served.
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
    log = {"executed": [], "gucs": []}
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

    async def apply_gucs(executor, **kw):
        log["gucs"].append(kw)

    async def execute(session, command):
        log["executed"].append(command)
        if state["raise"] is not None:
            raise state["raise"]
        return state["result"]

    # S.2 for taps (phase 2): the window's count before the flip and the debit
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
    monkeypatch.setattr(unit_of_work, "apply_gucs", apply_gucs)
    monkeypatch.setattr(commands, "execute", execute)
    monkeypatch.setattr(rate_counters, "count", count)
    monkeypatch.setattr(rate_counters, "increment", increment)
    state["log"] = log
    return state


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
        if telegram_dispatch.PUBLISH_LEG_LIVE:
            assert "posting shortly" in r.answer_text.lower()
        else:
            assert "isn't live yet" in r.answer_text

    @pytest.mark.asyncio
    async def test_an_answered_result_reads_back_the_cards_state(self, seams):
        seams["result"] = CommandResult(
            "answered",
            {
                "intent_id": INTENT,
                "state": "approved",
                "settled_by": "Chris",
                "settled_at": "2026-09-09 14:14 UTC",
            },
        )
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, tap("skip"))
        assert r.outcome == "answered" and r.handled is True
        assert "Already" in r.answer_text and "Chris" in r.answer_text

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


class TestEveryReasonHasAnAnswer:
    def test_every_command_refusal_reason_has_an_entry(self):
        for reason in commands.REASONS:
            text, alert = telegram_dispatch.answer_for(reason)
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
        text, _ = telegram_dispatch.answer_for(reason)
        assert text

    @pytest.mark.parametrize("outcome", telegram_dispatch.TAP_OUTCOMES)
    def test_every_tap_outcome_has_an_entry(self, outcome):
        text, _ = telegram_dispatch.answer_for(outcome)
        assert text

    def test_an_unknown_reason_falls_back_to_the_web(self):
        text, alert = telegram_dispatch.answer_for("something_new")
        assert "web" in text.lower() and alert is True


class TestTapAdmission:
    """S.2 for taps (phase 2 step 3, F12): DEBITED only for a flip that ran,
    inside the flip's savepoint; the increment's own guard is the check."""

    @pytest.mark.asyncio
    async def test_an_executed_flip_debits_the_workspace_once(self, seams, monkeypatch):
        monkeypatch.setattr(settings, "TARGET_TAP_ADMISSION_PER_MINUTE", 120)
        r = await telegram_dispatch.TelegramDispatcher()(None, tap("skip"))
        assert r.outcome == "executed"
        assert seams["log"]["debits"] == [("ws_admission", "ws", 120)]
        assert "counts" not in seams["log"], "no read before the flip (R6)"

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
        """R6 at the cap: `_settle` answers before any write, so a repeat on a
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
