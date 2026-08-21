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


class TestTheHitsAreDifferentialNotCumulative:
    """The measurement must not depend on what was imported before it.

    `closure_for` computed `hits` from all of `sys.modules` rather than from
    `after - before`, so an earlier call's target modules were attributed to
    every later one. Measured on the pre-fix code: with `src.worker` imported
    first, `src.main` reported 14 target hits while importing none of them —
    which reads as "the deployed worker reaches target code", i.e. the cutover
    blocker RESOLVED. It failed toward the answer everyone wants.
    """

    def test_a_module_that_imports_no_target_reports_none_after_one_that_does(
        self,
    ):
        first = tr.closure_for("src.worker")
        assert first[1], "precondition: src.worker must reach target modules"
        _, hits, _ = tr.closure_for("src.main")
        assert hits == set(), (
            "src.main imports no target module, so measuring it AFTER a module "
            f"that does must still report none — got {sorted(hits)}"
        )

    def test_the_positive_control_is_cumulative_and_survives_being_second(self):
        """It must not go false merely because an earlier call imported it.

        A control that fails on correct behaviour teaches its reader to ignore
        it, so this asymmetry with `hits` is deliberate and is pinned here.
        """
        tr.closure_for("src.worker")
        _, _, positive_ok = tr.closure_for("src.main")
        assert positive_ok, (
            f"{tr.CONTROL_POSITIVE} is imported once per process; the positive "
            "control reads the cumulative closure and must stay true"
        )

    def test_the_negative_control_names_a_module_that_cannot_exist(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(tr.CONTROL_NEGATIVE)
