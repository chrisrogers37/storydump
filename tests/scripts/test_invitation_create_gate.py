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

from src.services.target import commands, invitations, sessions
from src.services.target.commands import Command, CommandRefused
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


class TestTheExecutorDoesNotNarrowTheWriter:
    """The defect this class exists for: `invitations.create` accepted both
    channels from the day it shipped, and `invite_member` passed the literal
    `"email"`, so no Telegram invitation could be minted through the command
    port at all. Every test above drives `create` DIRECTLY, which is precisely
    why that survived — a gate on the door says nothing about the caller.

    So these go through `commands.execute` — the real dispatch door, which
    also walks the registry entry and the role floor — and one of them is
    the same assertion as
    `test_a_telegram_invitation_is_the_same_object_with_another_channel`
    deliberately: the writer was never the broken half.
    """

    async def _execute(self, world, args):
        engine = create_async_engine(_async_url(world["dsn"]))
        try:
            async with engine.begin() as conn:
                return await commands.execute(
                    conn,
                    Command(
                        kind="invite_member",
                        workspace_id=world["ws"],
                        actor_user_id=world["user"],
                        channel="web",
                        args=args,
                    ),
                )
        finally:
            await engine.dispose()

    async def test_a_telegram_invitation_can_be_minted_through_the_command(self, world):
        """The bug, stated as the thing that could not happen."""
        result = await self._execute(
            world,
            {
                "delivery_channel": "telegram",
                "invited_tg_user_id": 4242,
                # Carried here for a reason worth stating: `delivery_channel`
                # and `invited_tg_user_id` reach the executor in this test
                # while `invited_channel_hint` only ever appeared in a DIRECT
                # `create()` call. Three arguments added by one change, two
                # exercised through the real caller and one not — which is the
                # same shape as the defect this class exists for, since a test
                # of the callee says nothing about what the caller passes.
                "invited_channel_hint": "@someone",
            },
        )
        assert result.outcome == "executed"
        engine = create_async_engine(_async_url(world["dsn"]))
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT delivery_channel, email, invited_tg_user_id,"
                            "       invited_channel_hint"
                            " FROM workspace_invitations WHERE id = :i"
                        ),
                        {"i": result.data["invitation_id"]},
                    )
                ).first()
        finally:
            await engine.dispose()
        assert row[0] == "telegram"
        # NULL, not empty string: `uq_invite_live` is (workspace_id, email) and
        # NULLs never collide, which is what lets telegram invitations coexist
        # with an email invite to the same workspace.
        assert row[1] is None
        assert row[2] == 4242
        assert row[3] == "@someone", (
            "the executor dropped invited_channel_hint — it is display data the"
            " card producer renders, and losing it is silent"
        )

    async def test_email_remains_the_default_so_clause_3_is_unchanged(self, world):
        """A caller that names no channel still gets the shipped behaviour."""
        result = await self._execute(world, {"email": "default@example.com"})
        assert result.outcome == "executed"
        assert result.data["invite_token"]

    async def test_an_unknown_channel_is_refused_by_name_not_by_constraint(self, world):
        """`ck_invite_channel` would also stop this. The refusal names the
        field instead, which is the same trade `email_required` already makes."""
        with pytest.raises(CommandRefused) as exc:
            await self._execute(world, {"delivery_channel": "carrier_pigeon"})
        assert "email or telegram" in str(exc.value) or "email, telegram" in str(
            exc.value
        )

    async def test_a_json_true_cannot_become_the_user_id_one(self, world):
        """`isinstance(True, bool)` and `isinstance(True, int)` are BOTH true in
        Python, so a bare int check would write `1` — a real Telegram user id —
        for a JSON `true`. Ordinary type confusion everywhere else; here it
        addresses an invitation at a stranger."""
        with pytest.raises(CommandRefused) as exc:
            await self._execute(
                world, {"delivery_channel": "telegram", "invited_tg_user_id": True}
            )
        assert "invited_tg_user_id" in str(exc.value)


class TestTheEmailProducer:
    """`06` §2's email arm: *"a `send_email` job through the `07` §1 port to
    the invited address"*.

    Everything downstream of this was already built — port, executor, budget,
    retry ladder, the `invitation` template — and **nothing minted a job**, so
    the whole channel was unreachable. These drive `commands.execute`, so they
    cover the producer AND the wiring, which is the pair the delivery_channel
    defect showed can disagree.
    """

    async def _invite(self, world, args, *, origin="https://app.example.test"):
        engine = create_async_engine(_async_url(world["dsn"]))
        try:
            import src.services.target.command_executors as ce

            class _Settings:
                web_app_origin = origin

            original = ce.settings
            ce.settings = _Settings()
            try:
                async with engine.begin() as conn:
                    return await commands.execute(
                        conn,
                        Command(
                            kind="invite_member",
                            workspace_id=world["ws"],
                            actor_user_id=world["user"],
                            channel="web",
                            args=args,
                        ),
                    )
            finally:
                ce.settings = original
        finally:
            await engine.dispose()

    async def _job(self, world, job_id):
        engine = create_async_engine(_async_url(world["dsn"]))
        try:
            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT kind, workspace_id, lane, state,"
                            "       serialization_key, payload"
                            " FROM jobs WHERE id = :i"
                        ),
                        {"i": job_id},
                    )
                ).mappings()
                return row.first()
        finally:
            await engine.dispose()

    async def test_an_email_invitation_enqueues_a_send_email_job(self, world):
        """The gap this closes, stated as the row that never existed."""
        result = await self._invite(world, {"email": "Invitee@Example.com"})
        delivery = result.data["delivery"]
        assert delivery["channel"] == "email"
        assert delivery["state"] == "queued"

        job = await self._job(world, delivery["job_id"])
        assert job["kind"] == "send_email"
        assert job["state"] == "ready"
        # NULL by `ck_jobs_system_kinds`, which is a biconditional — a workspace
        # id here would not merely be untidy, the INSERT would fail.
        assert job["workspace_id"] is None
        assert job["payload"]["to"] == "Invitee@Example.com"
        assert job["payload"]["template"] == "invitation"
        assert job["payload"]["v"] == 1

    async def test_the_accept_url_carries_the_token_the_acceptor_resolves(self, world):
        """The link is the whole credential, so it has to be THE token — not a
        second one, and not the invitation id. Asserted by round-tripping the
        value out of the URL through the real accept door."""
        result = await self._invite(world, {"email": "roundtrip@example.com"})
        job = await self._job(world, result.data["delivery"]["job_id"])
        accept_url = job["payload"]["params"]["accept_url"]
        assert accept_url.startswith("https://app.example.test/join/")

        from_url = accept_url.rsplit("/", 1)[-1]
        assert from_url == result.data["invite_token"]
        # And it actually accepts — the token in the email is not merely equal
        # to the returned one, it resolves at the door the email points at.
        round_ = _Round(world)
        try:
            outcome = await round_.accept(from_url, email="roundtrip@example.com")
            assert outcome is not None
        finally:
            await round_.engine.dispose()

    async def test_the_job_is_claimable_so_the_channel_is_actually_reachable(
        self, world
    ):
        """The point of the whole change: a real worker can pick this up.

        Asserting the row exists would pass even if it were minted into a lane
        or a state nothing claims — which is the shape the entire email channel
        was already in.
        """
        result = await self._invite(world, {"email": "claimable@example.com"})
        engine = create_async_engine(_async_url(world["dsn"]))
        try:
            async with engine.begin() as conn:
                claimed = (
                    await conn.execute(
                        text(
                            "SELECT id FROM jobs"
                            " WHERE state = 'ready' AND run_at <= now()"
                            "   AND kind = 'send_email'"
                            "   AND serialization_key = :k"
                            " ORDER BY run_at LIMIT 1"
                        ),
                        {"k": f"email:inv:{result.data['invitation_id']}"},
                    )
                ).scalar()
        finally:
            await engine.dispose()
        # str(): asyncpg returns a uuid.UUID and the job id travels as text.
        assert str(claimed) == result.data["delivery"]["job_id"]

    async def test_no_origin_enqueues_nothing_and_says_so(self, world):
        """A template that requires `accept_url` would refuse at RENDER, in the
        worker, having burned an attempt to learn something knowable here.

        The invitation still stands — the token is returned and can be shared
        by hand — so this is a delivery outcome, not a reason to refuse the
        command. What it must never be is silent.
        """
        result = await self._invite(
            world, {"email": "noorigin@example.com"}, origin=None
        )
        assert result.data["delivery"] == {
            "channel": "email",
            "state": "not_configured",
        }
        assert result.data["invite_token"]

        # Scoped to THIS invitation, not the whole table: the lane is
        # module-scoped, so earlier tests in this class have left their own
        # `send_email` rows behind and a global count would answer about them.
        engine = create_async_engine(_async_url(world["dsn"]))
        try:
            async with engine.begin() as conn:
                count = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM jobs WHERE kind = 'send_email'"
                            " AND serialization_key = :k"
                        ),
                        {"k": f"email:inv:{result.data['invitation_id']}"},
                    )
                ).scalar()
        finally:
            await engine.dispose()
        assert count == 0, "a job was enqueued that could never render"
