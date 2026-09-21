"""Datetime helpers — keep timezone handling consistent across the codebase."""

import time
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
