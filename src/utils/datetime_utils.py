"""Datetime helpers — keep timezone handling consistent across the codebase."""

from datetime import datetime, timezone
from typing import Optional


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


def naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return ``dt`` as a **naive** datetime holding UTC wall time.

    The mirror of :func:`ensure_utc`, and the same convention rather than a
    second one: this codebase's rule is "stored values are UTC", and the two
    helpers are its two boundaries. ``ensure_utc`` is for a naive value
    entering Python comparison, where the fix is to make it aware.
    ``naive_utc`` is for an aware value entering a **SQL comparison against a
    naive column**, where making it aware is exactly the wrong move.

    Why ``ensure_utc`` does not fit that case (#909): it makes a naive value
    aware and passes an already-aware one straight through, so applying it to
    an aware ``since`` is a no-op and leaves the defect in place. The column is
    the naive side, and it is not the side Python can coerce.

    Why the defect is worth a helper rather than a comment: comparing an aware
    parameter to a naive ``DateTime`` column does not raise. Postgres casts the
    parameter using the **session's** ``TimeZone``, so the query is correct on
    a UTC session and wrong by the offset on any other — right on one
    connection and wrong on another, with a believable number either way.

    An aware value is converted to UTC first (unlike ``ensure_utc``, which
    deliberately does not normalise offsets — here it must, because dropping
    the tzinfo from a non-UTC offset without converting would silently shift
    the value). ``None`` passes through unchanged.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
