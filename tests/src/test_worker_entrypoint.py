"""#942: the deployed worker entrypoint reaches the composition root.

`python -m src.main` is what the Procfile's `worker` line runs. Before #942
no deployed process could import one line of target code (its founding
measurement, re-confirmed at `1ae62bb` by two independent methods: the
closure instrument and a repo-wide literal-text sweep). The gate that
followed made the deployed worker ARTIFACT contain the target root while its
BEHAVIOUR stayed legacy until an operator armed the target root (2026-08-24).
The legacy tier is gone (the tear-out, phase 01; #1216) and so is the switch
(phase 02): the artifact IS the root, `src.main` runs it and reads nothing,
and what this file pins is the dispatch and the eager import.
"""

import os
import pathlib
import subprocess
import sys

import src.main as main_mod

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


class TestDispatch:
    """main() runs exactly one root — the target's — with no environment
    read in between: the switch that once chose an arm, and the refusal of a
    garbage value, retired with the legacy tier (phase 02). A worker's boot
    must not depend on a variable nothing else reads."""

    def test_main_runs_the_target_root(self, monkeypatch):
        ran = []
        monkeypatch.setattr(
            main_mod.target_worker, "main", lambda: ran.append("target")
        )
        main_mod.main()
        assert ran == ["target"]


class TestDeployedClosure:
    """The reachability property itself, pinned in a fresh interpreter.

    A subprocess is the only honest boundary here (the instrument's own test
    file learned this): in the shared pytest process the target tier is
    already imported by earlier gate files, so `sys.modules` proves nothing.

    ONE spawn carries two pinned properties, because the stricter env proves
    both. The probe strips the `TARGET_*` family (read at RUN time inside
    `src.worker.main`, never at import), so a pass simultaneously establishes
    (1) REACHABLE: importing the deployed entrypoint pulls the root and the
    target tier — red if anyone lazifies the import, which would silently
    unwind the deployed-axis movement (#979's blindness relied on in
    reverse); and (2) NO ENV AT IMPORT: the module imports with nothing of
    the tier's configuration set — a boot must not need the `TARGET_*` family
    at IMPORT time.
    """

    def test_the_entrypoint_pulls_the_root_with_no_target_variable_set(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TARGET_")}
        code = (
            "import sys, pathlib\n"
            "import src.main\n"
            "assert 'src.worker' in sys.modules, 'root not in deployed closure'\n"
            "hits = [m for m in sys.modules"
            " if m.startswith('src.services.target')]\n"
            "assert hits, 'no target module in deployed closure'\n"
            "got = pathlib.Path(src.main.__file__).resolve()\n"
            f"assert str(got).startswith({str(REPO)!r}), (\n"
            "    'measured the WRONG checkout: %s' % got)\n"
            "print('CLOSURE-OK', len(hits))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=REPO,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr[-800:]
        assert "CLOSURE-OK" in proc.stdout
