"""The rules for a Postgres URL: its encoded userinfo, and whether a host is a pooled endpoint.

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
