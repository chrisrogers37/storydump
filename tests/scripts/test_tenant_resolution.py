"""F.3 — the one resolver, driven against the real replayed schema (#842).

Subject discipline throughout (#852's lesson): every connection that claims
to act as a login asserts ``current_user`` before any assertion rides on it,
and every which-workspace-resolved assertion is made against TWO seeded
workspaces so the answer's identity is checked, not its existence.

The privilege facts are measured here, not narrated: the WEB half runs as
``svc_ingress`` (auth-plane + SET LOCAL claim); the CHAT half, executed as
``svc_ingress`` under the printed policies, fail-closes on a binding that
provably exists — the decision-fork evidence, pinned green so the day a
resolver door lands this test goes red and gets updated deliberately.

The resolver is async (the tier's shape, #1028); seeding stays psycopg2 —
the seeds are fixtures, the resolver is the subject, and only the subject
needs the production driver. Each call opens its own asyncpg connection,
asserts the login it claims, runs in ONE transaction, and rolls back.
"""

import hashlib
from contextlib import asynccontextmanager

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import sessions
from src.services.target.tenant_resolution import (
    ResolvedTenant,
    authorize_member,
    resolve_chat,
)
from src.services.target.unit_of_work import asyncpg_url
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

WS_A = "f3-tenant-a"
WS_B = "f3-tenant-b"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """Replayed schema + two workspaces with bindings, members and sessions.

    Everything after the first ``next`` sits inside the try so a failed
    replay or seed cannot leak the scratch DB and cluster roles (the
    documented cascade class)."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, WS_A)
            b = seed_workspace_chain(conn, WS_B)
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                for ids, name in ((a, WS_A), (b, WS_B)):
                    cur.execute(
                        "INSERT INTO channel_bindings"
                        " (workspace_id, channel, external_ref) VALUES"
                        " (%s, 'telegram_group', %s) RETURNING id",
                        (ids["ws"], f"-100{name[-1]}42"),
                    )
                    ids["binding"] = str(cur.fetchone()[0])
                    ids["external_ref"] = f"-100{name[-1]}42"
                # a revoked binding in workspace A
                cur.execute(
                    "INSERT INTO channel_bindings"
                    " (workspace_id, channel, external_ref, state) VALUES"
                    " (%s, 'telegram_dm', 'revoked-ref', 'revoked')",
                    (a["ws"],),
                )
                # a plain member (not owner) in workspace A for the ladder
                cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
                a["member_user"] = str(cur.fetchone()[0])
                cur.execute(
                    "INSERT INTO workspace_members (workspace_id, user_id, role)"
                    " VALUES (%s, %s, 'member')",
                    (a["ws"], a["member_user"]),
                )
            conn.commit()
        finally:
            conn.close()

        # Sessions are seeded AS INGRESS — the auth-plane is its surface.
        ingress = as_user(db, "svc_ingress")
        iconn = psycopg2.connect(ingress)
        try:
            with iconn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == "svc_ingress", "subject gate"
                for ids, tag in ((a, "a"), (b, "b")):
                    cur.execute(
                        "INSERT INTO session_tokens (token_hash, user_id,"
                        " expires_at) VALUES (%s, %s, now() + interval '1 day')",
                        (_sha(f"live-{tag}"), ids["user"]),
                    )
                cur.execute(
                    "INSERT INTO session_tokens (token_hash, user_id, expires_at)"
                    " VALUES (%s, %s, now() - interval '1 hour')",
                    (_sha("expired-a"), a["user"]),
                )
                cur.execute(
                    "INSERT INTO session_tokens (token_hash, user_id, expires_at,"
                    " revoked_at) VALUES (%s, %s, now() + interval '1 day', now())",
                    (_sha("revoked-a"), a["user"]),
                )
                cur.execute(
                    "INSERT INTO session_tokens (token_hash, user_id, expires_at)"
                    " VALUES (%s, %s, now() + interval '1 day')",
                    (_sha("member-a"), a["member_user"]),
                )
            iconn.commit()
        finally:
            iconn.close()
        yield {"stream": stream, "ingress": ingress, "a": a, "b": b}
    finally:
        gen.close()


@asynccontextmanager
async def _txn(dsn, expect_user=None):
    """ONE asyncpg transaction (SET LOCAL needs a transaction block), subject-
    gated when a login is named, always rolled back. NullPool + a fresh engine
    per call: an engine outlives no test, so no connection crosses loops."""
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tx = await conn.begin()
            try:
                if expect_user is not None:
                    who = (await conn.execute(text("SELECT current_user"))).scalar()
                    assert who == expect_user, f"wrong subject: {who}"
                yield conn
            finally:
                await tx.rollback()
    finally:
        await engine.dispose()


class TestChatResolutionAsAPrivilegedReader:
    """The resolver's own logic, driven where the binding rows are visible."""

    async def test_each_binding_resolves_to_exactly_its_workspace(self, world):
        async with _txn(world["stream"]) as conn:
            for ids in (world["a"], world["b"]):
                got = await resolve_chat(conn, "telegram_group", ids["external_ref"])
                assert isinstance(got, ResolvedTenant)
                assert got.workspace_id == str(ids["ws"]), (
                    "resolved to the wrong workspace — identity, not existence"
                )
                assert got.channel_binding_id == ids["binding"]
                assert got.via == "chat"
                assert got.user_id is None

    async def test_revoked_binding_refuses_by_name(self, world):
        async with _txn(world["stream"]) as conn:
            with pytest.raises(TenantResolutionError) as e:
                await resolve_chat(conn, "telegram_dm", "revoked-ref")
            assert e.value.reason == "revoked_binding"

    async def test_unknown_binding_and_unknown_channel_refuse(self, world):
        async with _txn(world["stream"]) as conn:
            with pytest.raises(TenantResolutionError) as e:
                await resolve_chat(conn, "telegram_group", "never-seen")
            assert e.value.reason == "unknown_binding"
            with pytest.raises(TenantResolutionError) as e:
                await resolve_chat(conn, "smoke_signal", "x")
            assert e.value.reason == "unknown_channel"


class TestChatResolutionAsIngressGoesThroughTheDoor:
    """The day the resolver door landed (068, #854 (a)) — updated
    deliberately, as the fail-closed pin said it would be: svc_ingress now
    resolves a bound chat pre-context through `fn_resolve_binding`, exposing
    exactly one row; an unknown chat still refuses by name."""

    async def test_existing_active_binding_resolves_as_ingress(self, world):
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            resolved = await resolve_chat(
                conn, "telegram_group", world["a"]["external_ref"]
            )
            assert str(resolved.workspace_id) == str(world["a"]["ws"])

    async def test_an_unknown_chat_still_refuses_as_ingress(self, world):
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            with pytest.raises(TenantResolutionError) as e:
                await resolve_chat(conn, "telegram_group", "-100999999")
            assert e.value.reason == "unknown_binding"


class TestWebSessionResolutionAsIngress:
    """The production path for the web half, end to end as svc_ingress: the
    router composes `sessions.resolve` (auth-plane) with `authorize_member`
    (the claimed tenant), and keeps them apart because a principal with no
    workspace is a normal state."""

    @staticmethod
    async def _resolve(conn, token, ws, minimum_role="member"):
        session = await sessions.resolve(conn, token_hash=_sha(token))
        role = await authorize_member(conn, str(ws), session.user_id, minimum_role)
        return session, role

    async def test_valid_session_resolves_the_claimed_workspace(self, world):
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            session, role = await self._resolve(conn, "live-a", world["a"]["ws"])
            assert session.user_id == str(world["a"]["user"])
            assert role == "owner"

    async def test_cross_workspace_claim_refuses_not_a_member(self, world):
        """User A claims workspace B: the gate reads under B's context and
        finds no membership — the two-identity check, refused by name."""
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            with pytest.raises(TenantResolutionError) as e:
                await self._resolve(conn, "live-a", world["b"]["ws"])
            assert e.value.reason == "not_a_member"

    async def test_the_other_tenant_resolves_its_own(self, world):
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            session, role = await self._resolve(conn, "live-b", world["b"]["ws"])
            assert session.user_id == str(world["b"]["user"])
            assert role == "owner"

    @pytest.mark.parametrize(
        "token,reason",
        [
            ("expired-a", "expired_session"),
            ("revoked-a", "revoked_session"),
            ("no-such-session", "invalid_session"),
        ],
    )
    async def test_bad_sessions_refuse_by_name(self, world, token, reason):
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            with pytest.raises(TenantResolutionError) as e:
                await self._resolve(conn, token, world["a"]["ws"])
            assert e.value.reason == reason

    async def test_a_live_session_slides_and_a_dead_one_is_not_touched(self, world):
        """`sessions.resolve` is one statement: the slide is a data-modifying
        CTE whose WHERE carries the liveness test, so the live row's
        `last_seen_at` moves and the expired row's does not."""
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            await sessions.resolve(conn, token_hash=_sha("live-a"))
            with pytest.raises(TenantResolutionError):
                await sessions.resolve(conn, token_hash=_sha("expired-a"))
            seen = (
                await conn.execute(
                    text(
                        "SELECT token_hash, last_seen_at IS NOT NULL FROM session_tokens"
                        " WHERE token_hash IN (:live, :dead)"
                    ),
                    {"live": _sha("live-a"), "dead": _sha("expired-a")},
                )
            ).all()
            assert dict(seen) == {_sha("live-a"): True, _sha("expired-a"): False}

    async def test_role_ladder_enforced_and_satisfied(self, world):
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            with pytest.raises(TenantResolutionError) as e:
                await self._resolve(conn, "member-a", world["a"]["ws"], "admin")
            assert e.value.reason == "insufficient_role"
        async with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            _, role = await self._resolve(conn, "live-a", world["a"]["ws"], "admin")
            assert role == "owner", "owner satisfies admin minimum"

    async def test_the_claim_does_not_outlive_the_transaction(self, world):
        """SET LOCAL semantics pinned: after rollback, the same connection
        carries no tenant context (the probe-harness leak class, checked on
        the resolver's own handoff)."""
        engine = create_async_engine(asyncpg_url(world["ingress"]), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                tx = await conn.begin()
                await self._resolve(conn, "live-a", world["a"]["ws"])
                await tx.rollback()
                left = (
                    await conn.execute(
                        text("SELECT current_setting('app.tenant_id', true)")
                    )
                ).scalar()
                assert left in (None, ""), (
                    f"claim leaked past the transaction: {left!r}"
                )
        finally:
            await engine.dispose()


class TestNoChatIdCrossesTheBoundary:
    """The F.6 rule's semantic half, enforced at birth: no signature in the
    target service tier accepts a chat id — the resolver is where channel
    identity dies. Source-level gate with a mutation control."""

    def test_target_tier_signatures_are_chat_free(self):
        import ast
        from pathlib import Path

        banned = {"chat_id", "telegram_chat_id", "chat_settings_id"}
        offenders = []
        pkg = Path(__file__).resolve().parents[2] / "src" / "services" / "target"
        for path in pkg.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for arg in node.args.args + node.args.kwonlyargs:
                        if arg.arg in banned:
                            offenders.append(f"{path.name}:{node.name}({arg.arg})")
        assert not offenders, f"chat identity crossed the boundary: {offenders}"

    def test_the_gate_can_fail(self):
        import ast

        tree = ast.parse("def f(telegram_chat_id): ...")
        node = tree.body[0]
        assert any(a.arg == "telegram_chat_id" for a in node.args.args)
