"""The tier's `oauth_states` door, and the credential ring beside it.

Split out of :mod:`src.services.target.ig_login_oauth` by the tech-debt audit
(2026-09-20, TD-B15). None of this is Instagram's: the state row is the tier's
ONE replay-protected handoff between "we sent someone to a provider" and "a
callback came back", and Google identity linking, Google Drive OAuth and
Telegram channel binding were all reaching into a module named "Instagram
Login OAuth" to get at it. What stayed behind in that module is what its name
says — the Instagram token exchange, refresh and credential rows.

## The state row, not a signed token

`07` §2. A state is a ROW in `oauth_states`, consumed by a one-shot CAS,
because replay protection has to be a fact about storage: a stateless
self-describing token cannot be single-use. Every refusal on this path says
which rule it broke (:class:`OAuthStateRefused`) — `rowcount` cannot
discriminate a replay from an expiry from a wrong-workspace callback, and on a
credential path the difference decides whether an operator hunts a bug or a
break-in.

## "Last issued wins" is a property of the ISSUE path

`07` §2 records that the pass-2 "last consumed wins" claim was false —
independently issued rows never consumed one another, so both callbacks could
land. :func:`retire_live_states` is the one statement that enforces it, and
:func:`issue_state` runs it in the issuing transaction so that at most one
live state exists per target at any commit.

## Why :func:`ring` is here

It is the other half of what the non-Instagram importers were taking from
`ig_login_oauth`: the Drive leg's `google_drive_oauth` and `drive_credentials`
want the encryption ring, not the Instagram flow. It is ONE door on purpose —
every credential writer and reader in the tier encrypts and decrypts through
it, so a ring change lands once.

## PKCE rides the state row

`07` §51, RFC 7636. A leg whose authorization URL carries a PKCE challenge
mints the verifier with its state (:func:`new_code_verifier`), sends only its
S256 challenge (:func:`code_challenge`), and :func:`issue_state` stores the
verifier on the row ENCRYPTED under :func:`ring`. The callback reads it back
from the row its one-shot consume returned (:func:`code_verifier_of`) and
sends it with the code (RFC 7636 §4.5). The verifier never reaches the browser.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any, Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import vocabulary


#: `05`: state token TTL, 15 minutes, every purpose. Legacy used 600s.
STATE_TTL_SECONDS = 900

#: `ck_oauth_state_purpose`'s closed set.
PURPOSES = ("connect", "reconnect", "signin", "link", "bind")


class OAuthStateRefused(StorydumpError):
    """A state could not be issued or consumed, with the reason NAMED.

    Every refusal on this path says which rule it broke. `rowcount` cannot
    discriminate a replay from an expiry from a wrong-workspace callback, and
    on a credential path the difference decides whether an operator hunts a
    bug or a break-in.
    """


def new_state() -> str:
    """128-bit urlsafe random, per the column's own comment."""
    return secrets.token_urlsafe(16)


def hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


async def retire_live_states(
    conn,
    *,
    provider: Optional[str] = None,
    purpose: Optional[str] = None,
    user_id=None,
    workspace_id=None,
    reconnect_target=None,
) -> int:
    """Consume every live state this selector matches. Returns the count.

    "Last issued wins" (`07` §2) is one security rule with four writers: a
    link mint, a bind mint, :func:`issue_state`'s own reconnect-target retire
    and `disable_destination`'s. Four spellings is how one of them keeps a
    state tappable after the next is minted. A removal (`07` §45) is the
    fifth caller: it spends whatever the removed person holds for one
    workspace, so it selects by user AND workspace and names no provider.
    The selectors given are ANDed, and a user, a workspace or a reconnect
    target is required: a provider or a purpose narrows a selection and is
    never one on its own. The statement is built from named fragments, never
    from a caller's string.
    """
    if user_id is None and workspace_id is None and reconnect_target is None:
        raise ValueError(
            "retire_live_states needs a user, a workspace or a reconnect target"
        )
    where = ["consumed_at IS NULL"]
    params: dict[str, Any] = {}
    if provider is not None:
        where.append("provider = :provider")
        params["provider"] = provider
    if purpose is not None:
        where.append("purpose = :purpose")
        params["purpose"] = purpose
    if user_id is not None:
        where.append("user_id = :uid")
        params["uid"] = str(user_id)
    if workspace_id is not None:
        where.append("workspace_id = :ws")
        params["ws"] = str(workspace_id)
    if reconnect_target is not None:
        where.append("reconnect_target = :target")
        params["target"] = str(reconnect_target)
    result = await conn.execute(
        text(
            "UPDATE oauth_states SET consumed_at = now() WHERE " + " AND ".join(where)
        ),
        params,
    )
    return result.rowcount


async def issue_state(
    conn,
    *,
    purpose: str,
    user_id=None,
    workspace_id=None,
    reconnect_target=None,
    cookie_nonce: Optional[str] = None,
    provider: str = vocabulary.PROVIDER_IG_LOGIN,
    code_verifier: Optional[str] = None,
) -> str:
    """Mint one state row and return the state value.

    For ``reconnect``, prior live states for the same target are invalidated in
    THIS transaction — `07` §2's "last issued wins", which is a property of the
    issue path rather than of the callback path. Doing it at consume time was
    the pass-2 error: two independently issued rows never consumed one another,
    so both callbacks could land.

    ``code_verifier``, when given, is stored ENCRYPTED under :func:`ring` and
    read back by :func:`code_verifier_of` (`07` §51); a state minted without
    one needs no ring.
    """
    if purpose not in PURPOSES:
        raise OAuthStateRefused(f"unknown purpose {purpose!r}")
    if purpose == "reconnect" and reconnect_target is None:
        raise OAuthStateRefused("reconnect requires a reconnect_target")
    # Before any statement: a ring that cannot be built refuses the mint whole.
    sealed = None if code_verifier is None else ring().encrypt(code_verifier)

    if reconnect_target is not None:
        # Last issued wins, for EVERY purpose that pins a target (`07` §2 says
        # it of reconnect; two live `connect` states for one destination,
        # consented with two different Instagram accounts, would let the
        # second callback re-point the row — so a connect retires its
        # predecessors exactly as a reconnect does).
        await retire_live_states(
            conn, provider=provider, reconnect_target=reconnect_target
        )

    state = new_state()
    await conn.execute(
        text(
            "INSERT INTO oauth_states"
            " (state, user_id, workspace_id, provider, purpose, reconnect_target,"
            "  cookie_nonce_hash, encrypted_code_verifier, expires_at)"
            " VALUES (:state, :uid, :ws, :provider, :purpose, :target, :nonce,"
            "         :verifier, now() + make_interval(secs => :ttl))"
        ),
        {
            "state": state,
            "uid": None if user_id is None else str(user_id),
            "ws": None if workspace_id is None else str(workspace_id),
            "provider": provider,
            "purpose": purpose,
            "target": None if reconnect_target is None else str(reconnect_target),
            "nonce": None if cookie_nonce is None else hash_nonce(cookie_nonce),
            "verifier": sealed,
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
            "           reconnect_target, cookie_nonce_hash, encrypted_code_verifier"
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


async def peek_live_state(
    conn, *, state: str, expected_provider: str, expected_purpose: str
) -> dict[str, Any]:
    """The live row for *state* WITHOUT consuming it, or a NAMED refusal.

    For a flow whose state must survive a first look and be consumed by a
    later, deliberate act — the Telegram identity link shows the tapper whose
    account the link belongs to and consumes the state only on their Confirm
    (:func:`consume_state` then decides, one-shot, as ever). A peek is never
    an authorization: nothing may be written on its strength alone.

    The provider and purpose are part of the lookup rather than checked
    after it, so a state minted for another leg reads exactly as an unknown
    one does — and, unlike :func:`consume_state`, is not burned by the look.
    """
    result = await conn.execute(
        text(
            "SELECT state, user_id, workspace_id, provider, purpose"
            "  FROM oauth_states"
            " WHERE state = :state AND provider = :provider AND purpose = :purpose"
            "   AND consumed_at IS NULL AND expires_at > now()"
        ),
        {"state": state, "provider": expected_provider, "purpose": expected_purpose},
    )
    row = result.mappings().first()
    if row is None:
        raise OAuthStateRefused(_why_not_live(await _peek(conn, state)))
    return dict(row)


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


# ---------------------------------------------------------------------------
# Credentials under the MultiFernet ring (`07` §3)
# ---------------------------------------------------------------------------


class RingUnavailable(StorydumpError):
    """The ring itself cannot be built: no key is configured, or a configured
    key is not a Fernet key.

    A fault of the PROCESS's configuration, never a fact about a credential,
    so nothing may record it as one. The per-credential failure is a payload
    that no key in a WORKING ring decrypts, and four doors classify it:
    `ig_login_oauth.load_credential` (`07` §3's fail-closed flip, committed),
    `ig_credentials.token_for_account` (a dead token, handed to review),
    `drive_credentials.token_for_workspace` (a dead grant: the sources go to
    `error` and the owner is alerted) and the revoke executor (`undecryptable`,
    audited and abandoned). Before this type existed a missing key reached all
    four as the same `ValueError` a corrupt row raises — the first refresh
    after a deploy without `ENCRYPTION_KEY` would have flipped a live Instagram
    account to `reauth_required` and messaged its owner to reconnect. So each
    door builds the ring BEFORE its `try`: this passes the `try`, and each
    door's own comment says where it goes from there.

    Both roots build the ring at startup and refuse to boot on this
    (`src/worker.py` `main`, `src/api/app.py` `_lifespan`), so a deployed
    process that reaches a credential already holds a working ring: Railway's
    health check fails the deploy and the previous one keeps serving. The
    doors' ordering is for every entry point that skips a root.

    The message is `TokenEncryption`'s: it names the variable that was read
    (and a rotation entry's position), never a key.
    """


def ring():
    """The ONE ring door in the tier. Every credential writer and reader —
    `ig_login_oauth`'s, and the Drive leg's in `google_drive_oauth` and
    `drive_credentials` — encrypts and decrypts through this, so a ring change
    lands once. `07` §3 keeps the shipped env name `ENCRYPTION_KEYS`; the
    import is lazy so `cryptography` loads on first use, not at import.

    A ring that cannot be built raises :class:`RingUnavailable`, never the
    bare `ValueError` a per-row decrypt failure also raises: the two are told
    apart by type, not by parsing a message."""
    from src.utils.encryption import TokenEncryption

    try:
        return TokenEncryption()
    except ValueError as exc:
        raise RingUnavailable(str(exc)) from exc


# ---------------------------------------------------------------------------
# PKCE: the verifier minted with a state (RFC 7636, `07` §51)
# ---------------------------------------------------------------------------

#: RFC 7636 §4.1 recommends 32 octets from a CSPRNG, base64url-encoded: 43
#: characters, every one in the unreserved set (§4.1 admits 43 to 128).
CODE_VERIFIER_BYTES = 32

#: The one transform this tier sends (RFC 7636 §4.2) — `plain`, never.
CODE_CHALLENGE_METHOD = "S256"


def new_code_verifier() -> str:
    """A PKCE code verifier: 43 unreserved characters, 256 bits from
    :mod:`secrets` (RFC 7636 §4.1)."""
    return secrets.token_urlsafe(CODE_VERIFIER_BYTES)


def code_challenge(verifier: str) -> str:
    """The S256 challenge of *verifier* — ``BASE64URL(SHA256(ASCII(verifier)))``
    without padding (RFC 7636 §4.2; Appendix B's vector is its pin)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def code_verifier_of(row) -> str:
    """The PKCE verifier stored with a consumed state, decrypted — what the
    code exchange sends beside the code (RFC 7636 §4.5).

    Refused BY NAME when the row carries none (a state minted without one, or
    before the column existed) or carries one no key in the ring decrypts:
    either way there is no verifier to send, and the callback says so before
    it spends the code. A ring that cannot be built is the process's fault,
    never the row's, so it is built outside the ``try`` and passes as
    :class:`RingUnavailable` — the order every decrypt door keeps.
    """
    sealed = row["encrypted_code_verifier"]
    if not sealed:
        raise OAuthStateRefused(
            "no code verifier: this state was minted without one, so there is"
            " none to send with the code returned for it"
        )
    keys = ring()
    try:
        return keys.decrypt(sealed)
    except ValueError as exc:
        # Never the ciphertext in the message: the reason is the whole story.
        raise OAuthStateRefused(
            "code verifier undecryptable: no key in the ring decrypts the one"
            " stored with this state"
        ) from exc
