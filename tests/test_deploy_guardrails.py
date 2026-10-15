"""The migration runner is ARMED — asserted, not remembered (#1195).

An apply on deploy is exactly what the runner exists for. The failure
it prevents is a merged migration that is never applied, with nothing
noticing (#1195).

What the guard pins:

1. **Exactly one arming point, and it is `railway.toml`'s `preDeployCommand`.**
   Two arming points would run the runner twice per deploy from two different
   environments; one arming point anywhere other than the file the runbook
   names is the one nobody reads.
2. **No other deploy configuration invokes the runner** — the Procfile in
   particular, where a `release:` process would be a second, un-runbooked route.
3. **The predicate can fail** — a guard that has only ever passed is a guard
   nobody has proven reads anything.

No database, no fixtures, no corpus, deliberately: this must stay
readable when the migration suites cannot run at all.

If the runner is ever disarmed on purpose, flip these assertions in the
same change and say why in the runbook, so a reviewer sees both halves at
once.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every file in the repo that can cause a command to run on deploy.
DEPLOY_CONFIGS = ("railway.toml", "Procfile")

#: What an invocation of the runner looks like in any of them.
RUNNER_MODULE = "migration_runner"

#: The one line that arms the runner, as the runbook prints it.
ARMING_POINT = 'preDeployCommand = "python -m scripts.migration_runner apply"'


def automatic_runner_invocations(text):
    """Uncommented lines invoking the migration runner.

    Deliberately substring-matched on the module name rather than on a
    particular key: `preDeployCommand`, a `release:` process, a start command
    wrapper and a Nixpacks phase all arm it the same way, and the module name
    is the one thing every form has to contain.
    """
    return [
        line.strip()
        for line in text.splitlines()
        if RUNNER_MODULE in line and not line.lstrip().startswith("#")
    ]


def _deploy_configs():
    return [(name, REPO_ROOT / name) for name in DEPLOY_CONFIGS]


def test_railway_arms_the_runner_exactly_once():
    railway = (REPO_ROOT / "railway.toml").read_text()

    assert automatic_runner_invocations(railway) == [ARMING_POINT], (
        "railway.toml must arm the migration runner exactly once, as the"
        f" runbook prints it ({ARMING_POINT!r}). Disarming or moving it is a"
        " deliberate change: flip this test with it and say why in its description."
    )


@pytest.mark.parametrize(
    "name,path", [c for c in _deploy_configs() if c[0] != "railway.toml"]
)
def test_no_other_deploy_config_invokes_the_runner(name, path):
    if not path.exists():
        pytest.skip(f"{name} is not in this repo")

    armed = automatic_runner_invocations(path.read_text())

    assert armed == [], (
        f"{name} invokes the migration runner on deploy: {armed}. The one"
        " arming point is railway.toml's preDeployCommand; a second would run"
        " the runner twice per deploy from a different environment."
    )


def test_the_check_can_fail():
    """The predicate against armed configs it never sees in the tree — a guard
    that has only ever passed is a guard nobody has proven reads anything."""
    predeploy = 'preDeployCommand = "python -m scripts.migration_runner apply"'
    release = "release: python -m scripts.migration_runner apply"

    assert automatic_runner_invocations(f"[deploy]\n{predeploy}\n") == [predeploy]
    assert automatic_runner_invocations(f"web: uvicorn app\n{release}\n") == [release]
    assert automatic_runner_invocations(f"[deploy]\n  # {predeploy}\n") == []
    assert automatic_runner_invocations('[deploy]\nhealthcheckPath = "/health"\n') == []


def test_per_service_dashboard_start_commands_are_required():
    """A multi-process Procfile does not select the worker service's process."""
    guide = (REPO_ROOT / "documentation/guides/cloud-deployment.md").read_text()
    heading = "### Required dashboard start commands"
    assert heading in guide, "each service needs an explicit dashboard Start Command"
    section = guide.split(heading, 1)[1].split("\n### ", 1)[0]
    assert "Settings > Deploy > Start Command" in section
    processes = dict(
        line.split(": ", 1)
        for line in (REPO_ROOT / "Procfile").read_text().splitlines()
        if line.strip()
    )
    assert f"| `worker` | `{processes['worker']}` |" in section
    assert f"| `storydump` | `{processes['web']}` |" in section
    assert "required" in section.lower()
