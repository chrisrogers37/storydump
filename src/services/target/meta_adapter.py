"""L.5 slice 2 — the Meta adapter seam: typed errors + the stub/sandbox
implementation (#915, `02` §6/§8).

**Nothing reaches a real Meta endpoint until M.3** (#862: "runs against
stub/sandbox targets until M.3 — there is no shadow phase"). So this module
ships the SEAM and the sandbox: the executor consumes a duck-typed adapter
(``create_container`` / ``container_status`` / ``publish`` / ``usage``), the
gate injects :class:`StubMetaAdapter`, and the composition root wires the same
stub until the cutover. The real Graph adapter (egress-floor-backed httpx) is
deliberately NOT here — building it now would create an untestable-until-M.3
path that reads as coverage.

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
- :class:`MetaTerminalError` — definitive and permanent (9004: the file cannot
  be parsed — no retry can fix the media). Fail + refund.
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

#: Definitive-permanent codes: 9004 = the uploaded file cannot be parsed.
TERMINAL_CODES = frozenset({9004})


class MetaLostResponse(StorydumpError):
    """The transport died mid-call: no answer exists, the effect is UNKNOWN.
    Deliberately not a MetaError — those are definitive ANSWERS."""


class MetaError(StorydumpError):
    """A DEFINITIVE, typed answer from Meta. Everything outside this
    hierarchy is a lost response and must be handled as ambiguous (R8)."""

    def __init__(self, *, code: int, subcode: Optional[int] = None, message: str = ""):
        self.code = code
        self.subcode = subcode
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
            raise MetaTerminalError(code=9004, message="stubbed unparseable media")
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

    async def container_status(self, container_id: str) -> str:
        self.status_calls.append(container_id)
        if self._status_script:
            return self._status_script.pop(0)
        polls = self._status_polls.get(container_id, 0)
        self._status_polls[container_id] = polls + 1
        return "FINISHED" if polls >= self._ready_after_polls else "IN_PROGRESS"

    async def publish(self, provider_account_ref: str, container_id: str) -> str:
        self.publish_calls.append(
            {"ref": provider_account_ref, "container_id": container_id}
        )
        self._raise_scripted(self._publish_outcomes)
        return f"media-{next(self._ids)}"

    async def usage(self, provider_account_ref: str) -> dict:
        self.usage_calls.append(provider_account_ref)
        self._raise_scripted(self._usage_outcomes)
        return {"quota_usage": self.quota_usage, "quota_total": self.quota_total}
