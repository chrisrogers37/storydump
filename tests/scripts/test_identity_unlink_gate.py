"""094: a person unlinks their own Telegram identity (`07` §37).

`fn_identity_unlink` is the one delete on `user_identities`: it removes the
user's Telegram identity only while they keep another one, refuses any other
provider, and leaves memberships alone. Driven here as `svc_ingress` in the
user plane, the shape `DELETE /api/v1/me/telegram` opens, through
`identity.unlink_telegram`, on the replayed advertised stream.

Each test mints its own users: every driver call commits, and
`uq_identity_per_provider` is global.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import psycopg2.errors
import pytest

from src.services.target import callback_tokens, identity, identity_link
from src.services.target.start_router import StartContext
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    in_user_plane,
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
            chain = seed_workspace_chain(conn, "unlink")
        finally:
            conn.close()
        yield {
            "stream": stream,
            "ws": str(chain["ws"]),
            "ingress": as_user(db, "svc_ingress"),
            "worker": as_user(db, "svc_worker"),
        }
    finally:
        gen.close()


def user_plane(world, fn):
    return asyncio.run(in_user_plane(world["ingress"], fn))


def signed_up(world) -> str:
    return str(
        user_plane(
            world,
            lambda c: identity.upsert_google_identity(
                c,
                sub=f"g-{uuid.uuid4()}",
                email=f"unlink-{uuid.uuid4().hex[:8]}@example.com",
                display_name=None,
                signup_open=True,
            ),
        )
    )


def telegram_user() -> str:
    return str(uuid.uuid4().int % 10**12)


def mint(world, user_id) -> str:
    link = user_plane(
        world,
        lambda c: identity_link.issue_link_state(
            c, user_id=user_id, bot_username="example_bot"
        ),
    )
    return link.rsplit("start=link-", 1)[1]


def link(world, user_id, uid):
    """The real two steps: open the link, then Confirm as the same person."""
    state = mint(world, user_id)
    ctx = StartContext(
        payload=state,
        telegram_user_id=uid,
        chat_id=uid,
        chat_type="private",
        display_name="opener",
    )
    user_plane(world, lambda c: identity_link.handle_link(c, ctx))
    tap = callback_tokens.LinkTap(action="linkok", state=state, telegram_user_id=uid)
    return user_plane(
        world,
        lambda c: identity_link.handle_tap(
            c,
            tap,
            from_user_id=uid,
            chat_ref=uid,
            chat_type="private",
            display_name="opener",
        ),
    ).outcome


def unlink(world, user_id) -> str:
    return user_plane(world, lambda c: identity.unlink_telegram(c, user_id=user_id))


def resolves_to(world, uid):
    return user_plane(
        world,
        lambda c: identity.user_for_identity(c, provider="telegram", external_id=uid),
    )


def providers(world, user_id) -> list:
    row = fetch_one(
        world["stream"],
        "SELECT coalesce(array_agg(provider ORDER BY provider), '{}')"
        "  FROM user_identities WHERE user_id = %s",
        (user_id,),
    )
    return list(row[0])


class TestTheDoor:
    def test_unlinking_removes_telegram_and_keeps_google(self, world):
        user_id, uid = signed_up(world), telegram_user()
        assert link(world, user_id, uid) == "linked"

        assert unlink(world, user_id) == "unlinked"

        assert providers(world, user_id) == ["google"]
        # A tap or a group message from that account now names nobody: the
        # dispatcher answers `unlinked`, membership sync `unknown_identity`.
        assert resolves_to(world, uid) is None

    def test_nothing_linked_is_not_linked(self, world):
        user_id = signed_up(world)
        assert unlink(world, user_id) == "not_linked"
        assert providers(world, user_id) == ["google"]

    def test_the_last_identity_is_kept(self, world):
        conn = psycopg2.connect(world["stream"])
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
                user_id = str(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO user_identities (user_id, provider, external_id)"
                    " VALUES (%s, 'telegram', %s)",
                    (user_id, telegram_user()),
                )
            conn.commit()
        finally:
            conn.close()

        assert unlink(world, user_id) == "last_identity"
        assert providers(world, user_id) == ["telegram"]

    def test_a_google_identity_is_never_unlinked_here(self, world):
        user_id = signed_up(world)
        assert link(world, user_id, telegram_user()) == "linked"
        with pytest.raises(psycopg2.errors.InvalidParameterValue):
            fetch_one(
                world["ingress"], "SELECT fn_identity_unlink(%s, 'google')", (user_id,)
            )
        assert providers(world, user_id) == ["google", "telegram"]

    def test_svc_ingress_still_cannot_delete_an_identity_directly(self, world):
        user_id = signed_up(world)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            fetch_one(
                world["ingress"],
                "DELETE FROM user_identities WHERE user_id = %s RETURNING 1",
                (user_id,),
            )

    def test_the_door_is_svc_ingress_alone(self, world):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            fetch_one(
                world["worker"],
                "SELECT fn_identity_unlink(%s, 'telegram')",
                (str(uuid.uuid4()),),
            )


class TestWhatUnlinkingLeavesAndRetires:
    def test_a_membership_stays(self, world):
        """Unlinking an identity is not leaving a workspace."""
        user_id, uid = signed_up(world), telegram_user()
        assert link(world, user_id, uid) == "linked"
        conn = psycopg2.connect(world["stream"])
        try:
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "INSERT INTO workspace_members (workspace_id, user_id, role)"
                    " VALUES (%s, %s, 'member')",
                    (world["ws"], user_id),
                )
            conn.commit()
        finally:
            conn.close()

        assert unlink(world, user_id) == "unlinked"

        assert fetch_one(
            world["stream"],
            "SELECT role FROM workspace_members WHERE workspace_id = %s AND user_id = %s",
            (world["ws"], user_id),
        ) == ("member",)

    def test_a_link_minted_before_the_unlink_is_retired(self, world):
        user_id = signed_up(world)
        assert link(world, user_id, telegram_user()) == "linked"
        stale = mint(world, user_id)

        assert unlink(world, user_id) == "unlinked"

        assert fetch_one(
            world["stream"],
            "SELECT consumed_at IS NOT NULL FROM oauth_states WHERE state = %s",
            (stale,),
        ) == (True,)

    def test_linking_again_after_an_unlink_works(self, world):
        user_id, first, second = signed_up(world), telegram_user(), telegram_user()
        assert link(world, user_id, first) == "linked"
        assert unlink(world, user_id) == "unlinked"

        assert link(world, user_id, second) == "linked"

        assert resolves_to(world, second) == user_id
        assert resolves_to(world, first) is None
        assert providers(world, user_id) == ["google", "telegram"]
