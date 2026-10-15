"""092: a new account needs a way in while in beta.

`fn_signup_admitted` answers whether a new Google account with a verified
email may create its user: the owner admitted the address, or a pending,
unexpired invitation is addressed to it. It runs here as the production role
(`svc_ingress`) on the replayed advertised stream, and so does the sign-in
upsert that asks it, so the bound parameter's type is the real one too. The
admissions are seeded as the schema owner, the way the owner admits someone.

098 holds the invitation branch to the invitation's standing, read
live: it counts only from an `active` workspace whose owner or admin still sent
it, so a removed or demoted inviter, a suspended workspace and an invitation
with no recorded inviter admit nobody new.

115 lets someone who signed in without the link see the invitations
the accept door would honour for their address, and have the link sent again:
a fresh token for the same invitation, emailed to its address alone.

129 (#1723) gives an admission a claim link: through the claim
page's form post and the callback, a new Google account with any address signs
up once, and the spot is used up for the admitted address too.

133 (#1740) makes an account created at an admitted address use the
admission up as it is created, so the spot stays used when the person's
verified email later moves: neither the old link nor the freed address opens a
second account. Two sign-ins of one admission at once are characterized, the
overlap the trigger leaves included.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import psycopg2
import psycopg2.errors
import pytest

from src.api.principal import COOKIE
from src.api.routes import auth
from src.services.target import (
    email_sender,
    google_oidc,
    identity,
    invitations,
    sessions,
    signup_admissions,
    workspaces,
)
from tests.scripts.conftest import (
    in_tenant,
    _scratch,
    as_user,
    execute,
    fetch_all,
    in_user_plane,
    ingress_engine,
    replay_advertised_stream,
    rows_holding,
    seed_workspace_chain,
)
from tests.scripts.test_waitlist_gate import _admission, _linked
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import (
    FRONT,
    api_client,
    cookie_value,
    unsigned_id_token,
)
from tests.src.services.target.conftest import RecordingSender

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
    "from-disabled@example.com": "disabled",
    "from-nobody@example.com": None,  # a service identity's: no inviter recorded
}
STANDING = {"from-admin@example.com"}


def _invite(
    cur,
    ws,
    email,
    *,
    by,
    state="pending",
    expires="7 days",
    sent="0",
    token=None,
    channel="email",
    tg=None,
):
    """An invitation to *email*, created *sent* from now, whose link's token is
    *token* (the address itself unless given); returns its id."""
    cur.execute(
        "INSERT INTO workspace_invitations (workspace_id, token_hash,"
        " delivery_channel, email, invited_tg_user_id, state, expires_at,"
        " invited_by_user_id, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, now() + %s::interval, %s,"
        " now() + %s::interval) RETURNING id",
        (
            ws,
            sessions.token_hash(token or email),
            channel,
            email,
            tg,
            state,
            expires,
            by,
            sent,
        ),
    )
    return str(cur.fetchone()[0])


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
                    "disabled": _user(cur, ws, "admin"),
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
                cur.execute(
                    "UPDATE users SET state = 'disabled' WHERE id = %s",
                    (inviters["disabled"],),
                )
            conn.commit()
        finally:
            conn.close()
        yield {
            "ws": ws,
            "suspended_ws": suspended["ws"],
            "ws_owner": chain["user"],
            "inviters": inviters,
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
        """098: the inviter was removed, demoted or disabled, the workspace is
        suspended, or nobody is recorded as the inviter — the invitation is
        pending and unexpired, and still admits no new account."""
        assert _admitted(world, email) is False

    def test_the_door_is_svc_ingress_alone(self, world):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _one(world["worker"], "SELECT fn_signup_admitted('friend@example.com')")


def _as_owner(world, *statements):
    """Run *statements* as the schema owner with an actor, committed, and
    return the last one's first column."""
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'operator'")
            last = None
            for sql, params in statements:
                cur.execute(sql, params)
                last = cur.fetchone()[0] if cur.description else None
        conn.commit()
        return last
    finally:
        conn.close()


def _existing_user(world, email):
    return _as_owner(
        world,
        (
            "INSERT INTO users (primary_email) VALUES (%s) RETURNING id",
            (email,),
        ),
    )


#: The accept door, asked as `invitations.accept` asks it.
_ACCEPT = (
    "SELECT o_granted_role FROM fn_invitation_accept(%s, %s, 'google', %s, NULL, 'web')"
)


def _accept(world, email, user, *, token=None):
    """Accept the invitation addressed to *email* as *user* by *token* (the
    address itself, as `_invite` seeds it, unless given); returns the granted
    role, rolled back."""
    return _one(
        world["ingress"], _ACCEPT, (sessions.token_hash(token or email), user, email)
    )


class TestTheAcceptDoor:
    """098 holds `fn_invitation_accept` to the same standing as the sign-up
    door, so an EXISTING account cannot come in through an invitation the
    sign-up door refuses, and a removal outranks an invitation sent before it.
    The refusal is the door's own `no_data_found`, the answer a used or
    revoked invitation gets."""

    @pytest.mark.parametrize(
        "by", ["removed", "demoted", "disabled", "suspended_owner", None]
    )
    def test_an_invitation_that_lost_its_standing_admits_no_existing_account(
        self, world, by
    ):
        email = f"accept-{by}@example.com"
        home = (
            _one(
                world["owner"],
                "SELECT workspace_id FROM workspace_members WHERE user_id = %s",
                (world["inviters"][by],),
            )
            if by == "suspended_owner"
            else world["ws"]
        )
        _as_owner(
            world,
            (
                "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                " delivery_channel, email, role, expires_at, invited_by_user_id)"
                " VALUES (%s, %s, 'email', %s, 'admin', now() + interval '7 days',"
                " %s)",
                (
                    home,
                    hashlib.sha256(email.encode()).hexdigest(),
                    email,
                    world["inviters"][by],
                ),
            ),
        )
        user = _existing_user(world, email)
        with pytest.raises(psycopg2.errors.NoDataFound):
            _accept(world, email, user)

    def test_an_invitation_from_a_current_admin_is_accepted(self, world):
        email = "accept-admin@example.com"
        _as_owner(
            world,
            (
                "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                " delivery_channel, email, role, expires_at, invited_by_user_id)"
                " VALUES (%s, %s, 'email', %s, 'admin', now() + interval '7 days',"
                " %s)",
                (
                    world["ws"],
                    hashlib.sha256(email.encode()).hexdigest(),
                    email,
                    world["inviters"]["admin"],
                ),
            ),
        )
        assert _accept(world, email, _existing_user(world, email)) == "admin"

    def test_a_removal_outranks_an_invitation_sent_before_it(self, world):
        """An invitation addressed to a member who is then removed does not
        bring them back; a fresh invitation to the same address after the
        removal does."""
        old = "removed-old@example.com"
        user = _existing_user(world, old)

        def invite(email, token):
            return (
                "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                " delivery_channel, email, role, expires_at, invited_by_user_id)"
                " VALUES (%s, %s, 'email', %s, 'admin', now() + interval '7 days',"
                " %s)",
                (
                    world["ws"],
                    hashlib.sha256(token.encode()).hexdigest(),
                    email,
                    world["ws_owner"],
                ),
            )

        _as_owner(
            world,
            invite(old, old),
            (
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'member')",
                (world["ws"], user),
            ),
        )
        _as_owner(
            world,
            (
                "DELETE FROM workspace_members WHERE workspace_id = %s"
                " AND user_id = %s",
                (world["ws"], user),
            ),
            (
                "INSERT INTO workspace_member_removals"
                " (workspace_id, user_id, removed_by_user_id)"
                " VALUES (%s, %s, %s)",
                (world["ws"], user, world["ws_owner"]),
            ),
        )
        with pytest.raises(psycopg2.errors.NoDataFound):
            _accept(world, old, user)

        # Positive control: an invitation to the same address sent after the
        # removal is the way back in. The removal's service revokes the old
        # one (`invitations.revoke_on_removal`, proven in
        # test_member_removal_gate), which frees `uq_invite_live` for it.
        _as_owner(
            world,
            (
                "UPDATE workspace_invitations SET state = 'revoked'"
                " WHERE workspace_id = %s AND email = %s AND state = 'pending'",
                (world["ws"], old),
            ),
            invite(old, "re-invite"),
        )
        assert _accept(world, old, user, token="re-invite") == "admin"

    def test_an_acceptance_in_flight_cannot_undo_a_removal(self, world):
        """The door's first removal read takes no lock. An acceptance that
        starts while the removal is uncommitted passes it, then waits on the
        member row the removal deleted; when the removal commits, the door
        reads the removal again and refuses instead of re-adding them."""
        email = "in-flight@example.com"
        user = _existing_user(world, email)
        _as_owner(
            world,
            (
                "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                " delivery_channel, email, role, expires_at, invited_by_user_id)"
                " VALUES (%s, %s, 'email', %s, 'member', now() + interval '7 days',"
                " %s)",
                (
                    world["ws"],
                    hashlib.sha256(email.encode()).hexdigest(),
                    email,
                    world["ws_owner"],
                ),
            ),
            (
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'member')",
                (world["ws"], user),
            ),
        )
        removal = psycopg2.connect(world["owner"])
        try:
            with removal.cursor() as cur:
                cur.execute("SET app.actor_kind = 'operator'")
                cur.execute(
                    "DELETE FROM workspace_members WHERE workspace_id = %s"
                    " AND user_id = %s",
                    (world["ws"], user),
                )
                cur.execute(
                    "INSERT INTO workspace_member_removals"
                    " (workspace_id, user_id, removed_by_user_id)"
                    " VALUES (%s, %s, %s)",
                    (world["ws"], user, world["ws_owner"]),
                )
            outcome = {}

            def accept():
                try:
                    outcome["role"] = _accept(world, email, user)
                except psycopg2.Error as exc:
                    outcome["error"] = exc

            worker = threading.Thread(target=accept)
            worker.start()
            assert _blocks_another(removal), (
                "the acceptance waits on the removal's member row"
            )
            removal.commit()
            worker.join(timeout=30)
        finally:
            removal.close()

        # The refusal is the proof: `_accept` never commits, so a member count
        # afterwards would read 0 either way.
        assert isinstance(outcome.get("error"), psycopg2.errors.NoDataFound), outcome


def _blocks_another(conn, timeout: float = 10.0) -> bool:
    """True once another backend waits on a lock *conn* holds
    (`pg_blocking_pids`, which, unlike `pg_stat_activity`'s query column, a
    non-superuser can read for any role's session); False after *timeout*."""
    deadline = time.monotonic() + timeout
    with conn.cursor() as cur:
        while time.monotonic() < deadline:
            # The activity view is snapshotted once per transaction: refresh it.
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity a"
                " WHERE pg_backend_pid() = ANY(pg_blocking_pids(a.pid)))"
            )
            if cur.fetchone()[0]:
                return True
            time.sleep(0.05)
    return False


def test_the_listing_shows_only_invitations_the_doors_would_honour(world):
    """`GET …/invitations` lists what the doors would still accept: not an
    invitation from a removed, demoted or disabled sender or with none. An
    invitation to someone removed after it was sent is revoked by the removal,
    so the state filter leaves it out."""
    listed = {
        row["email"]
        for row in asyncio.run(
            in_tenant(
                world["ingress"],
                world["ws"],
                world["ws_owner"],
                lambda s: workspaces.list_invitations(s, workspace_id=world["ws"]),
            )
        )
    }

    assert LIVE | STANDING <= listed
    assert not listed & (set(SENT_BY) - STANDING)


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


async def _google_sign_in(client, monkeypatch, *, sub, email, claim=None):
    """The real sign-in route pair on *client*, Google stubbed; the callback's
    response, which carries the session cookie when sign-in admitted them.
    With *claim*, the start is the claim page's form post of that token, and
    the callback carries both cookies that post set."""
    if claim is None:
        start = await client.get("/auth/google", follow_redirects=False)
        sent = (auth.NONCE_COOKIE,)
    else:
        start = await client.post(
            "/auth/google/claim", data={"claim": claim}, follow_redirects=False
        )
        assert start.status_code == 303, start.text
        sent = (auth.NONCE_COOKIE, auth.CLAIM_COOKIE)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    cookies = "; ".join(f"{name}={cookie_value(start, name)}" for name in sent)

    async def exchange_code(client_, **kw):
        return unsigned_id_token(state, sub=sub, email=email, name=sub)

    monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
    return await client.get(
        f"/auth/google/callback?state={state}&code=c0de",
        headers={"Cookie": cookies},
        follow_redirects=False,
    )


def _callback(world, monkeypatch, *, sub, email, claim=None):
    """The real sign-in route pair as `svc_ingress`, Google stubbed, with the
    deployment's default switch (gated); with *claim*, started by the claim
    page's form post of that token."""

    async def main():
        async with api_client(world["ingress"]) as (client, _):
            return await _google_sign_in(
                client, monkeypatch, sub=sub, email=email, claim=claim
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


# --- 129 (#1723): the claim link at sign-in ---------------------------------


def _unspent(world, email, token):
    """Whether *email*'s admission is unclaimed and still holds *token*'s link."""
    row = _admission(world, email)
    return row["claimed_at"] is None and row["claim_token_hash"] == sessions.token_hash(
        token
    )


class TestTheClaimLink:
    """An address admitted through the core (`signup_admissions.admit`) with
    its link stored as the worker stores it after the send (`fn_signup_link`,
    as `svc_worker`), then the claim page's form post and the real callback as
    `svc_ingress`. Only a new account the gate refuses spends the link, and it
    opens one account."""

    def test_a_different_google_address_signs_up_once_through_the_link(
        self, world, google_configured, monkeypatch
    ):
        token = _linked(world, "claim-once@example.com")
        done = _callback(
            world, monkeypatch, sub="sub-c1", email="c1-own@example.com", claim=token
        )
        assert done.headers["location"] == f"{FRONT}/welcome", done.headers
        assert cookie_value(done, COOKIE)
        assert _users_with(world, "c1-own@example.com") == 1
        assert _admission(world, "claim-once@example.com")["claimed_at"] is not None
        assert rows_holding(world["owner"], token) == {}

    def test_a_second_new_account_with_the_same_link_is_refused(
        self, world, google_configured, monkeypatch
    ):
        token = _linked(world, "claim-twice@example.com")
        first = _callback(
            world, monkeypatch, sub="sub-c2a", email="c2a@example.com", claim=token
        )
        assert first.headers["location"] == f"{FRONT}/welcome", first.headers
        again = _callback(
            world, monkeypatch, sub="sub-c2b", email="c2b@example.com", claim=token
        )
        assert again.headers["location"] == f"{FRONT}/login?error=claim_refused"
        assert COOKIE not in again.headers.get("set-cookie", "")
        assert _users_with(world, "c2b@example.com") == 0

    def test_the_admitted_address_signs_up_no_account_after_the_claim(
        self, world, google_configured, monkeypatch
    ):
        token = _linked(world, "claim-spot@example.com")
        done = _callback(
            world, monkeypatch, sub="sub-c3a", email="c3a@example.com", claim=token
        )
        assert done.headers["location"] == f"{FRONT}/welcome", done.headers
        as_address = _callback(
            world, monkeypatch, sub="sub-c3b", email="claim-spot@example.com"
        )
        assert as_address.headers["location"] == f"{FRONT}/login?error=not_admitted"
        assert _users_with(world, "claim-spot@example.com") == 0

    def test_an_existing_account_signing_in_leaves_the_claim_unspent(
        self, world, google_configured, monkeypatch
    ):
        token = _linked(world, "claim-kept@example.com")
        _google_account(world, "sub-c4", "c4-existing@example.com")
        done = _callback(
            world,
            monkeypatch,
            sub="sub-c4",
            email="c4-existing@example.com",
            claim=token,
        )
        assert done.headers["location"] == f"{FRONT}/welcome", done.headers
        assert _unspent(world, "claim-kept@example.com", token)
        # Its positive control: the link still opens a new account.
        fresh = _callback(
            world, monkeypatch, sub="sub-c4b", email="c4b@example.com", claim=token
        )
        assert fresh.headers["location"] == f"{FRONT}/welcome", fresh.headers

    def test_a_collision_after_the_claim_leaves_the_link_unspent(
        self, world, google_configured, monkeypatch
    ):
        """The new account's address is held by another account: the claim was
        spent, then the user's creation collides, and the savepoint takes the
        claim back with it. Nothing is committed without its user."""
        token = _linked(world, "claim-collide@example.com")
        _existing_user(world, "c5-held@example.com")
        done = _callback(
            world, monkeypatch, sub="sub-c5", email="c5-held@example.com", claim=token
        )
        assert done.headers["location"] == (
            f"{FRONT}/auth/error?reason=identity_collision"
        )
        assert _unspent(world, "claim-collide@example.com", token)
        assert _users_with(world, "c5-held@example.com") == 1


# --- 133 (#1740): an account's creation uses its admission up ---------------


def _welcomed(response):
    assert response.headers["location"] == f"{FRONT}/welcome", response.headers


def _refused(response, landing):
    """*response* landed on *landing* and set no session."""
    assert response.headers["location"] == f"{FRONT}{landing}", response.headers
    assert COOKIE not in response.headers.get("set-cookie", "")


def _session_user(world, response):
    """The user the session that *response* set belongs to."""
    return _one(
        world["owner"],
        "SELECT user_id::text FROM session_tokens WHERE token_hash = %s",
        (sessions.token_hash(cookie_value(response, COOKIE)),),
    )


def _google_user(world, sub):
    """The user Google subject *sub* signs in as, or None."""
    rows = fetch_all(
        world["owner"],
        "SELECT user_id::text AS user_id FROM user_identities"
        " WHERE provider = 'google' AND external_id = %s",
        (sub,),
    )
    return rows[0]["user_id"] if rows else None


def _address_of(world, user):
    return _one(
        world["owner"], "SELECT primary_email FROM users WHERE id = %s", (user,)
    )


def _google_account(world, sub, email):
    """An account created at *email* that Google subject *sub* signs in as."""
    user = _existing_user(world, email)
    _as_owner(
        world,
        (
            "INSERT INTO user_identities (user_id, provider, external_id)"
            " VALUES (%s, 'google', %s)",
            (user, sub),
        ),
    )
    return str(user)


def _used(world, email):
    """Whether *email*'s admission is used up: stamped, its link cleared."""
    row = _admission(world, email)
    return row["claimed_at"] is not None and row["claim_token_hash"] is None


@pytest.fixture
def signin_budget(world):
    """The sign-in budget every gate here shares, cleared before and after the
    test. A start and a callback each spend one of `principal.PREAUTH_LIMIT`
    a minute, keyed on the test client's one address, and these tests sign in
    more often than one minute allows. Clearing it is what a minute's wait
    would do, and the tests after them start with a whole minute's budget."""

    def clear():
        execute(world["owner"], "DELETE FROM rate_counters WHERE scope = 'preauth_ip'")

    clear()
    yield
    clear()


@pytest.mark.usefixtures("signin_budget")
class TestADirectSignUpUsesItsAdmission:
    """133 (#1740): an account created at an admitted address
    uses the admission up in the transaction that creates it, so the spot stays
    used when the person's verified email later moves (#1579).

    The direct sign-up writes the used spot down itself, as the claim link
    does: counting the spot used only while an account holds the address would
    let one refresh to another address reopen it, to its old link and to its
    address. Each test drives the real callback as `svc_ingress`, Google
    stubbed: the admission's link is stored through the worker's door
    (`fn_signup_link`, as `svc_worker`), the address signs up without a claim
    cookie, and the same subject signs in again with another verified
    address."""

    def _signed_up_then_moved(self, world, monkeypatch, tag):
        """`<tag>@example.com` admitted with its link stored, signed up at
        directly by `sub-<tag>`, which then signs in as
        `<tag>-moved@example.com`. Returns the link's token and the two
        callbacks' responses."""
        address = f"{tag}@example.com"
        token = _linked(world, address)
        first = _callback(world, monkeypatch, sub=f"sub-{tag}", email=address)
        _welcomed(first)
        again = _callback(
            world, monkeypatch, sub=f"sub-{tag}", email=f"{tag}-moved@example.com"
        )
        _welcomed(again)
        return token, first, again

    def test_its_old_link_opens_no_second_account_after_the_move(
        self, world, google_configured, monkeypatch
    ):
        token, _, _ = self._signed_up_then_moved(world, monkeypatch, "a1-link")
        other = _callback(
            world,
            monkeypatch,
            sub="sub-a1-link-other",
            email="a1-link-free@example.com",
            claim=token,
        )
        _refused(other, "/login?error=claim_refused")
        assert _users_with(world, "a1-link-free@example.com") == 0
        assert _google_user(world, "sub-a1-link-other") is None

    def test_its_freed_address_admits_no_new_account_after_the_move(
        self, world, google_configured, monkeypatch
    ):
        self._signed_up_then_moved(world, monkeypatch, "a1-address")
        other = _callback(
            world,
            monkeypatch,
            sub="sub-a1-address-other",
            email="a1-address@example.com",
        )
        _refused(other, "/login?error=not_admitted")
        assert _users_with(world, "a1-address@example.com") == 0
        assert _google_user(world, "sub-a1-address-other") is None

    def test_the_sign_up_marks_the_spot_used_and_the_move_keeps_it_so(
        self, world, google_configured, monkeypatch
    ):
        """The record behind both refusals. The sign-up stamped the admission
        and cleared its link's hash in the user's own transaction. The move is
        the same account, and both sessions are its, at the new address, with
        the stamp as it was."""
        token, first, again = self._signed_up_then_moved(world, monkeypatch, "a1-mark")
        user = _google_user(world, "sub-a1-mark")
        assert _session_user(world, first) == _session_user(world, again) == user
        assert _address_of(world, user) == "a1-mark-moved@example.com"
        assert _users_with(world, "a1-mark@example.com") == 0
        used = _admission(world, "a1-mark@example.com")
        assert used["claimed_at"] is not None, used
        assert used["claim_token_hash"] is None, used
        assert rows_holding(world["owner"], sessions.token_hash(token)) == {}

    def test_the_stamp_is_made_at_the_sign_up_not_at_the_move(
        self, world, google_configured, monkeypatch
    ):
        """Read between the two sign-ins: the spot is used as soon as the
        account exists, and the move changes nothing about it."""
        address = "a1-when@example.com"
        _linked(world, address)
        _welcomed(_callback(world, monkeypatch, sub="sub-a1-when", email=address))
        used = _admission(world, address)
        assert _used(world, address), used
        _welcomed(
            _callback(
                world, monkeypatch, sub="sub-a1-when", email="a1-when-moved@example.com"
            )
        )
        assert _admission(world, address) == used

    def test_carrying_its_own_link_the_address_is_used_up_the_same_way(
        self, world, google_configured, monkeypatch
    ):
        """The address signs up carrying its own link. The gate admits it, so
        the callback never asks the claim (the spy records no call): the
        account's creation used the spot up, not the fallback, and after the
        move the link opens no account, exactly as without the cookie."""
        address = "a1-own-link@example.com"
        token = _linked(world, address)
        asked = []
        real_claim = signup_admissions.claim

        async def spy(conn, *, token):
            asked.append(token)
            return await real_claim(conn, token=token)

        monkeypatch.setattr(signup_admissions, "claim", spy)
        _welcomed(
            _callback(world, monkeypatch, sub="sub-a1-own", email=address, claim=token)
        )
        assert asked == [], "an admitted address never reaches the claim fallback"
        _welcomed(
            _callback(
                world,
                monkeypatch,
                sub="sub-a1-own",
                email="a1-own-link-moved@example.com",
            )
        )
        other = _callback(
            world,
            monkeypatch,
            sub="sub-a1-own-other",
            email="a1-own-free@example.com",
            claim=token,
        )
        _refused(other, "/login?error=claim_refused")
        assert asked == [token], "only the stranger's sign-in asked the claim"
        assert _used(world, address)


@pytest.mark.usefixtures("signin_budget")
class TestWhatAnAccountsCreationLeavesAlone:
    """133 touches one row: the unconsumed admission at the address an account
    is created with. A claim the sign-up never asked, an account's later move
    to an admitted address, a refresh, a collision, invitations and open
    sign-up are left alone. The link-first sign-up, the link's reuse, the
    claimed address and an existing account's unspent claim are
    `TestTheClaimLink`'s, above."""

    def test_an_admitted_sign_up_carrying_another_admissions_link_spends_only_its_own(
        self, world, google_configured, monkeypatch
    ):
        _linked(world, "keep-own@example.com")
        token = _linked(world, "keep-other@example.com")
        _welcomed(
            _callback(
                world,
                monkeypatch,
                sub="sub-keep-own",
                email="keep-own@example.com",
                claim=token,
            )
        )
        assert _used(world, "keep-own@example.com")
        assert _unspent(world, "keep-other@example.com", token)

    def test_an_invited_sign_up_carrying_an_unrelated_link_leaves_it_unspent(
        self, world, google_configured, monkeypatch
    ):
        _invitation(world, "keep-invited@example.com")
        token = _linked(world, "keep-unrelated@example.com")
        _welcomed(
            _callback(
                world,
                monkeypatch,
                sub="sub-keep-invited",
                email="keep-invited@example.com",
                claim=token,
            )
        )
        assert _users_with(world, "keep-invited@example.com") == 1
        assert _unspent(world, "keep-unrelated@example.com", token)

    def test_an_invitation_alone_still_signs_up_and_makes_no_admission(
        self, world, google_configured, monkeypatch
    ):
        _invitation(world, "keep-invitee@example.com")
        _welcomed(
            _callback(
                world,
                monkeypatch,
                sub="sub-keep-invitee",
                email="keep-invitee@example.com",
            )
        )
        assert _users_with(world, "keep-invitee@example.com") == 1
        assert _admission(world, "keep-invitee@example.com") is None

    def test_open_sign_up_still_lets_an_address_nobody_admitted_in(
        self, world, google_configured, monkeypatch
    ):
        monkeypatch.setattr(
            api_conftest.settings, "TARGET_SIGNUP_OPEN", True, raising=False
        )
        _welcomed(
            _callback(
                world, monkeypatch, sub="sub-keep-open", email="keep-open@example.com"
            )
        )
        assert _users_with(world, "keep-open@example.com") == 1
        assert _admission(world, "keep-open@example.com") is None

    def test_an_account_moving_to_an_admitted_address_uses_nothing(
        self, world, google_configured, monkeypatch
    ):
        """133 runs when an account is created, never when one changes. An
        existing account whose verified email moves to an admitted address
        holds it, so 129's doors refuse that admission's link and a new
        account at it, but the admission was used to create no one and is
        not stamped."""
        user = _google_account(world, "sub-keep-mover", "keep-mover@example.com")
        token = _linked(world, "keep-target@example.com")
        done = _callback(
            world, monkeypatch, sub="sub-keep-mover", email="keep-target@example.com"
        )
        _welcomed(done)
        assert _session_user(world, done) == user
        assert _address_of(world, user) == "keep-target@example.com"
        assert _unspent(world, "keep-target@example.com", token)

    def test_a_refresh_follows_the_claim_and_a_token_without_one_keeps_the_address(
        self, world, google_configured, monkeypatch
    ):
        user = _google_account(world, "sub-keep-refresh", "keep-refresh@example.com")
        moved = _callback(
            world,
            monkeypatch,
            sub="sub-keep-refresh",
            email="keep-refreshed@example.com",
        )
        _welcomed(moved)
        silent = _callback(world, monkeypatch, sub="sub-keep-refresh", email=None)
        _welcomed(silent)
        assert _session_user(world, moved) == _session_user(world, silent) == user
        assert _address_of(world, user) == "keep-refreshed@example.com"

    def test_a_refresh_that_collides_keeps_the_old_address(
        self, world, google_configured, monkeypatch
    ):
        user = _google_account(world, "sub-keep-collide", "keep-collide@example.com")
        _existing_user(world, "keep-taken@example.com")
        done = _callback(
            world, monkeypatch, sub="sub-keep-collide", email="keep-taken@example.com"
        )
        _welcomed(done)
        assert _session_user(world, done) == user
        assert _address_of(world, user) == "keep-collide@example.com"
        assert _users_with(world, "keep-taken@example.com") == 1


@pytest.mark.usefixtures("signin_budget")
class TestTheStampCommitsWithItsAccount:
    """The stamp is a write in the user's own transaction: it commits with the
    account or not at all, and an account that is never created uses
    nothing."""

    def test_a_fault_before_the_sign_in_commits_takes_back_the_account_and_stamp(
        self, world, google_configured, monkeypatch
    ):
        """The fault is the session's mint, the callback's last write before
        its commit: the user, its identity and the trigger's stamp are all in
        the transaction it aborts. Signing in again then uses the spot up."""
        address = "undo-fault@example.com"
        token = _linked(world, address)

        class _Fault(Exception):
            pass

        real_issue = sessions.issue

        async def mint_fails(conn, *, user_id):
            raise _Fault

        monkeypatch.setattr(sessions, "issue", mint_fails)
        with pytest.raises(_Fault):
            _callback(world, monkeypatch, sub="sub-undo-fault", email=address)
        assert _users_with(world, address) == 0
        assert _google_user(world, "sub-undo-fault") is None
        assert _unspent(world, address, token)

        monkeypatch.setattr(sessions, "issue", real_issue)
        _welcomed(_callback(world, monkeypatch, sub="sub-undo-fault", email=address))
        assert _used(world, address)

    def test_an_account_created_without_an_address_uses_nothing(self, world):
        """The WHEN skips a user created with no address, as a Telegram-only
        account is: no admission changes, whichever there are."""
        _linked(world, "undo-sentinel@example.com")
        every = "SELECT * FROM signup_admissions ORDER BY email"
        before = fetch_all(world["owner"], every)
        user = asyncio.run(
            in_user_plane(
                world["ingress"],
                lambda c: identity.upsert_google_identity(
                    c,
                    sub="sub-undo-no-email",
                    email=None,
                    display_name=None,
                    signup_open=True,
                ),
            )
        )
        assert _address_of(world, user) is None
        assert fetch_all(world["owner"], every) == before

    def test_a_new_account_refused_as_a_collision_uses_nothing(
        self, world, google_configured, monkeypatch
    ):
        """An account moved to an admitted address (which uses nothing up, as
        above), then a new subject arrives with that address: the door admits
        it, the collision refuses it before any user is created, and the
        admission is still unused."""
        address = "undo-held@example.com"
        token = _linked(world, address)
        _google_account(world, "sub-undo-mover", "undo-mover@example.com")
        _welcomed(_callback(world, monkeypatch, sub="sub-undo-mover", email=address))
        clash = _callback(world, monkeypatch, sub="sub-undo-new", email=address)
        assert clash.headers["location"] == (
            f"{FRONT}/auth/error?reason=identity_collision"
        )
        assert _google_user(world, "sub-undo-new") is None
        assert _unspent(world, address, token)


def _now(conn):
    """*conn*'s transaction's `now()`, which is what any stamp it makes reads."""
    with conn.cursor() as cur:
        cur.execute("SELECT now()")
        return cur.fetchone()[0]


class TestTheOverlapLeft:
    """Two sign-ins of one admission at once, as `svc_ingress`, each statement
    the one its service issues: the direct sign-up's gate read
    (`fn_signup_admitted`) then its user INSERT, and the claim
    (`fn_signup_claim`) then the claimant's user INSERT. Ordered by barriers,
    never by sleeps: `_blocks_another` watches for the wait. Its answer is
    asserted only once the holder has committed and the waiter is joined, so
    a barrier that fails closes no connection a waiter is still using.

    The trigger narrows the overlap and does not close it, and these pin both
    halves. When the claim commits first, a direct sign-up whose gate read
    passed before that commit still creates its account: its stamp waits on
    the claimed row, then finds it used and changes nothing, so one admission
    has opened two accounts. That is the accepted limit: closing it
    needs the two sign-ins serialised. When the direct sign-up inserts first,
    the claim waits on the stamped row and is refused. Either way, no link is
    left open."""

    def test_a_claim_committing_first_and_a_direct_sign_up_both_open_an_account(
        self, world
    ):
        address = "overlap-claim-first@example.com"
        claimant = "overlap-claimant@example.com"
        token = _linked(world, address)
        claim = psycopg2.connect(world["ingress"])
        direct = psycopg2.connect(world["ingress"])
        outcome = {}
        worker = None
        try:
            claim_now = _now(claim)
            with claim.cursor() as cur:
                cur.execute("SELECT fn_signup_claim(%s)", (sessions.token_hash(token),))
                assert cur.fetchone()[0] is True
                cur.execute(
                    "INSERT INTO users (primary_email) VALUES (%s)", (claimant,)
                )
            direct_now = _now(direct)
            with direct.cursor() as cur:
                cur.execute("SELECT fn_signup_admitted(%s)", (address,))
                assert cur.fetchone()[0] is True, "the claim has not committed yet"

            def create():
                try:
                    with direct.cursor() as cur:
                        cur.execute(
                            "INSERT INTO users (primary_email) VALUES (%s)", (address,)
                        )
                    outcome["created"] = True
                except psycopg2.Error as exc:
                    outcome["error"] = exc

            worker = threading.Thread(target=create)
            worker.start()
            waited = _blocks_another(claim)
            claim.commit()
            worker.join(timeout=30)
            assert waited, "the direct sign-up's stamp waited on the row the claim held"
            assert not worker.is_alive()
            assert outcome == {"created": True}, outcome
            direct.commit()
        finally:
            # The holder first: a waiter is released, then joined, before the
            # connection it is using closes.
            claim.close()
            if worker is not None:
                worker.join(timeout=30)
            direct.close()
        assert _users_with(world, claimant) == 1
        assert _users_with(world, address) == 1, "the accepted limit: two accounts"
        row = _admission(world, address)
        assert claim_now < direct_now
        assert row["claimed_at"] == claim_now, "the claim's stamp; the trigger's no-op"
        assert row["claim_token_hash"] is None

    def test_a_direct_sign_up_inserting_first_makes_the_waiting_claim_refuse(
        self, world
    ):
        address = "overlap-direct-first@example.com"
        token = _linked(world, address)
        direct = psycopg2.connect(world["ingress"])
        claim = psycopg2.connect(world["ingress"])
        outcome = {}
        worker = None
        try:
            direct_now = _now(direct)
            with direct.cursor() as cur:
                cur.execute("SELECT fn_signup_admitted(%s)", (address,))
                assert cur.fetchone()[0] is True
                cur.execute("INSERT INTO users (primary_email) VALUES (%s)", (address,))

            def spend():
                try:
                    with claim.cursor() as cur:
                        cur.execute(
                            "SELECT fn_signup_claim(%s)", (sessions.token_hash(token),)
                        )
                        outcome["claimed"] = cur.fetchone()[0]
                except psycopg2.Error as exc:
                    outcome["error"] = exc

            worker = threading.Thread(target=spend)
            worker.start()
            waited = _blocks_another(direct)
            direct.commit()
            worker.join(timeout=30)
            assert waited, "the claim waited on the row the direct sign-up's stamp held"
            assert not worker.is_alive()
            assert outcome == {"claimed": False}, outcome
            # The callback answers claim_refused and creates no one.
            claim.rollback()
        finally:
            # The holder first: a waiter is released, then joined, before the
            # connection it is using closes.
            direct.close()
            if worker is not None:
                worker.join(timeout=30)
            claim.close()
        assert _users_with(world, address) == 1
        row = _admission(world, address)
        assert row["claimed_at"] == direct_now, "the direct sign-up's stamp"
        assert row["claim_token_hash"] is None


# --- 115: an invited person's own invitations -------------------------------

#: The web origin a link sent again is built on.
ORIGIN = "https://app.example.test"

_OWNER = object()


def _invitation(world, email, *, ws=None, by=_OWNER, **seeded):
    """An invitation to *email* from *ws* (the world's), sent by *by* (its
    owner; None for no inviter), committed, with `_invite`'s other settings.
    Returns ``(id, token)``: the token of its link, of which only the hash is
    stored."""
    token = f"old-{uuid.uuid4().hex}"
    conn = psycopg2.connect(world["owner"])
    try:
        with conn, conn.cursor() as cur:
            invitation_id = _invite(
                cur,
                ws or world["ws"],
                email,
                by=world["ws_owner"] if by is _OWNER else by,
                token=token,
                **seeded,
            )
    finally:
        conn.close()
    return invitation_id, token


def _listed(world, user):
    return asyncio.run(
        in_user_plane(
            world["ingress"],
            lambda conn: invitations.list_for_invitee(conn, user_id=user),
        )
    )


def _ids(rows):
    return [str(row["invitation_id"]) for row in rows]


class TestTheInviteeSeesTheirInvitations:
    """115: someone who signed in without the link is shown the invitations the
    accept door would still honour for their verified address, through
    `fn_invitations_for` as `svc_ingress`, and nothing else."""

    @pytest.mark.parametrize(
        "inviter", ["admin", "removed", "demoted", "disabled", "suspended_owner", None]
    )
    def test_an_invitation_is_listed_only_while_its_inviter_has_standing(
        self, world, inviter
    ):
        email = f"list-{inviter}@example.com"
        ws = world["suspended_ws"] if inviter == "suspended_owner" else world["ws"]
        invitation_id, _ = _invitation(
            world, email, ws=ws, by=world["inviters"][inviter]
        )
        listed = _ids(_listed(world, _existing_user(world, email)))
        assert listed == ([invitation_id] if inviter == "admin" else [])

    @pytest.mark.parametrize(
        "state, expires",
        [
            ("revoked", "7 days"),
            ("accepted", "7 days"),
            ("expired", "-1 day"),
            ("pending", "-1 minute"),  # past expiry, not yet reaped
        ],
    )
    def test_a_spent_or_lapsed_invitation_is_not_listed(self, world, state, expires):
        email = f"list-{state}-{expires.replace(' ', '')}@example.com"
        _invitation(world, email, state=state, expires=expires)
        assert _listed(world, _existing_user(world, email)) == []

    def test_the_address_matches_whatever_its_case(self, world):
        invitation_id, _ = _invitation(world, "List-Mixed@Example.com")
        user = _existing_user(world, "list-mixed@example.com")
        assert _ids(_listed(world, user)) == [invitation_id]

    def test_a_workspace_they_belong_to_and_a_telegram_invitation_are_not_listed(
        self, world
    ):
        email = "list-member@example.com"
        _invitation(world, email)
        member = _existing_user(world, email)
        _as_owner(
            world,
            (
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'member')",
                (world["ws"], member),
            ),
        )
        assert _listed(world, member) == []
        email = "list-telegram@example.com"
        _invitation(world, email, channel="telegram", tg=4242)
        assert _listed(world, _existing_user(world, email)) == []

    def test_newest_first_and_never_more_than_the_limit(self, world, monkeypatch):
        email = "list-two@example.com"
        older, _ = _invitation(world, email, sent="-1 hour")
        conn = psycopg2.connect(world["owner"])
        try:
            other = seed_workspace_chain(conn, "signup-second")
            conn.commit()
        finally:
            conn.close()
        newer, _ = _invitation(world, email, ws=other["ws"], by=other["user"])
        user = _existing_user(world, email)
        assert _ids(_listed(world, user)) == [newer, older]
        monkeypatch.setattr(invitations, "INVITEE_LIST_LIMIT", 1)
        assert _ids(_listed(world, user)) == [newer]

    def test_a_row_names_the_workspace_role_expiry_and_inviter_and_no_link(self, world):
        inviter = _as_owner(
            world, ("INSERT INTO users DEFAULT VALUES RETURNING id", ())
        )
        _as_owner(
            world,
            (
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'admin')",
                (world["ws"], inviter),
            ),
            (
                "INSERT INTO user_identities (user_id, provider, external_id,"
                " display_name) VALUES (%s, 'google', 'sub-list-dana', ' Dana ')",
                (inviter,),
            ),
        )
        email = "list-shape@example.com"
        invitation_id, token = _invitation(world, email, by=inviter)
        [row] = _listed(world, _existing_user(world, email))
        assert set(row) == {
            "invitation_id",
            "workspace_name",
            "role",
            "expires_at",
            "inviter_name",
        }
        assert str(row["invitation_id"]) == invitation_id
        assert (row["workspace_name"], row["role"], row["inviter_name"]) == (
            "signup",
            "member",
            "Dana",
        )
        assert token not in str(row) and sessions.token_hash(token) not in str(row)


async def _reissue(world, user, invitation_id, sender, *, origin=ORIGIN):
    async with ingress_engine(world["ingress"]) as engine:
        return await invitations.reissue(
            engine,
            sender,
            user_id=user,
            invitation_id=invitation_id,
            web_app_origin=origin,
        )


def _stored(world, invitation_id):
    """What a re-send may change (the token hash) and what it must not."""
    [row] = fetch_all(
        world["owner"],
        "SELECT token_hash, state, created_at, expires_at, invited_by_user_id"
        " FROM workspace_invitations WHERE id = %s",
        (invitation_id,),
    )
    return row


def _all_but_the_token(row):
    return {k: v for k, v in row.items() if k != "token_hash"}


class _AtSend(RecordingSender):
    """What the database holds at the moment the email goes: the invitation's
    stored token hash and the person's re-send count, each read on a connection
    of its own, which sees committed rows only."""

    def __init__(self, world, invitation_id, user, **kw):
        super().__init__(**kw)
        self.world, self.invitation_id, self.user = world, invitation_id, user
        self.seen = []

    async def send(self, *, to, subject, body):
        self.seen.append(
            {
                "token_hash": _stored(self.world, self.invitation_id)["token_hash"],
                "spent": _one(
                    self.world["owner"],
                    "SELECT count FROM rate_counters WHERE key = %s",
                    (f"resend:{self.user}",),
                ),
            }
        )
        return await super().send(to=to, subject=subject, body=body)


class TestTheLinkSentAgain:
    """115: "Email me the link again" emails a new link to the invitation's
    address, through `invitations.reissue` as `svc_ingress`, and replaces the
    token only once the email went. The old link then stops working, nothing
    else about the invitation changes, and the new link exists only in the
    email."""

    def test_only_the_new_link_accepts_and_nothing_else_changes(self, world):
        email = "resend-rotates@example.com"
        invitation_id, old = _invitation(world, email)
        user = _existing_user(world, email)
        before = _stored(world, invitation_id)
        sender = RecordingSender()
        answer = asyncio.run(_reissue(world, user, invitation_id, sender))
        assert answer == {"delivery": {"channel": "email", "state": "sent"}}
        [mail] = sender.sent
        assert mail["to"] == email
        new = mail["token"]
        assert f"{ORIGIN}/join/{new}" in mail["body"]
        after = _stored(world, invitation_id)
        assert after["token_hash"] == sessions.token_hash(new) != before["token_hash"]
        assert _all_but_the_token(after) == _all_but_the_token(before), (
            "a re-send changes the token and nothing else"
        )
        assert rows_holding(world["owner"], new) == {}
        with pytest.raises(psycopg2.errors.NoDataFound):
            _accept(world, email, user, token=old)
        assert _accept(world, email, user, token=new) == "member"

    def test_a_spent_day_sends_nothing_and_the_last_link_still_works(
        self, world, monkeypatch
    ):
        monkeypatch.setattr(invitations, "RESENDS_PER_PERSON_PER_DAY", 1)
        email = "resend-cap@example.com"
        invitation_id, _ = _invitation(world, email)
        user = _existing_user(world, email)
        sender = RecordingSender()
        first = asyncio.run(_reissue(world, user, invitation_id, sender))
        second = asyncio.run(_reissue(world, user, invitation_id, sender))
        assert first["delivery"]["state"] == "sent"
        assert second == {
            "delivery": {
                "channel": "email",
                "state": "over_budget",
                "reason": "person_limit",
            }
        }
        [mail] = sender.sent
        assert _stored(world, invitation_id)["token_hash"] == sessions.token_hash(
            mail["token"]
        )
        assert _accept(world, email, user, token=mail["token"]) == "member"

    def test_an_accept_that_commits_first_wins_and_keeps_its_token(self, world):
        """The rotation's guards are in its own WHERE, so when it waits on an
        accept's row lock, Postgres re-reads the row once the accept commits,
        finds it no longer pending, and rotates nothing."""
        email = "resend-race@example.com"
        invitation_id, old = _invitation(world, email)
        user = _existing_user(world, email)
        accepting = psycopg2.connect(world["ingress"])
        try:
            with accepting.cursor() as cur:
                cur.execute(_ACCEPT, (sessions.token_hash(old), user, email))
            outcome = {}

            def resend():
                try:
                    outcome["answer"] = asyncio.run(
                        _reissue(world, user, invitation_id, RecordingSender())
                    )
                except Exception as exc:  # noqa: BLE001 - the thread reports it
                    outcome["error"] = exc

            worker = threading.Thread(target=resend)
            worker.start()
            assert _blocks_another(accepting), "the rotation waits on the accept"
            accepting.commit()
            worker.join(timeout=30)
        finally:
            accepting.close()
        assert isinstance(outcome.get("error"), invitations.InvitationRefused), outcome
        assert outcome["error"].reason == "not_acceptable"
        stored = _stored(world, invitation_id)
        assert (stored["token_hash"], stored["state"]) == (
            sessions.token_hash(old),
            "accepted",
        )

    def test_the_token_is_replaced_only_after_the_email_went(self, world):
        """The day is spent and committed first, the email goes while the old
        token is still the stored one, and only then does the new one replace
        it: no transaction spans the send, and a send that never happens
        replaces nothing."""
        email = "resend-order@example.com"
        invitation_id, old = _invitation(world, email)
        user = _existing_user(world, email)
        sender = _AtSend(world, invitation_id, user)
        answer = asyncio.run(_reissue(world, user, invitation_id, sender))
        assert answer == {"delivery": {"channel": "email", "state": "sent"}}
        assert sender.seen == [{"token_hash": sessions.token_hash(old), "spent": 1}]
        [mail] = sender.sent
        assert _stored(world, invitation_id)["token_hash"] == sessions.token_hash(
            mail["token"]
        )

    def test_a_failed_send_leaves_the_old_link_working(self, world):
        email = "resend-fails@example.com"
        invitation_id, old = _invitation(world, email)
        user = _existing_user(world, email)
        sender = RecordingSender(
            raises=email_sender.EmailRefused("provider_rejected", "http_422")
        )
        answer = asyncio.run(_reissue(world, user, invitation_id, sender))
        assert answer == {
            "delivery": {
                "channel": "email",
                "state": "failed",
                "reason": "provider_rejected",
            }
        }
        assert _stored(world, invitation_id)["token_hash"] == sessions.token_hash(old)
        assert _accept(world, email, user, token=old) == "member"

    @pytest.mark.parametrize(
        "seeded, asked_as",
        [
            ({"state": "revoked"}, None),
            ({"expires": "-1 minute"}, None),  # past expiry, not yet reaped
            ({"channel": "telegram", "tg": 4243}, None),
            ({}, "someone-else@example.com"),
        ],
        ids=["revoked", "lapsed", "telegram", "another-address"],
    )
    def test_the_door_rotates_only_a_pending_unexpired_email_invitation_to_them(
        self, world, seeded, asked_as
    ):
        """`fn_invitation_reissue`'s own WHERE, called directly: the API asks it
        only after the list's check, so these guards are its last line."""
        email = f"resend-door-{uuid.uuid4().hex[:8]}@example.com"
        invitation_id, _ = _invitation(world, email, **seeded)
        assert (
            _one(
                world["ingress"],
                "SELECT fn_invitation_reissue(%s, %s, %s)",
                (invitation_id, asked_as or email, sessions.token_hash("new")),
            )
            is False
        )
        # Its positive control: a pending one addressed to that address rotates.
        live, _ = _invitation(world, f"live-{email}")
        assert (
            _one(
                world["ingress"],
                "SELECT fn_invitation_reissue(%s, %s, %s)",
                (live, f"live-{email}", sessions.token_hash("new-live")),
            )
            is True
        )


class TestTheInviteeJourney:
    """Through the real routes as the API's login: someone invited signs in from
    the front door, is shown the invitation, asks for the link again, and joins
    with the link the email carries."""

    def test_signed_in_without_the_link_they_find_it_and_join(
        self, world, google_configured, monkeypatch
    ):
        email = "journey@example.com"
        invitation_id, old = _invitation(world, email)
        sender = RecordingSender()

        async def main():
            async with api_client(world["ingress"], email=sender) as (client, _):
                done = await _google_sign_in(
                    client, monkeypatch, sub="sub-journey", email=email
                )
                assert done.headers["location"] == f"{FRONT}/welcome", done.headers
                me = {"Authorization": f"Bearer {cookie_value(done, COOKIE)}"}
                listed = await client.get("/api/v1/me/invitations", headers=me)
                resent = await client.post(
                    f"/api/v1/me/invitations/{invitation_id}/resend", headers=me
                )
                unknown = await client.post(
                    f"/api/v1/me/invitations/{uuid.uuid4()}/resend", headers=me
                )
                new = sender.sent[0]["token"]
                joined = await client.post(
                    f"/api/v1/invitations/{new}/accept", headers=me
                )
                after = await client.get("/api/v1/me/invitations", headers=me)
            return listed, resent, unknown, new, joined, after

        listed, resent, unknown, new, joined, after = asyncio.run(main())
        assert listed.status_code == 200, listed.text
        assert _ids(listed.json()["invitations"]) == [invitation_id]
        assert listed.json()["resend"] is True, "a sender and an origin are set"
        assert resent.status_code == 200, resent.text
        assert resent.json() == {"delivery": {"channel": "email", "state": "sent"}}
        assert old not in resent.text and new not in resent.text
        assert unknown.status_code == 404, unknown.text
        assert joined.status_code == 200, joined.text
        assert joined.json()["workspace_id"] == str(world["ws"])
        assert after.json()["invitations"] == []
