"""The scheduling monitor's mutants, run in CI only. Scratch: never merged.

Each mutant is one edit to scripts/scheduling_monitor.py and the tests that
should catch it. For each, this applies it, runs those tests, and restores the
source. The same six run twice:

  HEAD  the tests as committed. The job fails unless the baseline passes and
        every mutant is killed.
  BASE  the monitor's own test file as it was at BASE, to show which kills only
        the newer tests make. This pass only reports: survivors are expected
        and do not fail the job.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

MONITOR = Path("scripts/scheduling_monitor.py")
TEST_FILE = Path("tests/scripts/test_scheduling_monitor.py")
POLLER = (
    str(TEST_FILE),
    "tests/storydump_cli/test_health_classify_scheduling.py",
)
SURFACE = ("tests/src/api/test_app_factory.py::TestSchedulingHealthIsASecondSurface",)
# The commit whose copy of TEST_FILE the BASE pass runs. Its full id, so the
# lookup cannot be ambiguous.
BASE = "b1db1447a2cc8a89ae50d961bb544d534fa82854"

Results = dict[str, tuple[str, list[str]]]

_NO_SIGNAL = "RENOTICE_NO_SIGNAL_AFTER_S = 7 * 24 * 3600"

# (id, what it breaks, file, exact text (must occur once), replacement, tests)
MUTANTS = [
    ("no_signal_12h", "the no-signal repeat is 12 hours", MONITOR,
     _NO_SIGNAL, "RENOTICE_NO_SIGNAL_AFTER_S = 12 * 3600", POLLER),
    ("no_signal_1d", "the no-signal repeat is 1 day", MONITOR,
     _NO_SIGNAL, "RENOTICE_NO_SIGNAL_AFTER_S = 24 * 3600", POLLER),
    ("no_signal_6d", "the no-signal repeat is 6 days", MONITOR,
     _NO_SIGNAL, "RENOTICE_NO_SIGNAL_AFTER_S = 6 * 24 * 3600", POLLER),
    ("no_signal_8d", "the no-signal repeat is 8 days", MONITOR,
     _NO_SIGNAL, "RENOTICE_NO_SIGNAL_AFTER_S = 8 * 24 * 3600", POLLER),
    ("delivery_checks_failed_only",
     "the delivery reading checks only the failed count", MONITOR,
     '    for key in ("sent_in_window", "failed_or_ambiguous"):',
     '    for key in ("failed_or_ambiguous",):', POLLER),
    ("delivery_skips_int_check",
     "the delivery reading takes a count that is not an int", MONITOR,
     "        if isinstance(value, bool) or not isinstance(value, int) or value < 0:",
     "        if isinstance(value, bool) or value < 0:", POLLER),
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


def sweep(mutants, env: dict) -> Results:
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


def base_sweep(base_tests: str, env: dict) -> Results:
    """The mutants against TEST_FILE as it was at BASE. The committed file is
    put back afterwards."""
    head_tests = TEST_FILE.read_text()
    try:
        TEST_FILE.write_text(base_tests)
        # The source is unmutated here, so these tests should pass. If they do
        # not, a kill below may be the old tests failing on their own.
        rc, failed = pytest(POLLER, env)
        print(f"baseline {POLLER} with the BASE test file: exit {rc}, {len(failed)} failed")
        if rc != 0:
            print(f"the BASE tests fail on the unmutated source: {failed}")
        return sweep(MUTANTS, env)
    finally:
        TEST_FILE.write_text(head_tests)


def tally(results: Results) -> str:
    """One line: how many were killed, which survived, which did not run."""
    killed = [m for m, (v, _) in results.items() if v == "killed"]
    survived = [m for m, (v, _) in results.items() if v == "SURVIVED"]
    errored = [m for m, (v, _) in results.items() if v.startswith("ERROR")]
    line = f"killed {len(killed)}/{len(results)}, survived: {', '.join(survived) or 'none'}"
    if errored:
        line += f", did not run: {', '.join(errored)}"
    return line


def nodes(failed: list[str]) -> str:
    return "<br>".join(f"`{n}`" for n in failed)


def table(head: Results, base: Results) -> str:
    """Each mutant's verdict and failed tests, for each pass."""
    rows = [
        "| mutant | what it breaks | HEAD | failed (HEAD) | BASE | failed (BASE) |",
        "|---|---|---|---|---|---|",
    ]
    for mid, what, *_ in MUTANTS:
        (head_verdict, head_failed), (base_verdict, base_failed) = head[mid], base[mid]
        rows.append(
            f"| {mid} | {what} | {head_verdict} | {nodes(head_failed)} "
            f"| {base_verdict} | {nodes(base_failed)} |"
        )
    return "\n".join(rows)


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
    # Read before any file is touched: a BASE this clone lacks stops the run
    # here, not after the HEAD pass.
    shown = subprocess.run(
        ["git", "show", f"{BASE}:{TEST_FILE}"], capture_output=True, text=True
    )
    if shown.returncode != 0:
        sys.exit(f"cannot read {TEST_FILE} at {BASE}: {shown.stderr.strip()}")

    print("== HEAD tests ==")
    for tests in (POLLER, SURFACE):
        rc, failed = pytest(tests, env)
        print(f"baseline {tests}: exit {rc}, {len(failed)} failed")
        if rc != 0:
            sys.exit(f"a baseline does not pass: {failed}")
    head = sweep(MUTANTS, env)
    print(f"HEAD tally: {tally(head)}")

    print(f"== BASE {BASE[:8]} tests ==")
    base = base_sweep(shown.stdout, env)
    print(f"BASE tally: {tally(base)}")

    report = table(head, base)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(
                f"HEAD tests: {tally(head)}\n\n"
                f"BASE {BASE[:8]} tests: {tally(base)}\n\n{report}\n"
            )

    bad = [m for m, (v, _) in head.items() if v != "killed"]
    if bad:
        print(f"HEAD: not killed, or did not run: {bad}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
