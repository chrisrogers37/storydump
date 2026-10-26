"""When a refused past-post fetch is tried again, and when it parks.

A refusal that proves nothing is tried again after a doubling wait: a CDN
that refused the link, a Graph error that is not Graph's own "does not
exist", an owner the turn could not prove. A story parks on such a refusal
only once its attempts are spent. A proven class parks at once:
- Graph saying the post does not exist;
- a file over a named byte bound;
- a file whose kind Instagram names but whose link it withholds, on two
  reads in a row.

The decisions are pure: they read a count of failed tries and the last
refusal (`Tried`), never where those are kept. The slide's capture keeps them
today (`from_capture`, written by `instagram_preparation`); a job that keeps
them on its own row passes the same `Tried` and runs again at the decision's
`next_attempt_at`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Optional

from src.utils.datetime_utils import ensure_utc, parse_iso_timestamp

#: Graph's answer for an object that does not exist: code 100, subcode 33.
GRAPH_NOT_FOUND = (100, 33)


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
            or self.base_seconds < 0
            or self.cap_seconds < self.base_seconds
        ):
            raise ValueError(
                "a retry needs at least one attempt and a cap at or above its base"
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


@dataclass(frozen=True)
class Tried:
    """One slide's failed tries for one observation: how many, the last one's
    refusal, and when the next may start."""

    attempts: int = 0
    refusal: Optional[Mapping] = None
    next_attempt_at: Optional[datetime] = None


@dataclass(frozen=True)
class Decision:
    """Try again at `next_attempt_at`, or park when there is none; `attempt`
    counts this one."""

    attempt: int
    next_attempt_at: Optional[datetime] = None

    @property
    def park(self) -> bool:
        return self.next_attempt_at is None


def _kind(refusal):
    return refusal.get("kind") if isinstance(refusal, Mapping) else None


def proves_gone(refusal) -> bool:
    """Graph's own answer that the post does not exist."""
    return (
        _kind(refusal) == "graph_error"
        and (refusal.get("code"), refusal.get("error_subcode")) == GRAPH_NOT_FOUND
    )


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


def from_capture(capture, identity) -> Tried:
    """The failed tries a slide's stored capture counted for `identity` (its
    generation, revision and slide); none for a capture of anything else."""
    if not isinstance(capture, Mapping) or capture.get("reason") != "download_pending":
        return Tried()
    stored = (
        capture.get("generation_id"),
        capture.get("observation_revision"),
        capture.get("selected_asset_id"),
    )
    attempts = capture.get("attempt")
    if stored != tuple(identity) or type(attempts) is not int or attempts < 1:
        return Tried()
    try:
        next_at = ensure_utc(parse_iso_timestamp(capture["next_attempt_at"]))
    except (KeyError, TypeError, ValueError):
        next_at = None
    return Tried(attempts, capture.get("refusal"), next_at)


def decide(refusal, tried: Tried, now, retry: Retry) -> Decision:
    """The decision for one refused fetch after `tried`."""
    attempt = tried.attempts + 1
    if (
        attempt >= retry.attempts
        or proves_gone(refusal)
        or _names_a_bound(refusal)
        or (_withheld(refusal) and _withheld(tried.refusal))
    ):
        return Decision(attempt)
    return Decision(attempt, now + timedelta(seconds=retry.delay(attempt)))


def waiting(tried: Tried, now) -> bool:
    """Whether the next try is still ahead of `now`."""
    return tried.next_attempt_at is not None and tried.next_attempt_at > now
