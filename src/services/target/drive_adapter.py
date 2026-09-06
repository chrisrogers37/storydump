"""The Drive read leg — one once-owned seam for W6 and W5b (#982, `02` §6).

**Nothing reaches a real Google endpoint until M.3** (#862: "runs against
stub/sandbox targets until M.3 — there is no shadow phase"). So this module
ships the SEAM and a scripted stub: W6's `sync_media_source` /
`first_ingest_chunk` and W5b's `media_fetch` both consume a duck-typed adapter
(``list_files`` / ``fetch_bytes``), and the gate injects :class:`StubDriveAdapter`.
The real Drive adapter — egress-floor httpx against `www.googleapis.com` — is
deliberately NOT here: building it now creates an untestable-until-M.3 path that
reads as coverage.

Two workstreams need the same door, which is why it is once-owned rather than
built twice. `src/services/media_sources/google_drive_provider.py` is the legacy
reference for request shapes; the target tier imports nothing legacy.

## A BOUND MUST BE DETECTABLE BY THE CALLER

The read leg's whole hazard is that **truncation looks exactly like paging**. A
caller handed 200 files cannot tell "that is all of them" from "that is the
first 200" — and a limit enforced by silently dropping the rest is data loss
wearing a bound's clothing (astrid's phrase, found in her own W6 code before
review).

So exhaustion is carried STRUCTURALLY, not inferred from a count:

    page = await adapter.list_files(source.config)
    while page.next_page_token is not None:      # None == genuinely exhausted
        page = await adapter.list_files(source.config, page_token=page.next_page_token)

- ``next_page_token is None`` is the ONLY statement that the listing is
  complete. A full page is not that statement, and neither is a short one.
- ``page_size`` bounds ONE PAGE, never the traversal. Capping a page is always
  accompanied by a token, so a bound can be resumed rather than absorbed.
- A caller that ignores the token gets less data, but **cannot mistake it for
  all the data** — the token is right there, non-None, saying otherwise.

There is deliberately no `max_total` parameter. A total cap is the one shape
that cannot be announced through this protocol: it would have to either lie
(return None and claim exhaustion) or hand back a token it will not honour.
Whoever needs one should bound the CALLER's loop, where the truncation is the
caller's own decision and visible in its code.

## The door takes the SOURCE'S CONFIG, not a bare reference

`media_sources.config` is D37's `{v, folder_ref, root_name?}`, and `root_name`
scopes listing to a subfolder. A door that accepted only a folder reference
could not express that, so a source configured with a subfolder would be listed
from the wrong place — quietly, and looking like a correct empty-or-full
listing either way.

The whole mapping is passed rather than destructured parameters, so a later
config key reaches the door without a signature change and without every caller
being edited to forward it.

## THE DOOR REFUSES A SHAPELESS CONFIG, AND THE REFUSAL IS THE CONTRACT'S

Being able to CARRY `folder_ref` is not the same as REQUIRING it, and only the
first was fixed when the config mapping replaced a bare reference. A config of
`{"v": 1}` with no `folder_ref` still listed successfully — so the wrong answer
that looks right did not go away, it moved from *inexpressible* to *omittable*.
Same destination, quieter road: a subfolder source without `folder_ref` lists
from the drive root and looks correct whether it comes back full or empty.
`ck_sources_config_v` cannot help — it checks only that `v` is a number, so the
database will not refuse it either.

:func:`validate_source_config` is therefore a **module-level function and part
of the adapter contract, not the stub's private behaviour** — and that placement
is the point. A guard living only in the stub would let a consumer go green
against a refusal the real door never inherited: the same failure as a sync stub
defining a shape the real implementation cannot keep. **Every implementation of
this seam calls it first.** D37 makes config adapter-defined, which makes the
adapter the owner of validating it.

Terminal, not retryable: retrying a shapeless config cannot fix it.

## AWAITABLE, because the real door cannot be anything else

Both legs are `async def` even though the stub does no I/O. The real adapter is
egress-floor httpx and `egress.request` is a coroutine, so a sync seam would set
a contract the real implementation could only keep through an event-loop bridge
— and the stub would have quietly defined the shape of a door it never has to
open. The consumer awaits either way.

## Errors are typed, and the type is the routing

Same posture as :mod:`meta_adapter`: the executor never parses provider error
dicts.

- :class:`DriveRetryableError` — Drive answered, the effect did not happen, a
  retry may help. The DEFAULT for anything not definitively terminal.
- :class:`DriveTerminalError` — definitive and permanent for this input (file
  gone, not a file, no permission). Retrying cannot fix it.
- :class:`DriveAuthError` — the credential is the problem. Distinct because it
  routes to the credential lifecycle (D31), not to the job ladder.
- :class:`DriveLostResponse` — the transport died and NO ANSWER exists.
  Deliberately NOT a :class:`DriveError` subclass, for the same reason
  `MetaLostResponse` is not: "we do not know" must not be catchable as "it
  failed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

#: Provider string, matching `ck_sources_provider` / `ck_credentials_provider`.
PROVIDER = "gdrive"

#: Default page bound. A page bound, never a traversal bound — see the header.
DEFAULT_PAGE_SIZE = 100


class DriveError(Exception):
    """Drive answered and the effect definitively did not happen."""


class DriveRetryableError(DriveError):
    """Answered, did not happen, retry may help. The default classification."""


class DriveTerminalError(DriveError):
    """Definitive and permanent for this input; no retry can fix it."""


class DriveAuthError(DriveError):
    """The credential is the problem — routes to the lifecycle, not the ladder."""


class DriveLostResponse(Exception):
    """The transport died; the call may or may not have landed.

    NOT a DriveError: "no answer exists" must not be catchable as "it failed".
    """


def checkpoint_incomplete(checkpoint: Optional[Mapping[str, Any]]) -> bool:
    """Whether a listing has more to give. The ONLY complete checkpoint is the
    bare ``{"v": 1}``: a `page_token` (more pages of the current folder), a
    `current` (a folder being listed) or a `queue` (folders still to list)
    each say the walk is not finished — the bound announced, never absorbed.
    The chunk chain and the stub agree on this one definition."""
    if not checkpoint:
        return False
    return any(checkpoint.get(k) for k in ("page_token", "current", "queue"))


def validate_source_config(config: Mapping[str, Any]) -> None:
    """Refuse a config the door cannot honour. PART OF THE SEAM CONTRACT.

    Every implementation calls this first — the stub here, and the real adapter
    when it lands. Placed at module level rather than inside the stub so the
    real door inherits the obligation instead of a consumer going green against
    a refusal only the stand-in performs.

    Only `folder_ref` is required. `root_name` is genuinely optional (absent
    means the folder itself), and `v` is the database's business
    (`ck_sources_config_v`). Deliberately not a schema validator: the failure
    this closes is an ABSENT LOCATION, which is the one that lists from the
    wrong place while looking correct.
    """
    if "folder_ref" not in config:
        raise DriveTerminalError(
            "config carries no folder_ref — refusing to guess a root. A source"
            " listed from the drive root looks correct whether it comes back"
            " full or empty, which is why this is a refusal and not a default."
        )


@dataclass(frozen=True)
class DriveFile:
    """One listed file. `size_bytes`/`modified_at` may be absent upstream."""

    file_id: str
    name: str
    mime_type: str
    size_bytes: Optional[int] = None
    modified_at: Optional[str] = None


@dataclass(frozen=True)
class DrivePage:
    """One page, and the ONLY statement about whether more exist.

    `next_page_token is None` means exhausted. Nothing about `len(files)` means
    exhausted — see the module header.
    """

    files: tuple[DriveFile, ...]
    next_page_token: Optional[str] = None

    @property
    def exhausted(self) -> bool:
        """True only when the provider said there is no more."""
        return self.next_page_token is None


@dataclass(frozen=True)
class ProbeResult:
    """`probe`'s answer: `01`:78's ``ok | error-class``.

    A refusal is a RESULT here, not an exception, because that is the whole
    difference between `probe` and `list_changes`. `probe` is asked "can this
    config be used", and "no, the folder is gone" is a complete answer to that
    question; a caller validating a connect form should not have to catch to
    learn it.

    THE ERROR CLASS IS THE EXISTING TAXONOMY, NOT A SECOND ONE. `error` holds
    the very exception the transport raised, so a caller classifies with the
    same predicate `media_sync` already uses —
    ``isinstance(result.error, (DriveSourceGone, DriveCredentialDead))`` is
    persistent, everything else is not. A parallel vocabulary of probe-specific
    strings would be a second thing to keep in step with `02` §2's state
    machine, and the two would drift on the first new status code.

    `error_class` is the name, for logging and for anything that has to store
    the verdict rather than branch on it.
    """

    ok: bool
    error: Optional[BaseException] = None

    @property
    def error_class(self) -> Optional[str]:
        return None if self.error is None else type(self.error).__name__


@dataclass
class StubDriveAdapter:
    """Scripted adapter for the gate and the composition root until M.3.

    Every outcome is reachable and each can be made to fail independently —
    a stub whose error paths cannot be selected is a stub that only ever proves
    the happy path.
    """

    #: file_id -> bytes, for the fetch leg.
    contents: dict[str, bytes] = field(default_factory=dict)
    #: Pages handed out in order; each is (files, next_page_token).
    pages: list[DrivePage] = field(default_factory=list)
    #: file_id -> exception INSTANCE to raise from fetch_bytes.
    fetch_errors: dict[str, Exception] = field(default_factory=dict)
    #: Raised by list_files on the call whose index matches, if present.
    list_errors: dict[int, Exception] = field(default_factory=dict)

    list_calls: list[Optional[str]] = field(default_factory=list)
    configs_seen: list[dict] = field(default_factory=list)
    fetch_calls: list[str] = field(default_factory=list)

    async def list_files(
        self,
        config: Mapping[str, Any],
        *,
        page_token: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> DrivePage:
        validate_source_config(config)
        idx = len(self.list_calls)
        # Record the FULL config, not just the token: a consumer test asserting
        # that `root_name` reached the door is the only thing standing between
        # a subfolder-scoped source and being listed from the drive root.
        self.list_calls.append(page_token)
        self.configs_seen.append(dict(config))
        if idx in self.list_errors:
            raise self.list_errors[idx]
        if not self.pages:
            return DrivePage(files=(), next_page_token=None)
        page = self.pages[min(idx, len(self.pages) - 1)]
        # Honour the page bound the caller asked for, and say so with a token
        # rather than dropping the remainder — the header's whole contract.
        if len(page.files) > page_size:
            return DrivePage(
                files=page.files[:page_size],
                next_page_token=page.next_page_token or f"resume-{idx}",
            )
        return page

    async def fetch_bytes(self, file_id: str) -> bytes:
        self.fetch_calls.append(file_id)
        if file_id in self.fetch_errors:
            raise self.fetch_errors[file_id]
        try:
            return self.contents[file_id]
        except KeyError:
            raise DriveTerminalError(f"no such file: {file_id}") from None
