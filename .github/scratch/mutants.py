"""The scheduling monitor's mutants, run in CI only. Scratch: never merged.

Each mutant is one edit to the source and the tests that should catch it. For
each, this applies it, runs those tests, and restores the source. Two sets:
the 11 the post-merge review of #42 listed, and 19 rebuilt from the
descriptions of the survivors of a review's 60 mutants of #69's added lines.
It fails unless every baseline passes, every mutant compiles and runs, and
every mutant of the first set is killed. The second set reports.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

MONITOR = Path("scripts/scheduling_monitor.py")
CLASSIFY = Path("storydump_cli/health_classify/scheduling.py")
ROUTE = Path("src/api/routes/health.py")
POLLER = (
    "tests/scripts/test_scheduling_monitor.py",
    "tests/storydump_cli/test_health_classify_scheduling.py",
)
SURFACE = ("tests/src/api/test_app_factory.py::TestSchedulingHealthIsASecondSurface",)

_UNKNOWN = (
    '        # have no coverage" must not fade from memory just because it was said.\n'
)
_DOWN = "        # makes the outage longer.\n"
_COUNT = "    if isinstance(value, bool) or not isinstance(value, int):\n"
_READ = "        if isinstance(value, bool) or not isinstance(value, int) or value < 0:\n"
_SPLIT_PAGE = (
    '        f"FLEET ALERT: storydump VERSION SPLIT — the API runs {api}, the worker "\n'
    '        f"{worker}, for'
)

# (id, what it breaks, file, exact text (must occur once), replacement, tests)
LISTED = [
    ("1", "EXIT_SPOKE = 12", MONITOR,
     "EXIT_QUIET, EXIT_SPOKE, EXIT_NOTIFY_FAILED = 0, 10, 11\n",
     "EXIT_QUIET, EXIT_SPOKE, EXIT_NOTIFY_FAILED = 0, 12, 11\n", POLLER),
    ("2", "--stall-threshold ignored", MONITOR,
     "        threshold_s=args.stall_threshold,\n",
     "        threshold_s=DEFAULT_STALL_THRESHOLD_S,\n", POLLER),
    ("3", "--worker-overdue-threshold ignored", MONITOR,
     "        worker_overdue_s=args.worker_overdue_threshold,\n",
     "        worker_overdue_s=DEFAULT_WORKER_OVERDUE_S,\n", POLLER),
    ("4", "an absent worker block with a stall kept as STALLED", CLASSIFY,
     '    if worker in ("invalid", "absent"):\n',
     '    if worker == "invalid" or (worker == "absent" and not '
     "(lag is not None and lag > threshold_s)):\n", POLLER),
    ("5", "no RECOVERED after WORKER_DOWN", MONITOR,
     "    if announced in (STALLED, UNREACHABLE, WORKER_DOWN):\n",
     "    if announced in (STALLED, UNREACHABLE):\n", POLLER),
    ("6", "no RECOVERED after UNREACHABLE", MONITOR,
     "    if announced in (STALLED, UNREACHABLE, WORKER_DOWN):\n",
     "    if announced in (STALLED, WORKER_DOWN):\n", POLLER),
    ("7", "no SIGNAL ACQUIRED after WORKER_UNKNOWN", MONITOR,
     "    if announced in (NO_SIGNAL, WORKER_UNKNOWN):\n",
     "    if announced in (NO_SIGNAL,):\n", POLLER),
    ("8", "WORKER_DOWN never repeats", MONITOR,
     _DOWN + "        if not repeat_due(REALERT_AFTER_S):\n",
     _DOWN + '        if not repeat_due(float("inf")):\n', POLLER),
    ("9", "UNREACHABLE never repeats", MONITOR,
     "        if run < 2 or not repeat_due(REALERT_AFTER_S):\n",
     '        if run < 2 or not repeat_due(float("inf")):\n', POLLER),
    ("10", "WORKER_UNKNOWN never re-noticed", MONITOR,
     _UNKNOWN + "        if not repeat_due(RENOTICE_NO_SIGNAL_AFTER_S):\n",
     _UNKNOWN + '        if not repeat_due(float("inf")):\n', POLLER),
    ("11", "WORKER_UNKNOWN on the 6-h cadence", MONITOR,
     _UNKNOWN + "        if not repeat_due(RENOTICE_NO_SIGNAL_AFTER_S):\n",
     _UNKNOWN + "        if not repeat_due(REALERT_AFTER_S):\n", POLLER),
]

REVIEWED = [
    ("R1", "api_commit published as None", ROUTE,
     '            return {**lag, "worker": worker, "api_commit": api_commit}\n',
     '            return {**lag, "worker": worker, "api_commit": None}\n', SURFACE),
    ("R2", "api_commit read from nowhere", ROUTE,
     "    api_commit = request.app.state.commit\n",
     '    api_commit = getattr(request.app.state, "api_commit", None)\n', SURFACE),
    ("D1", "delivery pages from two failures, not one", MONITOR,
     '    failing = reading["failed_or_ambiguous"] >= 1\n',
     '    failing = reading["failed_or_ambiguous"] >= 2\n', POLLER),
    ("V1", "the split window is 30 seconds", MONITOR,
     "SPLIT_AFTER_S = 30 * 60\n", "SPLIT_AFTER_S = 30\n", POLLER),
    ("V2", "the split repeats hourly", MONITOR,
     "RENOTICE_SPLIT_AFTER_S = 24 * 3600\n", "RENOTICE_SPLIT_AFTER_S = 3600\n", POLLER),
    ("V3", "the lost-destination notice repeats every 6 hours", MONITOR,
     "RENOTICE_LOST_DESTINATIONS_AFTER_S = 24 * 3600\n",
     "RENOTICE_LOST_DESTINATIONS_AFTER_S = 6 * 3600\n", POLLER),
    ("T1", "the page lists a class with nothing alerting", MONITOR,
     '        if not _count(entry.get("alerting")):\n            continue\n',
     "        if False:\n            continue\n", POLLER),
    ("T2", "the split page swaps the two commits", MONITOR, _SPLIT_PAGE,
     _SPLIT_PAGE.replace("{api}", "{API}").replace("{worker}", "{api}").replace(
         "{API}", "{worker}"), POLLER),
    ("T3", "--delivery-url ignored", MONITOR,
     "    url = args.delivery_url or delivery_url(args.url)\n",
     "    url = delivery_url(args.url)\n", POLLER),
    ("T4", "no RECOVERED from unreachable after blindness", MONITOR,
     "        if announced in (UNREACHABLE, WORKER_DOWN):\n",
     "        if announced in (WORKER_DOWN,):\n", POLLER),
    ("L1", "a bool taken as a count in the state", MONITOR, _COUNT,
     "    if not isinstance(value, int):\n", POLLER),
    ("L2", "a negative number taken as a count in the state", MONITOR,
     "    return max(value, 0)\n", "    return value\n", POLLER),
    ("L3", "a bool taken as a count in the delivery reading", MONITOR, _READ,
     "        if not isinstance(value, int) or value < 0:\n", POLLER),
    ("L4", "a negative count taken in the delivery reading", MONITOR, _READ,
     "        if isinstance(value, bool) or not isinstance(value, int):\n", POLLER),
    ("L5", "> instead of >= at exactly the interval", MONITOR,
     "    return not spoke_at or now - spoke_at >= interval\n",
     "    return not spoke_at or now - spoke_at > interval\n", POLLER),
    ("L6", "a delivery reading taken for any status below 500", MONITOR,
     "    if status != 200:\n        return None\n",
     "    if status >= 500:\n        return None\n", POLLER),
    ("L7", "a delivery reading taken without by_class", MONITOR,
     '    if not isinstance(data, dict) or not isinstance(data.get("by_class"), dict):\n',
     "    if not isinstance(data, dict):\n", POLLER),
    ("L8", "commits() takes any string", CLASSIFY,
     "    if not all(isinstance(c, str) and _COMMIT.fullmatch(c) for c in pair):\n",
     "    if not all(isinstance(c, str) for c in pair):\n", POLLER),
    ("L9", "commits() compares whole strings, not the first seven", CLASSIFY,
     "    return pair[0][:7], pair[1][:7]\n", "    return pair[0], pair[1]\n", POLLER),
]


def pytest(tests, env: dict) -> tuple[int, list[str]]:
    """(exit code, the failed tests' node ids)."""
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p",
         "no:cacheprovider", "--tb=no", "-rf", *tests],
        capture_output=True,
        text=True,
        env=env,
    )
    failed = [
        line.split(" ", 2)[1]
        for line in run.stdout.splitlines()
        if line.startswith("FAILED ")
    ]
    if run.returncode not in (0, 1):
        print(run.stdout[-4000:], run.stderr[-4000:], sep="\n")
    return run.returncode, failed


def sweep(mutants, env: dict) -> dict[str, tuple[str, list[str]]]:
    results = {}
    for mid, what, path, old, new, tests in mutants:
        original = path.read_text()
        if original.count(old) != 1:
            sys.exit(f"mutant {mid}: its text occurs {original.count(old)} times")
        mutated = original.replace(old, new)
        try:
            compile(mutated, str(path), "exec")
            path.write_text(mutated)
            rc, failed = pytest(tests, env)
        finally:
            path.write_text(original)
        verdict = {0: "SURVIVED", 1: "killed"}.get(rc, f"ERROR (exit {rc})")
        results[mid] = (verdict, failed)
        print(f"mutant {mid} ({what}): {verdict}")
        for node in failed:
            print(f"    {node}")
    return results


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("CI only: this rewrites source files in place")
    # No bytecode: two mutants of one line can be the same size, so a cached
    # .pyc written in the same second as the next edit would run the previous.
    env = {
        **os.environ,
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for tests in (POLLER, SURFACE):
        rc, failed = pytest(tests, env)
        print(f"baseline {tests}: exit {rc}, {len(failed)} failed")
        if rc != 0:
            sys.exit(f"a baseline does not pass: {failed}")

    listed = sweep(LISTED, env)
    reviewed = sweep(REVIEWED, env)

    rows = ["| id | mutant | verdict | failed |", "|---|---|---|---|"]
    for mutants, results in ((LISTED, listed), (REVIEWED, reviewed)):
        for mid, what, *_ in mutants:
            verdict, failed = results[mid]
            names = "<br>".join(f"`{n}`" for n in failed)
            rows.append(f"| {mid} | {what} | {verdict} | {names} |")
    table = "\n".join(rows)
    print(table)
    killed = sum(v == "killed" for v, _ in reviewed.values())
    print(f"rebuilt survivors killed: {killed} of {len(REVIEWED)}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(table + "\n")

    bad = [m for m, (v, _) in listed.items() if v != "killed"]
    bad += [m for m, (v, _) in reviewed.items() if v.startswith("ERROR")]
    if bad:
        print(f"not killed, or did not run: {bad}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
