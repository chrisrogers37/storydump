"""The one place the SQLAlchemy→asyncpg error chain is unwrapped.

SQLAlchemy's asyncpg dialect wraps the driver exception one level deeper than
`exc.orig` — catching only one level was the intent ledger's first bug
(`intent_ledger.transition`'s comment records it), and the jobs service then
re-derived the same fact independently. Two finders, no signal between them;
hence this helper.

Two questions are asked of the unwrapped chain, and they are different
questions: **which constraint** (:func:`constraint_violated`, where the DDL
names one the caller can match) and **which class** (:func:`driver_error_is`,
where it does not). Each has a door so neither is re-derived a fifth time.
"""

from __future__ import annotations

from typing import Optional


def driver_candidates(exc: BaseException) -> tuple:
    """The driver exceptions possibly buried in a ``DBAPIError``: ``orig``
    and its ``__cause__``, Nones dropped. The raw unwrap, under both named
    questions below; a caller reaches for it directly only when it is asking
    something neither of them asks."""
    orig = getattr(exc, "orig", None)
    return tuple(c for c in (orig, getattr(orig, "__cause__", None)) if c is not None)


def driver_error_is(exc: BaseException, *classes: type) -> Optional[BaseException]:
    """The first buried driver exception that is one of *classes*, else None.

    The OTHER question, and the one three siblings were asking by hand
    (`invitations` twice, `workspaces` once): not "which constraint" but
    "which class". They ask it where the DDL names no constraint they could
    rely on — `fn_invitation_accept` raises `no_data_found`, and a
    check-violation's name is read off the exception itself rather than
    matched against a literal.

    Candidate order is the caller's tie-breaker: the FIRST candidate matching
    any of *classes* is returned, so a caller discriminating between two
    classes asks once and tests the answer, rather than running one pass per
    class and inverting the precedence ``driver_candidates`` established.
    """
    for candidate in driver_candidates(exc):
        if isinstance(candidate, classes):
            return candidate
    return None


def constraint_violated(exc: BaseException, *names: str) -> bool:
    """Did *exc* report a violation of one of *names*?

    The hoist happened here, from the X.3 identity door, which needed the
    question again from the OTHER driver. Three siblings still ask it with a
    hand-rolled predicate over ``driver_candidates`` — `jobs`, `publish_cap`
    and `provider_ops` — and each of those pins ONE named constraint it
    declares as a module constant, which is a narrower and more legible thing
    than a variadic name match; they are not owed this door. Prefer it where
    the constraint has a name; prefer :func:`driver_error_is` where the
    question is the class.

    **It covers both drivers, because the tier has both.** asyncpg reports the
    name flat on the exception and arrives wrapped in a SQLAlchemy
    ``DBAPIError``; psycopg2/3 report it on ``diag.constraint_name`` and, from
    a psycopg2 caller (the gates' fixtures), arrive unwrapped. So the exception
    itself is a candidate alongside ``driver_candidates``, and both spellings
    are read.

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
