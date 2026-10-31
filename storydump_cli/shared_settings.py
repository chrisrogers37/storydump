"""The settings the API and the worker must share, compared by fingerprint.

`documentation/guides/cloud-deployment.md` § 3 sets these on both Railway
services. :meth:`storydump_cli.railway.Railway.variables` reduces each value
to a SHA-256 digest as Railway's answer is read, so a comparison sees digests
only, and :func:`verdict` names settings, never a value or a digest: the
values are production's credentials.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from src.services.target.vocabulary import (
    DATABASE_URL_VAR,
    TELEGRAM_BOT_VAR,
    TELEGRAM_TOKEN_VAR,
)
from storydump_cli.commands import as_uuid

#: Held by both services: one that a single service holds is missing, and one
#: whose fingerprints differ is wrong.
REQUIRED: tuple[str, ...] = (
    "DATABASE_URL",
    "ENCRYPTION_KEY",
    "ENCRYPTION_KEYS",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "OPS_USER_IDS",
    DATABASE_URL_VAR,
    TELEGRAM_TOKEN_VAR,
    TELEGRAM_BOT_VAR,
    "WEB_APP_URL",
)
#: The API's before they are the worker's: the guide sets them on the worker
#: once a sign-in's claim link works, so one that a single service holds is
#: said, never a fault. Held by both, they must agree.
STAGED: tuple[str, ...] = ("EMAIL_FROM", "RESEND_API_KEY")
#: Every setting the check compares.
SHARED: tuple[str, ...] = REQUIRED + STAGED

#: The port a database URL that names none is reached on.
POSTGRES_PORT = 5432

FIX = (
    "give each named variable one value on both services (Railway › the"
    " service › Variables; documentation/guides/cloud-deployment.md § 3)"
)


def _entries(value: str) -> list[str]:
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def _operators(value: str) -> Optional[str]:
    """The ids the API's parser admits (`parse_ops_user_ids` in
    `src/config/settings.py`, which this package may not import), as a set:
    the order, the spacing and the spelling say nothing. An entry that is not
    an id admits nobody, so it is left out, and a value with no id left reads
    as unset, as both services read it."""
    ids = sorted({uid for uid in map(as_uuid, _entries(value)) if uid})
    return "\n".join(ids) if ids else None


def _key_ring(value: str) -> str:
    """A list: the first key encrypts and the rest only decrypt
    (`src/utils/encryption.py`), so the order is part of the value."""
    return "\n".join(_entries(value))


def _points_at(url: str) -> str:
    """Where a database URL points: its host, port and database name. Each
    service logs in as its own role (the API as `svc_ingress`, the worker as
    `svc_worker`), so the login is not compared. A URL that cannot be read as
    one is compared whole; the worker refuses it at boot."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port or POSTGRES_PORT
    except ValueError:
        return url
    if not host:
        return url
    return f"{host}\n{port}\n{parts.path.lstrip('/')}"


#: How a setting is read before it is compared, None where its services read
#: it as unset; any other setting is compared exactly.
READ_AS: Mapping[str, Callable[[str], Optional[str]]] = {
    "OPS_USER_IDS": _operators,
    "ENCRYPTION_KEYS": _key_ring,
    DATABASE_URL_VAR: _points_at,
}


def fingerprint(name: str, value: str) -> Optional[str]:
    """The SHA-256 digest of *value* as the services read the shared setting
    *name*; None for a setting outside :data:`SHARED`, or one its services
    read as unset: a blank value, or one whose :data:`READ_AS` reader answers
    None."""
    if name not in SHARED or not value.strip():
        return None
    text = READ_AS[name](value) if name in READ_AS else value
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def _held_alone(
    prints: Mapping[str, Mapping[str, str]], names: tuple[str, ...], said: str
) -> list[str]:
    """``only on <service><said>: A, B`` for each service that holds one of
    *names* the other lacks."""
    (first, a), (second, b) = prints.items()
    found = []
    for service, side, other in ((first, a, b), (second, b, a)):
        alone = [name for name in names if name in side and name not in other]
        if alone:
            found.append(f"only on {service}{said}: {', '.join(alone)}")
    return found


def verdict(prints: Mapping[str, Mapping[str, str]]) -> tuple[str, str, str]:
    """The check's row, ``(state, value, fix)``, from each service's
    fingerprints, keyed by service in report order. Wrong when a setting both
    hold differs, missing when one of :data:`REQUIRED` is held by one service
    only, else ok. The value names settings and counts them, nothing more."""
    api, worker = prints.values()
    both = [name for name in SHARED if name in api and name in worker]
    differ = [name for name in both if api[name] != worker[name]]
    lone = _held_alone(prints, REQUIRED, "")
    staged = _held_alone(prints, STAGED, " so far")
    if not differ and not lone:
        return ("ok", "; ".join([f"{len(both)} shared settings agree", *staged]), "")
    found = [f"differ: {', '.join(differ)}"] if differ else []
    return ("wrong" if differ else "missing", "; ".join(found + lone + staged), FIX)
