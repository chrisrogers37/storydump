"""#942 reachability instrument — the two guards that were learned the hard way.

Both exist because the hand-run version of this measurement failed in exactly
these two ways on the same afternoon:

1. it imported a DIFFERENT storydump checkout (an editable install had one on
   sys.path) and returned byte-identical numbers for two different commits —
   a wrong answer whose shape is "no change";
2. it crashed on any commit predating W1, because `src.worker` did not exist —
   which is precisely the "before" half of the before/after it exists to give.

The closure numbers themselves are not asserted. They are environment- and
commit-dependent, and a test pinning them would be pinning this laptop.
"""

import pathlib

import importlib
import json
import subprocess
import sys
import textwrap
import types

from unittest import mock

import pytest

from scripts import target_reachability as tr

from scripts.target_reachability import (
    PARITY_ITEMS,
    assert_root,
    parity,
    procfile_entrypoints,
    target_modules_on_disk,
)

REPO = pathlib.Path(__file__).resolve().parents[2]


class TestTheRootAssertion:
    def test_it_accepts_the_real_repo(self):
        resolved = assert_root(REPO)
        assert REPO in resolved.parents

    def test_it_refuses_a_root_that_does_not_own_src(self, tmp_path):
        """The failure that produced a wrong measurement: `src` resolving to
        another checkout. A guard that cannot fire would not have caught it."""
        (tmp_path / "src").mkdir()
        with pytest.raises(RuntimeError, match="shadowing|not under"):
            assert_root(tmp_path)


class TestTheEntrypointsComeFromTheProcfile:
    def test_it_parses_both_deployed_processes(self):
        got = dict(procfile_entrypoints(REPO))
        assert got == {"worker": "src.main", "web": "src.api.app"}

    def test_a_third_process_would_be_picked_up(self, tmp_path):
        """Parsed rather than hardcoded, so a Procfile that grows a process
        does not silently go unmeasured — the failure mode of a fixed pair."""
        (tmp_path / "Procfile").write_text(
            "worker: python -m src.main\n"
            "web: uvicorn src.api.app:app\n"
            "target: python -m src.worker\n"
        )
        assert dict(procfile_entrypoints(tmp_path))["target"] == "src.worker"


class TestTheParityMatcherIsHonestAboutBeingAFormMatcher:
    def test_it_returns_evidence_not_a_verdict(self):
        """Every item maps to the identifiers that matched, so a reader can see
        that `IntentNotApproved` is not an approve command."""
        ev = parity(["src.services.target.publish_cap.IntentNotApproved"])
        assert ev["approve"] == ["src.services.target.publish_cap.IntentNotApproved"]

    def test_it_reports_nothing_for_a_genuinely_unserved_item(self):
        ev = parity(["src.services.target.jobs.claim_job"])
        assert ev["prompts"] == []
        assert ev["notifications"] == []

    def test_every_parity_item_is_covered(self):
        """14 items per FC-7.3; a dropped one would silently stop being measured."""
        assert len(PARITY_ITEMS) == 14
        assert parity([]).keys() == {label for label, _ in PARITY_ITEMS}


class TestTheDenominatorComesFromDisk:
    def test_it_finds_the_target_tier(self):
        on_disk = target_modules_on_disk(REPO)
        assert any(m.startswith("src.services.target") for m in on_disk)
        assert any(m.startswith("src.models.target") for m in on_disk)

    def test_it_returns_empty_for_a_tree_with_no_target_tier(self, tmp_path):
        """Absence measured against a real denominator: 0 of 0 and 0 of 28 are
        different findings, and only the second one blocks a cutover."""
        (tmp_path / "src").mkdir()
        assert target_modules_on_disk(tmp_path) == set()


class TestIsolationRemovesTheSharingNotTheSubtraction:
    """#986 — one entrypoint per interpreter, so order stops deciding.

    The shared-process version made every number a function of measurement
    order: the same module measured twice in one process gave 19 target hits
    and then 0. That under-reported ANY entrypoint whose target modules
    something earlier imported — the root was merely where it surfaced.
    """

    def test_the_same_entrypoint_twice_gives_the_same_answer(self):
        """The defect, stated as its own regression test.

        In-process the second read collapses to zero. Under `measure` the two
        reads must AGREE, because each call is its own interpreter. The count
        itself is deliberately not named here: it tracks whatever the root
        reaches today, and a literal in this docstring would go stale the next
        time a module lands in the tier — which is what the assertion below
        avoids by comparing the two reads rather than pinning either.
        """
        root = pathlib.Path(tr.__file__).resolve().parent.parent
        first = tr.measure("src.worker", root)
        second = tr.measure("src.worker", root)
        assert first[1] == second[1] and first[1], (
            "measuring the same entrypoint twice changed the answer, so "
            "isolation is not holding"
        )

    def test_a_failed_probe_RAISES_rather_than_reporting_zero(self, monkeypatch):
        """The most important property, and the reason for the whole change.

        A probe that failed and returned "0 target modules" would be
        indistinguishable from the finding #942 rests on. No answer must be a
        third state, and it must be loud.

        The failure is injected rather than provoked, deliberately: this is a
        test of the CLASSIFIER — which stderr means finding and which means
        broken — not of Python's importer. Provoking a real crash would need a
        module written into the repo tree that raises on import, which is a
        larger and dirtier fixture for a smaller claim.
        """
        root = pathlib.Path(tr.__file__).resolve().parent.parent

        class _Fail:
            returncode, stdout, stderr = 1, "", "OperationalError: connection refused"

        monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: _Fail())
        with pytest.raises(tr.MeasurementFailed):
            tr.measure("src.worker", root)

    def test_a_probe_that_exits_zero_with_no_result_also_raises(self, monkeypatch):
        """Exit 0 is not the same as an answer.

        A probe whose output was swallowed would otherwise fall through to
        whatever the caller does with a missing value — which on this
        instrument is exactly the zero it must never manufacture.
        """
        root = pathlib.Path(tr.__file__).resolve().parent.parent

        class _Silent:
            returncode, stdout, stderr = 0, "some noise, no result line\n", ""

        monkeypatch.setattr(tr.subprocess, "run", lambda *a, **k: _Silent())
        with pytest.raises(tr.MeasurementFailed):
            tr.measure("src.worker", root)

    def test_a_missing_composition_root_stays_a_FINDING_not_an_error(self):
        """`src.worker` absent means the commit predates W1 — the before half
        of the before/after this instrument exists to produce. It must remain
        distinguishable from a broken probe."""
        root = pathlib.Path(tr.__file__).resolve().parent.parent
        with pytest.raises(ModuleNotFoundError):
            tr.measure("src.definitely_absent_module", root)

    def test_the_positive_control_survives_isolation(self):
        """One of the two musts from the ruling — asserted, not assumed.

        It reads CUMULATIVE `sys.modules`, which was correct in a shared
        process because settings imports once per process. In a subprocess each
        measurement has its own process, so it must still be true for every
        entrypoint — and if it silently went false, the check that catches a
        walker seeing nothing would be dead.
        """
        root = pathlib.Path(tr.__file__).resolve().parent.parent
        for entry in ("src.main", "src.worker"):
            assert tr.measure(entry, root)[2] is True, (
                f"the positive control went false for {entry} under isolation"
            )


class TestTheChildCannotEscapeTheRootItWasGiven:
    """#989 (astrid) — probes escaped `--root` via an editable install's finder.

    When the measured tree LACKS a submodule, `PathFinder` misses, falls
    through to the finder an editable install put on the path, and the REAL
    repo backfills it. The instrument then answers about a different tree than
    the one it was pointed at, silently — and the blast radius is the
    historical case exactly, since "a tree missing submodules" is what
    measuring a commit that predates W1 means.

    **THESE ASSERT THE MECHANISM, NOT THE COLOUR, AND THAT IS DELIBERATE.**
    The escape is environment-dependent: it reproduces under a PEP 660 venv and
    does not under a setuptools-develop one. I could not make it go red in my
    install mode, which is the EXPECTED result and is not evidence the bug is
    absent — so a test that merely passes here would be decoration. What is
    checked instead is that the child is started in a way that makes the finder
    unreachable at all.
    """

    def test_the_child_is_launched_with_S_and_E(self, monkeypatch):
        """`-S` skips site.py, so no `.pth` and no `.egg-link` is processed and
        neither editable finder is installed. `-E` drops PYTHONPATH, the same
        escape by another door. Asserted on the real argv, because a flag that
        does not reach `subprocess.run` is a comment."""
        seen = {}
        real = tr.subprocess.run

        def _spy(argv, *a, **k):
            seen["argv"] = argv
            return real(argv, *a, **k)

        monkeypatch.setattr(tr.subprocess, "run", _spy)
        tr.measure("src.worker", pathlib.Path(tr.__file__).resolve().parent.parent)
        assert "-S" in seen["argv"] and "-E" in seen["argv"], seen["argv"]

    def test_a_child_launched_that_way_has_no_editable_finder(self):
        """The mechanism itself, measured in a real interpreter.

        Anything an editable install adds arrives through a file `site.py`
        reads. Under `-S` that never runs, so the finder cannot be on
        `meta_path` — which is the property the fix rests on.
        """
        out = tr.subprocess.run(
            [
                sys.executable,
                "-S",
                "-E",
                "-c",
                "import sys;print([type(f).__name__ for f in sys.meta_path])",
            ],
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr
        assert "editable" not in out.stdout.lower(), out.stdout

    def test_dependencies_still_import_so_the_child_can_work_at_all(self):
        """The other half: `-S` alone makes the child unable to import `src`.

        Site-packages is re-added by hand, which restores DEPENDENCIES without
        restoring the path injection — that lived in the files site.py reads.
        Without this the instrument fails on every probe rather than measuring.
        """
        out = tr.subprocess.run(
            [
                sys.executable,
                "-S",
                "-E",
                "-c",
                f"import sys;sys.path.append({tr._PURELIB!r});import pydantic;print('ok')",
            ],
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-300:]

    def test_the_child_asserts_the_root_it_was_given(self):
        """The parent already asserted the tree; the CHILD does the importing
        and was asserting nothing — the same guard missing one layer down."""
        # The CALL, not the name. `assert_root` also appears in the probe's
        # import line, so a substring test passes even when the call is gone --
        # measured: deleting the call left this test green.
        assert "assert_root(pathlib.Path(" in tr._PROBE, (
            "the child must CALL the assertion, not merely import it; the"
            " parent's assertion says nothing about the child's path"
        )

    def test_that_assertion_can_actually_fire(self, tmp_path):
        """The predicate the child calls must be able to refuse.

        Verified directly, since the escape it guards cannot be provoked in
        this environment: a root that does not own the `src` on the path is
        refused.
        """
        with pytest.raises(RuntimeError, match="refusing"):
            tr.assert_root(tmp_path)


class TestTheProvenanceSweep:
    """#989 review (astrid) — the belt that could not see the escape.

    `assert_root` validates the `src` ANCHOR and passes; the editable finder
    backfills SUBMODULES from another checkout afterwards. Astrid measured the
    silent wrong answer — 483 modules, 19 target hits, read from the real repo
    — with the anchor assertion green. This sweep observes what the measurement
    actually loaded, which is the object that moves.

    Driven through an INJECTED module map rather than the live `sys.modules`,
    for a reason that is the point of the test rather than a convenience: the
    escape does not reproduce in this install mode (egg-link, not PEP 660), so
    the environment cannot supply the condition. A test that called
    `assert_provenance(tmp_path)` against the live map would fire on whatever
    `src.*` the suite had already imported and pass without exercising the
    predicate at all — green for a reason unrelated to the fix.
    """

    @staticmethod
    def _loaded_from(path: pathlib.Path) -> types.ModuleType:
        m = types.ModuleType("stand-in")
        m.__file__ = str(path)
        return m

    def test_it_fires_on_a_submodule_backfilled_from_another_checkout(self, tmp_path):
        """The escape itself: anchor satisfied by the measured tree, submodule
        served from elsewhere. This is the state astrid produced by removing
        the flags, reconstructed at the level the sweep reads."""
        root, other = tmp_path / "root", tmp_path / "other"
        (root / "src").mkdir(parents=True)
        (other / "src").mkdir(parents=True)
        mods = {
            "src": self._loaded_from(root / "src" / "__init__.py"),
            "src.worker": self._loaded_from(other / "src" / "worker.py"),
        }
        with pytest.raises(RuntimeError, match="resolved outside"):
            tr.assert_provenance(root, modules=mods)

    def test_a_sibling_sharing_the_roots_name_prefix_does_not_pass(self, tmp_path):
        """The case a string-prefix containment test cannot see.

        `/x/root-two/src/worker.py` starts with `/x/root`, so `startswith`
        reports a sibling checkout as inside the root — and a sibling checkout
        on sys.path is the exact thing that produced the original wrong
        measurement this instrument exists to prevent.
        """
        root, sibling = tmp_path / "root", tmp_path / "root-two"
        (root / "src").mkdir(parents=True)
        (sibling / "src").mkdir(parents=True)
        # The fixture must actually construct the trap, or this passes for the
        # wrong reason on a tmp layout where the names do not share a prefix.
        assert str(sibling).startswith(str(root)), "fixture built no prefix overlap"
        mods = {"src.worker": self._loaded_from(sibling / "src" / "worker.py")}
        with pytest.raises(RuntimeError, match="resolved outside"):
            tr.assert_provenance(root, modules=mods)

    def test_it_stays_quiet_when_every_src_module_came_from_the_root(self, tmp_path):
        """Negative control. Without it, a sweep that raised unconditionally —
        or one whose containment test is inverted — passes both cases above.

        The non-`src` entry is load-bearing too: every stdlib module resolves
        outside the root, so a sweep that did not scope its prefix would be
        permanently red and would be switched off within a day.
        """
        root = tmp_path / "root"
        (root / "src").mkdir(parents=True)
        mods = {
            "src": self._loaded_from(root / "src" / "__init__.py"),
            "src.worker": self._loaded_from(root / "src" / "worker.py"),
            "json": self._loaded_from(tmp_path / "stdlib" / "json.py"),
        }
        tr.assert_provenance(root, modules=mods)

    def test_a_module_carrying_no_file_is_skipped_not_crashed_on(self, tmp_path):
        """The stated bound, pinned so it stays a known gap rather than an
        AttributeError discovered in the child at measurement time."""
        root = tmp_path / "root"
        (root / "src").mkdir(parents=True)
        tr.assert_provenance(root, modules={"src.ns": types.ModuleType("src.ns")})

    def test_the_child_calls_the_sweep(self):
        """The CALL, not the name — the same pin as the anchor assertion and
        for the same measured reason: `assert_provenance` also appears on the
        probe's import line, so a substring test on the bare name stays green
        with the call deleted."""
        assert "assert_provenance(pathlib.Path(" in tr._PROBE, (
            "the child must CALL the provenance sweep after importing; a"
            " backstop that is never invoked is not a backstop"
        )

    def test_the_default_module_map_is_the_live_one(self):
        """The injected seam must not be the only path that works.

        The child calls `assert_provenance(pathlib.Path(root))` with no map, so
        the default has to bind to the real `sys.modules`. Tested against the
        real repo root with one planted stand-in, and matched on that
        stand-in's NAME — matching the generic message would let an unrelated
        ambient module satisfy this test.
        """
        planted = "src.not_served_by_this_checkout"
        marker = types.ModuleType(planted)
        marker.__file__ = str(pathlib.Path("/nowhere/near/here/src/x.py"))
        with mock.patch.dict(sys.modules, {planted: marker}):
            with pytest.raises(RuntimeError, match=planted):
                tr.assert_provenance(REPO)


class TestABrokenRootIsLoudNotAbsent:
    """#989 review (astrid) — the recursion surviving one axis over.

    `measure()` routed to the predates-W1 FINDING on a SUBSTRING test, so a
    root that merely fails to import — a typo'd submodule, a deleted-but-still-
    imported module, merge damage — printed *"no composition root exists"*.
    That is #942's literal headline, manufactured by the instrument out of a
    broken root.

    It is this file's own standard violated on the axis nobody was watching:
    the target axis already refuses to turn a failure into a zero, and the same
    rule has to hold for root-exists vs root-absent, because those are
    *different states with different remaining work*.

    The construction below is astrid's, kept as she built it rather than
    re-derived — she found the break, so her break is the thing that must stay
    red if someone reverts the fix.
    """

    @staticmethod
    def _broken_root(tmp_path: pathlib.Path) -> pathlib.Path:
        """A root that EXISTS and does not import: three files."""
        real = pathlib.Path(tr.__file__).resolve().parent.parent
        (tmp_path / "scripts").symlink_to(real / "scripts")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "__init__.py").write_text("")
        (tmp_path / "src" / "worker").mkdir()
        (tmp_path / "src" / "worker" / "__init__.py").write_text(
            "import src.worker.does_not_exist\n"
        )
        return tmp_path

    def test_a_broken_root_raises_rather_than_reading_as_absent(self, tmp_path):
        root = self._broken_root(tmp_path)
        with pytest.raises(tr.MeasurementFailed):
            tr.measure("src.worker", root)

    def test_it_is_NOT_reported_as_the_predates_W1_finding(self, tmp_path):
        """The specific misroute, asserted as its own claim.

        A bare `pytest.raises(MeasurementFailed)` would also pass if the code
        raised for some unrelated reason, so this pins that the finding branch
        is *not* taken — that is the defect, not the exception type.
        """
        root = self._broken_root(tmp_path)
        try:
            tr.measure("src.worker", root)
        except ModuleNotFoundError:  # pragma: no cover - the defect
            pytest.fail(
                "a BROKEN root was routed to the predates-W1 finding — the "
                "instrument just manufactured #942's headline"
            )
        except tr.MeasurementFailed:
            pass

    def test_a_genuinely_absent_root_is_still_the_finding(self, tmp_path):
        """Guard the guard: over-refusing would destroy the before/after.

        `src.worker` absent is the BEFORE half this instrument exists to
        produce. If the fix made every failure `MeasurementFailed`, commits
        predating W1 could no longer be measured at all.
        """
        real = pathlib.Path(tr.__file__).resolve().parent.parent
        (tmp_path / "scripts").symlink_to(real / "scripts")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "__init__.py").write_text("")
        with pytest.raises(ModuleNotFoundError):
            tr.measure("src.worker", tmp_path)

    def test_the_missing_module_is_matched_exactly_not_by_substring(self):
        assert tr._missing_module("No module named 'src.worker'") == "src.worker"
        assert (
            tr._missing_module("No module named 'src.worker.does_not_exist'")
            == "src.worker.does_not_exist"
        ), "the inner failure is the one that did not resolve"
        assert tr._missing_module("some other traceback") is None


class TestAHungImportIsNotAnAnswer:
    """#989 review (astrid) — the silent fourth state.

    `subprocess.run` carried no timeout, so an entrypoint whose import blocks
    hangs the instrument indefinitely: neither *reaches*, nor *does not reach*,
    nor *raises*. An instrument that can neither answer nor fail is not loud.
    """

    def test_a_timeout_becomes_a_loud_failure_not_a_zero(self, monkeypatch):
        root = pathlib.Path(tr.__file__).resolve().parent.parent

        def _hang(*a, **k):
            raise tr.subprocess.TimeoutExpired(cmd="probe", timeout=k.get("timeout"))

        monkeypatch.setattr(tr.subprocess, "run", _hang)
        with pytest.raises(tr.MeasurementFailed):
            tr.measure("src.worker", root)

    def test_the_probe_actually_passes_a_timeout(self):
        """The knob has to reach `subprocess.run`, not merely exist."""
        seen = {}
        real = tr.subprocess.run

        def _spy(*a, **k):
            seen.update(k)
            return real(*a, **k)

        tr.subprocess.run = _spy
        try:
            tr.measure("src.worker", pathlib.Path(tr.__file__).resolve().parent.parent)
        finally:
            tr.subprocess.run = real
        assert seen.get("timeout") == tr.PROBE_TIMEOUT_SECONDS


class TestTheHitsAreDifferentialNotCumulative:
    """The measurement must not depend on what was imported before it.

    `closure_for` computed `hits` from all of `sys.modules` rather than from
    `after - before`, so an earlier call's target modules were attributed to
    every later one. Measured on the pre-fix code: with `src.worker` imported
    first, `src.main` reported 14 target hits while importing none of them —
    which reads as "the deployed worker reaches target code", i.e. the cutover
    blocker RESOLVED. It failed toward the answer everyone wants.
    """

    #: `closure_for` is per-process-sequential BY DESIGN, so only a fresh
    #: interpreter can test it honestly. In the shared pytest process the
    #: earlier `tests/scripts/` gate files have already imported the target
    #: tier, so `src.worker`'s delta is empty before this file runs and the
    #: precondition fails — the first version of this test passed file-solo and
    #: was red on every full-suite run. **That is this PR's own defect one
    #: scope up**: the fix stopped an earlier CALL leaking into a later one, and
    #: the test then inherited an earlier FILE leaking into it. Same mechanism,
    #: wider scope. A subprocess is the only boundary that actually holds.
    PROBE = textwrap.dedent(
        """
        import json, sys
        sys.path.insert(0, %r)
        from scripts.target_reachability import closure_for
        out = {}
        for mod in ("src.worker", "src.main"):
            size, hits, positive_ok = closure_for(mod)
            out[mod] = {"size": size, "hits": sorted(hits), "positive_ok": positive_ok}
        print(json.dumps(out))
        """
    )

    def _run_probe(self):
        root = str(pathlib.Path(tr.__file__).resolve().parent.parent)
        proc = subprocess.run(
            [sys.executable, "-c", self.PROBE % root],
            capture_output=True,
            text=True,
            cwd=root,
        )
        assert proc.returncode == 0, f"probe failed: {proc.stderr[-800:]}"
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_a_module_that_imports_no_target_reports_none_after_one_that_does(
        self,
    ):
        out = self._run_probe()
        assert out["src.worker"]["hits"], (
            "precondition: src.worker must reach target modules in a FRESH "
            "process — an empty set here means the isolation is not holding"
        )
        assert out["src.main"]["hits"] == [], (
            "src.main imports no target module, so measuring it AFTER a module "
            f"that does must still report none — got {out['src.main']['hits']}"
        )

    def test_the_positive_control_is_cumulative_and_survives_being_second(self):
        """It must not go false merely because an earlier call imported it.

        A control that fails on correct behaviour teaches its reader to ignore
        it, so this asymmetry with `hits` is deliberate and is pinned here.
        """
        out = self._run_probe()
        positive_ok = out["src.main"]["positive_ok"]
        assert positive_ok, (
            f"{tr.CONTROL_POSITIVE} is imported once per process; the positive "
            "control reads the cumulative closure and must stay true"
        )

    def test_the_negative_probe_is_present_and_not_target_tier(self):
        """The probe must be able to APPEAR in hits, or it cannot go red.

        Retired the fabricated `__NONEXISTENT__` probe and its test: that test
        verified the probe could not be imported, which is precisely WHY the
        control could never fire. A control is only a control if some defect
        makes it red.
        """
        importlib.import_module(tr.CONTROL_NEGATIVE)  # present: must not raise
        assert not tr.CONTROL_NEGATIVE.startswith(tr.TARGET_PREFIXES), (
            "the negative probe must not be target tier, or a correct walker "
            "would count it and the control would be red on healthy code"
        )


class TestTheDeployedLabelCannotOutliveItsOwnPremise:
    """The label's message must be keyed on the fact it names (#942 review).

    The first version fired on "any entrypoint non-zero" and printed one fixed
    conclusion: that this is NOT the blocker clearing, because the blocker
    clears when the worker entrypoint reaches the tier. But the worker is itself
    a key in `deployed`, so on the day it DID move, the banner would have listed
    it as non-zero underneath a headline denying the blocker was clearing.

    That is a caveat outliving its premise at the exact moment it matters, which
    is the failure the label exists to prevent — so both branches are pinned,
    and the pin is what stops the message and the predicate drifting apart.
    """

    @staticmethod
    def _render(deployed, capsys):
        tr._label_deployed(deployed)
        return capsys.readouterr().out

    def test_it_is_silent_when_every_entrypoint_reads_zero(self, capsys):
        """A banner that always fires is one nobody reads."""
        out = self._render(
            {"worker": {"target_hits": []}, "web": {"target_hits": []}}, capsys
        )
        assert out == ""

    def test_a_non_clearing_entrypoint_moving_is_labelled_not_cleared(self, capsys):
        out = self._render(
            {"worker": {"target_hits": []}, "web": {"target_hits": ["x"]}}, capsys
        )
        assert "IMPORTABLE, NOT SERVING" in out
        assert "NOT the #942 blocker clearing" in out
        assert "Non-zero: web." in out

    def test_the_clearing_entrypoint_moving_gets_a_DIFFERENT_message(self, capsys):
        """The regression this class exists for.

        With the clearing entrypoint in the non-zero set, the fixed denial is
        the wrong thing to print — and printing it there is worse than the prose
        it replaced, because a script carries more authority than a comment.
        """
        out = self._render(
            {"worker": {"target_hits": ["x"]}, "web": {"target_hits": ["x"]}}, capsys
        )
        assert "THE CLEARING ENTRYPOINT HAS MOVED" in out
        assert "NOT the #942 blocker clearing" not in out, (
            "the label denied the blocker was clearing in the one state where "
            "its own stated clearing condition had been met"
        )
        assert "CANNOT confirm the blocker is cleared" in out

    def test_the_clearing_entrypoint_is_a_real_procfile_entrypoint(self):
        """A constant naming an entrypoint that does not exist can never fire."""
        procs = [proc for proc, _mod in tr.procfile_entrypoints(_repo_root())]
        assert tr.CLEARING_ENTRYPOINT in procs, (
            f"{tr.CLEARING_ENTRYPOINT!r} is not in the Procfile ({procs}), so the "
            "clearing branch is unreachable and the label can only ever deny"
        )


def _repo_root():
    return pathlib.Path(__file__).resolve().parents[2]
