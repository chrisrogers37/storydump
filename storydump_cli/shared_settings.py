"""The settings the API and the worker must share, compared by fingerprint.

`documentation/guides/cloud-deployment.md` § 3 sets these on both Railway
services. :meth:`storydump_cli.railway.Railway.variables` reduces each value
to a SHA-256 digest as Railway's answer is read, so a comparison sees digests
only, and :func:`verdict` names settings, never a value or a digest: the
values are production's credentials.
"""

from __future__ import annotations

import hashlib
from typing import Mapping, Optional
from urllib.parse import urlsplit

from src.services.target.vocabulary import (
    DATABASE_URL_VAR,
    TELEGRAM_BOT_VAR,
    TELEGRAM_TOKEN_VAR,
)

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


def fingerprint(name: str, value: str) -> Optional[str]:
    """The SHA-256 digest of *value* for the shared setting *name*: the
    runtime database URL by where it points (:func:`_points_at`), every other
    setting exactly. None for a setting outside :data:`SHARED`, or a blank
    value, which its services read as unset."""
    if name not in SHARED or not value.strip():
        return None
    text = _points_at(value) if name == DATABASE_URL_VAR else value
    return hashlib.sha256(text.encode()).hexdigest()


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
