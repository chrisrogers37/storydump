"""100: the waitlist is written by the API, not by the site (`07` §43).

`POST /public/waitlist` runs here as the production role (`svc_ingress`) on
the replayed advertised stream, so the grant, the policy, the CHECKs and the
bound parameters are the real ones: an address lands, a repeat answers the
same and stores nothing new, and the login that wrote it cannot read it back.
The list is read as the schema owner, the way the owner reads it.
"""

from __future__ import annotations

import asyncio
import json

import psycopg2
import psycopg2.errors
import pytest

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.routes import public
from src.services.target import waitlist
from src.services.target.unit_of_work import asyncpg_url
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    set_test_passwords,
)
from tests.src.api.conftest import api_client

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner_dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"owner": owner_dsn, "ingress": as_user(db, "svc_ingress")}
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
        # The shared counter, though the call names a visitor.
        assert [r.status_code for r in responses] == [202, 202, 429]

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
        self, world, monkeypatch, n, secret, headers, key
    ):
        monkeypatch.setattr(public.settings, "WAITLIST_SITE_SECRET", secret)
        seen = []
        acquire = public.WaitlistSlots.acquire

        async def spy(slots, address):
            seen.append(address)
            return await acquire(slots, address)

        monkeypatch.setattr(public.WaitlistSlots, "acquire", spy)
        (resp,) = _post(world, {"email": f"slot-{n}@example.com"}, headers=headers)
        assert resp.status_code == 202, resp.text
        assert seen == [key]

    def test_set_without_a_usable_visitor_the_shared_counter_serves(
        self, world, armed, monkeypatch
    ):
        monkeypatch.setattr(public, "WAITLIST_LIMIT", 1)
        monkeypatch.setattr(public, "WAITLIST_KEY_PREFIX", "waitlist-novisitor-test:")
        responses = _post(
            world,
            {"email": "novisitor1@example.com"},
            {"email": "novisitor2@example.com"},
            headers=_from_site("not-an-address"),
        )
        assert [r.status_code for r in responses] == [202, 429]


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
