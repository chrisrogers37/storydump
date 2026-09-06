"""The Drive connect gate — the first non-`ig_login` credential, measured as
`svc_ingress` under RLS (P3 of the gdrive credential epic; since 069 the grant
is the WORKSPACE's — owner ruling 2026-09-05, #1165 lean (b), `07` §15).

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

from src.api.routes import auth as auth_routes
from src.services.target import drive_credentials
from src.services.target import google_drive_oauth as drive
from src.services.target.ig_login_oauth import (
    OAuthStateRefused,
    consume_state,
    issue_state,
)
from src.services.target.drive_adapter import DriveRetryableError
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
from tests.src.api.conftest import API, FRONT, api_client, cookie_value, sign_in
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


def _store(world, tenant, *, grant) -> str:
    """`store_credential` as the callback runs it: one committed unit of work
    under *tenant*, as its user. Returns the credential id. The grant is the
    workspace's (069): no source is named."""
    return _run(
        in_tenant(
            world["ingress"],
            tenant["ws"],
            tenant["user"],
            lambda s: drive.store_credential(s, workspace_id=tenant["ws"], grant=grant),
        )
    )


async def _token(dsn: str, workspace_id) -> str:
    """The read door, on an ingress engine — the worker's own shape."""
    async with ingress_engine(dsn) as engine:
        return await drive_credentials.token_for_workspace(engine, str(workspace_id))


def _credential_rows(world, workspace_id) -> list[dict]:
    return fetch_all(
        world["stream"],
        "SELECT id, provider, media_source_id, ig_account_id, next_refresh_at,"
        "       state, expires_at, encrypted_payload"
        "  FROM oauth_credentials"
        " WHERE workspace_id = %s AND provider = 'gdrive'"
        "   AND media_source_id IS NULL AND ig_account_id IS NULL",
        (str(workspace_id),),
    )


# --- the credential row ------------------------------------------------------


class TestTheCredentialRow:
    def test_store_writes_a_workspace_owned_gdrive_row_and_the_read_door_answers(
        self, world
    ):
        a = world["a"]
        cid = _store(world, a, grant=drive_grant())
        (row,) = _credential_rows(world, a["ws"])
        assert str(row["id"]) == cid
        # THE invariant 069 states: provider, and NO owner column — the
        # workspace is the owner.
        assert row["provider"] == "gdrive"
        assert row["media_source_id"] is None and row["ig_account_id"] is None
        # F3 (b): the 063 fence stays closed, so this must be NULL.
        assert row["next_refresh_at"] is None
        assert row["state"] == "active"
        assert row["expires_at"] is not None
        # ciphertext only — neither token, and no envelope field name, in the clear
        for secret in ("ya29.access", "1//refresh", "refresh_token"):
            assert secret not in row["encrypted_payload"]
        # The read door answers by WORKSPACE — and by source, which is the
        # same door: a folder's token is its workspace's grant.
        assert _run(_token(world["ingress"], a["ws"])) == "ya29.access"

        async def by_source():
            async with ingress_engine(world["ingress"]) as engine:
                return await drive_credentials.token_for_source(
                    engine, str(a["src"]), workspace_id=str(a["ws"])
                )

        assert _run(by_source()) == "ya29.access"

    def test_a_reconnect_replaces_the_row_in_place(self, world):
        a = world["a"]
        (before,) = _credential_rows(world, a["ws"])
        cid = _store(world, a, grant=drive_grant(access_token="ya29.second"))
        (after,) = _credential_rows(world, a["ws"])
        assert str(after["id"]) == cid == str(before["id"]), (
            "same row id — no gap, no second row"
        )
        assert after["encrypted_payload"] != before["encrypted_payload"], (
            "the payload moved"
        )
        assert _run(_token(world["ingress"], a["ws"])) == "ya29.second"

    def test_each_workspace_holds_its_own_grant(self, world):
        """The owner's question (2026-09-05): the same Google account
        connected from another workspace is a SECOND grant, held there."""
        a, b = world["a"], world["b"]
        # B holds nothing yet: A's grant is not B's, refused by name.
        with pytest.raises(DriveCredentialDead, match="never connected"):
            _run(_token(world["ingress"], b["ws"]))
        _store(world, b, grant=drive_grant(access_token="ya29.b"))
        assert _run(_token(world["ingress"], b["ws"])) == "ya29.b"
        assert _run(_token(world["ingress"], a["ws"])) == "ya29.second"
        assert len(_credential_rows(world, a["ws"])) == 1
        assert len(_credential_rows(world, b["ws"])) == 1

    def test_under_the_owner_role_the_where_alone_binds_the_row(self, world):
        """Production connects as the owner role with BYPASSRLS, so the tenant
        GUC binds nothing there: the WHERE must. Read on the OWNER stream —
        A holds a grant, B holds none — and B must still get nothing
        (review of #1246: the first cut relied on RLS alone)."""
        a, b = world["a"], world["b"]

        async def owner_read(ws):
            async with ingress_engine(world["stream"]) as engine:
                return await drive_credentials.token_for_workspace(engine, str(ws))

        # Both hold a grant from the tests above; make B's distinguishable and
        # then remove it, so a leak would surface as A's token under B's id.
        _store(world, b, grant=drive_grant(access_token="ya29.b-owner"))
        assert _run(owner_read(b["ws"])) == "ya29.b-owner"
        conn = psycopg2.connect(world["stream"])
        try:
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "DELETE FROM oauth_credentials WHERE workspace_id = %s AND provider = 'gdrive'",
                    (str(b["ws"]),),
                )
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(DriveCredentialDead, match="never connected"):
            _run(owner_read(b["ws"]))
        assert _run(owner_read(a["ws"])) == "ya29.second"

    def test_the_schema_refuses_a_per_source_gdrive_row(self, world):
        """069's CHECK, measured: the folder-first shape cannot come back by
        accident — a gdrive row naming a source is refused at the constraint."""
        a = world["a"]
        conn = psycopg2.connect(world["stream"])
        try:
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                with pytest.raises(psycopg2.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO oauth_credentials"
                        " (workspace_id, media_source_id, provider, encrypted_payload)"
                        " VALUES (%s, %s, 'gdrive', 'ct')",
                        (str(a["ws"]), str(a["src"])),
                    )
        finally:
            conn.rollback()
            conn.close()

    def test_an_expired_access_token_is_minted_on_the_read_path(
        self, world, monkeypatch
    ):
        """P5, F3 (b) (#1247): the read door refreshes an expired access token
        from the stored refresh token, writes the new envelope IN PLACE (same
        row) and hands the fresh token back — measured on the real row."""
        from src.config.settings import settings

        b = world["b"]
        _store(
            world,
            b,
            grant=drive_grant(
                expires_at=datetime.now(timezone.utc) - timedelta(days=1)
            ),
        )
        (before,) = _credential_rows(world, b["ws"])
        asked = []

        async def refresh_access_token(
            client, *, refresh_token, client_id, client_secret
        ):
            asked.append((refresh_token, client_id))
            return drive_grant(access_token="ya29.fresh")

        monkeypatch.setattr(drive, "refresh_access_token", refresh_access_token)
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "gid", raising=False)
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec", raising=False)

        assert _run(_token(world["ingress"], b["ws"])) == "ya29.fresh"
        assert asked == [("1//refresh", "gid")]
        (after,) = _credential_rows(world, b["ws"])
        assert str(after["id"]) == str(before["id"]), "in place — same row"
        assert after["expires_at"] > datetime.now(timezone.utc)
        assert after["encrypted_payload"] != before["encrypted_payload"]
        assert after["state"] == "active"
        # Live now: the next read hands the token back with no refresh.
        assert _run(_token(world["ingress"], b["ws"])) == "ya29.fresh"
        assert len(asked) == 1

    def test_googles_invalid_grant_marks_the_row_expired(self, world, monkeypatch):
        """D31's definitive class: the grant is gone on Google's side, so the
        row goes `expired` — `drive_status` reads reconnect, the picker
        refuses `drive_reconnect_needed` — and the refusal says reconnect."""
        from src.config.settings import settings

        b = world["b"]
        _store(
            world,
            b,
            grant=drive_grant(
                expires_at=datetime.now(timezone.utc) - timedelta(days=1)
            ),
        )

        async def refresh_access_token(client, **kw):
            raise drive.DriveOAuthRefused("grant_revoked", "invalid_grant")

        monkeypatch.setattr(drive, "refresh_access_token", refresh_access_token)
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "gid", raising=False)
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec", raising=False)
        with pytest.raises(DriveCredentialDead, match="reconnect"):
            _run(_token(world["ingress"], b["ws"]))
        (row,) = _credential_rows(world, b["ws"])
        assert row["state"] == "expired"
        # And the process that cannot refresh says which variables it lacks.
        _store(
            world,
            b,
            grant=drive_grant(
                expires_at=datetime.now(timezone.utc) - timedelta(days=1)
            ),
        )
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", None, raising=False)
        with pytest.raises(DriveRetryableError, match="GOOGLE_CLIENT_SECRET"):
            _run(_token(world["ingress"], b["ws"]))


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
                reconnect_target=tenant["ws"],
            )

        return _run(in_tenant(world["ingress"], tenant["ws"], tenant["user"], issue))

    @staticmethod
    def _consume(world, tenant, state, **expect):
        """Consume as the callback does: a refusal is caught INSIDE the
        transaction, which therefore commits — so a refused state is burned.
        A driver that let the refusal unwind the unit of work would roll the
        CAS back and prove the opposite. Returns the row, or the refusal."""

        async def consume(s):
            try:
                return await consume_state(s, state=state, **expect)
            except OAuthStateRefused as exc:
                return exc

        return _run(in_tenant(world["ingress"], tenant["ws"], tenant["user"], consume))

    @staticmethod
    def _refused(result, reason: str) -> None:
        assert isinstance(result, OAuthStateRefused), result
        assert reason in str(result), result

    def test_a_connect_state_pins_user_workspace_and_the_workspace_as_target(
        self, world
    ):
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
        assert not isinstance(row, OAuthStateRefused), row
        assert row["provider"] == "gdrive"
        assert row["purpose"] == "connect"
        assert str(row["workspace_id"]) == str(a["ws"])
        assert str(row["reconnect_target"]) == str(a["ws"])

    def test_an_unknown_state_is_refused_by_name(self, world):
        self._refused(self._consume(world, world["a"], "never-issued"), "unknown state")

    def test_a_state_for_another_leg_is_refused_by_name_and_burned(self, world):
        """The callback's expectations are what keep a sign-in state out of
        the Drive leg and a Drive state out of sign-in: each mismatch is its
        own reason, and the state is consumed either way — a refused
        presentation burns it, exactly as a cross-workspace one does."""
        a = world["a"]
        state = self._issue(world, a)
        self._refused(
            self._consume(world, a, state, expected_provider="google"), "wrong provider"
        )
        state = self._issue(world, a)
        self._refused(
            self._consume(world, a, state, expected_purpose="signin"), "wrong purpose"
        )
        self._refused(self._consume(world, a, state), "already consumed")


# --- the route pair, through the real app --------------------------------------


class TestTheRoutePairAsSvcIngress:
    def test_connect_then_callback_writes_the_grant_and_folders_pick_under_it(
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

                # 0. No grant yet: a folder cannot be picked, and the status says why.
                early = await client.post(
                    f"/api/v1/workspaces/{ws}/sources",
                    json={"folder_ref": "folder-abc"},
                    headers=owner,
                )
                assert early.status_code == 409, early.text
                assert early.json()["detail"] == "drive_not_connected"
                status = await client.get(
                    f"/api/v1/workspaces/{ws}/drive", headers=owner
                )
                assert status.json()["drive"] == {
                    "status": "none",
                    "connected_at": None,
                }

                # 1. The connect route: the state row and where the browser goes.
                started = await client.post(
                    f"/api/v1/workspaces/{ws}/drive/connect", headers=owner
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
                ) == ("gdrive", "connect", ws, ws)

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
                (row,) = _credential_rows(world, ws)
                assert (
                    row["provider"],
                    row["media_source_id"],
                    row["ig_account_id"],
                    row["next_refresh_at"],
                    row["state"],
                ) == ("gdrive", None, None, None, "active")
                assert (
                    await drive_credentials.token_for_workspace(ingress, ws)
                    == "ya29.access"
                )
                status = await client.get(
                    f"/api/v1/workspaces/{ws}/drive", headers=owner
                )
                assert status.json()["drive"]["status"] == "active"
                assert status.json()["drive"]["connected_at"] is not None

                # 3. A replayed callback is refused: the state is one-shot.
                again = await client.get(
                    f"/auth/google-drive/callback?state={state}&code=c0de",
                    follow_redirects=False,
                )
                assert again.status_code == 302
                assert again.headers["location"] == _error_page("state_refused")
                assert len(_credential_rows(world, ws)) == 1

                # 4. A reconnect: a 'reconnect' state, and the row replaced in place.
                restarted = await client.post(
                    f"/api/v1/workspaces/{ws}/drive/connect", headers=owner
                )
                state2 = parse_qs(
                    urlsplit(restarted.json()["authorization_url"]).query
                )["state"][0]
                assert fetch_one(
                    world["stream"],
                    "SELECT purpose, reconnect_target::text FROM oauth_states WHERE state = %s",
                    (state2,),
                ) == ("reconnect", ws)
                redone = await client.get(
                    f"/auth/google-drive/callback?state={state2}&code=c0de",
                    follow_redirects=False,
                )
                assert redone.status_code == 302, redone.text
                after = _credential_rows(world, ws)
                assert [str(r["id"]) for r in after] == [str(row["id"])]

                # 5. The browser, through the grant (the transport stubbed —
                # what is measured is admission and wiring, the mapping is unit).
                from src.api.routes import v1

                from src.services.target.google_drive_adapter import FolderPage

                class _Adapter:
                    async def list_folders(self, *, parent, workspace_id):
                        assert workspace_id == ws
                        return FolderPage([{"id": "folder-abc", "name": "Stories"}])

                monkeypatch.setattr(v1, "_drive_adapter", lambda request: _Adapter())
                listed = await client.get(
                    f"/api/v1/workspaces/{ws}/drive/folders", headers=owner
                )
                assert listed.status_code == 200, listed.text
                assert listed.json() == {
                    "parent": "root",
                    "folders": [{"id": "folder-abc", "name": "Stories"}],
                    "truncated": False,
                }

                # 6. A folder picked under the grant: created, named, ARMED.
                picked = await client.post(
                    f"/api/v1/workspaces/{ws}/sources",
                    json={"folder_ref": "folder-abc", "folder_name": "Stories"},
                    headers=owner,
                )
                assert picked.status_code == 201, picked.text
                sid = picked.json()["source_id"]
                assert fetch_one(
                    world["stream"],
                    "SELECT state, next_sync_at IS NOT NULL, config->>'folder_name',"
                    "       config->>'folder_ref'"
                    "  FROM media_sources WHERE id = %s",
                    (sid,),
                ) == ("active", True, "Stories", "folder-abc")
                sources = await client.get(
                    f"/api/v1/workspaces/{ws}/sources", headers=owner
                )
                (src_row,) = sources.json()["sources"]
                assert (src_row["folder_ref"], src_row["folder_name"]) == (
                    "folder-abc",
                    "Stories",
                )
                assert "credential_status" not in src_row

                # 7. Removed = paused, never deleted; picked again = revived.
                removed = await client.delete(
                    f"/api/v1/workspaces/{ws}/sources/{sid}", headers=owner
                )
                assert removed.status_code == 200, removed.text
                assert removed.json() == {"source_id": sid, "state": "paused"}
                assert fetch_one(
                    world["stream"],
                    "SELECT state FROM media_sources WHERE id = %s",
                    (sid,),
                ) == ("paused",)
                revived = await client.post(
                    f"/api/v1/workspaces/{ws}/sources",
                    json={"folder_ref": "folder-abc", "folder_name": "Stories 2"},
                    headers=owner,
                )
                assert revived.status_code == 200, revived.text
                assert revived.json() == {"source_id": sid, "created": False}
                assert fetch_one(
                    world["stream"],
                    "SELECT state, config->>'folder_name' FROM media_sources WHERE id = %s",
                    (sid,),
                ) == ("active", "Stories 2")

                # 8. The folder-first route is gone.
                gone = await client.post(
                    f"/api/v1/workspaces/{ws}/sources/{sid}/connect", headers=owner
                )
                assert gone.status_code in (404, 405), gone.text

                # 9. Removed stays removed across a RECONNECT (review of #1246):
                # the marker tells a removal apart from a disconnect's pause.
                await client.delete(
                    f"/api/v1/workspaces/{ws}/sources/{sid}", headers=owner
                )
                restarted = await client.post(
                    f"/api/v1/workspaces/{ws}/drive/connect", headers=owner
                )
                state3 = parse_qs(
                    urlsplit(restarted.json()["authorization_url"]).query
                )["state"][0]
                redone = await client.get(
                    f"/auth/google-drive/callback?state={state3}&code=c0de",
                    follow_redirects=False,
                )
                assert redone.status_code == 302, redone.text
                assert fetch_one(
                    world["stream"],
                    "SELECT state, config->>'removed' FROM media_sources WHERE id = %s",
                    (sid,),
                ) == ("paused", "true")
                # …and a pick clears the marker with the re-arm.
                repicked = await client.post(
                    f"/api/v1/workspaces/{ws}/sources",
                    json={"folder_ref": "folder-abc", "folder_name": "Stories"},
                    headers=owner,
                )
                assert repicked.status_code == 200, repicked.text
                assert fetch_one(
                    world["stream"],
                    "SELECT state, config->>'removed' FROM media_sources WHERE id = %s",
                    (sid,),
                ) == ("active", None)

        _run(main())

    def test_a_sign_in_state_cannot_be_replayed_into_the_drive_callback(
        self, world, google_configured
    ):
        """Refused by name at the Drive callback — and BURNED there: the
        sign-in callback the state was minted for cannot use it afterwards
        either, nonce cookie and all. One-shot means one presentation."""

        async def main():
            async with api_client(world["ingress"]) as (client, _):
                start = await client.get("/auth/google", follow_redirects=False)
                state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
                nonce = cookie_value(start, auth_routes.NONCE_COOKIE)
                done = await client.get(
                    f"/auth/google-drive/callback?state={state}&code=c0de",
                    follow_redirects=False,
                )
                assert done.status_code == 302
                assert done.headers["location"] == _error_page("state_refused")
                back = await client.get(
                    f"/auth/google/callback?state={state}&code=c0de",
                    headers={"Cookie": f"{auth_routes.NONCE_COOKIE}={nonce}"},
                    follow_redirects=False,
                )
                assert back.status_code == 302
                assert (
                    back.headers["location"]
                    == f"{FRONT}/auth/error?reason=state_refused"
                )

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
