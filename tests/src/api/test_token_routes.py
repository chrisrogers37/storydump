"""The token routes and the command route under a token (phase 01).

Minting is session-only (the web's Settings › API tokens panel), a person
lists and revokes their own tokens, an admin the workspace's service
identities, a service identity reads only — and a person-bound token drives
the command route as the person, over the `cli` channel, with one direct
`cli_command` audit row beside the port's own.
"""

from __future__ import annotations

import json

import pytest

from src.api.principal import Principal, current_principal
from src.api.routes import v1
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import commands, service_tokens, webhook_ingress, workspaces
from src.services.target.commands import Command, CommandResult
from tests.src.api.conftest import INTENT, PRINCIPAL, WS
from tests.src.api.test_token_principal import (
    PERSON_TOKEN,
    SERVICE_ID,
    SERVICE_TOKEN,
    TOKEN_ID,
)

KEY = {"Idempotency-Key": "approve:" + INTENT}
OTHER_WS = "55555555-5555-4555-8555-555555555555"
READONLY_PERSON = Principal(
    session_id=None,
    user_id=PRINCIPAL.user_id,
    kind="token",
    channel="cli",
    token_id=TOKEN_ID,
    token_name="readonly-agent",
    token_role="readonly",
    token_workspace_id=None,
)


@pytest.fixture
def as_principal(app):
    def install(principal):
        app.dependency_overrides[current_principal] = lambda: principal
        return principal

    yield install
    app.dependency_overrides.clear()


@pytest.fixture
def tokens(monkeypatch):
    """The token service seam: records every call, answers from `log`."""
    log = {"mint": [], "revoke": [], "list_user": [], "list_ws": []}
    log["revoke_answer"] = True
    log["mint_refuse"] = None

    async def mint(conn, **kw):
        log["mint"].append(kw)
        if log["mint_refuse"] is not None:
            raise log["mint_refuse"]
        return "sdt_" + "s" * 43, {
            "id": TOKEN_ID,
            "name": kw["name"],
            "role": kw["role"],
            "user_id": kw.get("user_id"),
            "workspace_id": kw.get("workspace_id"),
            "expires_at": "2026-12-14T00:00:00+00:00",
            "created_at": "2026-09-15T00:00:00+00:00",
        }

    async def list_for_user(conn, *, user_id):
        log["list_user"].append(user_id)
        return [{"id": TOKEN_ID, "name": "claude-code", "role": "operator"}]

    async def list_for_workspace(conn, *, workspace_id):
        log["list_ws"].append(workspace_id)
        return [{"id": SERVICE_ID, "name": "ops-bot", "role": "readonly"}]

    async def revoke(conn, **kw):
        log["revoke"].append(kw)
        return log["revoke_answer"]

    monkeypatch.setattr(service_tokens, "mint", mint)
    monkeypatch.setattr(service_tokens, "list_for_user", list_for_user)
    monkeypatch.setattr(service_tokens, "list_for_workspace", list_for_workspace)
    monkeypatch.setattr(service_tokens, "revoke", revoke)
    return log


@pytest.fixture
def port(monkeypatch):
    log = {
        "admit": [],
        "execute": [],
        "outcome": CommandResult("executed", {"state": "approved"}),
        "audit": [],
    }

    async def admit(session, *, channel, external_ref, payload, principal=""):
        log["admit"].append((channel, external_ref, payload, principal))
        return {}

    async def execute(session, command, *, tenant_bound=False):
        log["execute"].append(command)
        return log["outcome"]

    async def audit(session, **kw):
        log["audit"].append(kw)

    monkeypatch.setattr(webhook_ingress, "admit", admit)
    monkeypatch.setattr(commands, "execute", execute)
    monkeypatch.setattr(v1, "_audit_cli_command", audit)
    return log


class TestMintingIsSessionOnly:
    def test_a_person_mints_their_own_token(self, client, signed_in, tokens):
        resp = client.post(
            "/api/v1/me/tokens",
            json={"name": "claude-code", "role": "operator", "expires_in_days": 30},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["secret"].startswith("sdt_")
        assert body["id"] == TOKEN_ID and body["role"] == "operator"
        assert set(body) == {"id", "name", "role", "expires_at", "secret"}
        (call,) = tokens["mint"]
        assert call["user_id"] == PRINCIPAL.user_id
        assert call.get("workspace_id") is None
        assert call["expires_in_days"] == 30

    def test_the_expiry_defaults_to_ninety_days(self, client, signed_in, tokens):
        resp = client.post("/api/v1/me/tokens", json={"name": "x", "role": "readonly"})
        assert resp.status_code == 201
        assert tokens["mint"][0]["expires_in_days"] == 90

    def test_a_bad_body_is_400_with_the_reason(self, client, signed_in, tokens):
        tokens["mint_refuse"] = service_tokens.TokenArgsInvalid("name is required")
        resp = client.post("/api/v1/me/tokens", json={"name": "", "role": "operator"})
        assert resp.status_code == 400
        assert resp.json()["reason"] == "invalid_args"

    def test_a_non_object_body_is_400(self, client, signed_in, tokens):
        resp = client.post("/api/v1/me/tokens", json=["nope"])
        assert resp.status_code == 400
        assert tokens["mint"] == []

    @pytest.mark.parametrize("principal", [PERSON_TOKEN, SERVICE_TOKEN])
    def test_a_token_never_mints(self, client, as_principal, tokens, principal):
        as_principal(principal)
        resp = client.post(
            "/api/v1/me/tokens", json={"name": "escalate", "role": "operator"}
        )
        assert resp.status_code == 403
        assert resp.json()["reason"] == "session_required"
        assert tokens["mint"] == []

    def test_an_admin_mints_a_readonly_service_identity(
        self, client, signed_in, tenant, tokens
    ):
        resp = client.post(
            f"/api/v1/workspaces/{WS}/tokens",
            json={"name": "ops-bot", "expires_in_days": 365},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["role"] == "readonly" and body["workspace_id"] == WS
        assert body["secret"].startswith("sdt_")
        (call,) = tokens["mint"]
        assert call["workspace_id"] == WS and call.get("user_id") is None
        assert call["role"] == "readonly"
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    def test_the_service_role_cannot_be_raised_by_the_body(
        self, client, signed_in, tenant, tokens
    ):
        resp = client.post(
            f"/api/v1/workspaces/{WS}/tokens", json={"name": "x", "role": "operator"}
        )
        assert resp.status_code == 201
        assert tokens["mint"][0]["role"] == "readonly"

    def test_a_member_cannot_mint_a_service_identity(
        self, client, signed_in, tenant, tokens
    ):
        tenant.refuse = TenantResolutionError("insufficient_role")
        resp = client.post(f"/api/v1/workspaces/{WS}/tokens", json={"name": "x"})
        assert resp.status_code == 403
        assert tokens["mint"] == []


class TestListingAndRevoking:
    def test_a_session_lists_its_own_tokens(self, client, signed_in, tokens):
        resp = client.get("/api/v1/me/tokens")
        assert resp.status_code == 200
        assert resp.json() == {
            "tokens": [{"id": TOKEN_ID, "name": "claude-code", "role": "operator"}]
        }
        assert tokens["list_user"] == [PRINCIPAL.user_id]

    def test_a_person_bound_token_lists_the_persons_tokens(
        self, client, as_principal, tokens
    ):
        as_principal(PERSON_TOKEN)
        resp = client.get("/api/v1/me/tokens")
        assert resp.status_code == 200
        assert tokens["list_user"] == [PRINCIPAL.user_id]

    def test_a_service_identity_has_no_personal_tokens(
        self, client, as_principal, tokens
    ):
        as_principal(SERVICE_TOKEN)
        resp = client.get("/api/v1/me/tokens")
        assert resp.status_code == 403
        assert resp.json()["reason"] == "session_required"

    def test_a_person_revokes_their_own_token(self, client, signed_in, tokens):
        resp = client.delete(f"/api/v1/me/tokens/{TOKEN_ID}")
        assert resp.status_code == 200 and resp.json() == {"revoked": True}
        assert tokens["revoke"] == [
            {"token_id": TOKEN_ID, "user_id": PRINCIPAL.user_id}
        ]

    def test_revoking_someone_elses_token_is_not_found(self, client, signed_in, tokens):
        tokens["revoke_answer"] = False
        resp = client.delete(f"/api/v1/me/tokens/{TOKEN_ID}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}

    def test_a_malformed_token_id_is_422_like_every_uuid_segment(
        self, client, signed_in, tokens
    ):
        resp = client.delete("/api/v1/me/tokens/not-a-uuid")
        assert resp.status_code == 422
        assert tokens["revoke"] == []

    def test_an_admin_lists_and_revokes_the_workspaces_identities(
        self, client, signed_in, tenant, tokens
    ):
        resp = client.get(f"/api/v1/workspaces/{WS}/tokens")
        assert resp.status_code == 200
        assert resp.json()["tokens"][0]["id"] == SERVICE_ID
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant
        resp = client.delete(f"/api/v1/workspaces/{WS}/tokens/{SERVICE_ID}")
        assert resp.status_code == 200
        assert tokens["revoke"] == [{"token_id": SERVICE_ID, "workspace_id": WS}]

    def test_a_service_identity_lists_its_own_workspace_only(
        self, client, as_principal, tenant, tokens
    ):
        as_principal(SERVICE_TOKEN)
        resp = client.get(f"/api/v1/workspaces/{WS}/tokens")
        assert resp.status_code == 200, resp.text
        assert tokens["list_ws"] == [WS]
        assert not any(entry[0] == "gate" for entry in tenant), (
            "a service identity has no membership to gate on"
        )
        resp = client.get(f"/api/v1/workspaces/{OTHER_WS}/tokens")
        assert resp.status_code == 403
        assert resp.json()["reason"] == "wrong_workspace"

    def test_a_service_identity_may_revoke_only_itself(
        self, client, as_principal, tenant, tokens
    ):
        as_principal(SERVICE_TOKEN)
        resp = client.delete(f"/api/v1/workspaces/{WS}/tokens/{TOKEN_ID}")
        assert resp.status_code == 403
        assert resp.json()["reason"] == "readonly_token"
        assert tokens["revoke"] == []
        resp = client.delete(f"/api/v1/workspaces/{WS}/tokens/{SERVICE_ID}")
        assert resp.status_code == 200
        assert tokens["revoke"] == [{"token_id": SERVICE_ID, "workspace_id": WS}]


class TestAReadonlyTokenRevokesOnlyItself:
    """A revoke is a write: the readonly fence the command route has applies
    to the two DELETE routes a token can reach (the adversarial lens)."""

    def test_a_readonly_person_token_cannot_revoke_the_persons_other_tokens(
        self, client, as_principal, tenant, tokens
    ):
        as_principal(READONLY_PERSON)
        resp = client.delete(f"/api/v1/me/tokens/{SERVICE_ID}")
        assert resp.status_code == 403
        assert resp.json()["reason"] == "readonly_token"
        resp = client.delete(f"/api/v1/workspaces/{WS}/tokens/{SERVICE_ID}")
        assert resp.status_code == 403
        assert resp.json()["reason"] == "readonly_token"
        assert tokens["revoke"] == []

    def test_a_readonly_person_token_may_still_revoke_itself(
        self, client, as_principal, tokens
    ):
        as_principal(READONLY_PERSON)
        resp = client.delete(f"/api/v1/me/tokens/{TOKEN_ID}")
        assert resp.status_code == 200 and resp.json() == {"revoked": True}
        assert tokens["revoke"] == [
            {"token_id": TOKEN_ID, "user_id": PRINCIPAL.user_id}
        ]

    def test_an_operator_person_token_revokes_as_the_person(
        self, client, as_principal, tenant, tokens
    ):
        as_principal(PERSON_TOKEN)
        resp = client.delete(f"/api/v1/me/tokens/{SERVICE_ID}")
        assert resp.status_code == 200
        resp = client.delete(f"/api/v1/workspaces/{WS}/tokens/{SERVICE_ID}")
        assert resp.status_code == 200
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant
        assert tokens["revoke"] == [
            {"token_id": SERVICE_ID, "user_id": PRINCIPAL.user_id},
            {"token_id": SERVICE_ID, "workspace_id": WS},
        ]


class TestTheCliCommandRow:
    """The row's entity is the story the PORT acted on, never the body's claim."""

    class _Session:
        def __init__(self):
            self.calls = []

        async def execute(self, statement, params):
            self.calls.append((str(statement), dict(params)))

    def _command(self, **args):
        return Command(
            kind="sync_now",
            workspace_id=WS,
            actor_user_id=PRINCIPAL.user_id,
            channel="cli",
            args=args,
            actor_label="claude-code",
        )

    @pytest.mark.asyncio
    async def test_a_claimed_intent_id_never_becomes_the_entity(self):
        session = self._Session()
        await v1._audit_cli_command(
            session,
            workspace_id=WS,
            command=self._command(intent_id=INTENT),
            principal=PERSON_TOKEN,
            external_ref="sync_now:k",
            result=CommandResult("enqueued", {"job": "sync_media_source"}),
        )
        ((sql, params),) = session.calls
        assert "INSERT INTO audit_events" in sql
        assert (params["entity_kind"], params["entity_id"]) == ("workspace", WS)
        detail = json.loads(params["detail"])
        assert detail == {
            "v": 1,
            "event": "cli_command",
            "kind": "sync_now",
            "token_id": TOKEN_ID,
            "token_name": "claude-code",
            "external_ref": "sync_now:k",
        }

    @pytest.mark.asyncio
    async def test_the_ports_intent_is_the_entity(self):
        session = self._Session()
        await v1._audit_cli_command(
            session,
            workspace_id=WS,
            command=self._command(intent_id=INTENT),
            principal=PERSON_TOKEN,
            external_ref="skip:" + INTENT,
            result=CommandResult("executed", {"intent_id": INTENT, "state": "skipped"}),
        )
        ((_, params),) = session.calls
        assert (params["entity_kind"], params["entity_id"]) == ("post_intent", INTENT)

    @pytest.mark.asyncio
    async def test_a_malformed_intent_in_the_answer_falls_back_to_the_workspace(self):
        session = self._Session()
        await v1._audit_cli_command(
            session,
            workspace_id=WS,
            command=self._command(),
            principal=PERSON_TOKEN,
            external_ref="k",
            result=CommandResult("executed", {"intent_id": "not-a-uuid"}),
        )
        ((_, params),) = session.calls
        assert (params["entity_kind"], params["entity_id"]) == ("workspace", WS)


class TestThePrincipalRoute:
    def test_a_session_sees_itself(self, client, signed_in, monkeypatch):
        async def memberships(conn, *, user_id):
            return [{"id": WS, "name": "Mine", "state": "active", "role": "owner"}]

        monkeypatch.setattr(workspaces, "list_for_user", memberships)
        resp = client.get("/api/v1/me/principal")
        assert resp.status_code == 200
        assert resp.json() == {
            "kind": "session",
            "user_id": PRINCIPAL.user_id,
            "token": None,
            "workspaces": [{"id": WS, "name": "Mine", "role": "owner"}],
        }

    def test_a_service_identity_sees_its_workspace_under_its_role(
        self, client, as_principal, tenant, monkeypatch
    ):
        as_principal(SERVICE_TOKEN)

        async def get_workspace(conn, *, workspace_id):
            return {"id": workspace_id, "name": "Ops", "state": "active"}

        monkeypatch.setattr(workspaces, "get_workspace", get_workspace)
        resp = client.get("/api/v1/me/principal")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "token" and body["user_id"] is None
        assert body["token"]["workspace_id"] == WS
        assert body["workspaces"] == [{"id": WS, "name": "Ops", "role": "readonly"}]
        assert ("uow", WS, None) in tenant


class TestTheCommandRouteUnderAToken:
    URL = f"/api/v1/workspaces/{WS}/commands/approve"

    def test_a_person_bound_operator_token_acts_as_the_person_over_cli(
        self, client, as_principal, tenant, port
    ):
        as_principal(PERSON_TOKEN)
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 200, resp.text
        assert port["admit"] == [
            ("cli", "approve:" + INTENT, {"intent_id": INTENT}, f"token:{TOKEN_ID}")
        ]
        (cmd,) = port["execute"]
        assert (cmd.actor_user_id, cmd.channel, cmd.actor_label) == (
            PRINCIPAL.user_id,
            "cli",
            "claude-code",
        )
        assert tenant[0] == ("uow", WS, PRINCIPAL.user_id)
        (audit,) = port["audit"]
        assert audit["workspace_id"] == WS
        assert audit["principal"] == PERSON_TOKEN
        assert audit["external_ref"] == "approve:" + INTENT
        assert audit["command"] is cmd

    def test_a_session_writes_no_cli_command_row(self, client, signed_in, tenant, port):
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 200
        assert port["admit"][0][0] == "web"
        assert port["audit"] == []

    def test_a_readonly_person_token_is_refused_before_admission(
        self, client, as_principal, tenant, port
    ):
        as_principal(READONLY_PERSON)
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 403
        assert resp.json()["reason"] == "readonly_token"
        assert port["admit"] == [] and port["execute"] == []

    def test_a_service_identity_never_writes(self, client, as_principal, tenant, port):
        as_principal(SERVICE_TOKEN)
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 403
        assert resp.json()["reason"] == "readonly_token"
        assert port["admit"] == []

    def test_a_service_identity_on_another_workspace_is_wrong_workspace(
        self, client, as_principal, tenant, port
    ):
        as_principal(SERVICE_TOKEN)
        resp = client.post(
            f"/api/v1/workspaces/{OTHER_WS}/commands/approve",
            json={"intent_id": INTENT},
            headers=KEY,
        )
        assert resp.status_code == 403
        assert resp.json()["reason"] == "wrong_workspace"

    def test_a_replay_under_a_token_writes_no_audit_row(
        self, client, as_principal, tenant, port, monkeypatch
    ):
        as_principal(PERSON_TOKEN)

        async def admit(session, **kw):
            raise webhook_ingress.DeliveryReplayed("same key, same body")

        monkeypatch.setattr(webhook_ingress, "admit", admit)
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 200 and resp.json() == {"outcome": "replayed"}
        assert port["audit"] == []
