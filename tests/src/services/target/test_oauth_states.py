"""`oauth_states`' PKCE half (RFC 7636): the verifier minted with a state,
its S256 challenge, and the verifier's place on the state row.

The transform is pinned to RFC 7636 Appendix B's vector. The verifier is
stored ENCRYPTED under the credential ring and read back only through
`code_verifier_of`, which refuses by name a row that carries none or one the
ring cannot decrypt. Scripted connections stand in for the database, so what
is pinned is each statement and its parameters; the column itself is the
Drive gate's (`tests/scripts/test_gdrive_oauth_gate.py`).
"""

from __future__ import annotations

import re

import pytest
from cryptography.fernet import Fernet

from src.services.target import oauth_states
from src.services.target.oauth_states import OAuthStateRefused
from tests.src.services.target.conftest import (
    APPENDIX_B_CHALLENGE,
    APPENDIX_B_VERIFIER,
)

#: RFC 7636 §4.1: 43 to 128 characters from the unreserved set (RFC 3986 §2.3).
VERIFIER_SHAPE = re.compile(r"[A-Za-z0-9\-._~]{43,128}")


@pytest.fixture
def keyed(ring_env):
    """A working ring, under a key generated for this test."""
    ring_env(key=Fernet.generate_key().decode())


class _Conn:
    """Records each statement and its parameters, and answers *row* to a
    `RETURNING` read (``None``: no live row)."""

    def __init__(self, row=None):
        self.statements = []
        self._row = row

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))
        row = self._row

        class _Mappings:
            def first(self):
                return row

        class _Result:
            rowcount = 0

            def mappings(self):
                return _Mappings()

        return _Result()


class TestTheVerifier:
    def test_is_43_to_128_unreserved_characters(self):
        assert VERIFIER_SHAPE.fullmatch(oauth_states.new_code_verifier())

    def test_is_fresh_every_time(self):
        assert len({oauth_states.new_code_verifier() for _ in range(32)}) == 32


class TestTheChallenge:
    def test_is_s256_as_rfc_7636_appendix_b_computes_it(self):
        assert oauth_states.code_challenge(APPENDIX_B_VERIFIER) == APPENDIX_B_CHALLENGE


class TestIssueStateKeepsTheVerifierEncrypted:
    async def test_the_row_stores_ciphertext_that_decrypts_to_the_verifier(self, keyed):
        conn = _Conn()
        verifier = oauth_states.new_code_verifier()
        await oauth_states.issue_state(
            conn,
            purpose="connect",
            provider="gdrive",
            user_id="u-1",
            workspace_id="ws-1",
            reconnect_target="ws-1",
            code_verifier=verifier,
        )
        sql, params = conn.statements[-1]
        assert sql.startswith("INSERT INTO oauth_states")
        assert "encrypted_code_verifier" in sql and ":verifier" in sql
        stored = params["verifier"]
        assert stored and verifier not in stored
        assert all(verifier not in str(value) for value in params.values())
        assert oauth_states.ring().decrypt(stored) == verifier

    async def test_a_state_minted_without_one_stores_null_and_needs_no_ring(
        self, ring_env
    ):
        """Sign-in, link, bind and every other state carry no verifier, and
        their mint does not depend on the ring: none is configured here."""
        ring_env()
        conn = _Conn()
        await oauth_states.issue_state(
            conn, purpose="signin", provider="google", cookie_nonce="n0nce"
        )
        ((sql, params),) = conn.statements
        assert "encrypted_code_verifier" in sql
        assert params["verifier"] is None


class TestConsumeStateReturnsTheVerifier:
    async def test_the_cas_returns_the_ciphertext_column(self):
        row = {
            "state": "st",
            "user_id": "u-1",
            "workspace_id": "ws-1",
            "provider": "gdrive",
            "purpose": "connect",
            "reconnect_target": "ws-1",
            "cookie_nonce_hash": None,
            "encrypted_code_verifier": "ciphertext",
        }
        conn = _Conn(row)
        await oauth_states.consume_state(conn, state="st")
        ((sql, params),) = conn.statements
        assert "encrypted_code_verifier" in sql.split("RETURNING", 1)[1]
        assert params == {"state": "st"}


class TestCodeVerifierOf:
    def test_reads_back_what_issue_state_stored(self, keyed):
        verifier = oauth_states.new_code_verifier()
        row = {"encrypted_code_verifier": oauth_states.ring().encrypt(verifier)}
        assert oauth_states.code_verifier_of(row) == verifier

    def test_a_state_without_one_is_refused_by_name(self, keyed):
        with pytest.raises(OAuthStateRefused, match="no code verifier"):
            oauth_states.code_verifier_of({"encrypted_code_verifier": None})

    @pytest.mark.parametrize(
        "ciphertext",
        [
            "not-a-fernet-token",
            Fernet(Fernet.generate_key()).encrypt(b"under-another-key").decode(),
        ],
        ids=["garbage", "another-key"],
    )
    def test_one_the_ring_cannot_decrypt_is_refused_by_name(self, keyed, ciphertext):
        with pytest.raises(OAuthStateRefused, match="code verifier undecryptable"):
            oauth_states.code_verifier_of({"encrypted_code_verifier": ciphertext})

    def test_a_ring_that_cannot_be_built_is_not_the_rows_fault(self, ring_env):
        """No key configured is the process's fault (`RingUnavailable`), never
        a refusal of the state in hand."""
        ring_env()
        with pytest.raises(oauth_states.RingUnavailable):
            oauth_states.code_verifier_of({"encrypted_code_verifier": "ciphertext"})
