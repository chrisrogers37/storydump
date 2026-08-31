"""`scripts/pr_ready.sh` — the refusal shapes, driven by fixtures.

The gate exists because a rollup answers "did any check fail", which is blind
to a check that never ran. **Its first version contained that same defect twice**
(rajan, review of #1145, both reproduced live): the staleness check
note-and-continued when `gh api` failed, and the drift gate silently skipped
when `ci.yml` was unreadable — so a failure to obtain a value rendered as a
passing value, one layer up from the bug it was written to catch.

Nothing protected it from that, because it had no tests: "verified by exercise"
proves the PRs of one afternoon and does not survive the author leaving. These
drive the decision logic through `--from-json` / `--behind`, so every arm is
reachable without the network — including the two regressions above.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pr_ready.sh"

#: The six job names `ci.yml` declares. The gate pins its list against the real
#: file, so a fixture workflow must agree or the drift gate (correctly) refuses.
JOBS = [
    "Lint",
    "FC-2 Telegram ratchet",
    "Test",
    "Security Scan",
    "Front End",
    "Changelog Check",
]


@pytest.fixture()
def workflow(tmp_path: Path) -> Path:
    wf = tmp_path / "ci.yml"
    wf.write_text(
        "jobs:\n" + "".join(f"  j{i}:\n    name: {n}\n" for i, n in enumerate(JOBS))
    )
    return wf


def _rollup(**over) -> dict:
    checks = [
        {
            "__typename": "CheckRun",
            "name": n,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }
        for n in JOBS
    ]
    payload = {
        "mergeable": "MERGEABLE",
        "headRefOid": "abc1234def",
        "headRefName": "feat/x",
        "baseRefName": "main",
        "statusCheckRollup": checks,
    }
    payload.update(over)
    return payload


def run(tmp_path, workflow, payload, behind="0", *names):
    f = tmp_path / "pr.json"
    f.write_text(json.dumps(payload))
    cmd = [
        str(SCRIPT),
        "--from-json",
        str(f),
        "--behind",
        str(behind),
        "--workflow",
        str(workflow),
        *names,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


class TestItSaysReadyWhenItShould:
    def test_a_current_mergeable_all_green_pr_is_ready(self, tmp_path, workflow):
        r = run(tmp_path, workflow, _rollup())
        assert r.returncode == 0, r.stdout + r.stderr
        assert "READY" in r.stdout and "NOT READY" not in r.stdout

    def test_a_status_context_node_is_read_by_state_not_conclusion(
        self, tmp_path, workflow
    ):
        """CheckRun carries `.conclusion`, StatusContext carries `.state`.
        Reading one silently drops the other."""
        checks = [
            {"__typename": "StatusContext", "context": n, "state": "SUCCESS"}
            for n in JOBS
        ]
        r = run(tmp_path, workflow, _rollup(statusCheckRollup=checks))
        assert r.returncode == 0, r.stdout + r.stderr


class TestTheFourWaysAGreenRollupLies:
    def test_conflicting_is_refused_even_with_every_check_green(
        self, tmp_path, workflow
    ):
        """The measured case: four PRs CONFLICTING while reading all-green,
        because `pull_request` workflows are never scheduled in that state."""
        r = run(tmp_path, workflow, _rollup(mergeable="CONFLICTING"))
        assert r.returncode == 1
        assert "not scheduled" in r.stdout

    def test_behind_the_base_is_refused_even_with_every_check_green(
        self, tmp_path, workflow
    ):
        r = run(tmp_path, workflow, _rollup(), behind="7")
        assert r.returncode == 1
        assert "7 commit(s) behind" in r.stdout

    def test_an_absent_required_check_is_refused(self, tmp_path, workflow):
        """ "Nothing failed" is TRUE of a check that never ran."""
        checks = [c for c in _rollup()["statusCheckRollup"] if c["name"] != "Test"]
        r = run(tmp_path, workflow, _rollup(statusCheckRollup=checks))
        assert r.returncode == 1
        assert "'Test' is ABSENT" in r.stdout

    def test_a_running_check_is_refused_and_named_running_not_absent(
        self, tmp_path, workflow
    ):
        """RUNNING and ABSENT need opposite responses — wait vs investigate.
        An earlier draft collapsed them because `gh` renders a pending
        conclusion as "" rather than null, so `//` never fell through."""
        checks = _rollup()["statusCheckRollup"]
        checks[2] = {
            "__typename": "CheckRun",
            "name": "Test",
            "status": "IN_PROGRESS",
            "conclusion": "",
        }
        r = run(tmp_path, workflow, _rollup(statusCheckRollup=checks))
        assert r.returncode == 1
        assert "RUNNING(IN_PROGRESS)" in r.stdout
        assert "ABSENT" not in r.stdout


class TestAFailureToObtainAValueNeverRendersAsAPassingValue:
    """The two regressions from review, and the rule they share.

    Both said READY when their own supporting instrument could not answer —
    the identical failure mode as a check that never ran reading as
    "nothing failed", recreated inside the thing meant to catch it.
    """

    def test_an_unreadable_workflow_refuses_rather_than_skipping_the_drift_gate(
        self, tmp_path, workflow
    ):
        r = run(tmp_path, tmp_path / "does-not-exist.yml", _rollup())
        assert r.returncode == 3, r.stdout + r.stderr
        assert "CANNOT LOOK" in r.stderr

    def test_a_workflow_that_parses_to_zero_jobs_refuses(self, tmp_path):
        empty = tmp_path / "empty.yml"
        empty.write_text("jobs:\n  build:\n    runs-on: ubuntu-latest\n")
        r = run(tmp_path, empty, _rollup())
        assert r.returncode == 3
        assert "zero job names" in r.stderr

    def test_a_non_numeric_behind_refuses_rather_than_continuing(
        self, tmp_path, workflow
    ):
        """`gh api` writes its error body to STDOUT, so a failed compare call
        arrives looking like a payload rather than as nothing."""
        r = run(
            tmp_path,
            workflow,
            _rollup(),
            behind='{"message":"Not Found","status":"404"}',
        )
        assert r.returncode == 3, r.stdout + r.stderr
        assert "CANNOT LOOK" in r.stderr
        assert "not the same as up to date" in r.stderr

    def test_a_drifted_required_list_refuses(self, tmp_path):
        """A hardcoded list that silently misses a newly added CI job
        re-creates the absence-blindness the gate exists to close."""
        wf = tmp_path / "drift.yml"
        wf.write_text(
            "jobs:\n"
            + "".join(
                f"  j{i}:\n    name: {n}\n"
                for i, n in enumerate([*JOBS, "Brand New Job"])
            )
        )
        r = run(tmp_path, wf, _rollup())
        assert r.returncode == 3
        assert "drifted" in r.stderr and "Brand New Job" in r.stderr
