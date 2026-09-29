"""Where a literal, a quoted identifier or a comment starts and ends in SQL (#1406).

Every scan that reads a migration as text needs that answer before it can read
anything else. `migration_runner.split_statements` cuts at a `;` only outside
all three. `advertised_ddl.normalize_statements` drops comments without
reaching into a literal. The tenancy gate reads structure — a comma, a paren, a
column name — only in code. Each of them once kept a partial answer of its own,
and each gap let a string literal change a gate's verdict. This module is the
one answer. Callers keep only their POLICY: what to do with a form they will
not read through.

The model is PostgreSQL's own scanner, for boundaries alone; it never decodes a
literal's value. Literals are read the way PostgreSQL reads them with
``standard_conforming_strings`` on, its default, so a backslash in ``'…'`` is
plain text and only an ``E'…'`` string gives it meaning. A line break before a
quote continues the literal before it, and matters only there: an escape
string's continuation keeps the escape rules.

Standalone stdlib, like the runner that imports it: the predeploy step must not
depend on the application's import closure.
"""

import re
import string
from dataclasses import dataclass

#: The kinds of segment. Every character of the input belongs to exactly one.
CODE = "code"
#: ``'…'``, and the ``B'…'`` ``X'…'`` ``N'…'`` ``U&'…'`` forms, which end by the
#: same rule: at a lone quote, a doubled one staying inside.
STRING = "string"
#: ``E'…'``: a backslash escapes the next character, the quote included.
ESCAPE_STRING = "escape_string"
#: ``"…"`` and ``U&"…"``.
IDENTIFIER = "identifier"
#: ``$tag$…$tag$``, closed only by the same tag.
DOLLAR = "dollar"
LINE_COMMENT = "line_comment"
BLOCK_COMMENT = "block_comment"

COMMENTS = frozenset({LINE_COMMENT, BLOCK_COMMENT})


@dataclass(frozen=True)
class Segment:
    kind: str
    text: str
    #: False when the input ends inside the form: a literal, identifier, body or
    #: block comment nothing closes. A line comment is closed by the end.
    closed: bool = True


#: A word — keyword or identifier. `$` may follow its first character, and any
#: non-ASCII character counts as a letter, as in PostgreSQL's ident rules. A
#: word is consumed whole, which is what keeps `a$b$` one identifier rather
#: than a dollar quote, and `name'…'` a typed literal rather than an E-string.
#: Public so a caller reading names out of a CODE segment splits them the same
#: way: `workspace_id$old` is one name, never `workspace_id`.
WORD = re.compile(r"[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_$\u0080-\U0010ffff]*")

#: A dollar-quote delimiter: `$$`, or `$tag$` whose tag does not start with a
#: digit — `$1` is a parameter.
_DOLLAR_TAG = re.compile(
    r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$"
)

#: What continues a literal into the next quote: a line break, with blanks and
#: line comments around it (scan.l's `quotecontinue`).
_CONTINUATION = re.compile(
    r"(?:[ \t\f]|--[^\n\r]*)*[\n\r](?:[ \t\n\r\f\v]|--[^\n\r]*)*(?=')"
)

_LINE_END = re.compile(r"[\n\r]")


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def name_of(token: str):
    """What an identifier token names, as PostgreSQL resolves it: a word folds
    its ASCII letters to lower case and nothing else (a Kelvin sign stays one),
    and a quoted name is exact, a doubled quote in it undoubled. ``None`` for a
    ``U&"…"`` name, whose escapes can spell anything: a caller that needs the
    name refuses rather than guesses."""
    if token[:2] in ("U&", "u&"):
        return None
    if token.startswith('"'):
        return token[1:-1].replace('""', '"')
    return token.translate(_ASCII_LOWER)


def segments(sql: str) -> list:
    """``sql`` as the run of segments PostgreSQL's lexer would bound, in order.

    Their texts concatenate back to ``sql`` exactly, so a caller that rebuilds
    a statement from its segments gets the statement.
    """
    out = []
    code_start = i = 0
    while i < len(sql):
        kind, end, closed = _form_at(sql, i)
        if kind is not None:
            if code_start < i:
                out.append(Segment(CODE, sql[code_start:i]))
            out.append(Segment(kind, sql[i:end], closed))
            code_start = end
        i = end
    if code_start < len(sql):
        out.append(Segment(CODE, sql[code_start:]))
    return out


def _form_at(sql: str, i: int):
    """The form that starts at ``i`` as ``(kind, end, closed)``; kind is None
    for code, with ``end`` past the word or character read."""
    ch = sql[i]
    if ch == "'":
        return (STRING, *_quoted(sql, i + 1, "'"))
    if ch == '"':
        return (IDENTIFIER, *_quoted(sql, i + 1, '"'))
    if sql.startswith("--", i):
        line_end = _LINE_END.search(sql, i)
        return LINE_COMMENT, line_end.start() if line_end else len(sql), True
    if sql.startswith("/*", i):
        return (BLOCK_COMMENT, *_block_comment(sql, i + 2))
    if ch == "$":
        tag = _DOLLAR_TAG.match(sql, i)
        if not tag:
            return None, i + 1, True
        close = sql.find(tag.group(), tag.end())
        if close == -1:
            return DOLLAR, len(sql), False
        return DOLLAR, close + len(tag.group()), True
    word = WORD.match(sql, i)
    if not word:
        return None, i + 1, True
    # A one-letter word run into a quote is a prefix, not an identifier.
    prefix, after = word.group(), word.end()
    if prefix in ("E", "e") and sql.startswith("'", after):
        return (ESCAPE_STRING, *_escape_string(sql, after + 1))
    if prefix in ("B", "b", "X", "x", "N", "n") and sql.startswith("'", after):
        return (STRING, *_quoted(sql, after + 1, "'"))
    if prefix in ("U", "u") and sql.startswith("&'", after):
        return (STRING, *_quoted(sql, after + 2, "'"))
    if prefix in ("U", "u") and sql.startswith('&"', after):
        return (IDENTIFIER, *_quoted(sql, after + 2, '"'))
    return None, after, True


def _quoted(sql: str, i: int, quote: str):
    """``(end, closed)`` for a body starting at ``i`` that ends at the first
    lone ``quote``; a doubled one stays inside."""
    while True:
        close = sql.find(quote, i)
        if close == -1:
            return len(sql), False
        if not sql.startswith(quote, close + 1):
            return close + 1, True
        i = close + 2


def _escape_string(sql: str, i: int):
    """``(end, closed)`` for an ``E'…'`` body starting at ``i``: a backslash
    escapes the next character, a doubled quote stays inside, and a
    continuation carries the same rules into the next quote."""
    while i < len(sql):
        ch = sql[i]
        if ch == "\\":
            i += 2
        elif ch != "'":
            i += 1
        elif sql.startswith("'", i + 1):
            i += 2
        else:
            continued = _CONTINUATION.match(sql, i + 1)
            if not continued:
                return i + 1, True
            i = continued.end() + 1
    return len(sql), False


def _block_comment(sql: str, i: int):
    """``(end, closed)`` for a ``/*`` body starting at ``i``. Block comments
    nest: each ``/*`` inside opens one more level."""
    depth = 1
    while i < len(sql):
        if sql.startswith("*/", i):
            depth -= 1
            i += 2
            if not depth:
                return i, True
        elif sql.startswith("/*", i):
            depth += 1
            i += 2
        else:
            i += 1
    return len(sql), False
