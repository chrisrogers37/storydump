"""The marketing waitlist (100, `07` §43): one statement, an INSERT.

The API owns the write the landing site used to make with its own database
credential. `waitlist_entries` gives `svc_ingress` INSERT and nothing else, so
:func:`join` cannot read whether an address was already there, and does not
try: a repeat is the primary key's refusal, absorbed under a savepoint so the
caller's transaction (and the rate counter it spent) carries on. Either way the
caller learns nothing about who is on the list.

The address and the campaign are validated before the statement, so a bad
input is a typed refusal rather than a CHECK violation; the table's CHECKs
remain the authority (`ck_waitlist_entries_email`, `ck_waitlist_entries_utm`).
"""

from __future__ import annotations

import json
import re
from typing import Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.services.target._dbapi import constraint_violated

#: RFC 5321's 256-octet path less its brackets; the table's CHECK holds it too.
MAX_EMAIL_LENGTH = 254
#: The campaign keys the site forwards (`landing/src/lib/analytics.ts`).
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
#: Each campaign value is cut to this, so a public form cannot grow a row.
MAX_UTM_LENGTH = 100

#: The site's rule: something, an @, something, a dot, something; no
#: whitespace. Stricter than the table's CHECK (which wants no dot), never
#: looser.
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
#: §35's invisible characters, refused here as the CHECK refuses them.
_INVISIBLE = re.compile(
    "[\u0080-\u00a0\u00ad\u180e\u2000-\u200f\u2028-\u202f\u205f-\u2064\u3000\ufeff]"
)
_PRIMARY_KEY = "waitlist_entries_pkey"


class InvalidWaitlistEmail(StorydumpError):
    """The address is not one the waitlist can hold."""


def normalize_email(raw: object) -> str:
    """The address as stored: trimmed and lower-cased, or a refusal."""
    email = raw.strip().lower() if isinstance(raw, str) else ""
    if (
        not email
        or len(email) > MAX_EMAIL_LENGTH
        or not _EMAIL.match(email)
        or _INVISIBLE.search(email)
    ):
        raise InvalidWaitlistEmail("not a valid email address")
    return email


def campaign(fields: Mapping[str, object]) -> Optional[dict]:
    """The UTM keys present as non-empty strings, each trimmed and capped."""
    utm = {}
    for key in UTM_KEYS:
        value = fields.get(key)
        if isinstance(value, str) and value.strip():
            utm[key] = value.strip()[:MAX_UTM_LENGTH]
    return utm or None


async def join(conn, email: str, utm: Optional[dict] = None) -> None:
    """Add *email* to the waitlist; an address already there is not an error.

    Runs in the caller's transaction and does not commit. *email* is expected
    already normalized (:func:`normalize_email`).
    """
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO waitlist_entries (email, utm)"
                    " VALUES (:email, CAST(:utm AS jsonb))"
                ),
                {"email": email, "utm": json.dumps(utm) if utm else None},
            )
    except DBAPIError as exc:
        if not constraint_violated(exc, _PRIMARY_KEY):
            raise
