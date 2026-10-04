"""Changelog fragments: one file per change in `changelog.d/`, folded into
`CHANGELOG.md` at release time.

Each pull request records its changelog entry as a new file in `changelog.d/`
and never edits `CHANGELOG.md`, so no two open pull requests change the same
lines. The format is in `changelog.d/README.md`; this module is its one parser.

- `check` validates every fragment. With `--base REF` it also applies the rule
  the Changelog Check job holds a pull request to (`pr_problems`): a change
  outside the docs needs a new fragment and must not edit `CHANGELOG.md`.
- `compile` folds every fragment into `CHANGELOG.md` under `## [Unreleased]`,
  each entry first in its section, and deletes the fragments.

Standalone stdlib module by the repository's rule for gates, so it is
unit-testable and runs in CI without installing the application.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

FRAGMENTS = "changelog.d"
UNRELEASED = "## [Unreleased]"

#: The headings a fragment may use, in the order a release lists them: Keep a
#: Changelog's six, then the two this changelog uses for changes with no
#: user-facing behaviour.
SECTIONS = (
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
    "Documentation",
    "Tests",
)

#: A pull request that changes only these needs no entry: the changelog tracks
#: behaviour, not docs. `CHANGELOG.md` is one of them, so the compile and a
#: correction to an existing entry land as docs-only pull requests.
DOCS_ONLY = re.compile(r"^(documentation/|.*\.md$|\.github/)")

HEADING = re.compile(r"^#{1,6}\s")


class ChangelogError(Exception):
    """A refusal: a malformed fragment, a changelog with nowhere to fold, or a
    diff git could not produce."""


def fragment_paths(directory: Path) -> list[Path]:
    """Every fragment in `directory`, in name order: each `*.md` but the README."""
    return sorted(p for p in directory.glob("*.md") if p.name != "README.md")


def parse_fragment(path: Path) -> dict[str, str]:
    """A fragment's entries by section: each `### <Section>` heading's body,
    verbatim, without the blank lines around it."""
    bodies: dict[str, list[str]] = {}
    section = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if HEADING.match(line):
            name = line[4:].strip() if line.startswith("### ") else None
            if name not in SECTIONS:
                raise ChangelogError(
                    f"{path.name}:{number}: {line.strip()!r} is not a section; a "
                    f"fragment's only headings are '### <Section>', one of "
                    f"{', '.join(SECTIONS)}"
                )
            if name in bodies:
                raise ChangelogError(
                    f"{path.name}:{number}: '### {name}' appears twice"
                )
            section = name
            bodies[section] = []
        elif section is not None:
            bodies[section].append(line)
        elif line.strip():
            raise ChangelogError(
                f"{path.name}:{number}: text before the first '### <Section>' heading"
            )
    if not bodies:
        raise ChangelogError(f"{path.name}: no '### <Section>' heading")
    entries = {}
    for name, lines in bodies.items():
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines or not lines[0].startswith("- "):
            raise ChangelogError(
                f"{path.name}: '### {name}' must hold an entry starting with '- '"
            )
        entries[name] = "\n".join(lines)
    return entries


def _insert(lines: list[str], section: str, entries: list[str]) -> None:
    """Put `entries` first under `section` in the newest group of [Unreleased].

    [Unreleased] can repeat its headings, newest group first; a group runs while
    its headings follow `SECTIONS` order. When the newest group lacks `section`,
    the heading is created where that order puts it.
    """
    start = lines.index(UNRELEASED)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    rank = SECTIONS.index(section)
    previous = -1
    for i in range(start + 1, end):
        name = lines[i][4:].strip() if lines[i].startswith("### ") else None
        if name not in SECTIONS:
            continue
        here = SECTIONS.index(name)
        if here == rank:
            gap = [""] if i + 1 < len(lines) and lines[i + 1].strip() else []
            lines[i + 1 : i + 1] = ["", *entries, *gap]
            return
        if here > rank or here <= previous:
            end = i
            break
        previous = here
    gap = [""] if lines[end - 1].strip() else []
    lines[end:end] = [*gap, f"### {section}", "", *entries, ""]


def fold(changelog: str, fragments: list[dict[str, str]]) -> str:
    """`changelog` with every fragment's entries folded in, in fragment order."""
    lines = changelog.split("\n")
    if UNRELEASED not in lines:
        raise ChangelogError(f"CHANGELOG.md has no '{UNRELEASED}' heading to fold into")
    for section in SECTIONS:
        bodies = [fragment[section] for fragment in fragments if section in fragment]
        if bodies:
            _insert(lines, section, "\n\n".join(bodies).split("\n"))
    return "\n".join(lines)


def compile_fragments(repo: Path) -> list[Path]:
    """Fold every fragment into `CHANGELOG.md` and delete it; the paths folded.

    Every fragment parses before anything is written, and the changelog is
    replaced whole before any fragment is deleted.
    """
    paths = fragment_paths(repo / FRAGMENTS)
    if not paths:
        return []
    fragments = [parse_fragment(path) for path in paths]
    changelog = repo / "CHANGELOG.md"
    folded = fold(changelog.read_text(encoding="utf-8"), fragments)
    staged = changelog.with_name("CHANGELOG.md.tmp")
    staged.write_text(folded, encoding="utf-8")
    os.replace(staged, changelog)
    for path in paths:
        path.unlink()
    return paths


def is_fragment(path: str) -> bool:
    """Whether a repository path is where a fragment lives."""
    p = PurePosixPath(path)
    return (
        p.parent == PurePosixPath(FRAGMENTS)
        and p.suffix == ".md"
        and p.name != "README.md"
    )


def pr_problems(changes: list[tuple[str, str]]) -> list[str]:
    """What fails a pull request whose `changes` are `git diff --name-status`
    pairs (status letter, path): nothing for a pass."""
    if all(DOCS_ONLY.match(path) for _, path in changes):
        return []
    problems = []
    if any(path == "CHANGELOG.md" for _, path in changes):
        problems.append(
            "This PR edits CHANGELOG.md. Put its entry in a changelog.d/ fragment "
            "instead (changelog.d/README.md): CHANGELOG.md changes only when the "
            "fragments are compiled, and a correction to an existing entry goes "
            "in a docs-only PR."
        )
    if not any(status == "A" and is_fragment(path) for status, path in changes):
        problems.append(
            "No changelog fragment: add changelog.d/<branch-or-PR>.md holding the "
            "entry under its '### <Section>' heading (changelog.d/README.md)."
        )
    return problems


def changes_since(repo: Path, base: str) -> list[tuple[str, str]]:
    """The pull request's own changes: `base...HEAD`, from the merge base."""
    diff = subprocess.run(
        ["git", "diff", "--name-status", "--no-renames", f"{base}...HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if diff.returncode != 0:
        raise ChangelogError(f"git diff {base}...HEAD failed: {diff.stderr.strip()}")
    return [tuple(line.split("\t", 1)) for line in diff.stdout.splitlines() if line]


def check(repo: Path, base: str | None = None) -> list[str]:
    """Every problem with the fragments, and with `base` the pull request's."""
    problems = []
    for path in fragment_paths(repo / FRAGMENTS):
        try:
            parse_fragment(path)
        except ChangelogError as exc:
            problems.append(str(exc))
    if base is not None:
        problems += pr_problems(changes_since(repo, base))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Changelog fragments (changelog.d/)")
    parser.add_argument("--repo", type=Path, default=Path("."), help="repository root")
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser(
        "check", help="validate every fragment; with --base, also the PR's rule"
    )
    check_parser.add_argument("--base", help="the ref the PR merges into")
    commands.add_parser(
        "compile", help="fold every fragment into CHANGELOG.md and delete it"
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "compile":
            folded = compile_fragments(args.repo)
            print(
                f"Folded into CHANGELOG.md: {', '.join(p.name for p in folded) or 'none'}"
            )
            return 0
        problems = check(args.repo, args.base)
    except ChangelogError as exc:
        problems = [str(exc)]
    for problem in problems:
        print(f"error: {problem}")
    if not problems:
        print("Changelog fragments: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
