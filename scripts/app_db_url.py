"""The migration runner's URL for a local database, for ``make init-db``.

``make init-db`` connects twice: psql builds the by-hand base, then the
migration runner applies every file. The recipe hands this script each field
quoted exactly as psql gets it — the password double-quoted, as in
``PGPASSWORD="$(DB_PASSWORD)"``, the user, host, port and database bare, as in
``PG_OPTS`` and ``-d $(DB_NAME)`` — so the two steps connect with the same
values: a quoted ``.env`` value, an escape inside one, a comment's leading
space and make's reading of its command line reach both alike.

What this adds is the URL encoding. The recipe used to paste the fields into a
``postgresql://`` URL, and libpq misread a password carrying ``@``, ``/`` or
``%`` — as part of the host, as the end of the authority, as an escape — after
the psql step had connected with it. Every part is percent-encoded here
(``src.config.db_url``, the one rule the test harness's URLs share), a socket
directory or an IPv6 address given as the host included.

What it does not change: the fields still pass through a shell line, as every
psql recipe in the Makefile passes them, so a password carrying ``"`` or a
backtick breaks that line, a ``$`` that starts a name is expanded in it, and a
single-quoted ``.env`` password keeps its quotes — in the psql step and this
one alike. Local development only: deployed services carry ``DATABASE_URL``
whole.

Run directly, it reads the five variables from the environment and falls back
to the Makefile's own defaults, the user to libpq's, which is the login name,
as ``DB_USER ?= $(USER)`` is::

    DB_USER=dev python -m scripts.app_db_url
"""

from __future__ import annotations

import os
import sys
from typing import Mapping
from src.config.db_url import enc, userinfo

#: The Makefile's ``?=`` defaults, for a direct run (the tests pin them against
#: the Makefile; under ``make`` the recipe passes every field explicitly).
DEFAULT_HOST = "localhost"
DEFAULT_PORT = "5432"
DEFAULT_NAME = "storydump"


def app_db_url(*, user: str, password: str, host: str, port: str, name: str) -> str:
    """``postgresql://[user[:password]@]host:port/name`` with every part
    percent-encoded (``src.config.db_url``; the host too, since a socket
    directory given as one is made of slashes). An empty user or password is
    left out, so libpq applies its own default."""
    return f"postgresql://{userinfo(user, password)}{enc(host)}:{enc(port)}/{enc(name)}"


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
