"""The schema as code: every file is in the build, and the conventions hold.

`src.models.target.ddl` builds a database from the models and its SQL files.
These checks read the files and need no database; whether the files build a
database is proved where one runs.
"""

import re

from scripts.migration_runner import split_statements
from scripts.sql_lexer import COMMENTS, DOLLAR, segments
from src.models.target import ddl

SQL_FILES = sorted(p.name for p in ddl.HERE.glob("*.sql"))


def _text(name):
    return (ddl.HERE / name).read_text(encoding="utf-8")


def _code(statement):
    """The statement without comments and with every dollar-quoted body
    replaced, so a pattern sees only the statement's own words."""
    return "".join(
        "$BODY$" if s.kind == DOLLAR else s.text
        for s in segments(statement)
        if s.kind not in COMMENTS
    )


def test_every_file_is_in_the_build_in_prefix_order():
    assert list(ddl.BEFORE_TABLES + ddl.AFTER_TABLES) == SQL_FILES


def test_a_catalog_comment_is_only_ever_cleared():
    """Descriptions live in the files as `--` lines; the catalog holds none."""
    cleared = []
    for name in SQL_FILES:
        for statement in split_statements(_text(name)):
            code = _code(statement).strip()
            if re.match(r"COMMENT\s+ON\b", code, re.I):
                assert re.search(r"\bIS\s+NULL$", code, re.I), code[:80]
                cleared.append(code)
    assert cleared, "the positive control: the schema's own comment is cleared"


def test_no_function_body_holds_a_comment():
    bodies = 0
    for name in SQL_FILES:
        for seg in segments(_text(name)):
            if seg.kind == DOLLAR:
                bodies += 1
                tag = seg.text[: seg.text.index("$", 1) + 1]
                body = seg.text[len(tag) : -len(tag)]
                assert not any(s.kind in COMMENTS for s in segments(body)), (
                    name,
                    body[:80],
                )
    assert bodies > 85, "every function body was read"


def test_every_security_definer_function_pins_its_search_path():
    definers = 0
    for name in ("03_table_functions.sql", "04_functions.sql"):
        for statement in split_statements(_text(name)):
            code = _code(statement)
            if re.search(r"\bSECURITY\s+DEFINER\b", code, re.I):
                definers += 1
                assert re.search(r"\bSET\s+search_path\s*=", code, re.I), code[:120]
    assert definers, "the positive control: the schema has definer functions"
