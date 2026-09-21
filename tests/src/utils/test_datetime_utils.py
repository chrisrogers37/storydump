"""Tests for src/utils/datetime_utils.py."""

import time
from datetime import datetime, timezone, timedelta

from src.utils.datetime_utils import ensure_utc, ms_since, utcnow


def test_ensure_utc_returns_none_for_none():
    assert ensure_utc(None) is None


def test_ensure_utc_coerces_naive_to_utc():
    naive = datetime(2026, 5, 16, 12, 30, 0)
    coerced = ensure_utc(naive)
    assert coerced is not None
    assert coerced.tzinfo == timezone.utc
    assert coerced.replace(tzinfo=None) == naive  # wall-clock preserved


def test_ensure_utc_passes_through_aware_unchanged():
    aware = datetime(2026, 5, 16, 12, 30, 0, tzinfo=timezone.utc)
    assert ensure_utc(aware) is aware  # same object, no allocation


def test_ensure_utc_preserves_non_utc_aware_datetime():
    # Should NOT silently re-anchor: a +05:00 datetime stays +05:00.
    plus5 = timezone(timedelta(hours=5))
    aware = datetime(2026, 5, 16, 12, 30, 0, tzinfo=plus5)
    assert ensure_utc(aware) is aware


def test_ensure_utc_preserves_microseconds():
    naive = datetime(2026, 5, 16, 12, 30, 0, 123456)
    coerced = ensure_utc(naive)
    assert coerced is not None
    assert coerced.microsecond == 123456


def test_utcnow_is_timezone_aware_utc():
    assert utcnow().tzinfo is timezone.utc


def test_utcnow_is_now():
    before = datetime.now(timezone.utc)
    assert before <= utcnow() <= datetime.now(timezone.utc)


def test_ms_since_truncates_rather_than_rounds():
    """Never negative, and never a rounded-up millisecond that has not
    elapsed: every `elapsed_ms` in the tier reads the same way."""
    assert ms_since(time.perf_counter()) == 0


def test_ms_since_counts_elapsed_milliseconds():
    started = time.perf_counter() - 1.5
    assert ms_since(started) >= 1500
