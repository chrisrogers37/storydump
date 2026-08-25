"""Identity — `users` + `user_identities` (`02` §1, `07` §1's D32/D35).

The one writer of a human's identity row. Keyed on the provider's IMMUTABLE
SUBJECT — `(provider, external_id)`, the Google OIDC `sub` — never on the
email address (D32): emails are mutable and recyclable, so identity keyed on
email is an account-takeover primitive. The verified email claim is metadata,
refreshed at every sign-in; `users.primary_email` fills from it when NULL; a
claim colliding with a DIFFERENT user's `primary_email` surfaces as an error
and never merges accounts (D35 — merging two populated users is an operator
action with an audit trail, out of v1).

Both tables are user-plane (`058` class 3: role-scoped `USING (true)`), so
this runs before any `app.tenant_id` exists — identity precedes tenancy.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import readers

PROVIDER_GOOGLE = "google"


class IdentityCollision(StorydumpError):
    """The verified email belongs to a different user. Refused, never merged."""


async def upsert_google_identity(
    executor, *, sub: str, email: Optional[str], display_name: Optional[str]
) -> str:
    """Find-or-create the user for a verified Google subject. Returns user_id.

    Serialized per subject with a transaction-scoped advisory lock, so two
    concurrent first sign-ins for the same `sub` cannot both insert (the
    second would otherwise create an orphan `users` row and then lose on
    `uq_identity_per_provider`). *email* is the VERIFIED claim or None —
    `google_oidc.verify_id_token` already drops an unverified one, so this
    function never sees a claim it must doubt.
    """
    if not sub:
        raise ValueError("sub is required")
    claim = email or None

    await executor.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"identity:{PROVIDER_GOOGLE}:{sub}"},
    )
    row = (
        await executor.execute(
            text(
                "SELECT i.user_id, u.primary_email"
                "  FROM user_identities i JOIN users u ON u.id = i.user_id"
                " WHERE i.provider = :p AND i.external_id = :sub"
            ),
            {"p": PROVIDER_GOOGLE, "sub": sub},
        )
    ).first()

    if row is not None:
        user_id, held = str(row[0]), row[1]
        await executor.execute(
            text(
                "UPDATE user_identities SET verified_at = now(), display_name = :dn"
                " WHERE provider = :p AND external_id = :sub"
            ),
            {"dn": display_name, "p": PROVIDER_GOOGLE, "sub": sub},
        )
        if claim is not None and held is None:
            await _fill_primary_email(executor, user_id=user_id, email=claim)
        return user_id

    if claim is not None:
        await _refuse_if_held_elsewhere(executor, email=claim, user_id=None)
    user_id = str(
        (
            await executor.execute(
                text("INSERT INTO users (primary_email) VALUES (:e) RETURNING id"),
                {"e": claim},
            )
        ).scalar_one()
    )
    await executor.execute(
        text(
            "INSERT INTO user_identities"
            " (user_id, provider, external_id, display_name, verified_at)"
            " VALUES (:u, :p, :sub, :dn, now())"
        ),
        {"u": user_id, "p": PROVIDER_GOOGLE, "sub": sub, "dn": display_name},
    )
    return user_id


async def _refuse_if_held_elsewhere(
    executor, *, email: str, user_id: Optional[str]
) -> None:
    holder = (
        await executor.execute(
            text("SELECT id FROM users WHERE primary_email = :e"), {"e": email}
        )
    ).first()
    if holder is not None and (user_id is None or str(holder[0]) != user_id):
        raise IdentityCollision(
            "the verified email belongs to a different account; accounts are never merged"
        )


async def _fill_primary_email(executor, *, user_id: str, email: str) -> None:
    """Fill `primary_email` when NULL; leave a populated one alone (email is a
    claim, not an edit); refuse the fill if another user holds it."""
    await _refuse_if_held_elsewhere(executor, email=email, user_id=user_id)
    await executor.execute(
        text(
            "UPDATE users SET primary_email = :e"
            " WHERE id = :u AND primary_email IS NULL"
        ),
        {"e": email, "u": user_id},
    )


async def get_user(executor, *, user_id: str) -> Optional[dict]:
    """The user row plus its identities — `/me`'s user half."""
    user = await readers.row(
        executor,
        "SELECT id, primary_email, state, created_at FROM users WHERE id = :u",
        u=str(user_id),
    )
    if user is None:
        return None
    user["identities"] = await readers.rows(
        executor,
        "SELECT provider, display_name, verified_at, created_at"
        "  FROM user_identities WHERE user_id = :u ORDER BY created_at",
        u=str(user_id),
    )
    return user
