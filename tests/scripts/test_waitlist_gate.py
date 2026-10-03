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

from src.api.routes import public
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


def _post(world, *bodies):
    """Each body to the real route as `svc_ingress`; the responses, in order."""

    async def main():
        async with api_client(world["ingress"]) as (client, _):
            # json.dumps escapes to ASCII, so a lone surrogate travels as
            # `\\ud800` the way a browser's JSON would carry it.
            return [
                await client.post(
                    "/public/waitlist",
                    content=json.dumps(b),
                    headers={"content-type": "application/json"},
                )
                for b in bodies
            ]

    return asyncio.run(main())


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
            "a" * 250 + "@ex.co",
            "zero​width@example.com",
            "nul\x00@example.com",
            "lone\ud800@example.com",
            42,
            None,
        ],
    )
    def test_an_address_the_list_cannot_hold_is_a_400_and_stores_nothing(
        self, world, email
    ):
        before = _rows(world["owner"], "SELECT count(*) FROM waitlist_entries")
        (resp,) = _post(world, {"email": email})
        assert resp.status_code == 400
        assert resp.json()["reason"] == "invalid_email"
        assert _rows(world["owner"], "SELECT count(*) FROM waitlist_entries") == before

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
