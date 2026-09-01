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

import logging

from typing import Any

from asyncpg.exceptions import (
    CheckViolationError,
    NoDataFoundError,
    UniqueViolationError,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import StorydumpError
from src.services.target import jobs, sessions
from src.services.target._dbapi import driver_candidates

logger = logging.getLogger(__name__)

#: `InvitationRefused.reason`, closed.
REASONS = (
    "not_acceptable",
    "identity_mismatch",
    "already_invited",
    "email_required",
    "invalid_role",
    "invalid_channel",
)

#: `053`'s `ck_invite_channel`, mirrored so the refusal can name the field. A
#: tuple rather than a set so the refusal message has a stable order.
CHANNELS = ("email", "telegram")

#: `05` seam, and `053`'s own words: "now() + 7 days at insert". Named here
#: because the DDL states it as a default in prose, not as a column default —
#: the writer owns it, and there is exactly one writer.
INVITE_TTL_SECONDS = 7 * 24 * 3600


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
    role: str = "member",
    delivery_channel: str = "email",
    email: str | None = None,
    invited_tg_user_id: int | None = None,
    invited_channel_hint: str | None = None,
) -> tuple[str, str]:
    """Mint an invitation. Returns ``(invitation_id, opaque_token)``.

    **The token exists in memory exactly once** — here and on its way to a
    delivery producer; only its SHA256 reaches the database. That is
    `sessions.issue`'s idiom and this module's docstring already committed to
    it: *"the `invite_member` executor that mints it must hash the same way"*.
    `fn_invitation_accept` resolves by `token_hash` alone, so the hash is the
    whole credential and possession is the whole authorization.

    **DELIVERY-AGNOSTIC ON PURPOSE.** `053` closes `delivery_channel` at
    `email | telegram` and gives each its own D33 acceptance value — `email`
    for a verified Google claim, `invited_tg_user_id` for the provider's
    immutable numeric id. Both are columns on one row, so an email invitation
    and a Telegram card are the same object with a different channel, not two
    objects. This function is the one writer for both; a second minting path
    would be two spellings of one credential, and they would drift on the
    parts that are authorization rather than display.

    **What is NOT decided here.** `role` is the invitation's CEILING, not a
    grant (`053`, D36): `fn_invitation_accept` grants `admin` only when the
    identity proof matched, and `member` plus an elevation-pending
    notification otherwise. Nothing in this function can widen that, which is
    why an admin invite is safe to mint without knowing who will accept it.

    Runs in the caller's transaction — an invitation and whatever announces it
    must commit together or not at all, the `02` §4 same-tx rule the prompt
    edge already follows.
    """
    if role not in ("admin", "member"):
        raise InvitationRefused(
            "invalid_role", f"role must be admin or member, not {role!r}"
        )
    # `ck_invite_channel` enforces this too. Refusing here is the same trade the
    # email check below makes: a person reads "must be email or telegram", not
    # the name of a check constraint. It also has to come FIRST — an unknown
    # channel is not `email`, so the check below would wave it through and the
    # row would die at the constraint with the less useful message.
    if delivery_channel not in CHANNELS:
        raise InvitationRefused(
            "invalid_channel",
            f"delivery_channel must be one of {', '.join(CHANNELS)},"
            f" not {delivery_channel!r}",
        )
    # `ck_invite_email_required` enforces this too; refusing here names the
    # field instead of surfacing a constraint name to a person.
    if delivery_channel == "email" and not (email or "").strip():
        raise InvitationRefused(
            "email_required", "an email invitation needs an address"
        )

    # Lowercased because `uq_invite_live` compares bytes and
    # `fn_invitation_accept` compares `lower(...)` on both sides: storing a
    # mixed-case address would let one person hold two live invitations to the
    # same workspace and match on neither reliably.
    address = email.strip().lower() if email else None
    token = sessions.new_token()

    try:
        row = (
            await executor.execute(
                text(
                    "INSERT INTO workspace_invitations"
                    " (workspace_id, token_hash, delivery_channel, email,"
                    "  invited_tg_user_id, invited_channel_hint, role,"
                    "  invited_by_user_id, expires_at)"
                    " VALUES (:ws, :h, :ch, :em, :tg, :hint, :role, :by,"
                    "         now() + make_interval(secs => :ttl))"
                    " RETURNING id"
                ),
                {
                    "ws": str(workspace_id),
                    "h": sessions.token_hash(token),
                    "ch": delivery_channel,
                    "em": address,
                    "tg": invited_tg_user_id,
                    "hint": invited_channel_hint,
                    "role": role,
                    "by": str(invited_by_user_id),
                    "ttl": float(INVITE_TTL_SECONDS),
                },
            )
        ).first()
    except DBAPIError as exc:
        # `uq_invite_live` is PARTIAL on `state = 'pending'`, so this fires
        # only against a LIVE invitation — a revoked or accepted one does not
        # block a new send, which is the behaviour a person expects when they
        # re-invite someone whose first invite expired.
        for cause in driver_candidates(exc):
            if isinstance(cause, UniqueViolationError):
                raise InvitationRefused(
                    "already_invited",
                    "that address already has a pending invitation to this workspace",
                ) from exc
        raise

    return str(row[0]), token


# ---------------------------------------------------------------------------
# Delivery — the email arm of `06` §2
# ---------------------------------------------------------------------------


#: `email_sender.TEMPLATES` key. Named here because the producer chooses the
#: template and the transport only renders it; a producer that named an
#: unregistered template would be refused at render, in the worker, hours from
#: the person who caused it.
INVITE_TEMPLATE = "invitation"

#: What a `CommandResult`'s ``data["delivery"]["state"]`` may say, for EITHER
#: arm. One closed list in the module both arms already import, because the two
#: producers are written in different lanes and a private copy of a shared
#: vocabulary is how they drift into meaning different things by the same word.
#:
#: * ``queued`` — something was actually produced. The only success.
#: * ``not_configured`` — nothing could be sent: no provider, or no origin to
#:   build an accept URL from. Email's shape.
#: * ``no_binding`` — nothing was owed. A workspace with no binding is the
#:   normal state of a web-born workspace, not a fault. Telegram's shape.
#: * ``none_produced`` — **there WAS a surface and nothing came out.** A bug,
#:   and the reason the arms report a count rather than a boolean: without this
#:   state it reads as ``queued``, which passes clause 3 while failing clause 4
#:   in silence. Telegram-only in practice today — `jobs.enqueue` returns None
#:   only under `unless_pending`, which the email arm does not pass.
DELIVERY_STATES = ("queued", "not_configured", "no_binding", "none_produced")


async def deliver_by_email(
    executor,
    *,
    workspace_id: str,
    invitation_id: str,
    token: str,
    email: str,
    web_app_origin: str | None,
    inviter_name: str | None = None,
) -> str | None:
    """Enqueue the `send_email` job that carries this invitation's token.

    Returns the job id, or **None** when no accept URL can be built — see the
    refusal note below. The caller must not discard either answer.

    **This is the producer half of a split the tier already made.**
    `email_sender` is the transport and says outright that it decides nothing;
    `06` §2 says an email invitation is *"a `send_email` job through the `07`
    §1 port to the invited address"*. Nothing minted one until now, so the
    whole email channel — port, executor, budget, retry ladder, template — was
    built and unreachable. Enumerated before building: twelve `send_email`
    references in `src/`, every one of them the kind enum, the executor, the
    registry entry, or a docstring. **Zero producers.**

    **Runs in the CALLER's transaction, deliberately.** `create` already
    commits to this rule — *"an invitation and whatever announces it must
    commit together or not at all"* (`02` §4). Split them and the two failures
    are both live: an invitation nobody was told about, or an email carrying a
    token that was rolled back.

    **`workspace_id` is for the NAME LOOKUP ONLY — the job row gets NULL.**
    `send_email` is a system kind and `ck_jobs_system_kinds` is a
    biconditional, so a workspace id on this row would fail the constraint.
    That is not an inconsistency: `02` §5 classes a system kind as
    payload-complete, and this payload carries everything the send needs, so
    the worker performs zero tenant reads at execution and needs no tenant to
    read them as.

    **The accept URL host is NEVER caller-supplied.** It comes from the
    deployment's own `web_app_origin`, passed down rather than read here (only
    one of forty-seven target services reads settings, and this is not the one
    to make it two). An origin taken from command arguments would let whoever
    invokes the command choose the host in an email our domain sends — a
    phishing primitive, not a configuration option.

    **With no origin configured it enqueues NOTHING and says so.** The
    template requires `accept_url` and would refuse at render, so enqueuing
    anyway would move a knowable failure into the worker and burn an attempt
    budget discovering it. The invitation itself still stands — the token is
    returned to the caller, who can deliver the link by hand — so this is a
    delivery outcome to report, not a reason to refuse the whole command.
    What it must never be is silent: a run where nobody could have been told
    must be distinguishable from a delivered one.
    """
    origin = (web_app_origin or "").strip().rstrip("/")
    if not origin:
        logger.warning(
            "invitation %s created but no email enqueued: no web_app_origin"
            " configured, so no accept URL can be built",
            invitation_id,
        )
        return None

    name = (
        await executor.execute(
            text("SELECT name FROM workspaces WHERE id = :ws"),
            {"ws": str(workspace_id)},
        )
    ).scalar()

    params: dict[str, str] = {
        # A workspace with no name is not a reason to withhold the invitation;
        # the sentence still reads and the link still works.
        "workspace_name": (name or "").strip() or "your workspace",
        "accept_url": f"{origin}/join/{token}",
    }
    if inviter_name and inviter_name.strip():
        # OPTIONAL in the template, which is why it is omitted rather than
        # defaulted: `_invitation` opens with "You have been invited" when it
        # is absent, and inventing a name would be worse than the generic line.
        params["inviter_name"] = inviter_name.strip()

    return await jobs.enqueue(
        executor,
        kind="send_email",
        # NULL, by constraint — see the docstring.
        workspace_id=None,
        # Keyed on the invitation, so a re-invite (a NEW row, `06` §2) gets its
        # own send rather than queueing behind the first one's key.
        serialization_key=f"email:inv:{invitation_id}",
        payload={
            "v": 1,
            "to": email,
            "template": INVITE_TEMPLATE,
            "params": params,
        },
        # `interactive`: a person is waiting on the other end of this, and it
        # is the lane the email gate already seeds.
        lane="interactive",
    )
