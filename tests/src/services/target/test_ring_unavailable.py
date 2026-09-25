"""A key ring this process cannot build is its configuration's fault, never a
credential's.

Before `oauth_states.RingUnavailable`, a missing or malformed `ENCRYPTION_KEY`
reached every decrypt door inside its `try`, as the same `ValueError` a corrupt
row raises — so each door recorded a fact about a credential that was not
true: the refresh leg flipped a live Instagram account to `reauth_required`
(and the clock then messaged its owner to reconnect), the publish read handed
a healthy destination to a human as a dead token, the Drive read flipped every
source of the workspace to `error`, and the revoke audited the grant as
undecryptable and abandoned it with the grant still live at Google.

Every door now builds the ring BEFORE its `try`. These tests break the ring
for real — the settings the ring reads, the singleton reset — and for each
door answer a row that would otherwise decrypt-fail, so a door that builds the
ring back inside its `try` takes the per-credential branch and fails here.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet

from src.services.target import (
    credential_lifecycle,
    drive_credentials,
    ig_credentials,
    ig_login_oauth,
    oauth_states,
    unit_of_work,
)
from src.services.target.drive_adapter import DriveRetryableError
from src.services.target.instagram_graph import InstagramGraphAdapter
from src.services.target.meta_adapter import MetaRetryableError
from src.services.target.publish_pipeline import _dead_credential
from src.utils import encryption
from src.utils.encryption import TokenEncryption

#: Planted as a malformed key. The refusal is printed and logged by both
#: roots, so no message may ever carry it.
SENTINEL = "not-a-fernet-key-SENTINEL-7f3a"

#: What a door would read for a live credential: active, unexpired, and a
#: payload no ring decrypts — the corrupt-row branch, if the ring gets that far.
CORRUPT = "gAAAAABnot-a-real-fernet-token"
LIVE_ROW = {
    "encrypted_payload": CORRUPT,
    "state": "active",
    "expires_at": None,
    "account_state": "active",
}


@pytest.fixture
def ring_env(monkeypatch):
    """Configure the ring for real: the settings `TokenEncryption` reads,
    with the singleton reset on the way in and on the way out."""

    def configure(*, key=None, keys=None):
        monkeypatch.setattr(
            encryption,
            "settings",
            SimpleNamespace(ENCRYPTION_KEY=key, ENCRYPTION_KEYS=keys),
        )
        TokenEncryption.reset()

    yield configure
    TokenEncryption.reset()


class _Conn:
    """A scripted connection: records every statement and commit, answers a
    credential's state and payload by the statement's head, and nothing else
    (so `mark_dead`'s UPDATE finds no account to flip and returns)."""

    def __init__(self, payload: str = CORRUPT):
        self.statements: list[str] = []
        self.commits = 0
        self._payload = payload

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.statements.append(sql)
        if sql.startswith("SELECT state FROM oauth_credentials"):
            row = ("active",)
        elif sql.startswith("SELECT encrypted_payload FROM oauth_credentials"):
            row = (self._payload,)
        else:
            row = None
        return SimpleNamespace(first=lambda: row)

    async def commit(self):
        self.commits += 1

    def writes(self) -> list[str]:
        return [s for s in self.statements if not s.startswith("SELECT")]


class _Rows:
    """A session answering every read with one mapped row."""

    def __init__(self, row):
        self._row = row

    async def execute(self, *args, **kwargs):
        return SimpleNamespace(
            mappings=lambda: SimpleNamespace(first=lambda: self._row)
        )


def _factory_over(conn):
    """`poller_session_factory`'s shape: a factory of sessions, all `conn`."""

    @asynccontextmanager
    async def session():
        yield conn

    return lambda engine, workspace_id: session


def _uow_answering(row):
    """`unit_of_work(...)`'s shape for a read: `begin()` → one mapped row."""

    class _Session:
        async def execute(self, *args, **kwargs):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))

    @asynccontextmanager
    async def begin():
        yield _Session()

    return lambda *args, **kwargs: SimpleNamespace(begin=begin)


def _job():
    return {
        "id": "job-1",
        "workspace_id": "ws-1",
        "payload": {"credential_id": "cred-1"},
    }


class TestTheDoor:
    def test_no_key_is_ring_unavailable_naming_the_variable(self, ring_env):
        ring_env()
        with pytest.raises(oauth_states.RingUnavailable, match="ENCRYPTION_KEY"):
            oauth_states.ring()

    def test_a_malformed_key_is_refused_and_never_echoed(self, ring_env):
        ring_env(key=SENTINEL)
        with pytest.raises(oauth_states.RingUnavailable) as caught:
            oauth_states.ring()
        assert "ENCRYPTION_KEY" in str(caught.value)
        assert SENTINEL not in str(caught.value)

    def test_a_malformed_entry_in_the_rotation_list_is_refused_too(self, ring_env):
        ring_env(keys=f"{Fernet.generate_key().decode()},{SENTINEL}")
        with pytest.raises(oauth_states.RingUnavailable) as caught:
            oauth_states.ring()
        assert SENTINEL not in str(caught.value)

    def test_a_working_ring_is_built(self, ring_env):
        ring_env(key=Fernet.generate_key().decode())
        keys = oauth_states.ring()
        assert keys.decrypt(keys.encrypt("a token")) == "a token"


class TestTheRefreshLegFlipsNothing:
    """`07` §3's fail-closed flip commits BEFORE it raises, so a flip made on a
    missing key survives the fix: the account stays `reauth_required` and the
    clock's reauth leg prompts its owner weekly until they reconnect."""

    async def test_load_credential_reads_and_flips_nothing(self, ring_env):
        ring_env()
        conn = _Conn()
        with pytest.raises(oauth_states.RingUnavailable):
            await ig_login_oauth.load_credential(conn, credential_id="cred-1")
        assert conn.writes() == [] and conn.commits == 0

    async def test_the_refresh_executor_raises_it_and_calls_no_provider(
        self, ring_env, monkeypatch
    ):
        """Raised, the job's own retry budget runs; the credential stays
        `active` and the next refresh after the fix goes through."""
        ring_env()
        conn = _Conn()
        monkeypatch.setattr(unit_of_work, "poller_session_factory", _factory_over(conn))
        called = []

        async def refresh(token):  # pragma: no cover — must not be reached
            called.append(token)

        deps = SimpleNamespace(engine=object(), refresh=refresh)
        with pytest.raises(oauth_states.RingUnavailable):
            await credential_lifecycle.refresh_credential(deps, object(), _job())
        assert called == []
        assert conn.writes() == [] and conn.commits == 0

    async def test_control_a_corrupt_row_under_a_working_ring_still_fails_closed(
        self, ring_env
    ):
        """The fake answers faithfully: the flip this PR keeps for a corrupt
        row is still made, and committed."""
        ring_env(key=Fernet.generate_key().decode())
        conn = _Conn()
        with pytest.raises(ig_login_oauth.CredentialUndecryptable):
            await ig_login_oauth.load_credential(conn, credential_id="cred-1")
        assert conn.writes()[0].startswith(
            "UPDATE oauth_credentials SET state = 'expired'"
        )
        assert conn.commits == 1


class TestThePublishReadIsNotADeadToken:
    async def test_token_for_account_raises_it_not_a_dead_credential(
        self, ring_env, monkeypatch
    ):
        ring_env()
        monkeypatch.setattr(ig_credentials, "unit_of_work", _uow_answering(LIVE_ROW))
        with pytest.raises(oauth_states.RingUnavailable):
            await ig_credentials.token_for_account(
                object(), "17841400000000001", workspace_id="ws-1"
            )

    async def test_control_a_corrupt_row_under_a_working_ring_is_dead(
        self, ring_env, monkeypatch
    ):
        ring_env(key=Fernet.generate_key().decode())
        monkeypatch.setattr(ig_credentials, "unit_of_work", _uow_answering(LIVE_ROW))
        with pytest.raises(ig_credentials.IgCredentialDead):
            await ig_credentials.token_for_account(
                object(), "17841400000000001", workspace_id="ws-1"
            )

    async def test_the_adapter_sends_nothing_and_blames_no_account(self):
        """Code 0, as the floor's refusal: nothing left the process. Not 190 —
        the pipeline hands that to a human as a dead credential at once — and
        not untyped: out of `publish` the pipeline reads an untyped exception
        as a lost response, and a story never sent would park as "maybe
        posted"."""

        async def token_for_account(ref, *, workspace_id=None):
            raise oauth_states.RingUnavailable("ENCRYPTION_KEY not configured.")

        def handler(request):  # pragma: no cover — nothing may be sent
            raise AssertionError("a call went out without a token")

        adapter = InstagramGraphAdapter(
            token_for_account=token_for_account,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            resolver=lambda host: ["93.184.216.34"],
        )
        try:
            for effect in (
                lambda: adapter.create_container(
                    "17841400000000001",
                    media_url="u",
                    media_kind="image",
                    workspace_id="ws-1",
                ),
                lambda: adapter.publish(
                    "17841400000000001", "ctr-1", workspace_id="ws-1"
                ),
            ):
                with pytest.raises(MetaRetryableError) as info:
                    await effect()
                assert info.value.code == 0
                assert "key ring" in str(info.value)
                assert not _dead_credential(info.value)
        finally:
            await adapter.aclose()


class TestTheDriveReadIsNotADeadGrant:
    async def test_token_for_workspace_is_retryable_and_names_the_fix(
        self, ring_env, monkeypatch
    ):
        """`DriveCredentialDead` flips every source of the workspace to
        `error` and alerts its owner to reconnect; a retryable refusal leaves
        the sources `active` on the lane's ladder."""
        ring_env()
        monkeypatch.setattr(
            drive_credentials, "poller_session_factory", _factory_over(_Rows(LIVE_ROW))
        )
        with pytest.raises(DriveRetryableError) as caught:
            await drive_credentials.token_for_workspace(object(), "ws-1")
        assert "ENCRYPTION_KEY" in str(caught.value)
        assert "stored grant stands" in str(caught.value)

    async def test_control_a_corrupt_row_under_a_working_ring_is_dead(
        self, ring_env, monkeypatch
    ):
        ring_env(key=Fernet.generate_key().decode())
        monkeypatch.setattr(
            drive_credentials, "poller_session_factory", _factory_over(_Rows(LIVE_ROW))
        )
        with pytest.raises(drive_credentials.DriveCredentialDead):
            await drive_credentials.token_for_workspace(object(), "ws-1")


class TestTheRevokeIsNotAbandoned:
    async def test_revoke_raises_it_audits_nothing_and_calls_no_provider(
        self, ring_env, monkeypatch
    ):
        """RETRYABLE, like a 5xx: the payload is fine and the fixed key reads
        it. Audited as undecryptable, the job succeeded and the revocation was
        abandoned for good, with the grant still live at Google."""
        ring_env()
        monkeypatch.setattr(
            unit_of_work, "poller_session_factory", _factory_over(_Conn())
        )
        audits = []

        async def audit(*args):  # pragma: no cover — must not be reached
            audits.append(args)

        async def revoke_token(client, *, token):  # pragma: no cover
            raise AssertionError("Google was called without a readable grant")

        monkeypatch.setattr(credential_lifecycle, "_audit_revoke_failed", audit)
        monkeypatch.setattr(
            credential_lifecycle.google_oidc, "revoke_token", revoke_token
        )
        with pytest.raises(oauth_states.RingUnavailable):
            await credential_lifecycle.revoke_workspace_credentials(
                SimpleNamespace(engine=object()), object(), _job()
            )
        assert audits == []

    async def test_control_a_corrupt_row_under_a_working_ring_is_audited(
        self, ring_env, monkeypatch
    ):
        ring_env(key=Fernet.generate_key().decode())
        monkeypatch.setattr(
            unit_of_work, "poller_session_factory", _factory_over(_Conn())
        )
        audits = []

        async def audit(factory, workspace_id, credential_id, reason):
            audits.append(reason)

        monkeypatch.setattr(credential_lifecycle, "_audit_revoke_failed", audit)
        outcome = await credential_lifecycle.revoke_workspace_credentials(
            SimpleNamespace(engine=object()), object(), _job()
        )
        assert outcome == "undecryptable" and audits == ["undecryptable"]
