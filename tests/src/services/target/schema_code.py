"""Function bodies read from the schema code.

`src.models.target.ddl` creates every function the schema holds from its SQL
files. A test that pins a Python fragment to the SQL a function runs reads the
function's body from those files here, so it reads what the builder installs.
"""

from __future__ import annotations

import functools
import re

from scripts.migration_runner import split_statements
from scripts.sql_lexer import CODE, COMMENTS, DOLLAR, segments
from src.models.target import ddl

#: A statement that defines a function, read over its code alone. A schema
#: before the name is read past.
_DEFINES = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:\w+\s*\.\s*)?(\w+)\s*\(", re.I
)


def function_body(name: str, *, arity: int | None = None) -> str:
    """The body of the function *name*: the text between its dollar quotes,
    as the schema code writes it.

    A name the schema code defines more than once needs *arity*, the number
    of parameters of the definition to read, so the caller names the overload
    it reads. For a name defined once, *arity* is a check.
    """
    definitions = function_definitions(name)
    if arity is None:
        if len(definitions) != 1:
            raise LookupError(
                f"{name} is defined with {sorted(definitions)} parameters:"
                " pass arity to name one"
            )
        (body,) = definitions.values()
        return body
    if arity not in definitions:
        raise LookupError(
            f"{name} is defined with {sorted(definitions)} parameters, none with"
            f" {arity}"
        )
    return definitions[arity]


def function_definitions(name: str) -> dict[int, str]:
    """Every definition of the function *name* in the schema code: its body,
    keyed by its number of parameters."""
    definitions = _definitions().get(name)
    if not definitions:
        raise LookupError(f"the schema code defines no function {name}")
    return dict(definitions)


@functools.cache
def _definitions() -> dict[str, dict[int, str]]:
    found: dict[str, dict[int, str]] = {}
    for file in ddl.BEFORE_TABLES + ddl.AFTER_TABLES:
        sql = (ddl.HERE / file).read_text(encoding="utf-8")
        for statement in split_statements(sql):
            parts = [s for s in segments(statement) if s.kind not in COMMENTS]
            # Every literal and quoted name stands in as one word, so a paren
            # or a comma read from the code is one PostgreSQL reads.
            code = "".join(s.text if s.kind == CODE else " _ " for s in parts).strip()
            head = _DEFINES.match(code)
            if not head:
                continue
            name, arity = head.group(1), _arity(code[head.end() :])
            bodies = [s.text for s in parts if s.kind == DOLLAR]
            if len(bodies) != 1:
                raise LookupError(f"{file}: {name} has {len(bodies)} dollar bodies")
            tag = bodies[0][: bodies[0].index("$", 1) + 1]
            if arity in found.setdefault(name, {}):
                raise LookupError(
                    f"{file}: {name} is defined twice with {arity} parameters"
                )
            found[name][arity] = bodies[0][len(tag) : -len(tag)]
    return found


def _arity(after_paren: str) -> int:
    """The number of parameters in the list *after_paren* opens: its commas
    outside any inner parenthesis, up to the paren that closes it."""
    depth, commas, written = 0, 0, False
    for ch in after_paren:
        if ch == ")" and depth == 0:
            return commas + 1 if written else 0
        depth += {"(": 1, ")": -1}.get(ch, 0)
        commas += ch == "," and depth == 0
        written = written or not ch.isspace()
    raise LookupError(f"an unclosed parameter list: {after_paren[:80]}")
