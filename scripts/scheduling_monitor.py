"""The caller `/health/scheduling` never had (#1099).

## Why a detector needed a caller at all

`/health/scheduling` (#1090 F1) answers a question and raises nothing. That was
deliberate — an alert whose SENDING is performed by the system it monitors cannot
fire when that system is down — but it left the outage class exactly where it
started: two total scheduling outages ran 18 and 19 hours with no alert, and a
detector nobody polls produces no alert either. Production breaking with nobody
finding out is observationally identical either way.

This is the poller. It runs on the FLEET HOST, not in the app, so the alert path
shares nothing with the subject: different machine, process, network path, clock
and notification channel. It mints no job, writes nothing, and reaches none of
`fn_clock_tick`, the outbox, `channel_bindings`, the worker process, or the app's
own notification routing — which has no writer, so an alert delivered there would
vanish silently.

## TWO AXES, and why the second one may read the jobs table (#1120)

`scheduling_health` deliberately watched CURSORS rather than job rows, on the
rule that *a monitor whose INPUT is produced by the system it monitors cannot
detect that system stopping*. The worker axis added in #1120 reads job rows, so
that rule has to be answered rather than stepped around.

It is answered by the DIRECTION of the signal. The rejected design was minting a
system job to perform the check — that shares fate with the worker, and dies
silently exactly when it is needed. This instead reads, from the API process,
what the worker has ALREADY finished, and alarms on the ABSENCE of it. A dead
worker cannot suppress the signal by dying; dying is what produces it. The
original objection to a heartbeat also does not carry: a heartbeat says "the
process is up", which #1026's second outage satisfied while doing no work, and
these two signals are outcome-shaped — a finished job and an unclaimed due job.

The two axes have opposite blind spots, which is the reason for keeping both.
The cursor axis is blind whenever no destination is active — the standing state
of production for a week, during which `no-signal` was the answer whether the
worker was healthy or dead. The worker axis is blind before the first system job
is ever minted, and says so as `WORKER_UNKNOWN` rather than guessing.

## THE PRIMARY CASE IS THAT THERE IS NOTHING TO SEE

Measured on production the night this was written: `ig_accounts` has **no rows at
all**, `workspaces` 0, `media_sources` 0, and the legacy tables are absent
entirely. The tier is live — `jobs` carried 27 rows and the clock was minting —
but it has **zero destinations**.

`scheduling_lag` reads only `ig_accounts WHERE state = 'active'`. With no rows,
`stalled` is 0 and `max_lag_seconds` is null **structurally**: not because
scheduling is healthy, but because there is no population that could be stalled.
A poller that reported "nothing is stalled" from that would be a green light
wired to nothing — and it would have stayed green through the whole 19-hour
outage had the estate been empty then.

So this module's load-bearing distinction is not the threshold. It is:

- **nothing is late** — genuinely healthy
- **nothing EXISTS to be late** — `NO_SIGNAL`, and it says so

Those have opposite remedies and must never render the same. It is the same rule
the fleet compositor states for read doors: *a reader that cannot reach its source
must not return what a reader that found nothing returns.* `NO_SIGNAL` never pages
— an empty estate is expected right now, not a fault — but it is never counted as
fine either.

## The asymmetry in confirmation is a property of the signal

`STALLED` fires on the **first** observation; `UNREACHABLE` on the **second**.
That is not two tunings of one knob. A lag past the threshold already *contains*
its own duration — ten minutes of non-advance is what the number means — so the
observation is the sustain and confirming it would only add delay to something
already proven. An unreachable reading contains no duration at all: one failed
request is indistinguishable from a dropped packet.

## Bound, stated because it will not be obvious later

**This cannot be validated against real traffic until there is a first user.**
Nobody has run the target tier end to end; sign-in needs a real Google account.
So every path here is exercised against captured payloads and the live endpoint's
`NO_SIGNAL` answer, and the `STALLED` path in particular has never seen a real
stalled cursor. That is a bound on this work, not a defect in it — but a later
reader must not mistake "tested" for "seen in production".
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

#: 40x the clock cadence (`work_loop.py` `clock_interval_seconds = 15.0`), and
#: 1/114th of the shorter of the two outages this exists for. The margin is the
#: point: the gap between a healthy 15s lag and a 19-hour outage is four orders
#: of magnitude, so a threshold near the noise floor buys nothing and costs false
#: alarms.
DEFAULT_STALL_THRESHOLD_S = 600

#: While a fault persists, repeat rather than going quiet — a long outage must not
#: look like a resolved one.
REALERT_AFTER_S = 6 * 3600

#: `NO_SIGNAL` is expected today, so it repeats rarely. Not never: "we have no
#: monitoring coverage" must not fade from memory just because it was said once.
RENOTICE_NO_SIGNAL_AFTER_S = 7 * 24 * 3600

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
#: no proof of life — a fresh database, or an app older than this poller that
#: does not serve the axis at all. **Deliberately not `NO_SIGNAL`**: that one now
#: carries a positive fact (the worker is provably alive, only the cursor is
#: blind), and collapsing the two would hide the strictly worse state inside the
#: better-understood one.
WORKER_UNKNOWN = "worker-unknown"

#: No system job finished within this long => the worker is not working. The
#: recurring cadence is 6h, so this is two beats plus slack; a deployment that
#: changes the cadence must move it.
DEFAULT_WORKER_STALE_S = 13 * 3600

#: A due system job nobody claimed for this long. Far tighter than the staleness
#: floor because it needs no cadence to elapse — the job is already late.
DEFAULT_WORKER_OVERDUE_S = 900

#: Nothing to say. Distinct from the alerting codes so a supervisor can route on
#: them without parsing text.
EXIT_QUIET, EXIT_SPOKE, EXIT_NOTIFY_FAILED = 0, 10, 11


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
    `invalid`, never healthy. An ABSENT block is a different fact — an app older
    than this poller — and is not an instrument fault, so it is reported as its
    own value and the caller decides what it means alongside the other axis.
    """
    if "worker" not in data:
        return "absent", "the endpoint does not serve a worker block"
    w = data.get("worker")
    if not isinstance(w, dict):
        return "invalid", "worker was not an object"
    for key in ("succeeded_ever", "overdue_ready"):
        if not _is_count(w.get(key)):
            return "invalid", f"worker.{key} missing or not an integer"
    for key in ("last_success_age_seconds", "max_overdue_seconds"):
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
    failure this poller exists to prevent, one layer inward.
    """
    if status != 200:
        # `fetch` reports a transport failure as status 0 and puts the exception
        # in the body. Surface that rather than "HTTP 0", which tells a human
        # woken at 3am nothing about whether it is DNS, TLS or a refused port.
        return Verdict(
            UNREACHABLE, body.strip()[:200] if status == 0 else f"HTTP {status}"
        )
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return Verdict(UNREACHABLE, "response was not JSON")
    if not isinstance(data, dict):
        return Verdict(UNREACHABLE, "response was not an object")

    for key in ("stalled", "accounts_active"):
        if not _is_count(data.get(key)):
            return Verdict(UNREACHABLE, f"{key} missing or not an integer")
    # `.get` WITHOUT the sentinel, deliberately: on this axis a missing key has
    # always been treated as null and accepted, and tightening that here would
    # be a behaviour change to the shipped cursor contract riding in on an
    # unrelated PR. The worker axis below is stricter because it is new — the
    # endpoint always emits all four of its keys — so the asymmetry is a
    # decision rather than drift.
    lag = data.get("max_lag_seconds")
    if not _is_count_or_null(lag):
        return Verdict(UNREACHABLE, "max_lag_seconds was neither null nor an integer")

    worker, worker_detail = _worker_state(
        data, stale_s=worker_stale_s, overdue_s=worker_overdue_s
    )
    if worker == "invalid":
        return Verdict(UNREACHABLE, worker_detail)
    if worker == "down":
        # FIRST, and above every cursor reading. A dead worker explains a
        # stalled cursor, and on an empty estate it is invisible to the cursor
        # axis entirely — which is the hole this state closes.
        return Verdict(WORKER_DOWN, worker_detail, data)

    active = data["accounts_active"]
    if active == 0:
        # THE PRIMARY CASE. Not an edge, not a footnote — the live state of
        # production. `stalled: 0` here is true and meaningless.
        if worker in ("absent", "unknown"):
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


def announce(state: dict, verdict: Verdict, now: float) -> dict:
    """Record that a human was actually told. Called ONLY after delivery succeeds.

    `announced` is deliberately separate from `state`: the first is what a human
    knows, the second is what the endpoint last said. Collapsing them is a live
    bug rather than a tidiness question — with one field, a failed notify still
    advances the history, so the next poll sees "same as last time" and stays
    quiet about a message nobody received. Found by this module's own test, twice:
    once on the `NO_SIGNAL` notice and once on `RECOVERED`.
    """
    state = dict(state)
    state["announced"] = verdict.state
    state["spoke_at"] = now
    return state


def decide(verdict: Verdict, prior: dict, now: float) -> tuple[dict, str | None]:
    """Verdict + history → (state to persist, message to send or None).

    Pure, so every transition is a unit test rather than a stakeout. It does NOT
    record that it spoke — only `announce` does, and only the caller knows whether
    delivery worked.

    `UNREACHABLE` needs two consecutive readings before it speaks; everything else
    acts on the first. Recovery and acquisition both speak, because "the alerts
    stopped" must never be ambiguous between *fixed* and *monitor died*, and
    because the day this monitor first gains sight is worth saying out loud.
    """
    announced = prior.get("announced")
    # A run of the SAME state the human already knows about; anything else means
    # they have not been told, whatever the endpoint has been saying.
    spoke_at = prior.get("spoke_at", 0.0) if announced == verdict.state else 0.0
    run = prior.get("consecutive", 0) + 1 if prior.get("state") == verdict.state else 1
    state = {
        "state": verdict.state,
        "detail": verdict.detail,
        "consecutive": run,
        "observed_at": now,
        "announced": announced,
        "spoke_at": prior.get("spoke_at", 0.0),
        "payload": verdict.payload,
    }

    def repeat_due(interval: float) -> bool:
        """Never told, or told long enough ago that silence would read as fixed."""
        return not spoke_at or now - spoke_at >= interval

    if verdict.state == UNREACHABLE:
        # One failed request is a dropped packet. Two is a fact.
        if run < 2 or not repeat_due(REALERT_AFTER_S):
            return state, None
        return state, (
            f"FLEET ALERT: storydump scheduling check UNREACHABLE "
            f"({verdict.detail}) on {run} consecutive polls. The detector cannot "
            f"look, which is not the same as scheduling being fine."
        )

    if verdict.state == STALLED:
        if not repeat_due(REALERT_AFTER_S):
            return state, None
        return state, (
            f"FLEET ALERT: storydump SCHEDULING IS STALLED — {verdict.detail}. "
            f"The clock is not advancing due cursors. Two prior outages in this "
            f"class ran 18 and 19 hours undetected."
        )

    if verdict.state == WORKER_DOWN:
        # Pages on the FIRST reading, unlike UNREACHABLE. That one waits because
        # a single failed request is a dropped packet; here the endpoint
        # answered and what it said is that work has stopped. There is no
        # dropped-packet reading of that, and waiting a cadence to be sure only
        # makes the outage longer.
        if not repeat_due(REALERT_AFTER_S):
            return state, None
        return state, (
            f"FLEET ALERT: storydump WORKER IS NOT ADVANCING — {verdict.detail}. "
            f"System jobs run on a cadence regardless of how many tenants exist, "
            f"so this is an outage whether or not any workspace is set up. The "
            f"last outage in this class ran 18 hours undetected."
        )

    if verdict.state == WORKER_UNKNOWN:
        # NOT an alert. Nothing is known to be wrong — but nothing is known to
        # be right either, and that must be said out loud rather than left to
        # look like quiet. Same cadence as NO_SIGNAL, for the same reason: "we
        # have no coverage" must not fade from memory just because it was said.
        if not repeat_due(RENOTICE_NO_SIGNAL_AFTER_S):
            return state, None
        return state, (
            f"storydump monitoring: NO COVERAGE — {verdict.detail}. Neither axis "
            f"can answer: there are no destinations to stall, and the worker has "
            f"left no proof of life. This is not an alert and not an all-clear — "
            f"an outage right now would be invisible to this check."
        )

    if verdict.state == NO_SIGNAL:
        if not repeat_due(RENOTICE_NO_SIGNAL_AFTER_S):
            return state, None
        return state, (
            f"storydump scheduling: NO SIGNAL — {verdict.detail}. This is not an "
            f"alert and not an all-clear: the check is answering correctly and "
            f"has nothing to watch, so it CANNOT detect an outage right now. "
            f"Expected until the first destination is connected."
        )

    # HEALTHY. Silence is right, except on the two transitions that would
    # otherwise be invisible — and those are keyed on what the human was last
    # TOLD, not on what the endpoint last said.
    if announced in (STALLED, UNREACHABLE, WORKER_DOWN):
        return state, (
            f"RECOVERED: storydump scheduling is advancing again — {verdict.detail}."
        )
    if announced in (NO_SIGNAL, WORKER_UNKNOWN):
        return state, (
            f"storydump scheduling: SIGNAL ACQUIRED — {verdict.detail}. The check "
            f"can now detect a stall; until now it had nothing to watch."
        )
    return state, None


def fetch(url: str, timeout_s: float) -> tuple[int, str]:
    """The endpoint, or a status that says why not. Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - any failure to reach it is one state
        return 0, f"{type(exc).__name__}: {exc}"


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A first run and a corrupt file both mean "no history", which only ever
        # makes this speak sooner. Failing toward saying something is the right
        # direction for a monitor.
        return {}


def save_state(path: str, state: dict) -> None:
    """Atomic, so a kill mid-write cannot leave a file that reads as a fresh run
    forever — which would turn every poll into a first observation and re-alert
    on every tick."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".scheduling-monitor.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def notify(command: str, message: str) -> bool:
    """Hand the message to whatever this deployment uses to reach a human.

    The command is CONFIG, not code: the fleet path lives in the systemd unit, so
    this repository holds no fleet-specific path and the coupling that F1 was
    locked to prevent — script drifting from endpoint — stays inside one repo and
    one test suite.
    """
    try:
        done = subprocess.run(  # noqa: S603
            [command, message], capture_output=True, text=True, timeout=60
        )
        return done.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True, help="the /health/scheduling endpoint")
    ap.add_argument("--state-file", required=True)
    ap.add_argument(
        "--notify-command", help="executable given one argument: the message"
    )
    ap.add_argument("--stall-threshold", type=int, default=DEFAULT_STALL_THRESHOLD_S)
    ap.add_argument(
        "--worker-stale-threshold",
        type=int,
        default=DEFAULT_WORKER_STALE_S,
        help="seconds since the last finished system job before the worker is "
        "called down; must exceed the recurring cadence",
    )
    ap.add_argument(
        "--worker-overdue-threshold",
        type=int,
        default=DEFAULT_WORKER_OVERDUE_S,
        help="seconds a due system job may sit unclaimed before the worker is "
        "called down",
    )
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument(
        "--status",
        action="store_true",
        help="print the last recorded state and exit without polling",
    )
    args = ap.parse_args(argv)

    if args.status:
        print(json.dumps(load_state(args.state_file), indent=2, sort_keys=True))
        return EXIT_QUIET

    status, body = fetch(args.url, args.timeout)
    verdict = classify(
        status,
        body,
        threshold_s=args.stall_threshold,
        worker_stale_s=args.worker_stale_threshold,
        worker_overdue_s=args.worker_overdue_threshold,
    )
    prior = load_state(args.state_file)
    state, message = decide(verdict, prior, time.time())

    # Always visible, even when silent — `NO_SIGNAL` must never be something a
    # reader has to go looking for.
    print(f"{verdict.state}: {verdict.detail}")

    if message is None:
        save_state(args.state_file, state)
        return EXIT_QUIET

    delivered = args.notify_command is None or notify(args.notify_command, message)
    if not delivered:
        # A monitor cannot page about its own paging failure. What it CAN do is
        # leave `announced` where it was, so the next poll re-derives the same
        # message and tries again, and exit nonzero so the supervisor logs a
        # failed unit.
        state["notify_failed_at"] = time.time()
        save_state(args.state_file, state)
        print(f"NOTIFY FAILED, message not delivered: {message}", file=sys.stderr)
        return EXIT_NOTIFY_FAILED

    save_state(args.state_file, announce(state, verdict, time.time()))
    print(message)
    return EXIT_SPOKE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
