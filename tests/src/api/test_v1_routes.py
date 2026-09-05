"""`/api/v1` — the adapter's contract, with every service seam patched.

What is pinned: authentication is required and cookie/bearer are one
credential; a non-member gets the same 404 a missing workspace gets; the
command route refuses an unknown name BEFORE admission, requires the
idempotency key, admits then executes, and maps every refusal the port can
raise to the status the design table says. The database path is the X.2 gate
in `tests/scripts/`.
"""

from __future__ import annotations

import pytest

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    channel_bind,
    commands,
    identity,
    identity_link,
    ig_login_oauth,
    invitations,
    sessions,
    webhook_ingress,
    workspaces,
)
from src.services.target.commands import CommandNotBuilt, CommandRefused, CommandResult
from src.services.target.webhook_ingress import AdmissionConflict, DeliveryReplayed
from tests.src.api.conftest import INTENT, PRINCIPAL, WS

KEY = {"Idempotency-Key": "k-1"}


@pytest.fixture
def user_plane(monkeypatch):
    async def get_user(conn, *, user_id):
        return {
            "id": user_id,
            "primary_email": "p@example.com",
            "state": "active",
            "identities": [],
        }

    async def list_for_user(conn, *, user_id):
        return [{"id": WS, "name": "Mine", "state": "active", "role": "owner"}]

    monkeypatch.setattr(identity, "get_user", get_user)
    monkeypatch.setattr(workspaces, "list_for_user", list_for_user)


class TestAuthentication:
    def test_no_session_is_401_and_says_nothing_more(self, client):
        resp = client.get("/api/v1/me")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "authentication required"}

    def test_cookie_and_bearer_are_one_credential(
        self, client, monkeypatch, user_plane
    ):
        hashes = []

        async def resolve(conn, *, token_hash):
            hashes.append(token_hash)
            return sessions.Session(id=PRINCIPAL.session_id, user_id=PRINCIPAL.user_id)

        monkeypatch.setattr(sessions, "resolve", resolve)
        assert (
            client.get("/api/v1/me", cookies={"sd_session": "opaque"}).status_code
            == 200
        )
        assert (
            client.get(
                "/api/v1/me", headers={"Authorization": "Bearer opaque"}
            ).status_code
            == 200
        )
        assert hashes == [sessions.token_hash("opaque")] * 2

    @pytest.mark.parametrize(
        "reason", ["expired_session", "revoked_session", "disabled_user"]
    )
    def test_dead_sessions_are_401_without_saying_why(
        self, client, monkeypatch, reason
    ):
        async def resolve(conn, *, token_hash):
            raise TenantResolutionError(reason)

        monkeypatch.setattr(sessions, "resolve", resolve)
        resp = client.get("/api/v1/me", headers={"Authorization": "Bearer x"})
        assert resp.status_code == 401
        assert resp.json() == {"detail": "authentication required"}


class TestTenantlessReads:
    def test_me_is_the_user_and_the_memberships(self, client, signed_in, user_plane):
        body = client.get("/api/v1/me").json()
        assert body["user"]["id"] == PRINCIPAL.user_id
        assert body["workspaces"][0]["role"] == "owner"

    def test_workspaces_is_the_membership_list(self, client, signed_in, user_plane):
        assert client.get("/api/v1/workspaces").json() == {
            "workspaces": [
                {"id": WS, "name": "Mine", "state": "active", "role": "owner"}
            ]
        }


class TestWorkspaceReads:
    def test_reads_pass_the_one_gate_under_the_claimed_tenant(
        self, client, signed_in, tenant, monkeypatch
    ):
        async def list_members(session, *, workspace_id):
            return [{"user_id": PRINCIPAL.user_id, "role": "owner"}]

        monkeypatch.setattr(workspaces, "list_members", list_members)
        resp = client.get(f"/api/v1/workspaces/{WS}/members")
        assert resp.status_code == 200
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "member"),
        ]

    def test_non_member_is_404_the_same_as_missing(self, client, signed_in, tenant):
        tenant.refuse = TenantResolutionError("not_a_member")
        resp = client.get(f"/api/v1/workspaces/{WS}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}

    def test_a_non_uuid_workspace_is_refused_before_any_seam(self, client, signed_in):
        assert client.get("/api/v1/workspaces/not-a-uuid").status_code == 422

    def test_list_limit_is_bounded_and_states_are_a_closed_list(
        self, client, signed_in, tenant, monkeypatch
    ):
        seen = {}

        async def list_intents(session, *, workspace_id, states=(), limit=50):
            seen.update(states=list(states), limit=limit)
            return []

        monkeypatch.setattr(workspaces, "list_intents", list_intents)
        assert (
            client.get(f"/api/v1/workspaces/{WS}/intents?limit=201").status_code == 422
        )
        assert client.get(f"/api/v1/workspaces/{WS}/intents?limit=0").status_code == 422
        assert (
            client.get(
                f"/api/v1/workspaces/{WS}/intents?state=posted,frobnicated"
            ).status_code
            == 422
        )
        resp = client.get(
            f"/api/v1/workspaces/{WS}/intents?state=posted, skipped,rejected"
        )
        assert resp.status_code == 200
        assert seen == {"states": ["posted", "skipped", "rejected"], "limit": 50}
        assert resp.json() == {"intents": [], "limit": 50}

    def test_media_reads_pass_the_gate_and_validate_the_state(
        self, client, signed_in, tenant, monkeypatch
    ):
        seen = {}

        async def list_media(
            session, *, workspace_id, state=None, never_posted=False, limit=50
        ):
            seen.update(state=state, never_posted=never_posted, limit=limit)
            return [{"id": INTENT, "file_name": "f.jpg"}]

        async def get_media(session, *, workspace_id, media_id):
            return (
                {"id": media_id, "file_name": "f.jpg"} if media_id == INTENT else None
            )

        monkeypatch.setattr(workspaces, "list_media", list_media)
        monkeypatch.setattr(workspaces, "get_media", get_media)
        assert (
            client.get(f"/api/v1/workspaces/{WS}/media?state=bogus").status_code == 422
        )
        resp = client.get(
            f"/api/v1/workspaces/{WS}/media?state=available&never_posted=true"
        )
        assert resp.status_code == 200
        assert seen == {"state": "available", "never_posted": True, "limit": 50}
        assert resp.json()["media"][0]["file_name"] == "f.jpg"
        assert client.get(f"/api/v1/workspaces/{WS}/media/{INTENT}").status_code == 200
        assert client.get(f"/api/v1/workspaces/{WS}/media/{WS}").status_code == 404
        assert ("gate", WS, PRINCIPAL.user_id, "member") in tenant

    def test_stats_is_served_under_the_gate(
        self, client, signed_in, tenant, monkeypatch
    ):
        async def stats(session, *, workspace_id):
            return {"intents_by_state": {"posted": 2}, "accounts": 1}

        monkeypatch.setattr(workspaces, "stats", stats)
        resp = client.get(f"/api/v1/workspaces/{WS}/stats")
        assert resp.status_code == 200
        assert resp.json()["intents_by_state"] == {"posted": 2}
        assert ("gate", WS, PRINCIPAL.user_id, "member") in tenant


@pytest.fixture
def port(monkeypatch):
    """Record admissions and executions; the executor answers `outcome`."""
    log = {
        "admit": [],
        "execute": [],
        "outcome": CommandResult("executed", {"state": "approved"}),
    }

    async def admit(session, *, channel, external_ref, payload, principal):
        log["admit"].append((channel, external_ref, payload, principal))

    async def execute(session, command, **kw):
        log["execute"].append(command)
        result = log["outcome"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(webhook_ingress, "admit", admit)
    monkeypatch.setattr(commands, "execute", execute)
    return log


class TestCommands:
    URL = f"/api/v1/workspaces/{WS}/commands/approve"

    def test_the_idempotency_key_is_required(self, client, signed_in, tenant, port):
        resp = client.post(self.URL, json={"intent_id": INTENT})
        assert resp.status_code == 400
        assert "Idempotency-Key" in resp.json()["detail"]
        assert port["admit"] == [] and port["execute"] == []

    def test_unknown_command_is_refused_before_admission(
        self, client, signed_in, tenant, port
    ):
        resp = client.post(
            f"/api/v1/workspaces/{WS}/commands/frobnicate", json={}, headers=KEY
        )
        assert resp.status_code == 404
        assert port["admit"] == []

    def test_create_workspace_has_its_own_route(self, client, signed_in, tenant, port):
        resp = client.post(
            f"/api/v1/workspaces/{WS}/commands/create_workspace",
            json={"name": "x"},
            headers=KEY,
        )
        assert resp.status_code == 404
        assert port["admit"] == []

    def test_admits_then_executes_the_normalized_command(
        self, client, signed_in, tenant, port
    ):
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 200
        assert resp.json() == {"outcome": "executed", "state": "approved"}
        assert port["admit"] == [
            ("web", "k-1", {"intent_id": INTENT}, PRINCIPAL.session_id)
        ]
        (cmd,) = port["execute"]
        assert (cmd.kind, cmd.workspace_id, cmd.actor_user_id, cmd.channel) == (
            "approve",
            WS,
            PRINCIPAL.user_id,
            "web",
        )
        assert cmd.args == {"intent_id": INTENT}
        assert tenant[0] == ("uow", WS, PRINCIPAL.user_id)

    def test_an_enqueued_outcome_is_202(self, client, signed_in, tenant, port):
        port["outcome"] = CommandResult("enqueued", {"job": "publish_pipeline"})
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 202
        assert resp.json()["job"] == "publish_pipeline"

    def test_a_replay_is_200_and_never_executes(
        self, client, signed_in, tenant, port, monkeypatch
    ):
        async def admit(session, **kw):
            raise DeliveryReplayed("same key, same body")

        monkeypatch.setattr(webhook_ingress, "admit", admit)
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 200
        assert resp.json() == {"outcome": "replayed"}
        assert port["execute"] == []

    def test_key_reuse_with_a_different_body_is_409(
        self, client, signed_in, tenant, port, monkeypatch
    ):
        async def admit(session, **kw):
            raise AdmissionConflict("same key, different body")

        monkeypatch.setattr(webhook_ingress, "admit", admit)
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 409
        assert resp.json()["reason"] == "admission_conflict"
        assert port["execute"] == []

    def test_not_built_is_501_naming_the_command(self, client, signed_in, tenant, port):
        port["outcome"] = CommandNotBuilt("transfer_ownership")
        resp = client.post(
            f"/api/v1/workspaces/{WS}/commands/transfer_ownership", json={}, headers=KEY
        )
        assert resp.status_code == 501
        assert resp.json() == {
            "command": "transfer_ownership",
            "detail": "not built",
            "reason": "not_built",
        }

    @pytest.mark.parametrize(
        "reason, status",
        [
            ("invalid_args", 400),
            ("workspace_required", 400),
            ("not_found", 404),
            ("illegal_transition", 409),
            ("manual_mode", 409),
        ],
    )
    def test_each_port_refusal_maps_to_its_status(
        self, client, signed_in, tenant, port, reason, status
    ):
        port["outcome"] = CommandRefused(reason, "detail")
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == status
        assert resp.json()["reason"] == reason

    def test_a_member_below_the_floor_is_403(self, client, signed_in, tenant, port):
        port["outcome"] = TenantResolutionError("insufficient_role", "member < admin")
        resp = client.post(self.URL, json={}, headers=KEY)
        assert resp.status_code == 403

    def test_a_non_object_body_is_400(self, client, signed_in, tenant, port):
        resp = client.post(
            self.URL,
            content=b"[1, 2]",
            headers={**KEY, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert port["admit"] == []


class TestCreateWorkspace:
    def test_preassigns_the_id_and_opens_the_unit_of_work_for_it(
        self, client, signed_in, tenant, port
    ):
        port["outcome"] = CommandResult(
            "executed", {"workspace_id": "filled-by-executor"}
        )
        resp = client.post("/api/v1/workspaces", json={"name": "Mine"}, headers=KEY)
        assert resp.status_code == 201
        (cmd,) = port["execute"]
        assert cmd.kind == "create_workspace" and cmd.workspace_id is None
        assert cmd.args["name"] == "Mine"
        preassigned = cmd.args["workspace_id"]
        assert len(preassigned) == 36
        assert tenant == [
            ("uow", preassigned, PRINCIPAL.user_id)
        ]  # no gate: no membership yet
        assert port["admit"][0][1] == "k-1"

    def test_a_client_supplied_id_is_ignored(self, client, signed_in, tenant, port):
        port["outcome"] = CommandResult("executed", {"workspace_id": "x"})
        client.post(
            "/api/v1/workspaces", json={"name": "Mine", "workspace_id": WS}, headers=KEY
        )
        (cmd,) = port["execute"]
        assert cmd.args["workspace_id"] != WS


class TestInvitations:
    def test_accept_is_the_service_call_and_its_refusals_map(
        self, client, signed_in, monkeypatch
    ):
        seen = {}

        async def accept(conn, *, token, user_id, channel):
            seen.update(token=token, user_id=user_id, channel=channel)
            return {"workspace_id": WS, "role": "member", "matched": True}

        monkeypatch.setattr(invitations, "accept", accept)
        resp = client.post("/api/v1/invitations/tok-1/accept")
        assert resp.status_code == 200
        assert resp.json() == {"workspace_id": WS, "role": "member", "matched": True}
        assert seen == {
            "token": "tok-1",
            "user_id": PRINCIPAL.user_id,
            "channel": "web",
        }

    @pytest.mark.parametrize(
        "reason, status", [("not_acceptable", 404), ("identity_mismatch", 403)]
    )
    def test_each_refusal_maps_to_its_status(
        self, client, signed_in, monkeypatch, reason, status
    ):
        async def accept(conn, **kw):
            raise invitations.InvitationRefused(reason)

        monkeypatch.setattr(invitations, "accept", accept)
        resp = client.post("/api/v1/invitations/tok-1/accept")
        assert resp.status_code == status
        assert resp.json()["reason"] == reason


ACCOUNT = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def instagram_configured(monkeypatch):
    """Instagram Login configured — shared by both connect routes' suites."""
    from src.config.settings import settings

    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "app-1", raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "sec", raising=False)
    monkeypatch.setattr(
        settings, "OAUTH_REDIRECT_BASE_URL", "https://api.example.test", raising=False
    )


@pytest.fixture
def issued(monkeypatch):
    """Records what `issue_state` was asked to mint; returns a fixed state."""
    seen = {}

    async def issue_state(session, **kw):
        seen.update(kw)
        return "st4te"

    from src.api.routes import v1

    monkeypatch.setattr(v1, "issue_state", issue_state)
    return seen


class TestDestinationConnect:
    """`POST /workspaces/{ws}/accounts/{id}/connect` — start the Instagram
    Login grant for ONE destination (#1220 step 2, #1041). The Drive connect
    route's shape: admin floor, a state row pinned to the account, and the
    URL the browser goes to."""

    @pytest.fixture
    def purpose(self, monkeypatch):
        holder = {"value": "connect", "asked": None}

        async def connect_purpose(session, *, workspace_id, ig_account_id):
            holder["asked"] = (workspace_id, ig_account_id)
            return holder["value"]

        monkeypatch.setattr(ig_login_oauth, "connect_purpose", connect_purpose)
        return holder

    def test_mints_a_state_pinned_to_the_account_and_says_where_to_go(
        self, client, signed_in, tenant, instagram_configured, purpose, issued
    ):
        resp = client.post(f"/api/v1/workspaces/{WS}/accounts/{ACCOUNT}/connect")
        assert resp.status_code == 200, resp.text
        url = resp.json()["authorization_url"]
        assert url.startswith("https://api.instagram.com/oauth/authorize?")
        assert "state=st4te" in url
        assert "instagram-login%2Fcallback" in url
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "admin"),
        ]
        assert purpose["asked"] == (WS, ACCOUNT)
        assert issued["purpose"] == "connect"
        assert issued["provider"] == ig_login_oauth.PROVIDER
        assert issued["reconnect_target"] == ACCOUNT
        assert issued["workspace_id"] == WS
        assert issued["user_id"] == PRINCIPAL.user_id

    def test_a_credentialed_account_mints_a_reconnect(
        self, client, signed_in, tenant, instagram_configured, purpose, issued
    ):
        purpose["value"] = "reconnect"
        resp = client.post(f"/api/v1/workspaces/{WS}/accounts/{ACCOUNT}/connect")
        assert resp.status_code == 200
        assert issued["purpose"] == "reconnect"

    def test_an_account_that_is_not_this_workspaces_is_404_never_403(
        self, client, signed_in, tenant, instagram_configured, purpose, issued
    ):
        purpose["value"] = None
        resp = client.post(f"/api/v1/workspaces/{WS}/accounts/{ACCOUNT}/connect")
        assert resp.status_code == 404
        assert issued == {}

    def test_unconfigured_instagram_refuses_503_before_any_seam(
        self, client, signed_in, tenant, purpose, issued, monkeypatch
    ):
        from src.config.settings import settings

        monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", None, raising=False)
        resp = client.post(f"/api/v1/workspaces/{WS}/accounts/{ACCOUNT}/connect")
        assert resp.status_code == 503
        assert "INSTAGRAM_APP_ID" in resp.json()["detail"]
        assert tenant == []

    def test_a_non_uuid_account_is_refused_before_any_seam(
        self, client, signed_in, tenant, instagram_configured
    ):
        resp = client.post(f"/api/v1/workspaces/{WS}/accounts/not-an-id/connect")
        assert resp.status_code == 422
        assert tenant == []


class TestWorkspaceConnect:
    """`POST /workspaces/{ws}/accounts/connect` — start the Instagram Login
    grant for an account that is NOT yet a destination (owner ruling
    2026-09-04: destinations are added by connecting). Admin floor; the state
    pins the user and the workspace and NO target — the callback adopts or
    creates the destination from the identity Instagram returns."""

    URL = f"/api/v1/workspaces/{WS}/accounts/connect"

    def test_requires_a_session(self, client, instagram_configured):
        assert client.post(self.URL).status_code == 401

    def test_mints_an_untargeted_connect_state_and_says_where_to_go(
        self, client, signed_in, tenant, instagram_configured, issued
    ):
        resp = client.post(self.URL)
        assert resp.status_code == 200, resp.text
        url = resp.json()["authorization_url"]
        assert url.startswith("https://api.instagram.com/oauth/authorize?")
        assert "state=st4te" in url
        assert "instagram-login%2Fcallback" in url
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "admin"),
        ]
        assert issued["purpose"] == "connect"
        assert issued["provider"] == ig_login_oauth.PROVIDER
        assert issued["reconnect_target"] is None
        assert issued["workspace_id"] == WS
        assert issued["user_id"] == PRINCIPAL.user_id

    def test_unconfigured_instagram_refuses_503_before_any_seam(
        self, client, signed_in, tenant, issued, monkeypatch
    ):
        from src.config.settings import settings

        monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", None, raising=False)
        resp = client.post(self.URL)
        assert resp.status_code == 503
        assert "INSTAGRAM_APP_ID" in resp.json()["detail"]
        assert tenant == [] and issued == {}


class TestTelegramGroupBindLink:
    """`POST /workspaces/{ws}/telegram/bind-link` — the admin's one-shot link
    that opens Telegram's group picker and binds the chosen group to THIS
    workspace (owner ruling 2026-09-05; #1175 D-3 token, D-4 same flow for the
    Nth group). Admin floor: deciding where a workspace's cards go is an
    admin act (`06` §4)."""

    URL = f"/api/v1/workspaces/{WS}/telegram/bind-link"

    @pytest.fixture
    def bot(self, monkeypatch):
        from src.config.settings import settings

        monkeypatch.setattr(
            settings, "TARGET_TELEGRAM_BOT_USERNAME", "storydump_app_bot", raising=False
        )

    @pytest.fixture
    def issued(self, monkeypatch):
        seen = {}

        async def issue_bind_state(conn, *, user_id, workspace_id, bot_username):
            seen.update(
                user_id=user_id, workspace_id=workspace_id, bot_username=bot_username
            )
            return f"https://t.me/{bot_username}?startgroup=bind-st4te"

        monkeypatch.setattr(channel_bind, "issue_bind_state", issue_bind_state)
        return seen

    @pytest.fixture(autouse=True)
    def linked(self, monkeypatch):
        """The minting admin has linked Telegram — the route's precondition."""
        holder = {"external_id": "tg-42"}

        async def identity_for_user(session, *, user_id, provider):
            return holder["external_id"]

        monkeypatch.setattr(identity, "identity_for_user", identity_for_user)
        return holder

    def test_requires_a_session(self, client, bot):
        assert client.post(self.URL).status_code == 401

    def test_an_admin_who_has_not_linked_telegram_is_told_first(
        self, client, signed_in, tenant, bot, issued, linked
    ):
        linked["external_id"] = None
        resp = client.post(self.URL)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "link_telegram_first"
        assert issued == {}

    def test_unconfigured_bot_is_a_503_naming_the_setting(
        self, client, signed_in, tenant, issued, monkeypatch
    ):
        from src.config.settings import settings

        monkeypatch.setattr(
            settings, "TARGET_TELEGRAM_BOT_USERNAME", None, raising=False
        )
        resp = client.post(self.URL)
        assert resp.status_code == 503
        assert "TARGET_TELEGRAM_BOT_USERNAME" in resp.json()["detail"]
        assert issued == {} and tenant == []

    def test_issues_a_group_link_at_the_admin_floor(
        self, client, signed_in, tenant, bot, issued
    ):
        resp = client.post(self.URL)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["link"] == "https://t.me/storydump_app_bot?startgroup=bind-st4te"
        assert body["expires_in_seconds"] == ig_login_oauth.STATE_TTL_SECONDS
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "admin"),
        ]
        assert issued == {
            "user_id": PRINCIPAL.user_id,
            "workspace_id": WS,
            "bot_username": "storydump_app_bot",
        }


class TestTelegramLink:
    """`POST /me/telegram/link` — the link a signed-in user taps to attach
    their Telegram identity (`07` §2 `link`: only from an authenticated
    session; the row pins the user). The service half landed in #1180; this
    is the route the X.3 drive was missing (#1172, #1157)."""

    URL = "/api/v1/me/telegram/link"

    @pytest.fixture
    def bot(self, monkeypatch):
        from src.config.settings import settings

        monkeypatch.setattr(
            settings, "TARGET_TELEGRAM_BOT_USERNAME", "storydump_app_bot", raising=False
        )

    @pytest.fixture
    def issued(self, monkeypatch):
        seen = {}

        async def issue_link_state(conn, *, user_id, bot_username):
            seen.update(user_id=user_id, bot_username=bot_username)
            return f"https://t.me/{bot_username}?start=link-st4te"

        monkeypatch.setattr(identity_link, "issue_link_state", issue_link_state)
        return seen

    def test_requires_a_session(self, client):
        assert client.post(self.URL).status_code == 401

    def test_unconfigured_bot_is_a_503_naming_the_setting(
        self, client, signed_in, issued, monkeypatch
    ):
        from src.config.settings import settings

        monkeypatch.setattr(
            settings, "TARGET_TELEGRAM_BOT_USERNAME", None, raising=False
        )
        resp = client.post(self.URL)
        assert resp.status_code == 503
        assert "TARGET_TELEGRAM_BOT_USERNAME" in resp.json()["detail"]
        assert issued == {}

    def test_a_username_that_is_not_a_telegram_bot_username_is_refused_by_name(
        self, client, signed_in, issued, monkeypatch
    ):
        """A trailing period took production down on 2026-09-04: the API
        minted `t.me/storydump_app_bot.?start=…` and the site refused it as a
        different bot. The route must refuse the setting's SHAPE up front,
        naming the setting and the value, and mint nothing."""
        from src.config.settings import settings

        monkeypatch.setattr(
            settings,
            "TARGET_TELEGRAM_BOT_USERNAME",
            "storydump_app_bot.",
            raising=False,
        )
        resp = client.post(self.URL)
        assert resp.status_code == 503
        assert "TARGET_TELEGRAM_BOT_USERNAME" in resp.json()["detail"]
        assert "storydump_app_bot." in resp.json()["detail"]
        assert issued == {}

    def test_a_stray_at_sign_in_the_setting_does_not_reach_the_link(
        self, client, signed_in, issued, monkeypatch
    ):
        from src.config.settings import settings

        monkeypatch.setattr(
            settings,
            "TARGET_TELEGRAM_BOT_USERNAME",
            " @storydump_app_bot ",
            raising=False,
        )
        resp = client.post(self.URL)
        assert resp.status_code == 200
        assert issued["bot_username"] == "storydump_app_bot"

    def test_issues_a_link_for_the_signed_in_user_and_says_how_long_it_lasts(
        self, client, signed_in, bot, issued
    ):
        resp = client.post(self.URL)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["link"] == "https://t.me/storydump_app_bot?start=link-st4te"
        assert body["expires_in_seconds"] == ig_login_oauth.STATE_TTL_SECONDS
        assert issued == {
            "user_id": PRINCIPAL.user_id,
            "bot_username": "storydump_app_bot",
        }
