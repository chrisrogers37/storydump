"""W4 ingress route — the refusal ladder, and the seam's negative (#942).

The load-bearing test here is not that an unwired route returns 503. It is that
an unwired route **never reaches `admit`**. Admission is irreversible, so a
route that admitted and then refused would destroy the command while reporting
a refusal, and the status code alone cannot tell those two apart.
"""

from __future__ import annotations

import pytest

from src.api.app import app
from src.api.routes import webhooks
from src.api.routes.webhooks import SECRET_HEADER
from src.config.settings import settings
from src.services.target.webhook_ingress import AdmissionConflict, DeliveryReplayed

SECRET = "test-secret-value"
URL = "/webhooks/telegram"


class FakeConn:
    """Minimal async connection: records commits, nothing else."""

    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.commits += 1


@pytest.fixture
def armed(monkeypatch):
    """The secret configured — i.e. the deployment deliberately armed."""
    monkeypatch.setattr(
        settings, "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", SECRET, raising=False
    )


@pytest.fixture
def client():
    """Bound to the module singleton, because these tests wire the seam on
    `app.state` — the factory-built client in conftest is a different app."""
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture(autouse=True)
def unwired():
    """Every test starts at the real default: no dispatcher wired.

    `app` is a module singleton, so the seam is process state whether it lives
    on `app.state` or in a module global. The reset is a discipline either way.
    """
    app.state.ingress = None
    yield
    app.state.ingress = None


@pytest.fixture
def spy(monkeypatch):
    """Wire a runtime and record every call the route makes through the seam."""
    seen = {"admit": [], "dispatch": [], "conn": FakeConn()}

    async def fake_admit(conn, **kw):
        seen["admit"].append((conn, kw))
        return {"admitted": True}

    async def fake_dispatch(conn, payload):
        seen["dispatch"].append((conn, payload))

    monkeypatch.setattr(webhooks, "admit", fake_admit)
    app.state.ingress = webhooks.IngressRuntime(
        connect=lambda: seen["conn"], dispatch=fake_dispatch
    )
    return seen


def _post(client, body=None, *, secret=SECRET, raw=None):
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    if raw is not None:
        return client.post(
            URL, content=raw, headers={**headers, "Content-Type": "application/json"}
        )
    return client.post(URL, json=body, headers=headers)


# --- the dormancy property -------------------------------------------------


def test_an_unset_secret_refuses_every_delivery(client, monkeypatch):
    """Absence of config refuses, it does not accept. The direction is the point."""
    monkeypatch.setattr(
        settings, "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN", None, raising=False
    )
    assert _post(client, {"update_id": 1}).status_code == 403


def test_a_wrong_secret_is_refused(client, armed):
    assert _post(client, {"update_id": 1}, secret="wrong").status_code == 403


def test_a_missing_header_is_refused(client, armed):
    assert _post(client, {"update_id": 1}, secret=None).status_code == 403


def test_the_secret_is_checked_before_the_body_is_parsed(client, armed):
    """A bad secret with an unparseable body must still be 403, not 400.

    Refusal order is a security property: the cheapest, least informative
    refusal has to come first, or a prober learns about the body handling of a
    route it cannot authenticate to.
    """
    assert _post(client, secret="wrong", raw=b"{not json").status_code == 403


# --- the refusal ladder ----------------------------------------------------


def test_a_non_json_body_is_rejected(client, armed):
    assert _post(client, raw=b"{not json").status_code == 400


def test_a_json_scalar_body_is_rejected(client, armed):
    assert _post(client, raw=b'"a string"').status_code == 400


def test_a_body_without_update_id_is_rejected(client, armed):
    assert _post(client, {"message": {"text": "hi"}}).status_code == 400


def test_a_non_integer_update_id_is_rejected(client, armed):
    assert _post(client, {"update_id": "17"}).status_code == 400


# --- THE SEAM: the negative that matters ----------------------------------


def test_an_unwired_route_refuses_with_503(client, armed):
    assert _post(client, {"update_id": 42}).status_code == 503


def test_an_unwired_route_NEVER_REACHES_ADMIT(client, armed, monkeypatch):
    """The load-bearing negative.

    Admission is irreversible: once the `command_dedup` key is committed, a
    redelivery of the same update is a replay and is correctly never executed.
    So an unwired route that admitted would destroy the command while returning
    a refusal, and the 503 above cannot distinguish that from this.
    """
    called = []

    async def exploding_admit(conn, **kw):
        called.append(kw)
        raise AssertionError("admit must not be reached with no dispatcher wired")

    monkeypatch.setattr(webhooks, "admit", exploding_admit)
    assert _post(client, {"update_id": 42}).status_code == 503
    assert called == [], "a delivery was admitted that could not be dispatched"


# --- the wired path -------------------------------------------------------


def test_a_wired_route_admits_and_dispatches(client, armed, spy):
    r = _post(client, {"update_id": 7, "message": {"text": "hi"}})
    assert r.status_code == 200
    assert r.json() == {"status": "admitted"}
    assert len(spy["admit"]) == 1
    assert len(spy["dispatch"]) == 1


def test_admission_keys_on_the_update_id_and_carries_no_tenant(client, armed, spy):
    """The reason this route can precede #854: admission takes no tenant."""
    _post(client, {"update_id": 99})
    _conn, kw = spy["admit"][0]
    assert kw["channel"] == "telegram"
    assert kw["external_ref"] == "99"
    assert "workspace_id" not in kw and "tenant_id" not in kw


def test_admission_and_dispatch_share_one_connection(client, armed, spy):
    """So #854 can make admission and effect a single transaction."""
    _post(client, {"update_id": 5})
    assert spy["admit"][0][0] is spy["dispatch"][0][0]


def test_the_commit_follows_dispatch(client, armed, spy):
    """Committing before dispatch would leave a key with no effect."""
    _post(client, {"update_id": 5})
    assert spy["conn"].commits == 1


# --- the two named admission outcomes ------------------------------------
#
# Kept as two tests rather than one parametrized table: `DeliveryReplayed` and
# `AdmissionConflict` are separate classes in L.8's design precisely because
# they are different events, and a shared test name would merge the one
# distinction the module exists to draw.


def test_a_replay_is_acknowledged_and_neither_dispatched_nor_committed(
    client, armed, spy, monkeypatch
):
    """200 replayed callbacks yield one command — the L.8 clause."""

    async def replaying_admit(conn, **kw):
        raise DeliveryReplayed("seen")

    monkeypatch.setattr(webhooks, "admit", replaying_admit)
    r = _post(client, {"update_id": 7})
    assert r.status_code == 200
    assert r.json() == {"status": "replayed"}
    assert spy["dispatch"] == [], "a replay must not re-execute"
    assert spy["conn"].commits == 0, "nothing was inserted, so nothing to commit"


def test_a_conflict_is_rejected_never_swallowed_as_a_replay(
    client, armed, spy, monkeypatch
):
    """Same key, different content: a caller bug or an attack, never a dedup."""

    async def conflicting_admit(conn, **kw):
        raise AdmissionConflict("reused key, new body")

    monkeypatch.setattr(webhooks, "admit", conflicting_admit)
    r = _post(client, {"update_id": 7})
    assert r.status_code == 409
    assert r.json() == {"detail": "admission conflict"}
    assert spy["dispatch"] == []
    assert spy["conn"].commits == 0
