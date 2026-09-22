"""The migration runner's URL for a local database, for ``make init-db``.

``make init-db`` connects twice: psql builds the by-hand base, then the
migration runner applies every file. The Makefile hands both steps the same
fields through the same shell quoting — ``PGPASSWORD="$(DB_PASSWORD)"`` for
psql, ``DB_PASSWORD="$(DB_PASSWORD)"`` (and the other four) for this script —
so the two connect with identical values: a quoted ``.env`` value, an escape
inside one, and make's reading of its command line apply to both alike.

What this adds is the URL encoding. The recipe used to paste the fields into a
``postgresql://`` URL, and libpq misread a password carrying ``@``, ``/`` or
``%`` — as part of the host, as the end of the authority, as an escape — after
the psql step had connected with it. Every part is percent-encoded here, a
socket directory or an IPv6 address given as the host included.

What it does not change: the fields still pass through a shell line, as every
psql recipe in the Makefile passes them, so a password carrying ``"`` or a
backtick breaks that line, a ``$`` is expanded in it, and a single-quoted
``.env`` value keeps its quotes — in the psql step and this one alike. Local
development only: deployed services carry ``DATABASE_URL`` whole.

Run directly, it reads the five variables from the environment and falls back
to the Makefile's own defaults, the user to libpq's, which is the login name,
as ``DB_USER ?= $(USER)`` is::

    DB_USER=dev python -m scripts.app_db_url
"""

from __future__ import annotations

import os
import sys
from typing import Mapping
from urllib.parse import quote

#: The Makefile's ``?=`` defaults, for a direct run (the tests pin them against
#: the Makefile; under ``make`` the recipe passes every field explicitly).
DEFAULT_HOST = "localhost"
DEFAULT_PORT = "5432"
DEFAULT_NAME = "storydump"


def _enc(part: str) -> str:
    # `safe=""`: even `/` is encoded — an unencoded one ends the authority, and
    # a socket directory given as the host is made of them.
    return quote(part, safe="")


def app_db_url(*, user: str, password: str, host: str, port: str, name: str) -> str:
    """``postgresql://[user[:password]@]host:port/name`` with every part
    percent-encoded. An empty user or password is left out, so libpq applies
    its own default."""
    auth = ""
    if user or password:
        auth = _enc(user) + (":" + _enc(password) if password else "") + "@"
    return f"postgresql://{auth}{_enc(host)}:{_enc(port)}/{_enc(name)}"


def main(env: Mapping[str, str] = os.environ) -> int:
    print(
        app_db_url(
            user=env.get("DB_USER", ""),
            password=env.get("DB_PASSWORD", ""),
            host=env.get("DB_HOST", DEFAULT_HOST),
            port=env.get("DB_PORT", DEFAULT_PORT),
            name=env.get("DB_NAME", DEFAULT_NAME),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
