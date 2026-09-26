"""The legacy `cli/` package is gone, and so is every live mention of it
(the v2 CLI plan, phase 03, fork F8).

Deleting the package is one commit; the name lingering in a runbook, a
Makefile target, a console script or a ratchet baseline is a pointer to a
command nobody can run. `storydump-cli` may survive only where it is
HISTORY: the CHANGELOG, the archive (the dated updates since the tear-out's
phase 05, and since 2026-09-22 the CLI plan that ordered the deletion) — and
the owner's own Claude Code permission file, which is not the repository's
to edit (queued to the owner).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "storydump-cli"

#: Where the name is history, not a live pointer.
HISTORY = (
    "CHANGELOG.md",
    "documentation/archive/",
    ".claude/settings.json",
    ".claude/settings.local.json",
    "tests/test_legacy_cli_gone.py",
    "tests/test_agent_docs.py",
)


def _tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return [ROOT / p for p in out.decode().split("\0") if p]


def test_the_package_its_tests_and_the_absorbed_script_are_gone():
    assert not (ROOT / "cli").exists(), "cli/ is still here"
    assert not (ROOT / "tests" / "cli").exists(), "tests/cli/ is still here"
    assert not (ROOT / "scripts" / "telegram_webhook.py").exists(), (
        "scripts/telegram_webhook.py was absorbed into `storydump webhook`"
    )


def test_import_cli_does_not_resolve_to_this_checkout():
    """A fresh interpreter finds no `cli` package in this checkout. Another
    checkout installed editable can still put one on `sys.path` (a worktree
    beside the main tree does, until the main tree merges this), so what is
    pinned is the origin: nothing under this repository answers to `cli`."""
    script = (
        "import importlib.util, sys\n"
        "spec = importlib.util.find_spec('cli')\n"
        "print(spec.origin if spec and spec.origin else '')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    origin = proc.stdout.strip()
    assert not origin.startswith(str(ROOT)), (
        f"`import cli` still resolves here: {origin}"
    )
    assert importlib.util.find_spec("storydump_cli") is not None


def test_no_console_script_makefile_target_or_ratchet_entry_names_it():
    assert f"{NAME}=" not in (ROOT / "setup.py").read_text()
    assert NAME not in (ROOT / "Makefile").read_text()
    baseline = json.loads(
        (ROOT / "scripts" / "telegram_ratchet_baseline.json").read_text()
    )
    entries = [e for group in baseline.values() for e in group]
    assert not [e for e in entries if e.startswith("cli/")], (
        "the FC-2 ratchet baseline still lists legacy CLI functions"
    )


def test_the_name_survives_only_in_history():
    offenders = []
    for path in _tracked():
        rel = path.relative_to(ROOT).as_posix()
        if any(rel == h or rel.startswith(h) for h in HISTORY):
            continue
        try:
            text = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            continue
        if NAME.encode() in text:
            offenders.append(rel)
    assert offenders == [], (
        f"{NAME} is still named outside the history files: {offenders}"
    )
