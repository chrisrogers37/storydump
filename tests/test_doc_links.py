"""Every relative link in the documentation resolves, and so does every
`documentation/…` path that a live page, the code or a migration cites (#1395).

Plans move — `documentation/planning/` to `documentation/archive/` when they
complete — and a move that breaks a reference says nothing: before this file,
no test read link targets or cited paths. Four rules:

- **Links** in every tracked Markdown file except `CHANGELOG.md`, which is
  history (each entry names paths as they were when it was written): a relative
  `[text](target)` names a file or folder that exists. `KNOWN_BROKEN` counts
  the exceptions — the archived v1 roadmap's links to phase docs deleted in
  #311 — and is a ratchet: fixing one occurrence fails until its count drops.
- **Live pages** (`documentation/` outside `archive/`, plus `.claude/`,
  `docs/`, `.github/` and the root pages): a `documentation/…` path cited in
  prose or in code exists — except that a live plan may name a page it will
  create, so under `documentation/planning/` a cite fails only when its target
  has moved to the archive. `KNOWN_ABSENT` holds the page that names a path in
  order to say it is gone.
- **Code** (`.py` files anywhere under `src/`, `storydump_cli/` and
  `scripts/`): a cited `documentation/…` path exists. `tests/` is out of scope
  on purpose: its fixtures build fake trees under `tmp_path`.
- **Migrations** (`scripts/migrations/*.sql`): the runner checksums every
  applied file (`documentation/operations/migration-runner.md`), so a header
  that cited a plan while it was live keeps that path forever. It must resolve
  as written or under `documentation/archive/` — the archive rule as
  `documentation/planning/README.md` spells it out for an old path: a completed
  plan keeps its folder name when it moves.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Markdown that is history rather than a pointer: never link-checked.
UNSCANNED = {"CHANGELOG.md"}
ROOT_PAGES = ("AGENTS.md", "CLAUDE.md", "README.md", "PROJECT_MISSION.md")
LIVE_ROOTS = (".claude/", "docs/", ".github/")
PLANNING = "documentation/planning/"

#: `[text](target)`, `[text](target "title")` and `[text](<target with spaces>)`.
_LINK = re.compile(r"\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
#: A URL scheme (`https:`, `mailto:`) — case-insensitive, as RFC 3986 says.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
#: A repository path under `documentation/`: not the tail of a URL such as
#: `https://cloudinary.com/documentation/…`, and without trailing punctuation.
_DOC_PATH = re.compile(r"(?<![\w/.:-])documentation/[A-Za-z0-9_./-]*[A-Za-z0-9_/-]")

_ROADMAP = "documentation/archive/phases/00_MASTER_ROADMAP.md"

#: (page, target) → occurrences of a link known to be broken and deliberately
#: left: the archived v1 roadmap points at phase docs deleted in #311. It is
#: history, and re-pointing its links at other pages would not make them true.
KNOWN_BROKEN = Counter(
    {
        (_ROADMAP, "../archive/01_instagram_api.md"): 2,
        (_ROADMAP, "01_settings_and_multitenancy.md"): 2,
    }
)

#: (live page, cited path) where the page names the path to say it is gone.
KNOWN_ABSENT = {("AGENTS.md", "documentation/updates/")}


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
    return [p for p in _tracked() if p.suffix == ".md" and _rel(p) not in UNSCANNED]


def _is_live(page: Path) -> bool:
    rel = _rel(page)
    return (
        rel in ROOT_PAGES
        or rel.startswith(LIVE_ROOTS)
        or (
            rel.startswith("documentation/")
            and not rel.startswith("documentation/archive/")
        )
    )


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


def _cites(files: list[Path]) -> set[tuple[str, str]]:
    return {
        (_rel(f), p)
        for f in files
        for p in _DOC_PATH.findall(f.read_text(encoding="utf-8"))
    }


def _live_cites() -> set[tuple[str, str]]:
    return _cites([p for p in _markdown() if _is_live(p)])


def _code_cites() -> set[tuple[str, str]]:
    code = [f for f in _tracked("src", "storydump_cli", "scripts") if f.suffix == ".py"]
    return _cites(code)


def _migration_cites() -> set[tuple[str, str]]:
    return _cites([f for f in _tracked("scripts/migrations") if f.suffix == ".sql"])


def _archived(path: str) -> str:
    """Where the archive rule puts a `documentation/planning/` path once its
    plan completes: the same name under `documentation/archive/`."""
    return path.replace(PLANNING, "documentation/archive/", 1)


def _dangling(page: str, path: str) -> bool:
    """A live page's cite that points at nothing. A live plan may name a page
    it will create, so a plan's cite dangles only when its target has moved to
    the archive; every other live page cites what is there."""
    if (ROOT / path).exists():
        return False
    return not page.startswith(PLANNING) or (ROOT / _archived(path)).exists()


def test_every_scan_sees_what_it_claims_to():
    """Positive controls: a scan that reads nothing passes everything."""
    pages = {_rel(p) for p in _markdown()}
    assert {
        "documentation/README.md",
        "documentation/archive/README.md",
        "AGENTS.md",
        ".github/README.md",
    } <= pages
    assert "CHANGELOG.md" not in pages
    index = (ROOT / "documentation/README.md").read_text(encoding="utf-8")
    assert "archive/README.md" in {a or p for a, p in _LINK.findall(index)}
    live = _live_cites()
    assert ("AGENTS.md", "documentation/archive/2026-09-15-cli-v2/") in live
    assert (
        "documentation/operations/reading-the-ledger.md",
        "documentation/archive/2026-09-15-cli-v2/probes/",
    ) in live
    assert (
        ".claude/rules/migrations.md",
        "documentation/operations/migration-runner.md",
    ) in live
    assert (
        "src/services/target/ops_views.py",
        "documentation/archive/2026-09-15-cli-v2/02_reads.md",
    ) in _code_cites()
    assert (
        "scripts/migrations/082_worker_doors.sql",
        "documentation/planning/2026-09-21-worker-login-doors/00_PLAN.md",
    ) in _migration_cites()


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
    unexpected = sorted((_broken_links() - KNOWN_BROKEN).elements())
    assert not unexpected, (
        "links to nothing — a moved or deleted page is still linked:\n  "
        + "\n  ".join(f"{page}  →  {target}" for page, target in unexpected)
    )


def test_known_broken_is_still_broken():
    """The ratchet: an occurrence that stopped being broken leaves the count."""
    fixed = sorted((KNOWN_BROKEN - _broken_links()).elements())
    assert not fixed, f"these links resolve now — lower KNOWN_BROKEN: {fixed}"


def test_every_documentation_path_a_live_page_cites_exists():
    missing = sorted(
        (page, path)
        for page, path in _live_cites() - KNOWN_ABSENT
        if _dangling(page, path)
    )
    assert not missing, (
        "a live page cites documentation that is not there — update the path:\n  "
        + "\n  ".join(f"{page}: {path}" for page, path in missing)
    )


def test_a_plan_may_name_a_page_it_will_create_but_not_one_that_moved():
    """The live-page rule's one exception, pinned both ways against a plan the
    archive really holds: a forward reference passes, a moved plan does not."""
    plan = "documentation/planning/2026-08-02-consolidated-design-plan/README.md"
    moved = "documentation/planning/2026-09-21-worker-login-doors/"
    unwritten = "documentation/guides/a-page-this-plan-will-write.md"
    assert (ROOT / _archived(moved)).exists()
    assert _dangling(plan, moved)
    assert not _dangling(plan, unwritten)
    assert _dangling("AGENTS.md", unwritten)


def test_known_absent_is_still_cited_and_still_absent():
    """The ratchet: an entry whose page stopped citing it, or whose path came
    back, leaves the list."""
    stale = sorted(
        (page, path)
        for page, path in KNOWN_ABSENT
        if (page, path) not in _live_cites() or (ROOT / path).exists()
    )
    assert not stale, f"remove these from KNOWN_ABSENT: {stale}"


def test_every_documentation_path_the_code_cites_exists():
    missing = sorted((f, p) for f, p in _code_cites() if not (ROOT / p).exists())
    assert not missing, (
        "the code cites documentation that is not there — update the path:\n  "
        + "\n  ".join(f"{f}: {p}" for f, p in missing)
    )


def test_a_migration_cites_a_plan_that_can_still_be_found():
    """A migration's header cannot be edited once applied, so the plan it
    cites must be where the archive rule says: as written, or archived under
    the same folder name."""
    unfound = sorted(
        (f, p)
        for f, p in _migration_cites()
        if not (ROOT / p).exists() and not (ROOT / _archived(p)).exists()
    )
    assert not unfound, (
        "an applied migration cites a plan that is neither where it was nor"
        " archived under the same name — a folder was renamed in the move:\n  "
        + "\n  ".join(f"{f}: {p}" for f, p in unfound)
    )
