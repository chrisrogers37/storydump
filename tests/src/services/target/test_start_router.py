"""The shared `/start` door (`07` §2 D33/D35) — lane A's half plus the
properties lane C registers against.

The load-bearing tests here are the ones about REFUSAL, not the ones about
success: this door is unauthenticated, serves two disjoint lookup tables, and
its failure mode is telling a prober which tokens exist.
"""

from __future__ import annotations

import pytest

from src.services.target.start_router import (
    GREETED,
    REFUSAL,
    UNROUTED,
    StartResult,
    StartRouter,
)


def update(text=None, uid=7, cid=99, ctype="private", name="ada"):
    msg = {"from": {"id": uid, "username": name}, "chat": {"id": cid, "type": ctype}}
    if text is not None:
        msg["text"] = text
    return {"message": msg}


async def ok(conn, ctx):
    return StartResult(outcome="did_it", handled=True, reply="done")


async def refuses(conn, ctx):
    return StartResult(outcome="nope", handled=False)


class TestTheThreeOutcomesStayThree:
    @pytest.mark.asyncio
    async def test_a_bare_start_is_greeted_not_an_error(self):
        r = await StartRouter().dispatch(None, update("/start"))
        assert r.outcome == GREETED and r.handled is True

    @pytest.mark.asyncio
    async def test_a_known_prefix_dispatches(self):
        router = StartRouter()
        router.register("link-", ok)
        r = await router.dispatch(None, update("/start link-abc"))
        assert (r.outcome, r.handled) == ("did_it", True)

    @pytest.mark.asyncio
    async def test_an_unknown_prefix_is_unrouted_and_never_dispatched(self):
        """The property lane C pins: D33/D35's tables are disjoint, so a
        payload matching no prefix has no fallback worth trying."""
        seen = []

        async def spy(conn, ctx):
            seen.append(ctx)
            return StartResult(outcome="x", handled=True, reply="y")

        router = StartRouter()
        router.register("link-", spy)
        r = await router.dispatch(None, update("/start inv-abc"))

        assert r.outcome == UNROUTED
        assert r.handled is False
        assert seen == [], "an unknown prefix reached a handler"

    @pytest.mark.asyncio
    async def test_unrouted_is_not_collapsed_into_greeted(self):
        """Both are 'nothing ran'. Only one of them is success."""
        router = StartRouter()
        bare = await router.dispatch(None, update("/start"))
        junk = await router.dispatch(None, update("/start wat"))
        assert bare.outcome != junk.outcome
        assert bare.handled is True and junk.handled is False


class TestTheRouterOwnsEveryRefusalString:
    """A security property, not tidiness — see the module docstring. If each
    handler wrote its own refusal, wording would drift and the difference
    would tell a prober which tokens exist (`07` §5)."""

    def test_a_refusing_handler_cannot_supply_reply_text(self):
        with pytest.raises(ValueError, match="existence oracle"):
            StartResult(outcome="nope", handled=False, reply="no such invitation")

    @pytest.mark.asyncio
    async def test_every_refusal_reads_identically_whatever_happened(self):
        router = StartRouter()
        router.register("link-", refuses)
        a = await router.dispatch(None, update("/start link-known-but-dead"))
        b = await router.dispatch(None, update("/start zzz-unregistered"))
        # Different outcomes for us; the tapper is told one thing.
        assert a.outcome != b.outcome
        assert a.reply is None and b.reply is None
        assert REFUSAL  # the single string the caller emits for both


class TestRegistrationRefusesAmbiguityAtRegistrationTime:
    def test_a_duplicate_prefix_is_refused(self):
        r = StartRouter()
        r.register("link-", ok)
        with pytest.raises(ValueError, match="already registered"):
            r.register("link-", refuses)

    def test_a_prefix_of_another_prefix_is_refused(self):
        """`link-` and `link-v2-` would resolve by accident of ordering."""
        r = StartRouter()
        r.register("link-", ok)
        with pytest.raises(ValueError, match="ambiguous"):
            r.register("link-v2-", refuses)

    @pytest.mark.asyncio
    async def test_dispatch_is_longest_prefix_first_not_registration_order(self):
        r = StartRouter()
        r.register("a-", refuses)
        r._handlers["ab-"] = ok  # bypass the guard to prove ordering, not the guard
        res = await r.dispatch(None, update("/start ab-x"))
        assert res.outcome == "did_it"


class TestPayloadParsing:
    def test_a_non_start_message_is_not_a_start(self):
        assert StartRouter.payload_of(update("/help")) is None

    def test_bare_start_is_empty_string_not_none(self):
        """'not a start command' and 'start with no payload' are different
        facts and must not collapse."""
        assert StartRouter.payload_of(update("/start")) == ""

    def test_group_suffixed_start_is_recognised(self):
        assert StartRouter.payload_of(update("/start@sd_bot link-x")) == "link-x"

    @pytest.mark.asyncio
    async def test_an_unattributable_start_is_refused_not_dispatched(self):
        """Every handler binds something to an identity; a /start we cannot
        attribute to a Telegram user is not routable."""
        router = StartRouter()
        router.register("link-", ok)
        u = {"message": {"text": "/start link-x", "chat": {"id": 1, "type": "private"}}}
        r = await router.dispatch(None, u)
        assert r.handled is False and r.outcome == "unattributable"


class TestTheHandlerContractLaneCRegistersAgainst:
    @pytest.mark.asyncio
    async def test_the_prefix_is_stripped_before_the_handler_sees_it(self):
        seen = {}

        async def h(conn, ctx):
            seen.update(payload=ctx.payload)
            return StartResult(outcome="x", handled=True, reply="y")

        r = StartRouter()
        r.register("inv-", h)
        await r.dispatch(None, update("/start inv-TOKEN123"))
        assert seen["payload"] == "TOKEN123", "handler saw the prefix"

    @pytest.mark.asyncio
    async def test_chat_type_reaches_the_handler_raw(self):
        """A group and a DM are different bindings. Telegram's own value is
        passed through un-mapped: translating it to ck_bindings_channel is the
        bindings writer's domain rule, and a copy here would drift."""
        seen = {}

        async def h(conn, ctx):
            seen.update(t=ctx.chat_type, chat=ctx.chat_id, uid=ctx.telegram_user_id)
            return StartResult(outcome="x", handled=True, reply="y")

        r = StartRouter()
        r.register("inv-", h)
        await r.dispatch(None, update("/start inv-t", ctype="supergroup", cid=-100))
        assert seen == {"t": "supergroup", "chat": "-100", "uid": "7"}
