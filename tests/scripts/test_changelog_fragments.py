"""`scripts/changelog_fragments.py`: the fragment format, the rule the Changelog
Check applies to a pull request, and the fold into `CHANGELOG.md`.

A PR records its changelog entry as one new file in `changelog.d/` and never
edits `CHANGELOG.md`, so two open PRs never change the same lines. `check
--base` is the CI job's whole rule; `compile` folds the fragments in. The rule
is driven through `pr_problems` with fixtures and through real git, because a
gate verified only by exercise does not outlive its author (`test_pr_ready.py`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import changelog_fragments as cf

REPO = Path(__file__).resolve().parents[2]

CHANGELOG = """\
# Changelog

## [Unreleased]

### Added

- **An added entry.** Detail.

### Fixed

- **A fixed entry.** Detail.

### Added

- **An older group's entry.** Detail.

## [1.0.0] - 2026-01-01

### Added

- **A released entry.** Detail.
"""
RELEASED = CHANGELOG[CHANGELOG.index("## [1.0.0]") :]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("# Fragments\n\nNot one.\n")
    return tmp_path


def fragment(repo: Path, name: str, text: str) -> Path:
    path = repo / "changelog.d" / name
    path.write_text(text)
    return path


def run(repo: Path, *args: str) -> int:
    return cf.main(["--repo", str(repo), *args])


def changelog(repo: Path) -> str:
    return (repo / "CHANGELOG.md").read_text()


# --- compile -----------------------------------------------------------------


def test_an_entry_lands_first_in_its_section_and_its_fragment_is_removed(repo):
    added = fragment(repo, "fix-x.md", "### Fixed\n\n- **A new fix (#1).** Detail.\n")

    assert run(repo, "compile") == 0

    text = changelog(repo)
    assert (
        "### Fixed\n\n- **A new fix (#1).** Detail.\n\n- **A fixed entry.** Detail.\n"
        in text
    )
    assert not added.exists()
    assert (repo / "changelog.d" / "README.md").exists()
    assert text.endswith(RELEASED)


def test_an_entry_goes_to_the_newest_group_never_to_an_older_repeat(repo):
    fragment(repo, "add-y.md", "### Added\n\n- **A new feature.** Detail.\n")

    run(repo, "compile")

    text = changelog(repo)
    assert "### Added\n\n- **A new feature.** Detail.\n\n- **An added entry.**" in text
    assert text.count("A new feature") == 1
    assert "### Added\n\n- **An older group's entry.**" in text


def test_a_section_the_newest_group_lacks_is_created_in_its_place(repo):
    fragment(repo, "a.md", "### Security\n\n- **A security fix.** Detail.\n")
    fragment(repo, "b.md", "### Changed\n\n- **A change.** Detail.\n")

    run(repo, "compile")

    text = changelog(repo)
    newest = text[: text.index("- **An older group's entry.**")]
    headings = [line for line in newest.splitlines() if line.startswith("### ")]
    assert headings == [
        "### Added",
        "### Changed",
        "### Fixed",
        "### Security",
        "### Added",
    ]
    assert "### Changed\n\n- **A change.** Detail.\n\n### Fixed" in text
    assert "### Security\n\n- **A security fix.** Detail.\n\n### Added" in text


def test_an_empty_unreleased_takes_its_sections_in_order(repo):
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n" + RELEASED)
    fragment(
        repo,
        "z.md",
        "### Fixed\n\n- **A fix.** Detail.\n\n### Added\n\n- **A feature.** Detail.\n",
    )

    run(repo, "compile")

    assert changelog(repo) == (
        "# Changelog\n\n## [Unreleased]\n\n"
        "### Added\n\n- **A feature.** Detail.\n\n"
        "### Fixed\n\n- **A fix.** Detail.\n\n" + RELEASED
    )


def test_fragments_fold_in_name_order(repo):
    fragment(repo, "b-second.md", "### Added\n\n- **Second.** Detail.\n")
    fragment(repo, "a-first.md", "### Added\n\n- **First.** Detail.\n")

    run(repo, "compile")

    assert (
        "### Added\n\n- **First.** Detail.\n\n- **Second.** Detail.\n\n"
        "- **An added entry.**" in changelog(repo)
    )


def test_an_entry_is_kept_verbatim(repo):
    body = "- **A long entry.** One line\n  continues.\n\n- **Another.** Detail."
    fragment(repo, "x.md", f"\n### Fixed\n\n{body}\n\n")

    run(repo, "compile")

    assert f"### Fixed\n\n{body}\n\n- **A fixed entry.**" in changelog(repo)


def test_no_fragments_leaves_the_changelog_alone(repo):
    assert run(repo, "compile") == 0
    assert changelog(repo) == CHANGELOG


def test_a_changelog_without_unreleased_is_refused_and_keeps_the_fragments(repo):
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n" + RELEASED)
    kept = fragment(repo, "x.md", "### Fixed\n\n- **x.** y\n")

    assert run(repo, "compile") == 1
    assert kept.exists()
    assert changelog(repo) == "# Changelog\n\n" + RELEASED


# --- the fragment format -----------------------------------------------------

MALFORMED = {
    "text before the first heading": "An entry.\n\n### Fixed\n\n- **x.** y\n",
    "no heading at all": "",
    "a section the changelog does not use": "### Fix\n\n- **x.** y\n",
    "a section with no entry": "### Fixed\n\n### Added\n\n- **x.** y\n",
    "an entry that is not a bullet": "### Fixed\n\nProse.\n",
    "a heading that is not a section": "### Fixed\n\n- **x.** y\n\n## [2.0.0]\n",
    "a subheading inside an entry": "### Fixed\n\n- **x.** y\n\n#### Details\n",
    "a section twice": "### Fixed\n\n- **x.** y\n\n### Fixed\n\n- **z.** w\n",
}


@pytest.mark.parametrize("text", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_malformed_fragment_fails_the_check_and_stops_the_compile_unwritten(
    repo, text, capsys
):
    good = fragment(repo, "a-good.md", "### Added\n\n- **Fine.** Detail.\n")
    bad = fragment(repo, "b-bad.md", text)

    assert run(repo, "check") == 1
    assert "b-bad.md" in capsys.readouterr().out
    assert run(repo, "compile") == 1
    assert changelog(repo) == CHANGELOG
    assert good.exists() and bad.exists()


def test_check_passes_well_formed_fragments_and_writes_nothing(repo):
    kept = fragment(repo, "ok.md", "### Tests\n\n- **A pin (#2).** Detail.\n")

    assert run(repo, "check") == 0
    assert kept.exists()
    assert changelog(repo) == CHANGELOG


def test_an_issue_number_at_a_line_start_is_not_a_heading(repo):
    fragment(repo, "ok.md", "### Fixed\n\n- **A fix.** Follows\n#1410's rule.\n")

    assert run(repo, "check") == 0


# --- the rule a pull request meets -------------------------------------------


def problems(*changes: str) -> list[str]:
    """`pr_problems` over "STATUS path" strings, as `git diff --name-status`."""
    return cf.pr_problems([tuple(change.split(" ", 1)) for change in changes])


def test_a_code_change_with_a_new_fragment_passes():
    assert problems("M src/app.py", "A changelog.d/fix-x.md") == []


def test_a_code_change_without_a_fragment_fails_and_names_the_directory():
    (problem,) = problems("M src/app.py")
    assert "changelog.d/" in problem


#: A docs-only PR edits CHANGELOG.md as often as not, so the edit is refused
#: whatever else the PR changes.
CHANGELOG_EDITS = {
    "alone": ("M CHANGELOG.md",),
    "beside-docs": ("M documentation/guides/x.md", "M CHANGELOG.md"),
    "beside-code-and-a-fragment": (
        "M src/app.py",
        "A changelog.d/fix-x.md",
        "M CHANGELOG.md",
    ),
}


@pytest.mark.parametrize(
    "changes", list(CHANGELOG_EDITS.values()), ids=list(CHANGELOG_EDITS)
)
def test_an_edit_to_changelog_md_fails_unless_it_is_the_compile(changes):
    (problem,) = problems(*changes)
    assert "CHANGELOG.md" in problem and "changelog.d/" in problem


DOCS_ONLY_CHANGES = {
    "documentation": ("M documentation/guides/x.md",),
    "markdown-and-github": ("M README.md", "M .github/workflows/ci.yml"),
    "a-fragment-alone": ("A changelog.d/fix-x.md",),
    "the-compile": ("M CHANGELOG.md", "D changelog.d/a.md", "D changelog.d/b.md"),
}


@pytest.mark.parametrize(
    "changes", list(DOCS_ONLY_CHANGES.values()), ids=list(DOCS_ONLY_CHANGES)
)
def test_a_docs_only_change_needs_no_fragment(changes):
    assert problems(*changes) == []


@pytest.mark.parametrize(
    "fragment_change",
    [
        "M changelog.d/already-on-main.md",
        "D changelog.d/old.md",
        "A changelog.d/README.md",
        "A changelog.d/nested/x.md",
        "A changelog.d/x.txt",
        "A x.md",
    ],
)
def test_only_a_new_fragment_directly_in_changelog_d_counts(fragment_change):
    assert problems("M src/app.py", fragment_change) != []


def git(repo: Path, *args: str) -> None:
    """git in the scratch repository, blind to this machine's config (a signing
    key or a hook would sign or run on every commit)."""
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def test_check_against_a_base_reads_the_pull_requests_own_diff(repo):
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text("x = 1\n")
    commit(repo, "code")

    assert run(repo, "check", "--base", "main") == 1

    # A name git would quote without -z still reads as a fragment.
    fragment(repo, "feature-café.md", "### Added\n\n- **x.** y\n")
    commit(repo, "fragment")
    assert run(repo, "check", "--base", "main") == 0

    # main moving on after the branch point is not this branch's edit: the
    # rule reads base...HEAD, from the merge base.
    git(repo, "checkout", "-q", "main")
    (repo / "CHANGELOG.md").write_text(CHANGELOG + "\n")
    commit(repo, "main moves")
    git(repo, "checkout", "-q", "feature")
    assert run(repo, "check", "--base", "main") == 0


def test_check_fails_when_git_cannot_read_the_diff(repo, capsys):
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "base")

    assert run(repo, "check", "--base", "no-such-ref") == 1
    assert "no-such-ref" in capsys.readouterr().out


# --- the repository's own files ----------------------------------------------


def test_the_repositorys_fragments_are_well_formed():
    assert cf.check(REPO) == []


def test_the_repositorys_changelog_takes_a_fold_in_every_section(tmp_path):
    """The real `CHANGELOG.md` and `changelog.d/`, plus a probe fragment in
    every section: each probe entry lands under the first heading of its
    section in [Unreleased], and nothing below [Unreleased] moves."""
    shutil.copytree(REPO / "changelog.d", tmp_path / "changelog.d")
    shutil.copy(REPO / "CHANGELOG.md", tmp_path / "CHANGELOG.md")
    probe = "".join(f"### {s}\n\n- **Probe {s}.** Detail.\n\n" for s in cf.SECTIONS)
    # "0" sorts before every name the README allows, so the probe folds first.
    fragment(tmp_path, "0000-probe.md", probe)
    before = changelog(tmp_path)

    assert run(tmp_path, "compile") == 0

    after = changelog(tmp_path)
    unreleased = after.index("## [Unreleased]\n")
    for section in cf.SECTIONS:
        first = after.index(f"### {section}\n", unreleased)
        assert after.startswith(f"### {section}\n\n- **Probe {section}.**", first)
    released = before.index("\n## [", before.index("## [Unreleased]\n"))
    assert after.endswith(before[released:])
    assert cf.fragment_paths(tmp_path / "changelog.d") == []
