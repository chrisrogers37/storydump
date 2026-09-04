"""L.6 — Instagram Login OAuth, target-side (`07` §§2-3, issue #863).

**This is a PORT, not a design.** A working Instagram Login flow already runs in
production (`src/services/integrations/instagram_login_oauth.py`, PRs #341/#378,
a real account connected). What follows moves that proven flow onto the target
tables — `oauth_states` and `oauth_credentials` — rather than re-deriving it.
Where the target differs, it differs deliberately, and each divergence is named
below so a reader can tell a decision from a drift.

## Where the target intentionally diverges from the proven flow

1. **State lives in `oauth_states`, not in a signed self-describing token.** The
   legacy `_create_state_token` mints a token the callback re-parses. The target
   stores a row and consumes it with a one-shot CAS, because replay protection
   has to be a fact about storage: a stateless token cannot be single-use.

2. **The TTL is 900s, not 600s.** `05` sets the state-token TTL at 15 minutes
   for *every* purpose; legacy used 10. The seam wins over the incumbent, and
   the number is read from the plan rather than carried over.

3. **The credential is a row under the MultiFernet ring**, not legacy token
   storage. `07` §3 is explicit that `main` already ships the ring in
   `src/utils/encryption.py` under `ENCRYPTION_KEYS`, so this reuses it — the
   plan's own note is that renaming the env var would be churn for zero gain.

4. **Refresh drops the host branch, and this is the interesting one.**
   `token_refresh.py` carries a hard-won lesson: `graph.instagram.com` accepts
   `grant_type=ig_refresh_token` + the token alone, while the FB host needs
   `fb_exchange_token` plus client id/secret — and sending IG-flavoured params
   to the FB host produced Meta error 101, "Missing client_id parameter". The
   target keeps the lesson and drops the branch, because under FC-7 no
   FB-vintage credential can exist here: `ck_credentials_provider` ships with
   **no** `fb_login_legacy` value, so the FB host is unreachable by
   construction rather than by convention. The branch is not forgotten; it is
   structurally impossible.

5. **Reconnect is "last issued wins", enforced in the ISSUE transaction.**
   `07` §2 records that the pass-2 "last consumed wins" claim was false —
   independently issued rows never consumed one another, so both callbacks
   could land. Issuing a reconnect state invalidates prior live states for the
   same target in the same transaction, so at most one live state exists per
   target at any commit.

## What this module does NOT do, stated rather than implied

The D31 dead-token symptom gate has three halves, and only one is L.6's. The
credential flip to `expired` / `reauth_required` is here; the **dispatcher**
declining to mint further intents and the **publish pipeline** recording the
fault land with L.7 and L.5. Issue #863 says so explicitly. Nothing here
pretends to close the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import hashlib
import logging
import secrets
from typing import Any, Optional

import httpx
from sqlalchemy import text

from src.config.constants import IG_LOGIN_API_BASE, IG_LOGIN_GRAPH_BASE
from src.exceptions import StorydumpError
from src.services.target import egress

logger = logging.getLogger(__name__)

#: `05`: state token TTL, 15 minutes, every purpose. Legacy used 600s.
STATE_TTL_SECONDS = 900

#: When a freshly stored credential first comes due for refresh: the `05`
#: row-56 cadence, which `0.4` confirmed clears Meta's 24-hour minimum age.
#: A PostgreSQL interval literal; the value is pinned by test.
FIRST_REFRESH_INTERVAL = "7 days"

#: The scopes the proven flow requests. Carried over verbatim — #410's
#: background turns on the app's *use case*, not on this list.
REQUIRED_SCOPES = ("instagram_business_basic", "instagram_business_content_publish")

AUTHORIZE_URL = f"{IG_LOGIN_API_BASE}/oauth/authorize"
TOKEN_URL = f"{IG_LOGIN_API_BASE}/oauth/access_token"
LONG_LIVED_URL = f"{IG_LOGIN_GRAPH_BASE}/access_token"
REFRESH_URL = f"{IG_LOGIN_GRAPH_BASE}/refresh_access_token"
#: Who the long-lived token belongs to — the real Meta id and the handle.
PROFILE_URL = f"{IG_LOGIN_GRAPH_BASE}/me"

PROVIDER = "ig_login"

#: `ck_oauth_state_purpose`'s closed set.
PURPOSES = ("connect", "reconnect", "signin", "link")


class OAuthStateRefused(StorydumpError):
    """A state could not be issued or consumed, with the reason NAMED.

    Every refusal on this path says which rule it broke. `rowcount` cannot
    discriminate a replay from an expiry from a wrong-workspace callback, and
    on a credential path the difference decides whether an operator hunts a
    bug or a break-in.
    """


class CredentialUndecryptable(StorydumpError):
    """No key in the ring decrypts this payload.

    `07` §3: fail CLOSED. The credential flips `expired` and the account flips
    `reauth_required`; the re-auth path recovers it. Never guess, never log
    ciphertext.
    """


def new_state() -> str:
    """128-bit urlsafe random, per the column's own comment."""
    return secrets.token_urlsafe(16)


def hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def authorization_url(state: str, *, redirect_uri: str, client_id: str) -> str:
    from urllib.parse import urlencode

    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": ",".join(REQUIRED_SCOPES),
            "response_type": "code",
            "state": state,
        }
    )


async def issue_state(
    conn,
    *,
    purpose: str,
    user_id=None,
    workspace_id=None,
    reconnect_target=None,
    cookie_nonce: Optional[str] = None,
    provider: str = PROVIDER,
) -> str:
    """Mint one state row and return the state value.

    For ``reconnect``, prior live states for the same target are invalidated in
    THIS transaction — `07` §2's "last issued wins", which is a property of the
    issue path rather than of the callback path. Doing it at consume time was
    the pass-2 error: two independently issued rows never consumed one another,
    so both callbacks could land.
    """
    if purpose not in PURPOSES:
        raise OAuthStateRefused(f"unknown purpose {purpose!r}")
    if purpose == "reconnect" and reconnect_target is None:
        raise OAuthStateRefused("reconnect requires a reconnect_target")

    if reconnect_target is not None:
        # Last issued wins, for EVERY purpose that pins a target (`07` §2 says
        # it of reconnect; two live `connect` states for one destination,
        # consented with two different Instagram accounts, would let the
        # second callback re-point the row — so a connect retires its
        # predecessors exactly as a reconnect does).
        await conn.execute(
            text(
                "UPDATE oauth_states SET consumed_at = now()"
                " WHERE reconnect_target = :target AND provider = :provider"
                "   AND consumed_at IS NULL"
            ),
            {"target": str(reconnect_target), "provider": provider},
        )

    state = new_state()
    await conn.execute(
        text(
            "INSERT INTO oauth_states"
            " (state, user_id, workspace_id, provider, purpose, reconnect_target,"
            "  cookie_nonce_hash, expires_at)"
            " VALUES (:state, :uid, :ws, :provider, :purpose, :target, :nonce,"
            "         now() + make_interval(secs => :ttl))"
        ),
        {
            "state": state,
            "uid": None if user_id is None else str(user_id),
            "ws": None if workspace_id is None else str(workspace_id),
            "provider": provider,
            "purpose": purpose,
            "target": None if reconnect_target is None else str(reconnect_target),
            "nonce": None if cookie_nonce is None else hash_nonce(cookie_nonce),
            "ttl": STATE_TTL_SECONDS,
        },
    )
    return state


async def consume_state(
    conn,
    *,
    state: str,
    expected_workspace_id=None,
    cookie_nonce: Optional[str] = None,
    expected_provider: Optional[str] = None,
    expected_purpose=None,
) -> dict[str, Any]:
    """One-shot CAS consume. Returns the row, or raises a NAMED refusal.

    ``expected_provider`` and ``expected_purpose`` (one purpose, or a set of
    them) refuse BY NAME a state minted for another leg — a sign-in state
    replayed into the Drive callback, or the reverse — before the caller reads
    a row it must not act on. The state is consumed either way: a refused
    replay burns it exactly as a cross-workspace one does.

    `07` §2: *"a consumed/expired/unknown state is rejected cold."* The three
    are deliberately NOT distinguished to the caller in one query — the CAS
    either matches a live row or it does not — but they ARE distinguished in
    the refusal message, by a second read that runs only on the failure path.
    That read is an existence check on a value the caller already supplied, so
    it discloses nothing it did not already know (`07` §5).
    """
    result = await conn.execute(
        text(
            "UPDATE oauth_states SET consumed_at = now()"
            " WHERE state = :state AND consumed_at IS NULL AND expires_at > now()"
            " RETURNING state, user_id, workspace_id, provider, purpose,"
            "           reconnect_target, cookie_nonce_hash"
        ),
        {"state": state},
    )
    row = result.mappings().first()
    if row is None:
        raise OAuthStateRefused(_why_not_live(await _peek(conn, state)))

    row = dict(row)

    # Cross-workspace: the row PINS the workspace, so a callback cannot be
    # replayed into a different one. Checked at callback as well as at issue,
    # which is what `07` §2 requires.
    if expected_workspace_id is not None and str(row["workspace_id"]) != str(
        expected_workspace_id
    ):
        raise OAuthStateRefused(
            "cross-workspace callback: this state was issued for a different "
            "workspace and will not be honoured here"
        )

    if expected_provider is not None and row["provider"] != expected_provider:
        raise OAuthStateRefused(
            f"wrong provider: this state was issued for {row['provider']!r},"
            f" not {expected_provider!r}, and will not be honoured here"
        )
    if expected_purpose is not None:
        allowed = (
            {expected_purpose}
            if isinstance(expected_purpose, str)
            else set(expected_purpose)
        )
        if row["purpose"] not in allowed:
            raise OAuthStateRefused(
                f"wrong purpose: this state was issued for {row['purpose']!r},"
                f" not {sorted(allowed)}, and will not be honoured here"
            )

    if row["purpose"] == "signin":
        if cookie_nonce is None or hash_nonce(cookie_nonce) != row["cookie_nonce_hash"]:
            raise OAuthStateRefused(
                "anonymous-state CSRF check failed: the browser presented no "
                "matching nonce cookie for this state"
            )
    return row


async def _peek(conn, state: str) -> Optional[dict]:
    result = await conn.execute(
        text(
            "SELECT consumed_at, expires_at <= now() AS is_expired"
            " FROM oauth_states WHERE state = :state"
        ),
        {"state": state},
    )
    row = result.mappings().first()
    return None if row is None else dict(row)


def _why_not_live(peeked: Optional[dict]) -> str:
    if peeked is None:
        return "unknown state: no such row"
    if peeked["consumed_at"] is not None:
        return (
            "state already consumed: a state is single-use, so this is a replay "
            "(or a reconnect superseded by a newer one)"
        )
    if peeked["is_expired"]:
        return "state expired"
    return "state not live"


async def reap_expired_states(conn, *, limit: int = 500) -> int:
    """`reap_expired`'s `oauth_states` class (`02` §5 staging rule).

    Deletes rows that are past expiry OR already consumed — a consumed row has
    served its whole purpose and is only evidence after that. Bounded, because
    an unbounded delete on a table the ingress path writes to is a lock-hold
    nobody scheduled.
    """
    result = await conn.execute(
        text(
            "DELETE FROM oauth_states WHERE state IN ("
            "  SELECT state FROM oauth_states"
            "  WHERE expires_at <= now() OR consumed_at IS NOT NULL"
            "  ORDER BY created_at LIMIT :lim)"
        ),
        {"lim": limit},
    )
    return result.rowcount


def refresh_params(current_token: str) -> dict[str, str]:
    """The IG-host refresh shape, and ONLY that shape.

    `graph.instagram.com/refresh_access_token` accepts the token alone. The
    legacy implementation had to branch here because a FB-host credential
    needed `fb_exchange_token` + client id/secret, and getting it wrong
    produced Meta error 101. The target cannot reach that host: under FC-7
    `ck_credentials_provider` has no `fb_login_legacy` value, so no
    FB-vintage credential exists to refresh. The lesson is kept; the branch is
    structurally unnecessary.
    """
    return {"grant_type": "ig_refresh_token", "access_token": current_token}


# ---------------------------------------------------------------------------
# Credentials under the MultiFernet ring (`07` §3)
# ---------------------------------------------------------------------------


def ring():
    """The ONE ring door in the tier. Every credential writer and reader —
    `ig_login`'s here, the Drive leg's in `google_drive_oauth` and
    `drive_credentials` — encrypts and decrypts through this, so a ring change
    lands once. `07` §3 keeps the shipped env name `ENCRYPTION_KEYS`; the
    import is lazy so `cryptography` loads on first use, not at import."""
    from src.utils.encryption import TokenEncryption

    return TokenEncryption()


async def store_credential(
    conn, *, workspace_id, ig_account_id, token: str, expires_at=None
) -> str:
    """Write the destination's `ig_login` credential — or, on a reconnect,
    replace the one it already has IN PLACE (`uq_credential_per_account`
    admits one row per account and provider; same id, no gap, no second
    row — `07` §2). Encrypted with ring key 0. Returns the id.

    The Drive leg's `store_credential` has the same shape; the conflict
    target repeats the partial index's predicate because Postgres cannot
    infer a partial unique index without it."""
    result = await conn.execute(
        text(
            "INSERT INTO oauth_credentials"
            " (workspace_id, ig_account_id, provider, encrypted_payload,"
            "  expires_at, next_refresh_at, state)"
            # next_refresh_at = now() + the `05` row-56 cadence (7 days from
            # issue), NOT now(): Meta refuses to refresh a long-lived token
            # younger than 24 hours (`0.4` — the recorded min-age floor), and an
            # immediately-due refresh would be a definitive 400, which the
            # refresh leg rightly treats as "dead" and flips the account to
            # reauth_required minutes after it was connected. Armed, so the
            # credential is visible to the refresh leg; late enough to clear
            # the floor. The clock re-arms the same cadence after each refresh.
            " VALUES (:ws, :acct, :provider, :payload, :exp,"
            f"         now() + interval '{FIRST_REFRESH_INTERVAL}', 'active')"
            " ON CONFLICT (workspace_id, ig_account_id, provider)"
            "   WHERE ig_account_id IS NOT NULL"
            " DO UPDATE SET encrypted_payload = EXCLUDED.encrypted_payload,"
            "               expires_at = EXCLUDED.expires_at,"
            f"               next_refresh_at = now() + interval '{FIRST_REFRESH_INTERVAL}',"
            "               state = 'active'"
            " RETURNING id"
        ),
        {
            "ws": str(workspace_id),
            "acct": str(ig_account_id),
            "provider": PROVIDER,
            "payload": ring().encrypt(token),
            "exp": expires_at,
        },
    )
    return str(result.scalar_one())


async def swap_credential(conn, *, credential_id, token: str, expires_at=None) -> None:
    """Reconnect: replace the payload IN PLACE, same row id.

    `07` §2 — *"no window where the account has zero credentials"*. Deleting and
    re-inserting would open exactly that window, and it is the kind of window
    nothing in the test suite would notice.
    """
    result = await conn.execute(
        text(
            "UPDATE oauth_credentials"
            " SET encrypted_payload = :payload, expires_at = :exp, state = 'active'"
            # A revoked credential (a removed destination's) stays revoked: a
            # refresh that was in flight when the account was removed must not
            # bring the token back to life. `expired` → `active` IS allowed —
            # that is the reconnect edge (D31).
            " WHERE id = :cid AND state <> 'revoked'"
        ),
        {
            "cid": str(credential_id),
            "payload": ring().encrypt(token),
            "exp": expires_at,
        },
    )
    if result.rowcount == 0:
        raise OAuthStateRefused(f"no credential {credential_id} to swap")


async def credential_state(conn, *, credential_id) -> Optional[str]:
    """The credential's state, or None when there is no such row — read
    before a refresh so a credential revoked since the job was minted is left
    alone (`refresh_credential`'s "stale" outcome)."""
    row = (
        await conn.execute(
            text("SELECT state FROM oauth_credentials WHERE id = :cid"),
            {"cid": str(credential_id)},
        )
    ).first()
    return None if row is None else row[0]


async def load_credential(conn, *, credential_id) -> str:
    """Decrypt, or FAIL CLOSED.

    `07` §3: a payload no ring entry decrypts flips the credential `expired`
    and the account `reauth_required`. It never guesses and never logs
    ciphertext — the exception carries the credential id and nothing else.
    """
    result = await conn.execute(
        text("SELECT encrypted_payload FROM oauth_credentials WHERE id = :cid"),
        {"cid": str(credential_id)},
    )
    row = result.first()
    if row is None:
        raise OAuthStateRefused(f"no credential {credential_id}")
    try:
        return ring().decrypt(row[0])
    except Exception as exc:
        # COMMIT the flip before raising. Fail-closed means the state change
        # SURVIVES the failure — if it rides on the caller's transaction it is
        # rolled back with the exception, and the credential stays `active`
        # while nothing can read it. Caught by the test asserting the flip
        # rather than asserting only that the call raised.
        await mark_dead(conn, credential_id=credential_id)
        await conn.commit()
        raise CredentialUndecryptable(
            f"credential {credential_id} decrypts under no key in the ring; "
            "flipped expired and the account to reauth_required"
        ) from exc


async def mark_dead(conn, *, credential_id) -> None:
    """The D31 slice that IS L.6's: the credential and account state flip.

    The other two halves — the dispatcher minting no further intents, and the
    publish pipeline recording the fault — are L.7's and L.5's. Issue #863 says
    so, and this function deliberately does not reach into either.
    """
    result = await conn.execute(
        text(
            "UPDATE oauth_credentials SET state = 'expired'"
            # Only a LIVE credential dies here: a revoked one (a removed
            # destination's) is already dead and must not be relabelled.
            " WHERE id = :cid AND state = 'active'"
            " RETURNING ig_account_id"
        ),
        {"cid": str(credential_id)},
    )
    row = result.first()
    if row is None or row[0] is None:
        return
    await conn.execute(
        text(
            "UPDATE ig_accounts SET state = 'reauth_required'"
            # From `active` only: a `disabled` (removed) row must not come
            # back to the list as "reconnect needed" because a stale refresh
            # job ran after the removal.
            " WHERE id = :acct AND state = 'active'"
        ),
        {"acct": str(row[0])},
    )


# ---------------------------------------------------------------------------
# The connect leg (#1220 step 2 / #1041): the code exchange and the route's
# three-way "which state do I mint" — the Drive leg's shape, on Instagram.
# ---------------------------------------------------------------------------

#: The closed set of exchange refusals. They stay distinct in the LOG — which
#: of the three provider calls failed, and how — and all collapse to one
#: `exchange_failed` on the error page, because to the person every one of
#: them is "the last step did not complete" (the callback does that collapse).
REASONS = (
    "exchange_failed",
    "malformed_response",
    "long_lived_failed",
    "profile_failed",
)


class IgOAuthRefused(StorydumpError):
    """The grant could not be completed. ``reason`` is one of :data:`REASONS`,
    and no token ever rides in the message."""

    def __init__(self, reason: str, detail: str = ""):
        if reason not in REASONS:
            raise ValueError(f"unknown IgOAuthRefused reason {reason!r}")
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"instagram grant refused: {reason}" + (f" — {detail}" if detail else "")
        )


@dataclass(frozen=True)
class IgGrant:
    """A long-lived Instagram Login token and who it is for. `ig_user_id` is
    the REAL account id `ig_accounts.provider_account_ref` keys on (054);
    `username` is display, and may be None if the profile read omitted it."""

    access_token: str
    expires_at: Optional[datetime]
    ig_user_id: str
    username: Optional[str]


def _json_object(response: httpx.Response, *, reason: str) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise IgOAuthRefused(reason, "the endpoint did not answer JSON") from exc
    if not isinstance(body, dict):
        raise IgOAuthRefused(reason, "the endpoint body is not a JSON object")
    return body


async def exchange_code(
    client: httpx.AsyncClient,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> IgGrant:
    """Trade the authorization code for a long-lived token and its owner —
    three provider calls, every one through the egress floor, none inside a
    transaction (`02` §5; the caller commits the state consume first).

    1. ``POST api.instagram.com/oauth/access_token`` — code → short-lived token.
       Instagram appends ``#_`` to the code it hands back; it is stripped here,
       the legacy flow's lesson.
    2. ``GET graph.instagram.com/access_token?grant_type=ig_exchange_token`` —
       short-lived → long-lived (60 days; `expires_in` is relative, so the
       expiry is stamped here).
    3. ``GET graph.instagram.com/me?fields=user_id,username`` — who it is for.

    Refuses BY NAME at each step; bodies are never logged (they carry tokens).
    """
    policy = egress.EgressPolicy(timeout_class="standard")

    short = await egress.request(
        client,
        "POST",
        TOKEN_URL,
        policy=policy,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            # Instagram appends a literal `#_` to the code it hands back; strip
            # exactly that suffix — `rstrip` would eat a trailing `_` that is
            # part of the code (the legacy flow's latent defect).
            "code": code.removesuffix("#_"),
        },
    )
    if short.status_code != 200:
        raise IgOAuthRefused(
            "exchange_failed", f"token endpoint answered {short.status_code}"
        )
    body = _json_object(short, reason="malformed_response")
    # Instagram Login wraps the answer in a one-element `data` list.
    token_data = (
        body["data"][0] if isinstance(body.get("data"), list) and body["data"] else body
    )
    short_token = (
        token_data.get("access_token") if isinstance(token_data, dict) else None
    )
    if not isinstance(short_token, str) or not short_token:
        raise IgOAuthRefused(
            "malformed_response", "no access token in the code exchange"
        )

    long_lived = await egress.request(
        client,
        "GET",
        LONG_LIVED_URL,
        policy=policy,
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": client_secret,
            "access_token": short_token,
        },
    )
    if long_lived.status_code != 200:
        raise IgOAuthRefused(
            "long_lived_failed",
            f"long-lived exchange answered {long_lived.status_code}",
        )
    body = _json_object(long_lived, reason="malformed_response")
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise IgOAuthRefused(
            "malformed_response", "no access token in the long-lived exchange"
        )
    expires_at = None
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    profile = await egress.request(
        client,
        "GET",
        PROFILE_URL,
        policy=policy,
        params={"fields": "user_id,username", "access_token": token},
    )
    if profile.status_code != 200:
        raise IgOAuthRefused(
            "profile_failed", f"profile read answered {profile.status_code}"
        )
    body = _json_object(profile, reason="malformed_response")
    ig_user_id = body.get("user_id")
    if ig_user_id is None or str(ig_user_id).strip() == "":
        raise IgOAuthRefused("malformed_response", "no user_id in the profile")
    username = body.get("username")
    return IgGrant(
        access_token=token,
        expires_at=expires_at,
        ig_user_id=str(ig_user_id),
        username=username if isinstance(username, str) and username else None,
    )


async def connect_purpose(conn, *, workspace_id, ig_account_id) -> Optional[str]:
    """Which state the connect route mints for this destination, in one query:
    ``None`` when the account is not this workspace's (the route answers 404 —
    never 403, since a destination's existence is not disclosed across
    tenants), ``"connect"`` when it has never been credentialed, ``"reconnect"``
    once it has — so `issue_state`'s last-issued-wins retires a stale state."""
    credentialed = (
        await conn.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM oauth_credentials c"
                "   WHERE c.ig_account_id = a.id AND c.workspace_id = a.workspace_id"
                "     AND c.provider = :provider"
                ") AS credentialed"
                "  FROM ig_accounts a"
                " WHERE a.id = :acct AND a.workspace_id = :ws AND a.state <> 'moved'"
            ),
            {"acct": str(ig_account_id), "ws": str(workspace_id), "provider": PROVIDER},
        )
    ).scalar()
    if credentialed is None:
        return None
    return "reconnect" if credentialed else "connect"
