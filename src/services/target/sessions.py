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

from src.config.settings import settings
from src.exceptions.tenancy import TenantResolutionError
from src.services.target import oauth_states, vocabulary

#: `07` §1: "now() + 30 days (05 seam), sliding on use".
SESSION_TTL_SECONDS = 30 * 24 * 3600

#: Sliding renewal writes at most once per this interval. Renewal on every
#: authenticated request would turn each page load into an UPDATE; the slide
#: only needs to be recent, not exact, and the cost of the throttle is that
#: a session expires at most this much earlier than a per-request slide would
#: allow — a rounding error against a 30-day TTL.
RENEW_THROTTLE_SECONDS = 60

#: Lookup and slide in ONE statement: the data-modifying CTE runs exactly once
#: whether or not the outer SELECT reads it, and its WHERE holds the whole
#: liveness test plus the throttle, so a dead session is read and never
#: touched. One round trip per authenticated request instead of two.
#:
#: Liveness has two clocks. `expires_at` is the sliding one; `created_at` +
#: ``:max_age`` is the absolute one (`settings.SESSION_MAX_AGE_SECONDS`), so a
#: session past its age is expired however recently it was used, and the
#: slide is clamped with `LEAST` so it never carries `expires_at` past it.
_RESOLVE = text(
    "WITH s AS ("
    "  SELECT s.id, s.user_id,"
    "         s.expires_at <= now()"
    "           OR s.created_at + make_interval(secs => :max_age) <= now()"
    "           AS expired,"
    "         s.created_at, s.revoked_at IS NOT NULL AS revoked, u.state"
    "    FROM session_tokens s JOIN users u ON u.id = s.user_id"
    "   WHERE s.token_hash = :h"
    "), slide AS ("
    "  UPDATE session_tokens t"
    "     SET expires_at = LEAST(now() + make_interval(secs => :ttl),"
    "                            s.created_at + make_interval(secs => :max_age)),"
    "         last_seen_at = now()"
    "    FROM s"
    "   WHERE t.id = s.id AND NOT s.expired AND NOT s.revoked"
    "     AND s.state = 'active'"
    "     AND (t.last_seen_at IS NULL"
    "          OR t.last_seen_at < now() - make_interval(secs => :throttle))"
    ") SELECT id, user_id, expired, revoked, state FROM s"
)


@dataclass(frozen=True)
class Session:
    """A live session: which token row, which user."""

    id: str
    user_id: str


def _max_age(max_age_seconds: int | None) -> int:
    """The absolute lifetime a call runs under: its argument, else the
    deployment's `SESSION_MAX_AGE_SECONDS`."""
    if max_age_seconds is None:
        return settings.SESSION_MAX_AGE_SECONDS
    return max_age_seconds


def token_hash(value: str) -> str:
    """The stored form. SHA256 hex over the opaque value."""
    return hashlib.sha256(value.encode()).hexdigest()


def new_token() -> str:
    """256 bits, URL-safe. Never logged, never stored in the clear.

    Re-drawn while it starts with the API token prefix: a bearer wearing
    that prefix is routed to the token resolver (`src/api/principal.py`),
    so a session value that happened to wear it would fail every
    server-side call until the next sign-in. One draw in sixteen million
    — a re-draw costs nothing and closes it.
    """
    while True:
        value = secrets.token_urlsafe(32)
        if not value.startswith(vocabulary.TOKEN_PREFIX):
            return value


async def issue(executor, *, user_id: str) -> str:
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
        {"uid": str(user_id), "h": token_hash(value), "ttl": SESSION_TTL_SECONDS},
    )
    return value


async def resolve(
    executor, *, token_hash: str, max_age_seconds: int | None = None
) -> Session:
    """Authenticate a presented value (already hashed) and slide its expiry.

    *max_age_seconds* is the absolute lifetime from sign-in; None reads
    `settings.SESSION_MAX_AGE_SECONDS`. A session older than it is
    ``expired_session`` — the same answer as a lapsed slide.

    Raises `TenantResolutionError` with the reason that names why, checked in
    the order that discloses least: an unknown hash reads exactly like a
    revoked or expired one to a caller who cannot see the row, and the
    distinct reasons exist for the adapter's logging, not for the response.
    A `disabled` user denies here — "the ONE ingress gate" `02` §1 names —
    so a disabled account cannot reach any route, workspace-scoped or not.
    """
    row = (
        await executor.execute(
            _RESOLVE,
            {
                "h": token_hash,
                "ttl": SESSION_TTL_SECONDS,
                "max_age": _max_age(max_age_seconds),
                "throttle": RENEW_THROTTLE_SECONDS,
            },
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


async def revoke_all_for_user(
    executor, *, token_hash: str, max_age_seconds: int | None = None
) -> int:
    """Sign out everywhere: revoke every live session of the user who
    presents *token_hash* (already hashed), that one included. Returns how
    many rows were revoked — 0 when the presented value is unknown, or is
    itself revoked or expired (either clock, as `resolve` reads them), so a
    dead session cannot reach its siblings.

    Keyed on the presented session rather than on a user id, so the only way
    to name a user here is to hold one of their live sessions; another user's
    sessions are out of reach by construction. Already-dead rows keep their
    first `revoked_at`, as `revoke` keeps it.

    The person's pending Telegram link states go too: one minted from a
    stolen session would otherwise still attach the thief's Telegram account
    for the rest of its TTL after the person signed out everywhere.
    """
    result = await executor.execute(
        text(
            "UPDATE session_tokens t SET revoked_at = now()"
            "  FROM session_tokens p"
            " WHERE p.token_hash = :h AND p.revoked_at IS NULL"
            "   AND p.expires_at > now()"
            "   AND p.created_at + make_interval(secs => :max_age) > now()"
            "   AND t.user_id = p.user_id AND t.revoked_at IS NULL"
            " RETURNING t.user_id"
        ),
        {"h": token_hash, "max_age": _max_age(max_age_seconds)},
    )
    revoked = result.all()
    if revoked:
        await oauth_states.retire_live_states(
            executor, provider="telegram", purpose="link", user_id=revoked[0][0]
        )
    return len(revoked)
