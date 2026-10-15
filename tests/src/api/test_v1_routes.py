"""`/api/v1` — the adapter's contract, with every service seam patched.

What is pinned: authentication is required and cookie/bearer are one
credential; a non-member gets the same 404 a missing workspace gets; the
command route refuses an unknown name BEFORE admission, requires the
idempotency key, admits then executes, and maps every refusal the port can
raise to its status in `src/api/app.py`. The database path is
`tests/scripts/test_web_router_x2_gate.py`.
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

    @pytest.mark.parametrize(
        "ops_user_ids, ops",
        [
            (PRINCIPAL.user_id, True),
            ("55555555-5555-4555-8555-555555555555", False),
            ("", False),
        ],
        ids=["listed", "another", "nobody"],
    )
    def test_me_says_whether_the_person_is_an_operator(
        self, client, signed_in, user_plane, monkeypatch, ops_user_ids, ops
    ):
        """`ops` is `OPS_USER_IDS` asked of the person (#1723): their own
        fact, a boolean either way, and it opens nothing."""
        from src.config.settings import settings

        monkeypatch.setattr(settings, "OPS_USER_IDS", ops_user_ids)
        body = client.get("/api/v1/me").json()
        assert body["ops"] is ops
        assert set(body) == {"user", "workspaces", "ops"}

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
        endpoint of its own (#1413)."""
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

    def test_after_commit_work_runs_once_the_unit_of_work_has_closed(
        self, client, signed_in, port, monkeypatch, app
    ):
        """The invitation email: `_dispatch` runs the send after its unit of
        work commits, with the app's sender, and the send's answer replaces
        the `delivery` the person reads."""
        from contextlib import asynccontextmanager

        from src.api import principal

        order = []

        @asynccontextmanager
        async def open_tenant(request, workspace_id, who):
            order.append("open")
            yield object()
            order.append("committed")

        class Email:
            async def run(self, sender, engine):
                order.append("send")
                assert sender is app.state.email
                return {"delivery": {"channel": "email", "state": "sent"}}

        monkeypatch.setattr(principal, "open_tenant", open_tenant)
        port["outcome"] = CommandResult(
            "executed",
            {"invitation_id": "inv-1", "delivery": {"channel": "email"}},
            after_commit=Email(),
        )
        resp = client.post(
            f"/api/v1/workspaces/{WS}/commands/invite_member",
            json={"email": "a@b.co"},
            headers=KEY,
        )
        assert resp.status_code == 200
        assert resp.json()["invitation_id"] == "inv-1"
        assert resp.json()["delivery"] == {"channel": "email", "state": "sent"}
        assert order == ["open", "committed", "send"]

    def test_a_failed_commit_sends_nothing(self, client, signed_in, port, monkeypatch):
        from contextlib import asynccontextmanager

        from src.api import principal

        @asynccontextmanager
        async def open_tenant(request, workspace_id, who):
            yield object()
            raise ConnectionResetError("the commit never landed")

        class Email:
            async def run(self, sender, engine):  # pragma: no cover - must not run
                raise AssertionError("an invitation that never committed was sent")

        monkeypatch.setattr(principal, "open_tenant", open_tenant)
        port["outcome"] = CommandResult("executed", {}, after_commit=Email())
        with pytest.raises(ConnectionResetError):
            client.post(
                f"/api/v1/workspaces/{WS}/commands/invite_member",
                json={"email": "a@b.co"},
                headers=KEY,
            )

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
    Login grant for ONE destination (#1220, #1041). The Drive connect
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
    grant for an account that is NOT yet a destination (destinations are
    added by connecting). Admin floor; the state pins the user and the
    workspace and NO target — the callback adopts or creates the destination
    from the identity Instagram returns."""

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
        resp = client.post(self.URL, json={"handle": "@Example.Brand"})
        assert resp.status_code == 201, resp.text
        assert resp.json() == {
            "account_id": ACCOUNT,
            "created": True,
            "scheduled": True,
        }
        assert written == [(WS, None, "@Example.Brand", True)]
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
    workspace (#1175: a token, and the same flow for the Nth group). Admin
    floor: deciding where a workspace's cards go is an admin act."""

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
    group: a revoke, never a delete, at the admin floor like the
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
    their Telegram identity (only from an authenticated session; the row pins
    the user). The service half is `identity_link` (#1180); this is its route
    (#1172, #1157)."""

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
        """A trailing period in the bot setting makes the API mint
        `t.me/storydump_app_bot.?start=…`, which the site refuses as a
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
    identity (099). Tenant-less; the door's outcome is the answer,
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


class TestTheInviteesOwnInvitations:
    """`/me/invitations` (115): the signed-in person's invitations,
    and "Email me the link again". Tenant-less, like `/me`; the service's answer
    is the response, and an invitation that is not theirs is one 404."""

    INVITATION = "3f8a1c2e-5b4d-4e6f-9a1b-7c2d3e4f5a6b"
    RESEND = f"/api/v1/me/invitations/{INVITATION}/resend"

    @pytest.fixture
    def seen(self, monkeypatch):
        seen = {}

        async def list_for_invitee(conn, *, user_id):
            seen["listed_for"] = user_id
            return [{"invitation_id": self.INVITATION, "workspace_name": "Acme"}]

        async def reissue(engine, sender, *, user_id, invitation_id, web_app_origin):
            seen["reissue"] = (sender, user_id, invitation_id, web_app_origin)
            if "refuse" in seen:
                raise seen["refuse"]
            return {"delivery": {"channel": "email", "state": "sent"}}

        monkeypatch.setattr(invitations, "list_for_invitee", list_for_invitee)
        monkeypatch.setattr(invitations, "reissue", reissue)
        return seen

    def test_both_need_a_session(self, client, seen):
        assert client.get("/api/v1/me/invitations").status_code == 401
        assert client.post(self.RESEND).status_code == 401
        assert seen == {}

    def test_it_lists_the_signed_in_persons_own(self, client, signed_in, seen):
        resp = client.get("/api/v1/me/invitations")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "invitations": [
                {"invitation_id": self.INVITATION, "workspace_name": "Acme"}
            ],
            "resend": False,
        }
        assert seen["listed_for"] == PRINCIPAL.user_id

    @pytest.mark.parametrize(
        "sender, origin, resend",
        [
            (object(), "https://app.example.test", True),
            (None, "https://app.example.test", False),
            (object(), None, False),
        ],
    )
    def test_it_says_whether_the_link_can_be_sent_again_here(
        self, client, signed_in, seen, app, monkeypatch, sender, origin, resend
    ):
        from src.config.settings import settings

        monkeypatch.setattr(settings, "WEB_APP_URL", origin)
        monkeypatch.setattr(app.state, "email", sender)
        assert client.get("/api/v1/me/invitations").json()["resend"] is resend

    def test_a_resend_uses_the_apps_sender_and_the_deployments_origin(
        self, client, signed_in, seen, app, monkeypatch
    ):
        from src.config.settings import settings

        monkeypatch.setattr(settings, "WEB_APP_URL", "https://app.example.test")
        resp = client.post(self.RESEND)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"delivery": {"channel": "email", "state": "sent"}}
        assert seen["reissue"] == (
            app.state.email,
            PRINCIPAL.user_id,
            self.INVITATION,
            "https://app.example.test",
        )

    def test_an_invitation_that_is_not_theirs_is_one_404(self, client, signed_in, seen):
        seen["refuse"] = invitations.InvitationRefused("not_acceptable")
        assert client.post(self.RESEND).status_code == 404

    def test_an_id_that_is_not_a_uuid_never_reaches_the_service(
        self, client, signed_in, seen
    ):
        assert client.post("/api/v1/me/invitations/nope/resend").status_code == 422
        assert "reissue" not in seen


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
    Drive ONCE (#1165). Admin floor; the state pins the workspace
    as its own target, so last-issued-wins retires a stale connect per
    workspace."""

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
        """PKCE (RFC 7636): the verifier is stored with the state,
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
    the tree is a person's Drive. Every refusal by name (#1246)."""

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

    @pytest.fixture(autouse=True)
    def legacy_landing(self, monkeypatch):
        # Adapter tests retain provider/ancestry seams; real versioned landing is DB-tested.
        async def connect(session, *, workspace_id, body):
            sid, created = await provisioning.get_or_create_media_source(
                session,
                workspace_id=workspace_id,
                folder_ref=body.get("folder_ref"),
                root_name=body.get("root_name"),
                folder_name=body.get("folder_name"),
            )
            await media_sync.rearm_after_connect(
                session, workspace_id=workspace_id, source_id=sid
            )
            return {"source_id": sid, "created": created}

        async def remove(session, *, workspace_id, source_id, body):
            paused = await provisioning.pause_media_source(
                session, workspace_id=workspace_id, source_id=source_id
            )
            return {"source_id": source_id, "state": "paused"} if paused else None

        monkeypatch.setattr(category_mix, "connect_drive", connect)
        monkeypatch.setattr(category_mix, "remove_drive", remove)

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
    slot draw, keyed on the CONNECTED FOLDER. Member read, admin write. The
    GET, and the protocol-1 PUT of `rows`, answer with `category_mix.resource`
    as it comes; the autouse `legacy_resource` makes that an unconverted
    workspace's protocol-1 card: `rows`, `protocol_version`,
    `supported_actions` and `conversion`, and nothing else. A refusal travels
    as `reason = invalid_mix_<reason>`."""

    @pytest.fixture(autouse=True)
    def legacy_resource(self, monkeypatch):
        async def resource(session, *, workspace_id, **kwargs):
            return {
                "rows": await category_mix.mix_view(session, workspace_id=workspace_id),
                "protocol_version": 1,
                "supported_actions": ["legacy_save"],
                "conversion": {"state": "pending"},
            }

        monkeypatch.setattr(category_mix, "resource", resource)

    URL = f"/api/v1/workspaces/{WS}/category-mix"
    ROWS = [
        {
            "source_id": "11111111-1111-4111-8111-111111111111",
            "provider": "gdrive",
            "name": "announcements",
            "state": "active",
            "media_count": 30,
            "ratio": 0.7,
            "effective": 70.0,
        },
        {
            "source_id": "22222222-2222-4222-8222-222222222222",
            "provider": "gdrive",
            "name": "behind-the-scenes",
            "state": "active",
            "media_count": 10,
            "ratio": 0.3,
            "effective": 30.0,
        },
    ]

    def test_get_reads_the_view_and_carries_ONLY_the_protocol_1_card(
        self, client, signed_in, tenant, monkeypatch
    ):
        """The body is the protocol-1 card's four keys and nothing else (#1263).

        The keys `mix` (by folder NAME) and `categories` are not in it: the
        web reads `protocol_version` to choose the card, and the protocol-1
        card (#1262) reads `rows`. They are asserted ABSENT rather than simply
        unasserted, because an unasserted key is one a change can quietly
        add."""

        async def mix_view(session, *, workspace_id):
            return list(self.ROWS)

        monkeypatch.setattr(category_mix, "mix_view", mix_view)
        resp = client.get(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "rows": self.ROWS,
            "protocol_version": 1,
            "supported_actions": ["legacy_save"],
            "conversion": {"state": "pending"},
        }, "the response is the protocol-1 card's four keys and nothing else"
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
                "provider": "gdrive",
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

    def test_a_drive_item_ignores_the_version_and_never_asks_the_archive_port(
        self, app, client, signed_in, tenant, drive
    ):
        app.state.archive_preview = _FailOnCall()
        resp = client.get(self.URL + "?v=0123456789abcdef")
        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "private, max-age=2592000"
        assert drive["asked"] == [(self.SOURCE, WS, "FILE1", self.LINK)]


class _FailOnCall:
    """An archive preview port that must not be reached."""

    async def preview(self, **kwargs):
        raise AssertionError(f"the archive preview port was asked: {kwargs}")


class TestArchiveThumbnail:
    """`GET …/media/{media_id}/thumbnail` for an archive item (#1644):
    an explicit preview through the port a trusted composition root injects,
    asked only with the row's current `thumbnail_version`. Every answer is
    no-store and carries no validator, so a browser keeps nothing and is
    never told 304; the admission and fence behind the port are the gate's,
    `tests/scripts/test_archive_preview_gate.py`."""

    URL = f"/api/v1/workspaces/{WS}/media/{INTENT}/thumbnail"
    VERSION = "66666666-6666-4666-8666-666666666666.3"
    NO_STORE = "private, no-store, max-age=0"
    PICTURE = b"\xff\xd8\xff\xe0preview"
    #: Every header a served preview carries: the picture's own three and the
    #: security middleware's — no ETag, Last-Modified, Location or upstream URL.
    #: Not `Vary`: the CORS middleware sets `Vary: Origin` on every response in
    #: the Starlette releases that do (the test clients send no Origin), so
    #: whether it is there depends on the release installed, not on the route.
    HEADERS = {
        "content-length",
        "content-type",
        "cache-control",
        "x-content-type-options",
        "strict-transport-security",
        "referrer-policy",
        "x-frame-options",
        "content-security-policy",
    }

    @pytest.fixture
    def archive(self, app, monkeypatch):
        from src.api.routes import v1

        holder = {
            "row": {
                "source_id": "55555555-5555-5555-5555-555555555555",
                "provider": "instagram_archive",
                "provider_file_ref": "17890000000000001",
                "thumbnail_url": None,
            },
            "content": self.PICTURE,
            "read": [],
            "asked": [],
            "events": [],
        }

        async def thumbnail_link(session, *, workspace_id, media_id):
            holder["read"].append((workspace_id, media_id))
            return holder["row"]

        class _Preview:
            async def preview(self, *, engine, workspace_id, media_id, version):
                holder["events"].append("preview")
                holder["asked"].append((workspace_id, media_id, version))
                return holder["content"]

        def no_drive(request):
            raise AssertionError("an archive item never asks Drive")

        monkeypatch.setattr(workspaces, "thumbnail_link", thumbnail_link)
        monkeypatch.setattr(v1, "_drive_adapter", no_drive)
        app.state.archive_preview = _Preview()
        return holder

    def test_serves_the_preview_no_store_with_exactly_its_headers(
        self, client, signed_in, tenant, archive
    ):
        resp = client.get(self.URL, params={"v": self.VERSION})
        assert resp.status_code == 200, resp.text
        assert resp.content == self.PICTURE
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["cache-control"] == self.NO_STORE
        assert set(resp.headers.keys()) - {"vary"} == self.HEADERS
        assert resp.headers.get("vary", "Origin") == "Origin"
        assert archive["asked"] == [(WS, INTENT, self.VERSION)]
        assert tenant == [
            ("uow", WS, PRINCIPAL.user_id),
            ("gate", WS, PRINCIPAL.user_id, "member"),
        ]

    def test_a_conditional_request_is_answered_in_full_never_304(
        self, client, signed_in, tenant, archive
    ):
        resp = client.get(
            self.URL,
            params={"v": self.VERSION},
            headers={
                "If-None-Match": "*",
                "If-Modified-Since": "Mon, 12 Oct 2026 00:00:00 GMT",
            },
        )
        assert resp.status_code == 200
        assert resp.content == self.PICTURE
        assert "etag" not in resp.headers and "last-modified" not in resp.headers

    def test_a_refused_preview_is_the_no_store_404(
        self, client, signed_in, tenant, archive
    ):
        archive["content"] = None
        resp = client.get(self.URL, params={"v": self.VERSION})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}
        assert resp.headers["cache-control"] == self.NO_STORE
        assert "etag" not in resp.headers and "last-modified" not in resp.headers
        assert archive["asked"] == [(WS, INTENT, self.VERSION)]

    @pytest.mark.parametrize("query", ["", "?v=", "?other=1"])
    def test_without_a_version_the_port_is_never_asked(
        self, app, client, signed_in, tenant, archive, query
    ):
        app.state.archive_preview = _FailOnCall()
        resp = client.get(self.URL + query)
        assert resp.status_code == 404
        assert resp.headers["cache-control"] == self.NO_STORE

    def test_without_the_injected_port_it_is_404_and_nothing_is_asked(
        self, app, client, signed_in, tenant, archive
    ):
        del app.state.archive_preview
        resp = client.get(self.URL, params={"v": self.VERSION})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}
        assert resp.headers["cache-control"] == self.NO_STORE
        assert archive["read"] == [(WS, INTENT)] and archive["asked"] == []

    def test_a_member_of_another_workspace_gets_404_and_nothing_is_asked(
        self, client, signed_in, tenant, archive
    ):
        tenant.refuse = TenantResolutionError("not_a_member")
        resp = client.get(self.URL, params={"v": self.VERSION})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}
        assert archive["read"] == [] and archive["asked"] == []

    def test_an_item_the_workspace_does_not_hold_never_asks_the_port(
        self, app, client, signed_in, tenant, archive
    ):
        archive["row"] = None
        app.state.archive_preview = _FailOnCall()
        assert client.get(self.URL, params={"v": self.VERSION}).status_code == 404

    def test_the_database_session_closes_before_the_preview_is_asked(
        self, client, signed_in, tenant, archive, engine, monkeypatch
    ):
        from contextlib import asynccontextmanager

        from src.api import principal

        @asynccontextmanager
        async def open_tenant(request, workspace_id, who):
            archive["events"].append("session open")
            yield engine.session
            archive["events"].append("session closed")

        monkeypatch.setattr(principal, "open_tenant", open_tenant)
        assert client.get(self.URL, params={"v": self.VERSION}).status_code == 200
        assert archive["events"] == ["session open", "session closed", "preview"]


class TestSourcePolicy:
    URL = f"/api/v1/workspaces/{WS}/sources/11111111-1111-4111-8111-111111111111/policy"

    def test_member_get_and_admin_put_return_the_same_complete_envelope(
        self, client, signed_in, tenant, monkeypatch
    ):
        from src.services.target import source_policy

        envelope = source_policy.project_policy(source_policy.policy_defaults(), None)
        calls = []

        async def get_policy(session, **kw):
            calls.append(("get", kw))
            return envelope

        async def put_policy(session, **kw):
            calls.append(("put", kw))
            return envelope

        monkeypatch.setattr(source_policy, "get_policy", get_policy)
        monkeypatch.setattr(source_policy, "put_policy", put_policy)
        assert client.get(self.URL).json() == envelope
        body = dict(
            schema_version=1,
            expected_revision=1,
            minimum_age_days=90,
            include_credited=False,
            include_unknown=False,
        )
        assert client.put(self.URL, json=body).json() == envelope
        assert calls[1][1]["body"] == body
        assert ("gate", WS, PRINCIPAL.user_id, "member") in tenant
        assert ("gate", WS, PRINCIPAL.user_id, "admin") in tenant
        assert calls[0][1]["capability"] is None

    @pytest.mark.parametrize(
        "reason,status",
        [
            ("policy_conflict", 409),
            ("unsupported_source_policy", 400),
            ("invalid_source_policy", 400),
            ("source_not_found", 404),
            ("unsupported_source_provider", 400),
            ("archive_not_activated", 503),
        ],
    )
    def test_closed_refusals_keep_current_revision(
        self, client, signed_in, tenant, monkeypatch, reason, status
    ):
        from src.services.target import source_policy

        async def put_policy(*args, **kw):
            raise source_policy.PolicyRefused(
                reason, current_revision=5 if reason == "policy_conflict" else None
            )

        monkeypatch.setattr(source_policy, "put_policy", put_policy)
        response = client.put(
            self.URL,
            json=dict(
                schema_version=1,
                expected_revision=1,
                minimum_age_days=90,
                include_credited=False,
                include_unknown=False,
            ),
        )
        assert response.status_code == status
        assert response.json()["reason"] == reason
        if reason == "policy_conflict":
            assert response.json()["current_revision"] == 5
        else:
            assert "current_revision" not in response.json()

    @pytest.mark.parametrize("raw", ["[]", "null", "bad-json", ""])
    def test_malformed_policy_body_is_named_invalid(
        self, client, signed_in, tenant, raw
    ):
        response = client.put(
            self.URL, content=raw, headers={"Content-Type": "application/json"}
        )
        assert (
            response.status_code == 400
            and response.json()["reason"] == "invalid_source_policy"
        )

    @pytest.mark.parametrize("raw", ["[]", "null", "bad-json"])
    def test_an_unreadable_policy_body_opens_no_session(
        self, client, signed_in, tenant, raw
    ):
        """Refused with the body in hand and before the unit of work opens, so
        before a pooled connection is taken."""
        response = client.put(
            self.URL, content=raw, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert response.json()["reason"] == "invalid_source_policy"
        assert tenant == []

    def test_the_policy_body_is_read_before_the_session_opens(
        self, client, signed_in, tenant, monkeypatch
    ):
        from src.api import principal
        from src.services.target import source_policy

        async def put_policy(session, **kw):
            return {}

        read = principal.json_object

        async def spy(request):
            tenant.append(("body",))
            return await read(request)

        monkeypatch.setattr(source_policy, "put_policy", put_policy)
        monkeypatch.setattr(principal, "json_object", spy)
        response = client.put(self.URL, json={})
        assert response.status_code == 200
        steps = [step[0] for step in tenant]
        assert steps.count("body") == 1
        assert steps[:2] == ["body", "uow"]

    def test_policy_refusal_mapping_is_total(self):
        from src.api.app import _SOURCE_POLICY_STATUS
        from src.services.target.source_policy import REASONS

        assert set(_SOURCE_POLICY_STATUS) == set(REASONS)


class TestVersionedMixResource:
    def test_get_preserves_full_authoritative_resource(
        self, client, signed_in, tenant, monkeypatch
    ):
        response = {
            "protocol_version": 2,
            "revision": 8,
            "mode": "custom",
            "rows": [],
            "destinations": [],
            "conversion": {"state": "complete"},
        }

        async def resource(*args, **kwargs):
            return response

        monkeypatch.setattr(category_mix, "resource", resource)
        assert client.get(f"/api/v1/workspaces/{WS}/category-mix").json() == response

    def test_mutation_forwards_action_and_conflict_metadata(
        self, client, signed_in, tenant, monkeypatch
    ):
        body = {"protocol_version": 2, "action": "reset_equal", "expected_revision": 7}

        async def mutate(*args, **kwargs):
            assert kwargs["body"] == body
            raise category_mix.MixConflict(8)

        monkeypatch.setattr(category_mix, "mutate", mutate)
        response = client.put(f"/api/v1/workspaces/{WS}/category-mix", json=body)
        assert response.status_code == 409
        assert response.json() == {"reason": "mix_conflict", "current_revision": 8}

    def test_protocol_required_maps_400(self, client, signed_in, tenant, monkeypatch):
        async def mutate(*args, **kwargs):
            raise category_mix.MixProtocolRequired()

        monkeypatch.setattr(category_mix, "mutate", mutate)
        response = client.put(
            f"/api/v1/workspaces/{WS}/category-mix", json={"protocol_version": 2}
        )
        assert (
            response.status_code == 400
            and response.json()["required_protocol_version"] == 2
        )
