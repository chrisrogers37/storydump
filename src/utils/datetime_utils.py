"""Datetime helpers — keep timezone handling consistent across the codebase."""

import re
import time
from datetime import datetime, timezone
from typing import Optional

#: Extended-format ISO-8601, the one shape :func:`parse_iso_timestamp` reads:
#: a date; optionally a ``T`` (or ``t``, or a space) and ``HH:MM``, then
#: optionally ``:SS`` with a fraction of any width after either decimal sign,
#: then optionally an offset written ``Z``, ``±HH:MM``, ``±HHMM`` or ``±HH``.
_ISO_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2})"
    r"(?:[Tt ](\d{2}:\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"(?:([Zz])|([+-]\d{2})(?::?(\d{2}))?)?)?"
)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return ``dt`` as a timezone-aware datetime, assuming UTC if naive.

    Several DB columns (notably ``api_tokens.expires_at`` and
    ``chat_settings.last_post_sent_at``) are declared as naive ``DateTime``
    but written with the convention "values are UTC". Comparing those to
    ``datetime.now(timezone.utc)`` raises ``TypeError``; this helper
    consolidates the coercion.

    Returns ``None`` unchanged. Already-aware datetimes pass through
    untouched (no unnecessary allocation) — including non-UTC offsets, which
    are **not** converted to UTC. This is intentional: the helper makes a
    naive value aware; it does not normalize offsets. If you need an offset
    conversion, call ``.astimezone(timezone.utc)`` after this.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def utcnow() -> datetime:
    """Now, timezone-aware, in UTC.

    Four modules had defined this privately (`publish_pipeline`, `work_loop`,
    `command_executors`, `email_sender`). It lives beside :func:`ensure_utc`
    because "stored values are UTC" is one convention, not four.
    """
    return datetime.now(timezone.utc)


def ms_since(started: float) -> int:
    """Milliseconds elapsed since a ``time.perf_counter()`` reading.

    The expression `publish_pipeline._ms_since` already named, written out at
    three more sites in `transit`. Every log line and audit `elapsed_ms` in
    the tier rounds the same way — it TRUNCATES — and one home is what keeps
    that true.
    """
    return int((time.perf_counter() - started) * 1000)


def parse_iso_timestamp(value: str) -> datetime:
    """An extended-format ISO-8601 timestamp, parsed the same way on every
    supported interpreter.

    ``datetime.fromisoformat`` reads general ISO-8601 from Python 3.11, but at
    the repository's 3.10 floor it takes only a 3- or 6-digit fraction and a
    ``±HH:MM`` offset — so whether a timestamp parses would depend on which
    interpreter runs it. Instead the shape is matched here, once
    (``_ISO_TIMESTAMP``), and ``fromisoformat`` is handed only the canonical
    form every supported version accepts: the fraction padded or truncated to
    microseconds (truncated, as 3.11 does), the offset written ``±HH:MM``.
    Anything outside the shape raises on every interpreter alike, including
    what 3.11 alone would accept (basic format, ``20260924T120000Z``).

    A value without an offset stays naive, as ``fromisoformat`` leaves it —
    pair with :func:`ensure_utc` where "values are UTC" is the convention.
    Raises ``ValueError`` for anything that is not such a timestamp, or whose
    fields are out of range.
    """
    match = _ISO_TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError(f"not an extended-format ISO-8601 timestamp: {value!r}")
    date, hours_minutes, seconds, fraction, zulu, offset_hours, offset_minutes = (
        match.groups()
    )
    if hours_minutes is None:
        return datetime.fromisoformat(date)
    canonical = f"{date}T{hours_minutes}"
    if seconds is not None:
        canonical += f":{seconds}"
    if fraction is not None:
        canonical += "." + fraction[:6].ljust(6, "0")
    if zulu is not None:
        canonical += "+00:00"
    elif offset_hours is not None:
        canonical += offset_hours + ":" + (offset_minutes or "00")
    return datetime.fromisoformat(canonical)
