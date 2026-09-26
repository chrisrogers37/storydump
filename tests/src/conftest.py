"""Fixtures shared across the unit tier.

`ring_env` configures the credential key ring for real — the settings
`TokenEncryption` reads, with its singleton reset on the way in and on the way
out — so a test can break the ring the way a deploy would (#1401).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.utils import encryption
from src.utils.encryption import TokenEncryption

#: Planted as a malformed key (19 characters; a Fernet key is 44). The refusal
#: is printed and logged by both roots, so no message may carry any of it —
#: and it reads like no word, so `leaks` can look for fragments.
MALFORMED_KEY = "ZqXj7Wv3Kp9Ls2Rt8Yh"


def leaks(text: str, secret: str = MALFORMED_KEY, width: int = 4) -> bool:
    """Whether *text* carries any *width*-character run of *secret* — a
    refusal that printed the key's first or last few characters leaks too."""
    return any(secret[i : i + width] in text for i in range(len(secret) - width + 1))


@pytest.fixture
def ring_env(monkeypatch):
    """`ring_env(key=…, keys=…)`: the ring's ENCRYPTION_KEY and
    ENCRYPTION_KEYS for this test; with neither, no key is configured."""

    def configure(*, key=None, keys=None):
        monkeypatch.setattr(
            encryption,
            "settings",
            SimpleNamespace(ENCRYPTION_KEY=key, ENCRYPTION_KEYS=keys),
        )
        TokenEncryption.reset()

    yield configure
    TokenEncryption.reset()
