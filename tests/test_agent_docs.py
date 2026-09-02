"""The two agent-facing docs are pinned to the CLI and to each other.

`CLAUDE.md` and `AGENTS.md` both tell an agent which commands are destructive.
A name in that list is acted on — an agent reads it and refuses — so a stale
entry is not a cosmetic defect: it points the guard at nothing while the real
surface goes unnamed.

That is not hypothetical. Both documents named `storydump-cli process-queue`
and `storydump-cli create-schedule` in their NEVER-run blocks for a month after
neither command existed, and the credential-destroying `revoke-tokens` and
`rotate-keys` were in neither. The list looked authoritative the whole time.

Two properties, because the two failures are independent:

1. Every `storydump-cli` command either document names is a command the CLI
   actually registers. Catches the ghosts.
2. The NEVER-run lists agree with each other exactly. That is the "the two
   files must not disagree" requirement made structural rather than a habit —
   the overlap between them is small and deliberate, and this is what keeps it
   honest when someone edits one and not the other.

**Bound, stated because it is the direction that reads clean.** This pins that
named commands EXIST and that the two lists MATCH. It cannot know whether a
command that is absent from the list ought to be on it — dangerousness is not
derivable from the registry — so it would not have caught `revoke-tokens` being
missing. Adding a command to the CLI without classifying it stays a human
judgement, and `AGENTS.md` says so where a reader will meet it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("CLAUDE.md", "AGENTS.md")

#: `storydump-cli <name>`, where a name is the click convention (lowercase,
#: hyphens). Trailing flags and comments are not part of the name.
_INVOCATION = re.compile(r"\bstorydump-cli\s+([a-z][a-z0-9-]*)")


def _doc(name: str) -> str:
    return (ROOT / name).read_text()


def _named_commands(text: str) -> set[str]:
    return set(_INVOCATION.findall(text))


def _never_run_block(text: str) -> list[str]:
    """The fenced block under the NEVER-run heading, as command lines.

    Anchored on the heading rather than "the first fence in the file", so
    reordering the document does not silently change what is compared.
    """
    m = re.search(r"### NEVER run these\s*\n+```bash\n(.*?)```", text, re.S)
    assert m, "no '### NEVER run these' bash block found — the heading moved"
    lines = []
    for raw in m.group(1).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _registry() -> set[str]:
    from cli.main import cli

    return set(cli.commands.keys())


@pytest.mark.parametrize("doc", DOCS)
def test_every_command_the_doc_names_actually_exists(doc):
    named = _named_commands(_doc(doc))
    assert named, (
        f"{doc} names no storydump-cli commands — the regex or the doc changed"
    )
    missing = sorted(named - _registry())
    assert not missing, (
        f"{doc} names storydump-cli command(s) that do not exist: {missing}."
        " A safety list pointing at a command nobody can run protects nothing"
        " — remove it, or register the command."
    )


def test_the_two_never_run_lists_are_identical():
    claude, agents = (_never_run_block(_doc(d)) for d in DOCS)
    assert claude == agents, (
        "CLAUDE.md and AGENTS.md disagree about what must never be run.\n"
        f"  CLAUDE.md: {claude}\n"
        f"  AGENTS.md: {agents}"
    )


def test_the_never_run_list_is_not_empty_and_covers_the_worker():
    """Positive control: a parser returning nothing would pass both tests above."""
    block = _never_run_block(_doc("AGENTS.md"))
    assert block, "the NEVER-run block parsed empty"
    assert any("python -m src.main" in line for line in block), (
        "the worker entry point is no longer in the NEVER-run list"
    )
