"""`refresh_credential` leaves a credential revoked since the mint alone
(#1233): no provider call, no flip — the one-tick window between a mint and a
removal. And the line it writes for a definitive rejection."""

import json
from types import SimpleNamespace

import httpx
import pytest

from src.services.target import credential_lifecycle, unit_of_work


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_a_revoked_credential_is_not_refreshed(monkeypatch):
    monkeypatch.setattr(
        unit_of_work, "poller_session_factory", lambda engine, ws: _Session
    )

    async def credential_state(conn, *, credential_id):
        return "revoked"

    async def load_credential(conn, *, credential_id, captured):
        raise AssertionError("must not load a revoked credential")

    monkeypatch.setattr(
        credential_lifecycle.oauth, "credential_state", credential_state
    )
    monkeypatch.setattr(credential_lifecycle.oauth, "load_credential", load_credential)
    called = []

    async def refresh(token):
        called.append(token)
        return ("new", None)

    deps = SimpleNamespace(engine=object(), refresh=refresh)
    job = {"id": "j", "workspace_id": "ws", "payload": {"credential_id": "c"}}
    assert await credential_lifecycle.refresh_credential(deps, object(), job) == "stale"
    assert called == []


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        pytest.param(
            {
                "error": {
                    "message": "tok-SENTINEL",
                    "type": "OAuthException",
                    "code": 190,
                    "error_subcode": 460,
                    "fbtrace_id": "AbC123xyz",
                }
            },
            "Meta error code=190 subcode=460 type=OAuthException fbtrace_id=AbC123xyz",
            id="fields",
        ),
        pytest.param(
            b"<html>denied</html>",
            "Meta's error fields could not be read",
            id="unreadable",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_rejection_line_names_metas_fields_and_never_the_token(
    monkeypatch, caplog, body, fields
):
    """The line a definitive rejection leaves, from what the real refresh door
    read of Meta's answer: its fields, or that they could not be read."""
    cl = credential_lifecycle

    async def credential_state(conn, *, credential_id):
        return "active"

    async def capture_credential(conn, *, workspace_id, credential_id):
        return None

    async def load_credential(conn, *, credential_id, captured):
        return "tok-SENTINEL"

    async def credential_rejected(s, *, workspace_id, credential_id, captured):
        return "rejected"

    content = body if isinstance(body, bytes) else json.dumps(body).encode()

    async def request(client, method, url, *, policy=None, **kwargs):
        return httpx.Response(400, content=content)

    monkeypatch.setattr(
        unit_of_work, "poller_session_factory", lambda engine, ws: _Session
    )
    monkeypatch.setattr(cl.oauth, "credential_state", credential_state)
    monkeypatch.setattr(cl.oauth, "load_credential", load_credential)
    monkeypatch.setattr(
        cl.past_posts_lifecycle, "capture_credential", capture_credential
    )
    monkeypatch.setattr(
        cl.past_posts_lifecycle, "credential_rejected", credential_rejected
    )
    monkeypatch.setattr(cl.egress, "request", request)
    caplog.set_level("DEBUG", logger=cl.logger.name)
    deps = SimpleNamespace(engine=object(), refresh=cl.ig_refresh)
    job = {"id": "j", "workspace_id": "ws", "payload": {"credential_id": "c"}}

    assert await cl.refresh_credential(deps, object(), job) == "rejected"
    assert [r.getMessage() for r in caplog.records] == [
        "refresh_credential j: credential c definitively rejected (HTTP 400;"
        f" {fields}) — marked dead, account flipped to reauth_required"
    ]
