"""#942: the deployed worker entrypoint reaches the composition root — gated.

`python -m src.main` is what the Procfile's `worker` line runs. Before this
gate, no deployed process could import one line of target code (#942's
founding measurement, re-confirmed at `1ae62bb` by two independent methods:
the closure instrument and a repo-wide literal-text sweep). The gate makes
the deployed worker ARTIFACT contain the target root while the deployed
worker BEHAVIOR stayed legacy until an operator set `WORKER_IMPL=target`
(2026-08-24). The legacy tier is gone (the tear-out, phase 01; #1216): the
artifact IS the root now, and what this gate still pins is the eager import
and the refusal of a garbage value.

The contract (constants + resolver) lives in `src.worker_impl`, a
stdlib-only leaf, so its second consumer — the reachability instrument's
label — never needs the legacy closure or its import-time settings floor to
read four strings. `src.main` enforces the contract; the dispatch tests here
prove the enforcement, the resolver tests prove the contract.

An unknown `WORKER_IMPL` value REFUSES (exit 2) rather than falling back:
an operator who typo'd the arm must get a worker that crash-loops loudly,
never a legacy worker they believe is the target. That substitution is the
plausible-wrong-value casualty class the W7 instruments exist for, one
layer earlier.
"""

import os
import pathlib
import subprocess
import sys

import pytest

import src.main as main_mod
import src.worker_impl as impl

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


class TestResolveWorkerImpl:
    def test_unset_selects_legacy(self):
        assert impl.resolve_worker_impl({}) == impl.WORKER_IMPL_LEGACY

    def test_explicit_legacy_selects_legacy(self):
        got = impl.resolve_worker_impl({impl.WORKER_IMPL_VAR: impl.WORKER_IMPL_LEGACY})
        assert got == impl.WORKER_IMPL_LEGACY

    def test_target_arms(self):
        got = impl.resolve_worker_impl({impl.WORKER_IMPL_VAR: impl.WORKER_IMPL_TARGET})
        assert got == impl.WORKER_IMPL_TARGET

    @pytest.mark.parametrize(
        "bad", ["Target", "TARGET", "1", "true", "", " target", "target "]
    )
    def test_anything_else_refuses_loudly(self, bad, capsys):
        """Exact match only, and the refusal names what it got and what is valid.

        `""` is deliberately in this list: present-but-empty is a state
        operators actually produce (a dashboard row saved blank), and it is
        NOT the same fact as unset. Treating it as legacy would make a
        half-typed arm silently serve the wrong implementation.
        """
        with pytest.raises(SystemExit) as exc:
            impl.resolve_worker_impl({impl.WORKER_IMPL_VAR: bad})
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert impl.WORKER_IMPL_VAR in err
        assert repr(bad) in err
        assert impl.WORKER_IMPL_TARGET in err
        assert impl.WORKER_IMPL_LEGACY in err


class TestDispatch:
    """main() runs exactly one root — the target's — and refuses garbage
    before it does. The legacy arm went with the legacy tier (the tear-out,
    phase 01): `legacy` and unset now mean "nothing else to run" and are
    logged as such, never refused — production has been armed since
    2026-08-24, and a boot must not fail on a variable phase 02 retires."""

    def _instrument(self, monkeypatch):
        ran = []
        monkeypatch.setattr(
            main_mod.target_worker, "main", lambda: ran.append("target")
        )
        return ran

    def test_unset_runs_the_target_root(self, monkeypatch):
        ran = self._instrument(monkeypatch)
        monkeypatch.delenv(impl.WORKER_IMPL_VAR, raising=False)
        main_mod.main()
        assert ran == ["target"]

    def test_legacy_runs_the_target_root_too(self, monkeypatch):
        ran = self._instrument(monkeypatch)
        monkeypatch.setenv(impl.WORKER_IMPL_VAR, impl.WORKER_IMPL_LEGACY)
        main_mod.main()
        assert ran == ["target"]

    def test_armed_runs_the_target_root(self, monkeypatch):
        ran = self._instrument(monkeypatch)
        monkeypatch.setenv(impl.WORKER_IMPL_VAR, impl.WORKER_IMPL_TARGET)
        main_mod.main()
        assert ran == ["target"]

    def test_garbage_refuses_before_the_root_runs(self, monkeypatch, capsys):
        ran = self._instrument(monkeypatch)
        monkeypatch.setenv(impl.WORKER_IMPL_VAR, "targit")
        with pytest.raises(SystemExit) as exc:
            main_mod.main()
        assert exc.value.code == 2
        assert ran == []


class TestDeployedClosure:
    """The reachability property itself, pinned in a fresh interpreter.

    A subprocess is the only honest boundary here (the instrument's own test
    file learned this): in the shared pytest process the target tier is
    already imported by earlier gate files, so `sys.modules` proves nothing.

    ONE spawn carries two pinned properties, because the stricter env proves
    both. The probe strips the `TARGET_*` family (read at RUN time inside
    `src.worker.main`, never at import) and `WORKER_IMPL` itself, so a pass
    simultaneously establishes (1) REACHABLE: importing the deployed
    entrypoint pulls the root and the target tier — red if anyone lazifies
    the import, which would silently unwind the deployed-axis movement
    (#979's blindness relied on in reverse); and (2) NO NEW ENV AT IMPORT:
    legacy's own env floor imports the gated module unchanged — the one
    regression this PR must not admit is the eager import breaking a legacy
    boot in an environment where legacy boots today.
    """

    def test_the_entrypoint_pulls_the_root_under_legacys_own_env_floor(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("TARGET_") and k != impl.WORKER_IMPL_VAR
        }
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
