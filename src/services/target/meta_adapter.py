"""L.5 slice 2 — the Meta adapter seam: typed errors + the stub/sandbox
implementation (#915, `02` §6/§8).

**This module owns the SEAM and the taxonomy, not the wire.** The executor
consumes a duck-typed adapter (``create_container`` / ``container_status`` /
``publish`` / ``usage``); :class:`StubMetaAdapter` is one implementation of
it, and it is what the L.5 gate and the unit tests inject, so every pipeline
proof runs with no network in it.

The real one is :class:`~src.services.target.instagram_graph.InstagramGraphAdapter`
(`instagram_graph.py`, #1220 step 3) — egress-floor-backed httpx against
``graph.instagram.com``, mapping every answer onto the taxonomy below. **It is
what the worker's composition root wires** (`worker.py::_meta_from_env`),
which is exactly what the seam was built for: the pipeline routes on these
error types and never learns which adapter produced them.

(This header read "nothing reaches a real Meta endpoint until M.3" (#862)
until the audit re-read it: true when the seam shipped, the opposite of the
truth once the Graph adapter landed — #1325, TD-B18.)

## The taxonomy IS the routing table

The pipeline never parses provider error dicts. The adapter raises typed
errors and the executor routes on type — each class selects one `02` behavior:

- :class:`MetaCapDeferral` — **error 9**, Meta's publish cap (`02` §8): a cap,
  not a fault. Defer to the account's next slot, no quarantine, debit stands.
- :class:`MetaRetryableError` — Meta ANSWERED and the effect definitively did
  not happen; retry may help. This is the DEFAULT for every code outside the
  two closed sets (the known rate limits 4/17 and transient 2 land here by
  that default — a separate retryable set would be dead code, since nothing
  falls past it). Retries ride the job ladder (`05` backoff); attempts
  exhausted → G5 poison.
- :class:`MetaTerminalError` — Meta's definitive answer to THIS call (9004:
  "the media could not be fetched"). Definitive for the call, not for the
  file: the pipeline rides its own short ladder on it (2026-09-12).
- :class:`MetaLostResponse` — the transport died (timeout, connection loss,
  5xx): the call may or may not have landed and NO ANSWER exists, which is
  why it is deliberately NOT a MetaError subclass. The executor catches
  exactly this type: on a publish it parks ``publishing_ambiguous`` with zero
  retries (R8); on a container create it resolves the permit
  failed/lost_response and retries on the ladder (recoverable, `02` §6).
  **A real adapter that forgets to wrap a transport error does not corrupt
  anything**: the unwrapped exception propagates, the run crashes, the lease
  expires, and the resume protocol reaches the SAME parked/repermitted state
  from the unresolved permit — eager typing is an optimization, the resume is
  the guarantee.

**An unknown-but-definitive code defaults to RETRYABLE, not terminal**: the
poison ladder bounds a persistent unknown at a human (`review_required`),
while a terminal default would refund + permanently fail intents on any new
error code Meta mints. Codes move between sets by editing the closed sets
below — with `0.4`-grade evidence, not from a single incident.
"""

from __future__ import annotations

import itertools
from typing import Optional

from src.exceptions.base import StorydumpError

#: Meta's publish-cap error code (`02` §8; verified against primary docs at 0.4).
CAP_ERROR_CODE = 9

#: Definitive for the call: 9004 = Meta could not fetch the media this time.
TERMINAL_CODES = frozenset({9004})
#: Meta's OAuth error code. The real adapter reports a dead or absent token as
#: a retryable error with this code, and the pipeline hands it straight to a
#: human (`review_required`): a retry cannot mint a credential.
OAUTH_ERROR_CODE = 190


class MetaLostResponse(StorydumpError):
    """The transport died mid-call: no answer exists, the effect is UNKNOWN.
    Deliberately not a MetaError — those are definitive ANSWERS."""


class MetaError(StorydumpError):
    """A DEFINITIVE, typed answer from Meta. Everything outside this
    hierarchy is a lost response and must be handled as ambiguous (R8)."""

    def __init__(
        self,
        *,
        code: int,
        subcode: Optional[int] = None,
        message: str = "",
        detail: Optional[dict] = None,
    ):
        self.code = code
        self.subcode = subcode
        #: Meta's whole answer, for the ledger (2026-09-13): `error_subcode`,
        #: `error_user_title`, `error_user_msg`, `fbtrace_id`, `http_status`,
        #: every text field already scrubbed of the token by the adapter. A
        #: refused fetch used to leave `{"error": 9004}` on its permit, and
        #: its investigation re-derived the rest from a removed deploy's logs.
        self.detail: dict = dict(detail or {})
        super().__init__(
            f"meta error {code}"
            + (f"/{subcode}" if subcode is not None else "")
            + (f": {message}" if message else "")
        )


class MetaCapDeferral(MetaError):
    """Error 9 — the publish cap. Defer, never quarantine (`02` §8)."""


class MetaRetryableError(MetaError):
    """Definitive non-effect that may succeed later; the job ladder retries."""


class MetaTerminalError(MetaError):
    """Definitive and permanent; the intent fails and the cap refunds."""


def classify_error(code: int) -> type:
    """The closed routing table, code-only. Unknown → retryable (module
    docstring). A subcode-discriminated rule gets the parameter when one
    exists (M.3) — until then a subcode arg would imply routing that
    does not happen."""
    if code == CAP_ERROR_CODE:
        return MetaCapDeferral
    if code in TERMINAL_CODES:
        return MetaTerminalError
    return MetaRetryableError


class StubMetaAdapter:
    """The sandbox Meta target until M.3, and the gate's counting instrument.

    Every method records its call (the L.3 lesson: a stub that counts, so
    at-most-once is COUNTED, not argued). Outcome scripts arm failure shapes
    per call, in order; an exhausted script means success.

    Script vocabulary (``publish_outcomes`` / ``create_outcomes`` /
    ``usage_outcomes``): ``"ok"`` · ``"error_9"`` · ``"retryable"`` ·
    ``"terminal"`` · ``"transport"``.
    ``status_script`` overrides the readiness ladder with literal
    ``status_code`` values, consumed per call. The ``transport`` outcome
    raises :class:`MetaLostResponse` — typed, but deliberately NOT a
    MetaError, because it models a lost response rather than an answer.
    """

    def __init__(
        self,
        *,
        ready_after_polls: int = 0,
        publish_outcomes: Optional[list] = None,
        create_outcomes: Optional[list] = None,
        usage_outcomes: Optional[list] = None,
        status_script: Optional[list] = None,
        quota_usage: int = 0,
        quota_total: int = 100,
    ):
        self._ready_after_polls = ready_after_polls
        self._publish_outcomes = list(publish_outcomes or [])
        self._create_outcomes = list(create_outcomes or [])
        self._usage_outcomes = list(usage_outcomes or [])
        self._status_script = list(status_script or [])
        self.quota_usage = quota_usage
        self.quota_total = quota_total
        self._ids = itertools.count(1)
        self._status_polls: dict[str, int] = {}
        self.create_calls: list[dict] = []
        self.status_calls: list[str] = []
        self.publish_calls: list[dict] = []
        self.usage_calls: list[str] = []

    def _raise_scripted(self, script: list) -> None:
        if not script:
            return
        outcome = script.pop(0)
        if outcome == "ok":
            return
        if outcome == "error_9":
            raise MetaCapDeferral(code=9, message="Application request limit reached")
        if outcome == "retryable":
            raise MetaRetryableError(code=4, message="stubbed rate limit")
        if outcome == "terminal":
            # The shape Meta answered on 2026-09-12: 9004/2207052 with a user
            # message and a trace id — the gate proves they reach the ledger.
            raise MetaTerminalError(
                code=9004,
                subcode=2207052,
                message="stubbed unparseable media",
                detail={
                    "error_subcode": 2207052,
                    "error_user_title": "Media fetch failed",
                    "error_user_msg": "Only photo or video can be accepted as media type.",
                    "fbtrace_id": "stub-fbtrace",
                    "http_status": 400,
                },
            )
        if outcome == "container_gone":
            # 2026-09-13 23:18: four seconds after Meta reported a container
            # ready, the publish call was answered "The requested resource
            # does not exist" (24/2207006). Retryable — and the float
            # recreates the container rather than re-publishing the id.
            raise MetaRetryableError(
                code=24,
                subcode=2207006,
                message="stubbed: The requested resource does not exist",
                detail={"error_subcode": 2207006, "http_status": 400},
            )
        if outcome == "transport":
            raise MetaLostResponse("stub transport lost")
        raise ValueError(f"unknown stub outcome: {outcome!r}")

    async def create_container(
        self,
        provider_account_ref: str,
        *,
        media_url: str,
        media_kind: str,
        caption: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> str:
        self.create_calls.append(
            {
                "ref": provider_account_ref,
                "media_url": media_url,
                "media_kind": media_kind,
                "caption": caption,
            }
        )
        self._raise_scripted(self._create_outcomes)
        container_id = f"ctr-{next(self._ids)}"
        self._status_polls[container_id] = 0
        return container_id

    async def container_status(
        self,
        container_id: str,
        *,
        provider_account_ref: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> str:
        # The real adapter needs the account to find its token (a resumed run
        # has no memory of the create); the stub keeps counting by container.
        self.status_calls.append(container_id)
        if self._status_script:
            return self._status_script.pop(0)
        polls = self._status_polls.get(container_id, 0)
        self._status_polls[container_id] = polls + 1
        return "FINISHED" if polls >= self._ready_after_polls else "IN_PROGRESS"

    async def publish(
        self,
        provider_account_ref: str,
        container_id: str,
        *,
        workspace_id: Optional[str] = None,
    ) -> str:
        self.publish_calls.append(
            {"ref": provider_account_ref, "container_id": container_id}
        )
        self._raise_scripted(self._publish_outcomes)
        return f"media-{next(self._ids)}"

    async def usage(self, provider_account_ref: str) -> dict:
        self.usage_calls.append(provider_account_ref)
        self._raise_scripted(self._usage_outcomes)
        return {"quota_usage": self.quota_usage, "quota_total": self.quota_total}
