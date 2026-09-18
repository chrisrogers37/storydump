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


# ---------------------------------------------------------------------------
# The legacy tier's names (the legacy tear-out, phase 05; #1216).
#
# The tier is gone: its code (phase 01), its settings (phase 02), its schema
# (the window of phase 04). A LIVE page that still names one of its tables,
# modules or variables points an agent or an operator at something that is not
# there — `.claude/rules/database.md` once described sixteen tables the code
# could no longer reach. Each list below has ONE home: the tables are the
# inventory literal minus the names the TARGET reuses, derived from the
# target's own metadata; the variables are phase 02's dead list; the paths are
# phase 01's deleted packages and files, which its own test asserts are gone
# (`tests/src/test_legacy_tier_gone.py`).
# History keeps its names: `documentation/archive/`, `documentation/planning/`
# and `CHANGELOG.md` are outside the live roots.
# ---------------------------------------------------------------------------

#: Where a page is LIVE — read as a description of the system that exists.
LIVE_ROOTS = (
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
    ".claude",
    "documentation/operations",
    "documentation/guides",
)

#: Live too, but only the directory's OWN pages: `documentation/README.md`,
#: `ROADMAP.md` — below them are the archive and the plans, which are history.
LIVE_FLAT_ROOTS = ("documentation",)

#: A live page ABOUT the legacy lineage that is still in the tree — the
#: migration files 001-050, the lane that replays them, the snapshots 078 took
#: — may name one of its tables. Each entry is a claim with its reason AND the
#: number of times the page names it: an exemption is read for the mentions
#: that were there, so one more (a stale line about the worker's token hiding
#: beside the pager's) or one fewer fails, and the page is read again.
LEGACY_NAME_EXEMPT: dict[str, dict[str, tuple[int, str]]] = {
    "documentation/operations/posting-monitor.md": {
        "TELEGRAM_BOT_TOKEN": (
            3,
            "the environment of `tg-post.sh`, the fleet host's pager script"
            " OUTSIDE this repository (the monitor knows it only as its"
            " --notify-command); the page says whose variable it is",
        ),
    },
    "documentation/guides/landing-vercel-deployment.md": {
        "TELEGRAM_BOT_TOKEN": (
            2,
            "the LANDING app's own server variable on Vercel"
            " (landing/src/lib/telegram.ts:1) — not the worker's or the API's",
        ),
        "ADMIN_TELEGRAM_CHAT_ID": (
            2,
            "the landing app's waitlist-notification chat"
            " (landing/src/lib/telegram.ts:2)",
        ),
    },
}


def _target_table_names() -> set[str]:
    import src.models.target  # noqa: F401 — importing registers every model
    from src.models.target.base import TargetBase

    return {table.name for table in TargetBase.metadata.tables.values()}


def _legacy_only_tables() -> tuple[str, ...]:
    from tests.scripts.legacy_inventory import LEGACY_TABLES

    reused = _target_table_names()
    return tuple(t for t in LEGACY_TABLES if t not in reused)


def _deleted_paths() -> tuple[str, ...]:
    """Every package and module the tear-out deleted: phase 01's own lists,
    each asserted gone where they live. A module is matched without its
    suffix, so `src/config/database` catches the dotted-path spelling and the
    `.py` one alike."""
    from tests.src.test_legacy_tier_gone import DELETED_FILES, DELETED_PACKAGES

    return DELETED_PACKAGES + tuple(f.removesuffix(".py") for f in DELETED_FILES)


def _retired_variables() -> tuple[str, ...]:
    from tests.src.test_legacy_settings_gone import DEAD_VARIABLES

    return DEAD_VARIABLES


def _pattern(name: str) -> re.Pattern[str]:
    """How one legacy name is recognised on a page. A table is a whole word in
    any case (`POSTING_HISTORY` in a SQL example is the same table); a deleted
    path in either spelling (`src/services/core`, `src.services.core`); a
    dead variable a whole word as written — NOT case-folded, because
    `workspaces.dry_run_mode` is a live target column and `DRY_RUN_MODE` a dead
    variable. A snapshot's name (`archive.posting_history_pre_cutover_20260917`)
    is not its table's: `_` is a word character, so no boundary falls there."""
    if name in _legacy_only_tables():
        return re.compile(rf"\b{name}\b", re.IGNORECASE)
    if name in _deleted_paths():
        # no letter, digit, `_` or `-` may follow: `src/services/domain` is not
        # a live `src/services/domain_events.py`
        return re.compile(re.escape(name).replace("/", "[/.]") + r"(?![\w-])")
    return re.compile(rf"\b{name}\b")


def _legacy_names_in(text: str) -> set[str]:
    """Every legacy-only table, deleted path and dead variable `text` names."""
    names = _legacy_only_tables() + _deleted_paths() + _retired_variables()
    return {name for name in names if _pattern(name).search(text)}


def _live_pages() -> list[Path]:
    pages: list[Path] = []
    for root in LIVE_ROOTS:
        path = ROOT / root
        pages += [path] if path.is_file() else sorted(path.rglob("*.md"))
    for root in LIVE_FLAT_ROOTS:
        pages += sorted((ROOT / root).glob("*.md"))
    # RELATIVE parts: an ancestor of the checkout named `archive` must not
    # empty the list and let the pin pass over nothing
    return [p for p in pages if "archive" not in p.relative_to(ROOT).parts]


def test_the_live_roots_cover_the_pages_an_agent_and_an_operator_read():
    """The pin is only as wide as its roots: a root dropped from LIVE_ROOTS
    turns every page under it into one nobody checks."""
    pages = {str(p.relative_to(ROOT)) for p in _live_pages()}
    assert {
        "CLAUDE.md",
        "AGENTS.md",
        "README.md",
        ".claude/rules/database.md",
        ".claude/PROJECT_CONTEXT.md",
        "documentation/operations/worker-recovery.md",
        "documentation/guides/deployment.md",
        "documentation/README.md",
        "documentation/ROADMAP.md",
    } <= pages
    assert not [p for p in pages if p.startswith("documentation/planning")], (
        "the plans are history: only the directory's own pages are live"
    )


def test_an_archive_under_a_live_root_is_history_and_an_ancestor_named_archive_is_not(
    tmp_path, monkeypatch
):
    """`_live_pages` leaves out an `archive/` directory UNDER a live root (none
    exists today; the day one does, its pages are history) — and reads the
    checkout's own path relatively, so a checkout that lives under a directory
    named `archive` still has live pages."""
    root = tmp_path / "archive" / "repo"
    operations = root / "documentation" / "operations"
    (operations / "archive").mkdir(parents=True)
    (root / "documentation" / "guides").mkdir()
    (root / ".claude").mkdir()
    for name in ("CLAUDE.md", "AGENTS.md", "README.md"):
        (root / name).write_text("x")
    (operations / "live.md").write_text("x")
    (operations / "archive" / "old.md").write_text("`posting_queue`")
    monkeypatch.setattr("tests.test_agent_docs.ROOT", root)
    pages = {str(p.relative_to(root)) for p in _live_pages()}
    assert "documentation/operations/live.md" in pages
    assert "documentation/operations/archive/old.md" not in pages


def test_no_live_page_names_the_legacy_tier():
    offenders = {}
    for page in _live_pages():
        rel = str(page.relative_to(ROOT))
        found = _legacy_names_in(page.read_text()) - set(
            LEGACY_NAME_EXEMPT.get(rel, {})
        )
        if found:
            offenders[rel] = sorted(found)
    assert not offenders, (
        "live pages still name the legacy tier — rewrite onto the target, move the"
        " page to documentation/archive/, or add a reasoned LEGACY_NAME_EXEMPT"
        f" entry: {offenders}"
    )


def _exemption_errors(exempt: dict, root: Path) -> dict[str, str]:
    """What is wrong with a set of exemptions, read against the pages under
    `root`: a name that is no legacy name (nothing to exempt), a count below
    one, a page that names the thing MORE or FEWER times than the exemption
    was read for — or not at all, or is gone."""
    known = set(_legacy_only_tables() + _deleted_paths() + _retired_variables())
    wrong = {}
    for rel, names in exempt.items():
        page = root / rel
        text = page.read_text() if page.exists() else ""
        for name, (count, _reason) in names.items():
            if name not in known:
                wrong[f"{rel}: {name}"] = "not a legacy name — nothing to exempt"
            elif count < 1:
                wrong[f"{rel}: {name}"] = "an exemption is for at least one mention"
            elif (found := len(_pattern(name).findall(text))) != count:
                wrong[f"{rel}: {name}"] = (
                    f"exempted for {count} mention(s), found {found}"
                )
    return wrong


def test_every_legacy_name_exemption_is_exact():
    wrong = _exemption_errors(LEGACY_NAME_EXEMPT, ROOT)
    assert not wrong, f"exemptions to re-read (or remove): {wrong}"


def test_an_exemption_is_read_again_when_its_page_or_its_claim_changes(tmp_path):
    """The counted exemption in every direction, on a page of its own."""
    (tmp_path / "p.md").write_text(
        "`TELEGRAM_BOT_TOKEN` is the pager's; so is `TELEGRAM_BOT_TOKEN` here."
    )

    def errors(name, count, page="p.md"):
        return _exemption_errors({page: {name: (count, "a reason")}}, tmp_path)

    assert not errors("TELEGRAM_BOT_TOKEN", 2)
    assert errors("TELEGRAM_BOT_TOKEN", 1), "one MORE mention than it was read for"
    assert errors("TELEGRAM_BOT_TOKEN", 3), "one FEWER"
    assert errors("TELEGRAM_BOT_TOKEN", 0), "an exemption for no mention at all"
    assert errors("NOT_A_LEGACY_NAME", 1), "a name in no list exempts nothing"
    assert errors("TELEGRAM_BOT_TOKEN", 2, page="gone.md"), "the page is gone"


def test_the_names_both_tiers_use_are_not_legacy_names():
    """`media_items`, `users` and two more are TARGET tables too; the pin
    derives them from the target's metadata rather than listing them, and this
    is the derivation's value today — a fifth means the target grew a table
    with a legacy name, which the docs may then name freely."""
    from tests.scripts.legacy_inventory import LEGACY_TABLES

    assert set(LEGACY_TABLES) & _target_table_names() == {
        "category_post_case_mix",
        "media_items",
        "onboarding_sessions",
        "users",
    }


def test_the_pin_sees_a_planted_legacy_name(tmp_path):
    """Positive control: the predicate finds each kind of name in the spellings
    a page would use, and passes over a target name, a target COLUMN that
    shares a dead variable's letters, and a snapshot's name."""
    page = tmp_path / "page.md"
    page.write_text(
        "Rows wait in `posting_queue`; the reader is `src/repositories/queue.py`;"
        " set `WORKER_IMPL=target` first.\n"
    )
    assert _legacy_names_in(page.read_text()) == {
        "posting_queue",
        "src/repositories",
        "WORKER_IMPL",
    }
    # a SQL example in capitals, and a module named the way Python imports it
    assert _legacy_names_in("SELECT * FROM POSTING_HISTORY;") == {"posting_history"}
    assert _legacy_names_in("`from src.services.core import x`") == {
        "src/services/core"
    }
    assert _legacy_names_in("the module `src.config.database`") == {
        "src/config/database"
    }
    assert not _legacy_names_in(
        "a live module may share a deleted package's letters:"
        " `src/services/domain_events.py`, `src.repositories_v2`."
    )
    assert not _legacy_names_in(
        "`media_items` and `users` are target tables;"
        " `workspaces.dry_run_mode` is a live column;"
        " `archive.posting_history_pre_cutover_20260917` is a snapshot;"
        " `TARGET_TELEGRAM_BOT_TOKEN` is the target's own variable."
    )
