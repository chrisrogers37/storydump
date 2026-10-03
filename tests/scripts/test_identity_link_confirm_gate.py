"""The Telegram identity link's two steps, against the real replayed schema.

Opening `t.me/<bot>?start=link-<state>` used to link the opener on the spot,
so a link minted by one person and opened by another made the opener's
Telegram the minter's identity. Now the `/start` only ASKS (`handle_link`
peeks the state and names the account by a masked email) and a Confirm tap
by the same Telegram user links (`handle_tap` consumes the state one-shot).

What only PostgreSQL can prove, driven as `svc_ingress` in the user plane —
the shape the ingress opens (no tenant, no GUCs):

- the peek is a read: after the `/start` the state is still live and no
  `user_identities` row exists;
- the masked email comes from `users.primary_email` through the real query;
- Confirm consumes the state by the CAS and writes the identity; a second
  Confirm is refused by the same CAS;
- Cancel spends the state and writes nothing, so a later Confirm is refused.

Each test mints its own user and state: every driver call commits, and
`uq_users_primary_email` / `uq_identity_per_provider` are global.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.services.target import callback_tokens, identity, identity_link
from src.services.target.start_router import StartContext
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    in_user_plane,
    replay_advertised_stream,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress")}
    finally:
        gen.close()


def user_plane(world, fn):
    return asyncio.run(in_user_plane(world["ingress"], fn))


def owner(world, sql, params=None):
    return fetch_one(world["stream"], sql, params)


def minted(world):
    """A signed-in user with an email, and a live link state they minted."""
    email = f"linker-{uuid.uuid4().hex[:8]}@example.com"
    user_id = user_plane(
        world,
        lambda c: identity.upsert_google_identity(
            c,
            sub=f"g-{uuid.uuid4()}",
            email=email,
            display_name=None,
            signup_open=True,
        ),
    )
    link = user_plane(
        world,
        lambda c: identity_link.issue_link_state(
            c, user_id=str(user_id), bot_username="example_bot"
        ),
    )
    state = link.rsplit("start=link-", 1)[1]
    return str(user_id), email, state


def telegram_user():
    return str(uuid.uuid4().int % 10**12)


def open_link(world, state, uid):
    ctx = StartContext(
        payload=state,
        telegram_user_id=uid,
        chat_id=uid,
        chat_type="private",
        display_name="opener",
    )
    return user_plane(world, lambda c: identity_link.handle_link(c, ctx))


def tap(world, action, state, *, offered_to, by):
    lt = callback_tokens.LinkTap(
        action=action, state=state, telegram_user_id=offered_to
    )
    return user_plane(
        world,
        lambda c: identity_link.handle_tap(
            c,
            lt,
            from_user_id=by,
            chat_ref=by,
            chat_type="private",
            display_name="opener",
        ),
    )


def linked_to(world, uid):
    row = owner(
        world,
        "SELECT user_id FROM user_identities"
        " WHERE provider = 'telegram' AND external_id = %s",
        (uid,),
    )
    return None if row is None else str(row[0])


def live(world, state):
    row = owner(
        world,
        "SELECT consumed_at IS NULL AND expires_at > now()"
        "  FROM oauth_states WHERE state = %s",
        (state,),
    )
    return bool(row and row[0])


class TestTheTwoSteps:
    def test_opening_asks_and_links_nothing(self, world):
        user_id, email, state = minted(world)
        uid = telegram_user()
        r = open_link(world, state, uid)
        assert r.outcome == "confirmation_offered" and r.handled
        local, domain = email.split("@")
        assert f"{local[0]}•••@{domain}" in r.reply
        assert email not in r.reply and local not in r.reply
        assert linked_to(world, uid) is None, "opening the link linked"
        assert live(world, state), "opening the link spent the state"

    def test_confirm_links_and_cannot_be_repeated(self, world):
        user_id, _, state = minted(world)
        uid = telegram_user()
        open_link(world, state, uid)
        first = tap(world, "linkok", state, offered_to=uid, by=uid)
        assert first.outcome == "linked"
        assert linked_to(world, uid) == user_id
        assert not live(world, state)
        second = tap(world, "linkok", state, offered_to=uid, by=uid)
        assert second.outcome == "state_refused"
        assert second.answer_text == identity_link.REFUSAL

    def test_cancel_links_nothing_and_kills_the_link(self, world):
        _, _, state = minted(world)
        uid = telegram_user()
        open_link(world, state, uid)
        assert tap(world, "linkno", state, offered_to=uid, by=uid).outcome == (
            "cancelled"
        )
        assert linked_to(world, uid) is None and not live(world, state)
        later = tap(world, "linkok", state, offered_to=uid, by=uid)
        assert later.outcome == "state_refused"
        assert linked_to(world, uid) is None

    def test_another_users_confirm_touches_nothing(self, world):
        _, _, state = minted(world)
        victim, other = telegram_user(), telegram_user()
        open_link(world, state, victim)
        r = tap(world, "linkok", state, offered_to=victim, by=other)
        assert r.outcome == "tapper_mismatch"
        assert linked_to(world, victim) is None and linked_to(world, other) is None
        assert live(world, state), "a refused tap spent the state"

    def test_a_refused_open_is_silent(self, world):
        r = open_link(world, "no-such-state", telegram_user())
        assert (r.outcome, r.handled, r.reply) == ("state_refused", False, None)
