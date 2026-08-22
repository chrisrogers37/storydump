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

    page = adapter.list_files(source_ref)
    while page.next_page_token is not None:      # None == genuinely exhausted
        page = adapter.list_files(source_ref, page_token=page.next_page_token)

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
from typing import Optional

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
    fetch_calls: list[str] = field(default_factory=list)

    def list_files(
        self,
        source_ref: str,
        *,
        page_token: Optional[str] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> DrivePage:
        idx = len(self.list_calls)
        self.list_calls.append(page_token)
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

    def fetch_bytes(self, file_id: str) -> bytes:
        self.fetch_calls.append(file_id)
        if file_id in self.fetch_errors:
            raise self.fetch_errors[file_id]
        try:
            return self.contents[file_id]
        except KeyError:
            raise DriveTerminalError(f"no such file: {file_id}") from None
