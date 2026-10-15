"""Invitation acceptance — the `fn_invitation_accept` door (`04_functions.sql`), wrapped
once, in the tier where the driver's refusals are translated.

The door owns everything that matters: it resolves the workspace from the
token, sets its own actor GUCs, evaluates the identity proof against the
caller's verified email, grants the role, and consumes the invitation. It
signals its two refusals as SQLSTATEs — ``no_data_found`` (used, revoked,
expired, unknown) and ``check_violation`` (identity proof mismatch) — and this
module turns them into a typed :class:`InvitationRefused` the adapter maps by
``reason``. The unwrap goes through `_dbapi.driver_error_is`, the one place
the SQLAlchemy→asyncpg chain is walked (`intent_ledger` precedent); an HTTP
layer sniffing SQLSTATEs was the shape this replaces.

The token's stored form is `sessions.token_hash` on purpose: `identity_and_tenancy.py` describes
the invite token as the "credential idiom as session_tokens", and the
`invite_member` executor that mints it must hash the same way.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from asyncpg.exceptions import (
    CheckViolationError,
    NoDataFoundError,
    UniqueViolationError,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.exceptions.base import RefusalError
from src.services.target import (
    egress,
    email_sender,
    identity,
    readers,
    sessions,
    tenant_resolution,
    vocabulary,
)
from src.services.target._dbapi import driver_error_is

logger = logging.getLogger(__name__)

#: `InvitationRefused.reason`, closed.
REASONS = (
    "not_acceptable",
    "identity_mismatch",
    "already_invited",
    "email_required",
    "email_invalid",
    "invalid_role",
    "invalid_channel",
)

#: An address an invitation can be emailed to: the waitlist CHECK's shape
#: (`ck_waitlist_email`) — one "@", no whitespace, a dot in the domain — and nothing
#: invisible (``isprintable``, a little stricter than that CHECK). Invitations
#: have no CHECK of their own, so this writer holds the rule.
_EMAIL_SHAPE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")

#: `identity_and_tenancy.py`'s `ck_invite_channel`, mirrored so the refusal can name the field. A
#: tuple rather than a set so the refusal message has a stable order.
CHANNELS = ("email", "telegram")

#: `identity_and_tenancy.py`'s own words: "now() + 7 days at insert". Named here
#: because the DDL states it as a default in prose, not as a column default —
#: the writer owns it, and there is exactly one writer.
INVITE_TTL_SECONDS = 7 * 24 * 3600

#: The caller's verified address, read in the statement that asks a door: the
#: accept door, and the invitee's two (invitee invitation functions). `users` is user-plane, so the
#: API's login reads it and the doors' owner never needs to.
_THEIR_ADDRESS = "(SELECT primary_email FROM users WHERE id = :u)"


class InvitationRefused(RefusalError):
    _prefix = "invitation refused"


async def accept(executor, *, token: str, user_id: str, channel: str) -> dict[str, Any]:
    """Accept by possession of *token* as *user_id*. Returns
    ``{workspace_id, role, matched}``.

    ``'google'`` is the acceptance provider because every web principal signs
    in with Google; a user created from Telegram who accepts on the web would
    need the identity's provider looked up here instead. The verified
    email is read by subselect — `users` is user-plane — rather than fetched
    first, so the whole acceptance is one statement.
    """
    try:
        row = (
            await executor.execute(
                text(
                    "SELECT o_workspace_id, o_granted_role, o_matched"
                    "  FROM fn_invitation_accept(:h, :u, :provider,"
                    f"       {_THEIR_ADDRESS}, NULL, :ch)"
                ),
                {
                    "h": sessions.token_hash(token),
                    "u": user_id,
                    "ch": channel,
                    "provider": identity.PROVIDER_GOOGLE,
                },
            )
        ).first()
    except DBAPIError as exc:
        # One pass, then discriminate: asking per class would invert the
        # candidate order where `orig` and its `__cause__` differ.
        cause = driver_error_is(exc, NoDataFoundError, CheckViolationError)
        if isinstance(cause, NoDataFoundError):
            raise InvitationRefused("not_acceptable", str(cause)) from exc
        if isinstance(cause, CheckViolationError):
            raise InvitationRefused("identity_mismatch", str(cause)) from exc
        raise
    if row is None:
        raise InvitationRefused("not_acceptable")
    return {"workspace_id": str(row[0]), "role": row[1], "matched": bool(row[2])}


async def revoke_on_removal(executor, *, workspace_id: str, user_id: str) -> int:
    """Revoke the pending invitations in *workspace_id* that *user_id* sent or
    that are addressed to them — what a removal does to the invitations it
    leaves behind (`workspaces.remove_member`, in the removal's transaction).

    Sent: the removed member's invitations no longer speak for the workspace.
    Addressed: the accept door already refuses an invitation older than the
    removal (invitation legitimacy checks), so neither arm should stay listed as pending; an email one
    would also hold `uq_invite_live` and block the fresh invitation that is
    meant to bring the person back. The Telegram id is compared as text
    because that is how `user_identities` stores it. Returns how many moved."""
    result = await executor.execute(
        text(
            "UPDATE workspace_invitations SET state = 'revoked'"
            " WHERE workspace_id = :ws AND state = 'pending'"
            "   AND (invited_by_user_id = :u"
            "        OR lower(email) = (SELECT lower(primary_email) FROM users"
            "                            WHERE id = :u)"
            "        OR invited_tg_user_id::text IN"
            "           (SELECT external_id FROM user_identities"
            "             WHERE user_id = :u AND provider = 'telegram'))"
        ),
        {"ws": str(workspace_id), "u": str(user_id)},
    )
    return result.rowcount


async def revoke(executor, *, workspace_id: str, invitation_id: str) -> bool:
    """Withdraw one invitation of *workspace_id* — an admin's Revoke. One
    compare-and-set on the state being left: False for anything not `pending`,
    unknown, or another workspace's. The accept door refuses it from here on."""
    result = await executor.execute(
        text(
            "UPDATE workspace_invitations SET state = 'revoked'"
            " WHERE workspace_id = :ws AND id = :id AND state = 'pending'"
        ),
        {"ws": str(workspace_id), "id": str(invitation_id)},
    )
    return result.rowcount == 1


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

    **DELIVERY-AGNOSTIC ON PURPOSE.** `identity_and_tenancy.py` closes `delivery_channel` at
    `email | telegram` and gives each its own acceptance value — `email`
    for a verified Google claim, `invited_tg_user_id` for the provider's
    immutable numeric id. Both are columns on one row, so an email invitation
    and a Telegram card are the same object with a different channel, not two
    objects. This function is the one writer for both; a second minting path
    would be two spellings of one credential, and they would drift on the
    parts that are authorization rather than display.

    **What is NOT decided here.** `role` is the invitation's CEILING, not a
    grant (`identity_and_tenancy.py`): `fn_invitation_accept` grants `admin` only when the
    identity proof matched, and `member` plus an elevation-pending
    notification otherwise. Nothing in this function can widen that, which is
    why an admin invite is safe to mint without knowing who will accept it.

    Runs in the caller's transaction. Its email is NOT in that transaction:
    `prepare_email` hands it to the adapter, which sends it after the commit.
    """
    if role not in tenant_resolution.INVITABLE_ROLES:
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
    if address is not None and not (
        len(address) <= vocabulary.MAX_EMAIL_LENGTH
        and _EMAIL_SHAPE.fullmatch(address)
        and address.isprintable()
    ):
        raise InvitationRefused(
            "email_invalid", "that is not an address an invitation can be sent to"
        )
    token = sessions.new_token()

    # Re-inviting an addressee replaces their pending invitation: the old link
    # stops working and the new one carries this send's role and inviter. The
    # addressee is the email or the Telegram id, as `revoke_on_removal` reads
    # it. The old one may also be one the accept door now refuses (its sender
    # demoted or removed, or its addressee removed since, invitation legitimacy checks), which for an
    # email would otherwise hold `uq_invite_live` until the reaper expires it.
    #
    # Sends to one addressee are serialized for the rest of the transaction:
    # `uq_invite_live` backs the email arm, but nothing indexes the Telegram
    # id, so two concurrent sends to it would each miss the other's row. Keys
    # are taken in a fixed order so a send naming both cannot deadlock.
    addressees = [f"em:{address}"] if address is not None else []
    if invited_tg_user_id is not None:
        addressees.append(f"tg:{invited_tg_user_id}")
    for addressee in addressees:
        await executor.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"invite:{workspace_id}:{addressee}"},
        )
    if addressees:
        await executor.execute(
            text(
                "UPDATE workspace_invitations SET state = 'revoked'"
                " WHERE workspace_id = :ws AND state = 'pending'"
                "   AND (lower(email) = :em OR invited_tg_user_id = :tg)"
            ),
            {"ws": str(workspace_id), "em": address, "tg": invited_tg_user_id},
        )

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
        # A backstop no product path reaches: the lock and revoke above
        # clear this address's live invitation before the INSERT, so
        # `uq_invite_live` (PARTIAL on `state = 'pending'`) has nothing left to
        # collide with unless a pending row appears outside `create`.
        if driver_error_is(exc, UniqueViolationError) is not None:
            raise InvitationRefused(
                "already_invited",
                "that address already has a pending invitation to this workspace",
            ) from exc
        raise

    return str(row[0]), token


# ---------------------------------------------------------------------------
# Delivery — the email arm, sent after the invitation commits
# ---------------------------------------------------------------------------


#: `email_sender.TEMPLATES` key. Named here because the producer chooses the
#: template and the transport only renders it.
INVITE_TEMPLATE = "invitation"

#: Invitation emails one person may send in a day, across every workspace —
#: anyone admitted can create workspaces, so a count per workspace would not
#: bound them. A key of `email_sender`'s budget, under its provider-wide one.
INVITES_PER_PERSON_PER_DAY = 20


def join_url(web_app_origin: str | None, token: str) -> str | None:
    """The link an invitee opens, ``{origin}/join/{token}``, or None when the
    deployment has no web origin to build it from.

    One spelling for every place a link is built. *web_app_origin* is
    `settings.web_app_origin`, already normalized: the host is the
    deployment's own setting, never a caller's — an origin taken from command
    arguments would let whoever invokes the command choose the host in an
    email our domain sends.
    """
    return f"{web_app_origin}/join/{token}" if web_app_origin else None


def email_params(
    *,
    workspace_name: str | None,
    accept_url: str,
    invited_email: str,
    expires_at: datetime,
    inviter_name: str | None,
) -> dict[str, str]:
    """The `invitation` template's params, from what the row stores."""
    expires = expires_at.astimezone(timezone.utc)
    params = {
        # A workspace with no name is not a reason to withhold the invitation.
        "workspace_name": (workspace_name or "").strip() or "your workspace",
        "accept_url": accept_url,
        "invited_email": invited_email,
        "expires_on": f"{expires:%B} {expires.day}, {expires.year} (UTC)",
    }
    if inviter_name:
        # Omitted rather than defaulted: the template opens with "You have been
        # invited" without one, and an invented name would be worse.
        params["inviter_name"] = inviter_name
    return params


def _delivery(**outcome: str) -> dict[str, Any]:
    """An invitation email's answer, ``{"delivery": {"channel": "email",...}}``,
    with *outcome* a state from `vocabulary.INVITATION_DELIVERY_STATES` and, for
    `over_budget` and `failed`, its reason."""
    return {"delivery": {"channel": "email", **outcome}}


@dataclass(frozen=True)
class InvitationEmail:
    """An invitation's email, carried past the commit.

    **Why it is not a queued job.** The link IS the credential, and a queued
    job would hold it in `jobs.payload`; the token reaches the database only as
    its SHA-256. So the email rides the command's result
    (`CommandResult.after_commit`) in memory, and the web adapter runs it once
    the invitation has committed.

    Never raises. By then the invitation exists and the inviter holds its link,
    so a failure is a `delivery` state, logged by invitation id and never by
    address or link."""

    invitation_id: str
    inviter_user_id: str
    to: str = field(repr=False)
    params: Mapping[str, str] = field(repr=False)

    async def run(self, sender, engine) -> dict[str, Any]:
        """Spend the sender's day and the deployment's, then send — the
        response's `delivery`, which this replaces."""
        if sender is None:
            outcome = {"state": "not_configured"}
        else:
            try:
                # Rendered first, so a refusal here spends no one's day.
                email_sender.render(INVITE_TEMPLATE, self.params)
                async with email_sender.system_session(engine) as session:
                    refused = await email_sender.debit_budget(
                        session,
                        person_key=f"invite:{self.inviter_user_id}",
                        person_limit=INVITES_PER_PERSON_PER_DAY,
                    )
            except Exception as exc:
                outcome = self._failed(exc)
            else:
                if refused is None:
                    outcome = await self.deliver(sender)
                else:
                    logger.info(
                        "invitation %s: email NOT sent (%s)",
                        self.invitation_id,
                        refused,
                    )
                    outcome = {"state": "over_budget", "reason": refused}
        return _delivery(**outcome)

    async def deliver(self, sender) -> dict[str, str]:
        """Render and send, outside any transaction, with the day already
        spent by the caller."""
        try:
            subject, body = email_sender.render(INVITE_TEMPLATE, self.params)
            ref = await sender.send(to=self.to, subject=subject, body=body)
        except Exception as exc:
            return self._failed(exc)
        logger.info(
            "invitation %s: email sent (provider ref %s)", self.invitation_id, ref
        )
        return {"state": "sent"}

    def _failed(self, exc: Exception) -> dict[str, str]:
        if isinstance(exc, email_sender.EmailRefused):
            reason = exc.reason
        elif isinstance(
            exc,
            (
                egress.EgressRefused,
                egress.EgressBudgetExhausted,
                egress.ResponseTooLarge,
            ),
        ):
            reason = "provider_unreachable"
        else:
            logger.exception(
                "invitation %s: email NOT sent — it failed after the invitation"
                " committed",
                self.invitation_id,
            )
            return {"state": "failed", "reason": "internal"}
        logger.warning("invitation %s: email NOT sent (%s)", self.invitation_id, reason)
        return {"state": "failed", "reason": reason}


async def prepare_email(
    executor,
    *,
    workspace_id: str,
    invitation_id: str,
    token: str,
    web_app_origin: str | None,
) -> tuple[datetime, dict[str, str], Optional[InvitationEmail]]:
    """What a minted invitation's answer says about its email, from the row as
    stored: ``(expires_at, delivery, email)``. With no origin there is no link
    to send, so `delivery` is already `not_configured` and `email` is None;
    otherwise `email` is the send for the adapter to run after the commit."""
    stored = await readers.row(
        executor,
        "SELECT i.email, i.expires_at, i.invited_by_user_id, w.name AS workspace_name,"
        f" {identity.invitation_sender_name_sql('i.invited_by_user_id')} AS inviter"
        " FROM workspace_invitations i JOIN workspaces w ON w.id = i.workspace_id"
        " WHERE i.workspace_id = :ws AND i.id = :id",
        ws=str(workspace_id),
        id=str(invitation_id),
    )
    link = join_url(web_app_origin, token)
    if link is None:
        return (
            stored["expires_at"],
            {"channel": "email", "state": "not_configured"},
            None,
        )
    return (
        stored["expires_at"],
        {"channel": "email"},
        _invitation_email(invitation_id, stored, link),
    )


def _invitation_email(
    invitation_id: str, stored: Mapping[str, Any], link: str
) -> InvitationEmail:
    """The email for an invitation as stored — its `email`, `expires_at`,
    `invited_by_user_id`, `workspace_name` and `inviter` (the Google name, or
    None) — carrying *link*."""
    return InvitationEmail(
        invitation_id=str(invitation_id),
        inviter_user_id=str(stored["invited_by_user_id"]),
        to=stored["email"],
        params=email_params(
            workspace_name=stored["workspace_name"],
            accept_url=link,
            invited_email=stored["email"],
            expires_at=stored["expires_at"],
            inviter_name=stored["inviter"],
        ),
    )


# ---------------------------------------------------------------------------
# The invitee's own (invitee invitation functions): what they could accept, and a fresh link
# ---------------------------------------------------------------------------


#: The invitations one person is shown (every list is bounded).
INVITEE_LIST_LIMIT = 20

#: "Email me the link again" asks one person may make in a day, under the
#: provider-wide budget: each mints a link and sends an email.
RESENDS_PER_PERSON_PER_DAY = 3


async def list_for_invitee(executor, *, user_id: str) -> list[dict[str, Any]]:
    """The invitations addressed to *user_id*'s verified address that the
    accept door would still honour (`fn_invitations_for`), newest first: what
    someone who signed in without the link is shown. Each is its id, workspace
    name, role, expiry and the inviter's Google name (or None), never a token
    or a link: the link is in the email, and `reissue` sends a new one."""
    return await readers.rows(
        executor,
        "SELECT d.o_invitation_id AS invitation_id,"
        " d.o_workspace_name AS workspace_name, d.o_role AS role,"
        " d.o_expires_at AS expires_at,"
        f" {identity.invitation_sender_name_sql('d.o_invited_by')} AS inviter_name"
        f" FROM fn_invitations_for(:u, {_THEIR_ADDRESS}, :n) WITH ORDINALITY AS d"
        " ORDER BY d.ordinality",
        u=str(user_id),
        n=INVITEE_LIST_LIMIT,
    )


async def reissue(
    engine,
    sender,
    *,
    user_id: str,
    invitation_id: str,
    web_app_origin: str | None,
) -> dict[str, Any]:
    """ "Email me the link again": a fresh link for one of *user_id*'s
    invitations, sent to their verified address, the one it is addressed to.
    Answers its `delivery`, never the link or the token, so the email is the
    only place the new link exists.

    The token is replaced only once the provider has taken the email, so a send
    that fails or times out leaves the old link working:

    1. In one transaction, the invitation must be one the person's list shows
       (`fn_invitations_for`), and then their day (`resend:<user>`) and the
       provider's are debited. A spent day answers before anything is sent.
    2. The email goes, carrying a link whose token is not stored yet.
    3. Only if it went does `fn_invitation_reissue` store the token's hash. An
       accept or a revoke that lands between the send and the rotation wins,
       and the email's link is the one that is dead.

    Raises `InvitationRefused` (`not_acceptable`) for an invitation that is not
    theirs, no longer open, or unknown, with nothing spent: one answer for all
    three, so an id is never an oracle. It raises the same when the invitation
    closed between the send and the rotation."""
    if sender is None or not web_app_origin:
        return _delivery(state="not_configured")
    async with email_sender.system_session(engine) as session:
        listed = await readers.row(
            session,
            f"SELECT {_THEIR_ADDRESS} AS email, d.o_expires_at AS expires_at,"
            " d.o_invited_by AS invited_by_user_id,"
            " d.o_workspace_name AS workspace_name,"
            f" {identity.invitation_sender_name_sql('d.o_invited_by')} AS inviter"
            f" FROM fn_invitations_for(:u, {_THEIR_ADDRESS}, :n) AS d"
            " WHERE d.o_invitation_id = :id",
            u=str(user_id),
            n=INVITEE_LIST_LIMIT,
            id=str(invitation_id),
        )
        if listed is None:
            raise InvitationRefused("not_acceptable")
        refused = await email_sender.debit_budget(
            session,
            person_key=f"resend:{user_id}",
            person_limit=RESENDS_PER_PERSON_PER_DAY,
        )
        if refused is not None:
            logger.info("invitation %s: link NOT re-sent (%s)", invitation_id, refused)
            return _delivery(state="over_budget", reason=refused)
    token = sessions.new_token()
    email = _invitation_email(invitation_id, listed, join_url(web_app_origin, token))
    outcome = await email.deliver(sender)
    if outcome["state"] != "sent":
        # Nothing was replaced: the link they already have still works.
        return _delivery(**outcome)
    async with email_sender.system_session(engine) as session:
        rotated = await readers.row(
            session,
            f"SELECT fn_invitation_reissue(:id, {_THEIR_ADDRESS}, :h) AS done",
            id=str(invitation_id),
            u=str(user_id),
            h=sessions.token_hash(token),
        )
    if not rotated["done"]:
        logger.info(
            "invitation %s: re-sent, but it closed before its new link was stored",
            invitation_id,
        )
        raise InvitationRefused("not_acceptable")
    return _delivery(state="sent")
