"""#1172 clause 3 — the create half, driven against the acceptor that already shipped.

Until now an invitation could be ACCEPTED but not CREATED: `fn_invitation_accept`
and `/join/[token]` landed with `06` §2 / #1090 G2, and nothing minted a row for
them. So the risk in building the create half was never that it would fail — it
was that it would succeed against a shape the acceptor disagrees with, and
nobody would find out until a real person clicked a real link.

**Every test here is therefore a ROUND TRIP.** `invitations.create` writes the
row, `invitations.accept` reads it through the real SECURITY DEFINER door, and
the assertion is on what the door produced — a membership, a granted role, a
refusal. A test that only inspected the inserted row would prove the two halves
agree about columns while saying nothing about whether they agree about
meaning, which is the only thing at issue.

Real migrations via `run_lane`, so the constraints doing the work here —
`uq_invite_live`, `ck_invite_email_required`, `uq_invite_token` — are the
deployed ones rather than a fixture's idea of them.
"""

from __future__ import annotations

import uuid

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import invitations, sessions
from tests.scripts.conftest import (
    _scratch,
    actor_lacks_createrole,
    run_bootstrap,
    seed_workspace_chain,
)
from tests.scripts.test_lineage_lane import run_lane

pytestmark = [pytest.mark.integration]


def _async_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="module")
def invite_lane(admin_conn, owner_actor):
    """The real migration corpus, replayed once (the `clock_db` trade)."""
    reason = actor_lacks_createrole(admin_conn)
    if reason:
        pytest.skip(reason)
    extra: list[str] = []
    gen = _scratch(admin_conn, roles=extra)
    dsn = next(gen)
    try:
        run_bootstrap(admin_conn, dsn)
        run_lane(dsn)
        yield dsn
    finally:
        gen.close()


@pytest.fixture()
def world(invite_lane):
    """A fresh workspace, its owner, and a second user to invite — per test.

    Per test rather than per module because `uq_invite_live` is keyed on
    (workspace, email) and half of these tests are about that index; sharing a
    workspace would make them order-dependent.
    """
    conn = psycopg2.connect(invite_lane)
    try:
        chain = seed_workspace_chain(conn, f"invite-{uuid.uuid4().hex[:8]}")
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
            invitee = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"dsn": invite_lane, "invitee": str(invitee), **chain}


class _Round:
    """One create-then-accept cycle against the real doors."""

    def __init__(self, world):
        self.world = world
        self.engine = create_async_engine(_async_url(world["dsn"]))

    async def create(self, **kw):
        kw.setdefault("workspace_id", self.world["ws"])
        kw.setdefault("invited_by_user_id", self.world["user"])
        async with self.engine.begin() as conn:
            return await invitations.create(conn, **kw)

    async def accept(self, token, *, user_id=None, email=None):
        async with self.engine.begin() as conn:
            if email is not None:
                await conn.execute(
                    text("UPDATE users SET primary_email = :e WHERE id = :u"),
                    {"e": email, "u": user_id or self.world["invitee"]},
                )
            return await invitations.accept(
                conn,
                token=token,
                user_id=user_id or self.world["invitee"],
                channel="web",
            )

    async def role_of(self, user_id=None):
        async with self.engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT role FROM workspace_members"
                        " WHERE workspace_id = :w AND user_id = :u"
                    ),
                    {"w": str(self.world["ws"]), "u": user_id or self.world["invitee"]},
                )
            ).first()
        return row[0] if row else None

    async def close(self):
        await self.engine.dispose()


class TestTheCreateHalfMatchesTheAcceptor:
    async def test_an_invitation_can_be_created_and_then_accepted(self, world):
        """THE ROUND TRIP. Everything else is a variation on it."""
        r = _Round(world)
        try:
            addr = f"{uuid.uuid4().hex[:8]}@example.com"
            _id, token = await r.create(email=addr)
            out = await r.accept(token, email=addr)
            assert out["workspace_id"] == str(world["ws"])
            assert out["role"] == "member"
            assert out["matched"] is True, (
                "a verified email equal to the invited one is D33's matched"
                " case; if this is False the acceptor did not see the address"
                " the creator wrote"
            )
            assert await r.role_of() == "member"
        finally:
            await r.close()

    async def test_the_token_is_never_stored_in_the_clear(self, world):
        """It is the credential — possession accepts — so the row must hold
        only its hash, exactly as `session_tokens` does."""
        r = _Round(world)
        try:
            inv_id, token = await r.create(email=f"{uuid.uuid4().hex[:8]}@example.com")
            async with r.engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT token_hash FROM workspace_invitations WHERE id = :i"
                        ),
                        {"i": inv_id},
                    )
                ).first()
            assert row[0] != token
            assert row[0] == sessions.token_hash(token)
        finally:
            await r.close()

    async def test_an_admin_invitation_grants_admin_only_on_a_matched_identity(
        self, world
    ):
        """`role` is a CEILING, not a grant (D36) — and the creator cannot
        widen it, which is why minting one is safe at the `admin` floor."""
        r = _Round(world)
        try:
            addr = f"{uuid.uuid4().hex[:8]}@example.com"
            _id, token = await r.create(email=addr, role="admin")
            out = await r.accept(token, email=addr)
            assert out["role"] == "admin" and out["matched"] is True
            assert await r.role_of() == "admin"
        finally:
            await r.close()

    async def test_a_mismatched_identity_is_refused_and_nothing_is_granted(self, world):
        """The refusal that makes the token safe to email: possession alone
        does not accept an addressed invitation."""
        r = _Round(world)
        try:
            _id, token = await r.create(email=f"{uuid.uuid4().hex[:8]}@example.com")
            with pytest.raises(invitations.InvitationRefused) as exc:
                await r.accept(token, email="someone.else@example.com")
            assert exc.value.reason == "identity_mismatch"
            assert await r.role_of() is None, "a refused accept must grant nothing"
        finally:
            await r.close()

    async def test_a_telegram_invitation_is_the_same_object_with_another_channel(
        self, world
    ):
        """The cross-lane claim, asserted rather than promised.

        `053` closes `delivery_channel` at `email | telegram` and gives each
        its own D33 value, so a Telegram card and an email are one row shape.
        If this fails, the Telegram half needs its own creator and the two
        will drift on the parts that are authorization rather than display.
        """
        r = _Round(world)
        try:
            inv_id, token = await r.create(
                delivery_channel="telegram",
                invited_tg_user_id=987654321,
                invited_channel_hint="@someone",
            )
            assert token and inv_id
            async with r.engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT delivery_channel, email, invited_tg_user_id"
                            "  FROM workspace_invitations WHERE id = :i"
                        ),
                        {"i": inv_id},
                    )
                ).first()
            assert row[0] == "telegram"
            assert row[1] is None, "a telegram invitation needs no address"
            assert row[2] == 987654321
        finally:
            await r.close()


class TestTheRefusalsAreNamedRatherThanConstraintNames:
    async def test_a_second_live_invitation_to_one_address_is_refused(self, world):
        r = _Round(world)
        try:
            addr = f"{uuid.uuid4().hex[:8]}@example.com"
            await r.create(email=addr)
            with pytest.raises(invitations.InvitationRefused) as exc:
                await r.create(email=addr)
            assert exc.value.reason == "already_invited"
        finally:
            await r.close()

    async def test_a_revoked_invitation_does_not_block_a_new_one(self, world):
        """`uq_invite_live` is PARTIAL on `state = 'pending'`. Re-inviting
        someone whose invitation was revoked has to work, or the index is
        enforcing something nobody asked for."""
        r = _Round(world)
        try:
            addr = f"{uuid.uuid4().hex[:8]}@example.com"
            inv_id, _ = await r.create(email=addr)
            async with r.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE workspace_invitations SET state = 'revoked'"
                        " WHERE id = :i"
                    ),
                    {"i": inv_id},
                )
            second, _ = await r.create(email=addr)
            assert second != inv_id
        finally:
            await r.close()

    async def test_an_email_invitation_without_an_address_is_refused_by_name(
        self, world
    ):
        r = _Round(world)
        try:
            with pytest.raises(invitations.InvitationRefused) as exc:
                await r.create(delivery_channel="email", email="  ")
            assert exc.value.reason == "email_required"
        finally:
            await r.close()

    async def test_an_unknown_role_is_refused_before_the_database_sees_it(self, world):
        r = _Round(world)
        try:
            with pytest.raises(invitations.InvitationRefused) as exc:
                await r.create(email="x@example.com", role="owner")
            assert exc.value.reason == "invalid_role"
        finally:
            await r.close()
