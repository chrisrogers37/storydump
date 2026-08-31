"""#1167 — the built-but-unreachable checker.

**The hardest thing to test here is the thing that matters most: that the script
cannot report a clean run when it failed to look.** Every other defect in a
checker is visible; that one renders as good news. So the tests are weighted
toward failure modes rather than toward the happy path, and the exit-code split
(`1` = found things, `3` = could not look) is asserted directly.

**Positive controls, not just negative ones.** A checker that has stopped
finding anything is indistinguishable from a clean repository unless something
pins a known instance. Two are pinned by name — `pause_workspace` (#1167's
seventh instance) and `fn_auth_plane_sweep` (its sixth) — so a parse that
quietly stops working fails here instead of reporting all clear.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import unreachable_capabilities as uc  # noqa: E402

pytestmark = pytest.mark.unit


class TestItCannotSilentlyFailToLook:
    """The class of defect this script exists to detect, in the script itself."""

    def test_a_moved_anchor_raises_instead_of_returning_an_empty_envelope(
        self, monkeypatch
    ):
        # An empty envelope would flag EVERY command as unreachable; an empty
        # vocabulary would report all clear. Both are wrong, and only one is
        # loud, so neither is allowed to happen silently.
        monkeypatch.setattr(uc, "_ENVELOPE_ANCHOR", "export const NOT_A_REAL_NAME")
        with pytest.raises(uc.CannotLook, match="anchor"):
            uc.envelope_commands()

    def test_an_unreadable_envelope_file_raises(self, monkeypatch):
        monkeypatch.setattr(uc, "_ENVELOPE_FILE", "landing/src/lib/does-not-exist.ts")
        with pytest.raises(uc.CannotLook, match="could not read"):
            uc.envelope_commands()

    def test_an_unreadable_reader_module_raises(self, monkeypatch):
        monkeypatch.setattr(uc, "_READER_MODULE", "src/services/target/nope.py")
        with pytest.raises(uc.CannotLook, match="could not read"):
            uc.probe_api_fields()

    def test_cannot_look_exits_3_and_findings_exit_1(self, monkeypatch, capsys):
        # THE load-bearing assertion. If these ever share a code, a caller
        # cannot tell "nothing is wrong" from "nothing was examined".
        assert uc.main([]) == uc.EXIT_FINDINGS
        monkeypatch.setattr(uc, "_ENVELOPE_ANCHOR", "export const NOT_A_REAL_NAME")
        assert uc.main(["--probe", "commands"]) == uc.EXIT_CANNOT_LOOK
        assert "CANNOT LOOK" in capsys.readouterr().err
        assert uc.EXIT_CANNOT_LOOK != uc.EXIT_FINDINGS != uc.EXIT_CLEAN

    def test_every_exit_code_is_distinct(self):
        codes = [
            uc.EXIT_CLEAN,
            uc.EXIT_FINDINGS,
            uc.EXIT_USAGE,
            uc.EXIT_CANNOT_LOOK,
            uc.EXIT_UNDECIDED,
        ]
        assert len(set(codes)) == len(codes)


class TestUndecidedReachesTheExitCode:
    """rajan's finding on #1170, and it was this script contracting its own
    disease: the UNDECIDED disclosure lived in the OUTPUT and not in the EXIT
    CODE, so once the `api_fields` findings triaged to zero the script would
    have returned CLEAN forever while still printing a permanent UNDECIDED.

    Present, correct, and unread by the thing that mattered — which is exactly
    what `credential_status` was. The honest text would have survived only until
    the tool was automated, which is the point of building it.
    """

    def _probe(self, *, findings: int, undecided: int) -> uc.ProbeResult:
        r = uc.ProbeResult(probe="fake", scope="synthetic")
        r.findings = [uc.Finding("fake", f"f{i}", "d") for i in range(findings)]
        r.undecided = [f"u{i}" for i in range(undecided)]
        return r

    def _exit_for(self, monkeypatch, probe: uc.ProbeResult) -> int:
        monkeypatch.setattr(uc, "PROBES", {"fake": lambda: probe})
        return uc.main(["--probe", "fake"])

    def test_no_findings_but_undecided_is_NOT_clean(self, monkeypatch):
        # The exact future state rajan projected: findings triaged to zero, the
        # undecidable axis still there. This must never be a pass.
        code = self._exit_for(monkeypatch, self._probe(findings=0, undecided=1))
        assert code == uc.EXIT_UNDECIDED
        assert code != uc.EXIT_CLEAN

    def test_nothing_at_all_is_clean(self, monkeypatch):
        # The positive control for the assertion above: without it, a checker
        # that returned UNDECIDED unconditionally would also pass that test.
        assert (
            self._exit_for(monkeypatch, self._probe(findings=0, undecided=0))
            == uc.EXIT_CLEAN
        )

    def test_findings_win_over_undecided(self, monkeypatch):
        # Findings are today's work; an undecidable axis is a standing
        # prerequisite. Folding UNDECIDED into FINDINGS would make it
        # indistinguishable from a real finding and get it triaged away.
        assert (
            self._exit_for(monkeypatch, self._probe(findings=1, undecided=1))
            == uc.EXIT_FINDINGS
        )

    def test_the_undecidable_axis_can_actually_clear(self, monkeypatch):
        # An UNDECIDED that can never clear is a permanent amber light, and a
        # permanent amber is one people learn to read past — which would rebuild
        # the habituation this whole script exists to defeat. So the
        # prerequisite is machine-checked, not asserted.
        assert uc.api_declares_response_models() is False
        monkeypatch.setattr(uc, "api_declares_response_models", lambda: True)
        assert not uc.probe_api_fields().undecided

    def test_render_names_the_code_a_machine_would_see(self):
        text = uc.render([self._probe(findings=0, undecided=1)])
        assert "UNDECIDED is not a pass" in text
        assert f"exits {uc.EXIT_UNDECIDED}" in text


class TestItDoesNotCarryACopyOfWhatItChecks:
    def test_the_port_side_is_imported_not_parsed(self):
        # A regex over commands.py would be a second copy of the registry that
        # can disagree with the first — the defect class, committed by the
        # finder. Asserted structurally: the module must import it.
        source = (Path(uc.__file__)).read_text()
        assert "from src.services.target import commands" in source
        assert "VOCABULARY" not in source.split("def port_commands")[1].split("def ")[1]

    def test_the_envelope_must_be_a_subset_of_the_vocabulary(self):
        # The self-check that caught this script's own first parser bug, where
        # English words from a comment were harvested as command names.
        with pytest.raises(uc.CannotLook, match="parse defect in this script"):
            uc.assert_envelope_is_a_subset({"approve", "not_a_command"}, {"approve"})

    def test_a_real_extraction_satisfies_that_invariant(self):
        uc.assert_envelope_is_a_subset(uc.envelope_commands(), set(uc.port_commands()))


class TestTheParserIgnoresProseAndStrings:
    def test_comments_and_strings_are_not_harvested_as_keys(self):
        noisy = 'const X = {\n  real: 1, // here: is a comment: with colons\n  "str: no": 2,\n};'
        stripped = uc._strip_ts_noise(noisy)
        assert "here" not in stripped and "str" not in stripped
        assert "real" in stripped
        assert len(stripped) == len(noisy), (
            "offsets must be preserved for the brace scan"
        )

    def test_privilege_statements_and_sql_comments_are_not_calls(self):
        sql = (
            "CREATE FUNCTION fn_x() ...;\n"
            "-- fn_x is described here in prose\n"
            "GRANT EXECUTE ON FUNCTION fn_x(interval) TO svc_worker;\n"
        )
        calls = uc._sql_calls_only(sql)
        assert calls.count("fn_x") == 1, "only the CREATE should survive"


class TestPositiveControls:
    """A checker that finds nothing must be distinguishable from a clean repo."""

    def test_it_still_finds_the_seventh_instance(self):
        names = {f.name for f in uc.probe_commands().findings}
        assert {"pause_workspace", "resume_workspace"} <= names, (
            "#1167's instance 7 is no longer detected — either it was fixed"
            " (update this test) or the probe stopped working (fix the probe)."
        )

    def test_it_still_finds_the_sixth_instance(self):
        names = {f.name for f in uc.probe_sql_functions().findings}
        assert "fn_auth_plane_sweep" in names, (
            "#1167's instance 6 is no longer detected — same two readings."
        )

    def test_a_command_with_no_executor_is_not_a_finding(self):
        # Unbuilt is a different finding with a different fix, already pinned by
        # `commands.UNBUILT`. Reporting it here would drown the real ones.
        port = uc.port_commands()
        unbuilt = {n for n, has in port.items() if not has}
        assert unbuilt, "expected some unbuilt commands to exist to test against"
        assert not (unbuilt & {f.name for f in uc.probe_commands().findings})


class TestTheAllowlistCannotBeUsedToSilence:
    def test_an_entry_without_a_reason_is_a_hard_error(self, monkeypatch):
        monkeypatch.setitem(uc.ALLOWLIST["commands"], "made_up", "too short")
        with pytest.raises(uc.CannotLook, match="no usable reason"):
            uc._validate_allowlist()

    def test_the_shipped_allowlist_passes_its_own_rule(self):
        uc._validate_allowlist()
        for entries in uc.ALLOWLIST.values():
            for reason in entries.values():
                assert len(reason.strip()) >= 20


class TestUndecidedIsNotAPass:
    def test_the_api_probe_declares_its_general_bound(self):
        # An axis silently omitted is the same defect one level up, so the
        # undecidable part is reported rather than left absent.
        res = uc.probe_api_fields()
        assert any("response model" in u for u in res.undecided)

    def test_render_says_undecided_is_not_a_pass(self):
        text = uc.render([uc.probe_api_fields()])
        assert "UNDECIDED is not a pass" in text
