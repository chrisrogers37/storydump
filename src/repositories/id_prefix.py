"""Prefix matching on an id column, without a pattern language (#905).

Both id-prefix lookups built their filter as ``cast(col, String).like(f"{p}%")``
with the prefix coming straight from a caller. ``%`` and ``_`` are LIKE
metacharacters, so a prefix carrying either was interpreted as a **pattern**
rather than a literal: ``_`` matches any single character and ``%`` matches any
run, silently widening the match set.

**Not exploitable today, and that is the reason to fix it.** Both call sites sit
behind an ownership gate that refuses regardless of how many rows the prefix
matched, so a widened set changes nothing right now. **The protection lives in a
different component from the defect** — any future caller of those functions not
sitting behind that gate inherits an unescaped pattern match, and nothing in
either signature warns them. A thing that happens to be right is not a thing
that must be right, and only the second belongs in a control (#868's
distinction, applied here).

## Why not just escape

Escaping works — ``.startswith(prefix, autoescape=True)`` would do it. It is
rejected for one reason: it stays correct only while a keyword argument stays
present. A future edit that drops ``autoescape=True``, or a new call site that
never adds it, reintroduces the defect silently and the operator still reads
fine. Comparing a fixed-length ``left()`` slice removes the pattern language
from the expression entirely — there is no metacharacter to escape because
nothing interprets one, and there is no argument to forget.

That is also why this is a helper rather than an inlined expression at two
sites: a third id-prefix lookup should get the safe behaviour by having nothing
to remember, not by its author recalling this note.
"""

from __future__ import annotations

from sqlalchemy import String, cast, func


def id_prefix_matches(column, prefix: str):
    """A filter matching rows whose *column*, as text, begins with *prefix*.

    The prefix is compared **literally**: `%` and `_` carry no special meaning,
    because the expression is an equality on a `left()` slice rather than a
    pattern match.

    An empty prefix matches every row, exactly as an empty LIKE prefix did —
    this helper closes a metacharacter hole, not a missing-input check, and
    silently turning an empty prefix into "match nothing" would be a different
    behaviour change hiding inside a security fix. Callers that need a minimum
    length should say so themselves.
    """
    text_column = cast(column, String)
    return func.left(text_column, len(prefix)) == prefix
