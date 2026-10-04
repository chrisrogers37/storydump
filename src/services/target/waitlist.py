"""The marketing waitlist (100, `07` §43): one statement, an INSERT.

The API owns the write the landing site used to make with its own database
credential. `waitlist_entries` gives `svc_ingress` INSERT and nothing else, so
:func:`join` cannot read whether an address was already there, and does not
try: a repeat is `ON CONFLICT DO NOTHING`, and the caller learns nothing about
who is on the list.

The table's CHECK is the authority on what an address is
(`ck_waitlist_entries_email`); :func:`join` only trims and lower-cases, then
translates the CHECK's refusal.
"""

from __future__ import annotations

import json
from typing import Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.services.target._dbapi import constraint_violated

#: The longest address the CHECK admits, refused before the statement so an
#: oversized value never travels to the database.
MAX_EMAIL_LENGTH = 254
#: The campaign keys the site forwards (`landing/src/lib/analytics.ts`).
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign")
#: Each campaign value is cut to this, so a public form cannot grow a row.
MAX_UTM_LENGTH = 100

_EMAIL_CHECK = "ck_waitlist_entries_email"


def _storable(value: str) -> bool:
    """Can PostgreSQL hold *value* in text and jsonb? Neither takes a NUL, and
    a lone surrogate (which JSON can carry) is not UTF-8: refused before the
    statement, where the driver would fail rather than a CHECK."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class InvalidWaitlistEmail(StorydumpError):
    """The address is not one the waitlist can hold."""


def campaign(fields: Mapping[str, object]) -> Optional[dict]:
    """The UTM keys present as non-empty, storable strings, each trimmed and
    capped; a value the database cannot hold is dropped, not refused."""
    utm = {}
    for key in UTM_KEYS:
        value = fields.get(key)
        if isinstance(value, str) and value.strip() and _storable(value):
            utm[key] = value.strip()[:MAX_UTM_LENGTH]
    return utm or None


async def join(conn, email: object, utm: Optional[dict] = None) -> None:
    """Add *email*, trimmed and lower-cased, to the waitlist; an address
    already there is not an error. Raises :class:`InvalidWaitlistEmail` for one
    the CHECK refuses, under a savepoint so the caller's transaction carries
    on. Runs in the caller's transaction and does not commit."""
    address = email.strip().lower() if isinstance(email, str) else ""
    if len(address) > MAX_EMAIL_LENGTH or not _storable(address):
        raise InvalidWaitlistEmail("not a valid email address")
    try:
        # ON CONFLICT names no column on purpose: `ON CONFLICT (email)` needs
        # SELECT on it, which svc_ingress does not hold (permission denied).
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO waitlist_entries (email, utm)"
                    " VALUES (:email, CAST(:utm AS jsonb)) ON CONFLICT DO NOTHING"
                ),
                {"email": address, "utm": json.dumps(utm) if utm else None},
            )
    except DBAPIError as exc:
        if constraint_violated(exc, _EMAIL_CHECK):
            raise InvalidWaitlistEmail("not a valid email address") from exc
        raise
