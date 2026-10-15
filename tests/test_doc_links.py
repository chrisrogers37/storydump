"""Every relative link in the documentation resolves, and so does every
`documentation/…` path that a documentation page cites.

- **Links** in every tracked Markdown file except `CHANGELOG.md` and the
  fragments in `changelog.d/` that fold into it, which are history: a relative
  `[text](target)` names a file or folder that exists.
- **Pages** (`documentation/`, `.github/` and the root pages): a
  `documentation/…` path cited in prose or in code exists.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

from scripts.changelog_fragments import is_fragment


ROOT = Path(__file__).resolve().parents[1]

#: Markdown that is history rather than a pointer: never link-checked.
UNSCANNED = {"CHANGELOG.md"}
ROOT_PAGES = ("README.md", "SECURITY.md")
PAGE_ROOTS = ("documentation/", ".github/")

#: `[text](target)`, `[text](target "title")` and `[text](<target with spaces>)`.
_LINK = re.compile(r"\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
#: A URL scheme (`https:`, `mailto:`) — case-insensitive, as RFC 3986 says.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
#: A repository path under `documentation/`: not the tail of a URL such as
#: `https://cloudinary.com/documentation/…`, and without trailing punctuation.
_DOC_PATH = re.compile(r"(?<![\w/.:-])documentation/[A-Za-z0-9_./-]*[A-Za-z0-9_/-]")


def _tracked(*roots: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *roots],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [ROOT / p for p in out.decode().split("\0") if p]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _markdown() -> list[Path]:
    return [
        p
        for p in _tracked()
        if p.suffix == ".md"
        and (rel := _rel(p)) not in UNSCANNED
        and not is_fragment(rel)
    ]


def _is_page(page: Path) -> bool:
    rel = _rel(page)
    return rel in ROOT_PAGES or rel.startswith(PAGE_ROOTS)


def _broken_links() -> Counter:
    broken: Counter = Counter()
    for page in _markdown():
        for angled, plain in _LINK.findall(page.read_text(encoding="utf-8")):
            target = angled or plain
            if _SCHEME.match(target) or target.startswith("#"):
                continue  # a URL, a mailto:, an in-page anchor
            path = target.split("#", 1)[0]
            if path and not (page.parent / path).exists():
                broken[(_rel(page), target)] += 1
    return broken


def _page_cites() -> set[tuple[str, str]]:
    return {
        (_rel(page), path)
        for page in _markdown()
        if _is_page(page)
        for path in _DOC_PATH.findall(page.read_text(encoding="utf-8"))
    }


def test_every_scan_sees_what_it_claims_to():
    """Positive controls: a scan that reads nothing passes everything."""
    pages = {_rel(p) for p in _markdown()}
    assert {
        "README.md",
        "documentation/README.md",
        "documentation/guides/testing-guide.md",
        ".github/README.md",
    } <= pages
    assert "CHANGELOG.md" not in pages
    assert "changelog.d/README.md" in pages
    assert not any(is_fragment(page) for page in pages)
    index = (ROOT / "documentation/README.md").read_text(encoding="utf-8")
    assert "guides/testing-guide.md" in {a or p for a, p in _LINK.findall(index)}
    cites = _page_cites()
    assert ("README.md", "documentation/guides/testing-guide.md") in cites
    assert (".github/README.md", "documentation/guides/ci-cd-pipeline.md") in cites


def test_the_patterns_parse_the_forms_they_claim_to():
    assert [a or p for a, p in _LINK.findall('[x](a.md "t") [y](<b c.md>)')] == [
        "a.md",
        "b c.md",
    ]
    assert _SCHEME.match("HTTPS://example.com")
    assert not _SCHEME.match("guides/x.md")
    assert _DOC_PATH.findall("https://cloudinary.com/documentation/upload") == []
    assert _DOC_PATH.findall("see `documentation/guides/x.md`.") == [
        "documentation/guides/x.md"
    ]


def test_every_relative_link_in_the_docs_resolves():
    broken = sorted(_broken_links().elements())
    assert not broken, (
        "links to nothing — a moved or deleted page is still linked:\n  "
        + "\n  ".join(f"{page}  →  {target}" for page, target in broken)
    )


def test_every_documentation_path_a_page_cites_exists():
    missing = sorted(
        (page, path) for page, path in _page_cites() if not (ROOT / path).exists()
    )
    assert not missing, (
        "a page cites documentation that is not there — update the path:\n  "
        + "\n  ".join(f"{page}: {path}" for page, path in missing)
    )
