"""The egress pins' mutants, run in CI only. Scratch: never merged.

Each mutant is one edit to a source file and the tests that should catch it.
For each, this applies it, runs those tests, and restores the source. The same
six run twice:

  HEAD  the tests as committed. The job fails unless the baseline passes and
        every mutant is killed.
  BASE  both test files as they were at BASE, to show which kills only the
        newer pins make. This pass only reports: survivors are expected and do
        not fail the job.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

EGRESS = Path("src/services/target/egress.py")
TRANSPORT = Path("src/channels/telegram_transport.py")
# The two files the pins live in, and the only tests any mutant runs.
TEST_FILES = (
    Path("tests/src/services/target/test_egress_floor.py"),
    Path("tests/src/channels/test_telegram_transport.py"),
)
TESTS = tuple(str(path) for path in TEST_FILES)
# The commit whose copies of TEST_FILES the BASE pass runs. Its full id, so the
# lookup cannot be ambiguous.
BASE = "d523e4fc988ab194bd209f219def4ad67594b82c"

Results = dict[str, tuple[str, list[str]]]

_NEVER_SENT = (
    "_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)"
)
# The one call in `_edit`, at its 12-space indent.
_EDIT_CALL = " " * 12 + "await self._call(method, payload, policy=policy)"
_EDIT_CALL_ONCE = " " * 12 + "await self._call(method, payload, policy=policy or self._once)"

# (id, what it breaks, file, exact text (must occur once), replacement, tests)
MUTANTS = [
    ("never_sent_takes_write_timeout",
     "a write timeout counts as a request that never left", EGRESS,
     _NEVER_SENT,
     "_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,"
     " httpx.WriteTimeout)", TESTS),
    ("never_sent_takes_write_error",
     "a write error counts as a request that never left", EGRESS,
     _NEVER_SENT,
     "_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,"
     " httpx.WriteError)", TESTS),
    ("never_sent_takes_read_error",
     "a read error counts as a request that never left", EGRESS,
     _NEVER_SENT,
     "_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,"
     " httpx.ReadError)", TESTS),
    ("connect_timeout_counts_as_sent",
     "a connect timeout counts as a request that may have left", EGRESS,
     _NEVER_SENT,
     "_NEVER_SENT = (httpx.ConnectError, httpx.PoolTimeout)", TESTS),
    ("pool_timeout_counts_as_sent",
     "a pool timeout counts as a request that may have left", EGRESS,
     _NEVER_SENT,
     "_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout)", TESTS),
    ("edit_sent_once",
     "an edit whose answer is lost is not attempted again", TRANSPORT,
     _EDIT_CALL,
     _EDIT_CALL_ONCE, TESTS),
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


def read_base() -> dict[Path, str]:
    """TEST_FILES as they were at BASE. Read before any file is touched: a BASE
    this clone lacks, or a file it did not have, stops the run here."""
    texts = {}
    for path in TEST_FILES:
        shown = subprocess.run(
            ["git", "show", f"{BASE}:{path}"], capture_output=True, text=True
        )
        if shown.returncode != 0:
            sys.exit(f"cannot read {path} at {BASE}: {shown.stderr.strip()}")
        texts[path] = shown.stdout
    return texts


def base_sweep(base_tests: dict[Path, str], env: dict) -> Results:
    """The mutants against TEST_FILES as they were at BASE. The committed files
    are put back afterwards."""
    head_tests = {path: path.read_text() for path in TEST_FILES}
    try:
        for path, text in base_tests.items():
            path.write_text(text)
        # The source is unmutated here, so these tests should pass. If they do
        # not, a kill below may be the old tests failing on their own.
        rc, failed = pytest(TESTS, env)
        print(f"baseline {TESTS} with the BASE test files: exit {rc}, {len(failed)} failed")
        if rc != 0:
            print(f"the BASE tests fail on the unmutated source: {failed}")
        return sweep(MUTANTS, env)
    finally:
        for path, text in head_tests.items():
            path.write_text(text)


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
    base_tests = read_base()

    print("== HEAD tests ==")
    rc, failed = pytest(TESTS, env)
    print(f"baseline {TESTS}: exit {rc}, {len(failed)} failed")
    if rc != 0:
        sys.exit(f"the baseline does not pass: {failed}")
    head = sweep(MUTANTS, env)
    print(f"HEAD tally: {tally(head)}")

    print(f"== BASE {BASE[:8]} tests ==")
    base = base_sweep(base_tests, env)
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
