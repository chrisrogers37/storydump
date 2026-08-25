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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.app import create_app
from src.api.principal import COOKIE
from src.api.routes import auth as auth_routes
from src.services.target import drive_credentials, google_oidc
from src.services.target import google_drive_oauth as drive
from src.services.target.ig_login_oauth import (
    OAuthStateRefused,
    consume_state,
    issue_state,
)
from src.services.target.media_sync import DriveCredentialDead
from src.services.target.unit_of_work import asyncpg_url, unit_of_work
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import API, FRONT, cookie_value, unsigned_id_token

google_configured = api_conftest.google_configured

pytestmark = [pytest.mark.integration, pytest.mark.slow]

NOW = datetime(2026, 8, 25, 20, 0, tzinfo=timezone.utc)


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


def _grant(**over) -> drive.DriveGrant:
    kw = dict(
        access_token="ya29.access",
        refresh_token="1//refresh",
        expires_at=NOW + timedelta(hours=1),
        scope=drive.SCOPE,
    )
    kw.update(over)
    return drive.DriveGrant(**kw)


async def _in_tenant(dsn: str, ws, user, fn):
    """One committed unit of work as `svc_ingress`, the role ASSERTED."""
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        uow = unit_of_work(
            engine, str(ws), actor_kind="user", actor_user_id=str(user), channel="web"
        )
        async with uow.begin() as session:
            who = (await session.execute(text("SELECT current_user"))).scalar()
            assert who == "svc_ingress", who
            return await fn(session)
    finally:
        await engine.dispose()


async def _token(dsn: str, source_id, workspace_id) -> str:
    """The read door, on an ingress engine — the worker's own shape."""
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        return await drive_credentials.token_for_source(
            engine, str(source_id), workspace_id=str(workspace_id)
        )
    finally:
        await engine.dispose()


def _credential_rows(world, source_id):
    conn = psycopg2.connect(world["stream"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, provider, media_source_id, ig_account_id, next_refresh_at,"
                "       state, expires_at, encrypted_payload"
                "  FROM oauth_credentials WHERE media_source_id = %s",
                (str(source_id),),
            )
            return cur.fetchall()
    finally:
        conn.close()


@asynccontextmanager
async def _api(dsn: str):
    engine = create_async_engine(asyncpg_url(dsn), poolclass=NullPool)
    try:
        app = create_app(engine=engine)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=API) as client:
            yield client, engine
    finally:
        await engine.dispose()


async def _sign_in(
    client: httpx.AsyncClient, monkeypatch, *, sub: str, email: str
) -> dict:
    start = await client.get("/auth/google", follow_redirects=False)
    assert start.status_code == 302, start.text
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    nonce = cookie_value(start, auth_routes.NONCE_COOKIE)

    async def exchange_code(client_, **kw):
        return unsigned_id_token(state, sub=sub, email=email, name=sub)

    monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
    done = await client.get(
        f"/auth/google/callback?state={state}&code=c0de",
        headers={"Cookie": f"{auth_routes.NONCE_COOKIE}={nonce}"},
        follow_redirects=False,
    )
    assert done.status_code == 302, done.text
    return {"Authorization": f"Bearer {cookie_value(done, COOKIE)}"}


# --- the credential row ------------------------------------------------------


class TestTheCredentialRow:
    def test_store_writes_a_source_owned_gdrive_row_and_the_read_door_answers(
        self, world
    ):
        a = world["a"]
        cid = _run(
            _in_tenant(
                world["ingress"],
                a["ws"],
                a["user"],
                lambda s: drive.store_credential(
                    s, workspace_id=a["ws"], media_source_id=a["src"], grant=_grant()
                ),
            )
        )
        rows = _credential_rows(world, a["src"])
        assert len(rows) == 1
        (rid, provider, src, acct, next_refresh, state, expires_at, payload) = rows[0]
        assert str(rid) == cid
        # THE invariant a copy of ig_login's writer breaks and the schema
        # cannot catch: provider AND both owner columns, explicitly.
        assert provider == "gdrive"
        assert str(src) == str(a["src"])
        assert acct is None
        # F3 (b): the 063 fence stays closed, so this must be NULL.
        assert next_refresh is None
        assert state == "active"
        assert expires_at is not None
        # ciphertext only — neither token, and no envelope field name, in the clear
        for secret in ("ya29.access", "1//refresh", "refresh_token"):
            assert secret not in payload
        # #1054's read door, which shipped against nothing, answers for the
        # first time — with the ACCESS token, straight into a bearer header.
        assert _run(_token(world["ingress"], a["src"], a["ws"])) == "ya29.access"

    def test_a_reconnect_replaces_the_row_in_place(self, world):
        a = world["a"]
        before = _credential_rows(world, a["src"])
        assert len(before) == 1
        cid = _run(
            _in_tenant(
                world["ingress"],
                a["ws"],
                a["user"],
                lambda s: drive.store_credential(
                    s,
                    workspace_id=a["ws"],
                    media_source_id=a["src"],
                    grant=_grant(access_token="ya29.second"),
                ),
            )
        )
        after = _credential_rows(world, a["src"])
        assert [str(r[0]) for r in after] == [cid] == [str(before[0][0])], (
            "same row id — no gap, no second row"
        )
        assert after[0][7] != before[0][7], "the payload moved"
        assert _run(_token(world["ingress"], a["src"], a["ws"])) == "ya29.second"

    def test_the_credential_is_bound_to_its_workspace(self, world):
        a, b = world["a"], world["b"]
        # Writing A's source under tenant B: the composite FK
        # (workspace_id, media_source_id) → media_sources (workspace_id, id)
        # makes a cross-workspace credential impossible by construction, and
        # `p_tenant` would hide the source anyway.
        with pytest.raises(DBAPIError):
            _run(
                _in_tenant(
                    world["ingress"],
                    b["ws"],
                    b["user"],
                    lambda s: drive.store_credential(
                        s,
                        workspace_id=b["ws"],
                        media_source_id=a["src"],
                        grant=_grant(),
                    ),
                )
            )
        assert len(_credential_rows(world, a["src"])) == 1
        # Reading A's credential from tenant B: nothing, refused by name —
        # never B reading A's token.
        with pytest.raises(DriveCredentialDead):
            _run(_token(world["ingress"], a["src"], b["ws"]))

    def test_an_expired_access_token_is_refused_until_the_read_path_mints(self, world):
        b = world["b"]
        _run(
            _in_tenant(
                world["ingress"],
                b["ws"],
                b["user"],
                lambda s: drive.store_credential(
                    s,
                    workspace_id=b["ws"],
                    media_source_id=b["src"],
                    grant=_grant(expires_at=NOW - timedelta(days=1)),
                ),
            )
        )
        with pytest.raises(DriveCredentialDead, match="expired"):
            _run(_token(world["ingress"], b["src"], b["ws"]))


# --- the state row -----------------------------------------------------------


class TestTheStateRow:
    def test_a_connect_state_pins_user_workspace_and_source(self, world):
        a = world["a"]

        async def issue(s):
            return await issue_state(
                s,
                purpose="connect",
                provider=drive.PROVIDER,
                user_id=a["user"],
                workspace_id=a["ws"],
                reconnect_target=a["src"],
            )

        state = _run(_in_tenant(world["ingress"], a["ws"], a["user"], issue))
        row = _run(
            _in_tenant(
                world["ingress"],
                a["ws"],
                a["user"],
                lambda s: consume_state(s, state=state),
            )
        )
        assert row["provider"] == "gdrive"
        assert row["purpose"] == "connect"
        assert str(row["workspace_id"]) == str(a["ws"])
        assert str(row["reconnect_target"]) == str(a["src"])

    def test_an_unknown_state_is_refused_by_name(self, world):
        a = world["a"]
        with pytest.raises(OAuthStateRefused, match="unknown state"):
            _run(
                _in_tenant(
                    world["ingress"],
                    a["ws"],
                    a["user"],
                    lambda s: consume_state(s, state="never-issued"),
                )
            )


# --- the route pair, through the real app --------------------------------------


class TestTheRoutePairAsSvcIngress:
    def test_connect_then_callback_writes_the_credential(
        self, world, google_configured, monkeypatch
    ):
        async def main():
            async with _api(world["ingress"]) as (client, ingress):
                owner = await _sign_in(
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
                    return _grant()

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
                rows = _credential_rows(world, sid)
                assert len(rows) == 1
                assert (
                    rows[0][1],
                    str(rows[0][2]),
                    rows[0][3],
                    rows[0][4],
                    rows[0][5],
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
                assert (
                    again.headers["location"]
                    == f"{FRONT}/auth/error?reason=state_refused"
                )
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
                assert [str(r[0]) for r in after] == [str(rows[0][0])]

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
            async with _api(world["ingress"]) as (client, _):
                start = await client.get("/auth/google", follow_redirects=False)
                state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
                done = await client.get(
                    f"/auth/google-drive/callback?state={state}&code=c0de",
                    follow_redirects=False,
                )
                assert done.status_code == 302
                assert (
                    done.headers["location"]
                    == f"{FRONT}/auth/error?reason=state_refused"
                )

        _run(main())

    def test_a_declined_consent_lands_on_the_error_page_and_writes_nothing(
        self, world, google_configured
    ):
        async def main():
            async with _api(world["ingress"]) as (client, _):
                done = await client.get(
                    "/auth/google-drive/callback?error=access_denied&state=x",
                    follow_redirects=False,
                )
                assert done.status_code == 302
                assert done.headers["location"] == f"{FRONT}/auth/error?reason=denied"

        _run(main())
