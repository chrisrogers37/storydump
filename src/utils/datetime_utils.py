"""Datetime helpers — keep timezone handling consistent across the codebase."""

from datetime import datetime, timezone
from typing import Optional


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Return ``dt`` as a timezone-aware datetime, assuming UTC if naive.

    The boundary it serves is a naive value arriving from OUTSIDE the tier and
    entering a Python comparison: the one live caller,
    :mod:`src.services.target.transit`, parses a provider timestamp off a
    JSON body and compares it to an aware cutoff, and a naive parse would
    raise ``TypeError``. There is no naive-column boundary any more — every
    target column is ``TIMESTAMPTZ`` (`src/models/target/columns.py`) — so the
    mirror helper #909 added for that boundary went with the tech-debt fold
    (#1325 audit, TD-C16) and this is the tier's one datetime coercion.

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
