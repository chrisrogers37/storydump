"""`egress.expires_at_from` — one guarded reading of a provider's `expires_in`.

Three legs derived this independently before it had a home (the Instagram
long-lived exchange, the Drive code exchange, the Drive refresh). The two
guards are what the table pins: a JSON body is untrusted, so a non-numeric
value is no expiry, and `bool` is an `int` in Python — `"expires_in": true`
must not read as one second.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.services.target.egress import expires_at_from


def _about(moment, seconds: int) -> bool:
    """*moment* is `seconds` from now, within a second of clock drift."""
    expected = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return abs((moment - expected).total_seconds()) < 1.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,seconds",
    [(3600, 3600), (3600.0, 3600), (0, 0), (60, 60)],
    ids=["int", "float", "zero", "minute"],
)
def test_a_numeric_expires_in_becomes_that_many_seconds_from_now(value, seconds):
    assert _about(expires_at_from(value), seconds)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [None, True, False, "3600", [], {}, object()],
    ids=["absent", "true", "false", "string", "list", "dict", "object"],
)
def test_anything_not_a_number_is_no_known_expiry(value):
    """`True` among them: a bool is an int, and one second is not an hour."""
    assert expires_at_from(value) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "value", [None, True, "3600"], ids=["absent", "true", "string"]
)
def test_the_default_is_what_an_unusable_value_means(value):
    """The Drive refresh passes Google's hour rather than write NULL, which
    would read as "no known expiry" and never be refreshed again."""
    assert _about(expires_at_from(value, default_seconds=3600), 3600)


@pytest.mark.unit
def test_a_usable_value_beats_the_default():
    assert _about(expires_at_from(60, default_seconds=3600), 60)


@pytest.mark.unit
def test_the_instant_is_timezone_aware_utc():
    assert expires_at_from(1).tzinfo is timezone.utc
