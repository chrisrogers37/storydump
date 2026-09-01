"""The Telegram arm of invitation delivery, driven against the real schema.

`053` makes an invitation ONE object with a `delivery_channel`, and each arm
carries its own D33 acceptance value. So the card is not a broadcast of an
email invitation — these drive that refusal, the quiet-beat empty case, and the
fact that a card actually reaches the already-built delivery chain.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.services.target import bindings, invitation_cards, invitations, work_loop
from src.services.target.invitation_cards import CardRefused
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    replay_advertised_stream,
    seed_workspace_chain,
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
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, "card-a")
            b = seed_workspace_chain(conn, "card-b")
        finally:
            conn.close()
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress"), "a": a, "b": b}
    finally:
        gen.close()


async def _in_uow(dsn, ws, user, fn):
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        uow = unit_of_work(
            engine, ws, actor_kind="user", actor_user_id=user, channel="telegram"
        )
        async with uow.begin() as session:
            return await fn(session)
    finally:
        await engine.dispose()


def run(world, fn, *, ids=None):
    ids = ids or world["a"]
    return asyncio.run(_in_uow(world["ingress"], str(ids["ws"]), str(ids["user"]), fn))


def _chat() -> str:
    return f"-100{uuid.uuid4().int % 10**10:010d}"


def _mint(world, *, channel="telegram", email=None, role="member", hint=None, ids=None):
    """Mint a real invitation through alex's one writer, then read the row
    back — the producer takes the row, not a hand-built dict."""
    ids = ids or world["a"]

    async def go(session):
        inv_id, token = await invitations.create(
            session,
            workspace_id=str(ids["ws"]),
            invited_by_user_id=str(ids["user"]),
            role=role,
            delivery_channel=channel,
            email=email,
            invited_channel_hint=hint,
        )
        row = (
            (
                await session.execute(
                    text(
                        "SELECT id, role, delivery_channel, email,"
                        "       invited_channel_hint"
                        "  FROM workspace_invitations WHERE id = :i"
                    ),
                    {"i": inv_id},
                )
            )
            .mappings()
            .first()
        )
        return dict(row), token

    return run(world, go, ids=ids)


def _cards(world, ids=None):
    ids = ids or world["a"]
    (n,) = fetch_one(
        world["stream"],
        "SELECT count(*) FROM channel_outbox"
        " WHERE workspace_id = %s AND kind = 'invitation'",
        (str(ids["ws"]),),
    )
    return n


class TestAnEmailInvitationIsNeverAnnouncedInAChat:
    def test_an_email_invitation_is_refused_by_name(self, world):
        """**The security case.** An email token is minted for one inbox.
        Posting it in a group would broadcast a credential — and it would
        DEGRADE rather than refuse: `email` is the D33 acceptance value, so a
        tapper fails the match, takes the recorded-skip path, and lands
        `member` with an elevation-pending notice. It would look like it
        worked."""
        inv, token = _mint(world, channel="email", email=f"{uuid.uuid4().hex}@x.com")
        with pytest.raises(CardRefused) as e:
            run(
                world,
                lambda s: invitation_cards.announce(
                    s,
                    workspace_id=str(world["a"]["ws"]),
                    invitation=inv,
                    token=token,
                ),
            )
        assert e.value.reason == "not_a_telegram_invitation"

    def test_a_missing_token_is_refused_rather_than_announced_empty(self, world):
        inv, _ = _mint(world, hint="someone")
        for bad in ("", "   ", None, 7):
            with pytest.raises(CardRefused) as e:
                run(
                    world,
                    lambda s, b=bad: invitation_cards.announce(
                        s,
                        workspace_id=str(world["a"]["ws"]),
                        invitation=inv,
                        token=b,
                    ),
                )
            assert e.value.reason == "token_required", bad


class TestTheEmptyCaseIsAQuietBeat:
    def test_no_bindings_enqueues_nothing_and_returns_zero(self, world):
        """Workspace B never acquires a binding. Not `UNDELIVERABLE`: the run
        is a whole-invitation claim and the email arm may have delivered."""
        inv, token = _mint(world, ids=world["b"], hint="nobody")
        before = _cards(world, world["b"])
        n = run(
            world,
            lambda s: invitation_cards.announce(
                s, workspace_id=str(world["b"]["ws"]), invitation=inv, token=token
            ),
            ids=world["b"],
        )
        assert n == 0
        assert _cards(world, world["b"]) == before

    def test_the_count_distinguishes_quiet_from_silently_skipped(self, world):
        """The bound on the ruling: a quiet beat is right for ZERO bindings and
        would be wrong as a blanket rule. Returning a count is what lets a
        caller tell "nobody to tell" from "had somewhere and wrote nothing" —
        the shape that would satisfy clause 3 while failing clause 4."""
        ref = _chat()
        assert (
            run(
                world,
                lambda s: bindings.bind(
                    s,
                    workspace_id=str(world["a"]["ws"]),
                    chat_type="supergroup",
                    external_ref=ref,
                ),
            )
            == "bound"
        )
        inv, token = _mint(world, hint="someone")
        n = run(
            world,
            lambda s: invitation_cards.announce(
                s, workspace_id=str(world["a"]["ws"]), invitation=inv, token=token
            ),
        )
        assert n >= 1, "a workspace WITH a binding must not report a quiet beat"


class TestThePayload:
    def test_it_carries_the_token_and_the_role_ceiling(self, world):
        inv, token = _mint(world, role="admin", hint="tapper")
        payload = invitation_cards.render_card(inv, token)
        assert payload["v"] == 1
        assert payload["token"] == token
        assert payload["role"] == "admin"
        assert payload["invited_hint"] == "tapper"
        assert payload["invitation_id"] == str(inv["id"])

    def test_it_never_carries_the_invitees_email(self, world):
        """A card lands in a group chat. The email is the OTHER arm's delivery
        address and would be a person's address published to everyone in it."""
        address = f"{uuid.uuid4().hex}@example.com"
        inv, token = _mint(world, channel="email", email=address)
        payload = invitation_cards.render_card(inv, token)
        assert address not in repr(payload)
        assert "email" not in payload

    def test_an_absent_hint_is_omitted_rather_than_rendered_empty(self, world):
        inv, token = _mint(world)
        payload = invitation_cards.render_card(inv, token)
        assert "invited_hint" not in payload


class TestItReachesTheBuiltChain:
    def test_a_card_lands_on_every_binding_and_mints_a_sender_job(self, world):
        """bind -> announce -> sweep, end to end. The chain was built and inert
        until a binding existed; this is the other half using it."""
        first, second = _chat(), _chat()
        for ref in (first, second):
            run(
                world,
                lambda s, r=ref: bindings.bind(
                    s,
                    workspace_id=str(world["a"]["ws"]),
                    chat_type="supergroup",
                    external_ref=r,
                ),
            )
        inv, token = _mint(world, hint="someone")
        before = _cards(world)

        async def announce_and_sweep(session):
            n = await invitation_cards.announce(
                session,
                workspace_id=str(world["a"]["ws"]),
                invitation=inv,
                token=token,
            )
            minted = await work_loop.ensure_sender_jobs(session)
            return n, minted

        n, minted = run(world, announce_and_sweep)
        assert n >= 2, "one card per binding"
        assert _cards(world) == before + n
        assert minted >= 1
        (jobs,) = fetch_one(
            world["stream"],
            "SELECT count(*) FROM jobs WHERE kind = 'deliver_outbox'"
            "   AND workspace_id = %s",
            (str(world["a"]["ws"]),),
        )
        assert jobs >= 1

    def test_a_rolled_back_invitation_takes_its_card_with_it(self, world):
        """`02` §4's same-tx rule: `outbox.enqueue` does not commit, so an
        invitation that never landed cannot leave a card announcing it."""
        ref = _chat()
        run(
            world,
            lambda s: bindings.bind(
                s,
                workspace_id=str(world["a"]["ws"]),
                chat_type="supergroup",
                external_ref=ref,
            ),
        )
        before = _cards(world)

        async def announce_then_fail(session):
            inv_id, token = await invitations.create(
                session,
                workspace_id=str(world["a"]["ws"]),
                invited_by_user_id=str(world["a"]["user"]),
                delivery_channel="telegram",
                invited_channel_hint="doomed",
            )
            await invitation_cards.announce(
                session,
                workspace_id=str(world["a"]["ws"]),
                invitation={
                    "id": inv_id,
                    "role": "member",
                    "delivery_channel": "telegram",
                },
                token=token,
            )
            raise RuntimeError("caller fails after announcing")

        with pytest.raises(RuntimeError):
            run(world, announce_then_fail)
        assert _cards(world) == before, "the card rolled back with the invitation"
