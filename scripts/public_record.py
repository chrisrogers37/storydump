"""The public-record check: no tenant's identifier in what this repository publishes.

The repository is public, so every tracked file, commit message and pull
request is world-readable, cached and indexed, and a later edit does not
recall it. A tenant's identifiers (a workspace's name, an account's handle, an
id, an email) must not appear in any of them (`AGENTS.md` › The record).

The list of those identifiers is private by definition, so it never lives in
the tree. CI reads it from the repository secret `PRIVATE_IDENTIFIERS`; a local
run reads `private-identifiers.txt` in the CLI's config directory
(`$STORYDUMP_CONFIG_DIR`, else `~/.config/storydump`). One identifier per line,
matched case-insensitively; blank lines and `#` comments are not identifiers.
Set the secret from the file (`gh secret set PRIVATE_IDENTIFIERS <
~/.config/storydump/private-identifiers.txt`) so a finding's list line means
the same line in both.

A finding names where it is and the list line it matched, never the
identifier. CI passes `--count-only`: its log is public, and a file and line
would show anyone the word, and so what the list holds.

Standalone stdlib module by the repository's rule for gates, so it runs in CI
without installing the application.

Exit: 0 clean, or no list configured · 1 a finding · 2 no list under
`--require-list`, which CI passes so a missing secret cannot read as clean.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

LIST_ENV = "PRIVATE_IDENTIFIERS"
CONFIG_DIR_ENV = "STORYDUMP_CONFIG_DIR"
LIST_FILE = "private-identifiers.txt"
ROOT = Path(__file__).resolve().parents[1]


def _list_path(env) -> Path:
    override = env.get(CONFIG_DIR_ENV)
    base = (
        Path(override).expanduser()
        if override
        else Path.home() / ".config" / "storydump"
    )
    return base / LIST_FILE


def load_list(env) -> list[tuple[int, str]] | None:
    """The identifiers as (list line, lowered text), or None when no list is configured."""
    text = env.get(LIST_ENV)
    if not text:
        path = _list_path(env)
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
    entries = []
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.strip()
        if entry and not entry.startswith("#"):
            entries.append((number, entry.lower()))
    return entries


def scan(where: str, text: str, entries) -> list[str]:
    """One finding per line that names an identifier. The identifier is not in it."""
    findings = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for list_line, entry in entries:
            if entry in lowered:
                findings.append(
                    f"{where}:{line_number}: private identifier (list line {list_line})"
                )
    return findings


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _files(root: Path) -> list[str]:
    """Tracked files, and new ones git does not ignore (a local run before a commit)."""
    out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [rel for rel in out.split("\0") if rel]


def _commits(root: Path, rev_range: str) -> list[tuple[str, str]]:
    out = _git(root, "log", "--format=%H%x00%B%x1e", rev_range)
    commits = []
    for record in out.split("\x1e"):
        sha, _, message = record.strip("\n").partition("\0")
        if sha:
            commits.append((sha, message))
    return commits


def main(argv=None, env=None, root=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--require-list",
        action="store_true",
        help="fail when no list is configured (CI, where it is a secret)",
    )
    parser.add_argument(
        "--commits",
        metavar="RANGE",
        help="also scan the commit messages in a git range, e.g. origin/main..HEAD",
    )
    parser.add_argument(
        "--event",
        metavar="PATH",
        help="also scan a pull request's title and body from a GitHub event file",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="report how many lines match, never where (CI: the log is public)",
    )
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    root = ROOT if root is None else Path(root)

    entries = load_list(env)
    if entries is None:
        print(
            f"public record: not checked: no ${LIST_ENV} and no {_list_path(env)}",
            file=sys.stderr,
        )
        return 2 if args.require_list else 0

    findings = []
    for rel in _files(root):
        path = root / rel
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            findings += scan(rel, text, entries)
    if args.commits:
        for sha, message in _commits(root, args.commits):
            findings += scan(f"commit {sha[:12]}", message, entries)
    if args.event:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        pull_request = event.get("pull_request") or {}
        findings += scan("pull request title", pull_request.get("title") or "", entries)
        findings += scan("pull request body", pull_request.get("body") or "", entries)

    if not findings:
        return 0
    if args.count_only:
        print(
            f"public record: {len(findings)} line(s) name a private identifier."
            " This log is public, so it says neither where nor which: run"
            " scripts/lint.sh with your copy of the list to see them.",
            file=sys.stderr,
        )
        return 1
    for finding in findings:
        print(finding)
    print(
        f"public record: {len(findings)} line(s) name a private identifier."
        " Write a placeholder instead (AGENTS.md › The record); the"
        " list line says which identifier, and this output never does.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
