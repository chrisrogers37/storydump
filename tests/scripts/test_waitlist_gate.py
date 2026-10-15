"""100: the waitlist is written by the API, not by the site.

`POST /public/waitlist` runs here as the production role (`svc_ingress`) on
the replayed advertised stream, so the grant, the policy, the CHECKs and the
bound parameters are the real ones: an address lands, a repeat answers the
same and stores nothing new, and the login that wrote it cannot read it back.
The list is read as the schema owner, the way the owner reads it.

129 (#1723) makes admission a runtime act, through four
`svc_membership` doors driven here as the production logins: the API lists
the waitlist and admits an address (`signup_admissions`), the worker stores
the claim link it has just emailed, and a claim spends it. One spot opens one
account, and the raw claim token rests in no table.

133 (#1740) has the account created at an admitted address use the
admission up as it is created, through a trigger whose function no role is
granted EXECUTE on. The owner's surfaces read the spot as used after that
account moves away, and an account that merely moves to an admitted address
uses nothing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg2
import psycopg2.errors
import pytest

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api import principal
from src.api.routes import public
from src.channels import telegram_waitlist_ping as waitlist_ping
from src.services.target import (
    email_sender,
    identity,
    sessions,
    signup_admissions,
    telegram_dispatch,
    vocabulary,
    waitlist,
)
from src.services.target.unit_of_work import asyncpg_url
from tests.scripts.conftest import (
    _scratch,
    as_user,
    execute,
    fetch_all,
    in_user_plane,
    ingress_engine,
    build_world,
    rows_holding,
    seed_workspace_chain,
)
from tests.src.api.conftest import api_client
from tests.src.services.target.conftest import RecordingSender

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner_dsn = build_world(db, owner_actor, admin_conn)
        yield {
            "owner": owner_dsn,
            "ingress": as_user(db, "svc_ingress"),
            "worker": as_user(db, "svc_worker"),
        }
    finally:
        gen.close()


def _rows(dsn: str, sql: str, params=()):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _send(world, *requests):
    """Each `(content, headers)` to the real route as `svc_ingress`."""

    async def main():
        async with api_client(world["ingress"]) as (client, _):
            return [
                await client.post("/public/waitlist", content=c, headers=h)
                for c, h in requests
            ]

    return asyncio.run(main())


JSON = {"content-type": "application/json"}


def _post(world, *bodies, headers=None):
    """Each body as JSON, with *headers* besides the content type. json.dumps
    escapes to ASCII, so a lone surrogate travels as `\\ud800` the way a
    browser's JSON would carry it."""
    sent = {**JSON, **(headers or {})}
    return _send(world, *((json.dumps(b), sent) for b in bodies))


def _entry(world, email):
    return _rows(
        world["owner"],
        "SELECT email, utm FROM waitlist_entries WHERE email = %s",
        (email,),
    )


class TestTheRoute:
    def test_an_address_lands_trimmed_and_lower_cased_with_its_campaign(self, world):
        (resp,) = _post(
            world,
            {
                "email": "  New.Person@Example.com ",
                "utm_source": "x" * 150,
                "other": "y",
            },
        )
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"status": "received"}
        assert _entry(world, "new.person@example.com") == [
            ("new.person@example.com", {"utm_source": "x" * 100})
        ]

    def test_a_repeat_answers_the_same_and_keeps_the_first_row(self, world):
        first, again = _post(
            world,
            {"email": "twice@example.com", "utm_campaign": "launch"},
            {"email": "Twice@example.com", "utm_campaign": "later"},
        )
        assert first.status_code == again.status_code == 202
        assert first.json() == again.json()
        assert _entry(world, "twice@example.com") == [
            ("twice@example.com", {"utm_campaign": "launch"})
        ]

    def test_a_campaign_value_the_database_cannot_hold_is_dropped(self, world):
        (resp,) = _post(
            world,
            {
                "email": "odd-campaign@example.com",
                "utm_source": "nul\x00here",
                "utm_medium": "lone\ud800",
                "utm_campaign": "kept",
            },
        )
        assert resp.status_code == 202, resp.text
        assert _entry(world, "odd-campaign@example.com") == [
            ("odd-campaign@example.com", {"utm_campaign": "kept"})
        ]

    def test_an_address_with_no_campaign_stores_none(self, world):
        (resp,) = _post(world, {"email": "plain@example.com"})
        assert resp.status_code == 202
        assert _entry(world, "plain@example.com") == [("plain@example.com", None)]

    @pytest.mark.parametrize(
        "email",
        [
            "",
            "no-at-sign",
            "a@nodot",
            "two words@example.com",
            "a" * 243 + "@example.com",
            "zero​width@example.com",
            "esc\x1b[0mx@example.com",
            "bidi\u2067x\u2069@example.com",
            "tag\U000e0041@example.com",
            "nul\x00@example.com",
            "lone\ud800@example.com",
            42,
            None,
        ],
    )
    def test_an_address_the_list_cannot_hold_is_a_400_and_stores_nothing(
        self, world, email
    ):
        # Only an over-long address, the NUL and the lone surrogate are
        # refused before the INSERT; every other case is the CHECK's refusal.
        before = _rows(world["owner"], "SELECT count(*) FROM waitlist_entries")
        (resp,) = _post(world, {"email": email})
        assert resp.status_code == 400
        assert resp.json()["reason"] == "invalid_email"
        assert _rows(world["owner"], "SELECT count(*) FROM waitlist_entries") == before

    def test_the_400_is_the_checks_refusal_not_a_python_rule(self, world):
        """The service keeps no copy of the address rule: an address it passes
        through is refused by `ck_waitlist_entries_email`, and the refusal it
        raises carries the database's error."""

        async def main():
            engine = create_async_engine(
                asyncpg_url(world["ingress"]), poolclass=NullPool
            )
            try:
                async with engine.begin() as conn:
                    with pytest.raises(waitlist.InvalidWaitlistEmail) as caught:
                        await waitlist.join(conn, "a@nodot")
                    # The savepoint kept the transaction usable.
                    await waitlist.join(conn, "after-refusal@example.com")
            finally:
                await engine.dispose()
            return caught.value

        refusal = asyncio.run(main())
        assert isinstance(refusal.__cause__, DBAPIError)
        assert _entry(world, "after-refusal@example.com") == [
            ("after-refusal@example.com", None)
        ]

    def test_the_limit_refuses_past_its_window(self, world, monkeypatch):
        monkeypatch.setattr(public, "WAITLIST_LIMIT", 2)
        monkeypatch.setattr(public, "WAITLIST_KEY_PREFIX", "waitlist-limit-test:")
        responses = _post(
            world,
            {"email": "limit1@example.com"},
            {"email": "limit2@example.com"},
            {"email": "limit3@example.com"},
        )
        assert [r.status_code for r in responses] == [202, 202, 429]
        assert _entry(world, "limit3@example.com") == []


#: Before `no_real_ping` replaces it for every test.
REAL_FROM_SETTINGS = waitlist_ping.from_settings


@pytest.fixture(autouse=True)
def no_real_ping(monkeypatch):
    """A developer's shell may hold the real bot: no test here sends."""
    monkeypatch.setattr(waitlist_ping, "from_settings", lambda *args: None)


#: Two operators who have linked Telegram (the first also Google), one who
#: has linked only Google, a person linked to Telegram who is no operator,
#: and a linked operator whose account is disabled.
LINKED_OP = "00000000-0000-4000-8000-0000000000a1"
GOOGLE_ONLY_OP = "00000000-0000-4000-8000-0000000000a2"
LINKED_OTHER = "00000000-0000-4000-8000-0000000000a3"
LINKED_OP_2 = "00000000-0000-4000-8000-0000000000a4"
DISABLED_OP = "00000000-0000-4000-8000-0000000000a5"


@pytest.fixture
def people(world):
    """The five, made once per database: user-plane rows the ping reads as
    `svc_ingress`."""
    conn = psycopg2.connect(world["owner"])
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id) VALUES (%s), (%s), (%s), (%s)"
                " ON CONFLICT DO NOTHING",
                (LINKED_OP, GOOGLE_ONLY_OP, LINKED_OTHER, LINKED_OP_2),
            )
            cur.execute(
                "INSERT INTO users (id, state) VALUES (%s, 'disabled')"
                " ON CONFLICT DO NOTHING",
                (DISABLED_OP,),
            )
            cur.execute(
                "INSERT INTO user_identities (user_id, provider, external_id)"
                " VALUES (%s, 'telegram', '5550001'), (%s, 'google', 'g-5550001'),"
                " (%s, 'google', 'g-5550002'), (%s, 'telegram', '5550003'),"
                " (%s, 'telegram', '5550004'), (%s, 'telegram', '5550005')"
                " ON CONFLICT DO NOTHING",
                (
                    LINKED_OP,
                    LINKED_OP,
                    GOOGLE_ONLY_OP,
                    LINKED_OTHER,
                    LINKED_OP_2,
                    DISABLED_OP,
                ),
            )
    finally:
        conn.close()


class _Bot:
    """The bot's stand-in: each signup's send as (chat, address, whether the
    address's row was already committed when it went); any other text as
    (chat, text)."""

    def __init__(self, world):
        self.world, self.sent = world, []

    async def send_text(self, chat_id, text, *, reply_markup=None):
        if "Email: " not in text:
            self.sent.append((chat_id, text))
            return "1"
        address = text.split("Email: ")[1].split("\n")[0]
        self.sent.append((chat_id, address, bool(_entry(self.world, address))))
        return "1"


@pytest.fixture
def ping_for(world, people, monkeypatch):
    """The real ping and its real read of linked operators, on the route's
    engine, with *operators* as `OPS_USER_IDS` and a stand-in bot."""

    def install(*operators):
        bot = _Bot(world)
        monkeypatch.setattr(principal.settings, "OPS_USER_IDS", ",".join(operators))
        monkeypatch.setattr(
            waitlist_ping,
            "from_settings",
            lambda _bot, *rest: REAL_FROM_SETTINGS(bot, *rest),
        )
        return bot

    return install


@pytest.fixture
def pinged(ping_for):
    """The bot, with one linked operator and one linked only to Google."""
    return ping_for(LINKED_OP, GOOGLE_ONLY_OP)


class TestTheAdminPing:
    """Every accepted address, a repeat too, is one message to each linked
    operator after the commit; nothing the route refuses is."""

    def test_it_goes_to_each_linked_operator_and_no_one_else(self, world, ping_for):
        bot = ping_for(LINKED_OP, GOOGLE_ONLY_OP, LINKED_OP_2, DISABLED_OP)
        (resp,) = _post(world, {"email": "ops-only@example.com"})
        assert resp.status_code == 202
        assert bot.sent == [
            ("5550001", "ops-only@example.com", True),
            ("5550004", "ops-only@example.com", True),
        ]

    def test_a_failed_read_costs_the_message_never_the_signup(
        self, world, ping_for, monkeypatch, caplog
    ):
        bot = ping_for(LINKED_OP)

        async def broken(executor, user_ids):  # a statement PostgreSQL refuses
            await executor.execute(text("SELECT 1/0"))

        monkeypatch.setattr(identity, "telegram_ids_for", broken)
        with caplog.at_level(logging.ERROR, logger=waitlist_ping.__name__):
            (resp,) = _post(world, {"email": "read-failed@example.com"})
        assert resp.status_code == 202
        assert _entry(world, "read-failed@example.com") != []
        assert bot.sent == []
        assert "the operators could not be read" in caplog.text

    def test_no_linked_operator_sends_nothing_and_says_why(
        self, world, ping_for, caplog
    ):
        bot = ping_for(GOOGLE_ONLY_OP)
        with caplog.at_level(logging.WARNING, logger=waitlist_ping.__name__):
            (resp,) = _post(world, {"email": "nobody-linked@example.com"})
        assert resp.status_code == 202
        assert bot.sent == []
        assert "no one in OPS_USER_IDS has linked Telegram" in caplog.text

    def test_an_accepted_address_is_pinged_as_stored_after_the_commit(
        self, world, pinged
    ):
        (resp,) = _post(world, {"email": "  Pinged@Example.com "})
        assert resp.status_code == 202
        assert pinged.sent == [("5550001", "pinged@example.com", True)]

    def test_a_repeat_is_pinged_again(self, world, pinged):
        responses = _post(
            world,
            {"email": "ping-twice@example.com"},
            {"email": "ping-twice@example.com"},
        )
        assert [r.status_code for r in responses] == [202, 202]
        assert pinged.sent == [("5550001", "ping-twice@example.com", True)] * 2

    def test_a_refused_address_is_not_pinged(self, world, pinged):
        (resp,) = _post(world, {"email": "no-at-sign"})
        assert resp.status_code == 400
        assert pinged.sent == []

    def test_a_full_ceiling_is_not_pinged_and_alerts_once_with_no_address(
        self, world, pinged, monkeypatch, caplog
    ):
        monkeypatch.setattr(public.settings, "WAITLIST_SITE_SECRET", SECRET)
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_LIMIT", 1)
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_KEY", "accepted-ping-test")
        # The route's shared logger does not propagate; caplog listens at root.
        monkeypatch.setattr(public.logger, "propagate", True)
        alerted = []
        send = waitlist_ping.WaitlistPing.send

        async def spy(ping, text, *, alert=False, **rest):
            alerted.append(alert)
            await send(ping, text, alert=alert, **rest)

        monkeypatch.setattr(waitlist_ping.WaitlistPing, "send", spy)
        caplog.set_level(logging.INFO)
        responses = _post(
            world,
            {"email": "ping-ceiling-0@example.com"},
            {"email": "ping-ceiling-1@example.com"},
            {"email": "ping-ceiling-2@example.com"},
            headers=_from_site("198.51.100.90"),
        )
        assert [r.status_code for r in responses] == [202, 429, 429]
        # The signup's own ping, then one alert for the two refusals: the
        # second refusal falls inside the first's notice window.
        alert = (
            "Waitlist signups hit their shared limit of 1 a minute, and are"
            " being turned away as busy: 1 on this server so far."
        )
        assert pinged.sent == [
            ("5550001", "ping-ceiling-0@example.com", True),
            ("5550001", alert),
        ]
        warned = [
            r.getMessage() for r in caplog.records if "turned away" in r.getMessage()
        ]
        assert warned == [f"waitlist: {alert}"]
        # The signup's ping is a plain send; the notice is an alert, which a
        # queue full of signup pings cannot drop.
        assert alerted == [False, True]
        assert "198.51.100.90" not in caplog.text
        assert "ping-ceiling-1" not in caplog.text


class TestTheDoor:
    """Only the site's server may call: no browser, no other body type, no
    oversized body, and a burst cannot take the pool."""

    @pytest.mark.parametrize(
        "headers",
        [
            {**JSON, "origin": "https://elsewhere.example.com"},
            {**JSON, "sec-fetch-site": "cross-site"},
            {**JSON, "sec-fetch-site": "same-origin"},
        ],
    )
    def test_a_request_a_browser_made_is_refused(self, world, headers):
        body = json.dumps({"email": "browser@example.com"})
        (resp,) = _send(world, (body, headers))
        assert (resp.status_code, resp.json()["reason"]) == (403, "browser")
        assert _entry(world, "browser@example.com") == []

    def test_a_body_that_is_not_json_by_type_is_refused(self, world):
        body = json.dumps({"email": "plain-text@example.com"})
        (resp,) = _send(world, (body, {"content-type": "text/plain"}))
        assert (resp.status_code, resp.json()["reason"]) == (415, "not_json")
        assert _entry(world, "plain-text@example.com") == []

    def test_an_oversized_body_is_a_413_declared_or_streamed(self, world):
        big = json.dumps({"email": "big@example.com", "pad": "x" * 9000})

        async def chunks():  # no Content-Length: the cap counts what arrives
            yield big.encode()

        declared, streamed = _send(world, (big, JSON), (chunks(), JSON))
        assert (declared.status_code, declared.json()["reason"]) == (413, "too_large")
        assert (streamed.status_code, streamed.json()["reason"]) == (413, "too_large")
        assert _entry(world, "big@example.com") == []

    def test_a_malformed_body_still_spends_the_counter(self, world, monkeypatch):
        monkeypatch.setattr(public, "WAITLIST_LIMIT", 1)
        monkeypatch.setattr(public, "WAITLIST_KEY_PREFIX", "waitlist-malformed:")
        bad, good = _send(
            world, ("[" * 2000, JSON), (json.dumps({"email": "m@example.com"}), JSON)
        )
        assert (bad.status_code, bad.json()["reason"]) == (400, "not_json")
        assert good.status_code == 429

    def test_no_free_slot_is_a_429_and_stores_nothing(self, world, monkeypatch):
        async def full(self, address):
            return False

        monkeypatch.setattr(public.WaitlistSlots, "acquire", full)
        (resp,) = _post(world, {"email": "burst@example.com"})
        assert (resp.status_code, resp.json()["reason"]) == (429, "busy")
        assert _entry(world, "burst@example.com") == []

    def test_the_media_type_is_matched_in_any_case(self, world):
        body = json.dumps({"email": "Case-Type@example.com"})
        (resp,) = _send(
            world, (body, {"content-type": "Application/JSON; charset=utf-8"})
        )
        assert resp.status_code == 202, resp.text

    def test_every_answer_gives_its_slot_back(self, world, monkeypatch):
        """Accepted, refused by the address rule, malformed and over the
        counter's limit: the app's slots are all free afterwards."""
        monkeypatch.setattr(public, "WAITLIST_KEY_PREFIX", "waitlist-slots:")
        monkeypatch.setattr(public, "WAITLIST_LIMIT", 3)

        async def main():
            async with api_client(world["ingress"]) as (client, _):
                answers = [
                    (
                        await client.post("/public/waitlist", content=c, headers=JSON)
                    ).status_code
                    for c in (
                        json.dumps({"email": "slots@example.com"}),
                        json.dumps({"email": "a@nodot"}),
                        "[" * 2000,
                        json.dumps({"email": "over@example.com"}),
                    )
                ]
                slots = client._transport.app.state.waitlist_slots
                return answers, slots.free._value, slots.by_address

        answers, free, held = asyncio.run(main())
        assert answers == [202, 400, 400, 429]
        assert (free, held) == (public.WAITLIST_MAX_IN_FLIGHT, {})


class TestTheSlots:
    """The per-process slots, without a database: one address cannot take
    them all, a request waits its turn, and every exit gives its share back."""

    def test_one_address_cannot_starve_another(self, monkeypatch):
        monkeypatch.setattr(public, "WAITLIST_SLOT_WAIT_SECONDS", 0.05)

        async def main():
            slots = public.WaitlistSlots()
            held = [await slots.acquire("10.0.0.1") for _ in range(2)]
            third = await slots.acquire("10.0.0.1")
            others = [await slots.acquire(f"10.0.0.{i}") for i in (2, 3)]
            return held, third, others

        assert asyncio.run(main()) == ([True, True], False, [True, True])

    def test_one_address_waits_for_its_own_share(self, monkeypatch):
        """The site's server is one address: its third signup at once waits
        for one of its two to finish instead of being refused."""
        monkeypatch.setattr(public, "WAITLIST_SLOT_WAIT_SECONDS", 1.0)

        async def main():
            slots = public.WaitlistSlots()
            for _ in range(2):
                assert await slots.acquire("10.0.0.9")
            third = asyncio.create_task(slots.acquire("10.0.0.9"))
            await asyncio.sleep(0.01)
            slots.release("10.0.0.9")
            return await third, slots.by_address["10.0.0.9"].queued

        assert asyncio.run(main()) == (True, 2)

    def test_one_address_cannot_pile_up_waiters(self, monkeypatch):
        monkeypatch.setattr(public, "WAITLIST_SLOT_WAIT_SECONDS", 5.0)

        async def main():
            slots = public.WaitlistSlots()
            waiters = [
                asyncio.create_task(slots.acquire("10.0.0.7"))
                for _ in range(public.WAITLIST_MAX_QUEUED_PER_ADDRESS)
            ]
            await asyncio.sleep(0.01)
            # Refused at once, not after the wait budget runs out.
            over = await asyncio.wait_for(slots.acquire("10.0.0.7"), 0.5)
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            return over

        assert asyncio.run(main()) is False

    def test_a_waiter_holds_its_own_share_not_a_global_slot(self, monkeypatch):
        """The address's share is taken before a global slot, so a hot
        address's extra requests leave the global slots to everyone else."""
        monkeypatch.setattr(public, "WAITLIST_SLOT_WAIT_SECONDS", 1.0)

        async def main():
            slots = public.WaitlistSlots()
            for _ in range(2):
                assert await slots.acquire("10.0.0.5")
            extra = asyncio.create_task(slots.acquire("10.0.0.5"))
            await asyncio.sleep(0.01)
            free = slots.free._value
            other = await asyncio.wait_for(slots.acquire("10.0.0.6"), 0.1)
            extra.cancel()
            await asyncio.gather(extra, return_exceptions=True)
            return free, other

        assert asyncio.run(main()) == (2, True)

    def test_a_timed_out_wait_gives_back_the_share_it_took(self, monkeypatch):
        """A wait that took its address's share and then timed out on the
        global slots returns the share: the address still has two."""
        monkeypatch.setattr(public, "WAITLIST_SLOT_WAIT_SECONDS", 0.05)

        async def main():
            slots = public.WaitlistSlots()
            assert await slots.acquire("site")
            for other in ("x", "y", "z"):
                assert await slots.acquire(other)
            second = await slots.acquire("site")
            slots.release("x")
            return second, await slots.acquire("site")

        assert asyncio.run(main()) == (False, True)

    def test_a_request_waits_for_a_slot_then_gets_it(self, monkeypatch):
        monkeypatch.setattr(public, "WAITLIST_SLOT_WAIT_SECONDS", 1.0)

        async def main():
            slots = public.WaitlistSlots()
            for i in range(public.WAITLIST_MAX_IN_FLIGHT):
                assert await slots.acquire(f"10.0.1.{i}")
            waiter = asyncio.create_task(slots.acquire("10.0.2.1"))
            await asyncio.sleep(0.01)
            slots.release("10.0.1.0")
            return await waiter

        assert asyncio.run(main()) is True

    @staticmethod
    async def _full(slots):
        for i in range(public.WAITLIST_MAX_IN_FLIGHT):
            assert await slots.acquire(f"10.0.1.{i}")

    def test_a_timeout_gives_the_share_back(self, monkeypatch):
        monkeypatch.setattr(public, "WAITLIST_SLOT_WAIT_SECONDS", 0.05)

        async def main():
            slots = public.WaitlistSlots()
            await self._full(slots)
            return await slots.acquire("10.0.3.1"), dict(slots.by_address)

        timed_out, held = asyncio.run(main())
        assert timed_out is False and "10.0.3.1" not in held

    def test_a_cancel_gives_the_share_back(self, monkeypatch):
        monkeypatch.setattr(public, "WAITLIST_SLOT_WAIT_SECONDS", 5.0)

        async def main():
            slots = public.WaitlistSlots()
            await self._full(slots)
            waiter = asyncio.create_task(slots.acquire("10.0.3.2"))
            await asyncio.sleep(0.01)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            for i in range(public.WAITLIST_MAX_IN_FLIGHT):
                slots.release(f"10.0.1.{i}")
            return slots.free._value, slots.by_address

        assert asyncio.run(main()) == (public.WAITLIST_MAX_IN_FLIGHT, {})


SECRET = "test-site-secret-not-real"


def _from_site(visitor=None, secret=SECRET):
    headers = {public.SITE_SECRET_HEADER: secret}
    if visitor is not None:
        headers[public.VISITOR_IP_HEADER] = visitor
    return headers


@pytest.fixture
def acquired(monkeypatch):
    """The addresses the route's slots were acquired for, in order."""
    seen = []
    acquire = public.WaitlistSlots.acquire

    async def spy(slots, address):
        seen.append(address)
        return await acquire(slots, address)

    monkeypatch.setattr(public.WaitlistSlots, "acquire", spy)
    return seen


@pytest.fixture(autouse=True)
def one_window(monkeypatch):
    """Every counter in one window, so a minute boundary cannot reset a count
    between a test's posts."""
    pinned = principal.rate_counters.window_start(
        datetime.now(timezone.utc), principal.PREAUTH_WINDOW_SECONDS
    )
    monkeypatch.setattr(
        principal.rate_counters, "window_start", lambda now, seconds: pinned
    )


class TestTheSiteSecret:
    """`WAITLIST_SITE_SECRET`: unset, the headers change nothing; set, a call
    without it is refused and each visitor the site names has a limit of their
    own."""

    @pytest.fixture
    def armed(self, monkeypatch):
        monkeypatch.setattr(public.settings, "WAITLIST_SITE_SECRET", SECRET)
        monkeypatch.setattr(public, "WAITLIST_VISITOR_LIMIT", 2)
        monkeypatch.setattr(
            public, "WAITLIST_VISITOR_KEY_PREFIX", "waitlist-visitor-test:"
        )

    def test_unset_the_headers_change_nothing(self, world, monkeypatch):
        monkeypatch.setattr(public.settings, "WAITLIST_SITE_SECRET", None)
        monkeypatch.setattr(public, "WAITLIST_LIMIT", 2)
        monkeypatch.setattr(public, "WAITLIST_KEY_PREFIX", "waitlist-unset-test:")
        responses = _post(
            world,
            {"email": "unset1@example.com"},
            {"email": "unset2@example.com"},
            {"email": "unset3@example.com"},
            headers=_from_site("198.51.100.1", secret="anything"),
        )
        # The shared counter, though the call names a visitor; without the
        # secret it is the caller's own, so its 429 says no more.
        assert [r.status_code for r in responses] == [202, 202, 429]
        assert "reason" not in responses[-1].json()

    @pytest.mark.parametrize("headers", [{}, _from_site("198.51.100.2", "wrong")])
    def test_set_a_call_without_it_is_refused_and_stores_nothing(
        self, world, armed, headers
    ):
        (resp,) = _post(world, {"email": "no-secret@example.com"}, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["reason"] == "not_site"
        assert _entry(world, "no-secret@example.com") == []

    def test_set_each_visitor_has_a_limit_of_their_own(self, world, armed):
        first = _post(
            world,
            {"email": "visitor-a1@example.com"},
            {"email": "visitor-a2@example.com"},
            {"email": "visitor-a3@example.com"},
            headers=_from_site("198.51.100.3"),
        )
        other = _post(
            world,
            {"email": "visitor-b1@example.com"},
            headers=_from_site("198.51.100.4"),
        )
        assert [r.status_code for r in first] == [202, 202, 429]
        # Their own limit, not one all visitors share: no "full", no "busy".
        assert first[-1].json() == {"detail": public.TOO_MANY}
        assert [r.status_code for r in other] == [202]
        assert _entry(world, "visitor-b1@example.com") != []

    @pytest.mark.parametrize(
        "n, secret, headers, key",
        [
            # Unset: the peer, whatever the site names.
            (1, None, _from_site("198.51.100.5"), "127.0.0.1"),
            # Set and matched: the visitor, an IPv6 one by its /64.
            (2, SECRET, _from_site("198.51.100.5"), "198.51.100.5"),
            (3, SECRET, _from_site("2001:db8:9:9::5"), "2001:db8:9:9::/64"),
        ],
    )
    def test_the_slot_share_is_keyed_like_the_counter(
        self, world, monkeypatch, acquired, n, secret, headers, key
    ):
        monkeypatch.setattr(public.settings, "WAITLIST_SITE_SECRET", secret)
        (resp,) = _post(world, {"email": f"slot-{n}@example.com"}, headers=headers)
        assert resp.status_code == 202, resp.text
        assert acquired == [key]

    def test_a_blank_secret_is_unset(self, world, monkeypatch):
        monkeypatch.setattr(public.settings, "WAITLIST_SITE_SECRET", " \n")
        (resp,) = _post(world, {"email": "blank-secret@example.com"})
        assert resp.status_code == 202, resp.text

    def test_set_the_secret_is_compared_without_surrounding_whitespace(
        self, world, armed, monkeypatch
    ):
        monkeypatch.setattr(public.settings, "WAITLIST_SITE_SECRET", f" {SECRET}\n")
        (resp,) = _post(
            world, {"email": "trimmed@example.com"}, headers=_from_site("198.51.100.6")
        )
        assert resp.status_code == 202, resp.text

    def test_set_a_refusal_reads_no_body(self, world, armed):
        read = []

        async def body():
            read.append(True)
            yield json.dumps({"email": "unread@example.com"}).encode()

        (resp,) = _send(world, (body(), JSON))
        assert (resp.status_code, resp.json()["reason"]) == (403, "not_site")
        assert read == []

    def test_set_accepted_signups_share_one_ceiling(self, world, armed, monkeypatch):
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_LIMIT", 2)
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_KEY", "accepted-ceiling-test")
        # Refused before the ceiling, so they spend none of it.
        refused = ("[]", '{"email": "not-an-email"}')
        accepted = tuple(
            json.dumps({"email": f"ceiling-{i}@example.com"}) for i in range(3)
        )
        responses = [
            _send(world, (body, {**JSON, **_from_site(f"198.51.100.{20 + i}")}))[0]
            for i, body in enumerate(refused + accepted)
        ]
        assert [r.status_code for r in responses] == [400, 400, 202, 202, 429]
        assert responses[-1].json() == {"detail": public.TOO_MANY, "reason": "full"}
        assert _entry(world, "ceiling-1@example.com") != []
        assert _entry(world, "ceiling-2@example.com") == []

    def test_set_a_full_ceiling_answers_a_repeat_as_it_answers_a_new_address(
        self, world, armed, monkeypatch
    ):
        """No oracle: the ceiling is spent whether the address is new or
        already listed, so at the ceiling both get the same answer."""
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_LIMIT", 1)
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_KEY", "accepted-oracle-test")
        (listed,) = _post(
            world,
            {"email": "oracle-0@example.com"},
            headers=_from_site("198.51.100.40"),
        )
        assert listed.status_code == 202
        (repeat,) = _post(
            world,
            {"email": "oracle-0@example.com"},
            headers=_from_site("198.51.100.41"),
        )
        (new,) = _post(
            world,
            {"email": "oracle-1@example.com"},
            headers=_from_site("198.51.100.42"),
        )
        assert (repeat.status_code, repeat.json()) == (new.status_code, new.json())
        assert repeat.json() == {"detail": public.TOO_MANY, "reason": "full"}
        assert _entry(world, "oracle-1@example.com") == []

    @pytest.mark.parametrize("visitor", [None, "not-an-address"])
    def test_set_the_fallback_spends_the_ceiling_too(
        self, world, armed, monkeypatch, visitor
    ):
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_LIMIT", 1)
        monkeypatch.setattr(
            public, "WAITLIST_ACCEPTED_KEY", f"accepted-fallback-test:{visitor}"
        )
        monkeypatch.setattr(public, "WAITLIST_KEY_PREFIX", f"fallback-test:{visitor}:")
        tag = "none" if visitor is None else "bad"
        responses = _post(
            world,
            {"email": f"fallback-{tag}-0@example.com"},
            {"email": f"fallback-{tag}-1@example.com"},
            headers=_from_site(visitor),
        )
        assert [r.status_code for r in responses] == [202, 429]
        assert _entry(world, f"fallback-{tag}-1@example.com") == []

    def test_set_a_full_ceiling_keeps_the_visitors_own_spend(
        self, world, armed, monkeypatch
    ):
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_LIMIT", 1)
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_KEY", "accepted-keep-test")
        site = _from_site("198.51.100.30")
        (first,) = _post(world, {"email": "keep-0@example.com"}, headers=site)
        (full,) = _post(world, {"email": "keep-1@example.com"}, headers=site)
        assert (first.status_code, full.status_code) == (202, 429)
        assert _entry(world, "keep-1@example.com") == []
        # With room in the ceiling again, the visitor has spent both of their
        # two: the refused attempt still counted against them.
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_LIMIT", 100)
        (third,) = _post(world, {"email": "keep-2@example.com"}, headers=site)
        assert third.status_code == 429
        assert _entry(world, "keep-2@example.com") == []

    def test_a_direct_ipv6_caller_is_counted_by_its_64(
        self, world, monkeypatch, acquired
    ):
        monkeypatch.setattr(public.settings, "WAITLIST_SITE_SECRET", None)
        # The real client_ip, as if the peer were that address: the /64 step
        # lives there (`principal.address_key`), so the stub must not skip it.
        peer = SimpleNamespace(client=SimpleNamespace(host="2001:db8:7:7::42"))
        real = public.client_ip
        monkeypatch.setattr(public, "client_ip", lambda request: real(peer))
        (resp,) = _post(world, {"email": "direct-v6@example.com"})
        assert resp.status_code == 202, resp.text
        assert acquired == ["2001:db8:7:7::/64"]
        counted = _rows(
            world["owner"],
            "SELECT key FROM rate_counters WHERE key = 'waitlist:2001:db8:7:7::/64'",
        )
        assert counted != []

    def test_set_without_a_usable_visitor_the_shared_counter_serves(
        self, world, armed, monkeypatch, caplog
    ):
        monkeypatch.setattr(public.logger, "propagate", True)
        caplog.set_level(logging.WARNING)
        monkeypatch.setattr(public, "WAITLIST_LIMIT", 1)
        monkeypatch.setattr(public, "WAITLIST_KEY_PREFIX", "waitlist-novisitor-test:")
        monkeypatch.setattr(public, "WAITLIST_ACCEPTED_KEY", "accepted-novisitor-test")
        responses = _post(
            world,
            {"email": "novisitor1@example.com"},
            {"email": "novisitor2@example.com"},
            headers=_from_site("not-an-address"),
        )
        # Every visitor without an address shares it, so it is full, not theirs.
        assert [r.status_code for r in responses] == [202, 429]
        assert responses[-1].json()["reason"] == "full"
        # Its own notice, naming its own limit, not the accepted signups'.
        assert [r.getMessage() for r in caplog.records] == [
            "waitlist: the site sent no usable visitor address",
            "waitlist: the site sent no usable visitor address",
            "waitlist: Waitlist calls without a visitor address hit their shared"
            " limit of 1 a minute, and are being turned away as busy: 1 on this"
            " server so far.",
        ]


class TestTheLogin:
    def test_ingress_can_add_and_cannot_read_change_or_remove(self, world):
        conn = psycopg2.connect(world["ingress"])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO waitlist_entries (email) VALUES ('direct@example.com')"
                )
                for sql in (
                    "SELECT count(*) FROM waitlist_entries",
                    "UPDATE waitlist_entries SET utm = NULL",
                    "DELETE FROM waitlist_entries",
                ):
                    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                        cur.execute(sql)
        finally:
            conn.close()

    def test_the_checks_hold_for_a_direct_insert(self, world):
        conn = psycopg2.connect(world["ingress"])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for email, utm in (
                    ("Upper@example.com", None),
                    ("a" * 244 + "@example.co", None),  # 255 characters
                    ("ok@example.com", '"a string"'),
                    ("big@example.com", '{"k": "' + "x" * 2100 + '"}'),
                ):
                    with pytest.raises(psycopg2.errors.CheckViolation):
                        cur.execute(
                            "INSERT INTO waitlist_entries (email, utm)"
                            " VALUES (%s, CAST(%s AS jsonb))",
                            (email, utm),
                        )
        finally:
            conn.close()


# --- 129 (#1723): admitting from the waitlist ------------------------------

#: The base of the claim link an admission's email carries.
BASE = "https://app.example.test/claim/"


def _join(world, email, ago="0"):
    """*email* on the waitlist, joined *ago* before now (negative: ahead)."""
    execute(
        world["owner"],
        "INSERT INTO waitlist_entries (email, joined_at)"
        " VALUES (%s, now() - CAST(%s AS interval))",
        (email, ago),
    )


def _hold(world, email):
    """An account created with *email* as its primary email. Its creation
    uses up an unconsumed admission at that address (133)."""
    execute(world["owner"], "INSERT INTO users (primary_email) VALUES (%s)", (email,))


def _move_into(world, email):
    """An account created with no address that then takes *email*, as a
    returning person's refresh does (#1579). It holds the address without
    having been created at it, so it uses no admission up (133 is birth only)."""
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
            (user,) = cur.fetchone()
            cur.execute(
                "UPDATE users SET primary_email = %s WHERE id = %s", (email, user)
            )
        conn.commit()
    finally:
        conn.close()


#: The two ways an address comes to be held: an account created at it, and an
#: account that moved to it.
HOLDS = {"created": _hold, "moved": _move_into}


def _ingress(world, fn):
    """*fn(conn)* in one committed user-plane transaction as `svc_ingress`."""
    return asyncio.run(in_user_plane(world["ingress"], fn))


def _admit(world, email, *, resend=False, claim_base=BASE):
    return _ingress(
        world,
        lambda c: signup_admissions.admit(
            c, email=email, claim_base=claim_base, resend=resend
        ),
    )


def _list(world, ref=None):
    return _ingress(world, lambda c: signup_admissions.waitlist(c, ref=ref))


def _claim(world, token):
    return _ingress(world, lambda c: signup_admissions.claim(c, token=token))


def _store(world, email, token):
    """The worker's store as `execute_send_email` makes it: a system session
    of its own, as `svc_worker`."""

    async def main():
        async with ingress_engine(world["worker"]) as engine:
            async with email_sender.system_session(engine) as session:
                who = (await session.execute(text("SELECT current_user"))).scalar()
                assert who == "svc_worker", who
                return await signup_admissions.store_link(
                    session, to=email, token=token
                )

    return asyncio.run(main())


def _admission(world, email):
    """The admission row as the owner reads it, or None."""
    rows = fetch_all(
        world["owner"],
        "SELECT email, admitted_at, emailed_at, claimed_at, claim_token_hash"
        " FROM signup_admissions WHERE email = %s",
        (email,),
    )
    return rows[0] if rows else None


def _key(email):
    return f"email:admission:{signup_admissions.ref(email)}"


def _emails(world, email):
    """The states of the `send_email` jobs queued for *email*'s admission,
    oldest first."""
    rows = _rows(
        world["owner"],
        "SELECT state FROM jobs WHERE serialization_key = %s ORDER BY created_at, id",
        (_key(email),),
    )
    return [state for (state,) in rows]


def _signup_admitted(world, email):
    return _rows(world["ingress"], "SELECT fn_signup_admitted(%s)", (email,))[0][0]


def _linked(world, email):
    """*email* admitted and its link stored, as the worker stores it after a
    send; returns the link's token."""
    assert _admit(world, email) == "admitted"
    token = sessions.new_token()
    assert _store(world, email, token) is True
    return token


class TestTheList:
    """`fn_waitlist` through `signup_admissions.waitlist`, as `svc_ingress`:
    the waitlist and the admissions off it, newest first, each with its
    state."""

    def test_entries_and_admissions_off_it_newest_first_with_their_state(
        self, world, monkeypatch
    ):
        """Signed up has three sources, one row each. `list-claimed`'s link was
        used. `list-created`'s account was created at the address, which uses
        the admission up (133). `list-held`'s account moved onto the address,
        which uses nothing (133 is birth only), so only the door's arm that
        reads who holds an address can call it signed up."""
        # Other tests here add entries too: read them all, then keep this one's.
        monkeypatch.setattr(signup_admissions, "LIST_LIMIT", 10_000)
        for email, ago in (
            ("list-waiting@example.com", "5 days"),
            ("list-admitted@example.com", "4 days"),
            ("list-emailed@example.com", "3 days"),
            ("list-claimed@example.com", "2 days"),
            ("list-created@example.com", "36 hours"),
            ("list-held@example.com", "1 day"),
        ):
            _join(world, email, ago)
        for email in (
            "list-admitted@example.com",
            "list-created@example.com",
            "list-held@example.com",
            "list-off@example.com",
        ):
            assert _admit(world, email) == "admitted"
        _linked(world, "list-emailed@example.com")
        assert _claim(world, _linked(world, "list-claimed@example.com")) is True
        _hold(world, "list-created@example.com")
        _move_into(world, "list-held@example.com")
        assert _admission(world, "list-created@example.com")["claimed_at"] is not None
        assert _admission(world, "list-held@example.com")["claimed_at"] is None

        mine = [r for r in _list(world) if r["email"].startswith("list-")]

        assert [(r["email"], r["state"], r["on_waitlist"]) for r in mine] == [
            ("list-off@example.com", "admitted", False),
            ("list-held@example.com", "signed_up", True),
            ("list-created@example.com", "signed_up", True),
            ("list-claimed@example.com", "signed_up", True),
            ("list-emailed@example.com", "emailed", True),
            ("list-admitted@example.com", "admitted", True),
            ("list-waiting@example.com", "waiting", True),
        ]
        off, *_, emailed, admitted, waiting = mine
        assert off["joined_at"] is None and off["admitted_at"] is not None
        assert emailed["emailed_at"] is not None and admitted["emailed_at"] is None
        assert (waiting["admitted_at"], waiting["emailed_at"]) == (None, None)
        assert set(off) == {
            "email",
            "on_waitlist",
            "joined_at",
            "state",
            "admitted_at",
            "emailed_at",
        }

    def test_the_limit_holds(self, world, monkeypatch):
        # Joined in the future, so these three are the newest rows there are.
        for email, ahead in (
            ("limit-a@example.com", "1000 days"),
            ("limit-b@example.com", "1001 days"),
            ("limit-c@example.com", "999 days"),
        ):
            _join(world, email, f"-{ahead}")
        monkeypatch.setattr(signup_admissions, "LIST_LIMIT", 2)
        assert [r["email"] for r in _list(world)] == [
            "limit-b@example.com",
            "limit-a@example.com",
        ]

    def test_a_ref_finds_exactly_its_row_so_python_and_sql_hash_alike(self, world):
        """The ref is `signup_admissions.ref`'s Python hash, and the door's
        `p_ref` filter is SQL's: one row back means they are one hash."""
        _join(world, "ref-entry@example.com")
        _join(world, "ref-other@example.com")
        assert _admit(world, "ref-off@example.com") == "admitted"
        for email, state in (
            ("ref-entry@example.com", "waiting"),
            ("ref-off@example.com", "admitted"),
        ):
            (row,) = _list(world, ref=signup_admissions.ref(email))
            assert (row["email"], row["state"]) == (email, state)
        assert _list(world, ref="0" * signup_admissions.REF_HEX) == []


class TestAdmit:
    """`signup_admissions.admit` as `svc_ingress`, through `fn_signup_admit`:
    one admission, one email, and the job commits with the row."""

    def test_an_entry_is_admitted_with_one_row_and_one_send_email_job(self, world):
        email = "admit-entry@example.com"
        _join(world, email)

        assert _admit(world, email) == "admitted"

        row = _admission(world, email)
        assert row["admitted_at"] is not None
        assert (row["emailed_at"], row["claimed_at"], row["claim_token_hash"]) == (
            None,
            None,
            None,
        )
        [job] = fetch_all(
            world["owner"],
            "SELECT kind, workspace_id, lane, state, max_attempts, payload"
            " FROM jobs WHERE serialization_key = %s",
            (_key(email),),
        )
        assert job == {
            "kind": "send_email",
            "workspace_id": None,
            "lane": "bulk",
            "state": "ready",
            "max_attempts": 5,
            "payload": {
                "v": 1,
                "to": email,
                "template": "admission",
                "params": {"address": email, "claim_base": BASE},
            },
        }

    def test_a_rollback_leaves_neither_the_row_nor_its_job(self, world):
        email = "admit-rolled-back@example.com"
        _join(world, email)

        class _Abort(Exception):
            pass

        async def admit_then_abort(conn):
            assert (
                await signup_admissions.admit(conn, email=email, claim_base=BASE)
                == "admitted"
            )
            raise _Abort

        with pytest.raises(_Abort):
            _ingress(world, admit_then_abort)
        assert _admission(world, email) is None
        assert _emails(world, email) == []

    def test_a_second_admit_is_admitted_before_and_queues_nothing(self, world):
        email = "admit-twice@example.com"
        _join(world, email)
        assert _admit(world, email) == "admitted"
        assert _admit(world, email) == "admitted_before"
        assert len(_emails(world, email)) == 1

    def test_mixed_case_matches(self, world):
        assert _admit(world, "  Admit-Mixed@Example.COM ") == "admitted"
        assert _admit(world, "admit-mixed@example.com") == "admitted_before"
        assert _admission(world, "admit-mixed@example.com") is not None
        assert len(_emails(world, "admit-mixed@example.com")) == 1

    def test_an_address_off_the_waitlist_is_admitted_and_listed_unjoined(self, world):
        email = "admit-off-list@example.com"
        assert _admit(world, email) == "admitted"
        (row,) = _list(world, ref=signup_admissions.ref(email))
        assert (row["email"], row["on_waitlist"], row["joined_at"]) == (
            email,
            False,
            None,
        )
        assert len(_emails(world, email)) == 1

    @pytest.mark.parametrize(
        "email",
        [
            "no-at-sign.example.com",
            "two words@example.com",
            "tab\tinside@example.com",
            "zero​width@example.com",
        ],
    )
    def test_an_address_the_check_refuses_writes_nothing_and_the_caller_goes_on(
        self, world, email
    ):
        """No Python copy of the address rule: these pass the length bound and
        reach the door, whose CHECK refuses them under the savepoint, and the
        caller's transaction admits the next address."""
        after = f"after-{signup_admissions.ref(email)[:8]}@example.com"

        async def both(conn):
            refused = await signup_admissions.admit(conn, email=email, claim_base=BASE)
            admitted = await signup_admissions.admit(conn, email=after, claim_base=BASE)
            return refused, admitted

        assert _ingress(world, both) == ("invalid_email", "admitted")
        assert _admission(world, email.lower()) is None
        assert _emails(world, email.lower()) == []
        assert len(_emails(world, after)) == 1

    def test_a_resend_queues_one_job_and_a_double_resend_still_one(self, world):
        email = "admit-resend@example.com"
        _join(world, email)
        assert _admit(world, email) == "admitted"
        # The first email went.
        execute(
            world["owner"],
            "UPDATE jobs SET state = 'succeeded' WHERE serialization_key = %s",
            (_key(email),),
        )
        assert _admit(world, email, resend=True) == "resent"
        assert _admit(world, email, resend=True) == "resent"
        assert _emails(world, email) == ["succeeded", "ready"]

    def test_a_held_address_is_signed_up_and_writes_nothing(self, world):
        email = "admit-held@example.com"
        _join(world, email)
        _hold(world, "Admit-Held@Example.com")
        for resend in (False, True):
            assert _admit(world, email, resend=resend) == "signed_up"
        assert _admission(world, email) is None
        assert _emails(world, email) == []

    def test_a_claimed_admission_is_signed_up_and_queues_nothing(self, world):
        email = "admit-claimed@example.com"
        assert _claim(world, _linked(world, email)) is True
        for resend in (False, True):
            assert _admit(world, email, resend=resend) == "signed_up"
        assert len(_emails(world, email)) == 1


class TestTheLink:
    """`fn_signup_link` through `signup_admissions.store_link`, as
    `svc_worker` in a system session: the link the email carried."""

    def test_it_stores_the_hash_with_emailed_at_and_replaces_an_earlier_link(
        self, world
    ):
        email = "link-replaced@example.com"
        first = _linked(world, email)
        stored = _admission(world, email)
        assert stored["claim_token_hash"] == sessions.token_hash(first)
        assert stored["emailed_at"] is not None
        second = sessions.new_token()
        assert _store(world, email, second) is True
        again = _admission(world, email)
        assert again["claim_token_hash"] == sessions.token_hash(second)
        assert again["emailed_at"] >= stored["emailed_at"]
        assert _claim(world, first) is False, "a replaced link claims nothing"
        assert _claim(world, second) is True

    def test_a_claimed_admission_stores_nothing(self, world):
        email = "link-claimed@example.com"
        assert _claim(world, _linked(world, email)) is True
        before = _admission(world, email)
        assert _store(world, email, sessions.new_token()) is False
        assert _admission(world, email) == before

    @pytest.mark.parametrize("how", sorted(HOLDS))
    def test_a_held_address_stores_nothing(self, world, how):
        """An account created at the address has also used the admission up
        (133), so the account that moved to it is the one that holds the
        address and nothing else: the door refuses both."""
        email = f"link-held-{how}@example.com"
        assert _admit(world, email) == "admitted"
        HOLDS[how](world, f"Link-Held-{how.title()}@Example.com")
        assert _store(world, email, sessions.new_token()) is False
        row = _admission(world, email)
        assert row["claim_token_hash"] is None
        assert (row["claimed_at"] is not None) == (how == "created"), row


class TestTheClaim:
    """`fn_signup_claim` through `signup_admissions.claim`, as `svc_ingress`:
    single use, and dead once the address has an account."""

    def test_a_link_claims_once(self, world):
        email = "claim-once@example.com"
        token = _linked(world, email)
        assert _claim(world, token) is True
        assert _claim(world, token) is False
        row = _admission(world, email)
        assert row["claimed_at"] is not None and row["claim_token_hash"] is None

    @pytest.mark.parametrize("how", sorted(HOLDS))
    def test_a_link_whose_address_an_account_holds_claims_nothing(self, world, how):
        """Dead once the address has an account. One created at it used the
        admission up as it was created (133), and the refused claim leaves
        that stamp as it was; one that moved to it used nothing, and the claim
        is refused on the hold alone."""
        email = f"claim-held-{how}@example.com"
        token = _linked(world, email)
        HOLDS[how](world, f"Claim-Held-{how.title()}@Example.com")
        held = _admission(world, email)
        assert _claim(world, token) is False
        assert _admission(world, email) == held
        assert (held["claimed_at"] is not None) == (how == "created"), held

    def test_an_unknown_or_malformed_token_claims_nothing(self, world):
        assert _claim(world, sessions.new_token()) is False
        assert _claim(world, "not a token") is False


class TestTheSignUpDoor:
    """`fn_signup_admitted` (092, 098, 129): a claimed admission does not admit
    its address, and the rest of the door is as 092 and 098 make it."""

    def test_an_unclaimed_admission_admits_and_a_claimed_one_no_longer_does(
        self, world
    ):
        email = "door-claimed@example.com"
        token = _linked(world, email)
        assert _signup_admitted(world, email) is True, "emailed, not yet claimed"
        assert _claim(world, token) is True
        assert _signup_admitted(world, email) is False
        assert _signup_admitted(world, email.upper()) is False

    def test_a_live_invitation_still_admits_a_claimed_admissions_address(self, world):
        email = "door-invited@example.com"
        assert _claim(world, _linked(world, email)) is True
        assert _signup_admitted(world, email) is False
        conn = psycopg2.connect(world["owner"])
        try:
            chain = seed_workspace_chain(conn, "admission-door")
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                    " delivery_channel, email, expires_at, invited_by_user_id)"
                    " VALUES (%s, %s, 'email', %s, now() + interval '7 days', %s)",
                    (chain["ws"], sessions.token_hash(email), email, chain["user"]),
                )
            conn.commit()
        finally:
            conn.close()
        assert _signup_admitted(world, email) is True


class TestTheAdmitTap:
    """The ✅ Admit tap through the REAL webhook route as `svc_ingress`,
    with no bot (the runtime keeps only connect and dispatch): the admission
    and its email's job commit with the delivery, a replay executes nothing,
    and a fault after the admission rolls all three back."""

    OPERATOR = "00000000-0000-4000-8000-00000000a7a1"
    TELEGRAM = "6200001"
    SECRET = "gate-webhook-secret"

    @pytest.fixture(autouse=True)
    def operator(self, world, monkeypatch):
        execute(
            world["owner"],
            "INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING",
            (self.OPERATOR,),
        )
        execute(
            world["owner"],
            "INSERT INTO user_identities (user_id, provider, external_id, display_name)"
            " VALUES (%s, 'telegram', %s, 'Owner') ON CONFLICT DO NOTHING",
            (self.OPERATOR, self.TELEGRAM),
        )
        for name, value in (
            ("TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", self.SECRET),
            ("OPS_USER_IDS", self.OPERATOR),
            ("WEB_APP_URL", "https://app.example.test"),
        ):
            monkeypatch.setattr(principal.settings, name, value, raising=False)

    def _tap(self, update_id, email):
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"q{update_id}",
                "from": {"id": int(self.TELEGRAM), "first_name": "Owner"},
                "data": f"v1:admit:{signup_admissions.ref(email)}",
                "message": {
                    "message_id": 77,
                    "chat": {"id": int(self.TELEGRAM), "type": "private"},
                    "text": f"New waitlist signup!\n\nEmail: {email}",
                },
            },
        }

    def _deliver(self, world, *updates):
        async def main():
            async with api_client(world["ingress"]) as (client, _):
                app = client._transport.app
                app.state.ingress = dataclasses.replace(
                    app.state.ingress, reply=None, answer_callback=None, edit=None
                )
                answers = []
                for update in updates:
                    resp = await client.post(
                        "/webhooks/telegram",
                        content=json.dumps(update),
                        headers={
                            **JSON,
                            vocabulary.WEBHOOK_SECRET_HEADER: self.SECRET,
                        },
                    )
                    answers.append((resp.status_code, resp.json()))
                return answers

        return asyncio.run(main())

    def _delivered(self, world, update_id):
        return _rows(
            world["owner"],
            "SELECT 1 FROM command_dedup WHERE channel = 'telegram'"
            " AND external_ref = %s",
            (str(update_id),),
        )

    def test_a_tap_commits_the_admission_and_its_one_email(self, world):
        email = "tap-admit@example.com"
        _join(world, email)
        first, again = self._tap(920001, email), self._tap(920002, email)

        answers = self._deliver(world, first, first, again)

        assert answers == [
            (200, {"status": "admitted", "outcome": "admitted"}),
            (200, {"status": "replayed"}),
            (200, {"status": "admitted", "outcome": "admitted_before"}),
        ]
        assert _admission(world, email) is not None
        assert _emails(world, email) == ["ready"]
        assert self._delivered(world, 920001)

    def test_a_fault_after_the_admission_rolls_back_all_three(self, world, monkeypatch):
        email = "tap-fault@example.com"
        _join(world, email)
        real = telegram_dispatch._admit

        async def then_fault(conn, from_id, ref):
            assert (await real(conn, from_id, ref))[0] == "admitted"
            raise OperationalError("gate", {}, Exception("fault"))

        monkeypatch.setattr(telegram_dispatch, "_admit", then_fault)

        ((status, _),) = self._deliver(world, self._tap(920010, email))

        assert status == 503
        assert _admission(world, email) is None
        assert _emails(world, email) == []
        assert not self._delivered(world, 920010)


class TestTheRuntimeLoginsHoldNoGrant:
    """Access before membership goes through the doors: neither login reads or
    writes the admissions, or reads the waitlist."""

    @pytest.mark.parametrize("login", ["ingress", "worker"])
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT count(*) FROM signup_admissions",
            "INSERT INTO signup_admissions (email) VALUES ('direct-admit@example.com')",
            "UPDATE signup_admissions SET claimed_at = now()",
            "SELECT count(*) FROM waitlist_entries",
        ],
    )
    def test_each_is_refused(self, world, login, sql):
        conn = psycopg2.connect(world[login])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cur.execute(sql)
        finally:
            conn.close()


class TestTheTokenNeverRests:
    """The admission's job, run by the real executor as `svc_worker`: the
    email carries the link, the worker stores only its hash after the send,
    and the claim clears that too. No table holds the raw token at any point
    after the send, the job's payload included."""

    def test_the_worker_emails_the_link_stores_its_hash_and_a_claim_spends_it(
        self, world
    ):
        email = "token-scan@example.com"
        _join(world, email)
        assert _admit(world, email) == "admitted"
        [job] = fetch_all(
            world["owner"],
            "SELECT id, lease_token, attempts, payload FROM jobs"
            " WHERE serialization_key = %s",
            (_key(email),),
        )
        sender = RecordingSender()

        async def send():
            async with ingress_engine(world["worker"]) as engine:
                return await email_sender.execute_send_email(
                    job, sender=sender, engine=engine
                )

        assert asyncio.run(send()) == "msg_test"
        [mail] = sender.sent
        token = mail["token"]
        assert mail["to"] == email
        assert f"{BASE}{token}" in mail["body"]
        stored = _admission(world, email)
        assert stored["claim_token_hash"] == sessions.token_hash(token)
        assert stored["emailed_at"] is not None
        assert rows_holding(world["owner"], token) == {}

        assert _claim(world, token) is True
        assert rows_holding(world["owner"], token) == {}
        assert rows_holding(world["owner"], sessions.token_hash(token)) == {}
        assert _admission(world, email)["claimed_at"] is not None


# --- 133 (#1740): an account's creation uses its admission up ---------------


def _sign_in(world, sub, email):
    """Google subject *sub* through the sign-in upsert as `svc_ingress`, gated
    as deployed: a new subject at an admitted address signs up, and a
    returning one's address follows *email* (#1579). Returns the user."""
    return _ingress(
        world,
        lambda c: identity.upsert_google_identity(
            c, sub=sub, email=email, display_name=None
        ),
    )


class TestTheOwnerSeesTheSpotStayUsed:
    """133 (#1740): the owner's surfaces, after the person who signed
    up at an admitted address has moved to another one. The spot reads as
    used on every surface: the list, an admit, a resend and the worker's
    stored link. Each surface is asked as its production login:
    `svc_ingress` lists and admits, `svc_worker` stores the link."""

    @pytest.mark.parametrize("emailed", [True, False], ids=["emailed", "not_emailed"])
    def test_after_the_account_moves_the_spot_stays_signed_up(self, world, emailed):
        email = f"moved-{'emailed' if emailed else 'unemailed'}@example.com"
        if emailed:
            _linked(world, email)
        else:
            assert _admit(world, email) == "admitted"
        # Its one email has gone, so a resend would queue another.
        execute(
            world["owner"],
            "UPDATE jobs SET state = 'succeeded' WHERE serialization_key = %s",
            (_key(email),),
        )
        user = _sign_in(world, f"sub-{email}", email)
        assert _sign_in(world, f"sub-{email}", f"now-{email}") == user

        (row,) = _list(world, ref=signup_admissions.ref(email))
        assert row["state"] == "signed_up", row
        for resend in (False, True):
            assert _admit(world, email, resend=resend) == "signed_up"
        assert _emails(world, email) == ["succeeded"]
        assert _store(world, email, sessions.new_token()) is False
        used = _admission(world, email)
        assert used["claimed_at"] is not None, used
        assert used["claim_token_hash"] is None, used


class TestTheTriggerIsNoDoor:
    """133's function runs as `svc_membership` for whichever login creates the
    user, and EXECUTE on it is granted to no role. PostgreSQL checks EXECUTE on
    a trigger's function when the trigger is created, never when an INSERT
    fires it, so neither runtime login holds it, and both still hold nothing on
    the admissions (`TestTheRuntimeLoginsHoldNoGrant`, above). The function's
    catalog contract (owner, VOLATILE, no arguments, `trigger` returned, the
    pinned path, an ACL holding the owner alone) is the RLS harness's
    (`TRIGGER_DEFINERS`), and so is the exhaustive policy census, which no
    new policy has joined."""

    @pytest.mark.parametrize("login", ["ingress", "worker"])
    def test_a_runtime_login_creating_a_user_uses_its_admission_up(self, world, login):
        email = f"born-as-{login}@example.com"
        token = _linked(world, email)
        conn = psycopg2.connect(world[login])
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_user, has_function_privilege("
                    "'trg_users_consume_admission()', 'EXECUTE')"
                )
                assert cur.fetchone() == (f"svc_{login}", False)
                # Upper case: the address is compared with lower().
                cur.execute(
                    "INSERT INTO users (primary_email) VALUES (%s)", (email.upper(),)
                )
        finally:
            conn.close()
        used = _admission(world, email)
        assert used["claimed_at"] is not None, used
        assert used["claim_token_hash"] is None, used
        assert _claim(world, token) is False

    def test_it_fires_after_a_user_is_created_with_an_address_and_never_else(
        self, world
    ):
        """The two triggers on `users`: 053's touch before an update, and
        133's after an insert of a row with an address, enabled, and running
        133's function. No other trigger fires after an insert, so no name
        order is in play."""
        rows = _rows(
            world["owner"],
            "SELECT t.tgname, t.tgenabled, pg_get_triggerdef(t.oid)"
            "  FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid"
            "  JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = 'public' AND c.relname = 'users'"
            "   AND NOT t.tgisinternal ORDER BY t.tgname",
        )
        assert [(name, enabled) for name, enabled, _ in rows] == [
            ("tg_touch_users", "O"),
            ("tg_users_consume_admission", "O"),
        ]
        assert rows[1][2] == (
            "CREATE TRIGGER tg_users_consume_admission AFTER INSERT ON public.users"
            " FOR EACH ROW WHEN ((new.primary_email IS NOT NULL))"
            " EXECUTE FUNCTION trg_users_consume_admission()"
        )

    def test_the_create_bracket_closed(self, world):
        """133 granted `svc_membership` CREATE on `public` for the owner
        transfer, and revoked it before the file ended."""
        assert _rows(
            world["owner"],
            "SELECT has_schema_privilege('svc_membership', 'public', 'CREATE')",
        ) == [(False,)]
