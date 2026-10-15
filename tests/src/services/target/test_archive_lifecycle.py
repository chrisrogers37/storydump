"""`archive_lifecycle`'s parts that need no database (#1644).

Everything that takes a lock, classifies an authorization or writes through
the epoch writer is proven under the runtime roles in
`tests/scripts/test_archive_lifecycle_gate.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from src.services.target import archive_lifecycle, ig_login_oauth


class TestARejectionWithNothingCaptured:
    async def test_marks_the_credential_dead_and_takes_no_lock(self, monkeypatch):
        """No `ig_login` row was captured, so there is no account to order and
        no archive to fence: `mark_dead` runs on the session and nothing is
        locked."""
        dead = []

        async def mark_dead(conn, *, credential_id):
            dead.append((conn, credential_id))

        async def no_lock(*args, **kwargs):  # pragma: no cover — must not run
            raise AssertionError("no account was captured, so nothing is locked")

        monkeypatch.setattr(archive_lifecycle.oauth, "mark_dead", mark_dead)
        monkeypatch.setattr(
            archive_lifecycle.source_state, "lock_workspace_sources", no_lock
        )
        session = object()
        outcome = await archive_lifecycle.credential_rejected(
            session, workspace_id="ws", credential_id="cred", captured=None
        )
        assert (outcome, dead) == ("rejected", [(session, "cred")])


class _UnreadablePayload:
    """A connection whose one read answers a payload no key decrypts, for a
    workspace, and counts the commits."""

    def __init__(self):
        self.commits = 0

    async def execute(self, stmt, params=None):
        return SimpleNamespace(first=lambda: ("gAAAAABnot-a-real-fernet-token", "ws-1"))

    async def commit(self):
        self.commits += 1


class TestAnUnreadablePayloadTakesTheRejectionDoor:
    """`load_credential` fails closed, and the flip it commits is the wrapper's,
    like every other definitive rejection: the lock order and an archive's epoch
    are proven under the runtime roles in `test_archive_lifecycle_gate.py`."""

    @pytest.fixture
    def door(self, ring_env, monkeypatch):
        """The wrapper, recording what it is handed and answering *outcome*."""
        ring_env(key=Fernet.generate_key().decode())
        seen, answer = [], {"outcome": "rejected"}

        async def rejected(executor, *, workspace_id, credential_id, captured):
            seen.append((executor, workspace_id, credential_id, captured))
            return answer["outcome"]

        monkeypatch.setattr(archive_lifecycle, "credential_rejected", rejected)
        return seen, answer

    async def test_the_capture_and_the_credentials_workspace_reach_the_wrapper(
        self, door
    ):
        seen, _ = door
        conn, captured = _UnreadablePayload(), object()
        with pytest.raises(
            ig_login_oauth.CredentialUndecryptable, match="flipped expired"
        ):
            await ig_login_oauth.load_credential(
                conn, credential_id="cred-1", captured=captured
            )
        assert seen == [(conn, "ws-1", "cred-1", captured)]
        assert conn.commits == 1, "the flip is committed before the raise"

    async def test_without_a_capture_the_wrapper_is_handed_none(self, door):
        seen, _ = door
        conn = _UnreadablePayload()
        with pytest.raises(ig_login_oauth.CredentialUndecryptable):
            await ig_login_oauth.load_credential(
                conn, credential_id="cred-1", captured=None
            )
        assert seen == [(conn, "ws-1", "cred-1", None)]

    async def test_the_capture_is_required_so_no_bare_flip_is_silent(self, door):
        seen, _ = door
        with pytest.raises(TypeError, match="captured"):
            await ig_login_oauth.load_credential(
                _UnreadablePayload(), credential_id="cred-1"
            )
        assert seen == [], "nothing was read, so nothing was flipped"

    async def test_a_reconnect_since_the_capture_flips_nothing_and_says_so(self, door):
        seen, answer = door
        answer["outcome"] = "stale"
        conn = _UnreadablePayload()
        with pytest.raises(ig_login_oauth.CredentialUndecryptable) as caught:
            await ig_login_oauth.load_credential(
                conn, credential_id="cred-1", captured=object()
            )
        assert "nothing was flipped" in str(caught.value)
        assert "flipped expired" not in str(caught.value)
        assert "gAAAAAB" not in str(caught.value), "never the ciphertext"
        assert conn.commits == 1
