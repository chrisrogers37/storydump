"""The ingress dispatcher (#1183) — the `/start` door, and what it refuses.

The load-bearing test here is `test_a_non_start_update_is_NAMED_not_dropped`.
Everything else is ordinary wiring; that one is what keeps the `/start`-only
bound observable from outside the process.
"""

from __future__ import annotations

import pytest

from src.services.target import telegram_dispatch
from src.services.target.start_router import GREETED, UNROUTED


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
        """An edit, a callback query, a channel post — none is a /start, and
        none may vanish silently either."""
        d = telegram_dispatch.TelegramDispatcher()
        r = await d(None, {"callback_query": {"id": "x"}})
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
