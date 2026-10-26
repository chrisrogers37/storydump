"""When a refused past-post fetch is tried again, and when it parks.

A refusal that proves nothing is tried again after a doubling wait: a CDN
that refused the link, a Graph error that is not Graph's own "does not
exist", an owner the turn could not prove. A story parks on such a refusal
only once its attempts are spent. A proven class parks at once:
- Graph saying the post does not exist;
- a file over a named byte bound;
- a file whose kind Instagram names but whose link it withholds, on two
  reads in a row.

The decision is pure (`parks`): it reads which try this is and the last
try's refusal, never where those are kept. The story's prepare job counts its
tries on its own row (its attempts) and runs again after `Retry.delay`; the
slide's capture keeps the last refusal (`last_refusal`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional

from src.services.target.refusal_evidence import proves_gone


@dataclass(frozen=True)
class Retry:
    """The attempt ceiling, and the doubling wait between attempts from its
    base to its cap (`WorkerConfig.past_posts_retry_*`)."""

    attempts: int
    base_seconds: float
    cap_seconds: float

    def __post_init__(self):
        if (
            type(self.attempts) is not int
            or self.attempts < 1
            or not math.isfinite(self.base_seconds)
            or not math.isfinite(self.cap_seconds)
            or self.base_seconds < 0
            or self.cap_seconds < self.base_seconds
        ):
            raise ValueError(
                "a retry needs at least one attempt, and a finite cap at or above"
                " its base"
            )

    @classmethod
    def from_config(cls, config) -> Retry:
        return cls(
            config.past_posts_retry_attempts,
            config.past_posts_retry_base_seconds,
            config.past_posts_retry_cap_seconds,
        )

    def delay(self, attempt: int) -> float:
        """The wait after failed attempt `attempt`, 1 for the first."""
        return min(self.cap_seconds, self.base_seconds * 2 ** (attempt - 1))


#: One try: its refusal parks the story, whatever it proves.
ONCE = Retry(attempts=1, base_seconds=0, cap_seconds=0)


def assert_fits(retry: Retry, late_seconds: float) -> None:
    """Refuse a ladder the worker cannot keep: more than one try with no wait
    spends every try at once, and the waits before the last try must end
    inside the late window, or the window parks the story first."""
    if retry.attempts > 1 and retry.base_seconds <= 0:
        raise ValueError("a retry of more than one try needs a wait between tries")
    waits = sum(retry.delay(attempt) for attempt in range(1, retry.attempts))
    if waits >= late_seconds:
        raise ValueError(
            f"the retry's waits ({waits:.0f} s) do not fit the late window"
            f" ({late_seconds} s)"
        )


def _kind(refusal):
    return refusal.get("kind") if isinstance(refusal, Mapping) else None


def _names_a_bound(refusal) -> bool:
    """A named technical bound the same file meets on every try: the byte cap,
    whose evidence carries the cap and the bytes read."""
    return _kind(refusal) == "over_byte_cap"


def _withheld(refusal) -> bool:
    return (
        _kind(refusal) == "no_media_link"
        and refusal.get("has_link") is False
        and refusal.get("media_type") in ("IMAGE", "VIDEO")
    )


def last_refusal(capture, identity) -> Optional[Mapping]:
    """The refusal a slide's stored capture keeps from its last refused try of
    `identity` (its generation, revision and slide); None for a capture of
    anything else."""
    if not isinstance(capture, Mapping) or capture.get("reason") != "download_pending":
        return None
    stored = (
        capture.get("generation_id"),
        capture.get("observation_revision"),
        capture.get("selected_asset_id"),
    )
    return capture.get("refusal") if stored == tuple(identity) else None


def parks(refusal, *, attempt: int, last_refusal, retry: Retry) -> bool:
    """Whether refused try *attempt* (1 for the first) parks the story: its
    tries are spent, or *refusal* is one every try would meet again, read with
    the try before it (*last_refusal*). Otherwise it is tried again after
    `retry.delay(attempt)`."""
    return (
        attempt >= retry.attempts
        or proves_gone(refusal)
        or _names_a_bound(refusal)
        or (_withheld(refusal) and _withheld(last_refusal))
    )
