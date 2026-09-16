"""``storydump --help`` groups the verbs — auth, reads, writes, environment —
and every verb's own help carries one example, so the help is the
authoritative list `AGENTS.md` says it is."""

from __future__ import annotations

import re

import click
import pytest

from src.services.target.vocabulary import EXIT_OK
from storydump_cli.main import SECTIONS, cli
from tests.storydump_cli.test_main import Api, run, runtime

SECTION_TITLES = ("Auth", "Reads", "Writes", "Environment")


def _verbs() -> list[tuple[str, click.Command]]:
    return sorted(cli.commands.items())


def test_the_root_help_lists_every_verb_once_under_its_section(tmp_path):
    result = run(runtime(tmp_path, Api({})), "--help")
    assert result.exit_code == EXIT_OK, result.output
    text = result.stdout
    positions = [text.index(f"{title}:") for title in SECTION_TITLES]
    assert positions == sorted(positions), (
        "auth, reads, writes, environment — in that order"
    )
    # the prose above the sections may wrap a line onto a verb's name; the
    # rows are counted inside the sections
    sections = text[positions[0] :]
    for name, _ in _verbs():
        rows = sections.count(f"\n  {name} ") + sections.count(f"\n  {name}\n")
        assert rows == 1, (name, sections)
    assert "Exit codes" in text


def test_every_verb_is_in_exactly_one_section():
    sectioned = [name for _, commands in SECTIONS for name in commands]
    assert sorted(sectioned) == sorted(cli.commands), (
        "a verb outside every section, or in two"
    )
    assert [title for title, _ in SECTIONS] == list(SECTION_TITLES)


@pytest.mark.parametrize("name", [name for name, _ in _verbs()])
def test_every_verbs_help_carries_an_example(tmp_path, name):
    result = run(runtime(tmp_path, Api({})), name, "--help")
    assert result.exit_code == EXIT_OK, result.output
    # Click's own `Usage: storydump <verb>` line does not count: an example
    # is an indented invocation under an Example(s) heading
    assert "Example" in result.stdout, result.stdout
    assert re.search(rf"^\s{{2,}}storydump {name}\b", result.stdout, re.M), (
        result.stdout
    )


@pytest.mark.parametrize(
    "group,subcommands",
    [("tokens", {"list", "revoke"}), ("webhook", {"status", "register", "deregister"})],
)
def test_groups_list_their_subcommands(tmp_path, group, subcommands):
    command = cli.commands[group]
    assert isinstance(command, click.Group)
    assert set(command.commands) == subcommands
    for sub in subcommands:
        result = run(runtime(tmp_path, Api({})), group, sub, "--help")
        assert result.exit_code == EXIT_OK, result.output
        assert re.search(rf"^\s{{2,}}storydump {group} {sub}\b", result.stdout, re.M)
