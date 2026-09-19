"""`landing/.env.local.example` names exactly what the landing reads.

Held in agreement both ways, as `.env.example` is for the Python tier
(`tests/src/test_legacy_settings_gone.py`): every `process.env.NAME` the
landing's source reads has a `NAME=` line in the example, and every line in
the example is read. The example once omitted `TARGET_API_URL` — the name
`lib/target-api.ts` reads first, before its `BACKEND_URL` fallback — so a
developer following the file configured the fallback only.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "landing"
EXAMPLE = LANDING / ".env.local.example"

#: Set by the platform (Next.js, Vercel), never by a person: not an example line.
PLATFORM = {"NODE_ENV"}

_READ = re.compile(r"process\.env\.([A-Z][A-Z0-9_]*)")
_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _sources() -> list[Path]:
    return sorted(
        p
        for ext in ("ts", "tsx")
        for p in (LANDING / "src").rglob(f"*.{ext}")
        if ".test." not in p.name
    )


def _read_names() -> set[str]:
    names: set[str] = set()
    for p in _sources():
        names.update(_READ.findall(p.read_text(encoding="utf-8")))
    return names - PLATFORM


def _example_names() -> set[str]:
    return set(_LINE.findall(EXAMPLE.read_text(encoding="utf-8")))


def test_the_finder_sees_a_known_read():
    """Positive control: `lib/db.ts` reads `DATABASE_URL`."""
    assert "DATABASE_URL" in _read_names()


def test_every_variable_the_landing_reads_is_in_the_example():
    missing = _read_names() - _example_names()
    assert not missing, (
        f"read by landing/src, absent from .env.local.example: {sorted(missing)}"
    )


def test_every_example_line_is_read_by_the_landing():
    unread = _example_names() - _read_names()
    assert not unread, (
        f"in .env.local.example, read by nothing under landing/src: {sorted(unread)}"
    )
