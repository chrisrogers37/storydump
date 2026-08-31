"""Both halves of `06` §2's invited join — minting and acceptance.

Acceptance is the `fn_invitation_accept` door (`059:533`) wrapped once, in the
tier where the driver's refusals are translated. Minting is :func:`create`,
which is plain SQL rather than a door because the inviter is already a member
of the workspace they are inviting into: tenant RLS serves them, so no
`SECURITY DEFINER` escape is needed. The acceptor is the one who is
pre-membership, and that asymmetry is why exactly one of the two is a door.

The door owns everything that matters: it resolves the workspace from the
token, sets its own actor GUCs, evaluates D33's identity proof against the
caller's verified email, grants the role, and consumes the invitation. It
signals its two refusals as SQLSTATEs — ``no_data_found`` (used, revoked,
expired, unknown) and ``check_violation`` (identity proof mismatch) — and this
module turns them into a typed :class:`InvitationRefused` the adapter maps by
``reason``. The unwrap goes through `_dbapi.driver_candidates`, the one place
the SQLAlchemy→asyncpg chain is walked (`intent_ledger` precedent); an HTTP
layer sniffing SQLSTATEs was the shape this replaces.

The token's stored form is `sessions.token_hash` on purpose: `053` describes
the invite token as the "credential idiom as session_tokens", and :func:`create`
hashes the same way. **The raw token never leaves this module**: it is minted,
hashed into the row, and written into the delivery job's payload, all in one
call. Handing it back to the inviter would put a membership-granting credential
in an API response and a browser history for no gain — the app delivers it
(`06` §2, FC-6).
"""

from __future__ import annotations

import json
from typing import Any

from asyncpg.exceptions import CheckViolationError, NoDataFoundError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.services.target import jobs, sessions
from src.services.target._dbapi import driver_candidates
from src.services.target.workspaces import InvalidWorkspaceArgs

#: `InvitationRefused.reason`, closed.
REASONS = ("not_acceptable", "identity_mismatch")

#: `05` — invite expiry, 7 days. The writer sets the clock; `fn_reaper_sweep`
#: (`059:377`) is what actually flips a lapsed row to `expired`.
INVITE_TTL_DAYS = 7

#: The role ceiling an invitation may name — `ck_invite_role` in the DDL, FC-6.4
#: in the plan. Never `owner`: ownership moves by `transfer_ownership`, and an
#: invitation that could mint an owner would be a second, unaudited path to it.
INVITABLE_ROLES = ("member", "admin")

#: `ck_invite_channel` admits `telegram` too, and `06` §2 specifies an
#: `invitation` outbox card for it. Nothing renders that card yet, so this tier
#: mints email invitations only — see :func:`create`.
DELIVERY_CHANNELS = ("email",)


def clean_email(value: Any) -> str:
    """The address as the DDL stores it: stripped and lowercased (`053:255`).

    Validation is deliberately thin — one `@`, something either side, no
    whitespace. A stricter pattern rejects real addresses, and the only thing
    downstream of here that can actually judge an address is the provider.
    """
    if not isinstance(value, str):
        raise InvalidWorkspaceArgs("email is required")
    address = value.strip().lower()
    local, sep, domain = address.partition("@")
    if not sep or not local or not domain or any(c.isspace() for c in address):
        raise InvalidWorkspaceArgs(f"not an email address: {value!r}")
    return address


class InvitationRefused(StorydumpError):
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(
            f"invitation refused: {reason}" + (f" — {detail}" if detail else "")
        )


async def accept(executor, *, token: str, user_id: str, channel: str) -> dict[str, Any]:
    """Accept by possession of *token* as *user_id*. Returns
    ``{workspace_id, role, matched}``.

    ``'google'`` is the acceptance provider because every web principal today
    signed in with Google; a user W4 creates from Telegram who accepts on the
    web will need the identity's provider looked up here instead. The verified
    email is read by subselect — `users` is user-plane — rather than fetched
    first, so the whole acceptance is one statement.
    """
    try:
        row = (
            await executor.execute(
                text(
                    "SELECT o_workspace_id, o_granted_role, o_matched"
                    "  FROM fn_invitation_accept(:h, :u, 'google',"
                    "       (SELECT primary_email FROM users WHERE id = :u), NULL, :ch)"
                ),
                {"h": sessions.token_hash(token), "u": user_id, "ch": channel},
            )
        ).first()
    except DBAPIError as exc:
        for cause in driver_candidates(exc):
            if isinstance(cause, NoDataFoundError):
                raise InvitationRefused("not_acceptable", str(cause)) from exc
            if isinstance(cause, CheckViolationError):
                raise InvitationRefused("identity_mismatch", str(cause)) from exc
        raise
    if row is None:
        raise InvitationRefused("not_acceptable")
    return {"workspace_id": str(row[0]), "role": row[1], "matched": bool(row[2])}


async def create(
    executor,
    *,
    workspace_id: str,
    invited_by_user_id: str,
    email: str,
    role: str,
    workspace_name: str,
    accept_url_base: str,
) -> dict[str, Any]:
    """Mint one email invitation and queue its delivery. `06` §2, one unit.

    Three writes in the caller's transaction, in this order, because `06` §2
    says so in one sentence — *"Re-invitation = new row + revoke prior, same
    transaction"*:

    1. Any live invitation for this address is revoked. `uq_invite_live` is a
       partial unique on ``(workspace_id, email) WHERE state = 'pending'``, so
       without this an admin re-inviting someone who lost the mail gets a
       constraint violation instead of a second mail. Revoking first also means
       the superseded token stops working the moment the new one exists, which
       is the property that makes re-invitation safe to offer at all.
    2. The row, carrying only the token's SHA-256.
    3. `audit_events`, because `06` §2 requires a record for every membership
       mutation and `workspace_invitations` carries no governance trigger
       (`055:448` attaches one to five tables; this is not among them).

    Then the `send_email` job — a **system** kind, so `workspace_id` is NULL
    (the `ck_jobs_system_kinds` biconditional refuses it otherwise) on the
    `interactive` lane (`07` §1: the inviter is mid-flow awaiting confirmation).
    It rides the same transaction as the row it announces, so a rolled-back
    invitation cannot leave a mail queued for a row that does not exist.

    Returns the invitation's id, address, role and expiry. **Not the token** —
    see the module docstring.
    """
    if role not in INVITABLE_ROLES:
        raise InvalidWorkspaceArgs(
            f"role must be one of {', '.join(INVITABLE_ROLES)} — got {role!r}"
        )
    address = clean_email(email)

    await executor.execute(
        text(
            "UPDATE workspace_invitations SET state = 'revoked'"
            " WHERE workspace_id = :ws AND email = :email AND state = 'pending'"
        ),
        {"ws": workspace_id, "email": address},
    )

    token = sessions.new_token()
    row = (
        await executor.execute(
            text(
                "INSERT INTO workspace_invitations"
                " (workspace_id, token_hash, delivery_channel, email, role,"
                "  invited_by_user_id, expires_at)"
                " VALUES (:ws, :h, 'email', :email, :role, :by,"
                "         now() + (interval '1 day' * CAST(:ttl AS int)))"
                " RETURNING id, expires_at"
            ),
            {
                "ws": workspace_id,
                "h": sessions.token_hash(token),
                "email": address,
                "role": role,
                "by": invited_by_user_id,
                "ttl": INVITE_TTL_DAYS,
            },
        )
    ).first()
    invitation_id = str(row[0])

    await executor.execute(
        text(
            "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
            " from_state, to_state, actor_kind, actor_user_id, channel, detail)"
            " VALUES (:ws, 'member', CAST(:inv AS uuid), NULL, 'invited',"
            "         current_setting('app.actor_kind'),"
            "         NULLIF(current_setting('app.actor_user_id', true), '')::uuid,"
            "         NULLIF(current_setting('app.channel', true), ''),"
            "         CAST(:detail AS jsonb))"
        ),
        # The invited ADDRESS is deliberately absent: `audit_events` has no FK
        # and outlives the workspace's cascade (`02` §0), so anything written
        # here survives an offboard that is supposed to take the tenant's data
        # with it. The role and the channel are what a later reader needs.
        {
            "ws": workspace_id,
            "inv": invitation_id,
            "detail": json.dumps(
                {"v": 1, "event": "invited", "role": role, "delivery": "email"}
            ),
        },
    )

    job_id = await jobs.enqueue(
        executor,
        kind="send_email",
        workspace_id=None,
        lane="interactive",
        serialization_key=f"email:{invitation_id}",
        payload={
            "v": 1,
            "to": address,
            "template": "invitation",
            "params": {
                "workspace_name": workspace_name,
                "accept_url": f"{accept_url_base.rstrip('/')}/join/{token}",
            },
        },
    )
    return {
        "invitation_id": invitation_id,
        "email": address,
        "role": role,
        "expires_at": row[1],
        "delivery": "email",
        "job_id": job_id,
    }
