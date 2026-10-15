"""The Drive read leg's seam contract and its typed errors (#982).

`sync_media_source` / `first_ingest_chunk` and `media_fetch` consume an adapter
through `WorkerDeps.adapters`. This module holds the Drive-specific config
refusal, checkpoint predicates and error types. The provider-neutral port and
routing taxonomy live in :mod:`media_adapters`. The live door is
:mod:`google_drive_adapter` (`list_changes` / `fetch_bytes`).

Two consumers need the same door, which is why it is once-owned rather than
built twice. The target tier imports nothing legacy (#1216).

## The door takes the SOURCE'S CONFIG, not a bare reference

`media_sources.config` is `{v, folder_ref, root_name?}`, and `root_name`
scopes listing to a subfolder. A door that accepted only a folder reference
could not express that, so a source configured with a subfolder would be listed
from the wrong place — quietly, and looking like a correct empty-or-full
listing either way.

The whole mapping is passed rather than destructured parameters, so a later
config key reaches the door without a signature change and without every caller
being edited to forward it.

## THE DOOR REFUSES A SHAPELESS CONFIG, AND THE REFUSAL IS THE CONTRACT'S

Being able to CARRY `folder_ref` is not the same as REQUIRING it. Without the
refusal, a config of `{"v": 1}` with no `folder_ref` lists successfully: the
wrong answer that looks right is *omittable* rather than *inexpressible*.
Same destination, quieter road: a subfolder source without `folder_ref` lists
from the drive root and looks correct whether it comes back full or empty.
`ck_sources_config_v` cannot help — it checks only that `v` is a number, so the
database will not refuse it either.

:func:`validate_source_config` is therefore a **module-level function and part
of the adapter contract, not any one implementation's private behaviour** — and
that placement is the point. A guard living inside one door would let a consumer
go green against a refusal another door never inherited. **Every implementation
of this seam calls it first.** Config is adapter-defined, which makes the
adapter the owner of validating it.

Terminal, not retryable: retrying a shapeless config cannot fix it.

## AWAITABLE, because the real door cannot be anything else

Both legs are `async def`. The real adapter is egress-floor httpx and
`egress.request` is a coroutine, so a sync seam would set a contract the real
implementation could only keep through an event-loop bridge. The consumer
awaits either way.

## Errors are typed, and the type is the routing

Same posture as :mod:`meta_adapter`: the executor never parses provider error
dicts.

- :class:`DriveRetryableError` — Drive answered, the effect did not happen, a
  retry may help. The DEFAULT for anything not definitively terminal.
- :class:`DriveTerminalError` — definitive and permanent for this input (file
  gone, not a file, no permission). Retrying cannot fix it.
- :class:`DriveLostResponse` — the transport died and NO ANSWER exists.
  Deliberately NOT a :class:`DriveError` subclass, for the same reason
  `MetaLostResponse` is not: "we do not know" must not be catchable as "it
  failed".

A dead credential routes to the lifecycle rather than the ladder, and that type
lives with the consumer that classifies it (`media_sync.DriveCredentialDead`).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from src.services.target import vocabulary
from src.services.target.media_adapters import (
    MediaError,
    MediaRetryableError,
    MediaTerminalError,
    MediaLostResponse,
)

#: Provider string, matching `ck_sources_provider` / `ck_credentials_provider`.
PROVIDER = vocabulary.PROVIDER_GDRIVE


class DriveError(MediaError):
    """Drive answered and the effect definitively did not happen."""


class DriveRetryableError(DriveError, MediaRetryableError):
    """Answered, did not happen, retry may help. The default classification."""


class DriveTerminalError(DriveError, MediaTerminalError):
    """Definitive and permanent for this input; no retry can fix it."""


class DriveMediaTooLarge(DriveTerminalError):
    """A file over the cap the caller can carry (the approval card's upload
    limit): known from metadata, refused before any download."""


class DriveMediaGone(DriveTerminalError):
    """The FILE is gone (deleted or unshared) — the source is fine."""


class DriveLostResponse(MediaLostResponse):
    """The transport died; the call may or may not have landed.

    NOT a DriveError: "no answer exists" must not be catchable as "it failed".
    """


def checkpoint_incomplete(checkpoint: Optional[Mapping[str, Any]]) -> bool:
    """Whether a listing has more to give. The ONLY complete checkpoint is the
    version key alone (`v`), with no `page_token`, `current` or `queue`: a
    `page_token` (more pages of the current folder), a `current` (a folder
    being listed) or a `queue` (folders still to list) each say the walk is
    not finished — the bound announced, never absorbed. The predicate is
    version-agnostic by design (it tests the three keys and ignores `v`), so
    the walk's `{"v": 2, ...}` cursors and any successor share one definition."""
    if not checkpoint:
        return False
    return any(checkpoint.get(k) for k in ("page_token", "current", "queue"))


def walk_saw_whole_tree(checkpoint: Optional[Mapping[str, Any]]) -> bool:
    """Whether a completed walk listed every folder under the connected one:
    the one fact that lets the sync judge what the walk did NOT list (#1545).
    Positive by design. Only a completed cursor whose adapter says `whole`,
    with no skip beside it (`truncated`, `partial`), is a whole walk. A cursor
    that does not say so reads as partial, and nothing is judged: an adapter
    or a fake that predates the key, or a lister that returns only changes."""
    if not checkpoint or checkpoint_incomplete(checkpoint):
        return False
    return (
        checkpoint.get("whole") is True
        and not checkpoint.get("truncated")
        and not checkpoint.get("partial")
    )


def validate_source_config(config: Mapping[str, Any]) -> None:
    """Refuse a config the door cannot honour. PART OF THE SEAM CONTRACT.

    Every implementation calls this first. Placed at module level rather than
    inside any one door so each implementation inherits the obligation instead
    of a consumer going green against a refusal only one of them performs.

    Only `folder_ref` is required. `root_name` is genuinely optional (absent
    means the folder itself), and `v` is the database's business
    (`ck_sources_config_v`). Deliberately not a schema validator: the failure
    it refuses is an ABSENT LOCATION, which is the one that lists from the
    wrong place while looking correct.
    """
    if "folder_ref" not in config:
        raise DriveTerminalError(
            "config carries no folder_ref — refusing to guess a root. A source"
            " listed from the drive root looks correct whether it comes back"
            " full or empty, which is why this is a refusal and not a default."
        )
