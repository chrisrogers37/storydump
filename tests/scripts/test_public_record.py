"""The public-record check (`scripts/public_record.py`).

The repository is public, so what a tenant is called must not reach it. The
list of those identifiers is private by definition and never lives in the
tree, so these tests drive the gate with a synthetic list on a synthetic
repository. One proves a failure's report never prints the identifier it
found: a CI log on a public repository is public too.
"""

from __future__ import annotations

import json
import subprocess

from scripts.public_record import main

#: Synthetic identifiers. Nothing real is named in this file.
SECRET = "zz-acme-tenant"
OTHER = "Quokka Holdings"


def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(root, files):
    """A committed synthetic repository: {relative path: text}."""
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _env(tmp_path, listing=f"{SECRET}\n{OTHER}\n"):
    """The list in the environment, as CI passes the secret, and a config
    directory of its own so a developer's real list is never read."""
    env = {"STORYDUMP_CONFIG_DIR": str(tmp_path / "cfg")}
    if listing is not None:
        env["PRIVATE_IDENTIFIERS"] = listing
    return env


def test_a_file_naming_an_identifier_fails_with_its_path_and_line(tmp_path, capsys):
    root = _repo(
        tmp_path / "r",
        {"docs/a.md": f"intro\nthe probe used {SECRET.upper()}\n", "ok.md": "ok\n"},
    )
    assert main([], env=_env(tmp_path), root=root) == 1
    findings = capsys.readouterr().out.splitlines()
    assert len(findings) == 1
    assert findings[0].startswith("docs/a.md:2:")
    assert "list line 1" in findings[0]


def test_the_report_never_prints_the_identifier_it_found(tmp_path, capsys):
    root = _repo(tmp_path / "r", {"a.md": f"{SECRET} met {OTHER}\n"})
    assert main([], env=_env(tmp_path), root=root) == 1
    captured = capsys.readouterr()
    printed = (captured.out + captured.err).lower()
    assert SECRET.lower() not in printed
    assert OTHER.lower() not in printed


def test_in_ci_the_report_points_at_no_line(tmp_path, capsys):
    """A public log that names a file and line shows anyone where the word is,
    and so what the list holds."""
    root = _repo(tmp_path / "r", {"docs/a.md": f"intro\n{SECRET}\n"})
    assert main(["--count-only"], env=_env(tmp_path), root=root) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "docs/a.md" not in captured.err
    assert "list line" not in captured.err


def test_a_tree_written_with_placeholders_passes(tmp_path, capsys):
    """The list's comments and blank lines are not identifiers: an empty entry
    would match every line in the tree."""
    root = _repo(tmp_path / "r", {"a.md": "# who we protect\nthe owner's @<handle>\n"})
    listing = f"# who we protect\n\n{SECRET}\n"
    assert main([], env=_env(tmp_path, listing), root=root) == 0
    assert capsys.readouterr().out == ""


def test_without_a_list_it_passes_locally_and_refuses_when_one_is_required(
    tmp_path, capsys
):
    root = _repo(tmp_path / "r", {"a.md": f"{SECRET}\n"})
    env = _env(tmp_path, listing=None)
    assert main([], env=env, root=root) == 0
    assert main(["--require-list"], env=env, root=root) == 2


def test_the_list_file_in_the_config_directory_is_read(tmp_path, capsys):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "private-identifiers.txt").write_text(f"{SECRET}\n", encoding="utf-8")
    root = _repo(tmp_path / "r", {"a.md": f"{SECRET}\n"})
    assert main([], env=_env(tmp_path, listing=None), root=root) == 1


def test_a_commit_message_in_the_range_is_scanned(tmp_path, capsys):
    root = _repo(tmp_path / "r", {"a.md": "ok\n"})
    base = _git(root, "rev-parse", "HEAD")
    (root / "b.md").write_text("ok too\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"probe {SECRET} live")
    sha = _git(root, "rev-parse", "--short=12", "HEAD")
    assert main(["--commits", f"{base}..HEAD"], env=_env(tmp_path), root=root) == 1
    assert f"commit {sha}" in capsys.readouterr().out


def test_a_pull_requests_title_and_body_are_scanned(tmp_path, capsys):
    root = _repo(tmp_path / "r", {"a.md": "ok\n"})
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {"pull_request": {"title": f"fix for {OTHER}", "body": f"one\n{SECRET}\n"}}
        ),
        encoding="utf-8",
    )
    assert main(["--event", str(event)], env=_env(tmp_path), root=root) == 1
    out = capsys.readouterr().out
    assert "pull request title" in out
    assert "pull request body:2:" in out
