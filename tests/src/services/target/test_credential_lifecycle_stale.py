"""`refresh_credential` leaves a credential revoked since the mint alone
(#1233): no provider call, no flip — the one-tick window between a mint and a
removal."""

from types import SimpleNamespace

import pytest

from src.services.target import credential_lifecycle, work_loop


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
        work_loop, "poller_session_factory", lambda engine, ws: _Session
    )

    async def credential_state(conn, *, credential_id):
        return "revoked"

    async def load_credential(conn, *, credential_id):
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
