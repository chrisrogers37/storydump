"""Open a libpq connection, refusing a connection string libpq cannot read by
the name of the variable it came from, never by its value.

libpq quotes the part of a connection string it could not read, and that part
can be the password, as with an escape it cannot decode, or the tail an
unencoded ``@`` or ``/`` in the password left in the host, the port or the
database name. :func:`connect` refuses with ``UnreadableUrl``
(``src.config.db_url``) a URL that carries a raw ``@`` besides the one before
the host, a string libpq's own parse refuses (``parse_dsn``, which does no
I/O), and a database name, or a URL's host, that parse reads with an ``@`` in
it, before libpq is handed any of them. Every other error is libpq's own,
raised unchanged: it quotes what someone typed in an option, or names the host
and the user, never the password.

Like the runner, which imports it, this loads the standard library, psycopg2
and, from ``src``, ``src.config.db_url`` alone
(``tests/test_deploy_guardrails.py``).
"""

from __future__ import annotations

import psycopg2
from psycopg2.extensions import parse_dsn

from src.config.db_url import UnreadableUrl, is_ambiguous_url


def connect(dsn: str, variable: str):
    """``psycopg2.connect(dsn)``, or ``UnreadableUrl`` naming *variable* when
    libpq cannot read *dsn* (:func:`_readable`). Any error libpq raises is
    its own, raised unchanged."""
    if _readable(dsn):
        return psycopg2.connect(dsn)
    # Past `_readable`'s `except`, so libpq's error is not even this one's
    # context.
    raise UnreadableUrl(variable)


def _readable(dsn: str) -> bool:
    """Whether libpq reads *dsn* as its writer meant it. Not a URL that carries
    a raw ``@`` besides the one before the host (``db_url.is_ambiguous_url``),
    whose password every driver misreads; not a string ``parse_dsn`` refuses,
    whose error quotes the token it choked on, or whose decoded escapes are no
    text, whose error carries the bytes; not one it reads into a database name
    carrying an ``@``; and not a URL it reads into a host carrying an ``@``,
    but for a socket directory (one starting with ``/``). libpq ends the
    authority at the first ``/``, so a raw ``/`` in the password leaves its
    head in the port and the rest, with the ``@`` after it, in the database
    name or, after a raw ``?``, a ``host`` parameter. The accepted cost: an
    abstract socket is named in a key/value string (``host=@pgsock``), which
    is not checked, and not in a URL."""
    if is_ambiguous_url(dsn):
        return False
    try:
        parsed = parse_dsn(dsn)
    except (psycopg2.ProgrammingError, UnicodeDecodeError):
        return False
    if "@" in (parsed.get("dbname") or ""):
        return False
    return "://" not in dsn or not any(
        "@" in item and not item.startswith("/")
        for item in (parsed.get("host") or "").split(",")
    )
