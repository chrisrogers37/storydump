"""090: a removal sticks, and a removed admin's tokens go with them.

Before 090, `fn_member_remove` deleted the membership row and
`fn_group_member_seen` inserted a member for any linked user seen in the bound
group, so a removed person still in the group was a member again the next time
they spoke there. And a workspace service identity carried no minter, so the
tokens a removed admin made kept reading the workspace.

Both doors run here as the production role (`svc_ingress`) on the replayed
advertised stream, and the removal runs through `workspaces.remove_member`, the
service the command port calls, so the token half is proven in the same
transaction the API uses. 098 adds the invitations the removed member sent: the
service revokes the pending ones in that same transaction.
"""

from __future__ import annotations

import asyncio

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import service_tokens, workspaces
from tests.scripts.conftest import (
    _scratch,
    as_user,
    async_url,
    fetch_one,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

GROUP = "-1000000000090"


@pytest.fixture()
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner_dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(owner_dsn)
        try:
            chain = seed_workspace_chain(conn, "removal")
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
                admin = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO workspace_members (workspace_id, user_id, role)"
                    " VALUES (%s, %s, 'admin')",
                    (chain["ws"], admin),
                )
                cur.execute(
                    "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                    " VALUES (%s, 'telegram_group', %s)",
                    (chain["ws"], GROUP),
                )
            conn.commit()
        finally:
            conn.close()
        yield {
            "ingress": as_user(db, "svc_ingress"),
            "ws": str(chain["ws"]),
            "owner": str(chain["user"]),
            "admin": str(admin),
        }
    finally:
        gen.close()


def _seen(dsn: str, user: str) -> str:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT o_outcome FROM fn_group_member_seen('telegram_group', %s, %s)",
                (GROUP, user),
            )
            outcome = cur.fetchone()[0]
        conn.commit()
        return outcome
    finally:
        conn.close()


async def _in_tenant(dsn: str, ws: str, actor: str, work):
    engine = create_async_engine(async_url(dsn))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "SELECT set_config('app.tenant_id', :ws, true),"
                    " set_config('app.actor_kind', 'user', true),"
                    " set_config('app.actor_user_id', :u, true),"
                    " set_config('app.channel', 'web', true)"
                ),
                {"ws": ws, "u": actor},
            )
            return await work(conn)
    finally:
        await engine.dispose()


def _remove(world, user: str) -> str:
    return asyncio.run(
        _in_tenant(
            world["ingress"],
            world["ws"],
            world["owner"],
            lambda conn: workspaces.remove_member(
                conn, workspace_id=world["ws"], user_id=user, by_user_id=world["owner"]
            ),
        )
    )


def _is_member(world, user: str) -> bool:
    async def read(conn):
        result = await conn.execute(
            text(
                "SELECT 1 FROM workspace_members"
                " WHERE workspace_id = :ws AND user_id = :u"
            ),
            {"ws": world["ws"], "u": user},
        )
        return result.first() is not None

    return asyncio.run(_in_tenant(world["ingress"], world["ws"], world["owner"], read))


def test_a_removed_member_is_not_re_added_by_speaking_in_the_group(world):
    assert _remove(world, world["admin"]) == "admin"
    assert not _is_member(world, world["admin"])

    assert _seen(world["ingress"], world["admin"]) == "removed"
    assert not _is_member(world, world["admin"])


def test_someone_never_removed_still_joins_through_the_group(world):
    # a fresh person, seen in the group for the first time
    seed = psycopg2.connect(world["ingress"])
    try:
        with seed.cursor() as cur:
            cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
            newcomer = str(cur.fetchone()[0])
        seed.commit()
    finally:
        seed.close()
    assert _seen(world["ingress"], newcomer) == "joined"
    assert _is_member(world, newcomer)


def test_the_removed_admins_service_tokens_are_revoked_with_the_membership(world):
    async def mint(conn):
        _, mine = await service_tokens.mint(
            conn,
            name="theirs",
            role="readonly",
            workspace_id=world["ws"],
            created_by_user_id=world["admin"],
        )
        _, other = await service_tokens.mint(
            conn,
            name="the owners",
            role="readonly",
            workspace_id=world["ws"],
            created_by_user_id=world["owner"],
        )
        return str(mine["id"]), str(other["id"])

    theirs, owners = asyncio.run(
        _in_tenant(world["ingress"], world["ws"], world["owner"], mint)
    )
    _remove(world, world["admin"])

    async def states(conn):
        rows = await service_tokens.list_for_workspace(conn, workspace_id=world["ws"])
        return {str(r["id"]): r["revoked_at"] is not None for r in rows}

    revoked = asyncio.run(
        _in_tenant(world["ingress"], world["ws"], world["owner"], states)
    )
    assert revoked == {theirs: True, owners: False}


def _admitted(world, email: str) -> bool:
    return fetch_one(world["ingress"], "SELECT fn_signup_admitted(%s)", (email,))[0]


def test_the_removed_members_pending_invitations_are_revoked_with_them(world):
    """098 (`07` §41): what the removed admin sent and nobody has used yet is
    revoked in the removal's transaction, so it neither admits a new account
    (`fn_signup_admitted`) nor can be accepted; the owner's invitation and the
    removed admin's already-accepted one are left as they were."""
    sent = {
        "theirs@example.com": (world["admin"], "pending"),
        "used@example.com": (world["admin"], "accepted"),
        "owners@example.com": (world["owner"], "pending"),
    }

    async def invite(conn):
        for email, (by, state) in sent.items():
            await conn.execute(
                text(
                    "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                    " delivery_channel, email, state, expires_at, invited_by_user_id)"
                    " VALUES (:ws, :h, 'email', :e, :s, now() + interval '7 days',"
                    " CAST(:by AS uuid))"
                ),
                {"ws": world["ws"], "h": email, "e": email, "s": state, "by": by},
            )

    asyncio.run(_in_tenant(world["ingress"], world["ws"], world["owner"], invite))
    assert _admitted(world, "theirs@example.com")

    _remove(world, world["admin"])

    async def states(conn):
        rows = await conn.execute(
            text(
                "SELECT email, state FROM workspace_invitations WHERE workspace_id = :ws"
            ),
            {"ws": world["ws"]},
        )
        return dict(rows.all())

    assert asyncio.run(
        _in_tenant(world["ingress"], world["ws"], world["owner"], states)
    ) == {
        "theirs@example.com": "revoked",
        "used@example.com": "accepted",
        "owners@example.com": "pending",
    }
    assert not _admitted(world, "theirs@example.com")
    assert _admitted(world, "owners@example.com")
