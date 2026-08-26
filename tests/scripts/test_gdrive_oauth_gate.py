"""The Drive connect gate — the first non-`ig_login` credential, measured as
`svc_ingress` under RLS (P3 of the gdrive credential epic).

Two workspaces, for the reason every tenancy assertion in this tier needs two:
"the row landed in workspace A" is only a claim if there was a workspace B it
could have landed in instead. The route pair is driven through the REAL app
with only the two provider calls stubbed (the sign-in exchange, as the X.2
gate does it, and the Drive exchange), so what is measured is the state row,
the credential row and the read door that #1054 shipped against nothing.
"""

from __future__ import annotations

import asyncio
import psycopg2
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy.exc import DBAPIError

from src.services.target import drive_credentials
from src.services.target import google_drive_oauth as drive
from src.services.target.ig_login_oauth import (
    OAuthStateRefused,
    consume_state,
    issue_state,
)
from src.services.target.media_sync import DriveCredentialDead
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_all,
    fetch_one,
    in_tenant,
    ingress_engine,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import API, FRONT, api_client, sign_in
from tests.src.services.target.conftest import drive_grant

google_configured = api_conftest.google_configured

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, "drive-a")
            b = seed_workspace_chain(conn, "drive-b")
        finally:
            conn.close()
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress"), "a": a, "b": b}
    finally:
        gen.close()


# --- drivers -----------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _error_page(reason: str) -> str:
    """Every Drive failure lands on the error page with its leg named, so the
    sign-in-shaped page can render the connect leg's copy."""
    return f"{FRONT}/auth/error?reason={reason}&flow=drive"


def _store(world, tenant, *, source, grant) -> str:
    """`store_credential` as the callback runs it: one committed unit of work
    under *tenant*, as its user. Returns the credential id."""
    return _run(
        in_tenant(
            world["ingress"],
            tenant["ws"],
            tenant["user"],
            lambda s: drive.store_credential(
                s, workspace_id=tenant["ws"], media_source_id=source, grant=grant
            ),
        )
    )


async def _token(dsn: str, source_id, workspace_id) -> str:
    """The read door, on an ingress engine — the worker's own shape."""
    async with ingress_engine(dsn) as engine:
        return await drive_credentials.token_for_source(
            engine, str(source_id), workspace_id=str(workspace_id)
        )


def _credential_rows(world, source_id) -> list[dict]:
    return fetch_all(
        world["stream"],
        "SELECT id, provider, media_source_id, ig_account_id, next_refresh_at,"
        "       state, expires_at, encrypted_payload"
        "  FROM oauth_credentials WHERE media_source_id = %s",
        (str(source_id),),
    )


# --- the credential row ------------------------------------------------------


class TestTheCredentialRow:
    def test_store_writes_a_source_owned_gdrive_row_and_the_read_door_answers(
        self, world
    ):
        a = world["a"]
        cid = _store(world, a, source=a["src"], grant=drive_grant())
        (row,) = _credential_rows(world, a["src"])
        assert str(row["id"]) == cid
        # THE invariant a copy of ig_login's writer breaks and the schema
        # cannot catch: provider AND both owner columns, explicitly.
        assert row["provider"] == "gdrive"
        assert str(row["media_source_id"]) == str(a["src"])
        assert row["ig_account_id"] is None
        # F3 (b): the 063 fence stays closed, so this must be NULL.
        assert row["next_refresh_at"] is None
        assert row["state"] == "active"
        assert row["expires_at"] is not None
        # ciphertext only — neither token, and no envelope field name, in the clear
        for secret in ("ya29.access", "1//refresh", "refresh_token"):
            assert secret not in row["encrypted_payload"]
        # #1054's read door, which shipped against nothing, answers for the
        # first time — with the ACCESS token, straight into a bearer header.
        assert _run(_token(world["ingress"], a["src"], a["ws"])) == "ya29.access"

    def test_a_reconnect_replaces_the_row_in_place(self, world):
        a = world["a"]
        (before,) = _credential_rows(world, a["src"])
        cid = _store(
            world, a, source=a["src"], grant=drive_grant(access_token="ya29.second")
        )
        (after,) = _credential_rows(world, a["src"])
        assert str(after["id"]) == cid == str(before["id"]), (
            "same row id — no gap, no second row"
        )
        assert after["encrypted_payload"] != before["encrypted_payload"], (
            "the payload moved"
        )
        assert _run(_token(world["ingress"], a["src"], a["ws"])) == "ya29.second"

    def test_the_credential_is_bound_to_its_workspace(self, world):
        a, b = world["a"], world["b"]
        # Writing A's source under tenant B: the composite FK
        # (workspace_id, media_source_id) → media_sources (workspace_id, id)
        # makes a cross-workspace credential impossible by construction, and
        # `p_tenant` would hide the source anyway.
        with pytest.raises(DBAPIError):
            _store(world, b, source=a["src"], grant=drive_grant())
        assert len(_credential_rows(world, a["src"])) == 1
        # Reading A's credential from tenant B: nothing, refused by name —
        # never B reading A's token.
        with pytest.raises(DriveCredentialDead):
            _run(_token(world["ingress"], a["src"], b["ws"]))

    def test_an_expired_access_token_is_refused_until_the_read_path_mints(self, world):
        b = world["b"]
        _store(
            world,
            b,
            source=b["src"],
            grant=drive_grant(
                expires_at=datetime.now(timezone.utc) - timedelta(days=1)
            ),
        )
        with pytest.raises(DriveCredentialDead, match="expired"):
            _run(_token(world["ingress"], b["src"], b["ws"]))


# --- the state row -----------------------------------------------------------


class TestTheStateRow:
    @staticmethod
    def _issue(world, tenant, *, purpose="connect") -> str:
        async def issue(s):
            return await issue_state(
                s,
                purpose=purpose,
                provider=drive.PROVIDER,
                user_id=tenant["user"],
                workspace_id=tenant["ws"],
                reconnect_target=tenant["src"],
            )

        return _run(in_tenant(world["ingress"], tenant["ws"], tenant["user"], issue))

    @staticmethod
    def _consume(world, tenant, state, **expect) -> dict:
        return _run(
            in_tenant(
                world["ingress"],
                tenant["ws"],
                tenant["user"],
                lambda s: consume_state(s, state=state, **expect),
            )
        )

    def test_a_connect_state_pins_user_workspace_and_source(self, world):
        """Also the positive control for the named refusals below: the
        expectations the Drive callback passes ADMIT the state it minted."""
        a = world["a"]
        state = self._issue(world, a)
        row = self._consume(
            world,
            a,
            state,
            expected_provider=drive.PROVIDER,
            expected_purpose={"connect", "reconnect"},
        )
        assert row["provider"] == "gdrive"
        assert row["purpose"] == "connect"
        assert str(row["workspace_id"]) == str(a["ws"])
        assert str(row["reconnect_target"]) == str(a["src"])

    def test_an_unknown_state_is_refused_by_name(self, world):
        with pytest.raises(OAuthStateRefused, match="unknown state"):
            self._consume(world, world["a"], "never-issued")

    def test_a_state_for_another_leg_is_refused_by_name(self, world):
        """The callback's expectations are what keep a sign-in state out of
        the Drive leg and a Drive state out of sign-in: each mismatch is its
        own reason, and the state is burned either way — one-shot."""
        a = world["a"]
        state = self._issue(world, a)
        with pytest.raises(OAuthStateRefused, match="wrong provider"):
            self._consume(world, a, state, expected_provider="google")
        state = self._issue(world, a)
        with pytest.raises(OAuthStateRefused, match="wrong purpose"):
            self._consume(world, a, state, expected_purpose="signin")
        with pytest.raises(OAuthStateRefused, match="already consumed"):
            self._consume(world, a, state)


# --- the route pair, through the real app --------------------------------------


class TestTheRoutePairAsSvcIngress:
    def test_connect_then_callback_writes_the_credential(
        self, world, google_configured, monkeypatch
    ):
        async def main():
            async with api_client(world["ingress"]) as (client, ingress):
                owner = await sign_in(
                    client, monkeypatch, sub="sub-drive", email="d@example.test"
                )
                made = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "Drive"},
                    headers={**owner, "Idempotency-Key": "drive-1"},
                )
                assert made.status_code == 201, made.text
                ws = made.json()["workspace_id"]
                source = await client.post(
                    f"/api/v1/workspaces/{ws}/sources",
                    json={"folder_ref": "folder-abc", "root_name": "Stories"},
                    headers=owner,
                )
                assert source.status_code == 201, source.text
                sid = source.json()["source_id"]

                # 1. The connect route: the state row and where the browser goes.
                started = await client.post(
                    f"/api/v1/workspaces/{ws}/sources/{sid}/connect", headers=owner
                )
                assert started.status_code == 200, started.text
                url = started.json()["authorization_url"]
                q = parse_qs(urlsplit(url).query)
                assert q["scope"] == [drive.SCOPE]
                assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
                assert q["redirect_uri"] == [f"{API}/auth/google-drive/callback"]
                state = q["state"][0]
                assert fetch_one(
                    world["stream"],
                    "SELECT provider, purpose, reconnect_target::text, workspace_id::text"
                    "  FROM oauth_states WHERE state = %s",
                    (state,),
                ) == ("gdrive", "connect", sid, ws)

                # 2. The callback, with only the Drive exchange stubbed.
                async def exchange_code(client_, **kw):
                    assert kw["code"] == "c0de"
                    return drive_grant()

                monkeypatch.setattr(drive, "exchange_code", exchange_code)
                done = await client.get(
                    f"/auth/google-drive/callback?state={state}&code=c0de",
                    follow_redirects=False,
                )
                assert done.status_code == 302, done.text
                assert (
                    done.headers["location"]
                    == f"{FRONT}/dashboard/settings?connected=gdrive"
                )
                (row,) = _credential_rows(world, sid)
                assert (
                    row["provider"],
                    str(row["media_source_id"]),
                    row["ig_account_id"],
                    row["next_refresh_at"],
                    row["state"],
                ) == ("gdrive", sid, None, None, "active")
                # the read door, as the worker's adapter would call it
                assert (
                    await drive_credentials.token_for_source(
                        ingress, sid, workspace_id=ws
                    )
                    == "ya29.access"
                )

                # 3. A replayed callback is refused: the state is one-shot.
                again = await client.get(
                    f"/auth/google-drive/callback?state={state}&code=c0de",
                    follow_redirects=False,
                )
                assert again.status_code == 302
                assert again.headers["location"] == _error_page("state_refused")
                assert len(_credential_rows(world, sid)) == 1

                # 4. A reconnect: a 'reconnect' state, and the row replaced in place.
                restarted = await client.post(
                    f"/api/v1/workspaces/{ws}/sources/{sid}/connect", headers=owner
                )
                state2 = parse_qs(
                    urlsplit(restarted.json()["authorization_url"]).query
                )["state"][0]
                assert fetch_one(
                    world["stream"],
                    "SELECT purpose, reconnect_target::text FROM oauth_states WHERE state = %s",
                    (state2,),
                ) == ("reconnect", sid)
                redone = await client.get(
                    f"/auth/google-drive/callback?state={state2}&code=c0de",
                    follow_redirects=False,
                )
                assert redone.status_code == 302, redone.text
                after = _credential_rows(world, sid)
                assert [str(r["id"]) for r in after] == [str(row["id"])]

                # 5. A source that is not this workspace's is not found — never 403.
                stranger = await client.post(
                    f"/api/v1/workspaces/{ws}/sources/{world['a']['src']}/connect",
                    headers=owner,
                )
                assert stranger.status_code == 404, stranger.text

        _run(main())

    def test_a_sign_in_state_cannot_be_replayed_into_the_drive_callback(
        self, world, google_configured
    ):
        async def main():
            async with api_client(world["ingress"]) as (client, _):
                start = await client.get("/auth/google", follow_redirects=False)
                state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
                done = await client.get(
                    f"/auth/google-drive/callback?state={state}&code=c0de",
                    follow_redirects=False,
                )
                assert done.status_code == 302
                assert done.headers["location"] == _error_page("state_refused")

        _run(main())

    def test_a_declined_consent_lands_on_the_error_page_and_writes_nothing(
        self, world, google_configured
    ):
        async def main():
            async with api_client(world["ingress"]) as (client, _):
                done = await client.get(
                    "/auth/google-drive/callback?error=access_denied&state=x",
                    follow_redirects=False,
                )
                assert done.status_code == 302
                assert done.headers["location"] == _error_page("denied")

        _run(main())
