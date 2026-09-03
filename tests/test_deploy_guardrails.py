"""The migration runner is ARMED — asserted, not remembered (#1195).

Until 2026-09-02 this file pinned the opposite: the runner shipped DORMANT,
because from migration 051 (the 3c schema move) an armed runner would have
performed the M.3 cutover on an ordinary deploy. That window is behind us —
051 through 066 were applied to production by hand on 2026-08-24 and
2026-08-26 (plan `00` FC-7 §7) — so an apply on deploy now meets only files
that land after this commit, which is exactly what the runner exists for. The
failure it prevents is #1195's: a migration merged, never applied, and nothing
noticing for a day.

What the guard pins now:

1. **Exactly one arming point, and it is `railway.toml`'s `preDeployCommand`.**
   Two arming points would run the runner twice per deploy from two different
   environments; one arming point anywhere other than the file the runbook
   names is the one nobody reads.
2. **No other deploy configuration invokes the runner** — the Procfile in
   particular, where a `release:` process would be a second, un-runbooked route.
3. **The predicate can fail** — a guard that has only ever passed is a guard
   nobody has proven reads anything.

Still no database, no fixtures, no corpus, deliberately: this must stay
readable when the migration suites cannot run at all.

If the runner is ever disarmed again on purpose, flip these assertions in the
same PR and say why in the runbook, so a reviewer sees both halves at once.
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
        " deliberate change: flip this test in the same PR and say why in"
        " documentation/operations/migration-runner.md."
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
