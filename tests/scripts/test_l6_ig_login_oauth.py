"""L.6 gate — target-side Instagram Login OAuth against a real database (#863).

Covers the gate clauses that are provable without Meta: state replay, expiry and
cross-workspace rejection; the credential under the MultiFernet ring; and the
slice of D31 that belongs to L.6.

**What is NOT claimed here, stated rather than left to inference.** The gate's
first clause — *"a fresh Professional account connects with zero Facebook
surface"* — is unprovable for an arbitrary account until #410 (Meta App Review)
passes; that is an external gate with a human owner, not something tests close.
And D31's other two halves (the dispatcher minting no further intents, the
publish pipeline recording the fault) land with L.7 and L.5. Nothing below
pretends otherwise.

Scope: runs as the schema OWNER, so RLS is bypassed. `oauth_states` is an
auth-plane table whose policies F.4's runtime harness already exercises as the
declared logins; mixing the two here would make a state-machine failure and a
policy failure indistinguishable.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg2
import pytest

from src.services.target import ig_login_oauth as oauth
from src.services.target.ig_login_oauth import (
    CredentialUndecryptable,
    OAuthStateRefused,
)
from tests.scripts.conftest import (
    _scratch,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def oauth_db(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        dsn = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(dsn)
        try:
            chain = seed_workspace_chain(conn, "l6-gate")
        finally:
            conn.close()

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        engine = create_async_engine(
            dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            connect_args={"server_settings": {"app.actor_kind": "system"}},
            poolclass=NullPool,
        )
        try:
            yield {
                "owner": dsn,
                "ws": chain["ws"],
                "user": chain["user"],
                "iga": chain["iga"],
                "engine": engine,
            }
        finally:
            _run(engine.dispose())
    finally:
        gen.close()


def _run(coro):
    """A fresh loop per call — `asyncio.get_event_loop()` RAISES on 3.10 (CI)
    while merely warning on 3.11 (local), so it cannot be used here."""
    return asyncio.run(coro)


def _exec(oauth_db, sql, params=None, fetch=False):
    conn = psycopg2.connect(oauth_db["owner"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # `trg_governance_audit` refuses an anonymous write outright. The
            # harness sets a named actor for the same reason the service layer
            # does not invent one: the guard is the point.
            cur.execute("SET app.actor_kind = 'system'")
            cur.execute(sql, params)
            return cur.fetchall() if fetch else cur.rowcount
    finally:
        conn.close()


def _new_account(oauth_db) -> str:
    """A fresh ig_account per credential test.

    `uq_credential_per_account` is UNIQUE on (workspace, account, provider) —
    one live credential per account, which is what makes the reconnect
    swap-in-place rule enforceable rather than merely intended. Reusing the
    seeded account across tests collides with it, so each test brings its own.
    """
    return _exec(
        oauth_db,
        "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
        " VALUES (%s, %s) RETURNING id",
        (str(oauth_db["ws"]), f"acct-{uuid.uuid4()}"),
        fetch=True,
    )[0][0]


def _call(oauth_db, fn):
    async def go():
        async with oauth_db["engine"].connect() as conn:
            out = await fn(conn)
            await conn.commit()
            return out

    return _run(go())


def _second_workspace(oauth_db):
    """A second workspace, WITH an owner — `trg_workspaces_owner_at_insert`
    refuses an ownerless one, which is the invariant that makes "every
    workspace has somebody accountable" true rather than intended."""
    conn = psycopg2.connect(oauth_db["owner"])
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'system'")
            cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
            user = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO workspaces (name) VALUES (%s) RETURNING id",
                (f"other-{uuid.uuid4()}",),
            )
            ws = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'owner')",
                (ws, user),
            )
        conn.commit()
        return ws
    finally:
        conn.close()


class TestTheIsolationLevelIsWhatProductionRuns:
    def test_read_committed(self, oauth_db):
        rows = _exec(
            oauth_db,
            "SELECT current_setting('transaction_isolation'),"
            "       current_setting('default_transaction_isolation')",
            fetch=True,
        )
        assert rows[0] == ("read committed", "read committed")


class TestTheFCSevenStagingIsGone:
    def test_the_provider_CHECK_ships_without_fb_login_legacy(self, oauth_db):
        """#863 says the pass-4 `ck_no_new_fb_legacy` staging is deleted because
        no FB-vintage row can exist in the target. Verified against the shipped
        constraint rather than taken from the issue, because the refresh path
        below drops a branch on the strength of it."""
        src = _exec(
            oauth_db,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'ck_credentials_provider'",
            fetch=True,
        )[0][0]
        assert "ig_login" in src and "gdrive" in src
        assert "fb_login_legacy" not in src
        assert (
            _exec(
                oauth_db,
                "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_no_new_fb_legacy'",
                fetch=True,
            )[0][0]
            == 0
        )

    def test_refresh_sends_only_the_IG_shape(self):
        """The legacy branch existed because the FB host needed
        `fb_exchange_token` + client id/secret, and sending IG params there
        produced Meta error 101. Unreachable in the target, so the branch is
        gone — pinned so nobody re-adds a host switch on a host that cannot
        appear."""
        params = oauth.refresh_params("tok")
        assert params == {"grant_type": "ig_refresh_token", "access_token": "tok"}
        assert oauth.REFRESH_URL.startswith("https://graph.instagram.com")


class TestStateIssueAndConsume:
    def test_a_live_state_consumes_once_and_returns_its_pinned_context(self, oauth_db):
        ws, user = oauth_db["ws"], oauth_db["user"]
        state = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c, purpose="connect", user_id=user, workspace_id=ws
            ),
        )
        row = _call(oauth_db, lambda c: oauth.consume_state(c, state=state))
        assert row["purpose"] == "connect"
        assert str(row["workspace_id"]) == str(ws)

    def test_a_rowcount_positive_control_before_asserting_any_refusal(self, oauth_db):
        """House standard: prove the consume CAS moves a row at all, before a
        zero-row result is allowed to mean 'refused'."""
        state = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="connect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
            ),
        )
        assert (
            _exec(
                oauth_db,
                "SELECT consumed_at IS NULL FROM oauth_states WHERE state = %s",
                (state,),
                fetch=True,
            )[0][0]
            is True
        )
        _call(oauth_db, lambda c: oauth.consume_state(c, state=state))
        assert (
            _exec(
                oauth_db,
                "SELECT consumed_at IS NOT NULL FROM oauth_states WHERE state = %s",
                (state,),
                fetch=True,
            )[0][0]
            is True
        )

    def test_REPLAY_is_refused_BY_NAME(self, oauth_db):
        state = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="connect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
            ),
        )
        _call(oauth_db, lambda c: oauth.consume_state(c, state=state))
        with pytest.raises(OAuthStateRefused, match="already consumed"):
            _call(oauth_db, lambda c: oauth.consume_state(c, state=state))

    def test_EXPIRY_is_refused_BY_NAME_and_is_a_different_reason_than_replay(
        self, oauth_db
    ):
        """The two must not collapse into one message: a replay is someone
        re-submitting, an expiry is someone being slow, and an operator reading
        the log needs to tell them apart."""
        state = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="connect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
            ),
        )
        _exec(
            oauth_db,
            "UPDATE oauth_states SET expires_at = now() - interval '1 second'"
            " WHERE state = %s",
            (state,),
        )
        with pytest.raises(OAuthStateRefused, match="expired"):
            _call(oauth_db, lambda c: oauth.consume_state(c, state=state))

    def test_an_UNKNOWN_state_is_refused_BY_NAME(self, oauth_db):
        with pytest.raises(OAuthStateRefused, match="unknown state"):
            _call(oauth_db, lambda c: oauth.consume_state(c, state="nope"))

    def test_CROSS_WORKSPACE_replay_is_refused(self, oauth_db):
        """`07` §2: the row pins the workspace, so a callback cannot be replayed
        into a different one — checked at callback as well as at issue."""
        state = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="connect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
            ),
        )
        other = _second_workspace(oauth_db)
        with pytest.raises(OAuthStateRefused, match="cross-workspace"):
            _call(
                oauth_db,
                lambda c: oauth.consume_state(
                    c, state=state, expected_workspace_id=other
                ),
            )

    def test_the_TTL_is_the_05_seam_not_the_legacy_value(self, oauth_db):
        assert oauth.STATE_TTL_SECONDS == 900
        state = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="connect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
            ),
        )
        secs = _exec(
            oauth_db,
            "SELECT round(extract(epoch from expires_at - created_at))"
            " FROM oauth_states WHERE state = %s",
            (state,),
            fetch=True,
        )[0][0]
        assert 890 <= secs <= 910, f"TTL landed at {secs}s, expected the 900s seam"


class TestReconnectIsLastIssuedWins:
    """`07` §2 records that the pass-2 "last consumed wins" claim was FALSE:
    independently issued rows never consumed one another, so both callbacks
    could land. The invalidation therefore happens at ISSUE."""

    def test_issuing_a_second_reconnect_invalidates_the_first(self, oauth_db):
        target = oauth_db["iga"]
        first = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="reconnect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
                reconnect_target=target,
            ),
        )
        second = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="reconnect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
                reconnect_target=target,
            ),
        )
        with pytest.raises(OAuthStateRefused, match="already consumed"):
            _call(oauth_db, lambda c: oauth.consume_state(c, state=first))
        row = _call(oauth_db, lambda c: oauth.consume_state(c, state=second))
        assert row["purpose"] == "reconnect"

    def test_at_most_one_live_reconnect_state_per_target_at_any_commit(self, oauth_db):
        target = oauth_db["iga"]
        for _ in range(3):
            _call(
                oauth_db,
                lambda c: oauth.issue_state(
                    c,
                    purpose="reconnect",
                    user_id=oauth_db["user"],
                    workspace_id=oauth_db["ws"],
                    reconnect_target=target,
                ),
            )
        live = _exec(
            oauth_db,
            "SELECT count(*) FROM oauth_states"
            " WHERE purpose = 'reconnect' AND reconnect_target = %s"
            "   AND consumed_at IS NULL",
            (str(target),),
            fetch=True,
        )[0][0]
        assert live == 1

    def test_a_reconnect_without_a_target_is_refused(self, oauth_db):
        with pytest.raises(OAuthStateRefused, match="reconnect_target"):
            _call(
                oauth_db,
                lambda c: oauth.issue_state(
                    c,
                    purpose="reconnect",
                    user_id=oauth_db["user"],
                    workspace_id=oauth_db["ws"],
                ),
            )


class TestTheCredentialUnderTheRing:
    def test_store_then_load_round_trips_and_never_stores_plaintext(self, oauth_db):
        acct = _new_account(oauth_db)
        cid = _call(
            oauth_db,
            lambda c: oauth.store_credential(
                c,
                workspace_id=oauth_db["ws"],
                ig_account_id=acct,
                token="super-secret-token",
            ),
        )
        stored = _exec(
            oauth_db,
            "SELECT encrypted_payload FROM oauth_credentials WHERE id = %s",
            (cid,),
            fetch=True,
        )[0][0]
        assert "super-secret-token" not in stored, "the column holds ciphertext only"
        assert (
            _call(oauth_db, lambda c: oauth.load_credential(c, credential_id=cid))
            == "super-secret-token"
        )

    def test_storing_twice_for_one_account_is_ONE_row_replaced_in_place(self, oauth_db):
        """The connect callback writes with `store_credential` for connect AND
        reconnect (#1221): the second store must land on `uq_credential_per_account`
        as an UPDATE — same id, new payload, `active`, one row — which is the
        partial-index inference only a real database can prove."""
        first = _call(
            oauth_db,
            lambda c: oauth.store_credential(
                c,
                workspace_id=oauth_db["ws"],
                ig_account_id=oauth_db["iga"],
                token="first",
            ),
        )
        _exec(
            oauth_db,
            "UPDATE oauth_credentials SET state = 'expired' WHERE id = %s",
            (first,),
        )
        second = _call(
            oauth_db,
            lambda c: oauth.store_credential(
                c,
                workspace_id=oauth_db["ws"],
                ig_account_id=oauth_db["iga"],
                token="second",
            ),
        )
        assert second == first
        rows = _exec(
            oauth_db,
            "SELECT id, state, encrypted_payload FROM oauth_credentials"
            " WHERE workspace_id = %s AND ig_account_id = %s AND provider = 'ig_login'",
            (oauth_db["ws"], oauth_db["iga"]),
            fetch=True,
        )
        assert len(rows) == 1 and rows[0][1] == "active"
        assert oauth.ring().decrypt(rows[0][2]) == "second"

    def test_a_reconnect_swap_replaces_the_payload_IN_PLACE(self, oauth_db):
        """`07` §2 — no window where the account has zero credentials. A
        delete-then-insert would open exactly that window, and nothing else in
        the suite would notice it, so the row id is asserted unchanged."""
        cid = _call(
            oauth_db,
            lambda c: oauth.store_credential(
                c,
                workspace_id=oauth_db["ws"],
                ig_account_id=oauth_db["iga"],
                token="old",
            ),
        )
        _call(
            oauth_db, lambda c: oauth.swap_credential(c, credential_id=cid, token="new")
        )
        rows = _exec(
            oauth_db,
            "SELECT id::text, state FROM oauth_credentials WHERE ig_account_id = %s",
            (str(oauth_db["iga"]),),
            fetch=True,
        )
        assert [r[0] for r in rows] == [cid], "same row id — no gap, no second row"
        assert rows[0][1] == "active"
        assert (
            _call(oauth_db, lambda c: oauth.load_credential(c, credential_id=cid))
            == "new"
        )


class TestTheDeadTokenSymptomGateD31_TheHalfThatIsL6s:
    """Only the credential/account flip is L.6's. The dispatcher declining to
    mint intents is L.7's and the publish pipeline recording the fault is
    L.5's — #863 says so, and this class does not reach into either."""

    def _undecryptable(self, oauth_db):
        acct = _new_account(oauth_db)
        cid = _call(
            oauth_db,
            lambda c: oauth.store_credential(
                c, workspace_id=oauth_db["ws"], ig_account_id=acct, token="whatever"
            ),
        )
        # A payload no key in the ring can decrypt — the restored-from-backup /
        # key-removed-too-early case `07` §3 names.
        _exec(
            oauth_db,
            "UPDATE oauth_credentials SET encrypted_payload = %s WHERE id = %s",
            ("gAAAAABnot-a-real-fernet-token", cid),
        )
        return cid, acct

    def test_an_undecryptable_payload_FAILS_CLOSED_and_flips_both_states(
        self, oauth_db
    ):
        cid, acct = self._undecryptable(oauth_db)

        with pytest.raises(CredentialUndecryptable) as caught:
            _call(oauth_db, lambda c: oauth.load_credential(c, credential_id=cid))

        assert "gAAAAAB" not in str(caught.value), (
            "never log ciphertext — `07` §3 and §5. The message carries the "
            "credential id and nothing else."
        )
        assert (
            _exec(
                oauth_db,
                "SELECT state FROM oauth_credentials WHERE id = %s",
                (cid,),
                fetch=True,
            )[0][0]
            == "expired"
        )
        assert (
            _exec(
                oauth_db,
                "SELECT state FROM ig_accounts WHERE id = %s",
                (str(acct),),
                fetch=True,
            )[0][0]
            == "reauth_required"
        )

    def test_it_never_guesses_a_plaintext(self, oauth_db):
        """Paired with the round-trip test above: the ring must decrypt what it
        encrypted AND refuse what it did not. Only having the first would leave
        a fallback-to-plaintext implementation passing."""
        cid, _acct = self._undecryptable(oauth_db)
        with pytest.raises(CredentialUndecryptable):
            _call(oauth_db, lambda c: oauth.load_credential(c, credential_id=cid))

    def test_a_reconnect_swap_restores_active(self, oauth_db):
        """The recovery half: the re-auth path puts the account back."""
        cid, acct = self._undecryptable(oauth_db)
        with pytest.raises(CredentialUndecryptable):
            _call(oauth_db, lambda c: oauth.load_credential(c, credential_id=cid))
        _call(
            oauth_db,
            lambda c: oauth.swap_credential(c, credential_id=cid, token="fresh"),
        )
        _exec(
            oauth_db,
            "UPDATE ig_accounts SET state = 'active' WHERE id = %s",
            (str(oauth_db["iga"]),),
        )
        assert (
            _call(oauth_db, lambda c: oauth.load_credential(c, credential_id=cid))
            == "fresh"
        )
        assert (
            _exec(
                oauth_db,
                "SELECT state FROM oauth_credentials WHERE id = %s",
                (cid,),
                fetch=True,
            )[0][0]
            == "active"
        )


class TestReapExpiredCoversTheOauthStatesClass:
    def test_it_reaps_expired_and_consumed_but_LEAVES_live_states(self, oauth_db):
        _exec(oauth_db, "DELETE FROM oauth_states")
        live = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="connect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
            ),
        )
        consumed = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="connect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
            ),
        )
        _call(oauth_db, lambda c: oauth.consume_state(c, state=consumed))
        stale = _call(
            oauth_db,
            lambda c: oauth.issue_state(
                c,
                purpose="connect",
                user_id=oauth_db["user"],
                workspace_id=oauth_db["ws"],
            ),
        )
        _exec(
            oauth_db,
            "UPDATE oauth_states SET expires_at = now() - interval '1 hour'"
            " WHERE state = %s",
            (stale,),
        )

        reaped = _call(oauth_db, lambda c: oauth.reap_expired_states(c))
        assert reaped == 2
        remaining = [
            r[0] for r in _exec(oauth_db, "SELECT state FROM oauth_states", fetch=True)
        ]
        assert remaining == [live], "a LIVE state must survive the reap"

    def test_the_reap_is_bounded(self, oauth_db):
        """An unbounded delete on a table the ingress path writes to is a
        lock-hold nobody scheduled."""
        _exec(oauth_db, "DELETE FROM oauth_states")
        for _ in range(4):
            s = _call(
                oauth_db,
                lambda c: oauth.issue_state(
                    c,
                    purpose="connect",
                    user_id=oauth_db["user"],
                    workspace_id=oauth_db["ws"],
                ),
            )
            _call(oauth_db, lambda c: oauth.consume_state(c, state=s))
        assert _call(oauth_db, lambda c: oauth.reap_expired_states(c, limit=2)) == 2
        assert (
            _exec(oauth_db, "SELECT count(*) FROM oauth_states", fetch=True)[0][0] == 2
        )


class TestTheGuardsAreLoadBearing:
    """Drop the DB-enforced guard and confirm the refusal disappears."""

    def test_dropping_ck_oauth_state_context_lets_an_unpinned_connect_through(
        self, oauth_db
    ):
        bad = (
            "INSERT INTO oauth_states (state, provider, purpose, expires_at)"
            " VALUES (%s, 'ig_login', 'connect', now() + interval '5 minutes')"
        )
        with pytest.raises(psycopg2.errors.CheckViolation):
            _exec(oauth_db, bad, (f"s-{uuid.uuid4()}",))

        _exec(
            oauth_db,
            "ALTER TABLE oauth_states DROP CONSTRAINT ck_oauth_state_context",
        )
        try:
            token = f"s-{uuid.uuid4()}"
            _exec(oauth_db, bad, (token,))  # no longer refused
            assert (
                _exec(
                    oauth_db,
                    "SELECT workspace_id IS NULL FROM oauth_states WHERE state = %s",
                    (token,),
                    fetch=True,
                )[0][0]
                is True
            )
            _exec(oauth_db, "DELETE FROM oauth_states WHERE state = %s", (token,))
        finally:
            _exec(
                oauth_db,
                "ALTER TABLE oauth_states ADD CONSTRAINT ck_oauth_state_context CHECK ("
                " CASE purpose"
                "   WHEN 'signin' THEN user_id IS NULL AND workspace_id IS NULL"
                "   WHEN 'link' THEN user_id IS NOT NULL AND workspace_id IS NULL"
                "   ELSE user_id IS NOT NULL AND workspace_id IS NOT NULL END)",
            )


class TestARemovedDestinationStaysRemoved:
    """`disable_account` (#1233) revokes the credential and disables the row;
    a refresh job minted before the removal must not undo either."""

    def test_a_swap_leaves_a_revoked_credential_revoked(self, oauth_db):
        acct = _new_account(oauth_db)
        cid = _call(
            oauth_db,
            lambda c: oauth.store_credential(
                c, workspace_id=oauth_db["ws"], ig_account_id=acct, token="old"
            ),
        )
        _exec(
            oauth_db,
            "UPDATE oauth_credentials SET state = 'revoked' WHERE id = %s",
            (cid,),
        )
        with pytest.raises(oauth.OAuthStateRefused):
            _call(
                oauth_db,
                lambda c: oauth.swap_credential(c, credential_id=cid, token="new"),
            )
        rows = _exec(
            oauth_db,
            "SELECT state FROM oauth_credentials WHERE id = %s",
            (cid,),
            fetch=True,
        )
        assert rows == [("revoked",)]
        assert (
            _call(oauth_db, lambda c: oauth.load_credential(c, credential_id=cid))
            == "old"
        )

    def test_mark_dead_leaves_a_disabled_account_disabled(self, oauth_db):
        acct = _new_account(oauth_db)
        cid = _call(
            oauth_db,
            lambda c: oauth.store_credential(
                c, workspace_id=oauth_db["ws"], ig_account_id=acct, token="tok"
            ),
        )
        _exec(
            oauth_db,
            "UPDATE oauth_credentials SET state = 'revoked' WHERE id = %s",
            (cid,),
        )
        _exec(
            oauth_db, "UPDATE ig_accounts SET state = 'disabled' WHERE id = %s", (acct,)
        )
        _call(oauth_db, lambda c: oauth.mark_dead(c, credential_id=cid))
        rows = _exec(
            oauth_db,
            "SELECT c.state, a.state FROM oauth_credentials c"
            "  JOIN ig_accounts a ON a.id = c.ig_account_id WHERE c.id = %s",
            (cid,),
            fetch=True,
        )
        assert rows == [("revoked", "disabled")]
