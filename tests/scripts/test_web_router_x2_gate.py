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

The last test measures the tenth door (`064`, #1037) under this role: the
membership list — a cross-tenant question `p_tenant` cannot serve — answers
through `fn_memberships_for_caller()`, is the caller's alone, and reads
empty when no caller is claimed.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.api.app import create_app
from src.api.principal import COOKIE
from src.api.routes import auth as auth_routes
from src.services.target import google_oidc
from src.services.target.unit_of_work import asyncpg_url
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    replay_advertised_stream,
    seed_intent_chain,
    set_test_passwords,
)
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import API, FRONT, cookie_value, unsigned_id_token

#: The configured sign-in world, registered here as a fixture by assignment.
google_configured = api_conftest.google_configured

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


def _run(coro):
    return asyncio.run(coro)


@asynccontextmanager
async def _api(dsn: str):
    """The real app over a fresh NullPool engine on *dsn*, as an ASGI client;
    the engine is disposed with the client. Yields (client, engine)."""
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
    """Drive the real sign-in with only the provider stubbed. Returns the
    bearer header for the new session."""
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
    assert done.headers["location"] == f"{FRONT}/welcome", done.headers["location"]
    return {"Authorization": f"Bearer {cookie_value(done, COOKIE)}"}


def _seed_intent(dsn: str, workspace_id: str, tag: str) -> str:
    """Fixture data INTO an API-created workspace, as the migration actor —
    the suite's one spelling of the chain, with the state the gate reads."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            chain = seed_intent_chain(cur, workspace_id, tag, state="awaiting_approval")
            # the account column the queue renders (`06` §3): a handle to read back
            cur.execute(
                "UPDATE ig_accounts SET handle = %s WHERE id = %s",
                (f"@{tag}_acct", str(chain["iga"])),
            )
        conn.commit()
        return str(chain["intent"])
    finally:
        conn.close()


def test_x2_gate_web_only_approval_as_svc_ingress(
    world, google_configured, monkeypatch
):
    async def main():
        async with _api(world["ingress"]) as (client, engine):
            # subject gate: the app really runs as the production role
            async with engine.connect() as conn:
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
            assert (replay.status_code, replay.json()) == (200, {"outcome": "replayed"})

            got = await client.get(f"/api/v1/workspaces/{ws}", headers=owner)
            assert got.status_code == 200, got.text
            assert got.json()["name"] == "Gate"
            assert got.json()["tz"] == "America/New_York"
            assert got.json()["api_publishing_enabled"] is False
            # #1033 landed the approvals surface and reverted the INTERIM
            # 'auto' with it: a new workspace carries the column's own default.
            assert got.json()["approval_mode"] == "manual"
            members = await client.get(
                f"/api/v1/workspaces/{ws}/members", headers=owner
            )
            assert [(m["user_id"], m["role"]) for m in members.json()["members"]] == [
                (owner_id, "owner")
            ]

            intent_id = _seed_intent(world["stream"], ws, "gate")
            pending = await client.get(
                f"/api/v1/workspaces/{ws}/intents?state=awaiting_approval",
                headers=owner,
            )
            assert pending.status_code == 200, pending.text
            assert [i["id"] for i in pending.json()["intents"]] == [intent_id]
            # the queue's account column rides the intent row (#1033)
            assert pending.json()["intents"][0]["account_handle"] == "@gate_acct"

            approve_url = f"/api/v1/workspaces/{ws}/commands/approve"
            manual = await client.post(
                approve_url,
                json={"intent_id": intent_id},
                headers={**owner, "Idempotency-Key": "approve-0"},
            )
            assert manual.status_code == 409, manual.text
            assert manual.json()["reason"] == "manual_mode"

            # Manual mode's own tap, through the real route (#1033): a second
            # intent is marked posted by hand and terminalizes with no job.
            by_hand = _seed_intent(world["stream"], ws, "byhand")
            marked = await client.post(
                f"/api/v1/workspaces/{ws}/commands/mark_posted",
                json={"intent_id": by_hand},
                headers={**owner, "Idempotency-Key": f"mark_posted:{by_hand}"},
            )
            assert marked.status_code == 200, marked.text
            assert marked.json() == {
                "outcome": "executed",
                "intent_id": by_hand,
                "state": "posted",
                "published_via": "manual",
            }
            assert fetch_one(
                world["stream"],
                "SELECT state, published_via FROM post_intents WHERE id = %s",
                (by_hand,),
            ) == ("posted", "manual")

            bad = await client.post(
                f"/api/v1/workspaces/{ws}/commands/settings_change",
                json={"settings": {"tz": "Mars/Olympus_Mons"}},
                headers={**owner, "Idempotency-Key": "settings-0"},
            )
            assert bad.status_code == 400, (
                bad.text
            )  # the CHECK refused it, as invalid_args
            assert bad.json()["reason"] == "invalid_args"

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
            assert (again.status_code, again.json()) == (200, {"outcome": "replayed"})
            reused = await client.post(
                approve_url,
                json={"intent_id": intent_id, "extra": 1},
                headers={**owner, "Idempotency-Key": "approve-1"},
            )
            assert reused.status_code == 409, reused.text

            # ground truth, read as the owner
            assert fetch_one(
                world["stream"],
                "SELECT state FROM post_intents WHERE id = %s",
                (intent_id,),
            ) == ("approved",)
            assert fetch_one(
                world["stream"],
                "SELECT count(*) FROM jobs WHERE workspace_id = %s AND kind = 'publish_pipeline'",
                (ws,),
            ) == (1,)
            # create-1, settings-1, approve-1, mark_posted:<by_hand> — and
            # NEITHER approve-0 NOR settings-0: a refused command rolled back
            # with its dedup row, so its key is free to retry. That is the
            # one-transaction property, measured.
            assert fetch_one(
                world["stream"],
                "SELECT count(*) FROM command_dedup WHERE channel = 'web'",
            ) == (4,)

    _run(main())


def test_a_second_user_sees_404_not_403_and_signout_revokes(
    world, google_configured, monkeypatch
):
    async def main():
        async with _api(world["ingress"]) as (client, _):
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
            assert (await client.get(f"/api/v1/workspaces/{ws}", headers=owner)).json()[
                "name"
            ] == "A"

            out = await client.post("/auth/signout", headers=other)
            assert out.status_code == 200
            assert (await client.get("/api/v1/me", headers=other)).status_code == 401

    _run(main())


class TestMembershipListingUnderTheProductionRole:
    """The tenth door, measured as `svc_ingress`: the list answers, it is the
    CALLER's list and nobody else's, and a caller that claimed nothing reads
    nothing — the failure direction is closed, not open."""

    def test_the_creator_lists_their_workspace_and_only_theirs(
        self, world, google_configured, monkeypatch
    ):
        async def main():
            async with _api(world["ingress"]) as (client, ingress):
                a = await _sign_in(
                    client, monkeypatch, sub="sub-list-a", email="la@example.test"
                )
                b = await _sign_in(
                    client, monkeypatch, sub="sub-list-b", email="lb@example.test"
                )
                made_a = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "A"},
                    headers={**a, "Idempotency-Key": "la-1"},
                )
                made_b = await client.post(
                    "/api/v1/workspaces",
                    json={"name": "B"},
                    headers={**b, "Idempotency-Key": "lb-1"},
                )
                ws_a, ws_b = (
                    made_a.json()["workspace_id"],
                    made_b.json()["workspace_id"],
                )

                listed = await client.get("/api/v1/workspaces", headers=a)
                assert listed.status_code == 200, listed.text
                assert [(w["id"], w["role"]) for w in listed.json()["workspaces"]] == [
                    (ws_a, "owner")
                ]
                whoami = await client.get("/api/v1/me", headers=a)
                assert [w["id"] for w in whoami.json()["workspaces"]] == [ws_a]
                assert [
                    w["id"]
                    for w in (await client.get("/api/v1/workspaces", headers=b)).json()[
                        "workspaces"
                    ]
                ] == [ws_b]

                # The door with no caller claimed: nothing, never everything.
                async with ingress.connect() as conn:
                    who = (await conn.execute(text("SELECT current_user"))).scalar()
                    assert who == "svc_ingress"
                    unclaimed = (
                        await conn.execute(
                            text("SELECT count(*) FROM fn_memberships_for_caller()")
                        )
                    ).scalar()
                    assert unclaimed == 0

        _run(main())
