"""Classification of one `/health/scheduling` reading into a verdict.

Pure functions over the endpoint's JSON: a cursor axis (stalled destinations
and the worst lag) and a worker axis (the age of the last finished system job
and the worst overdue one). `classify` is the entry point; `commits` reads the
two services' commits from the same reading.
"""

from __future__ import annotations

import json
import re


#: 40x the clock cadence (`work_loop.py` `clock_interval_seconds = 15.0`). The
#: margin is deliberate: a threshold near the noise floor of a healthy lag buys
#: nothing and costs false alarms.
DEFAULT_STALL_THRESHOLD_S = 600


HEALTHY = "healthy"
STALLED = "stalled"
NO_SIGNAL = "no-signal"
UNREACHABLE = "unreachable"

#: The worker is not finishing system jobs (#1120). An OUTAGE, and the one state
#: that can be raised while the cursor axis is empty — which is the whole point:
#: `NO_SIGNAL` is the honest cursor answer on an estate with no destinations, and
#: it is also what a dead worker produces there.
WORKER_DOWN = "worker-down"

#: Neither axis can answer. The cursor has no population AND the worker has left
#: no proof of life — a fresh database where no system job has ever finished.
#: **Deliberately not `NO_SIGNAL`**: that one
#: carries a positive fact (the worker is provably alive, only the cursor is
#: blind), and collapsing the two would hide the strictly worse state inside the
#: better-understood one.
WORKER_UNKNOWN = "worker-unknown"

#: No system job finished within this long => the worker is not working.
#:
#: **It rests on the FASTEST unconditional recurring system kinds**, not the
#: slowest: `last_success_age_seconds` is the age of the freshest success, so the
#: kind that beats most often sets how stale a healthy worker can ever read.
#: Those are `reap_expired` and `reconcile_ambiguous`, every 60 s (`src/worker.py`'s
#: `recurring`), so 600 s is ten beats. A late beat or an ordinary restart never
#: reaches it; a worker that has stopped finishing work does — dead, stuck, or a
#: deploy that left nothing serving.
#:
#: **Retire or slow those beats and this must rise with them.** The six-hourly
#: kinds alone need at least 18 h (three six-hour beats); 600 s against them flags
#: a healthy worker every cycle. `tests/src/test_worker.py` fails when three beats
#: (two plus one of slack) of the bare composition's fastest recurring kind no
#: longer fit.
#:
#: **The worker's loop watchdog is sized against this too.** A worker whose event
#: loop stopped ends itself after `WorkerConfig.loop_stall_seconds` and is
#: restarted; that bound, the restart and the first beat after it are meant to
#: fit inside this threshold, so a stall the worker repairs by itself is not
#: flagged. The same test file pins the sum: lower this, or raise that bound, and
#: it fails.
DEFAULT_WORKER_STALE_S = 600

#: A due system job nobody claimed for this long. It sees what the staleness
#: floor cannot: one kind left unclaimed while another keeps finishing and keeps
#: the age fresh — a stuck lane. For a dead worker the staleness floor fires first.
DEFAULT_WORKER_OVERDUE_S = 900


#: What `classify` and `commits` read, and so all `/health/scheduling`
#: publishes (a test holds the route to these): the cursor axis's counts and
#: its lag, and the worker block's counts and ages, which `classify` reads.
_COUNTS = ("stalled", "accounts_active")
_LAG = "max_lag_seconds"
_WORKER = "worker"
_WORKER_COUNTS = ("succeeded_ever", "overdue_ready")
_WORKER_AGES = ("last_success_age_seconds", "max_overdue_seconds")
#: The API's commit and the worker's, which `commits` reads for the scheduling
#: monitor's version split. `classify` ignores both, because a reading from
#: before either was published must not read as unreachable.
_API_COMMIT = "api_commit"
_WORKER_COMMIT = "commit"
#: A commit as either service publishes it, compared on its first seven.
_COMMIT = re.compile(r"[0-9a-f]{7,40}")


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


#: A JSON number that must be a whole count. `bool` is an `int` in Python and
#: `true` is not a count, so it is excluded explicitly — the cursor axis already
#: did this and the worker axis needs the identical rule.
def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


#: The same, for the fields whose ABSENCE of a value is meaningful. `null` is
#: legal; a missing key is not, which is why the caller passes a sentinel rather
#: than `None` when the key is not present.
def _is_count_or_null(v) -> bool:
    return v is None or _is_count(v)


#: Distinguishes "the key is absent" from "the key is null". `None` cannot do it
#: here because `None` is a legal VALUE for two of these fields.
_ABSENT = object()


def _worker_state(data: dict, *, stale_s: int, overdue_s: int) -> tuple[str, str]:
    """(state, detail) for the worker axis — `absent` when it is not served.

    Same strictness as the cursor axis: a block that is PRESENT but mistyped is
    `invalid`, never healthy. An ABSENT block gets its own value so its detail
    says which fault it is; `classify` treats both as a broken instrument.
    """
    if _WORKER not in data:
        return "absent", "the endpoint does not serve a worker block"
    w = data.get(_WORKER)
    if not isinstance(w, dict):
        return "invalid", "worker was not an object"
    for key in _WORKER_COUNTS:
        if not _is_count(w.get(key)):
            return "invalid", f"worker.{key} missing or not an integer"
    for key in _WORKER_AGES:
        if not _is_count_or_null(w.get(key, _ABSENT)):
            return "invalid", f"worker.{key} was neither null nor an integer"
    age = w.get("last_success_age_seconds")
    overdue = w.get("max_overdue_seconds")
    if overdue is not None and overdue > overdue_s:
        return "down", (
            f"{w['overdue_ready']} due system job(s) unclaimed, worst {overdue}s "
            f"late (threshold {overdue_s}s)"
        )
    if age is not None and age > stale_s:
        return "down", (f"no system job has finished for {age}s (threshold {stale_s}s)")
    if age is not None:
        return "alive", f"last system job finished {age}s ago"
    # No success has EVER been recorded. Nothing is provably wrong and nothing
    # is provably right; a large age would be an invention and `alive` would be
    # the reassuring lie this whole instrument exists to stop.
    return "unknown", "no system job has ever finished, so there is no baseline"


def classify(
    status: int,
    body: str,
    *,
    threshold_s: int,
    worker_stale_s: int = DEFAULT_WORKER_STALE_S,
    worker_overdue_s: int = DEFAULT_WORKER_OVERDUE_S,
) -> Verdict:
    """One reading → one state. Pure; the whole reason this file is testable.

    **Strict about the payload, deliberately.** A missing or mistyped key is
    `UNREACHABLE`, never healthy. The lenient spelling — `body.get("stalled", 0)`
    — turns a broken instrument into a clean bill of health, which is the exact
    failure this classifier exists to prevent, one layer inward.
    """
    if status != 200:
        # A caller reports a transport failure as status 0 and puts the exception
        # in the body. Surface that rather than "HTTP 0", which says nothing
        # about whether it is DNS, TLS or a refused port.
        return Verdict(
            UNREACHABLE, body.strip()[:200] if status == 0 else f"HTTP {status}"
        )
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return Verdict(UNREACHABLE, "response was not JSON")
    if not isinstance(data, dict):
        return Verdict(UNREACHABLE, "response was not an object")

    for key in _COUNTS:
        if not _is_count(data.get(key)):
            return Verdict(UNREACHABLE, f"{key} missing or not an integer")
    # `.get` WITHOUT the sentinel, deliberately: on this axis a missing key is
    # treated as null and accepted, and tightening that here would change the
    # shipped cursor contract. The worker axis below is stricter — the endpoint
    # always emits all four of its keys — so the asymmetry is a decision rather
    # than drift.
    lag = data.get(_LAG)
    if not _is_count_or_null(lag):
        return Verdict(UNREACHABLE, "max_lag_seconds was neither null nor an integer")

    worker, worker_detail = _worker_state(
        data, stale_s=worker_stale_s, overdue_s=worker_overdue_s
    )
    if worker in ("invalid", "absent"):
        # The route always serves the worker block (a test holds it to these
        # fields), so a reading without one is a broken instrument too. Read as
        # anything milder, it would drop the worker axis without a word, and with
        # a destination active the cursor axis alone would answer HEALTHY.
        return Verdict(UNREACHABLE, worker_detail)
    if worker == "down":
        # FIRST, and above every cursor reading. A dead worker explains a
        # stalled cursor, and on an empty estate it is invisible to the cursor
        # axis entirely — which is the hole this state closes.
        return Verdict(WORKER_DOWN, worker_detail, data)

    active = data["accounts_active"]
    if active == 0:
        # A common case, not an edge: an estate with no active destinations.
        # `stalled: 0` here is true and meaningless.
        if worker == "unknown":
            return Verdict(
                WORKER_UNKNOWN,
                f"no active destinations exist, and {worker_detail}",
                data,
            )
        return Verdict(
            NO_SIGNAL,
            f"no active destinations exist, so nothing could be stalled "
            f"({worker_detail})",
            data,
        )
    if lag is not None and lag > threshold_s:
        return Verdict(
            STALLED,
            f"{data['stalled']} of {active} due cursors unadvanced, worst {lag}s "
            f"behind (threshold {threshold_s}s)",
            data,
        )
    return Verdict(HEALTHY, f"{active} active, worst lag {lag if lag else 0}s", data)


def commits(data: dict) -> tuple[str, str] | None:
    """The API's and the worker's short commits from one reading's payload, or
    None while either is unknown: an unreachable reading, or rows the worker
    wrote before it stamped its commit."""
    worker = data.get(_WORKER)
    pair = (
        data.get(_API_COMMIT),
        worker.get(_WORKER_COMMIT) if isinstance(worker, dict) else None,
    )
    if not all(isinstance(c, str) and _COMMIT.fullmatch(c) for c in pair):
        return None
    return pair[0][:7], pair[1][:7]
