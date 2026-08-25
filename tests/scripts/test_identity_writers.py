"""X.3 — the three writers, driven against the real replayed target schema.

Sign-in's identity leg, the `create_workspace` command, and the web session
token, each executed AS `svc_ingress` under the printed RLS — the login the
production ingress runs as. A writer that has only ever been unit-tested has
not met the policy that decides whether its INSERT lands, and every one of
these three writes into a table whose policy is doing real work.

Subject discipline throughout (the F.3 convention): any connection whose
result depends on which login it is asserts `current_user` before the
assertion rides on it. Two users and two workspaces exist wherever a test
claims something resolved to a particular one, so identity is checked rather
than existence.

**Positive controls, because a passing negative proves nothing on its own.**
Two of the facts here are absences — the ownership invariant refusing an
ownerless workspace, and RLS refusing a foreign tenant. Both are paired with
the corresponding success on the same connection in the same schema, so a
failure to insert for some unrelated reason cannot read as the guard working.
"""

import hashlib
import threading
import time

import psycopg2
import pytest

from src.exceptions.identity import IdentityProvisioningError
from src.exceptions.tenancy import TenantProvisioningError, TenantResolutionError
from src.services.target.identity_provisioning import upsert_google_identity
from src.services.target.tenant_resolution import (
    Membership,
    resolve_web_session,
    workspaces_for_user,
)
from src.services.target.sync_tx import TransactionRequired
from src.services.target.web_sessions import (
    SESSION_TTL_DAYS,
    authenticate_session,
    mint_session,
    revoke_session,
    session_token_hash,
    touch_session,
)
from src.services.target.workspace_provisioning import create_workspace
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    set_test_passwords,
    txn,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """A replayed target schema with the seven roles, and nothing else.

    Deliberately EMPTY of identity rows: these are the writers, so every user,
    workspace and session a test needs is made by the code under test. A
    seeded fixture would hide the case that matters — the very first row.
    """
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress")}
    finally:
        gen.close()


@pytest.fixture()
def conn(world):
    """A subject-gated `svc_ingress` transaction — what almost every test here
    wants, since the whole point is driving the writers as the production
    ingress login. The two exceptions build their own: the race needs two
    connections and the autocommit refusal needs a connection this fixture
    would never hand out."""
    with txn(world["ingress"], "svc_ingress") as c:
        yield c


def _claim(cur, tenant, actor_kind="user", channel="web"):
    """Set the tenant/actor context with RAW SQL, for the positive controls.

    Deliberately NOT `sync_tx.apply_gucs`. The controls exist to prove the
    DATABASE refuses an ownerless workspace and a foreign tenant, and a
    control that reached the database through the helper under test would be
    measuring the helper as well — so a broken `guc_pairs` would redden the
    control and read as "the trigger fired", which is the one reading a
    positive control must never produce.
    """
    cur.execute("SET LOCAL app.tenant_id = %s", (str(tenant),))
    cur.execute("SET LOCAL app.actor_kind = %s", (actor_kind,))
    cur.execute("SET LOCAL app.channel = %s", (channel,))


@pytest.fixture()
def live_session(conn):
    """A user with one freshly minted session — the shape every session test
    starts from."""
    user = _new_user(conn)
    return conn, user, mint_session(conn, user)


def _new_user(conn):
    """A bare users row, for tests that need a subject-less user."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
        return str(cur.fetchone()[0])


class TestTheIdentityWriter:
    """`users` + `user_identities` from a verified OIDC subject."""

    def test_first_sign_in_creates_both_rows_keyed_on_the_subject(self, conn):
        got = upsert_google_identity(
            conn, subject="sub-first", email="first@example.com", display_name="A"
        )
        assert got.created is True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, external_id, display_name,"
                " verified_at IS NOT NULL FROM user_identities WHERE id = %s",
                (got.identity_id,),
            )
            provider, external_id, display, verified = cur.fetchone()
            cur.execute(
                "SELECT primary_email, state FROM users WHERE id = %s",
                (got.user_id,),
            )
            email, state = cur.fetchone()
        assert (provider, external_id) == ("google", "sub-first")
        assert display == "A" and verified is True
        # D32: the SUBJECT is the key and the email is metadata beside it.
        assert external_id != email
        assert (email, state) == ("first@example.com", "active")

    def test_returning_subject_reuses_the_user_and_refreshes(self, conn):
        first = upsert_google_identity(conn, subject="sub-return", display_name="old")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT verified_at FROM user_identities WHERE id = %s",
                (first.identity_id,),
            )
            before = cur.fetchone()[0]
        again = upsert_google_identity(conn, subject="sub-return", display_name="new")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT verified_at, display_name FROM user_identities"
                " WHERE id = %s",
                (first.identity_id,),
            )
            after, display = cur.fetchone()
            cur.execute("SELECT count(*) FROM users WHERE id = %s", (first.user_id,))
            assert cur.fetchone()[0] == 1
        assert again.user_id == first.user_id
        assert again.identity_id == first.identity_id
        assert again.created is False
        assert after >= before and display == "new"

    def test_an_absent_display_name_does_not_erase_the_stored_one(self, conn):
        first = upsert_google_identity(conn, subject="sub-keep", display_name="kept")
        upsert_google_identity(conn, subject="sub-keep", display_name=None)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT display_name FROM user_identities WHERE id = %s",
                (first.identity_id,),
            )
            assert cur.fetchone()[0] == "kept"

    def test_primary_email_fills_when_empty_and_never_overwrites(self, conn):
        first = upsert_google_identity(conn, subject="sub-mail")
        with conn.cursor() as cur:
            cur.execute("SELECT primary_email FROM users WHERE id = %s", (first.user_id,))
            assert cur.fetchone()[0] is None
        upsert_google_identity(conn, subject="sub-mail", email="filled@example.com")
        upsert_google_identity(conn, subject="sub-mail", email="changed@example.com")
        with conn.cursor() as cur:
            cur.execute("SELECT primary_email FROM users WHERE id = %s", (first.user_id,))
            # A provider changing the claim must not repoint a set account.
            assert cur.fetchone()[0] == "filled@example.com"

    def test_a_colliding_email_refuses_and_never_merges(self, conn):
        """D35, on both paths that can hit it: a brand-new subject whose email
        is taken, and a returning subject whose empty email is taken."""
        incumbent = upsert_google_identity(
            conn, subject="sub-incumbent", email="shared@example.com"
        )
        with pytest.raises(IdentityProvisioningError) as new_path:
            upsert_google_identity(
                conn, subject="sub-newcomer", email="shared@example.com"
            )
        assert new_path.value.reason == "email_belongs_to_another"

        later = upsert_google_identity(conn, subject="sub-later")
        with pytest.raises(IdentityProvisioningError) as fill_path:
            upsert_google_identity(
                conn, subject="sub-later", email="shared@example.com"
            )
        assert fill_path.value.reason == "email_belongs_to_another"

        # Neither refusal merged anything, and the transaction survived
        # both — the savepoint's whole purpose.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM user_identities WHERE external_id = %s",
                ("sub-newcomer",),
            )
            assert cur.fetchone() is None
            cur.execute(
                "SELECT primary_email FROM users WHERE id = %s", (later.user_id,)
            )
            assert cur.fetchone()[0] is None
            cur.execute(
                "SELECT count(*) FROM users WHERE primary_email = %s",
                ("shared@example.com",),
            )
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT id FROM users WHERE id = %s", (incumbent.user_id,))
            assert cur.fetchone() is not None

    def test_two_concurrent_first_sign_ins_converge_on_one_user(self, world):
        """The race the savepoint retry exists for, driven on real connections.

        THE ORDERING IS THE WHOLE TEST, and getting it wrong is silent: if A
        commits before B is called at all, B's own lookup HITS and the retry
        path is never entered — the assertions below still pass and prove
        nothing. Measured: with the retry deleted, that shape stayed green.

        So B runs in a thread and is driven INTO the collision. A inserts and
        does not commit; B's lookup therefore misses, and B's INSERT blocks on
        `uq_identity_per_provider` against A's uncommitted row. Only once B is
        observably waiting on a lock does A commit — at which point B's INSERT
        raises unique_violation and the retry re-reads A's now-visible row.
        """
        subject = "sub-race"
        result, failure = {}, {}

        with txn(world["ingress"], "svc_ingress") as a, txn(
            world["ingress"], "svc_ingress"
        ) as b:
            with b.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                b_pid = cur.fetchone()[0]

            first = upsert_google_identity(a, subject=subject, display_name="A")

            def racer():
                try:
                    result["got"] = upsert_google_identity(
                        b, subject=subject, display_name="B"
                    )
                except BaseException as exc:  # recorded, re-raised by the assert
                    failure["exc"] = exc

            thread = threading.Thread(target=racer)
            thread.start()
            try:
                with txn(world["stream"]) as watcher:
                    deadline = time.monotonic() + 20
                    blocked = False
                    while time.monotonic() < deadline:
                        with watcher.cursor() as cur:
                            cur.execute(
                                "SELECT cardinality(pg_blocking_pids(%s)) > 0", (b_pid,)
                            )
                            blocked = cur.fetchone()[0]
                        watcher.rollback()
                        if blocked:
                            break
                        time.sleep(0.05)
                assert blocked, (
                    "B never blocked on the unique index — the race was not"
                    " reproduced, so this test proves nothing about the retry"
                )
                a.commit()
            finally:
                thread.join(timeout=30)
            assert not thread.is_alive()
            assert "exc" not in failure, f"racer raised: {failure.get('exc')!r}"
            second = result["got"]
            b.commit()

            assert first.created is True
            assert second.created is False, "B took the retry path, not a plain hit"
            assert second.user_id == first.user_id
            with a.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM user_identities WHERE external_id = %s",
                    (subject,),
                )
                assert cur.fetchone()[0] == 1
                cur.execute(
                    "SELECT count(*) FROM users WHERE id = %s", (first.user_id,)
                )
                assert cur.fetchone()[0] == 1

    def test_an_empty_subject_is_refused_before_any_write(self, conn):
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users")
            before = cur.fetchone()[0]
        with pytest.raises(IdentityProvisioningError) as err:
            upsert_google_identity(conn, subject="   ")
        assert err.value.reason == "missing_subject"
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users")
            assert cur.fetchone()[0] == before


class TestTheSessionWriter:
    """`session_tokens`: mint, authenticate, revoke, slide."""

    def test_mint_stores_only_the_hash_and_authenticates(self, conn):
        user = _new_user(conn)
        minted = mint_session(conn, user)
        assert minted.token_hash == hashlib.sha256(minted.token.encode()).hexdigest()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token_hash, user_id, last_seen_at,"
                " expires_at > now() + make_interval(days => %s)"
                " FROM session_tokens WHERE id = %s",
                (SESSION_TTL_DAYS - 1, minted.session_id),
            )
            stored, owner, last_seen, roughly_ttl = cur.fetchone()
        # The raw value is nowhere in the row.
        assert stored == minted.token_hash and minted.token not in stored
        assert str(owner) == user and last_seen is None and roughly_ttl is True

        live = authenticate_session(conn, session_token_hash(minted.token))
        assert live.user_id == user and live.session_id == minted.session_id

    def test_an_unknown_token_is_invalid_not_empty(self, conn):
        with pytest.raises(TenantResolutionError) as err:
            authenticate_session(conn, session_token_hash("never-minted"))
        assert err.value.reason == "invalid_session"

    def test_revoke_is_idempotent_and_keeps_the_first_instant(self, live_session):
        conn, user, minted = live_session
        assert revoke_session(conn, minted.token_hash) is True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT revoked_at FROM session_tokens WHERE id = %s",
                (minted.session_id,),
            )
            first_kill = cur.fetchone()[0]
        assert revoke_session(conn, minted.token_hash) is False
        assert revoke_session(conn, session_token_hash("stale-cookie")) is False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT revoked_at FROM session_tokens WHERE id = %s",
                (minted.session_id,),
            )
            assert cur.fetchone()[0] == first_kill
        with pytest.raises(TenantResolutionError) as err:
            authenticate_session(conn, minted.token_hash)
        assert err.value.reason == "revoked_session"

    def test_an_expired_token_reports_expired_and_cannot_be_renewed(self, live_session):
        conn, user, minted = live_session
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE session_tokens SET expires_at = now() - interval '1 hour'"
                " WHERE id = %s",
                (minted.session_id,),
            )
        with pytest.raises(TenantResolutionError) as err:
            authenticate_session(conn, minted.token_hash)
        assert err.value.reason == "expired_session"
        # Presenting an expired token must not revive it.
        with pytest.raises(TenantResolutionError):
            touch_session(conn, minted.token_hash)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT expires_at < now() FROM session_tokens WHERE id = %s",
                (minted.session_id,),
            )
            assert cur.fetchone()[0] is True

    def test_touch_slides_the_window_and_stamps_last_seen(self, live_session):
        conn, user, minted = live_session
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE session_tokens SET expires_at = now() + interval '1 hour'"
                " WHERE id = %s RETURNING expires_at",
                (minted.session_id,),
            )
            shortened = cur.fetchone()[0]
        slid = touch_session(conn, minted.token_hash)
        assert slid.user_id == user
        assert slid.expires_at > shortened
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_seen_at IS NOT NULL FROM session_tokens WHERE id = %s",
                (minted.session_id,),
            )
            assert cur.fetchone()[0] is True

    def test_a_revoked_token_cannot_be_slid(self, live_session):
        conn, _user, minted = live_session
        revoke_session(conn, minted.token_hash)
        with pytest.raises(TenantResolutionError) as err:
            touch_session(conn, minted.token_hash)
        assert err.value.reason == "revoked_session"


class TestTheWorkspaceWriter:
    """`workspaces` + `workspace_members`, in one transaction, under RLS."""

    def test_create_workspace_commits_with_its_owner_and_audit_trail(self, conn):
        identity = upsert_google_identity(conn, subject="sub-ws-owner")
        made = create_workspace(
            conn, name="Owner's Space", owner_user_id=identity.user_id
        )
        # The commit is where the deferred invariant is checked. It is the
        # assertion, not a teardown step.
        conn.commit()
        with conn.cursor() as cur:
            _claim(cur, made.workspace_id)
            cur.execute(
                "SELECT name, state, tz FROM workspaces WHERE id = %s",
                (made.workspace_id,),
            )
            name, state, tz = cur.fetchone()
            cur.execute(
                "SELECT user_id::text, role FROM workspace_members"
                " WHERE workspace_id = %s",
                (made.workspace_id,),
            )
            members = cur.fetchall()
            cur.execute(
                "SELECT entity_kind, actor_kind, channel FROM audit_events"
                " WHERE workspace_id = %s ORDER BY id",
                (made.workspace_id,),
            )
            audit = cur.fetchall()
        conn.commit()
        assert (name, state, tz) == ("Owner's Space", "active", "UTC")
        assert members == [(identity.user_id, "owner")]
        assert [(k, a, c) for k, a, c in audit] == [
            ("workspace", "user", "web"),
            ("member", "user", "web"),
        ]

    def test_an_ownerless_workspace_cannot_commit(self, conn):
        """POSITIVE CONTROL for the invariant this writer relies on rather
        than re-implements: the same connection, the same schema, one INSERT
        short."""
        orphan = "11111111-2222-3333-4444-555555555555"
        with conn.cursor() as cur:
            _claim(cur, orphan)
            cur.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, 'ownerless')",
                (orphan,),
            )
        with pytest.raises(psycopg2.errors.CheckViolation) as err:
            conn.commit()
        assert "no owner" in str(err.value) or "without an owner" in str(err.value)
        conn.rollback()

    def test_rls_refuses_a_workspace_that_is_not_the_claimed_tenant(self, conn):
        """POSITIVE CONTROL that the policy is live: the same INSERT lands
        when the GUC names it and is refused when it names another id."""
        mine = "aaaaaaaa-0000-0000-0000-000000000001"
        other = "aaaaaaaa-0000-0000-0000-000000000002"
        with conn.cursor() as cur:
            _claim(cur, other)
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(
                    "INSERT INTO workspaces (id, name) VALUES (%s, 'foreign')",
                    (mine,),
                )
        conn.rollback()
        with conn.cursor() as cur:
            _claim(cur, mine)
            cur.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, 'own')", (mine,)
            )
        conn.rollback()

    def test_an_autocommit_connection_is_refused_by_name(self, world):
        conn = psycopg2.connect(world["ingress"])
        conn.autocommit = True
        try:
            with pytest.raises(TransactionRequired):
                create_workspace(conn, name="x", owner_user_id="ignored")
        finally:
            conn.close()

    def test_a_blank_name_and_a_missing_owner_are_refused(self, conn):
        with pytest.raises(TenantProvisioningError) as blank:
            create_workspace(conn, name="  ", owner_user_id="u")
        assert blank.value.reason == "invalid_name"
        with pytest.raises(TenantProvisioningError) as ownerless:
            create_workspace(conn, name="ok", owner_user_id="")
        assert ownerless.value.reason == "missing_owner"

    def test_an_invalid_timezone_is_refused_by_the_schema(self, conn):
        identity = upsert_google_identity(conn, subject="sub-badtz")
        with pytest.raises(psycopg2.errors.CheckViolation):
            create_workspace(
                conn,
                name="bad tz",
                owner_user_id=identity.user_id,
                tz="Mars/Olympus",
            )
        conn.rollback()

    def test_a_named_timezone_is_stored(self, conn):
        identity = upsert_google_identity(conn, subject="sub-goodtz")
        made = create_workspace(
            conn,
            name="ny",
            owner_user_id=identity.user_id,
            tz="America/New_York",
        )
        conn.commit()
        with conn.cursor() as cur:
            _claim(cur, made.workspace_id)
            cur.execute(
                "SELECT tz FROM workspaces WHERE id = %s", (made.workspace_id,)
            )
            assert cur.fetchone()[0] == "America/New_York"
        conn.commit()


class TestTheThreeWritersCompose:
    """Sign-up end to end: identity, then session, then tenant — the order
    the edge performs them in, on one transaction."""

    def test_sign_up_then_sign_in_reaches_the_new_workspace(self, conn):
        identity = upsert_google_identity(
            conn, subject="sub-e2e", email="e2e@example.com", display_name="E"
        )
        session = mint_session(conn, identity.user_id)
        made = create_workspace(
            conn, name="E2E", owner_user_id=identity.user_id
        )
        conn.commit()

        # A tenant-less moment is representable: the token authenticates
        # without naming any workspace at all.
        live = authenticate_session(conn, session.token_hash)
        assert live.user_id == identity.user_id

        resolved = resolve_web_session(
            conn, session.token_hash, made.workspace_id, minimum_role="owner"
        )
        conn.commit()
        assert resolved.workspace_id == made.workspace_id
        assert resolved.user_id == identity.user_id
        assert resolved.via == "session:owner"


class TestTheMembershipListGap:
    """The read the web surface needs and the printed policies do not serve.

    Pinned GREEN on purpose: the day a sanctioned door lands, this goes red
    and gets updated deliberately rather than silently continuing to describe
    a gap that closed.
    """

    def test_ingress_refuses_rather_than_reporting_no_workspaces(self, world, conn):
        identity = upsert_google_identity(conn, subject="sub-list")
        made = create_workspace(
            conn, name="Listed", owner_user_id=identity.user_id
        )
        conn.commit()
        with pytest.raises(TenantResolutionError) as err:
            workspaces_for_user(conn, identity.user_id)
        assert err.value.reason == "membership_list_unreadable"
        conn.rollback()

        # And the answer it refused to guess at is genuinely non-empty.
        with txn(world["stream"]) as owner_conn:
            rows = workspaces_for_user(owner_conn, identity.user_id)
        assert rows == [Membership(made.workspace_id, "owner")]
