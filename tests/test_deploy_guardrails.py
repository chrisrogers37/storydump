"""The migration runner ships DORMANT — asserted, not remembered (#746).

`04`'s ground rule is that no production schema change happens outside the M.3
window, and the runner's dormancy is what enforces it: nothing invokes
`scripts.migration_runner` automatically on deploy, so applying migrations to
production stays a deliberate human act.

**Why this became a test at F.2.1b.** Until migration 051 the worst an armed
runner could do to production was apply a fix-forward. From 051 on — the 3c
schema move — an armed runner performs the cutover: it renames `public` to
`legacy` and hands the database to a target schema that does not exist yet.
The blast radius behind one commented line changed completely, while the line
itself did not. That line has been read by hand and reported every time it
mattered, by three different people in one week; a hand-check does not survive
the person doing it.

**The invariant is general on purpose.** Not "railway.toml's `preDeployCommand`
key is commented" — that knows about exactly the one arming point that exists
today, and the next one lands somewhere it does not look. The rule is: no
deploy configuration in this repo invokes the runner automatically, whichever
file and whichever key.

No database, no fixtures, no corpus — deliberately. This guard must stay
readable when the migration suites cannot run at all.

**When the window genuinely opens**, delete this file in the same PR that arms
the runner, so a reviewer sees both halves at once.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every file in the repo that can cause a command to run on deploy.
DEPLOY_CONFIGS = ("railway.toml", "Procfile")

#: What an invocation of the runner looks like in any of them.
RUNNER_MODULE = "migration_runner"


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


@pytest.mark.parametrize("name,path", _deploy_configs())
def test_no_deploy_config_invokes_the_migration_runner(name, path):
    if not path.exists():
        pytest.skip(f"{name} is not in this repo")

    armed = automatic_runner_invocations(path.read_text())

    assert armed == [], (
        f"{name} invokes the migration runner on deploy, and the corpus now"
        f" contains the M.3 cutover (migration 051): {armed}"
    )


def test_the_dormant_switch_is_still_present_and_off():
    """Present AND off, because 'off' alone also describes a file that no
    longer mentions the runner at all — at which point the check above passes
    on a repo whose arming point moved somewhere it is not looking. This is
    what distinguishes disarmed from absent.
    """
    railway = (REPO_ROOT / "railway.toml").read_text()

    commented = [
        line.strip()
        for line in railway.splitlines()
        if RUNNER_MODULE in line and line.lstrip().startswith("#")
    ]

    assert commented, (
        "railway.toml no longer carries the commented-out runner predeploy"
        " command. If the arming point moved, point DEPLOY_CONFIGS and"
        " RUNNER_MODULE at wherever it went; if it was removed on purpose,"
        " delete this test deliberately."
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
