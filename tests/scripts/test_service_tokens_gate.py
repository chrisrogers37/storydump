"""Phase 01 of the v2 CLI, through the real app AS THE PRODUCTION ROLE.

A token is the second credential (`07` §1, §6, §23): minted only from a
signed-in session, stored as its hash, admitted to an allowlist of routes and
nowhere else, acting as its person on the command route over the ``cli``
channel with one direct `cli_command` row beside the port's own — or, as a
workspace service identity, reading its one workspace and never writing.
Every assertion below runs against the replayed target schema connected as
``svc_ingress``, because a gate run as the owner would bypass RLS and prove
the SQL rather than the deployment.

    sign in → mint a person-bound token → the token sees itself → a session-
    only route refuses it by name → the token skips a story as the person
    (audit: channel cli, the cli_command row paired with the transition) →
    the replay writes nothing → a readonly token cannot write → a service
    identity reads its workspace and only its workspace → revoked, expired
    and disabled-person tokens die at the door → a stranger sees 404.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import psycopg2
import pytest
from sqlalchemy import text

from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_intent_chain,
    set_test_passwords,
)
from tests.src.api import conftest as api_conftest
from tests.src.api.conftest import api_client, sign_in

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


def _seed_story(dsn: str, workspace_id: str, tag: str) -> str:
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            chain = seed_intent_chain(cur, workspace_id, tag, state="awaiting_approval")
        conn.commit()
        return str(chain["intent"])
    finally:
        conn.close()


def _sql(dsn: str, sql: str, params=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


def test_tokens_end_to_end_as_svc_ingress(world, google_configured, monkeypatch):
    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            async with engine.connect() as conn:
                who = (await conn.execute(text("SELECT current_user"))).scalar()
                assert who == "svc_ingress", who

            owner = await sign_in(
                client, monkeypatch, sub="sub-owner", email="owner@example.test"
            )
            me = await client.get("/api/v1/me", headers=owner)
            owner_id = me.json()["user"]["id"]
            created = await client.post(
                "/api/v1/workspaces",
                json={"name": "Tokens", "tz": "America/New_York"},
                headers={**owner, "Idempotency-Key": "create-tokens"},
            )
            assert created.status_code == 201, created.text
            ws = created.json()["workspace_id"]
            story = _seed_story(world["stream"], ws, "tok")

            # --- mint: the secret once, the hash in the row ---------------
            minted = await client.post(
                "/api/v1/me/tokens",
                json={"name": "claude-code", "role": "operator"},
                headers=owner,
            )
            assert minted.status_code == 201, minted.text
            token = minted.json()
            secret = token["secret"]
            assert secret.startswith("sdt_") and len(secret) == 47
            ((stored_hash, stored_user, stored_ws, role),) = _sql(
                world["stream"],
                "SELECT token_hash, user_id::text, workspace_id::text, role"
                " FROM service_tokens WHERE id = %s",
                (token["id"],),
            )
            assert stored_hash == hashlib.sha256(secret.encode()).hexdigest()
            assert (stored_user, stored_ws, role) == (owner_id, None, "operator")
            listed = await client.get("/api/v1/me/tokens", headers=owner)
            assert [t["id"] for t in listed.json()["tokens"]] == [token["id"]]
            assert "token_hash" not in listed.json()["tokens"][0]
            assert "secret" not in listed.json()["tokens"][0]

            bad = await client.post(
                "/api/v1/me/tokens",
                json={"name": "", "role": "operator"},
                headers=owner,
            )
            assert (bad.status_code, bad.json()["reason"]) == (400, "invalid_args")

            # --- the token sees itself; session-only routes refuse it -------
            whoami = await client.get("/api/v1/me/principal", headers=_bearer(secret))
            assert whoami.status_code == 200, whoami.text
            assert whoami.json()["kind"] == "token"
            assert whoami.json()["user_id"] == owner_id
            assert whoami.json()["token"]["name"] == "claude-code"
            assert whoami.json()["workspaces"] == [
                {"id": ws, "name": "Tokens", "role": "owner"}
            ]
            ((stamped,),) = _sql(
                world["stream"],
                "SELECT last_used_at IS NOT NULL FROM service_tokens WHERE id = %s",
                (token["id"],),
            )
            assert stamped, "every use stamps last_used_at"
            for method, path in [
                ("GET", "/api/v1/me"),
                ("POST", "/api/v1/me/tokens"),
                ("GET", f"/api/v1/workspaces/{ws}/intents"),
                ("POST", f"/api/v1/workspaces/{ws}/tokens"),
                ("POST", "/api/v1/invitations/nope/accept"),
            ]:
                resp = await client.request(
                    method, path, headers=_bearer(secret), json={}
                )
                assert resp.status_code == 403, (method, path, resp.text)
                assert resp.json()["reason"] == "session_required", (method, path)

            # --- the person-bound token skips a story as the person ---------
            key = {"Idempotency-Key": f"skip:{story}"}
            skipped = await client.post(
                f"/api/v1/workspaces/{ws}/commands/skip",
                json={"intent_id": story},
                headers={**_bearer(secret), **key},
            )
            assert skipped.status_code == 200, skipped.text
            assert skipped.json()["state"] == "skipped"
            rows = _sql(
                world["stream"],
                "SELECT entity_kind, from_state, to_state, actor_kind,"
                "       actor_user_id::text, channel, detail, created_at"
                "  FROM audit_events WHERE workspace_id = %s AND entity_id = %s"
                " ORDER BY id",
                (ws, story),
            )
            transition = [r for r in rows if r[1] == "awaiting_approval"]
            assert len(transition) == 1, rows
            assert transition[0][2] == "skipped"
            assert (transition[0][3], transition[0][4], transition[0][5]) == (
                "user",
                owner_id,
                "cli",
            )
            cli_rows = [r for r in rows if (r[6] or {}).get("event") == "cli_command"]
            assert len(cli_rows) == 1, rows
            (cli_row,) = cli_rows
            assert cli_row[0] == "post_intent"
            assert (cli_row[3], cli_row[4], cli_row[5]) == ("user", owner_id, "cli")
            assert cli_row[6]["token_id"] == token["id"]
            assert cli_row[6]["token_name"] == "claude-code"
            assert cli_row[6]["external_ref"] == f"skip:{story}"
            assert cli_row[6]["kind"] == "skip"
            assert cli_row[7] == transition[0][7], (
                "both rows share the transaction's now(): that is how they pair"
            )
            ((dedup_principal,),) = _sql(
                world["stream"],
                "SELECT principal FROM command_dedup"
                " WHERE channel = 'cli' AND external_ref = %s",
                (f"skip:{story}",),
            )
            assert dedup_principal == f"token:{token['id']}"

            replay = await client.post(
                f"/api/v1/workspaces/{ws}/commands/skip",
                json={"intent_id": story},
                headers={**_bearer(secret), **key},
            )
            assert (replay.status_code, replay.json()) == (200, {"outcome": "replayed"})
            ((cli_count,),) = _sql(
                world["stream"],
                "SELECT count(*) FROM audit_events"
                " WHERE entity_id = %s AND detail->>'event' = 'cli_command'",
                (story,),
            )
            assert cli_count == 1, "a replay leaves no second cli_command row"

            # --- a readonly person-bound token cannot write -----------------
            ro = await client.post(
                "/api/v1/me/tokens",
                json={"name": "reader", "role": "readonly"},
                headers=owner,
            )
            ro_secret = ro.json()["secret"]
            story2 = _seed_story(world["stream"], ws, "tok2")
            refused = await client.post(
                f"/api/v1/workspaces/{ws}/commands/skip",
                json={"intent_id": story2},
                headers={**_bearer(ro_secret), "Idempotency-Key": f"skip:{story2}"},
            )
            assert (refused.status_code, refused.json()["reason"]) == (
                403,
                "readonly_token",
            )
            ((admitted,),) = _sql(
                world["stream"],
                "SELECT count(*) FROM command_dedup WHERE external_ref = %s",
                (f"skip:{story2}",),
            )
            assert admitted == 0, "refused before admission"

            # --- a service identity reads its workspace, never writes -------
            svc = await client.post(
                f"/api/v1/workspaces/{ws}/tokens",
                json={"name": "ops-bot", "role": "operator", "expires_in_days": 7},
                headers=owner,
            )
            assert svc.status_code == 201, svc.text
            assert svc.json()["role"] == "readonly"
            svc_secret = svc.json()["secret"]
            svc_who = await client.get(
                "/api/v1/me/principal", headers=_bearer(svc_secret)
            )
            assert svc_who.status_code == 200, svc_who.text
            assert svc_who.json()["user_id"] is None
            assert svc_who.json()["workspaces"] == [
                {"id": ws, "name": "Tokens", "role": "readonly"}
            ]
            write = await client.post(
                f"/api/v1/workspaces/{ws}/commands/skip",
                json={"intent_id": story2},
                headers={**_bearer(svc_secret), "Idempotency-Key": f"svc:{story2}"},
            )
            assert (write.status_code, write.json()["reason"]) == (
                403,
                "readonly_token",
            )
            own = await client.get(
                f"/api/v1/workspaces/{ws}/tokens", headers=_bearer(svc_secret)
            )
            assert own.status_code == 200, own.text
            assert [t["name"] for t in own.json()["tokens"]] == ["ops-bot"]
            other = await client.get(
                f"/api/v1/workspaces/{uuid.uuid4()}/tokens",
                headers=_bearer(svc_secret),
            )
            assert (other.status_code, other.json()["reason"]) == (
                403,
                "wrong_workspace",
            )
            not_self = await client.delete(
                f"/api/v1/workspaces/{ws}/tokens/{token['id']}",
                headers=_bearer(svc_secret),
            )
            assert (not_self.status_code, not_self.json()["reason"]) == (
                403,
                "readonly_token",
            )
            personal = await client.get(
                "/api/v1/me/tokens", headers=_bearer(svc_secret)
            )
            assert (personal.status_code, personal.json()["reason"]) == (
                403,
                "session_required",
            )

            # --- a readonly person token reads, and revokes nothing but itself
            ro_who = await client.get(
                "/api/v1/me/principal", headers=_bearer(ro_secret)
            )
            assert (
                ro_who.status_code == 200
                and ro_who.json()["token"]["role"] == "readonly"
            )
            ro_list = await client.get("/api/v1/me/tokens", headers=_bearer(ro_secret))
            assert ro_list.status_code == 200
            assert {t["id"] for t in ro_list.json()["tokens"]} >= {
                token["id"],
                ro.json()["id"],
            }
            for path in (
                f"/api/v1/me/tokens/{token['id']}",
                f"/api/v1/workspaces/{ws}/tokens/{svc.json()['id']}",
            ):
                kill = await client.delete(path, headers=_bearer(ro_secret))
                assert (kill.status_code, kill.json()["reason"]) == (
                    403,
                    "readonly_token",
                ), path
            ((still_live,),) = _sql(
                world["stream"],
                "SELECT count(*) FROM service_tokens WHERE revoked_at IS NULL"
                " AND id IN (%s, %s)",
                (token["id"], svc.json()["id"]),
            )
            assert still_live == 2

            # --- an operator person token revokes the person's tokens, as the person
            spare = await client.post(
                "/api/v1/me/tokens",
                json={"name": "spare", "role": "readonly"},
                headers=owner,
            )
            by_token = await client.delete(
                f"/api/v1/me/tokens/{spare.json()['id']}", headers=_bearer(secret)
            )
            assert by_token.json() == {"revoked": True}

            # --- dead tokens die at the door, without saying why ------------
            revoked = await client.delete(
                f"/api/v1/me/tokens/{token['id']}", headers=owner
            )
            assert revoked.json() == {"revoked": True}
            dead = await client.get("/api/v1/me/principal", headers=_bearer(secret))
            assert (dead.status_code, dead.json()) == (
                401,
                {"detail": "authentication required"},
            )
            _sql(
                world["stream"],
                "UPDATE service_tokens SET expires_at = now() - interval '1 second'"
                " WHERE id = %s",
                (ro.json()["id"],),
            )
            expired = await client.get(
                "/api/v1/me/principal", headers=_bearer(ro_secret)
            )
            assert expired.status_code == 401
            unknown = await client.get(
                "/api/v1/me/principal", headers=_bearer("sdt_" + "x" * 43)
            )
            assert unknown.status_code == 401
            self_revoked = await client.delete(
                f"/api/v1/workspaces/{ws}/tokens/{svc.json()['id']}",
                headers=_bearer(svc_secret),
            )
            assert self_revoked.json() == {"revoked": True}
            gone = await client.get("/api/v1/me/principal", headers=_bearer(svc_secret))
            assert gone.status_code == 401

            # --- a stranger sees 404, and cannot mint for the workspace -----
            stranger = await sign_in(
                client, monkeypatch, sub="sub-stranger", email="s@example.test"
            )
            for method, path in [
                ("GET", f"/api/v1/workspaces/{ws}/tokens"),
                ("POST", f"/api/v1/workspaces/{ws}/tokens"),
            ]:
                resp = await client.request(
                    method, path, headers=stranger, json={"name": "x"}
                )
                assert resp.status_code == 404, (method, resp.text)
            theirs = await client.delete(
                f"/api/v1/me/tokens/{svc.json()['id']}", headers=stranger
            )
            assert theirs.status_code == 404

    _run(main())


def test_a_disabled_person_token_dies_at_the_door(
    world, google_configured, monkeypatch
):
    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            person = await sign_in(
                client, monkeypatch, sub="sub-disabled", email="d@example.test"
            )
            me = await client.get("/api/v1/me", headers=person)
            user_id = me.json()["user"]["id"]
            minted = await client.post(
                "/api/v1/me/tokens",
                json={"name": "laptop", "role": "operator"},
                headers=person,
            )
            secret = minted.json()["secret"]
            alive = await client.get("/api/v1/me/principal", headers=_bearer(secret))
            assert alive.status_code == 200
            _sql(
                world["stream"],
                "UPDATE users SET state = 'disabled' WHERE id = %s;"
                " UPDATE service_tokens SET last_used_at = NULL WHERE id = %s",
                (user_id, minted.json()["id"]),
            )
            dead = await client.get("/api/v1/me/principal", headers=_bearer(secret))
            assert (dead.status_code, dead.json()) == (
                401,
                {"detail": "authentication required"},
            )
            ((stamped,),) = _sql(
                world["stream"],
                "SELECT last_used_at IS NOT NULL FROM service_tokens WHERE id = %s",
                (minted.json()["id"],),
            )
            assert not stamped, "a refused authentication is not a use"

    _run(main())


def test_a_members_token_acts_as_a_member_never_above(
    world, google_configured, monkeypatch
):
    """The ceiling is the membership role, enforced by the port's own gate
    under a token exactly as under a session: a member's operator token skips
    a story (member floor) and is refused a settings change (admin floor)."""

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            owner = await sign_in(
                client, monkeypatch, sub="sub-owner-2", email="o2@example.test"
            )
            created = await client.post(
                "/api/v1/workspaces",
                json={"name": "Ceiling", "tz": "America/New_York"},
                headers={**owner, "Idempotency-Key": "create-ceiling"},
            )
            ws = created.json()["workspace_id"]
            member = await sign_in(
                client, monkeypatch, sub="sub-member", email="m@example.test"
            )
            member_id = (await client.get("/api/v1/me", headers=member)).json()["user"][
                "id"
            ]
            _sql(
                world["stream"],
                "SET app.actor_kind = 'migration';"
                " INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'member')",
                (ws, member_id),
            )
            minted = await client.post(
                "/api/v1/me/tokens",
                json={"name": "member-agent", "role": "operator"},
                headers=member,
            )
            secret = minted.json()["secret"]
            story = _seed_story(world["stream"], ws, "ceil")
            skipped = await client.post(
                f"/api/v1/workspaces/{ws}/commands/skip",
                json={"intent_id": story},
                headers={**_bearer(secret), "Idempotency-Key": f"skip:{story}"},
            )
            assert skipped.status_code == 200, skipped.text
            refused = await client.post(
                f"/api/v1/workspaces/{ws}/commands/settings_change",
                json={"api_publishing_enabled": True},
                headers={**_bearer(secret), "Idempotency-Key": "settings:1"},
            )
            assert refused.status_code == 403, refused.text
            elsewhere = await client.get(
                f"/api/v1/workspaces/{uuid.uuid4()}/tokens", headers=_bearer(secret)
            )
            assert elsewhere.status_code == 404, "not a member reads as not found"

    _run(main())
