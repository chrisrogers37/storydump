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
from src.services.target import oauth_states, readers, vocabulary

PROVIDER_GOOGLE = vocabulary.PROVIDER_GOOGLE
PROVIDER_TELEGRAM = vocabulary.PROVIDER_TELEGRAM


class IdentityCollision(StorydumpError):
    """The verified email belongs to a different user. Refused, never merged."""


class SignupNotAdmitted(StorydumpError):
    """A new Google account whose verified email nobody admitted or invited
    (092, `07` §35): no user is created. Sign-in maps it to `not_admitted`."""


class IdentityAlreadyLinked(StorydumpError):
    """This provider identity, or this user's slot for it, is already taken.

    Both directions are refusals and they are NOT the same fact:
    `uq_identity_per_provider` means the external account belongs to someone
    else; `uq_user_provider` means this user already linked a different one.
    Named separately in `reason` so an operator can tell them apart — the
    tapper is told neither (`07` §5).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def upsert_google_identity(
    executor,
    *,
    sub: str,
    email: Optional[str],
    display_name: Optional[str],
    signup_open: bool = False,
) -> str:
    """Find-or-create the user for a verified Google subject. Returns user_id.

    Serialized per subject with a transaction-scoped advisory lock, so two
    concurrent first sign-ins for the same `sub` cannot both insert (the
    second would otherwise create an orphan `users` row and then lose on
    `uq_identity_per_provider`). *email* is the VERIFIED claim or None —
    `google_oidc.verify_id_token` already drops an unverified one, so this
    function never sees a claim it must doubt.

    A subject seen before signs in whatever *signup_open* says. A NEW one
    creates its user only when `fn_signup_admitted` admits its email — an
    owner admission or a live invitation addressed to it (092) from an active
    workspace whose owner or admin still sent it (098) — and is
    refused with `SignupNotAdmitted` otherwise, a None email included.
    *signup_open* (`TARGET_SIGNUP_OPEN`) skips that ask.
    """
    if not sub:
        raise ValueError("sub is required")
    claim = email or None

    # `hashtext` (32-bit), NOT `hashtextextended` (64-bit), and the asymmetry
    # with `provisioning.py` is deliberate rather than an oversight (#1370).
    #
    # A hash collision here can only ever OVER-serialize — two unrelated
    # subjects would share one lock and briefly queue. It can never fail to
    # serialize, because the key is what the lock is on. So the only cost is
    # false contention, and the only question is its rate. At a 32-bit width
    # the chance of ANY collision reaches 1% at ~9,884 distinct keys;
    # `user_identities` held 3 when this was measured (2026-09-22). The
    # provisioning lock took the wide variant because folder keys are dense
    # "at estate scale"; subjects are not, and will not be until the estate
    # has ten thousand identities.
    #
    # Unifying them is NOT a cleanup. Changing the function changes every
    # key's value, so during a rolling deploy old and new processes compute
    # different keys for the same subject and the lock does not hold for the
    # length of the rollout. That needs a quiesce or a dual-take release, so
    # it is a deploy plan, not a patch. `tests/src/services/target/
    # test_advisory_lock_widths.py` is the ratchet that says so.
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
                # COALESCE, not a bare assignment (#1364): `google_oidc` maps an
                # absent, non-string or blank `name` claim to None before it
                # reaches here, so NULL means "this token said nothing about the
                # name" — which is not the same as "the name is now empty", and
                # only the second would justify a write. A bare `= :dn` erased a
                # stored name on every sign-in whose token omitted the claim.
                # `verified_at` stays unconditional: the identity was seen.
                "UPDATE user_identities SET verified_at = now(),"
                "       display_name = COALESCE(:dn, display_name)"
                " WHERE provider = :p AND external_id = :sub"
            ),
            {"dn": display_name, "p": PROVIDER_GOOGLE, "sub": sub},
        )
        if claim is not None and held is None:
            await _fill_primary_email(executor, user_id=user_id, email=claim)
        return user_id

    if not signup_open:
        await _refuse_unless_admitted(executor, email=claim)
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


async def _refuse_unless_admitted(executor, *, email: Optional[str]) -> None:
    """The sign-up gate (092): the door answers for the owner's admissions and
    every workspace's invitations, which this login cannot read itself. Asked
    before the collision check, so a refused address learns nothing about
    which accounts exist."""
    if email is not None:
        admitted = (
            await executor.execute(text("SELECT fn_signup_admitted(:e)"), {"e": email})
        ).scalar()
        if admitted:
            return
    raise SignupNotAdmitted(
        "a new account needs an admitted or invited email while sign-up is gated"
    )


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


async def user_for_identity(
    executor, *, provider: str, external_id: str
) -> Optional[str]:
    """The user a `(provider, external_id)` belongs to, or None. User-plane and
    role-open, so a door may ask this BEFORE any tenant context exists — the
    `bind-` lane checks the tapper against the minting admin with it."""
    row = (
        await executor.execute(
            text(
                "SELECT user_id FROM user_identities"
                " WHERE provider = :p AND external_id = :sub"
            ),
            {"p": provider, "sub": external_id},
        )
    ).first()
    return None if row is None else str(row[0])


async def tapper_for_identity(
    executor, *, provider: str, external_id: str
) -> Optional[tuple[str, Optional[str]]]:
    """`user_for_identity` plus the identity's own display name, in ONE read —
    for a tap, whose outcome line names the tapper (#1286: the name was a
    second query, inside the flip). The Telegram identity's name is the one a
    group already sees, which is exactly `display_name_for`'s first choice;
    an empty name here means "ask the long way" (`_actor_name`)."""
    row = (
        await executor.execute(
            text(
                "SELECT user_id, display_name FROM user_identities"
                " WHERE provider = :p AND external_id = :sub"
            ),
            {"p": provider, "sub": external_id},
        )
    ).first()
    if row is None:
        return None
    name = row[1]
    return str(row[0]), (str(name) if name else None)


async def identity_for_user(executor, *, user_id: str, provider: str) -> Optional[str]:
    """The external id *user_id* holds for *provider*, or None — "has this
    person linked Telegram?" asked before a flow that needs it."""
    row = (
        await executor.execute(
            text(
                "SELECT external_id FROM user_identities"
                " WHERE user_id = :u AND provider = :p"
            ),
            {"u": str(user_id), "p": provider},
        )
    ).first()
    return None if row is None else str(row[0])


async def telegram_ids_for(executor, user_ids) -> list[str]:
    """The Telegram ids of those of *user_ids* who have linked Telegram, in a
    stable order; a person who has not linked is simply absent."""
    if not user_ids:
        return []
    rows = await executor.execute(
        text(
            "SELECT external_id FROM user_identities"
            " WHERE provider = :p AND user_id = ANY(CAST(:u AS uuid[]))"
            " ORDER BY external_id"
        ),
        {"p": PROVIDER_TELEGRAM, "u": sorted(str(u) for u in user_ids)},
    )
    return [str(row[0]) for row in rows]


def display_name_sql(user_id_sql: str) -> str:
    """The name a shared chat may see for the person *user_id_sql* names (an
    SQL operand: a column or a bind, never user input) as one SQL expression:
    the Telegram identity's display name first (the group already sees it),
    else another identity's, else "a teammate" — never an email, since an
    address in a group chat is a disclosure (phase 1 of the 2026-09-09 tap
    plan, F3). A read that names many people joins this instead of asking
    once per person."""
    return (
        "COALESCE((SELECT ui.display_name FROM user_identities ui"
        f" WHERE ui.user_id = {user_id_sql}"
        "   AND ui.display_name IS NOT NULL AND ui.display_name <> ''"
        " ORDER BY (ui.provider = 'telegram') DESC, ui.created_at LIMIT 1),"
        " 'a teammate')"
    )


async def display_name_for(executor, *, user_id: str) -> str:
    """:func:`display_name_sql` for one person."""
    found = await readers.row(
        executor,
        f"SELECT {display_name_sql('CAST(:u AS uuid)')} AS name",
        u=str(user_id),
    )
    return str(found["name"])


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


async def link_identity(
    executor,
    *,
    user_id: str,
    provider: str,
    external_id: str,
    display_name: Optional[str] = None,
) -> bool:
    """Attach a provider identity to an EXISTING, pinned user. Returns whether
    a new row was written (False = this exact link already existed).

    **A sibling of `upsert_google_identity`, deliberately not a parameterised
    version of it.** That function is find-or-CREATE: it may mint a `users`
    row and it carries verified-email machinery. Linking is a different
    operation with different preconditions — the user already exists and is
    pinned by the `link` state (`07` §2: *"link pins the user but no
    workspace"*), it must NEVER create one, and Telegram supplies no verified
    email at all. Parameterising the provider would drag a create path and an
    email path into a flow where both are wrong.

    Idempotent on the exact pair, so a double-tap of the same deep link is not
    an error — but linking a DIFFERENT account, or an account already held by
    another user, is refused by name.

    Serialized per (provider, subject) with a transaction-scoped advisory lock,
    the same discipline `upsert_google_identity` uses and for the same reason:
    two concurrent taps for one subject must not race the uniqueness checks.
    """
    if not user_id or not external_id:
        raise ValueError("user_id and external_id are required")

    # The same 32-bit width, and it must stay the same as the site above:
    # both lock the `identity:` namespace, so they only exclude each other
    # while they agree on the function (#1370).
    await executor.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"identity:{provider}:{external_id}"},
    )

    held = (
        await executor.execute(
            text(
                "SELECT user_id FROM user_identities"
                " WHERE provider = :p AND external_id = :sub"
            ),
            {"p": provider, "sub": external_id},
        )
    ).first()
    if held is not None:
        if str(held[0]) == str(user_id):
            return False  # already linked to THIS user — idempotent, not an error
        raise IdentityAlreadyLinked("identity_held_by_another_user")

    mine = (
        await executor.execute(
            text(
                "SELECT external_id FROM user_identities"
                " WHERE user_id = :u AND provider = :p"
            ),
            {"u": str(user_id), "p": provider},
        )
    ).first()
    if mine is not None:
        # `uq_user_provider`. Replacing it would silently unlink the old
        # account; the person unlinks it first (`unlink_telegram`, 099).
        raise IdentityAlreadyLinked("user_already_has_this_provider")

    await executor.execute(
        text(
            "INSERT INTO user_identities"
            " (user_id, provider, external_id, display_name, verified_at)"
            " VALUES (:u, :p, :sub, :dn, now())"
        ),
        {"u": str(user_id), "p": provider, "sub": external_id, "dn": display_name},
    )
    return True


async def unlink_telegram(executor, *, user_id: str) -> str:
    """Remove *user_id*'s own Telegram identity — the reverse of
    :func:`link_identity`. Returns the door's outcome: `unlinked`,
    `not_linked` or `last_identity` (099, `07` §42).

    The delete is the `fn_identity_unlink` door's (099): no runtime role
    deletes from `user_identities`, and the door keeps the user's other
    identity, answering `last_identity` rather than leave an account with no
    way to sign in. The caller proves the person — this is the session's user.

    Memberships are untouched: a workspace joined from a Telegram group stays
    joined. That Telegram account now resolves to nobody, so its taps answer
    `unlinked` and its group messages join no one until the person links
    again. In the same transaction the user's live `link` states are retired,
    so a link minted before the unlink cannot re-attach an account the person
    just removed.
    """
    outcome = (
        await executor.execute(
            text("SELECT fn_identity_unlink(CAST(:u AS uuid), :p)"),
            {"u": str(user_id), "p": PROVIDER_TELEGRAM},
        )
    ).scalar_one()
    if outcome != "last_identity":
        # "link" is `identity_link.PURPOSE`, which imports this module.
        await oauth_states.retire_live_states(
            executor, provider=PROVIDER_TELEGRAM, purpose="link", user_id=user_id
        )
    return str(outcome)
