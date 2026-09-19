"""The two agent-facing docs are pinned to the CLI and to each other.

`CLAUDE.md` and `AGENTS.md` both tell an agent which commands are destructive.
A name in that list is acted on — an agent reads it and refuses — so a stale
entry is not a cosmetic defect: it points the guard at nothing while the real
surface goes unnamed.

That is not hypothetical. Both documents named `storydump-cli process-queue`
and `storydump-cli create-schedule` in their NEVER-run blocks for a month after
neither command existed, and the credential-destroying `revoke-tokens` and
`rotate-keys` were in neither. The list looked authoritative the whole time.
The legacy CLI is gone (the v2 CLI plan, phase 03); the same guard now pins
`storydump`, whose verbs nest (`storydump tokens revoke`), so a subcommand is
pinned, not its group.

Three properties, because the failures are independent:

1. Every `storydump` invocation either document writes AS CODE (a backtick
   span or a fenced block) names a verb the CLI actually registers, and a
   subcommand its group actually has. Catches the ghosts. Prose that merely
   says "storydump" is not an invocation.
2. The NEVER-run lists agree with each other exactly. That is the "the two
   files must not disagree" requirement made structural rather than a habit.
3. A NEVER-run entry under a group names the subcommand (`tokens revoke`),
   never the group alone — a bare group would forbid its harmless reads too.

**Bound, stated because it is the direction that reads clean.** This pins that
named commands EXIST and that the two lists MATCH. It cannot know whether a
command that is absent from the list ought to be on it — dangerousness is not
derivable from the registry — so it would not catch a new destructive verb
being left off. Adding a verb without classifying it stays a human judgement,
and `AGENTS.md` says so where a reader will meet it.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("CLAUDE.md", "AGENTS.md")
#: The other files an agent is handed as context that carry their own copy of
#: the never-run list. A copy that omits an entry is a guard with a hole in it
#: (both did — `resolve <story> retry` and `webhook register` were missing),
#: so each must name every `storydump` entry of the canonical block.
SATELLITES = (".claude/QUICK_REFERENCE.md", ".claude/PROJECT_CONTEXT.md")
#: Where a never-run list may live. A new file that spells "never run" over
#: `storydump` invocations anywhere else is a copy nobody pins.
_NEVER_RUN_WORDS = re.compile(r"never\s+(?:run|suggest running)", re.I)
_UNPINNED_ROOTS = (".claude", "documentation/operations", "documentation/guides")

#: `storydump [--global options] <verb> [<sub>]` inside code: a verb is Click's
#: convention (lowercase, hyphens); a placeholder (`<story>`) or a comment ends it,
#: and a global option before the verb (`--json`, `--api URL`) is stepped over.
_INVOCATION = re.compile(
    r"(?<![\w-])storydump(?:\s+--?[\w-]+(?:[= ]\S+)?)*\s+([a-z][a-z0-9-]*)"
    r"(?:\s+<[^>\n]+>)?(?:\s+([a-z][a-z0-9-]*))?"
)
_FENCE = re.compile(r"(?:```|~~~)[^\n]*\n(.*?)(?:```|~~~)", re.S)
#: A word after a placeholder (`storydump story <id> shows`) is prose, not a
#: subcommand; a group's subcommand never follows a placeholder.
_AFTER_PLACEHOLDER = re.compile(
    r"storydump\s+([a-z][a-z0-9-]*)\s+<[^>\n]+>\s+([a-z][a-z0-9-]*)"
)
_SPAN = re.compile(r"`([^`\n]+)`")


def _doc(name: str) -> str:
    return (ROOT / name).read_text()


def _code(text: str) -> list[str]:
    """Every fenced block and every backtick span — the places a command is
    written to be typed."""
    fences = _FENCE.findall(text)
    prose = _FENCE.sub("", text)
    return fences + _SPAN.findall(prose)


def _named_invocations(text: str) -> set[tuple[str, str | None]]:
    return {
        (verb, sub or None)
        for code in _code(text)
        for verb, sub in _INVOCATION.findall(code)
    }


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


def _registry() -> dict[str, set[str] | None]:
    """Every verb, and for a group the subcommands it registers."""
    from storydump_cli.main import cli

    return {
        name: set(command.commands) if isinstance(command, click.Group) else None
        for name, command in cli.commands.items()
    }


@pytest.mark.parametrize("doc", DOCS)
def test_every_command_the_doc_names_actually_exists(doc):
    named = _named_invocations(_doc(doc))
    assert named, (
        f"{doc} names no storydump verbs in code — the regex or the doc changed"
    )
    registry = _registry()
    prose = {
        (v, s) for code in _code(_doc(doc)) for v, s in _AFTER_PLACEHOLDER.findall(code)
    }
    ghosts = []
    for verb, sub in sorted(named, key=str):
        if verb not in registry:
            ghosts.append(f"storydump {verb}")
        elif (
            sub
            and registry[verb] is not None
            and sub not in registry[verb]
            and (verb, sub) not in prose
        ):
            ghosts.append(f"storydump {verb} {sub}")
    assert not ghosts, (
        f"{doc} names storydump command(s) that do not exist: {ghosts}."
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


def test_the_never_run_list_is_not_empty_and_covers_the_worker_and_the_cli():
    """Positive control: a parser returning nothing would pass both tests above."""
    block = _never_run_block(_doc("AGENTS.md"))
    assert block, "the NEVER-run block parsed empty"
    assert any("python -m src.main" in line for line in block), (
        "the worker entry point is no longer in the NEVER-run list"
    )
    assert any(line.startswith("storydump ") for line in block), (
        "the CLI's destructive verbs are no longer in the NEVER-run list"
    )
    assert not any("storydump-cli" in line for line in block), (
        "the legacy CLI is gone; a guard pointing at it protects nothing"
    )
    assert any("scripts.migration_runner apply --manual" in line for line in block), (
        "the gated door — the one command that drops the legacy schema — is"
        " not in the NEVER-run list (the tear-out, phase 04; fork F7)"
    )


def test_a_never_run_entry_under_a_group_names_the_subcommand():
    registry = _registry()
    for line in _never_run_block(_doc("AGENTS.md")):
        words = line.split()
        if words[:1] != ["storydump"]:
            continue
        verb = words[1]
        assert verb in registry, line
        if registry[verb] is not None:
            assert len(words) > 2 and words[2] in registry[verb], (
                f"{line!r} forbids a whole group; name the destructive"
                f" subcommand ({', '.join(sorted(registry[verb]))})"
            )


def _canonical_entries() -> set[tuple[str, str | None]]:
    """The canonical block's `storydump` entries as (verb, subcommand)."""
    entries = set()
    for line in _never_run_block(_doc("AGENTS.md")):
        for verb, sub in _INVOCATION.findall(line):
            entries.add((verb, sub or None))
    return entries


_NEVER_BULLETS = re.compile(r"\*\*NEVER[^\n]*\n((?:- .*\n)+)")


def _never_bullets(text: str) -> str:
    """The satellite's own never-run list: the bullet block under its
    `**NEVER …**` line, and nothing else — a command named in its SAFE list
    or its prose must not count as forbidden."""
    m = _NEVER_BULLETS.search(text)
    assert m, "no '**NEVER …**' bullet block found — the satellite's list moved"
    return m.group(1)


def _forbidden_by(text: str) -> set[tuple[str, str | None]]:
    """The commands a satellite forbids: those in its never-run bullets."""
    return _named_invocations(_never_bullets(text))


def test_a_command_named_only_in_a_safe_list_does_not_count():
    """The scan reads the never-run bullets, not the whole page: a satellite
    that lists `webhook register` as SAFE and omits it from NEVER is
    incomplete, and a whole-page scan would wave it through."""
    satellite = (
        "**NEVER run these commands**\n"
        "- `storydump approve <story>`\n"
        "\n"
        "**SAFE commands**\n"
        "- `storydump webhook register` / `storydump health`\n"
    )
    forbidden = _forbidden_by(satellite)
    assert ("approve", None) in forbidden
    assert ("webhook", "register") not in forbidden
    assert ("health", None) not in forbidden


@pytest.mark.parametrize("doc", SATELLITES)
def test_every_satellite_copy_of_the_list_is_complete(doc):
    """`.claude/*` context files repeat the list in their own words; each must
    name every destructive `storydump` command the canonical block names —
    IN its never-run bullets, not anywhere on the page."""
    named = _forbidden_by(_doc(doc))
    missing = sorted(
        f"storydump {verb}" + (f" {sub}" if sub else "")
        for verb, sub in _canonical_entries()
        if (verb, sub) not in named
    )
    assert not missing, (
        f"{doc}'s never-run list omits {missing} — an agent reading only that"
        " file is not told those commands are destructive"
    )


@pytest.mark.parametrize("doc", SATELLITES)
def test_every_satellite_names_only_real_commands(doc):
    registry = _registry()
    ghosts = [
        f"storydump {verb}" + (f" {sub}" if sub else "")
        for verb, sub in sorted(_named_invocations(_doc(doc)), key=str)
        if verb not in registry
        or (sub and registry[verb] is not None and sub not in registry[verb])
    ]
    assert not ghosts, f"{doc} names storydump command(s) that do not exist: {ghosts}"


def test_no_other_file_carries_an_unpinned_never_run_list():
    """A never-run list that this module does not check is a list that will
    drift. Any `.md` under the agent-context and runbook roots that says
    "never run" over `storydump` invocations must be one of DOCS or SATELLITES."""
    pinned = {ROOT / d for d in DOCS + SATELLITES}
    strays = []
    for root in _UNPINNED_ROOTS:
        for path in (ROOT / root).rglob("*.md"):
            if path in pinned or "archive" in path.parts:
                continue
            text = path.read_text()
            if _NEVER_RUN_WORDS.search(text) and _named_invocations(text):
                strays.append(str(path.relative_to(ROOT)))
    assert not strays, (
        f"{strays} carry a never-run list over storydump commands that nothing"
        " pins — add them to SATELLITES or point them at CLAUDE.md"
    )
