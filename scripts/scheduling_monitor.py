"""Polls `/health/scheduling` and tells a human when scheduling stops.

`/health/scheduling` answers a question and raises nothing, deliberately: an
alert whose SENDING is performed by the system it monitors cannot fire when that
system is down. A detector nobody polls produces no alert either, so this is the
poller. It runs on a separate host, not in the app, so the alert path shares
nothing with the subject: different machine, process, network path, clock and
notification channel. It mints no job, writes nothing to the app, and reaches
none of the clock, the outbox, `channel_bindings` or the worker process.

What one reading means is `storydump_cli.health_classify.scheduling`, the same
classifier `storydump health` uses: the cursor axis (due cursors the clock has
not advanced) and the worker axis (system jobs the worker has not finished). This
file adds only what a poller needs on top of it: the request, the history that
decides when to speak, the state file, and the command that reaches a human.

It runs on the system Python from a checkout, with nothing installed: it imports
only the standard library and that classifier, which does the same. The
operating guide is `documentation/operations/scheduling-monitor.md`.
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
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storydump_cli.health_classify.scheduling import (
    DEFAULT_STALL_THRESHOLD_S,
    DEFAULT_WORKER_OVERDUE_S,
    DEFAULT_WORKER_STALE_S,
    NO_SIGNAL,
    STALLED,
    UNREACHABLE,
    WORKER_DOWN,
    WORKER_UNKNOWN,
    Verdict,
    classify,
)

#: While a fault persists, repeat rather than going quiet — a long outage must not
#: look like a resolved one.
REALERT_AFTER_S = 6 * 3600

#: `NO_SIGNAL` and `WORKER_UNKNOWN` repeat rarely. Not never: "we have no
#: monitoring coverage" must not fade from memory just because it was said once.
RENOTICE_NO_SIGNAL_AFTER_S = 7 * 24 * 3600

#: Nothing to say, said it, and had something to say but could not deliver it.
#: Distinct codes so a supervisor can route on them without parsing text; the
#: unit counts 0 and 10 as success, so 11 is a failed unit.
EXIT_QUIET, EXIT_SPOKE, EXIT_NOTIFY_FAILED = 0, 10, 11


def announce(state: dict, verdict: Verdict, now: float) -> dict:
    """Record that a human was actually told. Called ONLY after delivery succeeds.

    `announced` is deliberately separate from `state`: the first is what a human
    knows, the second is what the endpoint last said. With one field, a failed
    notify would still advance the history, so the next poll would see "same as
    last time" and stay quiet about a message nobody received.
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
            f"The clock is not advancing due cursors."
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
            f"so this is an outage whether or not any workspace is set up."
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

    The command is CONFIG, not code: the path that reaches people lives in the
    deployment, so this repository holds no host-specific path, and the script
    and the endpoint it reads stay in one repository and one test suite.
    """
    try:
        done = subprocess.run(  # noqa: S603
            [command, message], capture_output=True, text=True, timeout=60
        )
        return done.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def build_parser() -> argparse.ArgumentParser:
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
        "called down; must span three beats (two plus one of slack) of the fastest "
        "recurring system kind",
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
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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
