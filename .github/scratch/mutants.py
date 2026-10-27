"""The scheduling monitor's 11 mutants, run in CI only. Scratch: never merged.

Each mutant is one edit to the source. For each, this applies it, runs the
monitor's and the classifier's test files, and restores the source: with the
test files as they stood at BASE first, when a BASE is given, then with this
commit's. It fails unless every baseline passes, every mutant compiles and
runs, and every mutant fails at least one test under this commit's test files.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

MONITOR = Path("scripts/scheduling_monitor.py")
CLASSIFY = Path("storydump_cli/health_classify/scheduling.py")
TESTS = [
    "tests/scripts/test_scheduling_monitor.py",
    "tests/storydump_cli/test_health_classify_scheduling.py",
]

_UNKNOWN = (
    '        # have no coverage" must not fade from memory just because it was said.\n'
)
_DOWN = "        # makes the outage longer.\n"

# (number, what it breaks, file, exact text (must occur once), replacement)
MUTANTS = [
    (
        1,
        "EXIT_SPOKE = 12",
        MONITOR,
        "EXIT_QUIET, EXIT_SPOKE, EXIT_NOTIFY_FAILED = 0, 10, 11\n",
        "EXIT_QUIET, EXIT_SPOKE, EXIT_NOTIFY_FAILED = 0, 12, 11\n",
    ),
    (
        2,
        "--stall-threshold ignored",
        MONITOR,
        "        threshold_s=args.stall_threshold,\n",
        "        threshold_s=DEFAULT_STALL_THRESHOLD_S,\n",
    ),
    (
        3,
        "--worker-overdue-threshold ignored",
        MONITOR,
        "        worker_overdue_s=args.worker_overdue_threshold,\n",
        "        worker_overdue_s=DEFAULT_WORKER_OVERDUE_S,\n",
    ),
    (
        4,
        "an absent worker block with a stall kept as STALLED",
        CLASSIFY,
        '    if worker in ("invalid", "absent"):\n',
        '    if worker == "invalid" or (worker == "absent" and not '
        "(lag is not None and lag > threshold_s)):\n",
    ),
    (
        5,
        "no RECOVERED after WORKER_DOWN",
        MONITOR,
        "    if announced in (STALLED, UNREACHABLE, WORKER_DOWN):\n",
        "    if announced in (STALLED, UNREACHABLE):\n",
    ),
    (
        6,
        "no RECOVERED after UNREACHABLE",
        MONITOR,
        "    if announced in (STALLED, UNREACHABLE, WORKER_DOWN):\n",
        "    if announced in (STALLED, WORKER_DOWN):\n",
    ),
    (
        7,
        "no SIGNAL ACQUIRED after WORKER_UNKNOWN",
        MONITOR,
        "    if announced in (NO_SIGNAL, WORKER_UNKNOWN):\n",
        "    if announced in (NO_SIGNAL,):\n",
    ),
    (
        8,
        "WORKER_DOWN never repeats",
        MONITOR,
        _DOWN + "        if not repeat_due(REALERT_AFTER_S):\n",
        _DOWN + '        if not repeat_due(float("inf")):\n',
    ),
    (
        9,
        "UNREACHABLE never repeats",
        MONITOR,
        "        if run < 2 or not repeat_due(REALERT_AFTER_S):\n",
        '        if run < 2 or not repeat_due(float("inf")):\n',
    ),
    (
        10,
        "WORKER_UNKNOWN never re-noticed",
        MONITOR,
        _UNKNOWN + "        if not repeat_due(RENOTICE_NO_SIGNAL_AFTER_S):\n",
        _UNKNOWN + '        if not repeat_due(float("inf")):\n',
    ),
    (
        11,
        "WORKER_UNKNOWN on the 6-h cadence",
        MONITOR,
        _UNKNOWN + "        if not repeat_due(RENOTICE_NO_SIGNAL_AFTER_S):\n",
        _UNKNOWN + "        if not repeat_due(REALERT_AFTER_S):\n",
    ),
]


def pytest(env: dict) -> tuple[int, list[str]]:
    """(exit code, the failed tests' node ids)."""
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p",
         "no:cacheprovider", "--tb=no", "-rf", *TESTS],
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


def sweep(label: str, env: dict) -> dict[int, tuple[str, list[str]]]:
    """One baseline and every mutant against the test files now in the tree."""
    rc, failed = pytest(env)
    print(f"[{label}] baseline: exit {rc}, {len(failed)} failed")
    if rc != 0:
        sys.exit(f"[{label}] the baseline does not pass: {failed}")
    results = {}
    for number, what, path, old, new in MUTANTS:
        original = path.read_text()
        if original.count(old) != 1:
            sys.exit(f"mutant {number}: its text occurs {original.count(old)} times")
        mutated = original.replace(old, new)
        try:
            compile(mutated, str(path), "exec")
            path.write_text(mutated)
            rc, failed = pytest(env)
        finally:
            path.write_text(original)
        verdict = {0: "SURVIVED", 1: "killed"}.get(rc, f"ERROR (exit {rc})")
        results[number] = (verdict, failed)
        print(f"[{label}] mutant {number} ({what}): {verdict}")
        for node in failed:
            print(f"    {node}")
    return results


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("CI only: this rewrites source and test files in place")
    base = sys.argv[1] if len(sys.argv) > 1 else None
    # No bytecode: mutants 5 and 6 are the same size, so a cached .pyc written
    # in the same second as the next edit would run the previous mutant.
    env = {
        **os.environ,
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    at_base = {}
    if base:
        head = {t: Path(t).read_text() for t in TESTS}
        for t in TESTS:
            Path(t).write_text(
                subprocess.run(
                    ["git", "show", f"{base}:{t}"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
        try:
            at_base = sweep(f"tests at {base[:8]}", env)
        finally:
            for t, text in head.items():
                Path(t).write_text(text)
    at_head = sweep("tests at HEAD", env)

    rows = ["| # | mutant | tests at base | tests at HEAD | failed at HEAD |", "|---|---|---|---|---|"]
    for number, what, *_ in MUTANTS:
        verdict, failed = at_head[number]
        rows.append(
            f"| {number} | {what} | {at_base.get(number, ('-',))[0]} | {verdict} | "
            f"{'<br>'.join(f'`{n}`' for n in failed)} |"
        )
    table = "\n".join(rows)
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(table + "\n")

    bad = [n for n, (verdict, _) in at_head.items() if verdict != "killed"]
    bad += [n for n, (verdict, _) in at_base.items() if verdict.startswith("ERROR")]
    if bad:
        print(f"not killed, or did not run: {sorted(set(bad))}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
