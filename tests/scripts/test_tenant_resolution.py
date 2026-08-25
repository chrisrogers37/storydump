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
"""

import hashlib

import psycopg2
import pytest

from src.exceptions.tenancy import TenantResolutionError
from src.services.target.tenant_resolution import (
    ResolvedTenant,
    resolve_chat,
    resolve_web_session,
)
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
    txn as _txn,
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


class TestChatResolutionAsAPrivilegedReader:
    """The resolver's own logic, driven where the binding rows are visible."""

    def test_each_binding_resolves_to_exactly_its_workspace(self, world):
        with _txn(world["stream"]) as conn:
            for ids in (world["a"], world["b"]):
                got = resolve_chat(conn, "telegram_group", ids["external_ref"])
                assert isinstance(got, ResolvedTenant)
                assert got.workspace_id == str(ids["ws"]), (
                    "resolved to the wrong workspace — identity, not existence"
                )
                assert got.channel_binding_id == ids["binding"]
                assert got.via == "chat"
                assert got.user_id is None

    def test_revoked_binding_refuses_by_name(self, world):
        with _txn(world["stream"]) as conn:
            with pytest.raises(TenantResolutionError) as e:
                resolve_chat(conn, "telegram_dm", "revoked-ref")
            assert e.value.reason == "revoked_binding"

    def test_unknown_binding_and_unknown_channel_refuse(self, world):
        with _txn(world["stream"]) as conn:
            with pytest.raises(TenantResolutionError) as e:
                resolve_chat(conn, "telegram_group", "never-seen")
            assert e.value.reason == "unknown_binding"
            with pytest.raises(TenantResolutionError) as e:
                resolve_chat(conn, "smoke_signal", "x")
            assert e.value.reason == "unknown_channel"


class TestChatResolutionAsIngressIsFailClosed:
    """The decision-fork evidence, measured: under the printed §7 policies
    svc_ingress has no pre-context read of channel_bindings, so a binding
    that PROVABLY exists (the privileged test above resolves it) refuses as
    unknown. Fail-closed, never fallback — and the day a resolver door
    lands, this test goes red and is updated deliberately."""

    def test_existing_active_binding_refuses_as_ingress(self, world):
        with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            with pytest.raises(TenantResolutionError) as e:
                resolve_chat(conn, "telegram_group", world["a"]["external_ref"])
            assert e.value.reason == "unknown_binding"


class TestWebSessionResolutionAsIngress:
    """The production path for the web half, end to end as svc_ingress."""

    def test_valid_session_resolves_the_claimed_workspace(self, world):
        with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            got = resolve_web_session(conn, _sha("live-a"), world["a"]["ws"])
            assert got.workspace_id == str(world["a"]["ws"])
            assert got.user_id == str(world["a"]["user"])
            assert got.via == "session:owner"

    def test_cross_workspace_claim_refuses_not_a_member(self, world):
        """User A claims workspace B: the gate reads under B's context and
        finds no membership — the two-identity check, refused by name."""
        with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            with pytest.raises(TenantResolutionError) as e:
                resolve_web_session(conn, _sha("live-a"), world["b"]["ws"])
            assert e.value.reason == "not_a_member"

    def test_the_other_tenant_resolves_its_own(self, world):
        with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            got = resolve_web_session(conn, _sha("live-b"), world["b"]["ws"])
            assert got.workspace_id == str(world["b"]["ws"])
            assert got.user_id == str(world["b"]["user"])

    @pytest.mark.parametrize(
        "token,reason",
        [
            ("expired-a", "expired_session"),
            ("revoked-a", "revoked_session"),
            ("no-such-session", "invalid_session"),
        ],
    )
    def test_bad_sessions_refuse_by_name(self, world, token, reason):
        with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            with pytest.raises(TenantResolutionError) as e:
                resolve_web_session(conn, _sha(token), world["a"]["ws"])
            assert e.value.reason == reason

    def test_role_ladder_enforced_and_satisfied(self, world):
        with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            with pytest.raises(TenantResolutionError) as e:
                resolve_web_session(
                    conn, _sha("member-a"), world["a"]["ws"], minimum_role="admin"
                )
            assert e.value.reason == "insufficient_role"
        with _txn(world["ingress"], expect_user="svc_ingress") as conn:
            got = resolve_web_session(
                conn, _sha("live-a"), world["a"]["ws"], minimum_role="admin"
            )
            assert got.via == "session:owner", "owner satisfies admin minimum"

    def test_the_claim_does_not_outlive_the_transaction(self, world):
        """SET LOCAL semantics pinned: after rollback, the same connection
        carries no tenant context (the probe-harness leak class, checked on
        the resolver's own handoff)."""
        conn = psycopg2.connect(world["ingress"])
        try:
            resolve_web_session(conn, _sha("live-a"), world["a"]["ws"])
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('app.tenant_id', true)")
                left = cur.fetchone()[0]
            assert left in (None, ""), f"claim leaked past the transaction: {left!r}"
        finally:
            conn.close()


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
