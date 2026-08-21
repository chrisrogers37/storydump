"""The Fork A partition classifier (#943).

Only the pure classifier is covered. The rest of the script is a read-only
production query whose value is the numbers it returns, and a test that mocked
those would be asserting its own fixture.

The classifier earns a test because **its one-word verdict is quotable**. A
reader who takes only the label into a ruling must not be misled by it, and the
production shape — 950 rows with no signal, 1 separable, 635 ambiguous — is
exactly the shape a two-bucket framing labels PARTIAL while the gate separates
0.1% of the population.
"""

import pytest

from scripts.fork_a_attribution import classify_partition


class TestTheVerdictSurvivesBeingQuotedAlone:
    def test_no_rows_is_its_own_answer(self):
        verdict, why = classify_partition(0, 0, 0)
        assert verdict == "NONE"
        assert "no rows" in why

    def test_separating_nothing_is_not_at_all_never_partial(self):
        """The case the three-bucket split exists for: a gate that separates
        zero rows must not be labelled PARTIAL because rows happen to fall on
        both sides of a cut."""
        verdict, why = classify_partition(950, 0, 635)
        assert verdict == "NOT-AT-ALL"
        assert "0 of 1585" in why

    def test_everything_separable_is_clean(self):
        verdict, _ = classify_partition(0, 50, 0)
        assert verdict == "CLEAN"

    def test_partial_states_the_fraction_not_just_the_word(self):
        """PARTIAL is only honest if it carries how partial. The measured
        production shape is 1 of 1586 — a verdict that said 'PARTIAL' and
        stopped would read as meaningful separation."""
        verdict, why = classify_partition(950, 1, 635)
        assert verdict == "PARTIAL"
        assert "1 of 1586" in why
        assert "0.1%" in why
        assert "950" in why and "635" in why

    @pytest.mark.parametrize(
        "no_signal,separable,ambiguous",
        [(950, 1, 635), (0, 30, 20), (5, 5, 5)],
        ids=["production-shape", "balanced", "even"],
    )
    def test_every_partial_carries_its_own_denominator(
        self, no_signal, separable, ambiguous
    ):
        """Structural, so a future edit cannot reintroduce a bare label."""
        verdict, why = classify_partition(no_signal, separable, ambiguous)
        assert verdict == "PARTIAL"
        total = no_signal + separable + ambiguous
        assert f"{separable} of {total}" in why

    def test_the_buckets_are_not_interchangeable(self):
        """A positive control on the argument ORDER. Same three numbers in a
        different order must not produce the same verdict — otherwise the
        classifier is summing where it should be discriminating."""
        assert classify_partition(950, 0, 635)[0] == "NOT-AT-ALL"
        assert classify_partition(0, 950, 635)[0] == "PARTIAL"
        assert classify_partition(0, 950, 0)[0] == "CLEAN"
