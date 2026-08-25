"""X.3 — the identity writer: a verified provider subject becomes a user
(`07` §1, D32/D35).

The first of the two mints sign-in performs. It creates `users` and
`user_identities` and **nothing else** — no workspace, no session. Tenancy is
an explicit act at `workspace_provisioning`, and the session is minted by
`web_sessions`; sign-in composes the three at the edge. Keeping them separate
is what lets a returning user sign in without touching tenancy at all.

**KEYED ON THE SUBJECT, NEVER ON THE EMAIL (D32).** `external_id` is the
provider's immutable subject — the OIDC `sub` for Google. Emails are mutable
and recyclable, so identity keyed on email is an account-takeover primitive.
The verified email claim is metadata: it fills `users.primary_email` when that
is NULL and never overwrites a value already there.

**A COLLIDING EMAIL IS AN ERROR, NEVER A MERGE (D35 — stated in full on
`IdentityProvisioningError`).** The refusal is raised BY CATCHING
`uq_users_primary_email` rather than by a prior lookup that could race, so the
app and the database agree by construction rather than by both remembering.

**The returning path is ONE statement.** Finding the identity and refreshing
it are the same UPDATE: its `RETURNING` yields the ids the lookup existed for,
plus whether the user's email slot is still empty, and a miss returns zero
rows — the same signal the create path already branches on. That is not only
cheaper (a returning sign-in went from five round-trips to one), it narrows
the read-then-write window, because the UPDATE's row lock is strictly stronger
than the non-locking SELECT it replaces.

**Speculative writes run inside a SAVEPOINT** (`sync_tx.savepoint`), for two
reasons and the second is load-bearing. The first is the race: two browser
tabs completing sign-in at once both miss, and the loser must recover rather
than crash — `uq_identity_per_provider` is what makes that safe, so the loser
re-reads and returns the winner's row. The second is that a caller receiving
`email_belongs_to_another` needs its transaction still usable to render the
page that says so; a bare integrity error poisons the transaction, and a
caller that cannot continue learns to catch and ignore.

**No GUCs, and none are needed.** `users` and `user_identities` are `02` §7's
class-3 user plane: no workspace key exists, because identity precedes
tenancy. Their policies are row-open to `svc_ingress`, and neither table
carries a governance-audit trigger — verified against the live production
schema, where the only triggers on either are `trg_touch_updated_at`.
"""

from dataclasses import dataclass
from typing import Optional

from src.exceptions.identity import IdentityProvisioningError
from src.services.target._dbapi import constraint_violated
from src.services.target.sync_tx import require_transaction, savepoint

#: `02` §1 `ck_user_identities_provider`. Google is the one non-Telegram
#: provider X.3 ships; Apple re-entry is one CHECK value plus one flow.
GOOGLE = "google"

#: One prose home for the D35 refusal, so the two raise sites cannot drift.
_D35_DETAIL = (
    "the verified email claim is another user's primary_email"
    " — accounts are never merged on an email (D35)"
)


@dataclass(frozen=True)
class ProvisionedIdentity:
    """The result of a sign-in's identity leg. ``created`` distinguishes a
    first sign-in from a returning one — the edge routes onboarding on it, and
    it is a fact about THIS call rather than about the row's age."""

    user_id: str
    identity_id: str
    created: bool


def _email_collision() -> IdentityProvisioningError:
    return IdentityProvisioningError("email_belongs_to_another", _D35_DETAIL)


def _find_and_refresh(conn, subject: str, display_name: Optional[str]):
    """Refresh the identity for *subject* and return
    ``(identity_id, user_id, email_slot_is_empty)``, or None if there is none.

    An absent *display_name* leaves the stored one alone rather than erasing
    it: a provider that stops sending a name has not told us the user lost it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_identities ui"
            "   SET verified_at = now(),"
            "       display_name = COALESCE(%s, ui.display_name)"
            "  FROM users u"
            " WHERE ui.provider = %s AND ui.external_id = %s AND u.id = ui.user_id"
            " RETURNING ui.id, ui.user_id, u.primary_email IS NULL",
            (display_name, GOOGLE, subject),
        )
        return cur.fetchone()


def _fill_primary_email(conn, user_id: str, email: str) -> None:
    """Fill `users.primary_email` iff it is still NULL.

    The NULL guard is in the WHERE clause and not only in the caller's
    fast-path check: the check skips work, the clause is the correctness. A
    provider changing a user's email must not repoint an account that already
    has one, and a read used as a guard would race.
    """
    try:
        with savepoint(conn) as cur:
            cur.execute(
                "UPDATE users SET primary_email = %s"
                " WHERE id = %s AND primary_email IS NULL",
                (email, str(user_id)),
            )
    except Exception as exc:
        if constraint_violated(exc, "uq_users_primary_email"):
            raise _email_collision() from exc
        raise


def _returning(conn, row, email: Optional[str]) -> ProvisionedIdentity:
    identity_id, user_id, email_slot_empty = row
    if email and email_slot_empty:
        _fill_primary_email(conn, str(user_id), email)
    return ProvisionedIdentity(
        user_id=str(user_id), identity_id=str(identity_id), created=False
    )


def upsert_google_identity(
    conn,
    *,
    subject: str,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
) -> ProvisionedIdentity:
    """Find-or-create the user behind a VERIFIED Google OIDC subject.

    *subject* is the `sub` claim, already verified against Google's JWKS by
    the caller — this function performs no verification and must never be
    handed an unverified value.

    Returning user: the identity row is refreshed and `primary_email` filled
    if still empty. First sign-in: both rows are created together, so a user
    with no identity cannot exist.
    """
    require_transaction(conn)
    if not subject or not str(subject).strip():
        raise IdentityProvisioningError("missing_subject", "empty OIDC subject")

    row = _find_and_refresh(conn, subject, display_name)
    if row is not None:
        return _returning(conn, row, email)

    try:
        with savepoint(conn) as cur:
            cur.execute(
                "INSERT INTO users (primary_email) VALUES (%s) RETURNING id", (email,)
            )
            user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_identities"
                " (user_id, provider, external_id, display_name, verified_at)"
                " VALUES (%s, %s, %s, %s, now()) RETURNING id",
                (user_id, GOOGLE, subject, display_name),
            )
            identity_id = cur.fetchone()[0]
        return ProvisionedIdentity(
            user_id=str(user_id), identity_id=str(identity_id), created=True
        )
    except Exception as exc:
        if constraint_violated(exc, "uq_users_primary_email"):
            raise _email_collision() from exc
        if not constraint_violated(exc, "uq_identity_per_provider"):
            raise
        # Lost a race with a concurrent sign-in for the same subject. The
        # winner's rows are committed by now — the INSERT blocked on the index
        # until they were — but a miss here is treated as a miss rather than
        # asserted, since an assertion would turn a surprising database into a
        # crash at sign-in.
        row = _find_and_refresh(conn, subject, display_name)
        if row is None:
            raise
        return _returning(conn, row, email)
