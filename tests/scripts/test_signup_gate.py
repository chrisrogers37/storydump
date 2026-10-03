"""092: a new account needs a way in while in beta (`07` §35).

`fn_signup_admitted` answers whether a new Google account with a verified
email may create its user: the owner admitted the address, or a pending,
unexpired invitation is addressed to it. It runs here as the production role
(`svc_ingress`) on the replayed advertised stream, and so does the sign-in
upsert that asks it, so the bound parameter's type is the real one too. The
admissions are seeded as the schema owner, the way the owner admits someone.

098 (`07` §41) holds the invitation branch to the invitation's standing, read
live: it counts only from an `active` workspace whose owner or admin still sent
it, so a removed or demoted inviter, a suspended workspace and an invitation
with no recorded inviter admit nobody new.
"""

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import parse_qs, urlsplit

import psycopg2
import psycopg2.errors
import pytest

from src.api.principal import COOKIE
from src.api.routes import auth
from src.services.target import google_oidc, identity
from tests.scripts.conftest import (
    _scratch,
    as_user,
    in_user_plane,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import (
    FRONT,
    api_client,
    cookie_value,
    unsigned_id_token,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

google_configured = api_conftest.google_configured

#: (address, state, expires in) for each invitation the world holds.
INVITATIONS = {
    "live@example.com": ("pending", "7 days"),
    "revoked@example.com": ("revoked", "7 days"),
    "accepted@example.com": ("accepted", "7 days"),
    "lapsed@example.com": ("expired", "-1 day"),
    "unswept@example.com": ("pending", "-1 minute"),  # past expiry, not yet reaped
    # Not as `invitations.create` writes it: the door lowers both sides.
    "Mixed@Example.com": ("pending", "7 days"),
}
LIVE = {"live@example.com", "Mixed@Example.com"}

#: (address, who sent it) for each pending, unexpired invitation whose
#: standing 098 judges. The owner sent every invitation above.
SENT_BY = {
    "from-admin@example.com": "admin",
    "from-removed@example.com": "removed",
    "from-demoted@example.com": "demoted",
    "from-suspended@example.com": "suspended_owner",
    "from-nobody@example.com": None,  # a service identity's: no inviter recorded
}
STANDING = {"from-admin@example.com"}


def _invite(cur, ws, email, *, by, state="pending", expires="7 days"):
    cur.execute(
        "INSERT INTO workspace_invitations (workspace_id, token_hash,"
        " delivery_channel, email, state, expires_at, invited_by_user_id)"
        " VALUES (%s, %s, 'email', %s, %s, now() + %s::interval, %s)",
        (ws, hashlib.sha256(email.encode()).hexdigest(), email, state, expires, by),
    )


def _user(cur, ws=None, role=None):
    cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
    user = cur.fetchone()[0]
    if ws is not None:
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, %s)",
            (ws, user, role),
        )
    return user


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner_dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(owner_dsn)
        try:
            chain = seed_workspace_chain(conn, "signup")
            suspended = seed_workspace_chain(conn, "signup-suspended")
            ws = chain["ws"]
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO signup_admissions (email, note)"
                    " VALUES ('friend@example.com', 'from the waitlist')"
                )
                for email, (state, expires) in INVITATIONS.items():
                    _invite(
                        cur, ws, email, by=chain["user"], state=state, expires=expires
                    )
                # Each inviter as they stand once their invitation is out: the
                # removed one is no longer a member, the demoted one is a member.
                inviters = {
                    "admin": _user(cur, ws, "admin"),
                    "removed": _user(cur),
                    "demoted": _user(cur, ws, "member"),
                    "suspended_owner": suspended["user"],
                    None: None,
                }
                for email, by in SENT_BY.items():
                    home = suspended["ws"] if by == "suspended_owner" else ws
                    _invite(cur, home, email, by=inviters[by])
                cur.execute(
                    "UPDATE workspaces SET state = 'suspended' WHERE id = %s",
                    (suspended["ws"],),
                )
            conn.commit()
        finally:
            conn.close()
        yield {
            "owner": owner_dsn,
            "ingress": as_user(db, "svc_ingress"),
            "worker": as_user(db, "svc_worker"),
        }
    finally:
        gen.close()


def _one(dsn: str, sql: str, params=()):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]
    finally:
        conn.close()


def _admitted(world, email):
    return _one(world["ingress"], "SELECT fn_signup_admitted(%s)", (email,))


class TestTheDoor:
    def test_an_address_nobody_admitted_or_invited_is_refused(self, world):
        assert _admitted(world, "stranger@example.com") is False

    def test_no_address_is_refused(self, world):
        assert _admitted(world, None) is False

    @pytest.mark.parametrize("email", ["friend@example.com", "Friend@Example.COM"])
    def test_an_admitted_address_is_let_in_whatever_its_case(self, world, email):
        assert _admitted(world, email) is True

    @pytest.mark.parametrize(
        "email", ["live@example.com", "LIVE@example.com", "mixed@example.com"]
    )
    def test_a_live_invitation_lets_its_address_in(self, world, email):
        assert _admitted(world, email) is True

    @pytest.mark.parametrize(
        "email",
        [e for e in INVITATIONS if e not in LIVE],
    )
    def test_a_spent_or_lapsed_invitation_lets_nobody_in(self, world, email):
        assert _admitted(world, email) is False

    def test_a_live_invitation_from_a_current_admin_lets_its_address_in(self, world):
        assert _admitted(world, "from-admin@example.com") is True

    @pytest.mark.parametrize("email", [e for e in SENT_BY if e not in STANDING])
    def test_an_invitation_that_lost_its_standing_lets_nobody_in(self, world, email):
        """098: the inviter was removed or demoted, the workspace is suspended,
        or nobody is recorded as the inviter — the invitation is pending and
        unexpired, and still admits no new account."""
        assert _admitted(world, email) is False

    def test_the_door_is_svc_ingress_alone(self, world):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _one(world["worker"], "SELECT fn_signup_admitted('friend@example.com')")


class TestTheAdmissionsTable:
    @pytest.mark.parametrize("login", ["ingress", "worker"])
    def test_the_runtime_roles_cannot_read_it(self, world, login):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _one(world[login], "SELECT count(*) FROM signup_admissions")

    @pytest.mark.parametrize(
        "typed",
        [
            "Shout@Example.com",
            "pasted@example.com ",
            "pasted@example.com\t",
            "pasted@example.com\n",
            "pasted@example.com\u00a0",
            "\ufeffbom@example.com",
            "zero@example.com\u200b",
            "narrow@example.com\u202f",
            "no-at-sign.example.com",
        ],
    )
    def test_an_address_that_would_never_match_is_refused_at_the_insert(
        self, world, typed
    ):
        """Capitals, any whitespace (a spreadsheet's tab or newline, an email
        client's no-break space) and a missing @ are refused, not stored."""
        with pytest.raises(psycopg2.errors.CheckViolation):
            _one(
                world["owner"],
                "INSERT INTO signup_admissions (email) VALUES (%s) RETURNING email",
                (typed,),
            )

    def test_an_accented_address_is_stored(self, world):
        """The invisible-character ban stops short of letters: Latin-1's
        accented letters start above the range it refuses."""
        assert (
            _one(
                world["owner"],
                "INSERT INTO signup_admissions (email) VALUES (%s) RETURNING email",
                ("josé@example.com",),
            )
            == "josé@example.com"
        )


def _sign_in(world, sub, email):
    return asyncio.run(
        in_user_plane(
            world["ingress"],
            lambda c: identity.upsert_google_identity(
                c, sub=sub, email=email, display_name=None
            ),
        )
    )


def _users_with(world, email) -> int:
    return _one(
        world["owner"], "SELECT count(*) FROM users WHERE primary_email = %s", (email,)
    )


class TestTheSignInUpsert:
    def test_a_stranger_creates_no_user(self, world):
        with pytest.raises(identity.SignupNotAdmitted):
            _sign_in(world, "sub-stranger", "stranger2@example.com")
        assert _users_with(world, "stranger2@example.com") == 0

    def test_an_invitee_creates_their_user_and_signs_in_again(self, world):
        user = _sign_in(world, "sub-invitee", "live@example.com")
        assert _users_with(world, "live@example.com") == 1
        assert _sign_in(world, "sub-invitee", "live@example.com") == user


def _callback(world, monkeypatch, *, sub, email):
    """The real sign-in route pair as `svc_ingress`, Google stubbed, with the
    deployment's default switch (gated)."""

    async def main():
        async with api_client(world["ingress"]) as (client, _):
            start = await client.get("/auth/google", follow_redirects=False)
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            nonce = cookie_value(start, auth.NONCE_COOKIE)

            async def exchange_code(client_, **kw):
                return unsigned_id_token(state, sub=sub, email=email, name=sub)

            monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
            return await client.get(
                f"/auth/google/callback?state={state}&code=c0de",
                headers={"Cookie": f"{auth.NONCE_COOKIE}={nonce}"},
                follow_redirects=False,
            )

    return asyncio.run(main())


class TestTheSignInRoute:
    def test_a_stranger_lands_on_the_sign_in_page_with_no_session(
        self, world, google_configured, monkeypatch
    ):
        done = _callback(world, monkeypatch, sub="sub-r1", email="nobody@example.com")
        assert done.headers["location"] == f"{FRONT}/login?error=not_admitted"
        assert COOKIE not in done.headers.get("set-cookie", "")
        assert _users_with(world, "nobody@example.com") == 0

    def test_an_admitted_address_signs_up(self, world, google_configured, monkeypatch):
        done = _callback(world, monkeypatch, sub="sub-r2", email="friend@example.com")
        assert done.headers["location"] == f"{FRONT}/welcome"
        assert _users_with(world, "friend@example.com") == 1
