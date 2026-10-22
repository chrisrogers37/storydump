"""`scripts/pr_ready.sh` — the refusal shapes, driven by fixtures.

The gate exists because a rollup answers "did any check fail", which is blind to
a check that never ran. The gate holds itself to the same rule (#1145): a
staleness check whose `gh api` call fails refuses rather than noting and
continuing, and a drift gate that cannot read `ci.yml` refuses rather than
skipping, so a failure to obtain a value never renders as a passing value.

These drive the decision logic through `--from-json` / `--behind`, so every arm
is reachable without the network, both refusals above included.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pr_ready.sh"

#: The job names `ci.yml` declares. The gate pins its list against the real
#: file, so a fixture workflow must agree or the drift gate (correctly) refuses.
JOBS = [
    "Lint",
    "Telegram ratchet",
    "Test",
    "Security Scan",
    "Front End",
    "Public record",
    "Changelog Check",
    "Schema consistency",
]

#: Declared but not required: the test-shard matrix, whose legs report as
#: "Test shard 1".."Test shard 5" and which `Test` needs, every leg of it.
COVERED = ["Test shard ${{ matrix.group }}"]

SHARD_LEGS = [f"Test shard {g}" for g in range(1, 6)]


def _workflow_file(path: Path, names) -> Path:
    path.write_text(
        "jobs:\n" + "".join(f"  j{i}:\n    name: {n}\n" for i, n in enumerate(names))
    )
    return path


@pytest.fixture()
def workflow(tmp_path: Path) -> Path:
    return _workflow_file(tmp_path / "ci.yml", [*JOBS, *COVERED])


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
    def test_the_repository_workflow_matches_the_default_required_checks(
        self, tmp_path
    ):
        workflow = SCRIPT.parents[1] / ".github" / "workflows" / "ci.yml"
        result = run(tmp_path, workflow, _rollup())
        assert result.returncode == 0, result.stdout + result.stderr

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
        """A CONFLICTING PR reads all-green, because `pull_request` workflows
        are never scheduled in that state."""
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
        `gh` renders a pending conclusion as "" rather than null, so `//` never
        falls through, and a classifier that leans on it collapses the two."""
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
    """The drift gate and the staleness check, and the rule they share.

    Each refuses when its own supporting instrument cannot answer, rather
    than saying READY — the same failure mode as a check that never ran
    reading as "nothing failed", inside the thing meant to catch it.
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
        wf = _workflow_file(tmp_path / "drift.yml", [*JOBS, *COVERED, "Brand New Job"])
        r = run(tmp_path, wf, _rollup())
        assert r.returncode == 3
        assert "drifted" in r.stderr and "Brand New Job" in r.stderr

    def test_a_workflow_that_lost_the_test_shards_is_drift_too(self, tmp_path):
        """The covered list is pinned like the required one: a workflow that
        no longer declares the shard matrix is refused, naming it."""
        wf = _workflow_file(tmp_path / "no-shards.yml", JOBS)
        r = run(tmp_path, wf, _rollup())
        assert r.returncode == 3
        assert "drifted" in r.stderr and "Test shard" in r.stderr


class TestTheShardLegsAreCoveredByTest:
    """The suite runs in a matrix whose legs report as "Test shard N". They are
    declared, so the drift gate must know them, but never required: `Test`
    needs every leg and passes only when all of them did."""

    def test_a_rollup_with_only_the_required_checks_is_ready(self, tmp_path, workflow):
        r = run(tmp_path, workflow, _rollup())
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"all {len(JOBS)} required checks" in r.stdout

    def test_green_shard_legs_never_stand_in_for_test(self, tmp_path, workflow):
        checks = [c for c in _rollup()["statusCheckRollup"] if c["name"] != "Test"]
        checks += [
            {
                "__typename": "CheckRun",
                "name": leg,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
            for leg in SHARD_LEGS
        ]
        r = run(tmp_path, workflow, _rollup(statusCheckRollup=checks))
        assert r.returncode == 1
        assert "'Test' is ABSENT" in r.stdout
