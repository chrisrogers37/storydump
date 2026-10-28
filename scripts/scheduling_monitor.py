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
not advanced), the worker axis (system jobs the worker has not finished), and
the commits the API and the worker run. `/health/delivery` is read beside it as
an axis of its own, judged by `storydump_cli.health_classify.delivery`. This
file adds what a poller needs on top: the requests, the history that decides
when to speak, the state file, and the command that reaches a human.
`storydump health` judges delivery with the same classifier; a commit split is
this poller's alone.

It runs on the system Python from a checkout, with nothing installed: it imports
only the standard library and those classifiers, which do the same. The
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

from storydump_cli.health_classify import delivery
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
    commits,
)

#: While a fault persists, repeat rather than going quiet — a long outage must not
#: look like a resolved one.
REALERT_AFTER_S = 6 * 3600

#: `NO_SIGNAL` and `WORKER_UNKNOWN` repeat rarely. Not never: "we have no
#: monitoring coverage" must not fade from memory just because it was said once.
RENOTICE_NO_SIGNAL_AFTER_S = 7 * 24 * 3600

#: An estate that HAD destinations and now has none repeats daily: nothing is
#: scheduled or posted until one comes back, which is not the quiet a new,
#: empty estate's notice describes.
RENOTICE_LOST_DESTINATIONS_AFTER_S = 24 * 3600

#: Nothing to say, said it, and had something to say but could not deliver it.
#: Distinct codes so a supervisor can route on them without parsing text; the
#: unit counts 0 and 10 as success, so 11 is a failed unit.
EXIT_QUIET, EXIT_SPOKE, EXIT_NOTIFY_FAILED = 0, 10, 11


def _count(value) -> int:
    """A destination count as the state file or a payload holds it: a positive
    whole number, else 0. A corrupt value means no history, which only ever
    makes this speak sooner."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def announce(state: dict, now: float) -> dict:
    """Record that a human was actually told. Called ONLY after delivery succeeds.

    `announced` is deliberately separate from `state`: the first is what a human
    knows, the second is what the endpoint last said. With one field, a failed
    notify would still advance the history, so the next poll would see "same as
    last time" and stay quiet about a message nobody received. Each axis keeps
    its own pair, and this is the one writer for all of them.
    """
    return {**state, "announced": state["state"], "spoke_at": now}


#: `UNREACHABLE` speaks on this many readings in a row: one failed request is a
#: dropped packet, two is a fact.
UNREACHABLE_AFTER_READINGS = 2


def _run(prior: dict, state: str) -> int:
    """Readings in a row of `state`, this one included."""
    return prior.get("consecutive", 0) + 1 if prior.get("state") == state else 1


def _carried(prior: dict, now: float) -> dict:
    """What every axis keeps from poll to poll: when it last read, and what a
    human was last told, and when."""
    return {
        "observed_at": now,
        "announced": prior.get("announced"),
        "spoke_at": prior.get("spoke_at", 0.0),
    }


def _repeat_due(prior: dict, told: str, now: float, interval: float) -> bool:
    """Whether `told` is due to be said: never told it, or told it long enough
    ago that silence would read as fixed."""
    spoke_at = prior.get("spoke_at", 0.0) if prior.get("announced") == told else 0.0
    return not spoke_at or now - spoke_at >= interval


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
    run = _run(prior, verdict.state)
    # The most destinations any reading has shown. A reading with no counts (an
    # unreachable one) never lowers it, or a network blip would turn a lost
    # destination back into the notice for an estate that never had one.
    seen = max(
        _count(prior.get("max_active_seen")),
        _count(verdict.payload.get("accounts_active")),
    )
    state = {
        "state": verdict.state,
        "detail": verdict.detail,
        "consecutive": run,
        **_carried(prior, now),
        "payload": verdict.payload,
        "max_active_seen": seen,
    }

    def closed(text: str) -> str:
        """A fault the human was told about, closed in words by a reading that
        can see nothing. A stall's evidence going away is not the stall ending:
        pausing the only workspace does exactly that."""
        if announced == STALLED:
            return f"the stall is no longer observable; {text}"
        if announced in (UNREACHABLE, WORKER_DOWN):
            return f"RECOVERED from {announced}; {text}"
        return text

    def repeat_due(interval: float) -> bool:
        # A run of the SAME state the human already knows about; anything else
        # means they have not been told, whatever the endpoint has been saying.
        return _repeat_due(prior, verdict.state, now, interval)

    if verdict.state == UNREACHABLE:
        if run < UNREACHABLE_AFTER_READINGS or not repeat_due(REALERT_AFTER_S):
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

    if verdict.state in (WORKER_UNKNOWN, NO_SIGNAL) and seen:
        # The estate had destinations and has none now: nothing will be
        # scheduled or posted, which is worth saying daily, not weekly.
        if not repeat_due(RENOTICE_LOST_DESTINATIONS_AFTER_S):
            return state, None
        return state, closed(
            f"storydump scheduling: NO ACTIVE DESTINATIONS — the estate had {seen}; "
            f"nothing will be scheduled or posted until one is re-authorized or "
            f"resumed ({verdict.detail})."
        )

    if verdict.state == WORKER_UNKNOWN:
        # NOT an alert. Nothing is known to be wrong — but nothing is known to
        # be right either, and that must be said out loud rather than left to
        # look like quiet. Same cadence as NO_SIGNAL, for the same reason: "we
        # have no coverage" must not fade from memory just because it was said.
        if not repeat_due(RENOTICE_NO_SIGNAL_AFTER_S):
            return state, None
        return state, closed(
            f"storydump monitoring: NO COVERAGE — {verdict.detail}. Neither axis "
            f"can answer: there are no destinations to stall, and the worker has "
            f"left no proof of life. This is not an alert and not an all-clear — "
            f"an outage right now would be invisible to this check."
        )

    if verdict.state == NO_SIGNAL:
        if not repeat_due(RENOTICE_NO_SIGNAL_AFTER_S):
            return state, None
        return state, closed(
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


#: Delivery's own surface, beside the scheduling one. It is read and judged as
#: its own axis: cards can fail while posts land, so neither may mask the other.
SCHEDULING_PATH, DELIVERY_PATH = "/health/scheduling", "/health/delivery"


def delivery_url(url: str) -> str | None:
    """The delivery surface beside a scheduling URL, or None when the URL does
    not end in `/health/scheduling`."""
    if not url.endswith(SCHEDULING_PATH):
        return None
    return url[: -len(SCHEDULING_PATH)] + DELIVERY_PATH


def api_unreachable(status: int) -> bool:
    """A `/health/scheduling` fetch that says the API is down, not that one
    reading was malformed: no answer at all, or a server error."""
    return status == 0 or status >= 500


def decide_delivery(
    verdict: delivery.Verdict, prior: dict, now: float, *, held: bool = False
) -> tuple[dict, str | None]:
    """A delivery verdict + the axis's own history → (its state, a message or
    None). Pure, like `decide`; only `announce` records that a human was told.

    Failing pages at once and repeats while it lasts. A surface that cannot be
    read pages on the second reading, as an unreachable endpoint does, and
    says RECOVERED when it reads again. `held` is a run in which
    `/health/scheduling` could not be fetched either: the API is down, the
    scheduling axis pages that, and an unreadable delivery reading in such a
    run counts toward nothing.
    """
    announced = prior.get("announced")
    held = held and verdict.state == delivery.UNREACHABLE
    run = 0 if held else _run(prior, verdict.state)
    state = {
        "state": verdict.state,
        "detail": verdict.detail,
        "consecutive": run,
        **_carried(prior, now),
    }
    if verdict.state == delivery.UNREACHABLE:
        if run < UNREACHABLE_AFTER_READINGS or not _repeat_due(
            prior, verdict.state, now, REALERT_AFTER_S
        ):
            return state, None
        return state, (
            f"FLEET ALERT: storydump delivery check UNREACHABLE ({verdict.detail}) "
            f"on {run} consecutive polls. Failed sends cannot be seen, which is "
            f"not the same as delivery being fine."
        )
    if verdict.state == delivery.FAILING:
        if not _repeat_due(prior, verdict.state, now, REALERT_AFTER_S):
            return state, None
        return state, f"FLEET ALERT: storydump DELIVERY IS FAILING — {verdict.detail}."
    if announced == delivery.FAILING:
        return state, f"RECOVERED: storydump delivery — {verdict.detail}."
    if announced == delivery.UNREACHABLE:
        return state, (
            f"RECOVERED: the storydump delivery check reads again — {verdict.detail}."
        )
    return state, None


#: How long the API and the worker may run different commits before it pages:
#: longer than a deploy, which moves the two services one after the other.
SPLIT_AFTER_S = 30 * 60
#: A split that lasts repeats daily.
RENOTICE_SPLIT_AFTER_S = 24 * 3600
SPLIT, SAME = "split", "same"


def decide_split(payload: dict, prior: dict, now: float) -> tuple[dict, str | None]:
    """A scheduling reading's two commits + the split's own history → (its
    state, a message or None). Pure, like `decide`; only `announce` records
    that a human was told. A split pages once it has lasted `SPLIT_AFTER_S`,
    repeats daily, and says RESOLVED when the two match again; one that ends
    sooner is never said. A reading without both commits changes nothing."""
    pair = commits(payload)
    if pair is None:
        return dict(prior), None
    api, worker = pair
    announced = prior.get("announced")
    state = _carried(prior, now)
    if api == worker:
        state["state"] = SAME
        if announced == SPLIT:
            return state, (
                f"VERSION SPLIT RESOLVED: the API and the worker both run {api}."
            )
        return state, None
    since = prior.get("since", now) if prior.get("state") == SPLIT else now
    state.update(state=SPLIT, since=since, api=api, worker=worker)
    if now - since < SPLIT_AFTER_S:
        return state, None
    if not _repeat_due(prior, SPLIT, now, RENOTICE_SPLIT_AFTER_S):
        return state, None
    return state, (
        f"FLEET ALERT: storydump VERSION SPLIT — the API runs {api}, the worker "
        f"{worker}, for {int((now - since) // 60)} min. A deploy reached one "
        f"service and not the other."
    )


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
    ap.add_argument(
        "--delivery-url",
        help="the /health/delivery endpoint; by default the one beside --url",
    )
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
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(load_state(args.state_file), indent=2, sort_keys=True))
        return EXIT_QUIET

    # A delivery surface nothing watches must not be a quiet one: refused, so
    # the supervisor logs a failed unit.
    url = args.delivery_url or delivery_url(args.url)
    if url is None:
        parser.error(
            f"--url does not end in {SCHEDULING_PATH}, so the delivery surface "
            f"beside it is unknown: pass --delivery-url"
        )

    status, body = fetch(args.url, args.timeout)
    verdict = classify(
        status,
        body,
        threshold_s=args.stall_threshold,
        worker_stale_s=args.worker_stale_threshold,
        worker_overdue_s=args.worker_overdue_threshold,
    )
    prior = load_state(args.state_file)
    now = time.time()
    state, message = decide(verdict, prior, now)

    # Delivery: its own surface, judged with its own state under "delivery".
    prior_delivery = prior.get("delivery")
    delivery_verdict = delivery.classify(*fetch(url, args.timeout))
    state["delivery"], delivery_message = decide_delivery(
        delivery_verdict,
        prior_delivery if isinstance(prior_delivery, dict) else {},
        now,
        held=api_unreachable(status),
    )

    # The API's commit beside the worker's, from the same reading, under "split".
    prior_split = prior.get("split")
    state["split"], split_message = decide_split(
        verdict.payload, prior_split if isinstance(prior_split, dict) else {}, now
    )

    # Always visible, even when silent — `NO_SIGNAL` must never be something a
    # reader has to go looking for. One line per poll, both axes on it.
    print(
        f"{verdict.state}: {verdict.detail} · "
        f"delivery {delivery_verdict.state}: {delivery_verdict.detail}"
    )

    spoke = failed = False
    # Scheduling's history is the top level of the state; each other axis
    # keeps its own under its key.
    for axis, said in (
        (None, message),
        ("delivery", delivery_message),
        ("split", split_message),
    ):
        if said is None:
            continue
        if args.notify_command is None or notify(args.notify_command, said):
            if axis is None:
                state = announce(state, time.time())
            else:
                state[axis] = announce(state[axis], time.time())
            print(said)
            spoke = True
        else:
            # A monitor cannot page about its own paging failure. What it CAN do
            # is leave that axis's `announced` where it was, so the next poll
            # re-derives the same message and tries again, and exit nonzero so
            # the supervisor logs a failed unit.
            failed = True
            print(f"NOTIFY FAILED, message not delivered: {said}", file=sys.stderr)
    if failed:
        state["notify_failed_at"] = time.time()
    save_state(args.state_file, state)
    if failed:
        return EXIT_NOTIFY_FAILED
    return EXIT_SPOKE if spoke else EXIT_QUIET


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
