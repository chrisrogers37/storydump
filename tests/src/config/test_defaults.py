"""Every constant `src/config/defaults.py` declares is read by the target tier.

The file once carried fourteen: twelve of them (`DEFAULT_POSTS_PER_DAY`,
`DEFAULT_POSTING_TIMEZONE`, the toggles, the caption style ...) were read by
nothing after the legacy tier's deletion, and several disagreed with the DDL
defaults a workspace actually starts with (053's `posting_hours_start` is 14;
the constant said 9). A constant nobody reads is a claim about the product
that nothing checks; this pin keeps the file at what is consulted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = ROOT / "src" / "config" / "defaults.py"


def _declared() -> list[str]:
    tree = ast.parse(DEFAULTS.read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _readers(name: str) -> list[Path]:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    return sorted(
        p.relative_to(ROOT)
        for p in (ROOT / "src").rglob("*.py")
        if p != DEFAULTS and pattern.search(p.read_text(encoding="utf-8"))
    )


def test_the_file_declares_something():
    assert _declared(), "the pin would pass vacuously on an empty file"


def test_the_checker_sees_a_known_reader():
    """Positive control: a grep that finds nothing would report every constant
    unread — this asserts the finder finds the fallback `intent_ledger` reads."""
    assert Path("src/services/target/intent_ledger.py") in _readers(
        "DEFAULT_REPOST_TTL_DAYS"
    )


@pytest.mark.parametrize("name", _declared())
def test_every_declared_default_is_read_by_the_target_tier(name):
    assert _readers(name), (
        f"{name} is declared in src/config/defaults.py and read by nothing under src/ — "
        "delete it, or the reader that needs it is missing"
    )
