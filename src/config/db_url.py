"""The userinfo of a Postgres URL built from separate fields — the one encoding rule.

Four places build a connection URL from ``DB_*``-style fields rather than take
one whole: the test suite's ``settings.test_database_url``, the harness's
``unit_of_work.async_database_url``, the gates' ``tests/scripts/conftest._dsn``,
and ``make init-db``'s ``scripts/app_db_url``. Pasted raw, a user or password
carrying ``@``, ``/`` or ``%`` is misread — as part of the host, as the end of
the authority, as an escape — so each of them encodes through here. libpq and
SQLAlchemy both decode the escapes back (measured with ``parse_dsn`` and
``make_url``). Deployed services never build a URL: they carry
``TARGET_DATABASE_URL`` and ``DATABASE_URL`` whole.
"""

from __future__ import annotations

from urllib.parse import quote


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
