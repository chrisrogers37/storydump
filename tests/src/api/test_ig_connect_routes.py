"""#1041 — the connect and reconnect routes.

**Bound, stated up front because it is the thing a reader will assume wrongly:
no test here performs a real OAuth round trip.** Nothing contacts Instagram.
These prove the ROUTE SHAPE only — floor, refusals, and what the browser is
handed. The exchange leg is a service concern and is tested beside its Drive
sibling in `tests/src/services/target/test_ig_login_oauth_exchange.py`; that
split is the repo's layout, and keeping the async exchange tests out of this
module also keeps a second event loop out of a file built on `TestClient`.

Whether Meta accepts our redirect_uri, and whether a development-mode app
grants the two scopes, is a question only a real grant answers — see the PR.
"""

from __future__ import annotations


import pytest

from src.api.routes import v1
from src.config.settings import settings
from src.services.target import ig_login_oauth, provisioning

from .conftest import API, FRONT

WS = "11111111-1111-1111-1111-111111111111"
ACCT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def instagram_configured(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "ig-cid", raising=False)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "ig-sec", raising=False)
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE_URL", API, raising=False)
    monkeypatch.setattr(settings, "WEB_APP_URL", FRONT, raising=False)


@pytest.fixture
def minted(monkeypatch):
    """`issue_state` stubbed at the seam the route imports it through."""
    calls = []

    async def issue_state(session, **kw):
        calls.append(kw)
        return "STATE-VALUE"

    monkeypatch.setattr(v1, "issue_state", issue_state)
    return calls


class TestTheConnectLeg:
    def test_it_hands_back_an_instagram_url_carrying_the_minted_state(
        self, client, signed_in, tenant, instagram_configured, minted
    ):
        r = client.post(f"/api/v1/workspaces/{WS}/accounts/connect")
        assert r.status_code == 200, r.text
        url = r.json()["authorization_url"]
        assert url.startswith(ig_login_oauth.AUTHORIZE_URL)
        assert "STATE-VALUE" in url
        assert "ig-cid" in url

    def test_it_mints_a_connect_state_with_no_reconnect_target(
        self, client, signed_in, tenant, instagram_configured, minted
    ):
        client.post(f"/api/v1/workspaces/{WS}/accounts/connect")
        assert minted[0]["purpose"] == "connect"
        assert minted[0]["provider"] == ig_login_oauth.PROVIDER
        assert minted[0]["workspace_id"] == WS
        assert minted[0].get("reconnect_target") is None

    def test_it_is_at_the_admin_floor(
        self, client, signed_in, tenant, instagram_configured, minted
    ):
        """`06` §4, through the SAME `authorize_member` gate the reads use —
        not a second authorization path invented for this leg."""
        client.post(f"/api/v1/workspaces/{WS}/accounts/connect")
        gates = [c for c in tenant if c[0] == "gate"]
        assert gates and gates[0][3] == "admin"

    def test_an_unconfigured_deployment_refuses_rather_than_half_working(
        self, client, signed_in, tenant, monkeypatch, minted
    ):
        monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", None, raising=False)
        r = client.post(f"/api/v1/workspaces/{WS}/accounts/connect")
        assert r.status_code == 503
        assert "INSTAGRAM_APP_ID" in r.json()["detail"]


class TestTheReconnectLeg:
    @pytest.fixture(autouse=True)
    def _exists(self, monkeypatch):
        self.found = True

        async def destination_exists(executor, *, workspace_id, account_id):
            return self.found

        monkeypatch.setattr(provisioning, "destination_exists", destination_exists)

    def test_it_pins_the_account_in_reconnect_target(
        self, client, signed_in, tenant, instagram_configured, minted
    ):
        r = client.post(f"/api/v1/workspaces/{WS}/accounts/{ACCT}/reconnect")
        assert r.status_code == 200, r.text
        assert minted[0]["purpose"] == "reconnect"
        assert minted[0]["reconnect_target"] == ACCT

    def test_an_unknown_account_is_refused_at_ISSUE_time(
        self, client, signed_in, tenant, instagram_configured, minted
    ):
        """A state minted against an id that does not exist is a state nothing
        can ever consume. Refusing here gives a 404 the caller can read, rather
        than a redirect that dies minutes later at a URL they cannot."""
        self.found = False
        r = client.post(f"/api/v1/workspaces/{WS}/accounts/{ACCT}/reconnect")
        assert r.status_code == 404
        assert minted == [], "a state was minted for an account that does not exist"

    def test_it_is_at_the_admin_floor(
        self, client, signed_in, tenant, instagram_configured, minted
    ):
        client.post(f"/api/v1/workspaces/{WS}/accounts/{ACCT}/reconnect")
        gates = [c for c in tenant if c[0] == "gate"]
        assert gates and gates[0][3] == "admin"
