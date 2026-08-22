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


def _is_word_char_for_this_query(ch: str) -> bool:
    """Word-character test for parsing `LOCKS_SQL` — and ONLY that query.

    Handles unquoted `[A-Za-z0-9_]` identifiers, which is every alias
    `LOCKS_SQL` uses. `_` is included because Python's `str.isalnum()` excludes
    it while SQL identifiers use it constantly: `rows_from_history` once carried
    a `from` that read as a bounded keyword and truncated the projection at
    `rows_`, hiding every column after it (#974, #976).

    DOES NOT HANDLE — measured on this parser, not presumed. Each still
    truncates, and a `live_locks` planted after it goes invisible:

        rows$from$history      `$` is legal in an unquoted identifier
        "rows from history"    a quoted identifier: the DELIMITER decides the
                               boundary, not the character class
        "from"                 a bare keyword as a quoted alias

    None is reachable by any alias this query currently uses, which is why #977
    was a follow-up rather than a block — unlike the underscore hole, which the
    file's own naming convention walked straight into.

    NAMED FOR ITS SCOPE ON PURPOSE (#977). The previous name claimed SQL's
    definition of an identifier character and implemented a narrower one. **A
    visible name that over-claims is worse than an inline expression**, because
    the next reader trusts it and stops checking — the same failure as a gate
    that reads as protection and does not protect. Earning the old name means a
    SQL tokenizer living inside a test helper, to parse a seven-line query it
    fully controls: more surface than the thing it guards, and a bigger place
    for holes to live.

    `test_every_alias_stays_inside_this_parsers_domain` keeps the precondition
    true, so a future alias in an unhandled shape fails loudly here rather than
    silently shrinking the gate's field of view.
    """
    return ch.isalnum() or ch == "_"


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
        """(alias, expression) for each projected column of LOCKS_SQL.

        The projection ends at the FROM **at depth 0**, not at the first one.
        Bounding it as "everything up to the next FROM" ended the scan inside
        the `backed_by_history` subquery, so that column — and every column
        after it — was invisible to the gate. A fresh `count(*) AS live_locks`
        appended there passed both checks, which is the exact defect the gate
        exists to catch, sitting in the gate's own blind spot.

        The rule that avoids the class: **bound the scope positively.**
        "Everything until I hit X" inherits whatever the text does next;
        "the tokens at depth 0" is a property of the thing being parsed.
        """
        body = mod.LOCKS_SQL.strip()
        start = body.upper().index("SELECT") + 6
        depth, end = 0, None
        i = start
        upper = body.upper()
        while i < len(body):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
            elif (
                depth == 0
                and upper.startswith("FROM", i)
                and (i == 0 or not _is_word_char_for_this_query(body[i - 1]))
                and (
                    i + 4 >= len(body) or not _is_word_char_for_this_query(body[i + 4])
                )
            ):
                end = i
                break
            i += 1
        assert end is not None, "no depth-0 FROM: the projection was not bounded"
        body = body[start:end]
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

    def test_a_defect_AFTER_the_subquery_is_visible_to_the_gate(self):
        """The control virgil built, kept as a test rather than a demonstration.

        The parser used to end the projection at the FIRST `FROM` — the one
        inside `backed_by_history`'s subquery — so any column defined at or
        after that point was outside the gate's view. He proved it by appending
        a fresh `count(*) AS live_locks`, the exact defect this gate exists to
        catch, and watching it pass both checks.

        A gate with a blind spot at the end of its own input is worse than no
        gate: it reports on the columns it happens to reach and says nothing
        about the rest, in the same green.
        """
        before = mod.LOCKS_SQL
        try:
            # appended AFTER the subquery, i.e. in the old parser's blind spot
            mod.LOCKS_SQL = before.replace(
                "AS backed_by_history\n  FROM media_posting_locks l",
                "AS backed_by_history,\n       count(*) AS live_locks"
                "\n  FROM media_posting_locks l",
            )
            assert mod.LOCKS_SQL != before, "mutation did not apply"
            aliases = [a for a, _ in self._select_items()]
            assert "live_locks" in aliases, (
                "the gate cannot see a column defined after the subquery — "
                f"it sees only {aliases}"
            )
            with pytest.raises(AssertionError):
                self.test_every_lifecycle_alias_filters_on_locked_until()
            with pytest.raises(AssertionError):
                self.test_the_unfiltered_count_is_not_named_for_a_lifecycle()
        finally:
            mod.LOCKS_SQL = before

    def test_every_alias_stays_inside_this_parsers_domain(self):
        """Guard the PRECONDITION, rather than pin the limitation.

        `_is_word_char_for_this_query` handles unquoted `[A-Za-z0-9_]` and says
        so. The risk is not that the bound exists — it is that someone adds an
        alias outside it and the projection silently truncates there, shrinking
        the gate's field of view with everything still green.

        So this asserts the INPUT stays in the domain. A test asserting the
        three unhandled shapes truncate would pin the defect instead, which is
        what `test_the_negative_control_names_a_module_that_cannot_exist` did
        on #972 before it was retired for exactly that reason.
        """
        raw = mod.LOCKS_SQL
        offenders = [
            a
            for a, _ in self._select_items()
            if not a or not all(_is_word_char_for_this_query(c) for c in a)
        ]
        assert offenders == [], (
            "these aliases fall outside what this parser handles, so the "
            "projection may truncate at one of them: " + ", ".join(offenders)
        )
        assert '"' not in raw.split("FROM media_posting_locks")[0], (
            "a quoted identifier appeared in the projection — the delimiter "
            "decides the boundary there, which this parser does not implement "
            "(see _is_word_char_for_this_query)"
        )

    def test_an_alias_containing_from_does_not_end_the_projection(self):
        """The boundary's own boundary — virgil's finding, kept as a control.

        `_` is not `isalnum`, so an alias like `rows_from_history` used to read
        as a bounded `from` and truncate the scan at `rows_`. Every alias in
        this query uses underscores, so it is the file's own naming convention
        walking into the hole, not a contrived shape.
        """
        before = mod.LOCKS_SQL
        try:
            mod.LOCKS_SQL = before.replace(
                "AS backed_by_history\n  FROM media_posting_locks l",
                "AS rows_from_history,\n       count(*) AS live_locks"
                "\n  FROM media_posting_locks l",
            )
            assert mod.LOCKS_SQL != before, "mutation did not apply"
            aliases = [a for a, _ in self._select_items()]
            assert "rows_from_history" in aliases, (
                "an alias containing 'from' ended the projection early — "
                f"the gate sees only {aliases}"
            )
            assert "live_locks" in aliases, (
                f"the column after it is invisible — gate sees {aliases}"
            )
            with pytest.raises(AssertionError):
                self.test_the_unfiltered_count_is_not_named_for_a_lifecycle()
        finally:
            mod.LOCKS_SQL = before

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
