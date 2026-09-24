"""Tests for src/utils/datetime_utils.py."""

import time
from datetime import datetime, timezone, timedelta

import pytest

from src.utils.datetime_utils import ensure_utc, ms_since, parse_iso_timestamp, utcnow


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


@pytest.mark.parametrize(
    ("fraction", "microsecond"),
    [
        ("", 0),
        (".1", 100000),
        (".12", 120000),
        (".123", 123000),
        (".1234", 123400),
        (".05024", 50240),
        (".123456", 123456),
        (".1234567", 123456),
        (".123456789", 123456),
        (",5", 500000),
    ],
)
def test_parse_iso_timestamp_reads_every_fraction_width(fraction, microsecond):
    """3.10's ``fromisoformat`` takes only widths 0, 3 and 6; the parse reads
    every width to the same microsecond on every interpreter — a short
    fraction padded, a long one truncated (3.11's rule), either decimal sign.
    Mutations that redden: round instead of truncate; pad on the wrong side;
    (on 3.10) hand the fraction to ``fromisoformat`` as it came."""
    assert parse_iso_timestamp(f"2026-09-24T12:00:00{fraction}Z") == datetime(
        2026, 9, 24, 12, 0, 0, microsecond, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("offset", "utcoffset"),
    [
        ("Z", timedelta(0)),
        ("z", timedelta(0)),
        ("+00:00", timedelta(0)),
        ("+0000", timedelta(0)),
        ("+00", timedelta(0)),
        ("-05:00", timedelta(hours=-5)),
        ("+0530", timedelta(hours=5, minutes=30)),
        ("-03", timedelta(hours=-3)),
    ],
)
def test_parse_iso_timestamp_reads_every_offset_spelling(offset, utcoffset):
    """Every offset spelling names its instant on every interpreter, and the
    offset itself survives the parse.
    Mutations that redden: write ``±HHMM``'s minutes as zero; read a negative
    offset as positive; read ``Z`` as no offset at all."""
    parsed = parse_iso_timestamp(f"2026-09-24T12:00:00{offset}")
    assert parsed.utcoffset() == utcoffset
    assert parsed == datetime(2026, 9, 24, 12, tzinfo=timezone(utcoffset))


def test_parse_iso_timestamp_reads_either_separator_and_a_minute_clock():
    """A ``T``, a ``t`` or a space separates date and time, and the seconds
    may be absent.
    Mutation that reddens: narrow the separator to ``T``."""
    expected = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    for value in (
        "2026-09-24T12:00:00Z",
        "2026-09-24t12:00:00Z",
        "2026-09-24 12:00:00Z",
        "2026-09-24T12:00Z",
    ):
        assert parse_iso_timestamp(value) == expected, value


def test_parse_iso_timestamp_leaves_an_offsetless_value_naive():
    """No offset stays naive, as ``fromisoformat`` leaves it: "values are
    UTC" is :func:`ensure_utc`'s call at the caller, never the parse's.
    Mutation that reddens: default a missing offset to UTC inside the parse."""
    with_clock = parse_iso_timestamp("2026-09-24T12:00:00.5")
    assert with_clock.tzinfo is None
    assert with_clock == datetime(2026, 9, 24, 12, 0, 0, 500000)
    date_only = parse_iso_timestamp("2026-09-24")
    assert date_only.tzinfo is None
    assert date_only == datetime(2026, 9, 24)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "yesterday-ish",
        "Thu, 24 Sep 2026 12:00:00 GMT",
        "1790289258",
        "20260924T120000Z",
        "2026-9-24T12:00:00Z",
        "2026-09-24T12:00:00+05:",
        "2026-09-24T12:00:00.Z",
        "2026-09-24T25:00:00Z",
    ],
    ids=[
        "empty",
        "prose",
        "rfc-2822",
        "epoch-seconds",
        "basic-format",
        "unpadded-month",
        "dangling-offset-colon",
        "empty-fraction",
        "hour-out-of-range",
    ],
)
def test_parse_iso_timestamp_refuses_what_is_not_extended_iso_8601(value):
    """Outside the one shape it raises — on every interpreter alike, so a
    value 3.11 alone would read (basic format) raises too, and no caller's
    answer depends on the interpreter. A value of the right shape with a
    field out of range raises as ``fromisoformat`` does.
    Mutation that reddens: fall back to bare ``fromisoformat`` when the shape
    does not match (on 3.11+, the basic-format case)."""
    with pytest.raises(ValueError):
        parse_iso_timestamp(value)
