"""The reference check reads prose without interpreting runtime numeric values."""

from pathlib import Path

import pytest

from scripts.check_schema_references import check_repository, check_source


@pytest.mark.parametrize(
    ("filename", "source", "numbers"),
    [
        ("sample.py", '# migration 059\nvalue = "migration 101"\n', ["059"]),
        (
            "sample.py",
            '"""The 082 door."""\nclass Example:\n    """Migration 101."""\n',
            ["082", "101"],
        ),
        (
            "sample.sql",
            "SELECT 'migration 101'; -- migration 059\n"
            "CREATE FUNCTION example() RETURNS void AS $body$\n"
            "BEGIN /* migration 134 /* nested */ */ NULL; END $body$ LANGUAGE plpgsql;",
            ["059", "134"],
        ),
        (
            "sample.ts",
            'const url = "https://example.invalid/migration_101";\n'
            "const text = `migration 059`;\n/** migration 101 */\n",
            ["101"],
        ),
        (
            "sample.tsx",
            r"const url = /https?:\/\//; /* migration 101 */"
            + "\n"
            + "const text = `ignore migration 059 ${ /* migration 134 */ value }`;",
            ["101", "134"],
        ),
        (
            "guide.md",
            "Migration 101, 082's door, and `059_functions.sql`.",
            ["059", "082", "101"],
        ),
        (".env.example", "# migration 101\nVALUE=migration_059\n", ["101"]),
    ],
)
def test_source_languages_find_only_prose(filename, source, numbers):
    assert (
        sorted({item.number for item in check_source(Path(filename), source)})
        == numbers
    )


def test_supported_markers_and_legitimate_numbers_are_allowed():
    source = """000_initial.sql, migration 001, `002`, 128-bit keys, 100 rows,
20 msgs/min, 120/min/workspace, VARCHAR(100), 127.0.0.1 and 100.64.0.0/10.
100 chunks of 1 KiB, 1–2,048 characters and 2,000 posts are quantities.
"""
    assert check_source(Path("guide.md"), source) == []


def test_line_numbers_account_for_unicode_and_multiline_docstrings():
    source = 'title = "é"  # a quantity: 120 seconds\n\ndef example():\n    """Schema behavior.\n\n    Migration 101 owns the policy.\n    """\n'
    findings = check_source(Path("sample.py"), source)
    assert [(item.line, item.number) for item in findings] == [(6, "101")]


def test_repository_has_no_retired_references():
    assert check_repository(Path(__file__).resolve().parents[2]) == []
