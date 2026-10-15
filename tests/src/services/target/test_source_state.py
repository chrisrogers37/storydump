"""Application SQL site guard for the archive fence's single writer."""

import ast
import re
from dataclasses import replace
from pathlib import Path

import pytest

from src.services.target.source_state import ArchiveState


UPDATE_EPOCH = re.compile(
    r"\bUPDATE\s+media_sources\b[^;]*\bsource_use_epoch\s*=", re.I
)


def epoch_writers(root):
    writers = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and UPDATE_EPOCH.search(value.value)
                    for value in ast.walk(node)
                ):
                    writers.add((path.relative_to(root).as_posix(), node.name))
    return writers


def test_only_the_archive_transition_writer_updates_source_epoch():
    source = Path(__file__).resolve().parents[4] / "src"
    assert epoch_writers(source) == {
        ("services/target/source_state.py", "apply_archive_transition")
    }


def test_epoch_guard_catches_an_unapproved_sql_site(tmp_path):
    (tmp_path / "rogue.py").write_text(
        "async def callback(conn):\n"
        '    await conn.execute("UPDATE media_sources SET source_use_epoch = source_use_epoch + 1")\n'
    )
    assert epoch_writers(tmp_path) == {("rogue.py", "callback")}


READABLE = ArchiveState(
    enabled=True,
    removed=False,
    generation_id="generation",
    credential_state="active",
    assigned=True,
)


def test_a_source_is_readable_when_on_kept_authorized_by_a_live_credential_and_assigned():
    assert READABLE.readable is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", False),
        ("removed", True),
        ("generation_id", None),
        ("credential_state", None),
        ("credential_state", "expired"),
        ("credential_state", "revoked"),
        ("assigned", False),
    ],
)
def test_losing_any_one_condition_makes_a_source_unreadable(field, value):
    assert replace(READABLE, **{field: value}).readable is False


def test_services_use_one_direct_audit_writer_with_documented_api_exception():
    source = Path(__file__).resolve().parents[4] / "src"
    pattern = re.compile(r"\bINSERT\s+INTO\s+audit_events\b", re.I)
    sites = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*.py")
        if any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and pattern.search(node.value)
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }
    assert sites == {"services/target/audit.py", "api/routes/v1.py"}
