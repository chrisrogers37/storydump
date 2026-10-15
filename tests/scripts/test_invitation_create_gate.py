"""#1172 — the create half, driven against the acceptor.

The risk in the create half is not that it fails — it is that it succeeds
against a shape the acceptor (`fn_invitation_accept` and `/join/[token]`,
#1090) disagrees with, and nobody finds out until a real person clicks a real
link.

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

import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.config.settings import settings
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    commands,
    email_sender,
    invitations,
    rate_counters,
    sessions,
)
from src.services.target.commands import Command, CommandRefused
from src.services.target.unit_of_work import apply_gucs
from tests.scripts.conftest import (
    _scratch,
    actor_lacks_createrole,
    as_user,
    async_url,
    fetch_all,
    in_tenant,
    rows_holding,
    run_bootstrap,
    seed_workspace_chain,
    set_test_passwords,
)
from tests.scripts.test_lineage_lane import run_lane
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import api_client, owned_workspace
from tests.src.services.target.conftest import RecordingSender

google_configured = api_conftest.google_configured

pytestmark = [pytest.mark.integration]


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
        # The revoke gate connects as `svc_ingress`, whose password the
        # bootstrap leaves to deployment env.
        set_test_passwords(admin_conn)
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
        self.engine = create_async_engine(async_url(world["dsn"]))

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


#: The web origin the invite tests' deployment is configured with.
ORIGIN = "https://app.example.test"


async def _execute_invite(world, args, *, origin=ORIGIN, actor=None):
    """`invite_member` through `commands.execute`, the executor the web's
    command route dispatches to, on a deployment whose web origin is *origin*."""
    engine = create_async_engine(async_url(world["dsn"]))
    try:
        with mock.patch.object(settings, "WEB_APP_URL", origin):
            async with engine.begin() as conn:
                await apply_gucs(conn, tenant_id=str(world["ws"]))
                return await commands.execute(
                    conn,
                    Command(
                        kind="invite_member",
                        workspace_id=world["ws"],
                        actor_user_id=actor or world["user"],
                        channel="web",
                        args=args,
                    ),
                )
    finally:
        await engine.dispose()


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
                "a verified email equal to the invited one is the matched"
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
        """`role` is a CEILING, not a grant — and the creator cannot
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
        its own acceptance value, so a Telegram card and an email are one
        row shape. If this fails, the Telegram half needs its own creator
        and the two will drift on the parts that are authorization rather
        than display.
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
    async def test_re_inviting_an_address_replaces_its_pending_invitation(self, world):
        """A second send to one address revokes the first: its link stops
        working, the new one is the only live invitation, and it is the new
        send's role that is granted. Without the revoke `uq_invite_live` would
        refuse the send, and an invitation the accept door refuses (098)
        would keep refusing it until the reaper expired it."""
        r = _Round(world)
        try:
            addr = f"{uuid.uuid4().hex[:8]}@example.com"
            first, old_token = await r.create(email=addr, role="member")
            second, new_token = await r.create(email=addr, role="admin")
            assert second != first

            with pytest.raises(invitations.InvitationRefused) as exc:
                await r.accept(old_token, email=addr)
            assert exc.value.reason == "not_acceptable"
            out = await r.accept(new_token, email=addr)
            assert out["role"] == "admin"

            async with r.engine.begin() as conn:
                states = (
                    await conn.execute(
                        text(
                            "SELECT id, state FROM workspace_invitations"
                            " WHERE id IN (:a, :b)"
                        ),
                        {"a": first, "b": second},
                    )
                ).all()
            assert dict((str(i), st) for i, st in states) == {
                first: "revoked",
                second: "accepted",
            }
        finally:
            await r.close()

    async def test_re_inviting_a_telegram_id_replaces_its_pending_invitation(
        self, world
    ):
        """The addressee of a Telegram invitation is its Telegram id, so a
        second send to that id revokes the first exactly as an email one does;
        no index forces it, so without the revoke both would stay live."""
        r = _Round(world)
        try:
            kw = {"delivery_channel": "telegram", "invited_tg_user_id": 555000111}
            first, _ = await r.create(**kw)
            second, _ = await r.create(**kw)
            async with r.engine.begin() as conn:
                states = (
                    await conn.execute(
                        text(
                            "SELECT id, state FROM workspace_invitations"
                            " WHERE id IN (:a, :b)"
                        ),
                        {"a": first, "b": second},
                    )
                ).all()
            assert dict((str(i), st) for i, st in states) == {
                first: "revoked",
                second: "pending",
            }
        finally:
            await r.close()

    async def test_a_re_invite_leaves_another_workspaces_invitation_alone(self, world):
        """The revoke is this workspace's: the same addressee's pending
        invitation to another workspace stays live."""
        conn = psycopg2.connect(world["dsn"])
        try:
            other = seed_workspace_chain(conn, f"invite-other-{uuid.uuid4().hex[:8]}")
        finally:
            conn.close()
        r = _Round(world)
        try:
            addr = f"{uuid.uuid4().hex[:8]}@example.com"
            tg = {"delivery_channel": "telegram", "invited_tg_user_id": 555000222}
            theirs = [
                (
                    await r.create(
                        **kw, workspace_id=other["ws"], invited_by_user_id=other["user"]
                    )
                )[0]
                for kw in ({"email": addr}, tg)
            ]
            await r.create(email=addr)
            await r.create(**tg)
            states = fetch_all(
                world["dsn"],
                "SELECT state FROM workspace_invitations WHERE id IN (%s, %s)",
                tuple(theirs),
            )
            assert [row["state"] for row in states] == ["pending", "pending"]
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


class TestTheLinkIsShownOnce:
    """#1563/#1564: an admin mints an invitation from the web and is shown its
    link once. The token reaches the database only as its SHA-256, so the
    command's response is the only place it appears.

    An origin is configured unless a test says otherwise, so an email arm that
    ran would have written its accept URL.
    """

    @staticmethod
    def _address():
        return f"{uuid.uuid4().hex[:8]}@example.com"

    async def test_the_response_carries_the_join_link_and_its_expiry(self, world):
        result = await _execute_invite(world, {"email": self._address()})
        token = result.data["invite_token"]
        assert result.data["join_url"] == f"{ORIGIN}/join/{token}"
        left = datetime.fromisoformat(result.data["expires_at"]) - datetime.now(
            timezone.utc
        )
        assert timedelta(days=7) - timedelta(minutes=5) < left <= timedelta(days=7)
        [row] = fetch_all(
            world["dsn"],
            "SELECT expires_at FROM workspace_invitations WHERE id = %s",
            (result.data["invitation_id"],),
        )
        assert datetime.fromisoformat(result.data["expires_at"]) == row["expires_at"]

    async def test_with_no_web_origin_there_is_no_link_and_nothing_to_send(self, world):
        result = await _execute_invite(world, {"email": self._address()}, origin=None)
        assert result.data["join_url"] is None
        assert result.data["invite_token"]
        assert result.data["delivery"] == {
            "channel": "email",
            "state": "not_configured",
        }
        assert result.after_commit is None

    async def test_the_executor_hands_the_email_to_the_adapter(self, world):
        """The send rides `after_commit`, addressed from the stored row, for the
        adapter to run once this commits and to answer its `delivery`."""
        address = self._address()
        result = await _execute_invite(world, {"email": address.upper()})
        assert result.data["delivery"] == {"channel": "email"}
        email = result.after_commit
        assert isinstance(email, invitations.InvitationEmail)
        assert email.to == address, "the stored, lower-cased address"
        assert email.params["accept_url"] == result.data["join_url"]

    async def test_the_token_reaches_the_database_only_as_its_hash(self, world):
        result = await _execute_invite(world, {"email": self._address()})
        token = result.data["invite_token"]
        assert rows_holding(world["dsn"], token) == {}
        # The scan's own control: it finds the hash, in the one row that holds it.
        assert rows_holding(world["dsn"], sessions.token_hash(token)) == {
            "public.workspace_invitations": 1
        }

    async def test_the_token_is_logged_nowhere(self, world, caplog):
        with caplog.at_level(logging.DEBUG):
            result = await _execute_invite(world, {"email": self._address()})
        token = result.data["invite_token"]
        assert [r.name for r in caplog.records if token in r.getMessage()] == []

    async def test_a_member_below_admin_cannot_mint_one(self, world):
        r = _Round(world)
        try:
            address = self._address()
            _id, token = await r.create(email=address)
            await r.accept(token, email=address)
            assert await r.role_of() == "member"
        finally:
            await r.close()
        with pytest.raises(TenantResolutionError) as refused:
            await _execute_invite(
                world, {"email": self._address()}, actor=world["invitee"]
            )
        assert refused.value.reason == "insufficient_role"

    async def test_the_link_is_refused_once_it_has_expired(self, world):
        address = self._address()
        result = await _execute_invite(world, {"email": address})
        token = result.data["invite_token"]
        r = _Round(world)

        async def expire_at(moment):
            async with r.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE workspace_invitations SET expires_at = "
                        + moment
                        + " WHERE id = :i"
                    ),
                    {"i": result.data["invitation_id"]},
                )

        try:
            await expire_at("now() - interval '1 second'")
            with pytest.raises(invitations.InvitationRefused) as refused:
                await r.accept(token, email=address)
            assert refused.value.reason == "not_acceptable"
            # The control: the same link with its expiry back in the future is
            # accepted, so the refusal above was the expiry's.
            await expire_at("now() + interval '1 hour'")
            joined = await r.accept(token, email=address)
            assert joined["workspace_id"] == str(world["ws"])
        finally:
            await r.close()

    async def test_the_link_works_once(self, world):
        address = self._address()
        result = await _execute_invite(world, {"email": address})
        token = result.data["join_url"].rsplit("/", 1)[-1]
        r = _Round(world)
        try:
            joined = await r.accept(token, email=address)
            assert joined["workspace_id"] == str(world["ws"])
            with pytest.raises(invitations.InvitationRefused) as again:
                await r.accept(token, email=address)
            assert again.value.reason == "not_acceptable"
        finally:
            await r.close()


async def _revoke(world, invitation_id, *, dsn=None, expect_user="svc_ingress"):
    """`revoke_invitation` through `commands.execute`, in the web's unit of
    work under the tenant, as *expect_user*: the API's own login by default,
    or the owner's, where the policies stand aside and only the statement's
    own `workspace_id` predicate can refuse another workspace's row."""

    async def go(session):
        return await commands.execute(
            session,
            Command(
                kind="revoke_invitation",
                workspace_id=str(world["ws"]),
                actor_user_id=world["user"],
                channel="web",
                args={"invitation_id": str(invitation_id)},
            ),
        )

    return await in_tenant(
        dsn or as_user(world["dsn"], "svc_ingress"),
        world["ws"],
        world["user"],
        go,
        expect_user=expect_user,
    )


def _login_of(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            return cur.fetchone()[0]
    finally:
        conn.close()


class TestAnAdminRevokesAPendingInvitation:
    """`revoke_invitation`, as `svc_ingress` under the tenant: the
    link it withdraws is refused at accept, and both fences hold — the policies
    and, for a login that bypasses them, the statement's own predicate."""

    @staticmethod
    def _address():
        return f"revoke-{uuid.uuid4().hex[:8]}@example.com"

    async def test_a_revoked_link_is_refused_at_accept(self, world):
        address = self._address()
        made = await _execute_invite(world, {"email": address})
        result = await _revoke(world, made.data["invitation_id"])
        assert result.outcome == "executed"
        assert result.data == {"invitation_id": str(made.data["invitation_id"])}
        token = made.data["join_url"].rsplit("/", 1)[-1]
        r = _Round(world)
        try:
            with pytest.raises(invitations.InvitationRefused) as refused:
                await r.accept(token, email=address)
            assert refused.value.reason == "not_acceptable"
            assert await r.role_of() is None
        finally:
            await r.close()

    async def test_a_second_revoke_is_not_found(self, world):
        made = await _execute_invite(world, {"email": self._address()})
        await _revoke(world, made.data["invitation_id"])
        with pytest.raises(CommandRefused) as again:
            await _revoke(world, made.data["invitation_id"])
        assert again.value.reason == "not_found"

    async def test_another_workspaces_invitation_is_not_found_and_stays_live(
        self, world
    ):
        conn = psycopg2.connect(world["dsn"])
        try:
            other = seed_workspace_chain(conn, f"revoke-other-{uuid.uuid4().hex[:8]}")
        finally:
            conn.close()
        theirs_world = {**world, "ws": other["ws"], "user": other["user"]}
        address = self._address()
        theirs = await _execute_invite(theirs_world, {"email": address})
        with pytest.raises(CommandRefused) as refused:
            await _revoke(world, theirs.data["invitation_id"])
        assert refused.value.reason == "not_found"
        # The owner's login reads past the policies, so here only the
        # statement's own `workspace_id = :ws` can refuse.
        with pytest.raises(CommandRefused) as by_owner:
            await _revoke(
                world,
                theirs.data["invitation_id"],
                dsn=world["dsn"],
                expect_user=_login_of(world["dsn"]),
            )
        assert by_owner.value.reason == "not_found"
        token = theirs.data["join_url"].rsplit("/", 1)[-1]
        r = _Round(theirs_world)
        try:
            joined = await r.accept(token, email=address)
            assert joined["workspace_id"] == str(other["ws"])
        finally:
            await r.close()


class _CommitProbe(RecordingSender):
    """The invitation email's transport, recorded — with whether its
    invitation was already committed when it went, read on a connection of its
    own, which sees committed rows only."""

    def __init__(self, dsn):
        super().__init__()
        self.dsn = dsn

    async def send(self, *, to, subject, body):
        ref = await super().send(to=to, subject=subject, body=body)
        mail = self.sent[-1]
        [row] = fetch_all(
            self.dsn,
            "SELECT count(*) AS n FROM workspace_invitations"
            " WHERE token_hash = %s AND state = 'pending'",
            (sessions.token_hash(mail["token"]),),
        )
        mail["committed"] = row["n"]
        return ref


async def _invite(client, owner, ws, email):
    """`invite_member` through the real command route, keyed by its address."""
    resp = await client.post(
        f"/api/v1/workspaces/{ws}/commands/invite_member",
        json={"email": email, "role": "member"},
        headers={**owner, "Idempotency-Key": f"inv-{email}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _spend_the_providers_day(dsn, key):
    """Seed *key*'s counter at the provider-wide ceiling, today and tomorrow,
    so a run that straddles midnight UTC still finds the day spent."""
    today = rate_counters.window_start(
        datetime.now(timezone.utc), email_sender.BUDGET_WINDOW_SECONDS
    )
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            for window in (today, today + timedelta(days=1)):
                cur.execute(
                    "INSERT INTO rate_counters (scope, key, window_start, count)"
                    " VALUES (%s, %s, %s, %s)",
                    (email_sender.BUDGET_SCOPE, key, window, email_sender.BUDGET_LIMIT),
                )
    finally:
        conn.close()


@pytest.mark.slow
class TestTheInvitationIsEmailedAfterItCommits:
    """Through the real command route as `svc_ingress`, the email goes after
    the invitation commits, so its link exists only in that request's memory —
    and an email that does not go still returns the link."""

    def _ingress(self, world):
        return as_user(world["dsn"], "svc_ingress")

    async def test_the_email_goes_after_the_commit_and_its_link_accepts(
        self, world, google_configured, monkeypatch
    ):
        sender = _CommitProbe(world["dsn"])
        async with api_client(self._ingress(world), email=sender) as (client, _):
            owner, _id, ws = await owned_workspace(client, monkeypatch, tag="sent")
            made = await _invite(client, owner, ws, "Partner@Example.com")
        assert made["delivery"] == {"channel": "email", "state": "sent"}
        [mail] = sender.sent
        assert mail["committed"] == 1, "the email went before its invitation committed"
        assert mail["to"] == "partner@example.com"
        assert mail["subject"] == email_sender.INVITATION_SUBJECT
        assert mail["body"].startswith('"sub-sent" has invited you'), "the Google name"
        assert mail["token"] == made["invite_token"]
        assert made["join_url"].endswith(f"/join/{mail['token']}")
        assert rows_holding(world["dsn"], mail["token"]) == {}
        r = _Round({**world, "ws": ws})
        try:
            joined = await r.accept(mail["token"], email="partner@example.com")
        finally:
            await r.close()
        assert joined["workspace_id"] == ws

    async def test_a_persons_spent_day_sends_nothing_and_still_returns_the_link(
        self, world, google_configured, monkeypatch
    ):
        monkeypatch.setattr(invitations, "INVITES_PER_PERSON_PER_DAY", 1)
        sender = _CommitProbe(world["dsn"])
        async with api_client(self._ingress(world), email=sender) as (client, _):
            owner, _id, ws = await owned_workspace(client, monkeypatch, tag="cap")
            first = await _invite(client, owner, ws, "one@example.com")
            second = await _invite(client, owner, ws, "two@example.com")
        assert first["delivery"]["state"] == "sent"
        assert second["delivery"] == {
            "channel": "email",
            "state": "over_budget",
            "reason": "person_limit",
        }
        assert second["join_url"]
        assert [m["to"] for m in sender.sent] == ["one@example.com"]

    async def test_the_providers_spent_day_refuses_everyone(
        self, world, google_configured, monkeypatch
    ):
        # A key of its own: the module's other sends spend the real one, and a
        # window's first hit always lands, whatever the limit.
        key = f"gate-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(email_sender, "BUDGET_KEY", key)
        _spend_the_providers_day(world["dsn"], key)
        sender = _CommitProbe(world["dsn"])
        async with api_client(self._ingress(world), email=sender) as (client, _):
            owner, _id, ws = await owned_workspace(client, monkeypatch, tag="daily")
            made = await _invite(client, owner, ws, "late@example.com")
        assert made["delivery"] == {
            "channel": "email",
            "state": "over_budget",
            "reason": "daily_limit",
        }
        assert made["join_url"]
        assert sender.sent == []

    async def test_a_replay_sends_nothing_more(
        self, world, google_configured, monkeypatch
    ):
        sender = _CommitProbe(world["dsn"])
        async with api_client(self._ingress(world), email=sender) as (client, _):
            owner, _id, ws = await owned_workspace(client, monkeypatch, tag="replay")
            await _invite(client, owner, ws, "again@example.com")
            again = await _invite(client, owner, ws, "again@example.com")
        assert again == {"outcome": "replayed"}
        assert len(sender.sent) == 1
