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

from scripts import fork_a_attribution as mod
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


class TestALivenessNameIsBackedByALivenessFilter:
    """#974 — `live_locks` was the alias on a bare `count(*)`. It was not
    returning a wrong number: the table holds zero expired rows because
    `cleanup_expired_locks` deletes them, so filtered and unfiltered agreed.
    The name asserted a property the query never checked, and it was true only
    because a reaper outside this script kept it true.

    So the check is inverted into a drift gate rather than pinned to a count:
    any alias claiming a lifecycle property must be backed by a FILTER that
    reads `locked_until`. A count with no filter must be named for that.
    """

    LIFECYCLE_WORDS = ("live", "in_force", "permanent", "expired", "active")

    def _select_items(self):
        """(alias, expression) for each projected column of LOCKS_SQL."""
        body = mod.LOCKS_SQL.strip()
        body = body[body.upper().index("SELECT") + 6 : body.upper().index("FROM")]
        items, depth, cur = [], 0, ""
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                items.append(cur)
                cur = ""
            else:
                cur += ch
        items.append(cur)
        out = []
        for it in items:
            expr, _, alias = it.rpartition(" AS ")
            out.append((alias.strip().lower(), " ".join(expr.split())))
        return out

    def test_every_lifecycle_alias_filters_on_locked_until(self):
        offenders = [
            alias
            for alias, expr in self._select_items()
            if any(w in alias for w in self.LIFECYCLE_WORDS)
            and "locked_until" not in expr
        ]
        assert offenders == [], (
            "these aliases claim a lock lifecycle property but the expression "
            "never reads locked_until: " + ", ".join(offenders)
        )

    def test_the_unfiltered_count_is_not_named_for_a_lifecycle(self):
        unfiltered = [
            alias for alias, expr in self._select_items() if "FILTER" not in expr
        ]
        assert unfiltered, "expected at least one unfiltered total for comparison"
        for alias in unfiltered:
            assert not any(w in alias for w in self.LIFECYCLE_WORDS), (
                f"{alias!r} is an unfiltered count(*) wearing a lifecycle name"
            )

    def test_the_gate_would_have_caught_the_defect(self):
        """Positive control: the pre-#974 shape must fail both checks."""
        before = mod.LOCKS_SQL
        try:
            mod.LOCKS_SQL = (
                "SELECT count(*) AS live_locks,"
                " count(*) FILTER (WHERE l.media_item_id IN"
                " (SELECT media_item_id FROM posting_history)) AS backed_by_history"
                " FROM media_posting_locks l"
            )
            with pytest.raises(AssertionError):
                self.test_every_lifecycle_alias_filters_on_locked_until()
            with pytest.raises(AssertionError):
                self.test_the_unfiltered_count_is_not_named_for_a_lifecycle()
        finally:
            mod.LOCKS_SQL = before
