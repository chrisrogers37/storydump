"""Classification of one `/health/delivery` reading into a verdict.

Pure functions over the endpoint's JSON: the last hour's sends got through,
some of them failed or are ambiguous, or the reading cannot be read.
`classify` is the entry point, and the scheduling monitor judges delivery
with it.
"""

from __future__ import annotations

import json

DELIVERING = "delivering"
FAILING = "failing"
UNREACHABLE = "unreachable"

#: What `classify` reads, and so, with its `window_seconds`, all
#: `/health/delivery` publishes (a test holds the route to these): the hour's
#: sends, the failed or ambiguous among them, and the failures by class.
_COUNTS = ("sent_in_window", "failed_or_ambiguous")
_BY_CLASS = "by_class"


class Verdict:
    """What one reading means, before any history is applied."""

    def __init__(self, state: str, detail: str, payload: dict | None = None):
        self.state = state
        self.detail = detail
        self.payload = payload or {}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Verdict({self.state!r}, {self.detail!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Verdict) and (self.state, self.detail) == (
            other.state,
            other.detail,
        )


def _is_count(v) -> bool:
    """A whole, non-negative JSON number. `bool` is an `int` in Python and
    `true` is not a count, so it is excluded explicitly."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _unreadable(status: int, body: str) -> str:
    """Why an answer is not a reading: the transport's error when no answer
    came (status 0), else the status, with the body's `reason` when it names
    one, as the route's 503s do (`statement_timeout`, `pool_saturated`)."""
    if status == 0:
        return body.strip()[:200]
    try:
        reason = json.loads(body).get("reason")
    except (ValueError, TypeError, AttributeError):
        reason = None
    if isinstance(reason, str) and reason:
        return f"HTTP {status} ({reason})"
    return f"HTTP {status}"


def _failures(by_class: dict) -> str:
    """The alerting failures by class and provider code, as one clause. A
    deferral (a 429) is context, never a failure, so a class with nothing
    alerting is left out."""
    parts = []
    for name in sorted(by_class):
        entry = by_class[name] if isinstance(by_class[name], dict) else {}
        alerting = entry.get("alerting")
        if not (_is_count(alerting) and alerting > 0):
            continue
        codes = entry.get("codes") if isinstance(entry.get("codes"), dict) else {}
        said = ", ".join(f"{code}×{n}" for code, n in sorted(codes.items()))
        parts.append(f"{name}: {said}" if said else str(name))
    return "; ".join(parts) or "no class recorded"


def classify(status: int, body: str) -> Verdict:
    """One reading → one state. Pure, so every rule is a unit test.

    **Strict about the payload, deliberately**, as the other classifiers are:
    a missing or mistyped count is `unreachable`, never an hour that
    delivered. One failed or ambiguous send is failing: the number the
    endpoint publishes for its poller, with no threshold in the app.
    """
    if status != 200:
        return Verdict(UNREACHABLE, _unreadable(status, body))
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return Verdict(UNREACHABLE, "response was not JSON")
    if not isinstance(data, dict):
        return Verdict(UNREACHABLE, "response was not an object")
    for key in _COUNTS:
        if not _is_count(data.get(key)):
            return Verdict(UNREACHABLE, f"{key} missing or not a whole count")
    if not isinstance(data.get(_BY_CLASS), dict):
        return Verdict(UNREACHABLE, f"{_BY_CLASS} missing or not an object")
    sent, failed = data["sent_in_window"], data["failed_or_ambiguous"]
    if failed:
        return Verdict(
            FAILING,
            f"{failed} of the last hour's sends failed or are ambiguous "
            f"({sent} sent; {_failures(data[_BY_CLASS])})",
            data,
        )
    return Verdict(
        DELIVERING,
        f"no send failed or is ambiguous in the last hour ({sent} sent)",
        data,
    )
