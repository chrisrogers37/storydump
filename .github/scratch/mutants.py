"""Where the scheduling monitor counts the API as down, run in CI only.
Scratch: never merged.

Each mutant moves `api_unreachable`'s line. For each, this applies it, runs
the monitor's and the classifier's tests, and restores the source. It fails
unless the baseline passes and every mutant fails at least one test.
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
_HOLD = "    return status == 0 or status >= 500\n"
LISTED = [
    ("H1", "the API counts as down only with no answer", MONITOR, _HOLD,
     "    return status == 0\n", POLLER),
    ("H2", "the API counts as down on any 4xx too", MONITOR, _HOLD,
     "    return status == 0 or status >= 400\n", POLLER),
]
REVIEWED = []


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
    for tests in (POLLER,):
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
