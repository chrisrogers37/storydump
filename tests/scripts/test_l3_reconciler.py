"""L.3 — the reconciler's decision logic, driven from stubbed evidence (#860).

The gate requires a test PER MODE. These run without a database because the
verdict logic is where the wrong answers live: the machinery is
mode-independent, so what has to be pinned is which observed value means what,
and that is a pure function.
"""

from __future__ import annotations

import pytest

from src.services.target.reconciler import (
    CONTAINER_EXPIRY_SECONDS,
    DEFAULT_MODE,
    EVIDENCE_MODES,
    INCONCLUSIVE,
    LADDER_SECONDS,
    NEGATIVE,
    POSITIVE,
    classify,
    next_delay_seconds,
)


@pytest.mark.unit
class TestTheSeamRecordsWhatZeroPointFourFound:
    def test_the_selected_mode_is_container_verdict(self):
        """0.4 selected it on 2026-08-13; the default must not drift off that."""
        assert DEFAULT_MODE == "container_verdict"

    def test_both_modes_ship(self):
        """Building only the selected mode would fail L.3's own gate, and
        evidence-capture stays correct for any account or API version where the
        positive value stops arriving."""
        assert set(EVIDENCE_MODES) == {"container_verdict", "evidence_capture"}

    def test_evidence_capture_has_BOTH_sets_empty(self):
        """That emptiness IS the mode: with no authoritative value in either
        direction the machine can never self-terminalize."""
        sets = EVIDENCE_MODES["evidence_capture"]
        assert sets["positive"] == frozenset()
        assert sets["negative"] == frozenset()


@pytest.mark.unit
class TestContainerVerdictMode:
    @pytest.mark.parametrize("code", ["PUBLISHED"])
    def test_authoritative_positive(self, code):
        assert classify(code, "container_verdict") == POSITIVE

    @pytest.mark.parametrize("code", ["EXPIRED", "ERROR"])
    def test_authoritative_negative(self, code):
        assert classify(code, "container_verdict") == NEGATIVE

    def test_IN_PROGRESS_is_inconclusive(self):
        assert classify("IN_PROGRESS", "container_verdict") == INCONCLUSIVE

    def test_FINISHED_IS_NOT_A_NEGATIVE_VERDICT(self):
        """The trap `02` §6 names explicitly, and the one wrong answer that is
        worse than no answer.

        After `publish_called`, `FINISHED` means *ready to be published* —
        evidence the publish has NOT landed, not evidence that it failed.
        Classifying it authoritative-negative would terminalize a still-live
        publish as a definite non-event, refund the cap, and report a
        successful post as failed.
        """
        assert classify("FINISHED", "container_verdict") == INCONCLUSIVE
        assert "FINISHED" not in EVIDENCE_MODES["container_verdict"]["negative"]
        assert "FINISHED" not in EVIDENCE_MODES["container_verdict"]["positive"]

    @pytest.mark.parametrize("code", [None, "", "WAT", "published", "Published"])
    def test_anything_unrecognised_is_inconclusive_never_a_verdict(self, code):
        """Including case variants: an authoritative set is an exact-value set,
        and a near-miss must not be read as the value it resembles."""
        assert classify(code, "container_verdict") == INCONCLUSIVE


@pytest.mark.unit
class TestEvidenceCaptureMode:
    @pytest.mark.parametrize(
        "code", ["PUBLISHED", "EXPIRED", "ERROR", "FINISHED", "IN_PROGRESS", None]
    )
    def test_the_machine_NEVER_self_terminalizes(self, code):
        """Not merely 'usually parks' — no observed value reaches a verdict,
        including the one container-verdict mode calls authoritative-positive.
        That difference is the entire mode."""
        assert classify(code, "evidence_capture") == INCONCLUSIVE

    def test_the_same_value_differs_between_modes(self):
        """Paired control: if PUBLISHED were inconclusive in BOTH modes the
        test above would pass while proving nothing about the seam."""
        assert classify("PUBLISHED", "container_verdict") == POSITIVE
        assert classify("PUBLISHED", "evidence_capture") == INCONCLUSIVE

    def test_an_unknown_mode_raises_rather_than_defaulting(self):
        """A typo'd mode silently falling back to the permissive default would
        terminalize intents under a policy nobody selected."""
        with pytest.raises(ValueError):
            classify("PUBLISHED", "no_such_mode")


@pytest.mark.unit
class TestThePollLadder:
    def test_the_rungs_are_05s(self):
        assert LADDER_SECONDS == (60, 300, 1800, 7200)

    def test_it_climbs_then_repeats_the_last_rung(self):
        assert [next_delay_seconds(i) for i in range(4)] == [60, 300, 1800, 7200]
        assert next_delay_seconds(4) == 7200

    def test_it_is_exhausted_at_container_expiry(self):
        """Capped at ~24 h — the ladder must not poll a container that can no
        longer exist."""
        spent = 0
        checks = 0
        while (d := next_delay_seconds(checks)) is not None:
            spent += d
            checks += 1
            assert checks < 100, "ladder never terminated"
        assert spent <= CONTAINER_EXPIRY_SECONDS

    def test_the_call_budget_matches_the_plans_own_claim(self):
        """`05` claims 15-20 calls per ambiguous intent worst case, against
        ~1,440 at a flat 60 s poll. A ladder that quietly drifted to hundreds
        would still 'work' and would blow the budget the ladder exists for."""
        checks = 0
        while next_delay_seconds(checks) is not None:
            checks += 1
        assert 10 <= checks <= 25, f"ladder issues {checks} polls"
        assert checks < 1440 / 10

    def test_a_negative_check_count_raises(self):
        with pytest.raises(ValueError):
            next_delay_seconds(-1)
