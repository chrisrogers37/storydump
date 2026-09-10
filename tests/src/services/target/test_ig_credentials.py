"""The account token reader (#1220 step 3): the row it joins, the refusals it
names, and the plaintext it returns — under the key ring, never logged."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.services.target import ig_credentials
from src.services.target.ig_login_oauth import ring

REF = "17841400000000001"


class _Result:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Session:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        return _Result(self.row)


@pytest.fixture
def engine(monkeypatch):
    """`async_sessionmaker(engine)` answered by a scripted session."""
    holder = {"row": None, "session": None}

    def maker(engine_, expire_on_commit=False):
        def factory():
            holder["session"] = _Session(holder["row"])
            return holder["session"]

        return factory

    monkeypatch.setattr(ig_credentials, "async_sessionmaker", maker)
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
    async def test_the_active_credential_decrypts_to_the_token(self, engine):
        engine["row"] = _row()
        assert await ig_credentials.token_for_account(object(), REF) == "IGQVJtoken"
        ((sql, params),) = engine["session"].statements
        assert "provider_account_ref = :ref" in sql and params["ref"] == REF
        assert "c.provider = :provider" in sql and params["provider"] == "ig_login"
        assert "a.state <> 'disabled'" in sql

    async def test_no_row_names_connect(self, engine):
        engine["row"] = None
        with pytest.raises(ig_credentials.IgCredentialDead, match="connect Instagram"):
            await ig_credentials.token_for_account(object(), REF)

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
