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

#: Planted as a malformed key. The refusal is printed and logged by both
#: roots, so no message may ever carry it.
MALFORMED_KEY = "not-a-fernet-key-SENTINEL-7f3a"


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
