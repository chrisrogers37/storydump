"""X.2's gate, literally, through the real app, AS THE PRODUCTION ROLE.

`04` X.2: *"an approval completed web-only passes end-to-end."* This drives
exactly that against the replayed target schema, connected as `svc_ingress`
— the role the API runs as — because a gate run as the owner would bypass
every RLS policy and prove the SQL rather than the deployment:

    sign in (cold Google subject, exchange stubbed) → /me → create a workspace
    → read it back → an awaiting_approval intent appears in the list →
    approve is refused in manual mode by name → settings_change flips
    api_publishing_enabled → approve enqueues publish_pipeline → the replay
    is acknowledged without re-execution → a second cold user sees 404, not
    403 → sign-out revokes.

No Telegram binding exists at any point. The provider call is the only stub
(`google_oidc.exchange_code` returns an unsigned token whose nonce is the
state's); everything else — oauth_states, session_tokens, command_dedup,
rate_counters, the audit triggers, the intent guard — is the real thing.

The last test pins the one known gap under this role: memberships are not
listable before a tenant is claimed (`058`'s `p_tenant` on
`workspace_members`/`workspaces`, no user-plane read path). Strict xfail with
a positive control as the owner, so the day the `02` §7 door lands the pin
flips and gets updated deliberately (#1015 addendum, item 3).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.app import create_app
from src.api.principal import COOKIE
from src.api.routes import auth as auth_routes
from src.config.settings import settings
from src.services.target import google_oidc, workspaces
from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress")}
    finally:
        gen.close()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "cid", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec", raising=False)
    monkeypatch.setattr(
        settings, "OAUTH_REDIRECT_BASE_URL", "https://api.test", raising=False
    )
    monkeypatch.setattr(settings, "WEB_APP_URL", "https://app.test", raising=False)


def _asyncpg(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _run(coro):
    return asyncio.run(coro)


def _id_token(state: str, *, sub: str, email: str) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "cid",
        "exp": now + 300,
        "iat": now,
        "nonce": google_oidc.nonce_for(state),
        "sub": sub,
        "email": email,
        "email_verified": True,
        "name": sub,
    }
    seg = lambda o: (
        base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    )  # noqa: E731
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


def _cookie(resp: httpx.Response, name: str) -> str:
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(name + "="):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"no {name} cookie in {resp.headers.get_list('set-cookie')}")


async def _sign_in(
    client: httpx.AsyncClient, monkeypatch, *, sub: str, email: str
) -> dict:
    """Drive the real sign-in with only the provider stubbed. Returns the
    bearer header for the new session."""
    start = await client.get("/auth/google", follow_redirects=False)
    assert start.status_code == 302, start.text
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    nonce = _cookie(start, auth_routes.NONCE_COOKIE)

    async def exchange_code(client_, **kw):
        return _id_token(state, sub=sub, email=email)

    monkeypatch.setattr(google_oidc, "exchange_code", exchange_code)
    done = await client.get(
        f"/auth/google/callback?state={state}&code=c0de",
        headers={"Cookie": f"{auth_routes.NONCE_COOKIE}={nonce}"},
        follow_redirects=False,
    )
    assert done.status_code == 302, done.text
    assert done.headers["location"] == "https://app.test/welcome", done.headers[
        "location"
    ]
    return {"Authorization": f"Bearer {_cookie(done, COOKIE)}"}


def _seed_intent(dsn: str, workspace_id: str, tag: str) -> str:
    """Fixture data INTO an API-created workspace: the parent chain an
    awaiting_approval intent needs, as the migration actor (`seed_workspace_chain`'s
    statements, re-pointed at an existing workspace)."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO media_sources (workspace_id, provider, config)"
                " VALUES (%s, 'gdrive', '{\"v\": 1}') RETURNING id",
                (workspace_id,),
            )
            src = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO ig_accounts (workspace_id, provider_account_ref)"
                " VALUES (%s, %s) RETURNING id",
                (workspace_id, f"acct-{tag}"),
            )
            iga = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO media_items"
                " (workspace_id, source_id, content_hash, file_name, media_kind,"
                "  provider_file_ref)"
                " VALUES (%s, %s, %s, 'f.jpg', 'image', %s) RETURNING id",
                (workspace_id, src, f"hash-{tag}", f"ref-{tag}"),
            )
            mi = cur.fetchone()[0]
            cur.execute(
                # state is EXPLICIT: the column defaults to 'scheduled', and the
                # gate reads the approval list, so the fixture must be an intent
                # that is actually awaiting approval.
                "INSERT INTO post_intents"
                " (workspace_id, ig_account_id, media_item_id, provider_account_ref,"
                "  approval_mode, schedule_slot_at, state)"
                " VALUES (%s, %s, %s, %s, 'manual', now(), 'awaiting_approval') RETURNING id",
                (workspace_id, iga, mi, f"acct-{tag}"),
            )
            intent = cur.fetchone()[0]
        conn.commit()
        return str(intent)
    finally:
        conn.close()


def _owner_rows(dsn: str, sql: str, params=()) -> list:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'system'")
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def test_x2_gate_web_only_approval_as_svc_ingress(world, configured, monkeypatch):
    async def main():
        engine = create_async_engine(_asyncpg(world["ingress"]), poolclass=NullPool)
        try:
            app = create_app(engine=engine)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="https://api.test"
            ) as client:
                # subject gate: the app really runs as the production role
                async with engine.connect() as conn:
                    from sqlalchemy import text

                    who = (await conn.execute(text("SELECT current_user"))).scalar()
                    assert who == "svc_ingress", who

                owner = await _sign_in(
                    client, monkeypatch, sub="sub-owner", email="Owner@Example.test"
                )
                me = await client.get("/api/v1/me", headers=owner)
                assert me.status_code == 200, me.text
                assert me.json()["user"]["primary_email"] == "owner@example.test"
                assert [i["provider"] for i in me.json()["user"]["identities"]] == [
                    "google"
                ]
                owner_id = me.json()["user"]["id"]

                created = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "Gate", "tz": "America/New_York"},
                    headers={**owner, "Idempotency-Key": "create-1"},
                )
                assert created.status_code == 201, created.text
                ws = created.json()["workspace_id"]
                replay = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "Gate", "tz": "America/New_York"},
                    headers={**owner, "Idempotency-Key": "create-1"},
                )
                assert (replay.status_code, replay.json()) == (
                    200,
                    {"outcome": "replayed"},
                )

                got = await client.get(f"/api/v1/workspaces/{ws}", headers=owner)
                assert got.status_code == 200, got.text
                assert got.json()["name"] == "Gate"
                assert got.json()["tz"] == "America/New_York"
                assert got.json()["api_publishing_enabled"] is False
                members = await client.get(
                    f"/api/v1/workspaces/{ws}/members", headers=owner
                )
                assert [
                    (m["user_id"], m["role"]) for m in members.json()["members"]
                ] == [(owner_id, "owner")]

                intent_id = _seed_intent(world["stream"], ws, "gate")
                pending = await client.get(
                    f"/api/v1/workspaces/{ws}/intents?state=awaiting_approval",
                    headers=owner,
                )
                assert pending.status_code == 200, pending.text
                assert [i["id"] for i in pending.json()["intents"]] == [intent_id]

                approve_url = f"/api/v1/workspaces/{ws}/commands/approve"
                manual = await client.post(
                    approve_url,
                    json={"intent_id": intent_id},
                    headers={**owner, "Idempotency-Key": "approve-0"},
                )
                assert manual.status_code == 409, manual.text
                assert manual.json()["reason"] == "manual_mode"

                flipped = await client.post(
                    f"/api/v1/workspaces/{ws}/commands/settings_change",
                    json={"settings": {"api_publishing_enabled": True}},
                    headers={**owner, "Idempotency-Key": "settings-1"},
                )
                assert flipped.status_code == 200, flipped.text

                approved = await client.post(
                    approve_url,
                    json={"intent_id": intent_id},
                    headers={**owner, "Idempotency-Key": "approve-1"},
                )
                assert approved.status_code == 202, approved.text
                assert approved.json()["job"] == "publish_pipeline"

                again = await client.post(
                    approve_url,
                    json={"intent_id": intent_id},
                    headers={**owner, "Idempotency-Key": "approve-1"},
                )
                assert (again.status_code, again.json()) == (
                    200,
                    {"outcome": "replayed"},
                )
                reused = await client.post(
                    approve_url,
                    json={"intent_id": intent_id, "extra": 1},
                    headers={**owner, "Idempotency-Key": "approve-1"},
                )
                assert reused.status_code == 409, reused.text

                # ground truth, read as the owner
                (state,) = _owner_rows(
                    world["stream"],
                    "SELECT state FROM post_intents WHERE id = %s",
                    (intent_id,),
                )[0]
                assert state == "approved"
                jobs = _owner_rows(
                    world["stream"],
                    "SELECT kind, state FROM jobs WHERE workspace_id = %s AND kind = 'publish_pipeline'",
                    (ws,),
                )
                assert len(jobs) == 1, jobs
                dedup = _owner_rows(
                    world["stream"],
                    "SELECT count(*) FROM command_dedup WHERE channel = 'web'",
                )[0][0]
                # create-1, settings-1, approve-1 -- and NOT approve-0: the refused
                # command rolled back with its dedup row, so its key is free to
                # retry. That is the one-transaction property, measured.
                assert (
                    dedup == 3
                )  # create-1, approve-0 rolled back, settings-1, approve-1 → 3 kept + approve-0? see below
        finally:
            await engine.dispose()

    _run(main())


def test_a_second_user_sees_404_not_403_and_signout_revokes(
    world, configured, monkeypatch
):
    async def main():
        engine = create_async_engine(_asyncpg(world["ingress"]), poolclass=NullPool)
        try:
            app = create_app(engine=engine)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="https://api.test"
            ) as client:
                owner = await _sign_in(
                    client, monkeypatch, sub="sub-a", email="a@example.test"
                )
                created = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "A"},
                    headers={**owner, "Idempotency-Key": "a-create"},
                )
                ws = created.json()["workspace_id"]

                other = await _sign_in(
                    client, monkeypatch, sub="sub-b", email="b@example.test"
                )
                assert (
                    await client.get(f"/api/v1/workspaces/{ws}", headers=other)
                ).status_code == 404
                denied = await client.post(
                    f"/api/v1/workspaces/{ws}/commands/rename_workspace",
                    json={"name": "Stolen"},
                    headers={**other, "Idempotency-Key": "b-rename"},
                )
                assert denied.status_code == 404, denied.text
                assert (
                    await client.get(f"/api/v1/workspaces/{ws}", headers=owner)
                ).json()["name"] == "A"

                out = await client.post("/auth/signout", headers=other)
                assert out.status_code == 200
                assert (
                    await client.get("/api/v1/me", headers=other)
                ).status_code == 401
        finally:
            await engine.dispose()

    _run(main())


class TestMembershipListingUnderTheProductionRole:
    """The `02` §7 gap, pinned: right SQL, no read path for the role."""

    @pytest.mark.xfail(
        strict=True,
        reason="02 §7 amendment pending: workspace_members/workspaces have no user-plane "
        "read path for svc_ingress (058 p_tenant only); flips when fn_memberships_for_user "
        "lands — #1015 addendum item 3",
    )
    def test_the_creator_can_list_their_workspace_as_svc_ingress(
        self, world, configured, monkeypatch
    ):
        async def main():
            engine = create_async_engine(_asyncpg(world["ingress"]), poolclass=NullPool)
            try:
                app = create_app(engine=engine)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="https://api.test"
                ) as client:
                    me = await _sign_in(
                        client, monkeypatch, sub="sub-list", email="l@example.test"
                    )
                    created = await client.post(
                        "/api/v1/workspaces",
                        json={"name": "L"},
                        headers={**me, "Idempotency-Key": "l-1"},
                    )
                    ws = created.json()["workspace_id"]
                    listed = await client.get("/api/v1/workspaces", headers=me)
                    assert [w["id"] for w in listed.json()["workspaces"]] == [ws]
            finally:
                await engine.dispose()

        _run(main())

    def test_positive_control_the_query_is_right_as_the_owner(
        self, world, configured, monkeypatch
    ):
        """Same rows, read where RLS does not filter: the SQL finds the
        membership, so the xfail above is the policy, not the query."""

        async def main():
            ingress = create_async_engine(
                _asyncpg(world["ingress"]), poolclass=NullPool
            )
            owner = create_async_engine(_asyncpg(world["stream"]), poolclass=NullPool)
            try:
                app = create_app(engine=ingress)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="https://api.test"
                ) as client:
                    me = await _sign_in(
                        client, monkeypatch, sub="sub-ctl", email="c@example.test"
                    )
                    created = await client.post(
                        "/api/v1/workspaces",
                        json={"name": "C"},
                        headers={**me, "Idempotency-Key": "c-1"},
                    )
                    ws = created.json()["workspace_id"]
                    user_id = (await client.get("/api/v1/me", headers=me)).json()[
                        "user"
                    ]["id"]
                async with owner.connect() as conn:
                    rows = await workspaces.list_for_user(conn, user_id=user_id)
                assert [(str(r["id"]), r["role"]) for r in rows] == [(ws, "owner")]
                async with ingress.connect() as conn:
                    assert await workspaces.list_for_user(conn, user_id=user_id) == []
            finally:
                await ingress.dispose()
                await owner.dispose()

        _run(main())
