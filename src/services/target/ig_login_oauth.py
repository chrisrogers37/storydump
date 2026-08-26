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

import hashlib
import logging
import secrets
from typing import Any, Optional

from sqlalchemy import text

from src.config.constants import IG_LOGIN_API_BASE, IG_LOGIN_GRAPH_BASE
from src.exceptions import StorydumpError

logger = logging.getLogger(__name__)

#: `05`: state token TTL, 15 minutes, every purpose. Legacy used 600s.
STATE_TTL_SECONDS = 900

#: The scopes the proven flow requests. Carried over verbatim — #410's
#: background turns on the app's *use case*, not on this list.
REQUIRED_SCOPES = ("instagram_business_basic", "instagram_business_content_publish")

AUTHORIZE_URL = f"{IG_LOGIN_API_BASE}/oauth/authorize"
TOKEN_URL = f"{IG_LOGIN_API_BASE}/oauth/access_token"
LONG_LIVED_URL = f"{IG_LOGIN_GRAPH_BASE}/access_token"
REFRESH_URL = f"{IG_LOGIN_GRAPH_BASE}/refresh_access_token"

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

    if purpose == "reconnect":
        await conn.execute(
            text(
                "UPDATE oauth_states SET consumed_at = now()"
                " WHERE purpose = 'reconnect' AND reconnect_target = :target"
                "   AND consumed_at IS NULL"
            ),
            {"target": str(reconnect_target)},
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
    """Insert one `ig_login` credential, encrypted with ring key 0."""
    result = await conn.execute(
        text(
            "INSERT INTO oauth_credentials"
            " (workspace_id, ig_account_id, provider, encrypted_payload,"
            "  expires_at, next_refresh_at)"
            # next_refresh_at = now(): immediately due, so the next clock tick
            # mints a refresh and that mint re-arms the cadence. Without this
            # a stored credential is invisible to the refresh leg forever.
            " VALUES (:ws, :acct, :provider, :payload, :exp, now()) RETURNING id"
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
            " WHERE id = :cid"
        ),
        {
            "cid": str(credential_id),
            "payload": ring().encrypt(token),
            "exp": expires_at,
        },
    )
    if result.rowcount == 0:
        raise OAuthStateRefused(f"no credential {credential_id} to swap")


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
            " WHERE id = :cid AND state <> 'expired'"
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
            " WHERE id = :acct AND state <> 'reauth_required'"
        ),
        {"acct": str(row[0])},
    )
