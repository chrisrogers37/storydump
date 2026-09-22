"""The migration runner's URL for a local database, from the Makefile's DB_* fields.

`make init-db` used to paste the fields into the URL raw, so a password carrying
``@``, ``:``, ``/`` or ``%`` parsed into the host or the port. Every part is
percent-encoded here instead. Local development only: deployed services carry
``DATABASE_URL`` whole and never build one.

The Makefile exports ``DB_HOST``, ``DB_PORT``, ``DB_NAME``, ``DB_USER`` and
``DB_PASSWORD`` by name, so its own defaults reach this script with or without
a ``.env``; the defaults below mirror them for a direct call.

    DATABASE_URL="$(python -m scripts.app_db_url)" python -m scripts.migration_runner apply
"""

from __future__ import annotations

import os
import sys
from typing import Mapping
from urllib.parse import quote


def app_db_url(*, user: str, password: str, host: str, port: str, name: str) -> str:
    """``postgresql://user[:password]@host:port/name`` with the user, the
    password and the database name percent-encoded (``safe=""``: even ``/`` is
    encoded, because an unencoded one ends the authority)."""
    auth = quote(user, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    return f"postgresql://{auth}@{host}:{port}/{quote(name, safe='')}"


def main(env: Mapping[str, str] = os.environ) -> int:
    print(
        app_db_url(
            user=env.get("DB_USER", ""),
            password=env.get("DB_PASSWORD", ""),
            host=env.get("DB_HOST", "localhost"),
            port=env.get("DB_PORT", "5432"),
            name=env.get("DB_NAME", "storydump"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
