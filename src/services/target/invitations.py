"""Invitation acceptance — the `fn_invitation_accept` door (`059:533`), wrapped
once, in the tier where the driver's refusals are translated.

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
the invite token as the "credential idiom as session_tokens", and the
`invite_member` executor that mints it must hash the same way.
"""

from __future__ import annotations

from typing import Any

from asyncpg.exceptions import CheckViolationError, NoDataFoundError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.services.target import sessions
from src.services.target._dbapi import driver_candidates

#: `InvitationRefused.reason`, closed.
REASONS = ("not_acceptable", "identity_mismatch")


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
