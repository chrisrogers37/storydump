"""`/api/v1` — the adapter's contract, with every service seam patched.

What is pinned: authentication is required and cookie/bearer are one
credential; a non-member gets the same 404 a missing workspace gets; the
command route refuses an unknown name BEFORE admission, requires the
idempotency key, admits then executes, and maps every refusal the port can
raise to the status the design table says. The database path is the X.2 gate
in `tests/scripts/`.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import (
    bindings,
    category_mix,
    channel_bind,
    commands,
    content_runway,
    google_drive_oauth,
    identity,
    identity_link,
    ig_login_oauth,
    invitations,
    media_sync,
    oauth_states,
    provisioning,
    sessions,
    tenant_resolution,
    webhook_ingress,
    workspaces,
)
from src.services.target.drive_adapter import DriveRetryableError
from src.services.target.commands import CommandNotBuilt, CommandRefused, CommandResult
from src.services.target.webhook_ingress import AdmissionConflict, DeliveryReplayed
from src.services.target.work_loop import WorkerConfig
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

        async def list_intents(
            session,
            *,
            workspace_id,
            states=(),
            origin=None,
            newest_first=False,
            limit=50,
            from_date=None,
            to_date=None,
        ):
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

    def test_the_origin_is_a_closed_list_and_reaches_the_read(
        self, client, signed_in, tenant, monkeypatch
    ):
        """What is coming is the Queue read filtered to planned stories: no
        endpoint of its own (#1413 phase 5)."""
        seen = {}

        async def list_intents(
            session,
            *,
            workspace_id,
            states=(),
            origin=None,
            newest_first=False,
            limit=50,
            from_date=None,
            to_date=None,
        ):
            seen.update(states=list(states), origin=origin, newest_first=newest_first)
            return []

        monkeypatch.setattr(workspaces, "list_intents", list_intents)
        for bad, word in (("origin=someday", "someday"), ("order=up", "up")):
            resp = client.get(f"/api/v1/workspaces/{WS}/intents?{bad}")
            assert resp.status_code == 422 and word in resp.json()["detail"]
        assert seen == {}
        resp = client.get(
            f"/api/v1/workspaces/{WS}/intents?origin=planned&state=scheduled"
        )
        assert resp.status_code == 200
        assert seen == {
            "states": ["scheduled"],
            "origin": "planned",
            "newest_first": False,
        }
        client.get(f"/api/v1/workspaces/{WS}/intents?state=expired&order=desc")
        assert seen["origin"] is None and seen["newest_first"] is True

    def test_a_date_range_is_local_days_both_or_neither_and_bounded(
        self, client, signed_in, tenant, monkeypatch
    ):
        """The calendar's day view (#1634): ``from`` and ``to`` are the
        workspace's local days, half-open, and reach the read as dates."""
        seen = {}

        async def list_intents(session, **kwargs):
            seen.update(kwargs)
            return []

        monkeypatch.setattr(workspaces, "list_intents", list_intents)
        url = f"/api/v1/workspaces/{WS}/intents"
        for bad in (
            "from=2026-10-03",
            "to=2026-10-04",
            "from=2026-10-04&to=2026-10-04",
            "from=2026-10-05&to=2026-10-04",
            "from=2026-10-01&to=2026-11-16",
            "from=2026-13-01&to=2026-13-02",
        ):
            assert client.get(f"{url}?{bad}").status_code == 422, bad
        assert seen == {}
        assert client.get(f"{url}?from=2026-10-03&to=2026-10-04").status_code == 200
        assert (seen["from_date"], seen["to_date"]) == (
            date(2026, 10, 3),
            date(2026, 10, 4),
        )
        client.get(url)
        assert (seen["from_date"], seen["to_date"]) == (None, None)
        # The span is a difference: a sum would overflow at the calendar's end.
        assert client.get(f"{url}?from=9999-12-30&to=9999-12-31").status_code == 200
        assert seen["to_date"] == date(9999, 12, 31)

    def test_the_month_read_needs_a_state_and_a_range_and_bounds_its_names(
        self, client, signed_in, tenant, monkeypatch
    ):
        """``GET …/intents/days`` (#1634): the month's count and newest names
        per local day. Registered before ``/intents/{intent_id}``, which would
        otherwise read ``days`` as an id."""
        seen = {}
        day = {"date": "2026-10-03", "count": 15, "newest": []}

        async def intent_days(session, **kwargs):
            seen.update(kwargs)
            return [day]

        monkeypatch.setattr(workspaces, "intent_days", intent_days)
        url = f"/api/v1/workspaces/{WS}/intents/days"
        month = "from=2026-09-28&to=2026-11-02"
        for bad in (
            month,
            f"state=frobnicated&{month}",
            "state=posted",
            f"state=posted&{month}&per_day=0",
            f"state=posted&{month}&per_day=11",
        ):
            assert client.get(f"{url}?{bad}").status_code == 422, bad
        assert seen == {}
        resp = client.get(f"{url}?state=posted&{month}")
        assert resp.status_code == 200
        assert resp.json() == {"days": [day], "per_day": 3}
        assert seen["states"] == ["posted"]
        assert (seen["from_date"], seen["to_date"]) == (
            date(2026, 9, 28),
            date(2026, 11, 2),
        )
        assert seen["per_day"] == 3

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

    def test_runway_is_served_under_the_gate(
        self, client, signed_in, tenant, monkeypatch
    ):
        from src.api.routes import v1

        seen = {}

        async def runway(session, *, workspace_id, below_days):
            seen.update(ws=workspace_id, below_days=below_days)
            return {
                "below_days": below_days,
                "accounts": [{"id": "a-1", "days_left": 4}],
            }

        monkeypatch.setattr(content_runway, "runway", runway)
        # A worker level that is not the default, so what is pinned is the
        # worker's level, the one the notice is told at, not the constant.
        assert content_runway.LOW_RUNWAY_DAYS != 5
        monkeypatch.setattr(v1, "WorkerConfig", lambda: WorkerConfig(low_runway_days=5))
        resp = client.get(f"/api/v1/workspaces/{WS}/runway")
        assert resp.status_code == 200
        assert resp.json() == {
            "below_days": 5,
            "accounts": [{"id": "a-1", "days_left": 4}],
        }
        assert seen == {"ws": str(WS), "below_days": 5}
        assert ("gate", WS, PRINCIPAL.user_id, "member") in tenant

    @pytest.fixture
    def pending(self, monkeypatch):
        async def list_invitations(session, *, workspace_id):
            return [{"email": "invitee@example.com"}]

        monkeypatch.setattr(workspaces, "list_invitations", list_invitations)

    def test_an_admin_gets_the_pending_invitations(
        self, client, signed_in, tenant, pending
    ):
        resp = client.get(f"/api/v1/workspaces/{WS}/invitations")
        assert resp.status_code == 200
        assert resp.json()["invitations"][0]["email"] == "invitee@example.com"
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    def test_a_member_gets_no_pending_invitation(
        self, client, signed_in, tenant, pending, monkeypatch
    ):
        """A member: the gate passes the member floor and refuses any higher."""

        async def as_a_member(session, workspace_id, user_id, minimum_role="member"):
            if minimum_role != "member":
                raise TenantResolutionError("insufficient_role")

        monkeypatch.setattr(tenant_resolution, "authorize_member", as_a_member)
        resp = client.get(f"/api/v1/workspaces/{WS}/invitations")
        assert resp.status_code == 403
        assert "invitee@example.com" not in resp.text

    def test_the_listing_floor_is_the_minting_floor(
        self, client, signed_in, tenant, pending, monkeypatch
    ):
        monkeypatch.setitem(commands.ROLE_FLOOR, "invite_member", "owner")
        resp = client.get(f"/api/v1/workspaces/{WS}/invitations")
        assert resp.status_code == 200
        assert ("gate", WS, PRINCIPAL.user_id, "owner") in tenant


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

    async def execute(session, command):
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
            ("locked", 409),
        ],
    )
    def test_each_port_refusal_maps_to_its_status(
        self, client, signed_in, tenant, port, reason, status
    ):
        port["outcome"] = CommandRefused(reason, "detail")
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == status
        assert resp.json()["reason"] == reason
        assert "facts" not in resp.json(), "a refusal with no facts carries no key"

    def test_a_refusals_facts_ride_its_body_under_their_own_key(
        self, client, signed_in, tenant, port
    ):
        """`locked` names what is in the way and whether an override gets past
        it, beside the reason — a front end acts on them without parsing the
        prose — under one key, so no fact can stand in for `reason`."""
        port["outcome"] = CommandRefused(
            "locked",
            "item x: skip",
            facts={"in_the_way": ["skip"], "overridable": True, "reason": "forged"},
        )
        resp = client.post(self.URL, json={"intent_id": INTENT}, headers=KEY)
        assert resp.status_code == 409
        assert resp.json() == {
            "reason": "locked",
            "detail": "command refused: locked — item x: skip",
            "facts": {"in_the_way": ["skip"], "overridable": True, "reason": "forged"},
        }

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


#: A purpose-reading connect route's gates: (the purpose it mints, the entries
#: moved, the floors its gates check, in order). The first gate is the lower
#: connect floor; the minted purpose's own follows only when it is stricter.
CONNECT_FLOOR_CASES = [
    ("connect", {"connect_account": "owner"}, ["admin", "owner"]),
    ("reconnect", {"reconnect_account": "owner"}, ["admin", "owner"]),
    ("connect", {"reconnect_account": "owner"}, ["admin"]),
    (
        "reconnect",
        {"connect_account": "member", "reconnect_account": "member"},
        ["member"],
    ),
]


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
    """Records what the Instagram connect routes asked `issue_state` to mint;
    returns a fixed state."""
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

    @pytest.mark.parametrize("minted,moved,gates", CONNECT_FLOOR_CASES)
    def test_the_gates_follow_the_minted_purposes_floor(
        self,
        client,
        signed_in,
        tenant,
        instagram_configured,
        purpose,
        issued,
        monkeypatch,
        minted,
        moved,
        gates,
    ):
        purpose["value"] = minted
        for kind, floor in moved.items():
            monkeypatch.setitem(commands.ROLE_FLOOR, kind, floor)
        resp = client.post(f"/api/v1/workspaces/{WS}/accounts/{ACCOUNT}/connect")
        assert resp.status_code == 200, resp.text
        assert tenant == [("uow", WS, PRINCIPAL.user_id)] + [
            ("gate", WS, PRINCIPAL.user_id, floor) for floor in gates
        ]

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

    @pytest.mark.parametrize(
        "moved,floor", [("connect_account", "owner"), ("reconnect_account", "admin")]
    )
    def test_the_floor_is_connects_alone(
        self,
        client,
        signed_in,
        tenant,
        instagram_configured,
        issued,
        monkeypatch,
        moved,
        floor,
    ):
        monkeypatch.setitem(commands.ROLE_FLOOR, moved, "owner")
        resp = client.post(self.URL)
        assert resp.status_code == 200, resp.text
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, floor),
        ]

    def test_unconfigured_instagram_refuses_503_before_any_seam(
        self, client, signed_in, tenant, issued, monkeypatch
    ):
        from src.config.settings import settings

        monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", None, raising=False)
        resp = client.post(self.URL)
        assert resp.status_code == 503
        assert "INSTAGRAM_APP_ID" in resp.json()["detail"]
        assert tenant == [] and issued == {}


class TestTypedDestination:
    """`POST /workspaces/{ws}/accounts` takes a handle only (`v1.create_account`):
    a body carrying ``provider_account_ref`` is refused by name, after the
    admin gate, and nothing is written."""

    URL = f"/api/v1/workspaces/{WS}/accounts"

    @pytest.fixture
    def written(self, monkeypatch):
        calls = []

        async def create_destination(
            session,
            *,
            workspace_id,
            provider_account_ref=None,
            handle=None,
            schedule=True,
        ):
            calls.append((workspace_id, provider_account_ref, handle, schedule))
            return ACCOUNT, True

        monkeypatch.setattr(provisioning, "create_destination", create_destination)
        return calls

    def test_a_handle_reaches_the_writer_with_no_account_id(
        self, client, signed_in, tenant, written
    ):
        """The control: the writer gets the handle and no reference, and
        derives ``manual:<handle>`` from it (what lands is pinned by
        `tests/scripts/test_provisioning_gate.py`,
        `TestATypedHandleBecomesADestination`)."""
        resp = client.post(self.URL, json={"handle": "@ExampleShop"})
        assert resp.status_code == 201, resp.text
        assert resp.json() == {
            "account_id": ACCOUNT,
            "created": True,
            "scheduled": True,
        }
        assert written == [(WS, None, "@ExampleShop", True)]
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    @pytest.mark.parametrize(
        "body",
        [
            {"provider_account_ref": "17841400000000000"},
            {"handle": "h", "provider_account_ref": "manual:h"},
        ],
        ids=["account-id", "manual-ref-beside-a-handle"],
    )
    def test_a_provider_account_ref_is_refused_by_name_and_nothing_is_created(
        self, client, signed_in, tenant, written, body
    ):
        resp = client.post(self.URL, json=body)
        assert resp.status_code == 400, resp.text
        assert resp.json()["reason"] == "account_ref_requires_connect"
        assert written == []
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant


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
        assert body["expires_in_seconds"] == oauth_states.STATE_TTL_SECONDS
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "admin"),
        ]
        assert issued == {
            "user_id": PRINCIPAL.user_id,
            "workspace_id": WS,
            "bot_username": "storydump_app_bot",
        }


class TestRemoveTelegramGroup:
    """`DELETE /workspaces/{ws}/bindings/{binding_id}` — an admin removes a
    group (`07` §13): a revoke, never a delete, at the admin floor like the
    bind link."""

    BINDING = "44444444-4444-4444-8444-444444444444"
    URL = f"/api/v1/workspaces/{WS}/bindings/{BINDING}"

    @pytest.fixture
    def revoked(self, monkeypatch):
        seen = {"moved": True}

        async def revoke_for_workspace(session, *, workspace_id, binding_id):
            seen["asked"] = (workspace_id, binding_id)
            return seen["moved"]

        monkeypatch.setattr(bindings, "revoke_for_workspace", revoke_for_workspace)
        return seen

    def test_requires_a_session(self, client, revoked):
        assert client.delete(self.URL).status_code == 401
        assert "asked" not in revoked

    def test_revokes_at_the_admin_floor(self, client, signed_in, tenant, revoked):
        resp = client.delete(self.URL)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"binding_id": self.BINDING, "state": "revoked"}
        assert revoked["asked"] == (WS, self.BINDING)
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    def test_a_binding_that_is_not_here_or_already_revoked_is_404(
        self, client, signed_in, tenant, revoked
    ):
        revoked["moved"] = False
        assert client.delete(self.URL).status_code == 404

    def test_a_binding_id_that_is_not_a_uuid_never_reaches_the_service(
        self, client, signed_in, tenant, revoked
    ):
        resp = client.delete(f"/api/v1/workspaces/{WS}/bindings/nope")
        assert resp.status_code == 422
        assert "asked" not in revoked


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
        assert body["expires_in_seconds"] == oauth_states.STATE_TTL_SECONDS
        assert issued == {
            "user_id": PRINCIPAL.user_id,
            "bot_username": "storydump_app_bot",
        }


class TestTelegramUnlink:
    """`DELETE /me/telegram` — the signed-in user removes their own Telegram
    identity (099, `07` §42). Tenant-less; the door's outcome is the answer,
    and `last_identity` is the one refusal."""

    URL = "/api/v1/me/telegram"

    @pytest.fixture
    def unlink(self, monkeypatch):
        seen = {"outcome": "unlinked"}

        async def unlink_telegram(conn, *, user_id):
            seen["user_id"] = user_id
            return seen["outcome"]

        monkeypatch.setattr(identity, "unlink_telegram", unlink_telegram)
        return seen

    def test_requires_a_session(self, client, unlink):
        assert client.delete(self.URL).status_code == 401
        assert "user_id" not in unlink

    @pytest.mark.parametrize("outcome", ["unlinked", "not_linked"])
    def test_unlinks_the_signed_in_users_own_identity(
        self, client, signed_in, unlink, outcome
    ):
        unlink["outcome"] = outcome
        resp = client.delete(self.URL)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"outcome": outcome}
        assert unlink["user_id"] == PRINCIPAL.user_id

    def test_the_last_identity_is_refused_by_name(self, client, signed_in, unlink):
        unlink["outcome"] = "last_identity"
        resp = client.delete(self.URL)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "last_identity"


SRC = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def drive_configured(monkeypatch):
    """The Google client configured for the Drive leg."""
    from src.config.settings import settings

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "gid", raising=False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "sec", raising=False)
    monkeypatch.setattr(
        settings, "OAUTH_REDIRECT_BASE_URL", "https://api.example.test", raising=False
    )


class TestDriveConnect:
    """`POST /workspaces/{ws}/drive/connect` — the workspace connects Google
    Drive ONCE (owner ruling 2026-09-05, #1165 lean (b); `07` §15). Admin
    floor; the state pins the workspace as its own target, so last-issued-wins
    retires a stale connect per workspace."""

    URL = f"/api/v1/workspaces/{WS}/drive/connect"

    @pytest.fixture
    def purpose(self, monkeypatch):
        holder = {"value": "connect", "asked": None}

        async def connect_purpose(session, *, workspace_id):
            holder["asked"] = workspace_id
            return holder["value"]

        monkeypatch.setattr(google_drive_oauth, "connect_purpose", connect_purpose)
        return holder

    @pytest.fixture
    def issued(self, monkeypatch):
        """Records what the Drive leg's `issue_connect_state` asked
        `oauth_states.issue_state` to mint; returns a fixed state."""
        seen = {}

        async def issue_state(conn, **kw):
            seen.update(kw)
            return "st4te"

        monkeypatch.setattr(oauth_states, "issue_state", issue_state)
        return seen

    def test_mints_a_state_pinned_to_the_workspace_and_says_where_to_go(
        self, client, signed_in, tenant, drive_configured, purpose, issued
    ):
        resp = client.post(self.URL)
        assert resp.status_code == 200, resp.text
        url = resp.json()["authorization_url"]
        assert url.startswith("https://accounts.google.com/")
        assert "state=st4te" in url and "google-drive%2Fcallback" in url
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "admin"),
        ]
        assert purpose["asked"] == WS
        assert issued["purpose"] == "connect"
        assert issued["provider"] == google_drive_oauth.PROVIDER
        assert issued["reconnect_target"] == WS
        assert issued["workspace_id"] == WS and issued["user_id"] == PRINCIPAL.user_id

    @pytest.mark.parametrize("minted,moved,gates", CONNECT_FLOOR_CASES)
    def test_the_gates_follow_the_minted_purposes_floor(
        self,
        client,
        signed_in,
        tenant,
        drive_configured,
        purpose,
        issued,
        monkeypatch,
        minted,
        moved,
        gates,
    ):
        purpose["value"] = minted
        for kind, floor in moved.items():
            monkeypatch.setitem(commands.ROLE_FLOOR, kind, floor)
        resp = client.post(self.URL)
        assert resp.status_code == 200, resp.text
        assert tenant == [("uow", WS, PRINCIPAL.user_id)] + [
            ("gate", WS, PRINCIPAL.user_id, floor) for floor in gates
        ]

    def test_a_workspace_that_holds_a_grant_mints_a_reconnect(
        self, client, signed_in, tenant, drive_configured, purpose, issued
    ):
        purpose["value"] = "reconnect"
        assert client.post(self.URL).status_code == 200
        assert issued["purpose"] == "reconnect" and issued["reconnect_target"] == WS

    def test_the_url_carries_the_s256_challenge_of_the_verifier_minted_with_the_state(
        self, client, signed_in, tenant, drive_configured, purpose, issued
    ):
        """PKCE (RFC 7636, `07` §51): the verifier is stored with the state,
        and only its S256 challenge reaches the browser, in the URL."""
        resp = client.post(self.URL)
        assert resp.status_code == 200, resp.text
        verifier = issued["code_verifier"]
        assert verifier and verifier not in resp.text
        q = parse_qs(urlsplit(resp.json()["authorization_url"]).query)
        assert q["code_challenge"] == [oauth_states.code_challenge(verifier)]
        assert q["code_challenge_method"] == ["S256"]

    def test_unconfigured_google_refuses_503_before_any_seam(
        self, client, signed_in, tenant, purpose, issued, monkeypatch
    ):
        from src.config.settings import settings

        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None, raising=False)
        resp = client.post(self.URL)
        assert resp.status_code == 503
        assert "GOOGLE_CLIENT_ID" in resp.json()["detail"]
        assert tenant == [] and issued == {}

    def test_the_per_folder_connect_route_is_gone(self, client, signed_in, tenant):
        resp = client.post(f"/api/v1/workspaces/{WS}/sources/{SRC}/connect")
        assert resp.status_code in (404, 405)
        assert tenant == []


class TestDriveFolders:
    """`GET /workspaces/{ws}/drive/folders` — the browser the picker reads,
    through the workspace's grant. Admin floor: the grant is the workspace's,
    the tree is a person's Drive. Every refusal by name (review of #1246)."""

    URL = f"/api/v1/workspaces/{WS}/drive/folders"

    @pytest.fixture
    def browser(self, monkeypatch):
        from src.services.target.google_drive_adapter import FolderPage

        holder = {
            "folders": [{"id": "f1", "name": "Trips"}],
            "truncated": False,
            "raise": None,
            "asked": None,
            "status": "active",
            "mine": True,
        }

        class _Adapter:
            async def list_folders(self, *, parent, workspace_id):
                holder["asked"] = (parent, workspace_id)
                if holder["raise"] is not None:
                    raise holder["raise"]
                return FolderPage(holder["folders"], holder["truncated"])

        async def drive_status(session, *, workspace_id):
            return {"status": holder["status"], "connected_at": None}

        from src.api.routes import v1

        monkeypatch.setattr(v1, "_drive_adapter", lambda request: _Adapter())

        async def list_sources(session, *, workspace_id):
            return []

        async def may_browse_drive(session, *, workspace_id, user_id):
            holder["browser"] = (workspace_id, user_id)
            return holder["mine"]

        monkeypatch.setattr(workspaces, "drive_status", drive_status)
        monkeypatch.setattr(workspaces, "list_sources", list_sources)
        monkeypatch.setattr(workspaces, "may_browse_drive", may_browse_drive)
        return holder

    def test_lists_the_root_at_the_admin_floor(
        self, client, signed_in, tenant, browser
    ):
        resp = client.get(self.URL)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "parent": "root",
            "folders": [{"id": "f1", "name": "Trips"}],
            "truncated": False,
        }
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "admin"),
        ]
        assert browser["asked"] == (None, WS)

    def test_a_parent_narrows_the_listing(self, client, signed_in, tenant, browser):
        resp = client.get(self.URL + "?parent=abc_-1")
        assert resp.status_code == 200
        assert resp.json()["parent"] == "abc_-1" and browser["asked"] == ("abc_-1", WS)

    def test_the_shared_root_is_a_parent_the_route_admits(
        self, client, signed_in, tenant, browser
    ):
        resp = client.get(self.URL + "?parent=shared-with-me")
        assert resp.status_code == 200 and browser["asked"] == ("shared-with-me", WS)

    def test_a_parent_that_is_not_a_drive_id_is_400_after_admission(
        self, client, signed_in, tenant, browser
    ):
        resp = client.get(self.URL + "?parent=x'%20or%201")
        assert resp.status_code == 400 and resp.json()["detail"] == "invalid_parent"
        assert browser["asked"] is None
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    def test_never_connected_is_409_drive_not_connected_before_any_request(
        self, client, signed_in, tenant, browser
    ):
        browser["status"] = "none"
        resp = client.get(self.URL)
        assert (
            resp.status_code == 409 and resp.json()["detail"] == "drive_not_connected"
        )
        assert browser["asked"] is None

    @pytest.mark.parametrize("status", ["expired", "revoked"])
    def test_a_dead_grant_is_409_drive_reconnect_needed(
        self, client, signed_in, tenant, browser, status
    ):
        browser["status"] = status
        resp = client.get(self.URL)
        assert (
            resp.status_code == 409
            and resp.json()["detail"] == "drive_reconnect_needed"
        )
        assert browser["asked"] is None

    def test_google_refusing_a_live_looking_grant_is_409_drive_grant_refused(
        self, client, signed_in, tenant, browser
    ):
        browser["raise"] = media_sync.DriveCredentialDead("401 from drive")
        resp = client.get(self.URL)
        assert (
            resp.status_code == 409 and resp.json()["detail"] == "drive_grant_refused"
        )

    def test_google_unreachable_is_503(self, client, signed_in, tenant, browser):
        browser["raise"] = DriveRetryableError("503 from drive")
        resp = client.get(self.URL)
        assert resp.status_code == 503 and resp.json()["detail"] == "drive_unavailable"

    def test_a_cut_listing_says_so(self, client, signed_in, tenant, browser):
        browser["truncated"] = True
        assert client.get(self.URL).json()["truncated"] is True

    def test_an_admin_who_did_not_grant_it_is_403_drive_not_yours(
        self, client, signed_in, tenant, browser
    ):
        """091: the tree is the granter's Drive. Another admin passes the
        admin floor and is refused by name — with `reason`, the web's one
        code carrier — before any request reaches Google."""
        browser["mine"] = False
        resp = client.get(self.URL)
        assert resp.status_code == 403, resp.text
        assert resp.json()["reason"] == "drive_not_yours"
        assert browser["asked"] is None
        assert browser["browser"] == (WS, PRINCIPAL.user_id)
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    def test_a_dead_grant_says_reconnect_even_to_an_admin_who_did_not_grant_it(
        self, client, signed_in, tenant, browser
    ):
        """Reconnecting is every admin's remedy, and it makes them the
        granter, so the status refusal comes first."""
        browser["mine"] = False
        browser["status"] = "expired"
        resp = client.get(self.URL)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "drive_reconnect_needed"


class TestDriveStatus:
    """`GET /workspaces/{ws}/drive` — the workspace's grant, projected as the
    destinations' credentials are (`none` · `active` · `expired` · `revoked`)."""

    def test_reads_at_the_member_floor(self, client, signed_in, tenant, monkeypatch):
        async def drive_status(session, *, workspace_id):
            assert workspace_id == WS
            return {"status": "active", "connected_at": "2026-09-05T00:00:00+00:00"}

        monkeypatch.setattr(workspaces, "drive_status", drive_status)
        resp = client.get(f"/api/v1/workspaces/{WS}/drive")
        assert resp.status_code == 200
        assert resp.json() == {
            "drive": {"status": "active", "connected_at": "2026-09-05T00:00:00+00:00"}
        }
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "member"),
        ]


class TestSourcesUnderTheWorkspaceGrant:
    """`POST /workspaces/{ws}/sources` picks a folder UNDER the grant: refused
    by name without one, armed for its first sync with one. `DELETE` pauses a
    folder — never deletes it."""

    URL = f"/api/v1/workspaces/{WS}/sources"

    @pytest.fixture
    def grant(self, monkeypatch):
        holder = {"status": "active", "mine": True}

        async def drive_status(session, *, workspace_id):
            return {"status": holder["status"], "connected_at": None}

        async def list_sources(session, *, workspace_id):
            return []

        async def may_browse_drive(session, *, workspace_id, user_id):
            holder["browser"] = (workspace_id, user_id)
            answers = holder.get("answers")
            return answers.pop(0) if answers else holder["mine"]

        monkeypatch.setattr(workspaces, "drive_status", drive_status)
        monkeypatch.setattr(workspaces, "list_sources", list_sources)
        monkeypatch.setattr(workspaces, "may_browse_drive", may_browse_drive)
        return holder

    @pytest.fixture
    def created(self, monkeypatch):
        log = {}

        async def get_or_create_media_source(
            session, *, workspace_id, folder_ref, root_name=None, folder_name=None
        ):
            log["source"] = (workspace_id, folder_ref, root_name, folder_name)
            return SRC, True

        async def rearm_after_connect(session, *, workspace_id, source_id=None):
            log["armed"] = (workspace_id, source_id)
            return True

        async def assert_sources_unchanged(session, *, workspace_id, expected):
            log["rechecked"] = expected

        monkeypatch.setattr(
            provisioning, "get_or_create_media_source", get_or_create_media_source
        )
        monkeypatch.setattr(
            provisioning, "assert_sources_unchanged", assert_sources_unchanged
        )
        monkeypatch.setattr(media_sync, "rearm_after_connect", rearm_after_connect)
        return log

    def test_a_picked_folder_is_created_named_and_armed(
        self, client, signed_in, tenant, grant, created
    ):
        resp = client.post(self.URL, json={"folder_ref": "f1", "folder_name": "Trips"})
        assert resp.status_code == 201, resp.text
        assert resp.json() == {"source_id": SRC, "created": True}
        assert created["source"] == (WS, "f1", None, "Trips")
        assert created["armed"] == (WS, SRC)
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    def test_without_a_usable_grant_nothing_is_created(
        self, client, signed_in, tenant, grant, created
    ):
        grant["status"] = "none"
        resp = client.post(self.URL, json={"folder_ref": "f1"})
        assert (
            resp.status_code == 409 and resp.json()["detail"] == "drive_not_connected"
        )
        assert created == {}

    def test_an_admin_who_did_not_grant_it_adds_nothing(
        self, client, signed_in, tenant, grant, created
    ):
        """091: a pick reads the granter's Drive and adds a folder from it, so
        it is the folder browser's rule — refused by name, nothing created."""
        grant["mine"] = False
        resp = client.post(self.URL, json={"folder_ref": "f1", "folder_name": "Trips"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["reason"] == "drive_not_yours"
        assert grant["browser"] == (WS, PRINCIPAL.user_id)
        assert created == {}

    def test_a_reconnect_by_someone_else_mid_pick_is_403_drive_not_yours(
        self, client, signed_in, tenant, grant, created
    ):
        """The browse check is asked again in the writing unit of work: a
        reconnect between the two makes someone else the granter."""
        grant["answers"] = [True, False]
        resp = client.post(self.URL, json={"folder_ref": "f1", "folder_name": "Trips"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["reason"] == "drive_not_yours"
        assert created == {}

    @pytest.fixture
    def connected(self, monkeypatch):
        """One active source already here — folder PARENT, named Trips — and a
        Drive whose parent chains are scripted per folder id."""
        chains = {
            "CHILD": ["PARENT", "ROOT"],
            "PARENT": ["GRAND", "ROOT"],
            "GRAND": ["ROOT"],
            "ELSEWHERE": ["ROOT"],
        }

        class _Drive:
            asked = []

            async def folder_ancestors(self, *, workspace_id, folder_ref):
                self.asked.append(folder_ref)
                if folder_ref == "DEAD":
                    raise media_sync.DriveSourceGone("gone in Drive")
                return list(chains.get(folder_ref, []))

        async def list_sources(session, *, workspace_id):
            return [
                {
                    "id": SRC,
                    "provider": "gdrive",
                    "state": "active",
                    "folder_ref": "PARENT",
                    "folder_name": "Trips",
                    "removed": False,
                },
                {
                    "id": "old",
                    "provider": "gdrive",
                    "state": "paused",
                    "folder_ref": "GONE",
                    "folder_name": "Old",
                    "removed": True,
                },
                {
                    "id": "dead",
                    "provider": "gdrive",
                    "state": "error",
                    "folder_ref": "DEAD",
                    "folder_name": "Dead",
                    "removed": False,
                },
            ]

        from src.api.routes import v1

        monkeypatch.setattr(v1, "_drive_adapter", lambda request: _Drive())
        monkeypatch.setattr(workspaces, "list_sources", list_sources)
        return _Drive

    def test_a_folder_inside_a_connected_one_is_refused_by_name(
        self, client, signed_in, tenant, grant, created, connected
    ):
        resp = client.post(
            self.URL, json={"folder_ref": "CHILD", "folder_name": "Summer"}
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["reason"] == "source_nested"
        assert "Trips" in resp.json()["detail"], "the refusal names the folder"
        assert created == {}

    def test_a_folder_that_contains_a_connected_one_is_refused_too(
        self, client, signed_in, tenant, grant, created, connected
    ):
        resp = client.post(
            self.URL, json={"folder_ref": "GRAND", "folder_name": "Everything"}
        )
        assert resp.status_code == 409 and resp.json()["reason"] == "source_nested"
        assert created == {}

    def test_a_disjoint_folder_and_a_re_pick_are_allowed(
        self, client, signed_in, tenant, grant, created, connected
    ):
        assert (
            client.post(
                self.URL, json={"folder_ref": "ELSEWHERE", "folder_name": "Other"}
            ).status_code
            == 201
        )
        before = len(connected.asked)
        assert (
            client.post(
                self.URL, json={"folder_ref": "PARENT", "folder_name": "Trips"}
            ).status_code
            == 201
        )
        assert len(connected.asked) == before, (
            "a re-pick of the same folder asks no chain"
        )

    def test_a_connected_folder_gone_in_drive_does_not_fail_an_unrelated_pick(
        self, client, signed_in, tenant, grant, created, connected
    ):
        resp = client.post(
            self.URL, json={"folder_ref": "ELSEWHERE", "folder_name": "Other"}
        )
        assert resp.status_code == 201, resp.text
        assert "DEAD" in connected.asked, (
            "the dead folder was checked, and contains nothing"
        )

    def test_folders_that_changed_between_the_check_and_the_write_are_refused(
        self, client, signed_in, tenant, grant, created, connected, monkeypatch
    ):
        async def assert_sources_unchanged(session, *, workspace_id, expected):
            raise provisioning.ProvisioningRefused("sources_changed", "changed")

        monkeypatch.setattr(
            provisioning, "assert_sources_unchanged", assert_sources_unchanged
        )
        resp = client.post(
            self.URL, json={"folder_ref": "FRESH", "folder_name": "Fresh"}
        )
        assert resp.status_code == 409 and resp.json()["reason"] == "sources_changed"
        assert "source" not in created

    def test_a_removed_source_does_not_block_a_pick_inside_it(
        self, client, signed_in, tenant, grant, created, connected
    ):
        # GONE is paused (removed); its chain is never asked, and a folder that
        # was under it is a fresh pick.
        resp = client.post(self.URL, json={"folder_ref": "ELSEWHERE"})
        assert resp.status_code == 201
        assert "GONE" not in connected.asked

    def test_removing_a_folder_pauses_it(self, client, signed_in, tenant, monkeypatch):
        asked = {}

        async def pause_media_source(session, *, workspace_id, source_id):
            asked["pause"] = (workspace_id, source_id)
            return True

        monkeypatch.setattr(provisioning, "pause_media_source", pause_media_source)
        resp = client.delete(f"{self.URL}/{SRC}")
        assert resp.status_code == 200
        assert resp.json() == {"source_id": SRC, "state": "paused"}
        assert asked["pause"] == (WS, SRC)
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    def test_removing_a_folder_that_is_not_here_is_404(
        self, client, signed_in, tenant, monkeypatch
    ):
        async def pause_media_source(session, *, workspace_id, source_id):
            return False

        monkeypatch.setattr(provisioning, "pause_media_source", pause_media_source)
        assert client.delete(f"{self.URL}/{SRC}").status_code == 404


class TestCategoryMix:
    """`GET`/`PUT /workspaces/{ws}/category-mix` — the weights that drive the
    slot draw, keyed on the CONNECTED FOLDER (owner ruling 2026-09-08). Member
    read, admin write; the v1 keys ride along for one release so the card
    already deployed keeps working across the API/web deploy window; a
    refusal travels as `reason = invalid_mix_<reason>`."""

    URL = f"/api/v1/workspaces/{WS}/category-mix"
    ROWS = [
        {
            "source_id": "11111111-1111-4111-8111-111111111111",
            "provider": "gdrive",
            "name": "memes",
            "state": "active",
            "media_count": 30,
            "ratio": 0.7,
            "effective": 70.0,
        },
        {
            "source_id": "22222222-2222-4222-8222-222222222222",
            "provider": "gdrive",
            "name": "merch",
            "state": "active",
            "media_count": 10,
            "ratio": 0.3,
            "effective": 30.0,
        },
    ]

    def test_get_reads_the_view_and_carries_ONLY_rows(
        self, client, signed_in, tenant, monkeypatch
    ):
        """`rows` is the whole shape (#1263).

        The v1 keys `mix` (by folder NAME) and `categories` rode along for one
        release so the API could deploy ahead of the web. The web has been on
        `rows` since #1262's card shipped and reads no other field, so they are
        gone — and asserted ABSENT rather than simply unasserted, because an
        unasserted key is one a later change can quietly put back."""

        async def mix_view(session, *, workspace_id):
            return list(self.ROWS)

        monkeypatch.setattr(category_mix, "mix_view", mix_view)
        resp = client.get(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"rows": self.ROWS}, (
            "the response is rows and nothing else — no v1 keys, no explicit_total"
        )
        assert ("gate", WS, PRINCIPAL.user_id, "member") in tenant

    def test_put_by_source_replaces_the_mix_at_the_admin_floor(
        self, client, signed_in, tenant, monkeypatch
    ):
        seen = {}

        async def set_mix(session, *, workspace_id, mix, by_user_id):
            seen.update(ws=workspace_id, mix=mix, by=by_user_id)
            return [
                {"source_id": "11111111-1111-4111-8111-111111111111", "ratio": 0.7},
                {"source_id": "22222222-2222-4222-8222-222222222222", "ratio": 0.3},
            ]

        async def mix_view(session, *, workspace_id):
            return list(self.ROWS)

        monkeypatch.setattr(category_mix, "set_mix", set_mix)
        monkeypatch.setattr(category_mix, "mix_view", mix_view)
        body = {
            "rows": [
                {"source_id": "11111111-1111-4111-8111-111111111111", "ratio": 0.7},
                {"source_id": "22222222-2222-4222-8222-222222222222", "ratio": 0.3},
            ]
        }
        resp = client.put(self.URL, json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["rows"] == self.ROWS, "the PUT answers with the GET shape"
        assert seen == {"ws": WS, "mix": body["rows"], "by": PRINCIPAL.user_id}
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant

    def test_a_refused_mix_is_400_with_the_reason_the_web_reads(
        self, client, signed_in, tenant, monkeypatch
    ):
        async def set_mix(session, *, workspace_id, mix, by_user_id):
            raise category_mix.MixInvalid(
                "unknown_source", "22222222-2222-4222-8222-222222222222"
            )

        monkeypatch.setattr(category_mix, "set_mix", set_mix)
        resp = client.put(
            self.URL,
            json={
                "rows": [
                    {"source_id": "22222222-2222-4222-8222-222222222222", "ratio": 1.0}
                ]
            },
        )
        assert resp.status_code == 400
        assert resp.json()["reason"] == "invalid_mix_unknown_source"


class TestMediaThumbnail:
    """`GET /workspaces/{ws}/media/{media_id}/thumbnail` (#1634): the picture,
    fetched on the server through the row's stored link, behind the member
    gate the media reads use. Every way of having no picture is the 404 an
    unknown item gets, and the page draws its placeholder."""

    URL = f"/api/v1/workspaces/{WS}/media/{INTENT}/thumbnail"
    SOURCE = "55555555-5555-5555-5555-555555555555"
    LINK = "https://lh3.googleusercontent.com/drive-storage/thumb-1=s220"

    @pytest.fixture
    def drive(self, monkeypatch):
        from src.api.routes import v1
        from src.services.target.google_drive_adapter import Thumbnail

        holder = {
            "row": {
                "source_id": self.SOURCE,
                "provider_file_ref": "FILE1",
                "thumbnail_url": self.LINK,
            },
            "thumbnail": Thumbnail(b"\xff\xd8picture", "image/jpeg"),
            "raise": None,
            "read": [],
            "asked": [],
            "events": [],
        }

        async def thumbnail_link(session, *, workspace_id, media_id):
            holder["read"].append((workspace_id, media_id))
            return holder["row"]

        class _Adapter:
            async def fetch_thumbnail(self, *, source_id, workspace_id, file_ref, link):
                holder["events"].append("drive")
                holder["asked"].append((source_id, workspace_id, file_ref, link))
                if holder["raise"] is not None:
                    raise holder["raise"]
                return holder["thumbnail"]

        monkeypatch.setattr(workspaces, "thumbnail_link", thumbnail_link)
        monkeypatch.setattr(v1, "_drive_adapter", lambda request: _Adapter())
        return holder

    def test_serves_the_picture_at_the_member_floor_for_private_keeping(
        self, client, signed_in, tenant, drive
    ):
        resp = client.get(self.URL)
        assert resp.status_code == 200, resp.text
        assert resp.content == b"\xff\xd8picture"
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["cache-control"] == "private, max-age=2592000"
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "member"),
        ]
        assert drive["read"] == [(WS, INTENT)]
        assert drive["asked"] == [(self.SOURCE, WS, "FILE1", self.LINK)]

    def test_a_member_of_another_workspace_gets_404_and_drive_is_never_asked(
        self, client, signed_in, tenant, drive
    ):
        tenant.refuse = TenantResolutionError("not_a_member")
        resp = client.get(self.URL)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}
        assert drive["read"] == [] and drive["asked"] == []

    def test_an_item_the_workspace_does_not_hold_is_404(
        self, client, signed_in, tenant, drive
    ):
        drive["row"] = None
        assert client.get(self.URL).status_code == 404
        assert drive["asked"] == []

    def test_an_item_with_no_link_is_404_and_drive_is_never_asked(
        self, client, signed_in, tenant, drive
    ):
        drive["row"] = {**drive["row"], "thumbnail_url": None}
        assert client.get(self.URL).status_code == 404
        assert drive["asked"] == []

    def test_no_picture_from_drive_is_the_same_404(
        self, client, signed_in, tenant, drive
    ):
        drive["thumbnail"] = None
        resp = client.get(self.URL)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}

    @pytest.mark.parametrize(
        ("exc", "status", "detail"),
        [
            (media_sync.DriveCredentialDead("revoked"), 409, "drive_grant_refused"),
            (DriveRetryableError("quota"), 503, "drive_unavailable"),
        ],
    )
    def test_drive_refusals_are_named_the_way_every_drive_route_names_them(
        self, client, signed_in, tenant, drive, exc, status, detail
    ):
        drive["raise"] = exc
        resp = client.get(self.URL)
        assert resp.status_code == status
        assert resp.json()["detail"] == detail

    def test_the_database_session_closes_before_drive_is_asked(
        self, client, signed_in, tenant, drive, engine, monkeypatch
    ):
        from contextlib import asynccontextmanager

        from src.api import principal

        @asynccontextmanager
        async def open_tenant(request, workspace_id, who):
            drive["events"].append("session open")
            yield engine.session
            drive["events"].append("session closed")

        monkeypatch.setattr(principal, "open_tenant", open_tenant)
        assert client.get(self.URL).status_code == 200
        assert drive["events"] == ["session open", "session closed", "drive"]

    def test_a_non_uuid_media_id_is_refused_before_any_seam(
        self, client, signed_in, tenant, drive
    ):
        resp = client.get(f"/api/v1/workspaces/{WS}/media/not-a-uuid/thumbnail")
        assert resp.status_code == 422
        assert drive["read"] == [] and drive["asked"] == []
