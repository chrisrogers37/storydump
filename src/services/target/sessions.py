"""Web sessions — `07` §1's opaque cookie, served from `session_tokens`.

The session is an opaque random 256-bit value. Only its SHA256 is stored, so
a database read discloses nothing that can be presented back; verification is
one indexed lookup plus the expiry/revocation check; renewal slides. There is
no JWT for human web sessions (`07` §1) — the value carries no claims, and the
database is the only authority on whether it is live.

Every function takes the caller's async executor (an `AsyncConnection` or an
`AsyncSession`) and runs inside the caller's transaction — the tier's
conn-first raw-SQL shape. `session_tokens` is an auth-plane table (`07` §2):
role-scoped `USING (true)` for `svc_ingress`, no tenant context needed, which
is what lets a tenant-less user (every user, for their first few seconds on
the greenfield) be resolved at all.

Refusals are `TenantResolutionError`s with the shared closed reasons
(`invalid_session` · `expired_session` · `revoked_session` · `disabled_user`),
because the adapter already maps that type and a second refusal type for the
same door would split the mapping.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import text

from src.exceptions.tenancy import TenantResolutionError

#: `07` §1: "now() + 30 days (05 seam), sliding on use".
SESSION_TTL_SECONDS = 30 * 24 * 3600

#: Sliding renewal writes at most once per this interval. Renewal on every
#: authenticated request would turn each page load into an UPDATE; the slide
#: only needs to be recent, not exact, and the cost of the throttle is that
#: a session expires at most this much earlier than a per-request slide would
#: allow — a rounding error against a 30-day TTL.
RENEW_THROTTLE_SECONDS = 60


@dataclass(frozen=True)
class Session:
    """A live session: which token row, which user."""

    id: str
    user_id: str


def token_hash(value: str) -> str:
    """The stored form. SHA256 hex over the opaque value."""
    return hashlib.sha256(value.encode()).hexdigest()


def new_token() -> str:
    """256 bits, URL-safe. Never logged, never stored in the clear."""
    return secrets.token_urlsafe(32)


async def issue(
    executor, *, user_id: str, ttl_seconds: int = SESSION_TTL_SECONDS
) -> str:
    """Mint a session for *user_id* and return the OPAQUE value (the cookie).

    The value exists in memory exactly once — here and on the wire; only its
    hash reaches the database.
    """
    value = new_token()
    await executor.execute(
        text(
            "INSERT INTO session_tokens (user_id, token_hash, expires_at)"
            " VALUES (:uid, :h, now() + make_interval(secs => :ttl))"
        ),
        {"uid": str(user_id), "h": token_hash(value), "ttl": ttl_seconds},
    )
    return value


async def resolve(executor, *, token_hash: str) -> Session:
    """Authenticate a presented value (already hashed) and slide its expiry.

    Raises `TenantResolutionError` with the reason that names why, checked in
    the order that discloses least: an unknown hash reads exactly like a
    revoked or expired one to a caller who cannot see the row, and the
    distinct reasons exist for the adapter's logging, not for the response.
    A `disabled` user denies here — "the ONE ingress gate" `02` §1 names —
    so a disabled account cannot reach any route, workspace-scoped or not.
    """
    row = (
        await executor.execute(
            text(
                "SELECT s.id, s.user_id, s.expires_at <= now() AS expired,"
                "       s.revoked_at IS NOT NULL AS revoked, u.state"
                "  FROM session_tokens s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = :h"
            ),
            {"h": token_hash},
        )
    ).first()
    if row is None:
        raise TenantResolutionError("invalid_session")
    session_id, user_id, expired, revoked, user_state = row
    if revoked:
        raise TenantResolutionError("revoked_session")
    if expired:
        raise TenantResolutionError("expired_session")
    if user_state != "active":
        raise TenantResolutionError("disabled_user")

    await executor.execute(
        text(
            "UPDATE session_tokens"
            "   SET expires_at = now() + make_interval(secs => :ttl),"
            "       last_seen_at = now()"
            " WHERE id = :id"
            "   AND (last_seen_at IS NULL"
            "        OR last_seen_at < now() - make_interval(secs => :throttle))"
        ),
        {
            "ttl": SESSION_TTL_SECONDS,
            "id": session_id,
            "throttle": RENEW_THROTTLE_SECONDS,
        },
    )
    return Session(id=str(session_id), user_id=str(user_id))


async def revoke(executor, *, token_hash: str) -> bool:
    """Sign out: set `revoked_at`. True if a live row was revoked, False if
    there was nothing live to revoke (already revoked, or unknown) — the
    caller clears the cookie either way and does not distinguish."""
    result = await executor.execute(
        text(
            "UPDATE session_tokens SET revoked_at = now()"
            " WHERE token_hash = :h AND revoked_at IS NULL"
        ),
        {"h": token_hash},
    )
    return result.rowcount == 1
