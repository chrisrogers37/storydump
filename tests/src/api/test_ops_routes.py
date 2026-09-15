"""The `/ops` read routes (phase 02 of the v2 CLI): one file the security
review reads. Each route opens the tenant's unit of work under the
principal, calls one view, and answers the phase-01 envelope; the views
themselves are proven against the replayed schema by the gate.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.api.principal import TOKEN_ROUTES, current_principal
from src.api.routes import ops as ops_routes
from src.services.target import ops_views
from tests.src.api.conftest import INTENT, PRINCIPAL, WS
from tests.src.api.test_token_principal import PERSON_TOKEN, SERVICE_TOKEN

OTHER_WS = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def as_principal(app):
    def install(principal):
        app.dependency_overrides[current_principal] = lambda: principal
        return principal

    yield install
    app.dependency_overrides.clear()


@pytest.fixture
def views(monkeypatch):
    """Every view answers a recording stub."""
    calls = []

    def make(name):
        async def view(session, **kw):
            calls.append((name, kw))
            return [{"workspace_id": kw.get("workspace_id"), "view": name}]

        return view

    for name in ("story", "cards", "floating", "account", "jobs", "outbox", "burst"):
        monkeypatch.setattr(ops_views, name, make(name))

    async def posture(conn):
        calls.append(("posture", {}))
        return {
            "ledger": "absent",
            "migrations": [],
            "role": {},
            "rls": [],
            "doors": [],
        }

    monkeypatch.setattr(ops_views, "posture", posture)
    return calls


class TestTheAllowlist:
    def test_every_ops_route_is_admitted_to_tokens(self, app):
        from fastapi.routing import APIRoute

        ops = {
            (m, r.path)
            for r in app.routes
            if isinstance(r, APIRoute) and r.path.startswith("/api/v1/ops/")
            for m in r.methods - {"HEAD", "OPTIONS"}
        }
        assert ops, "no /ops routes registered"
        assert ops <= TOKEN_ROUTES
        assert all(m == "GET" for m, _ in ops), "the views are reads"


class TestTheEnvelope:
    @pytest.mark.parametrize(
        "path, view, kw",
        [
            (f"story/{INTENT}", "story", {"workspace_id": WS, "intent_id": INTENT}),
            (f"cards/{INTENT}", "cards", {"workspace_id": WS, "intent_id": INTENT}),
            ("floating", "floating", {"workspace_id": WS, "limit": 100}),
            ("account/@gator", "account", {"workspace_id": WS, "key": "@gator"}),
        ],
    )
    def test_a_member_gets_one_workspaces_rows(
        self, client, as_principal, tenant, views, path, view, kw
    ):
        as_principal(PERSON_TOKEN)
        resp = client.get(f"/api/v1/ops/workspaces/{WS}/{path}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "v": 1,
            "kind": view,
            "data": {"workspace_id": WS, "rows": [{"workspace_id": WS, "view": view}]},
            "error": None,
        }
        assert views == [(view, kw)]
        assert ("gate", WS, PRINCIPAL.user_id, "member") in tenant

    @pytest.mark.parametrize("view", ["jobs", "outbox", "burst"])
    def test_the_window_defaults_to_three_hours(
        self, client, as_principal, tenant, views, view
    ):
        as_principal(PERSON_TOKEN)
        before = dt.datetime.now(dt.timezone.utc)
        resp = client.get(f"/api/v1/ops/workspaces/{WS}/{view}")
        assert resp.status_code == 200, resp.text
        ((name, kw),) = views
        assert name == view
        assert (
            dt.timedelta(hours=2, minutes=59)
            <= before - kw["since"]
            <= dt.timedelta(hours=3, seconds=5)
        )

    def test_the_window_accepts_a_relative_or_absolute_since(
        self, client, as_principal, tenant, views
    ):
        as_principal(PERSON_TOKEN)
        client.get(f"/api/v1/ops/workspaces/{WS}/jobs?since=72h")
        client.get(f"/api/v1/ops/workspaces/{WS}/jobs?since=2026-09-15T14:50:00Z")
        (_, a), (_, b) = views
        assert dt.datetime.now(dt.timezone.utc) - a["since"] >= dt.timedelta(hours=71)
        assert b["since"] == dt.datetime(2026, 9, 15, 14, 50, tzinfo=dt.timezone.utc)

    def test_a_bad_window_is_422(self, client, as_principal, tenant, views):
        as_principal(PERSON_TOKEN)
        resp = client.get(f"/api/v1/ops/workspaces/{WS}/jobs?since=yesterday")
        assert resp.status_code == 422
        assert views == []

    def test_floating_clamps_its_limit(self, client, as_principal, tenant, views):
        as_principal(PERSON_TOKEN)
        assert (
            client.get(f"/api/v1/ops/workspaces/{WS}/floating?limit=5000").status_code
            == 422
        )
        assert views == []

    def test_a_service_identity_reads_its_own_workspace_without_a_membership(
        self, client, as_principal, tenant, views
    ):
        as_principal(SERVICE_TOKEN)
        resp = client.get(f"/api/v1/ops/workspaces/{WS}/floating")
        assert resp.status_code == 200, resp.text
        assert not any(entry[0] == "gate" for entry in tenant)
        assert ("uow", WS, None) in tenant
        elsewhere = client.get(f"/api/v1/ops/workspaces/{OTHER_WS}/floating")
        assert elsewhere.status_code == 403
        assert elsewhere.json()["reason"] == "wrong_workspace"

    def test_posture_is_not_tenant_data(self, client, as_principal, views, engine):
        as_principal(SERVICE_TOKEN)
        resp = client.get("/api/v1/ops/posture")
        assert resp.status_code == 200, resp.text
        assert resp.json()["kind"] == "posture"
        assert resp.json()["data"]["ledger"] == "absent"
        assert views == [("posture", {})]

    def test_a_session_is_admitted_too(self, client, signed_in, tenant, views):
        resp = client.get(f"/api/v1/ops/workspaces/{WS}/floating")
        assert resp.status_code == 200


class TestSinceParsing:
    @pytest.mark.parametrize(
        "value, delta",
        [
            ("3h", dt.timedelta(hours=3)),
            ("45m", dt.timedelta(minutes=45)),
            ("2d", dt.timedelta(days=2)),
        ],
    )
    def test_relative(self, value, delta):
        now = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)
        assert ops_routes.parse_since(value, now=now) == now - delta

    def test_absolute_utc_offset_and_naive_as_utc(self):
        now = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc)
        want = dt.datetime(2026, 9, 15, 14, 50, tzinfo=dt.timezone.utc)
        assert ops_routes.parse_since("2026-09-15T14:50:00Z", now=now) == want
        assert ops_routes.parse_since("2026-09-15T10:50:00-04:00", now=now) == want
        assert ops_routes.parse_since("2026-09-15T14:50:00", now=now) == want

    @pytest.mark.parametrize(
        "value",
        [
            "yesterday",
            "3",
            "h",
            "-3h",
            "",
            "99999999999d",  # more digits than the grammar admits
            "999999d",  # an overflow in the arithmetic, and past the cap
            "31d",  # past the cap
            "0001-01-01T00:00:00+14:00",  # out of range under astimezone
            "9999-12-31T23:59:59-05:00",  # the future, and out of range
        ],
    )
    def test_anything_else_is_refused(self, value):
        now = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc)
        with pytest.raises(ValueError):
            ops_routes.parse_since(value, now=now)

    def test_the_cap_and_the_future_are_refused_by_name(self):
        now = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc)
        with pytest.raises(ValueError, match="at most 30 days"):
            ops_routes.parse_since("2026-08-01T00:00:00Z", now=now)
        with pytest.raises(ValueError, match="future"):
            ops_routes.parse_since("2026-09-17T00:00:00Z", now=now)
        assert ops_routes.parse_since("30d", now=now) == now - dt.timedelta(days=30)

    def test_an_overflowing_window_is_422_not_500(
        self, client, as_principal, tenant, views
    ):
        as_principal(PERSON_TOKEN)
        for value in ("999999d", "0001-01-01T00:00:00+14:00"):
            resp = client.get(f"/api/v1/ops/workspaces/{WS}/jobs?since={value}")
            assert resp.status_code == 422, value
        assert views == []
