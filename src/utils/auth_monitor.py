"""In-memory authentication failure monitoring with Telegram alerting."""

import time
import threading

import httpx

from src.config.settings import settings
from src.utils.logger import logger

FAILURE_THRESHOLD = 5
WINDOW_SECONDS = 600  # 10 minutes

# Hard ceiling on tracked sources. WINDOW_SECONDS bounds how long a failure
# COUNTS; it does not bound how many distinct sources are retained, and the two
# are different guarantees. The sweep below bounds the steady state, but only
# between its runs — a distributed caller can mint keys far faster than the
# sweep interval, so the cap is what holds during a burst.
#
# At roughly 150 bytes per entry this ceiling is a few MB, which is the point:
# a bound that a long-lived process cannot exceed, not a tuning knob.
MAX_SOURCES = 10_000

# Evict down to a low-water mark rather than to the ceiling. Landing exactly on
# MAX_SOURCES means the next novel source trips the check again, so the O(n log
# n) selection below would run on every request for as long as a burst lasts.
# Undershooting amortises it over the headroom instead — the same sources are
# evicted in aggregate, only the granularity changes.
#
# Derived at call time, not stored: a second constant computed from the first
# is a second thing to keep in step with it.
EVICT_HEADROOM = 0.9

_lock = threading.Lock()

#: source -> failure timestamps, ascending. Both the sweep and the eviction
#: ordering read ``[-1]`` as the most recent failure, which holds because
#: entries are only ever appended at the current clock and pruning preserves
#: order. A source present in this map always has at least one timestamp.
_failures: dict[str, list[float]] = {}
_last_sweep = 0.0


def _sweep(now: float, force: bool = False) -> None:
    """Drop every source whose failures have all aged out. Caller holds _lock.

    Amortised: pruning inside ``record_failure`` only ever revisits the key
    being written, so a source that never recurs is never revisited and its
    entry outlives the window indefinitely. This is the pass that revisits the
    others. ``force`` skips the interval gate for the over-cap path, where the
    cost is worth paying immediately.
    """
    global _last_sweep
    if not force and now - _last_sweep < WINDOW_SECONDS:
        return
    cutoff = now - WINDOW_SECONDS
    for stale in [s for s, ts in _failures.items() if ts[-1] <= cutoff]:
        del _failures[stale]
    _last_sweep = now


def _evict_to_cap() -> None:
    """Force the map below MAX_SOURCES, down to the low-water mark.

    Caller holds _lock.

    Ordered by LEAST RECENT failure, deliberately — not by when a source was
    first seen. The two invert on exactly the case that matters: a source that
    started failing early in the window and is still failing has the OLDEST
    first-seen, so first-seen ordering would evict the source closest to
    FAILURE_THRESHOLD and turn a memory bound into a missed alert.

    Eviction is still lossy by construction — at the cap something must go, and
    a source dropped here restarts its count. Ordering only guarantees the
    victim is the least active one available, which is why the cap sits above
    any plausible legitimate population rather than being tuned close to it.
    """
    excess = len(_failures) - int(MAX_SOURCES * EVICT_HEADROOM)
    if excess <= 0:
        return
    coldest = sorted(_failures, key=lambda s: _failures[s][-1])
    for source in coldest[:excess]:
        del _failures[source]


def record_failure(source: str, reason: str) -> None:
    """Record an auth failure and alert if threshold is exceeded.

    Args:
        source: Identifier for the failure origin (IP address or chat_id).
        reason: Human-readable failure reason for the log/alert.
    """
    now = time.time()
    cutoff = now - WINDOW_SECONDS

    with _lock:
        _sweep(now)

        timestamps = _failures.get(source, [])
        # Prune expired entries
        timestamps = [t for t in timestamps if t > cutoff]
        timestamps.append(now)
        _failures[source] = timestamps
        count = len(timestamps)

        if len(_failures) > MAX_SOURCES:
            # Cheap pass first: at the cap, stale keys are the likeliest
            # excess, and dropping them costs nobody their count.
            _sweep(now, force=True)
            _evict_to_cap()

    logger.warning("Auth failure #%d from %s: %s", count, source, reason)

    if count == FAILURE_THRESHOLD:
        _send_alert(source, count)


def _send_alert(source: str, count: int) -> None:
    """Send a Telegram alert to the admin chat (fire-and-forget)."""
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.ADMIN_TELEGRAM_CHAT_ID
    text = (
        f"Auth alert: {count} failures from {source} "
        f"in the last {WINDOW_SECONDS // 60} min"
    )

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.error("Failed to send auth-failure alert for %s", source)


def reset() -> None:
    """Clear all tracked failures. Useful for testing."""
    global _last_sweep
    with _lock:
        _failures.clear()
        _last_sweep = 0.0
