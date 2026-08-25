"""X.3 — the web session token: mint, authenticate, revoke, renew (`07` §1).

The write side of the seam `tenant_resolution` already reads. Sessions are
auth-plane: `session_tokens` carries no workspace key and its RLS policy is
row-open to `svc_ingress`, because a session is *the door tenant context walks
through* — it must be readable before any `app.tenant_id` exists. So nothing
here sets a GUC, and nothing here needs one.

**THE TOKEN IS OPAQUE, AND THAT IS THE WHOLE DESIGN.** `07` §1: an opaque
random 256-bit value in an httpOnly/SameSite=Lax/secure cookie, only its hash
stored, verification one indexed lookup. It names nothing — not the user, not
the workspace, not an issue time. Everything it proves is proved by the row it
matches, so a signed-in user with no workspace is simply a row with no
membership rather than a shape the credential has to be able to express.

## Which credential this is, because there are two and they are not rivals

There are two hops and each has its own credential. This module is the FIRST:

- **browser ↔ web front end** — this. The session cookie `07` §1 rules:
  opaque, server-side, revocable, sliding.
- **web front end ↔ backend API** — `src/utils/webapp_auth.py`'s
  `sd1b`/`sd1u`, an HMAC-signed BFF credential.

**A correction worth carrying, because the opposite is written down
elsewhere.** The front-end surface doc states that the BFF credential "cannot
express a signed-in user who has no workspace" and asks for a shape that can.
That was true of an earlier draft and is not true of `main`: `webapp_auth`
ships TWO shapes, and the unbound one — `sd1u.{user_uuid}.{ts}.{nonce}.{sig}`
— **names no tenant at all**, with the slot ABSENT rather than empty so no
reader can coerce a sentinel into a tenant id. It landed in the same commit as
the bound shape. So that hop already expresses a tenant-less user, and this
module is not the fix for it.

What IS still open on that hop, and is not this module's to close: the BOUND
shape names `chat_settings.id`, a legacy tenant key, while the target's tenant
is `workspaces.id`.

**Everything downstream of the boundary speaks HASHES, never tokens.** The raw
value exists in exactly two places: the return of `mint_session`, which hands
it to the caller that sets the cookie, and the edge that hashes an inbound
cookie once via `session_token_hash`. `resolve_web_session`'s shipped
signature already took a hash; this module keeps that, rather than pushing the
raw token one layer deeper so two functions could hash it differently.

**Refusals reuse `TenantResolutionError`.** `invalid_session`,
`expired_session` and `revoked_session` are already in that type's closed
vocabulary and are already what `resolve_web_session` raises — its edges catch
it today. A new exception type for the same three reasons would be churn with
a behavioural risk and no reader benefit.

**The database is the clock.** Every expiry comparison and every timestamp
written is `now()` evaluated server-side. A client-supplied instant would make
session lifetime a function of whichever replica's clock drifted.

**Which doors require the caller's transaction, and why not all of them.**
`mint_session` and `touch_session` do: the first must commit atomically with
the identity mint it composes with at sign-in, and the second is a read
followed by a dependent write. `authenticate_session` is a pure read and
`revoke_session` is a single self-contained statement — a standalone sign-out
on an autocommit connection is legitimate, and refusing it would be a rule
kept for symmetry rather than for a reason.
"""

import dataclasses
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime

from src.exceptions.tenancy import TenantResolutionError
from src.services.target.sync_tx import require_transaction

#: `05` seam: "session 30 d sliding". Sliding is implemented by
#: `touch_session`, not by a longer window.
SESSION_TTL_DAYS = 30

#: 256 bits, per `07` §1. `token_urlsafe` takes BYTES, so this is 32 — the
#: rendered string is longer and its length is not the entropy.
SESSION_TOKEN_BYTES = 32


@dataclass(frozen=True)
class MintedSession:
    """A freshly minted session. ``token`` is the only time the raw value is
    ever available — it is not stored and cannot be recovered from the row.
    ``token_hash`` is carried so the caller can address the row it just made
    without re-deriving it."""

    token: str
    token_hash: str
    session_id: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthenticatedSession:
    """What a live session token proves: a user, and nothing about tenancy."""

    session_id: str
    user_id: str
    expires_at: datetime


def session_token_hash(token: str) -> str:
    """SHA256 hex of the opaque cookie value — the ONE spelling.

    Hex rather than raw bytes because the column is TEXT and every existing
    reader (and the F.3 test suite) already compares hex.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def mint_session(
    conn, user_id: str, *, ttl_days: int = SESSION_TTL_DAYS
) -> MintedSession:
    """Issue a session for *user_id* and return the raw token exactly once.

    The caller supplies the connection and owns the commit, matching the rest
    of this tier — a mint that committed on its own could not be composed with
    the identity upsert that precedes it at sign-in, and sign-in creating a
    user but no session (or the reverse) is the state this seam exists to
    make impossible.
    """
    require_transaction(conn)
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    token_hash = session_token_hash(token)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO session_tokens (user_id, token_hash, expires_at)"
            " VALUES (%s, %s, now() + make_interval(days => %s))"
            " RETURNING id, expires_at",
            (str(user_id), token_hash, ttl_days),
        )
        session_id, expires_at = cur.fetchone()
    return MintedSession(
        token=token,
        token_hash=token_hash,
        session_id=str(session_id),
        expires_at=expires_at,
    )


def authenticate_session(conn, token_hash: str) -> AuthenticatedSession:
    """The user-plane half of web auth: which user does this token prove?

    Deliberately says nothing about a workspace. A signed-in user with no
    workspace authenticates successfully here and is routed to the tenant-less
    surfaces — that is the greenfield's normal state, and refusing it would
    push the front end back to inferring tenancy from an auth failure.

    Ordering is revoked-then-expired, matching the shipped resolver: a token
    that was revoked AND has since aged out is reported as revoked, because
    revocation is the fact an operator acted on.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, user_id, expires_at, expires_at <= now(),"
            " revoked_at IS NOT NULL"
            " FROM session_tokens WHERE token_hash = %s",
            (token_hash,),
        )
        row = cur.fetchone()
    if row is None:
        raise TenantResolutionError("invalid_session")
    session_id, user_id, expires_at, expired, revoked = row
    if revoked:
        raise TenantResolutionError("revoked_session")
    if expired:
        raise TenantResolutionError("expired_session")
    return AuthenticatedSession(
        session_id=str(session_id), user_id=str(user_id), expires_at=expires_at
    )


def revoke_session(conn, token_hash: str) -> bool:
    """Sign-out. Returns whether a live row was revoked.

    IDEMPOTENT AND SILENT ON A MISS, deliberately: an unknown or
    already-revoked token is what a stale cookie looks like, and a sign-out
    that errors on one teaches the front end to swallow sign-out errors —
    which is how a real revocation failure gets swallowed too. Clearing the
    cookie is not a sign-out once tokens are server-side; this is.

    ``revoked_at IS NULL`` in the predicate keeps the first revocation's
    instant, so the audit answer to "when was this killed" does not move
    every time a stale cookie is replayed.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE session_tokens SET revoked_at = now()"
            " WHERE token_hash = %s AND revoked_at IS NULL",
            (token_hash,),
        )
        return cur.rowcount > 0


def touch_session(
    conn, token_hash: str, *, ttl_days: int = SESSION_TTL_DAYS
) -> AuthenticatedSession:
    """Sliding renewal (`05`: "session 30 d sliding") — authenticate, then
    push the window out from now.

    Authenticates FIRST, so a revoked or expired token is refused rather than
    renewed. An expired session must not be revivable by presenting it: that
    would make expiry unreachable for exactly the tokens most likely to have
    leaked. Merging the two statements into one conditional UPDATE would be a
    round-trip cheaper and would collapse invalid/revoked/expired into a
    single zero-row answer, destroying the vocabulary callers route on.

    The UPDATE addresses the row by primary key rather than re-probing the
    hash index, and keeps `revoked_at IS NULL` — that predicate is not
    redundant with the read above, it narrows the window between the two
    statements.
    """
    require_transaction(conn)
    live = authenticate_session(conn, token_hash)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE session_tokens"
            " SET last_seen_at = now(),"
            "     expires_at = now() + make_interval(days => %s)"
            " WHERE id = %s AND revoked_at IS NULL"
            " RETURNING expires_at",
            (ttl_days, live.session_id),
        )
        row = cur.fetchone()
    if row is None:
        raise TenantResolutionError("invalid_session")
    return dataclasses.replace(live, expires_at=row[0])
