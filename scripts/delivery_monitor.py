"""The check that alarms when the outbox's deliveries start failing (#1482).

`/health/delivery` counts, estate-wide, the outbox rows whose last failure fell
in the last hour, by class and the provider's code (migration 090). This polls
it and says so when the count is high, the way `posting_monitor.py` beside it
polls `/health/posting`. It runs outside the app for the reason that one gives:
an alert whose sending is done by the system it watches cannot fire when that
system is down.

## When it speaks

- **It raises at `--raise-at` (5) or more** failed or ambiguous rows in the
  hour. A 429 is a deferral, not a failure: the endpoint reports it as context
  and it never counts. Measured on production over the 30 days to 2026-09-30:
  21 failed rows, all in one burst on 09-12 (16 in one hour, 5 in another), and
  none in any other hour. At most 43 sends an hour, so an absolute count is
  steadier than a rate: a rate over three sends is noise.
- **It clears only at `--clear-at` (1) or fewer, on two consecutive polls.**
  Between the two it holds whatever it last said. The gap between the
  thresholds is the hysteresis; the second poll is the dwell. Together they keep
  a count that wobbles around one number from paging on every poll.
- **While failing, it repeats every six hours** (`REALERT_AFTER_S`, the
  siblings'), so a long outage does not look like a resolved one.
- **An unreachable endpoint speaks on the second consecutive poll**, as in both
  siblings: one failed request is a dropped packet.

It keeps what the human was TOLD (`announced`) apart from what the endpoint last
said, as `posting_monitor.py` does: a failed notify leaves `announced` alone, so
the next poll says it again.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

try:  # imported as a package (the tests, the route's wire-contract test)
    from scripts.posting_monitor import (
        EXIT_NOTIFY_FAILED,
        EXIT_QUIET,
        EXIT_SPOKE,
        REALERT_AFTER_S,
        Verdict,
        fetch,
        load_state,
        notify,
        save_state,
    )
except ImportError:  # run as a file from scripts/ by the systemd unit
    from posting_monitor import (  # type: ignore[no-redef]
        EXIT_NOTIFY_FAILED,
        EXIT_QUIET,
        EXIT_SPOKE,
        REALERT_AFTER_S,
        Verdict,
        fetch,
        load_state,
        notify,
        save_state,
    )

DEFAULT_RAISE_AT = 5
DEFAULT_CLEAR_AT = 1
#: Consecutive readings at or under `--clear-at` before a failure is called
#: over. One quiet poll inside a burst is not a recovery.
CLEAR_POLLS = 2

#: The three things a human can have been told.
DELIVERING = "delivering"
FAILING = "failing"
UNREACHABLE = "unreachable"

#: What one reading says on its own, before any history is applied.
ABOVE = "above"
BAND = "band"
BELOW = "below"


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _summary(by_class: dict) -> str:
    """`destination_gone ×5 (403 ×5), refused ×2 (400 ×2)`: the classes that
    count, largest first, then the context classes that do not."""
    alerting, context = [], []
    for name, entry in sorted(
        by_class.items(), key=lambda kv: -kv[1].get("alerting", 0)
    ):
        codes = ", ".join(
            f"{code} ×{n}"
            for code, n in sorted(entry.get("codes", {}).items(), key=lambda kv: -kv[1])
        )
        part = f"{name} ×{entry.get('alerting') or entry.get('rows', 0)}" + (
            f" ({codes})" if codes else ""
        )
        (alerting if entry.get("alerting") else context).append(part)
    out = ", ".join(alerting) or "none"
    return out + (f"; context: {', '.join(context)}" if context else "")


def classify(status: int, body: str, *, raise_at: int, clear_at: int) -> Verdict:
    """One reading → ABOVE, BAND, BELOW or UNREACHABLE. Pure."""
    if status != 200:
        return Verdict(UNREACHABLE, f"HTTP {status}" if status else body[:200])
    try:
        data = json.loads(body)
    except ValueError:
        return Verdict(UNREACHABLE, "the response is not JSON")
    if not isinstance(data, dict):
        return Verdict(UNREACHABLE, "the response is not an object")
    n, sent, window = (
        data.get(k) for k in ("failed_or_ambiguous", "sent_in_window", "window_seconds")
    )
    by_class = data.get("by_class")
    if not (
        _is_count(n)
        and _is_count(sent)
        and _is_count(window)
        and isinstance(by_class, dict)
    ):
        # A missing or mistyped key is a broken detector, never a quiet hour.
        return Verdict(UNREACHABLE, "the payload is missing or mistypes a key")
    hours = window / 3600
    span = f"the last {hours:g}h" if hours >= 1 else f"the last {window // 60} min"
    detail = f"{n} failed or ambiguous in {span} ({_summary(by_class)}); {sent} sent in the same window"
    reading = ABOVE if n >= raise_at else BELOW if n <= clear_at else BAND
    return Verdict(
        reading,
        detail,
        {"failed_or_ambiguous": n, "sent_in_window": sent, "by_class": by_class},
    )


def decide(
    verdict: Verdict, prior: dict, now: float
) -> tuple[dict, str | None, str | None]:
    """Reading + history → (state to persist, message or None, what the message
    announces). Pure: it never records that it spoke; only `announce` does, and
    only after the message was delivered."""
    announced = prior.get("announced")
    was = prior.get("effective", DELIVERING)
    run = (
        prior.get("consecutive", 0) + 1 if prior.get("reading") == verdict.state else 1
    )
    if verdict.state == ABOVE:
        now_is = FAILING
    elif verdict.state == BELOW:
        now_is = FAILING if was == FAILING and run < CLEAR_POLLS else DELIVERING
    else:  # BAND holds; UNREACHABLE knows nothing new about deliveries
        now_is = was
    state = {
        "reading": verdict.state,
        "effective": now_is,
        "detail": verdict.detail,
        "consecutive": run,
        "observed_at": now,
        "announced": announced,
        "spoke_at": prior.get("spoke_at", 0.0),
        "payload": verdict.payload,
    }

    def due(subject: str) -> bool:
        """Never told this, or told it long enough ago that silence would read
        as fixed."""
        return (
            announced != subject or now - prior.get("spoke_at", 0.0) >= REALERT_AFTER_S
        )

    if verdict.state == UNREACHABLE:
        if run < 2 or not due(UNREACHABLE):
            return state, None, None
        return (
            state,
            (
                f"FLEET ALERT: storydump delivery check UNREACHABLE ({verdict.detail}) "
                f"on {run} consecutive polls. The detector cannot look, which is not "
                f"the same as deliveries being fine."
            ),
            UNREACHABLE,
        )
    if now_is == FAILING:
        # A reading at or under `--clear-at` is a recovery waiting out its
        # dwell: it never pages "failing", even after an unreachable spell
        # left `announced` elsewhere.
        if verdict.state == BELOW or not due(FAILING):
            return state, None, None
        return (
            state,
            (
                f"FLEET ALERT: storydump OUTBOX DELIVERIES ARE FAILING — {verdict.detail}."
            ),
            FAILING,
        )
    if announced in (FAILING, UNREACHABLE):
        return (
            state,
            f"RECOVERED: storydump outbox deliveries — {verdict.detail}.",
            DELIVERING,
        )
    return state, None, None


def announce(state: dict, subject: str, now: float) -> dict:
    """Record that a human was actually told *subject*. Called ONLY after
    delivery succeeded."""
    state = dict(state)
    state["announced"] = subject
    state["spoke_at"] = now
    return state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True, help="the /health/delivery endpoint")
    ap.add_argument("--state-file", required=True)
    ap.add_argument(
        "--notify-command", help="executable given one argument: the message"
    )
    ap.add_argument(
        "--raise-at",
        type=int,
        default=DEFAULT_RAISE_AT,
        help="failed or ambiguous rows in the hour that start an alert",
    )
    ap.add_argument(
        "--clear-at",
        type=int,
        default=DEFAULT_CLEAR_AT,
        help="rows at or under which, on two polls in a row, it is over",
    )
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument(
        "--status",
        action="store_true",
        help="print the last recorded state and exit without polling",
    )
    args = ap.parse_args(argv)
    if args.clear_at >= args.raise_at:
        ap.error("--clear-at must be below --raise-at, or there is no hysteresis")

    if args.status:
        print(json.dumps(load_state(args.state_file), indent=2, sort_keys=True))
        return EXIT_QUIET

    prior = load_state(args.state_file)
    status, body = fetch(args.url, args.timeout)
    verdict = classify(status, body, raise_at=args.raise_at, clear_at=args.clear_at)
    now = time.time()
    state, message, subject = decide(verdict, prior, now)
    print(f"{state['effective']} ({verdict.state}): {verdict.detail}")

    if message is None:
        save_state(args.state_file, state)
        return EXIT_QUIET
    delivered = args.notify_command is None or notify(args.notify_command, message)
    if not delivered:
        state["notify_failed_at"] = time.time()
        save_state(args.state_file, state)
        print(f"NOTIFY FAILED, message not delivered: {message}", file=sys.stderr)
        return EXIT_NOTIFY_FAILED
    save_state(args.state_file, announce(state, subject, time.time()))
    print(message)
    return EXIT_SPOKE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
