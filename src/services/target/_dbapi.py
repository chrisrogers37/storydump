"""The one place the SQLAlchemy→asyncpg error chain is unwrapped.

SQLAlchemy's asyncpg dialect wraps the driver exception one level deeper than
`exc.orig` — catching only one level was the intent ledger's first bug
(`intent_ledger.transition`'s comment records it), and the jobs service then
re-derived the same fact independently. Two finders, no signal between them;
hence this helper.
"""

from __future__ import annotations


def driver_candidates(exc: BaseException) -> tuple:
    """The driver exceptions possibly buried in a ``DBAPIError``: ``orig``
    and its ``__cause__``, Nones dropped. Callers apply their own predicate
    (an isinstance for the ledger, a constraint name for the jobs service)."""
    orig = getattr(exc, "orig", None)
    return tuple(c for c in (orig, getattr(orig, "__cause__", None)) if c is not None)


def constraint_violated(exc: BaseException, *names: str) -> bool:
    """Did *exc* report a violation of one of *names*?

    THE FOURTH READER OF THE SAME QUESTION, AND THE LAST ONE. Three siblings
    already ask it with a hand-rolled two-line predicate over
    ``driver_candidates`` (`jobs`, `publish_cap`, `provider_ops`), and the X.3
    identity door needed it again from the OTHER driver — which is what made
    this worth hoisting rather than writing a fourth private copy of the
    lesson this module's docstring already records.

    **It covers both drivers, because the tier has both.** asyncpg reports the
    name flat on the exception and arrives wrapped in a SQLAlchemy
    ``DBAPIError``; psycopg2/3 report it on ``diag.constraint_name`` and, on
    the sync lane, arrive unwrapped. So the exception itself is a candidate
    alongside ``driver_candidates``, and both spellings are read.

    ``sqlstate`` is checked where it is available (23505 is unique_violation)
    but is not required: a check-constraint violation names a constraint too,
    and callers ask about the name, not the class.
    """
    for candidate in (exc,) + driver_candidates(exc):
        reported = getattr(candidate, "constraint_name", None) or getattr(
            getattr(candidate, "diag", None), "constraint_name", None
        )
        if reported and reported in names:
            return True
    return False
