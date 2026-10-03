"""The visitor the landing site names becomes a counter key (`07` §43)."""

from __future__ import annotations

import pytest

from src.api.routes.public import _address


@pytest.mark.parametrize(
    "raw, key",
    [
        ("203.0.113.7", "203.0.113.7"),
        (" 203.0.113.7 ", "203.0.113.7"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:DB8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
        ("fe80::1%eth0", "fe80::/64"),
    ],
)
def test_an_address_is_its_key(raw, key):
    assert _address(raw) == key


@pytest.mark.parametrize(
    "raw", [None, "", "unknown", "203.0.113.7, 10.0.0.1", "203.0.113.7:443"]
)
def test_anything_else_is_no_address(raw):
    assert _address(raw) is None
