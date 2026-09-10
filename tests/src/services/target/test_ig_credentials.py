"""The account token reader (#1220 step 3): the row it joins — within the
workspace when one is named — the refusals it names, and the plaintext it
returns under the key ring, never logged."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.services.target import ig_credentials
from src.services.target.ig_login_oauth import ring

REF = "17841400000000001"
WS = "0d1d7a24-9d69-4c3a-9c2e-6b1f8b0e0b11"


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Session:
    def __init__(self, holder):
        self.holder = holder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        self.holder["statements"].append((str(statement), params))
        return _Result(self.holder["row"])


@pytest.fixture
def engine(monkeypatch):
    """Both doors answered by one scripted session: `async_sessionmaker` for
    the ref-only read, `unit_of_work(...).begin()` for the tenant-scoped one."""
    holder = {"row": None, "statements": [], "uow": []}

    def maker(engine_, expire_on_commit=False):
        return lambda: _Session(holder)

    class _Uow:
        def begin(self):
            return _Session(holder)

    def unit_of_work(engine_, tenant_id, **gucs):
        holder["uow"].append((tenant_id, gucs))
        return _Uow()

    monkeypatch.setattr(ig_credentials, "async_sessionmaker", maker)
    monkeypatch.setattr(ig_credentials, "unit_of_work", unit_of_work)
    return holder


def _row(**over):
    base = {
        "encrypted_payload": ring().encrypt("IGQVJtoken"),
        "state": "active",
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        "account_state": "active",
    }
    base.update(over)
    return base


class TestTheRead:
    async def test_with_a_workspace_the_read_is_tenant_scoped_and_keyed_by_it(
        self, engine
    ):
        engine["row"] = _row()
        assert (
            await ig_credentials.token_for_account(object(), REF, workspace_id=WS)
            == "IGQVJtoken"
        )
        assert engine["uow"] == [(WS, {"actor_kind": "system"})]
        ((sql, params),) = engine["statements"]
        assert "a.workspace_id = :ws" in sql and params["ws"] == WS
        assert "provider_account_ref = :ref" in sql and params["ref"] == REF
        assert "c.provider = :provider" in sql and params["provider"] == "ig_login"
        assert "a.state NOT IN ('disabled', 'moved')" in sql, "tombstones are skipped"
        assert "ORDER BY (c.state = :usable) DESC" in sql, (
            "active first on this path too"
        )
        assert "updated_at" not in sql, "oauth_credentials carries no such column"

    async def test_without_a_workspace_the_read_prefers_an_active_row(self, engine):
        engine["row"] = _row()
        assert await ig_credentials.token_for_account(object(), REF) == "IGQVJtoken"
        assert engine["uow"] == []
        ((sql, params),) = engine["statements"]
        assert (
            "ORDER BY (c.state = :usable) DESC" in sql and params["usable"] == "active"
        )
        assert "updated_at" not in sql

    async def test_no_row_names_connect(self, engine):
        engine["row"] = None
        with pytest.raises(ig_credentials.IgCredentialDead, match="connect Instagram"):
            await ig_credentials.token_for_account(object(), REF, workspace_id=WS)

    async def test_a_revoked_credential_names_reauth(self, engine):
        engine["row"] = _row(state="revoked")
        with pytest.raises(ig_credentials.IgCredentialDead, match="re-auth"):
            await ig_credentials.token_for_account(object(), REF)

    async def test_an_account_awaiting_reauth_is_refused(self, engine):
        engine["row"] = _row(account_state="reauth_required")
        with pytest.raises(ig_credentials.IgCredentialDead, match="reauth_required"):
            await ig_credentials.token_for_account(object(), REF)

    async def test_an_expired_credential_names_the_refresh_leg(self, engine):
        engine["row"] = _row(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        with pytest.raises(ig_credentials.IgCredentialDead, match="expired"):
            await ig_credentials.token_for_account(object(), REF)

    async def test_an_undecryptable_payload_is_refused_by_name(self, engine):
        engine["row"] = _row(encrypted_payload="gAAAAAnot-a-fernet-token")
        with pytest.raises(ig_credentials.IgCredentialDead, match="decrypted"):
            await ig_credentials.token_for_account(object(), REF)
