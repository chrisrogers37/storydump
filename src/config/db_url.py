"""The rules for a Postgres URL: its encoded userinfo, whether a host is a
pooled endpoint, and the refusal of a URL a driver cannot read.

Four places build a connection URL from ``DB_*``-style fields rather than take
one whole: the test suite's ``settings.test_database_url``, the harness's
``unit_of_work.async_database_url``, the gates' ``tests/scripts/conftest._dsn``,
and ``make init-db``'s ``scripts/app_db_url``. Pasted raw, a user or password
carrying ``@``, ``/`` or ``%`` is misread — as part of the host, as the end of
the authority, as an escape — so each of them encodes through here. libpq and
SQLAlchemy both decode the escapes back (``parse_dsn``, ``make_url``). Deployed
services never build a URL: they carry ``TARGET_DATABASE_URL`` and
``DATABASE_URL`` whole.

**A pooled endpoint.** The worker's clock election and the migration runner's
lock are session-scoped advisory locks, released when the server session
ends. A transaction pooler (PgBouncer in transaction mode, which is what
Neon's pooled endpoint is) keeps the server session after the client's
connection closes and lends it to the next client, lock and all. Neon names an
endpoint's pooled host by adding ``-pooler`` to the first label of its direct
host: ``ep-<id>.<region>.aws.neon.tech`` is direct,
``ep-<id>-pooler.<region>.aws.neon.tech`` is pooled. :func:`is_pooled_host`
reads that name. It takes a host, not a URL: each caller hands it the hosts
its own driver uses (the runner, the one libpq connected to; the worker, the
ones SQLAlchemy reads from its URL, ``unit_of_work.engine_url_refusal``), so no
second reading of a connection string can disagree with the driver's. A
pooler behind any other name is not seen here.

**An unreadable URL.** A driver that cannot read a connection string quotes
the part it could not read, and when a password carries an unencoded ``@``, a
``/`` libpq takes for the end of the authority or a ``%`` it cannot decode,
that part is the password, its head read as a port, or its tail as a host, a
database name or a query parameter. Every process that takes a database URL
refuses one it cannot read with :class:`UnreadableUrl`, which names the
variable the URL came from and never its value
(:func:`unreadable_url_refusal`), raised past the driver's own error so that
error is neither its cause nor its context.

One rule holds at every door: a URL that carries a raw ``@`` besides the one
before the host (:func:`is_ambiguous_url`) is refused before any driver reads
it. Every driver reads part of a password that carries one as the rest of the
URL, and resolving a host read so would send the password's tail to the
resolver; libpq misreads a user name that carries one the same way. Nor
may a database name carry an ``@``: with one ``@`` at most, a driver puts it
there only where it read no user information, after a raw ``/`` in the user,
or for libpq in the password. Past those, which strings are unreadable is
each driver's to say, as with a pooled host: what SQLAlchemy cannot parse for
the API and the worker (``unit_of_work.engine_url_from_env``) and the schema
builder; what libpq cannot parse, or reads into a URL's host carrying an
``@`` (a socket directory aside), for the migration runner and the scripts
(``scripts/libpq_connect.py``).

Standard library only: the migration runner imports this module, and its
predeploy step must not depend on the application's import closure
(``tests/test_deploy_guardrails.py`` pins what importing the runner loads).
"""

from __future__ import annotations

from urllib.parse import quote

#: Neon's mark of a POOLED host: the suffix on its first DNS label. The one
#: spelling the refusals and the docs share.
POOLED_HOST_LABEL_SUFFIX = "-pooler"


def enc(part: object) -> str:
    """*part* percent-encoded for any URL component. ``safe=""``: even ``/`` is
    encoded — an unencoded one ends the authority."""
    return quote(str(part), safe="")


def userinfo(user: str | None, password: str | None) -> str:
    """``user[:password]@``, both parts encoded — or ``""`` when both are empty,
    so libpq applies its own default user. An empty password is left out
    (libpq reads ``user:@`` as ``user@`` anyway)."""
    user, password = user or "", password or ""
    if not user and not password:
        return ""
    return enc(user) + (":" + enc(password) if password else "") + "@"


def is_pooled_host(host: str | None) -> bool:
    """Whether *host* names a pooled endpoint: a host whose first DNS label ends
    in ``-pooler``.

    *host* is one host or libpq's comma-separated list of them (``PGHOST``
    takes the same form), and a host may carry a port (``name:5432``,
    SQLAlchemy's multi-host form). An IPv6 literal and a socket directory (a
    value starting with ``/``) are never pooled. Nothing here raises.
    """
    for item in (host or "").split(","):
        name = item.strip().lower()
        if not name or name.startswith(("/", "[")):
            continue
        name = name.partition(":")[0]
        if name.split(".", 1)[0].endswith(POOLED_HOST_LABEL_SUFFIX):
            return True
    return False


def is_ambiguous_url(url: str) -> bool:
    """Whether *url* is a URI (it carries ``://``) whose text after the
    ``://`` carries more than one ``@``.

    A raw ``@`` in a password makes every driver end the user information at
    one ``@`` and read the rest of the password as the host, the port, the
    database name or the query; in a user name it makes libpq do the same,
    though SQLAlchemy reads one there. A well-formed URL carries only
    the one ``@`` before the host, and percent-encodes any other as ``%40``,
    which libpq and SQLAlchemy decode in the password and in query values. A
    key/value string carries no ``://`` and is never ambiguous.

    The accepted cost: a user name with an ``@`` (``user@server``) must encode
    it, even for SQLAlchemy, which would have read it; a URL with a user or a
    password and an ``@`` in a query value must encode that ``@`` too, as in
    ``?host=/run/user%401000``; and one with an ``@`` in the database name is
    refused (a database name may carry none at any door). Without a user or a
    password, the one ``@`` may stand in a socket directory, and a key/value
    string's ``host=@pgsock`` still works. Nothing here raises.
    """
    _, separator, rest = url.partition("://")
    return bool(separator) and rest.count("@") > 1


def pooled_endpoint_refusal(variable: str) -> str:
    """The sentence both refusals print. It names *variable* and never its
    value: the URL carries a password, and its host the endpoint's id."""
    return (
        f"{variable} names a pooled database endpoint (its host's first label"
        f" ends in {POOLED_HOST_LABEL_SUFFIX!r}, Neon's connection pooler)."
        " This process holds a session-scoped advisory lock, and a transaction"
        " pooler keeps the server session, and the lock with it, after the"
        " process lets go, so the next process waits on it. Use the endpoint's"
        f" direct host: the same host without {POOLED_HOST_LABEL_SUFFIX!r}."
    )


#: How a refusal names the owner's URL in the tools that take it as
#: ``DATABASE_URL`` or ``--database-url``: the migration runner, the schema
#: builder, the catalog comparator's ``extract`` and the schema consistency
#: job. The one spelling they share.
DATABASE_URL_NAME = "DATABASE_URL (or --database-url)"


def unreadable_url_refusal(variable: str) -> str:
    """The sentence every refusal of an unreadable URL prints. It names
    *variable* and every fix, never the value, so it never says which fix
    applies: the driver's own error, which would, quotes the part it could not
    read, which can be the password."""
    return (
        f"{variable} cannot be read as a database URL: write it as"
        " postgresql://user:password@host:port/database, percent-encoding every"
        " @ but the one before the host and any / ? # or % in the user or"
        " password, and check its port and each query parameter (or key/value"
        " option): its name, its = and its value."
    )


class UnreadableUrl(ValueError):
    """A database URL refused by the variable it came from, never by its value
    (:func:`unreadable_url_refusal`).

    Raised past the driver's own error, never inside its ``except``: that error
    quotes the part it could not read, so it may be neither this one's cause
    nor its context, which a traceback or a logger prints as well. A
    ``ValueError``, so a tool that reports a bad value reports this one too."""

    def __init__(self, variable: str):
        super().__init__(unreadable_url_refusal(variable))
