"""Provider-neutral media read port and pure provider-to-adapter registry.

Adapters own config validation and checkpoint progress. The sync reserves only
``walk``, ``started_at`` and ``last_whole_started_at`` in a checkpoint mapping;
every other key belongs to the adapter. A completed page is not necessarily a
whole inventory: only ``checkpoint_whole`` may authorize absence judgments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Mapping, Optional, Protocol, TypeVar

from src.exceptions.base import StorydumpError


class MediaError(Exception):
    """The provider answered and the read did not happen."""


class MediaRetryableError(MediaError):
    """A retry may help."""


class MediaTerminalError(MediaError):
    """The requested media cannot be read; retrying cannot repair it."""


class MediaLostResponse(Exception):
    """No provider answer exists; deliberately not a MediaError."""


class MediaSourceGone(StorydumpError):
    """The configured source no longer exists."""


class MediaCredentialDead(StorydumpError):
    """The provider definitively rejected the source's credential."""


class ProviderNotWired(MediaRetryableError):
    """A deployment lacks this source's adapter; the source itself is valid."""

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(f"media provider {provider!r} is not wired")


MEDIA_FAILURES = (MediaError, MediaLostResponse, MediaCredentialDead, MediaSourceGone)


def redacted_error(exc: Exception) -> Exception:
    """A safe neutral failure with the same routing classification.

    Callers raise this from None at the provider boundary, before any database
    handling can chain the original exception into a later traceback.
    """
    counters = getattr(exc, "counters", None)
    if type(counters) is not MediaTurnCounters or any(
        type(value) is not int or value < 0
        for value in (
            counters.graph_attempts,
            counters.observed_parents,
            counters.source_fence_drop,
            counters.listed_parents,
            counters.cdn_attempts,
        )
    ):
        counters = None
    if isinstance(exc, MediaSourceFenceDrop):
        clean = MediaSourceFenceDrop(counters, reason=exc.reason)
    elif isinstance(exc, MediaSelectedAssetChanged):
        clean = MediaSelectedAssetChanged()
    elif isinstance(exc, MediaSelectedAssetGone):
        clean = MediaSelectedAssetGone()
    elif isinstance(exc, MediaGraphResponseTooLarge):
        clean = MediaGraphResponseTooLarge()
    elif isinstance(exc, MediaTurnBudgetExhausted):
        clean = MediaTurnBudgetExhausted(
            counters or MediaTurnCounters(), reason=exc.reason
        )
    else:
        for error_type in (
            MediaCredentialDead,
            MediaSourceGone,
            MediaTerminalError,
            MediaRetryableError,
            MediaLostResponse,
            MediaError,
        ):
            if isinstance(exc, error_type):
                clean = error_type("media provider read failed")
                break
        else:
            raise TypeError("not a typed media failure")
    if counters is not None:
        clean.counters = counters
    return clean


#: Telegram upload limits shared by every provider's approval-card fetch.
MEDIA_CARD_MAX_BYTES = {"image": 10 * 1024 * 1024, "video": 50 * 1024 * 1024}


Observation = TypeVar("Observation")


@dataclass(frozen=True)
class MediaTurnCounters:
    """Transient provider work, never a claim of committed database progress."""

    graph_attempts: int = 0
    observed_parents: int = 0
    source_fence_drop: int = 0
    listed_parents: int = 0
    cdn_attempts: int = 0


@dataclass(frozen=True)
class MediaPage(Generic[Observation]):
    """Complete observations are separate from representable candidates.

    Tuple unpacking/indexing serves the Drive/sync port: Drive returns its
    tuple, and only past-posts consumers use observations. Landing/checkpoint CAS
    owns the separate committed-progress counters.
    """

    candidates: list[dict]
    checkpoint: Mapping[str, Any]
    observations: tuple[Observation, ...] = ()
    counters: MediaTurnCounters = MediaTurnCounters()
    diagnostics: tuple[str, ...] = ()
    outcome: str = "page"
    #: Targeted enrichment only: the parents this turn read that must wait (a
    #: retryable read) and those the provider refused for good. Neither is in
    #: ``observations``; a queued parent in neither is simply not yet read.
    deferred: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    def __iter__(self):
        yield self.candidates
        yield self.checkpoint

    def __getitem__(self, index):
        return (self.candidates, self.checkpoint)[index]


SOURCE_FENCE_REASONS = frozenset(
    {
        "source_fence_drop",
        "source_capture_missing",
        "source_capture_mismatch",
        "source_off",
        "source_unavailable",
        "origin_unavailable",
        "generation_unavailable",
        "credential_unavailable",
        "past_posts_not_activated",
        "source_not_found",
        "source_epoch_changed",
        "source_generation_changed",
        "source_credential_changed",
        "unsupported_source_provider",
    }
)


class MediaSourceFenceDrop(MediaRetryableError):
    """Captured source authorization no longer permits serving this result."""

    def __init__(
        self,
        counters: MediaTurnCounters | None = None,
        *,
        reason: str = "source_fence_drop",
    ):
        if reason not in SOURCE_FENCE_REASONS:
            raise ValueError("unknown source fence reason")
        self.reason = reason
        self.counters = counters or MediaTurnCounters(source_fence_drop=1)
        super().__init__("source_fence_drop")


class MediaTurnBudgetExhausted(MediaRetryableError):
    """A bounded replay cannot advance without inventing scan completion."""

    def __init__(self, counters: MediaTurnCounters, *, reason: str):
        if reason not in ("graph_attempt_budget", "parent_replay_budget"):
            raise ValueError("unknown media turn budget")
        self.counters, self.reason = counters, reason
        super().__init__("Instagram timestamp boundary exceeded the page budget")


class MediaSelectedAssetChanged(MediaRetryableError):
    """Fresh bytes cannot inherit another observed asset's validation."""

    reason = "selected_asset_changed"

    def __init__(self):
        super().__init__(self.reason)


class MediaSelectedAssetGone(MediaTerminalError):
    """A complete provider read no longer contains the captured file."""

    reason = "selected_asset_gone"

    def __init__(self):
        super().__init__(self.reason)


class MediaGraphResponseTooLarge(MediaRetryableError):
    """A bounded Graph reply exceeded the configured response byte cap."""

    reason = "graph_response_too_large"

    def __init__(self):
        super().__init__(self.reason)


class MediaAdapter(Protocol):
    """One source's listing and both media-fetch paths use the same adapter."""

    async def list_changes(
        self,
        config: Mapping[str, Any],
        checkpoint: Optional[Mapping[str, Any]],
        *,
        source_id: str,
        workspace_id: str,
    ) -> tuple[list[dict], Mapping[str, Any]] | MediaPage: ...

    async def fetch_bytes(
        self,
        config: Mapping[str, Any],
        file_ref: str,
        *,
        source_id: str,
        workspace_id: str,
        max_bytes: int,
    ) -> tuple[bytes, str, Optional[str]]: ...

    def initial_checkpoint(self) -> Mapping[str, Any]: ...

    def checkpoint_incomplete(
        self, checkpoint: Optional[Mapping[str, Any]]
    ) -> bool: ...

    def checkpoint_whole(self, checkpoint: Optional[Mapping[str, Any]]) -> bool:
        """True only for a complete inventory that skipped nothing."""
        ...


MediaAdapters = Mapping[str, MediaAdapter]


def get_adapter(adapters: MediaAdapters, provider: str) -> MediaAdapter:
    """Resolve exactly this provider. No database, credentials, or fallback."""
    try:
        return adapters[provider]
    except KeyError:
        raise ProviderNotWired(provider) from None
