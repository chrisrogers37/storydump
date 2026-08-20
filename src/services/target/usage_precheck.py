"""L.5 slice 2 — the lazy inline usage pre-check (#915, `02` §8).

ADVISORY only, and the constraints are §8's, verbatim where it matters:

- Runs "inside the publish pipeline, immediately before the §4 flip
  transaction" — the executor calls it with NO transaction open (it is a
  provider read).
- In-process cache keyed on **provider_account_ref**, "shared across
  duplicate workspace rows of one real account", TTL per `05` (5 min).
- "There is no background refresh job" — staleness resolves lazily at the
  next check, never on a timer.
- "Miss/stale/error/flag-off on the pre-check ⇒ proceed to the flip; error 9
  remains the arbiter." A failed read PROCEEDS and is not cached — caching an
  error would convert one blip into a TTL-long blind spot.
- Worst-case provider load ≤ one usage query per publish attempt — the cache
  answers everything inside the TTL, including the DEFER answer.

**The default-off flag (C7 process-class) lives at the executor seam**, not
here: the executor takes an optional precheck and skips the call when it is
None/disabled. This module is the cache and the decision, kept flag-free so
the S.5 canary can flip the flag without touching decision code.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

PROCEED = "proceed"
DEFER = "defer"

#: `05` §8 row: in-process cache TTL 5 min.
DEFAULT_TTL_SECONDS = 300.0


class UsagePrecheck:
    """The §8 advisory read: at/over Meta's own counter ⇒ DEFER, else PROCEED."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, int, int]] = {}

    async def check(self, meta, provider_account_ref: str) -> str:
        """One decision, at most one provider read. Never raises."""
        now = self._clock()
        cached = self._cache.get(provider_account_ref)
        if cached is not None and now - cached[0] < self._ttl:
            _, usage, total = cached
        else:
            try:
                payload = await meta.usage(provider_account_ref)
                usage = int(payload["quota_usage"])
                total = int(payload["quota_total"])
            except Exception:  # noqa: BLE001 — §8: error ⇒ proceed, uncached
                # The account ref is deliberately NOT in the log line: it is
                # the real IG user id, and the telegram-ratchet pins log
                # hygiene for exactly this identifier class (#874 armor).
                logger.warning(
                    "usage pre-check read failed; proceeding (error 9 "
                    "remains the arbiter)",
                    exc_info=True,
                )
                return PROCEED
            self._cache[provider_account_ref] = (now, usage, total)
        return DEFER if usage >= total else PROCEED
