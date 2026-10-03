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
import threading
import time
from urllib.parse import parse_qs, urlsplit

import psycopg2
import psycopg2.errors
import pytest

from src.api.principal import COOKIE
from src.api.routes import auth
from src.services.target import google_oidc, identity, workspaces
from tests.scripts.conftest import (
    in_tenant,
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
    "from-disabled@example.com": "disabled",
    "from-nobody@example.com": None,  # a service identity's: no inviter recorded
}
STANDING = {"from-admin@example.com"}


def _invite(cur, ws, email, *, by, state="pending", expires="7 days", sent="0"):
    """An email invitation; *sent* is how far from now it was created."""
    cur.execute(
        "INSERT INTO workspace_invitations (workspace_id, token_hash,"
        " delivery_channel, email, state, expires_at, invited_by_user_id,"
        " created_at)"
        " VALUES (%s, %s, 'email', %s, %s, now() + %s::interval, %s,"
        " now() + %s::interval)",
        (
            ws,
            hashlib.sha256(email.encode()).hexdigest(),
            email,
            state,
            expires,
            by,
            sent,
        ),
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


def _accept(world, email, user):
    """Accept the invitation addressed to *email* as *user*, the way
    `invitations.accept` asks the door; returns the granted role."""
    return _one(
        world["ingress"],
        "SELECT o_granted_role FROM fn_invitation_accept(%s, %s, 'google', %s,"
        " NULL, 'web')",
        (hashlib.sha256(email.encode()).hexdigest(), user, email),
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
        assert (
            _one(
                world["ingress"],
                "SELECT o_granted_role FROM fn_invitation_accept(%s, %s, 'google',"
                " %s, NULL, 'web')",
                (hashlib.sha256(b"re-invite").hexdigest(), user, old),
            )
            == "admin"
        )

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


def _backfill_sql() -> str:
    """098's one-time revoke, read from the file so the test runs the shipped
    statement rather than a copy."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[2]
        / "scripts/migrations/098_invitations_admit_while_legitimate.sql"
    )
    text_ = migration.read_text()
    start = text_.index("UPDATE workspace_invitations i SET state = 'revoked'")
    return text_[start : text_.index(";", start)]


def test_the_listing_shows_only_invitations_the_doors_would_honour(world):
    """`GET …/invitations` lists what the doors would still accept: not an
    invitation from a removed, demoted or disabled sender or with none. An
    invitation to someone removed after it was sent is revoked by the removal
    (or by 098's one-time revoke), so the state filter leaves it out."""
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


def test_the_one_time_revoke_takes_only_a_removed_inviters_pending_invitations(world):
    """098 revokes, once, the pending invitations of an inviter who has a
    removal record and is not a member there again, and those addressed (by
    email or Telegram id) to a person removed after they were sent. A removed-then-re-invited inviter's,
    an accepted one, a current admin's and one sent to a removed person after
    the removal are left as they were. Run inside a transaction that is rolled
    back, so the world is untouched."""
    gone = _existing_user(world, "gone-inviter@example.com")
    back = _existing_user(world, "back-inviter@example.com")
    addressee = _existing_user(world, "Removed-Addressee@example.com")
    rows = {
        "by-gone@example.com": (gone, "pending"),
        "by-gone-used@example.com": (gone, "accepted"),
        "by-back@example.com": (back, "pending"),
        "by-admin@example.com": (world["inviters"]["admin"], "pending"),
    }
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'operator'")
            cur.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'admin')",
                (world["ws"], back),
            )
            for user in (gone, back, addressee):
                cur.execute(
                    "INSERT INTO workspace_member_removals"
                    " (workspace_id, user_id, removed_by_user_id)"
                    " VALUES (%s, %s, %s)",
                    (world["ws"], user, world["ws_owner"]),
                )
            for email, (by, state) in rows.items():
                _invite(cur, world["ws"], email, by=by, state=state)
            admin = world["inviters"]["admin"]
            # Sent before the removal, and after it.
            for email, sent in (
                ("removed-addressee@example.com", "-1 hour"),
                ("REMOVED-ADDRESSEE@example.com", "1 minute"),
            ):
                _invite(cur, world["ws"], email, by=admin, sent=sent)
            # A Telegram-only person, removed, invited by Telegram id before
            # the removal and after it.
            cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
            tg_person = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_identities (user_id, provider, external_id)"
                " VALUES (%s, 'telegram', '770098')",
                (tg_person,),
            )
            cur.execute(
                "INSERT INTO workspace_member_removals"
                " (workspace_id, user_id, removed_by_user_id) VALUES (%s, %s, %s)",
                (world["ws"], tg_person, world["ws_owner"]),
            )
            for token, sent in (("tg-before", "-1 hour"), ("tg-after", "1 minute")):
                cur.execute(
                    "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                    " delivery_channel, invited_tg_user_id, expires_at,"
                    " invited_by_user_id, created_at)"
                    " VALUES (%s, %s, 'telegram', 770098, now() + interval '7 days',"
                    " %s, now() + %s::interval)",
                    (world["ws"], token, admin, sent),
                )
            cur.execute(_backfill_sql())
            cur.execute(
                "SELECT token_hash, state FROM workspace_invitations"
                " WHERE token_hash IN ('tg-before', 'tg-after')"
            )
            by_telegram = dict(cur.fetchall())
            cur.execute(
                "SELECT email, state FROM workspace_invitations WHERE email = ANY(%s)",
                (
                    [
                        *rows,
                        "removed-addressee@example.com",
                        "REMOVED-ADDRESSEE@example.com",
                    ],
                ),
            )
            after = dict(cur.fetchall())
    finally:
        conn.rollback()
        conn.close()
    assert after == {
        "by-gone@example.com": "revoked",
        "by-gone-used@example.com": "accepted",
        "by-back@example.com": "pending",
        "by-admin@example.com": "pending",
        "removed-addressee@example.com": "revoked",
        "REMOVED-ADDRESSEE@example.com": "pending",
    }
    assert by_telegram == {"tg-before": "revoked", "tg-after": "pending"}


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
