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
