"""The Meta App Review runbook's marker system is pinned to itself.

`documentation/operations/meta-app-review.md` labels every section with how
strongly its claims are evidenced — `WALKED`, `DOCUMENTED-FROM-META'S-DOCS`,
`COMMUNITY-REPORTED`, `NOT-YET-ATTEMPTED`. **The labels are that document's
entire value.** A submission runbook whose reader cannot tell "somebody did
this" from "somebody read this on a forum" is worse than no runbook, because it
reads authoritative either way.

That system has already drifted three times **in the two changes that created
it**, which is why this file exists rather than a note asking people to be
careful:

1. `#1203` shipped claims labelled `DOCUMENTED-FROM-META'S-DOCS` that came from
   community writeups and from the runbook's own issue (navi).
2. The legend closed with "no section is WALKED" while the document marked one.
   A legend that miscounted its own labels.
3. `#1207` added `COMMUNITY-REPORTED` defined as "applied inline, not to a whole
   section" and then used it section-level twice (rajan), and left a
   "not the same claim as the row above" cross-reference stale when the row
   moved.

The mechanism is the same every time: **a rule and its applications drift apart
at the moment the rule is written, because the author is holding the rule in
their head rather than reading it back against each use.** A human pass catches
that once. Only a committed check catches it next time — and the next author
will be editing this file under submission pressure, which is when it is least
likely to be read back.

**Bound, stated because it is the direction that reads clean.** This pins the
system to *itself*: that markers are ones the legend defines, that the legend's
own count is true, that the scope rule is followed, and that a new section
cannot arrive unlabelled. **It cannot tell whether a marker is CORRECT** — a
claim labelled `DOCUMENTED-FROM-META'S-DOCS` that actually came from a forum
post is exactly what navi found, and finding it required reading Meta's
documentation. No test here can do that; it needs a human with the external
source. Sorting a claim into the right bucket stays a human judgement, and the
legend says so where a reader will meet it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "documentation" / "operations" / "meta-app-review.md"

#: A section marker line: `**Marker: ...**` at the start of a line.
_MARKER_LINE = re.compile(r"^\*\*Marker: (.+?)\*\*", re.M)

#: An inline label: `**NAME:**` anywhere after the legend.
_INLINE = re.compile(r"\*\*([A-Z][A-Z'\-]+(?:-[A-Z'\-]+)*):\*\*")

#: `## ` headings.
_SECTION = re.compile(r"^## (.+)$", re.M)

#: Sections that carry no marker, each for a stated reason. Asserted to be a
#: SUBSET of the document's real headings below, so a renamed section cannot
#: leave a stale exemption behind quietly.
EXEMPT = {
    "How to read the status markers": "defines the markers; labelling it would be circular",
    "⚠ STANDING CONSTRAINT — do not drop the `legacy` schema before the demo videos are recorded": "a constraint this team imposes, not a claim about Meta",
    "See also": "pointers only",
    "Related issues": "pointers only",
}


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _legend_markers(text: str) -> set[str]:
    """Marker names the legend table defines."""
    head = text.split("## Why App Review is required")[0]
    return set(re.findall(r"^\| \*\*([A-Z][A-Z'\-]+)\*\* \|", head, re.M))


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) for each `## ` section, in document order."""
    parts = _SECTION.split(text)
    return list(zip(parts[1::2], parts[2::2]))


def test_every_section_marker_names_a_marker_the_legend_defines():
    """Catches an invented or typo'd marker — a label nothing explains."""
    text = _text()
    legend = _legend_markers(text)
    assert legend, "the legend table defines no markers — its shape changed"
    for raw in _MARKER_LINE.findall(text):
        if "mixed" in raw.lower():
            continue  # a per-row section; its rows carry their own status
        assert any(m in raw for m in legend), (
            f"section marker {raw!r} names no marker the legend defines "
            f"(legend defines: {sorted(legend)})"
        )


def test_the_legend_count_of_walked_sections_is_true():
    """The #1203 defect, made unrepeatable.

    The legend states how many sections are WALKED. If that sentence and the
    document disagree, the legend is doing the exact thing the markers exist
    to prevent.
    """
    text = _text()
    stated = re.search(
        r"\*\*(no|exactly one|exactly \w+) sections? (?:is|are) WALKED\*\*", text
    )
    assert stated, "the legend no longer states how many sections are WALKED"
    words = {"no": 0, "exactly one": 1}
    claimed = words.get(stated.group(1).lower())
    assert claimed is not None, (
        f"legend claims {stated.group(1)!r} WALKED sections and this test cannot "
        "read that spelling — extend `words` deliberately"
    )
    actual = sum(1 for raw in _MARKER_LINE.findall(text) if "WALKED" in raw)
    assert claimed == actual, (
        f"the legend claims {claimed} WALKED section(s); the document marks {actual}"
    )


def test_no_inline_label_sits_inside_a_section_already_carrying_it():
    """The scope rule the legend states: an inline label inside a section
    already marked with it is noise, not emphasis."""
    text = _text()
    legend = _legend_markers(text)
    for heading, body in _sections(text):
        marker = _MARKER_LINE.search(body)
        if not marker:
            continue
        section_labels = {m for m in legend if m in marker.group(1)}
        for inline in _INLINE.findall(body):
            assert inline not in section_labels, (
                f"section {heading!r} is marked {inline} and also labels a claim "
                f"inline with {inline} — forbidden by the legend's scope rule"
            )


def test_every_section_carries_a_marker_or_is_deliberately_exempt():
    """The drift ari predicted: a section added under submission pressure with
    no marker at all, which reads as authoritative by default."""
    text = _text()
    headings = [h.strip() for h, _ in _sections(text)]
    stale = set(EXEMPT) - set(headings)
    assert not stale, (
        f"EXEMPT names sections that no longer exist: {sorted(stale)} — a renamed "
        "section must not leave its exemption behind"
    )
    for heading, body in _sections(text):
        if heading.strip() in EXEMPT:
            continue
        assert _MARKER_LINE.search(body), (
            f"section {heading!r} carries no marker. Add one, or add it to EXEMPT "
            "with a reason."
        )


def test_the_legend_states_the_ranking():
    """rajan's finding 2. Four labels of equal apparent standing give the
    document more categories and no more honesty; the ranking is what makes
    them worth having."""
    text = _text()
    head = text.split("## Why App Review is required")[0]
    assert "RANKED" in head, "the legend no longer states that the markers are ranked"
    assert "use the weaker" in head.lower(), (
        "the legend no longer tells a reader which marker to pick when two apply"
    )
