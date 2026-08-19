"""#909 — the standing gate: no aware datetime compared to a naive column.

`get_recent_posts` was reported as one instance. It was fifteen, across two
files, and the class is not localised — which is exactly why a fix that only
patched the reported line would have left the next reader to rediscover it.
This asserts the population is empty rather than trusting that it stays so.

**Why static rather than runtime.** The defect does not raise and does not
change behaviour on a UTC session, so a runtime test only catches it on a
non-UTC connection — and CI's `postgres:15` defaults to `Etc/UTC`. A sixteenth
instance would therefore ship green through every existing test. The behaviour
is pinned separately in `test_naive_column_timezone_invariance.py`; this pins
the *population*.

**What it covers, stated rather than implied.** Every `Compare` node under
`src/` whose one side is an attribute access on a class that declares that
attribute as a `Column(DateTime)` in `src/models/`. Awareness of the other side
is resolved from: `datetime.now(tz)` (aware), `datetime.now()` / `utcnow()`
(naive), `ensure_utc()` (aware), `naive_utc()` (naive), a local name assigned
from one of those, or the left operand of an arithmetic expression on one.

**What it does not cover, equally stated.** A cutoff that reaches the
comparison through a function call, an attribute, or a container is `UNKNOWN`,
and `UNKNOWN` is not a failure here — it is reported. Making it one would
either force noise at every legitimate parameter or tempt someone to silence
the check. The four `UNKNOWN` sites #909 found were closed by coercing at the
method boundary instead, which is a fix the caller cannot undo; this test
asserts that count has not grown.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[3] / "src"

#: Sites where the cutoff arrives as a parameter, so awareness is the caller's.
#: Each is coerced at the method boundary (`x = naive_utc(x)`), which is why
#: they are safe; the number is pinned so a NEW one has to be looked at.
EXPECTED_UNKNOWN = 3


def _naive_datetime_columns() -> set:
    """`{"Class.attr"}` for every `Column(DateTime)` without `timezone=True`."""
    naive = set()
    for path in (SRC / "models").rglob("*.py"):
        for cls in [
            n
            for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.ClassDef)
        ]:
            for stmt in cls.body:
                if not isinstance(stmt, (ast.AnnAssign, ast.Assign)):
                    continue
                value = stmt.value
                if not (
                    isinstance(value, ast.Call)
                    and getattr(value.func, "id", None) == "Column"
                ):
                    continue
                is_datetime = any(
                    (isinstance(a, ast.Name) and a.id == "DateTime")
                    or (
                        isinstance(a, ast.Call)
                        and getattr(a.func, "id", None) == "DateTime"
                    )
                    for a in value.args
                )
                if not is_datetime:
                    continue
                tz_aware = any(
                    isinstance(a, ast.Call)
                    and getattr(a.func, "id", None) == "DateTime"
                    and any(
                        k.arg == "timezone" and getattr(k.value, "value", False) is True
                        for k in a.keywords
                    )
                    for a in value.args
                )
                if tz_aware:
                    continue
                targets = (
                    [stmt.target] if isinstance(stmt, ast.AnnAssign) else stmt.targets
                )
                for t in targets:
                    if isinstance(t, ast.Name):
                        naive.add(f"{cls.name}.{t.id}")
    return naive


def _awareness(node, aware_names: set, naive_names: set) -> str:
    if isinstance(node, ast.Name):
        if node.id in aware_names:
            return "AWARE"
        if node.id in naive_names:
            return "NAIVE"
        return "UNKNOWN"
    if isinstance(node, ast.Call):
        func = node.func
        if getattr(func, "attr", None) == "replace":
            # `.replace(tzinfo=None)` is the hand-rolled spelling of naive_utc;
            # `.replace(tzinfo=<anything else>)` makes it aware.
            for kw in node.keywords:
                if kw.arg == "tzinfo":
                    return (
                        "NAIVE" if getattr(kw.value, "value", "x") is None else "AWARE"
                    )
            return _awareness(func.value, aware_names, naive_names)
        if getattr(func, "attr", None) == "astimezone":
            return "AWARE"
        if getattr(func, "attr", None) == "now":
            return "AWARE" if (node.args or node.keywords) else "NAIVE"
        if getattr(func, "attr", None) == "utcnow":
            return "NAIVE"
        if getattr(func, "id", None) == "ensure_utc":
            return "AWARE"
        if getattr(func, "id", None) == "naive_utc":
            return "NAIVE"
    if isinstance(node, ast.BinOp):
        return _awareness(node.left, aware_names, naive_names)
    return "UNKNOWN"


def _scan() -> tuple:
    """Returns (hazards, unknowns, sites_examined)."""
    naive_cols = _naive_datetime_columns()
    hazards, unknowns, examined = [], [], 0
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            aware_names, naive_names = set(), set()
            for n in ast.walk(fn):
                if (
                    isinstance(n, ast.Assign)
                    and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                ):
                    verdict = _awareness(n.value, aware_names, naive_names)
                    if verdict == "AWARE":
                        aware_names.add(n.targets[0].id)
                    elif verdict == "NAIVE":
                        naive_names.add(n.targets[0].id)
            for cmp_ in [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]:
                sides = [cmp_.left] + list(cmp_.comparators)
                if len(sides) != 2:
                    continue
                for i, side in enumerate(sides):
                    if not (
                        isinstance(side, ast.Attribute)
                        and isinstance(side.value, ast.Name)
                    ):
                        continue
                    key = f"{side.value.id}.{side.attr}"
                    if key not in naive_cols:
                        continue
                    examined += 1
                    verdict = _awareness(sides[1 - i], aware_names, naive_names)
                    where = f"{path.relative_to(SRC.parent)}:{cmp_.lineno} {fn.name} ({key})"
                    if verdict == "AWARE":
                        hazards.append(where)
                    elif verdict == "UNKNOWN":
                        unknowns.append(where)
    return hazards, unknowns, examined


class TestNoAwareValueIsComparedToANaiveColumn:
    def test_the_scanner_finds_the_columns_it_is_supposed_to_scan(self):
        """Positive control on the analyser itself.

        Without this the whole gate passes vacuously the day a models refactor
        stops matching the `Column(DateTime)` shape — zero columns means zero
        comparisons means zero hazards, and a green suite says nothing.
        """
        columns = _naive_datetime_columns()
        assert len(columns) >= 30, f"only {len(columns)} naive columns found"
        assert "PostingHistory.posted_at" in columns, sorted(columns)[:10]

    def test_the_scanner_examines_a_meaningful_number_of_sites(self):
        """The second half of the same control: columns can be found and still
        never compared to anything if the Compare walk breaks."""
        _, _, examined = _scan()
        assert examined >= 30, f"only {examined} comparison sites examined"

    def test_no_naive_column_is_compared_against_an_aware_datetime(self):
        hazards, _, examined = _scan()
        assert hazards == [], (
            f"{len(hazards)} of {examined} comparison sites pass an AWARE "
            f"datetime to a naive DateTime column. Postgres casts the "
            f"parameter using the SESSION timezone, so these are silently "
            f"correct on a UTC connection and silently wrong on any other "
            f"(#909). Wrap the cutoff in `naive_utc()`:\n  " + "\n  ".join(hazards)
        )

    def test_the_caller_supplied_sites_have_not_grown(self):
        """`UNKNOWN` is disclosed, not failed — but a NEW one is worth a look.

        The four that exist take their cutoff as a parameter and coerce it at
        the method boundary, so they are correct whatever the caller passes.
        A fifth may or may not do the same, and this is what makes someone
        check rather than assume.
        """
        _, unknowns, _ = _scan()
        assert len(unknowns) <= EXPECTED_UNKNOWN, (
            f"{len(unknowns)} caller-supplied cutoffs, expected at most "
            f"{EXPECTED_UNKNOWN}. Coerce the new one at the method boundary "
            f"(`since = naive_utc(since)`) and raise the constant:\n  "
            + "\n  ".join(unknowns)
        )


class TestTheTwoHelpersAreMirrors:
    """`ensure_utc` and `naive_utc` are one convention with two boundaries.

    Pinned because the obvious mistake is reaching for `ensure_utc` at the SQL
    boundary — #909's own issue suggested it — where it is a no-op on an
    already-aware value and leaves the defect in place.
    """

    def test_ensure_utc_is_a_noop_on_an_aware_value_which_is_why_it_did_not_fit(
        self,
    ):
        from datetime import datetime, timezone

        from src.utils.datetime_utils import ensure_utc

        aware = datetime.now(timezone.utc)
        assert ensure_utc(aware) is aware, (
            "ensure_utc changed an aware value — then the #909 reasoning about"
            " why it does not fit the SQL boundary would need revisiting"
        )

    def test_naive_utc_strips_tzinfo_after_converting_not_before(self):
        """The trap in the mirror: dropping tzinfo from a non-UTC offset
        without converting shifts the value silently — the same failure the
        helper exists to prevent, one layer down."""
        from datetime import datetime, timedelta, timezone

        from src.utils.datetime_utils import naive_utc

        offset = timezone(timedelta(hours=-5))
        aware = datetime(2026, 1, 1, 12, 0, tzinfo=offset)
        got = naive_utc(aware)
        assert got.tzinfo is None
        assert got == datetime(2026, 1, 1, 17, 0), (
            f"got {got} — tzinfo was dropped without converting to UTC, which"
            " shifts the value by the offset"
        )

    def test_naive_utc_passes_a_naive_value_through_so_it_is_idempotent(self):
        from datetime import datetime

        from src.utils.datetime_utils import naive_utc

        naive = datetime(2026, 1, 1, 12, 0)
        assert naive_utc(naive) is naive
        assert naive_utc(naive_utc(naive)) is naive

    def test_both_helpers_return_none_unchanged(self):
        from src.utils.datetime_utils import ensure_utc, naive_utc

        assert ensure_utc(None) is None
        assert naive_utc(None) is None
