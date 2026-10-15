"""Admitting people from the waitlist (signup admission functions): the one core pathway.

Every owner surface (the waitlist ping's Admit tap, the
`/ops` page, the CLI) calls this module, and the doors decide. The
runtime roles hold no grant on `signup_admissions` or `waitlist_entries`, so
every read or write of those two tables here is a `svc_membership` door. The
one direct statement is the email's job, an INSERT into `jobs`:

- :func:`waitlist` lists the waitlist, each entry with its state, and every
  admission made off it;
- :func:`admit` admits an address, on the waitlist or not, and queues its one
  email on the worker's durable `send_email` lane, in the caller's
  transaction; on the owner's say-so (*resend*) it queues the email again;
- :func:`store_link` stores the link the worker has just emailed;
- :func:`claim` spends a link at sign-in.

## The claim link never rests

The email carries a one-time link that any Google account can claim. The
worker draws the link's token as it sends (`email_sender.execute_send_email`),
so a job's payload carries the address and the link's base, never a token.
Only the token's SHA-256 is stored, and only after the provider accepted the
email: the worker sends first and stores second, so a failed send leaves the
last link working.

## One spot, one account

The first account through an admission, by its link or as its address, uses it
up. That rule is the database's: the doors, and the trigger that stamps an
admission used when an account is created at its address (`trg_signup_admission_consume`). So
is the table's CHECK on what an address is: this module keeps no Python copy
of either beyond the length bound, and routes on the doors' answers.

Logging carries outcomes and job ids only, never an address, a token or a hash.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.services.target import jobs, readers, sessions, vocabulary
from src.services.target._dbapi import constraint_violated
from src.services.target.waitlist import storable

logger = logging.getLogger(__name__)

#: The `send_email` template an admission's job names (`email_sender.TEMPLATES`).
TEMPLATE = "admission"

#: The hex characters of an address's SHA-256 that stand for it where the
#: address itself must not travel: the Telegram button's callback data, and
#: `fn_waitlist`'s `p_ref`, which the gate pins to :func:`ref`.
REF_HEX = vocabulary.WAITLIST_REF_HEX

#: The newest rows the owner's list shows.
LIST_LIMIT = 200

_EMAIL_CHECK = "ck_signup_admissions_email"

#: A claim token's shape: at most 256 URL-safe characters. A token the worker
#: draws is 43 characters (`sessions.new_token`).
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,256}")


def wellformed(token: object) -> bool:
    """Whether *token* has a claim token's shape: a string of 1 to 256
    URL-safe characters. The one spelling of it: :func:`claim` refuses
    anything else before a statement, and the sign-in route
    (`POST /auth/google/claim`) before it keeps one."""
    return isinstance(token, str) and _TOKEN.fullmatch(token) is not None


def ref(email: str) -> str:
    """The reference that stands for *email*: the first :data:`REF_HEX` hex
    characters of its SHA-256, which `fn_waitlist`'s `p_ref` matches."""
    return hashlib.sha256(email.encode()).hexdigest()[:REF_HEX]


def claim_base(web_app_origin: Optional[str]) -> Optional[str]:
    """The claim link's base, ``{origin}/claim/``, to which the worker adds
    the token it draws; or None when the deployment has no web origin to build
    it from, which :func:`admit` answers ``not_configured``. One spelling for
    every surface that admits, as `invitations.join_url` is for an
    invitation's link: *web_app_origin* is `settings.web_app_origin`, already
    normalized, never a caller's."""
    return f"{web_app_origin}/claim/" if web_app_origin else None


def _state(row: dict[str, Any]) -> str:
    """Where one listed address stands: signed up (its admission was used, by
    its link or by an account created at its address, or an account holds the
    address), emailed, admitted, or waiting."""
    if row["signed_up"]:
        return "signed_up"
    if row["emailed_at"] is not None:
        return "emailed"
    if row["admitted_at"] is not None:
        return "admitted"
    return "waiting"


async def waitlist(conn, *, ref: Optional[str] = None) -> list[dict[str, Any]]:
    """The newest :data:`LIST_LIMIT` waitlist entries and admissions made off
    the list, newest first (`fn_waitlist`); with *ref*, only the row whose
    address it stands for. Each row is ``{email, on_waitlist, joined_at,
    state, admitted_at, emailed_at}``. Runs in the caller's transaction."""
    rows = await readers.rows(
        conn,
        "SELECT o_email AS email, o_joined_at AS joined_at,"
        "       o_admitted_at AS admitted_at, o_emailed_at AS emailed_at,"
        "       o_signed_up AS signed_up"
        "  FROM fn_waitlist(CAST(:lim AS integer), CAST(:ref AS text))",
        lim=LIST_LIMIT,
        ref=ref,
    )
    return [
        {
            "email": row["email"],
            "on_waitlist": row["joined_at"] is not None,
            "joined_at": row["joined_at"],
            "state": _state(row),
            "admitted_at": row["admitted_at"],
            "emailed_at": row["emailed_at"],
        }
        for row in rows
    ]


async def admit(
    conn, *, email: object, claim_base: Optional[str], resend: bool = False
) -> str:
    """Admit *email*, trimmed and lower-cased, and queue its one email, in the
    caller's transaction: the admission and its job commit or roll back
    together. Answers one of `vocabulary.ADMIT_OUTCOMES`.

    - Anything but a string, an address past `vocabulary.MAX_EMAIL_LENGTH`, or
      one PostgreSQL cannot hold, is ``invalid_email``; with no *claim_base*
      (the email's link, less its token) the answer is ``not_configured``.
      Either way nothing is written.
    - `fn_signup_admit` runs under a savepoint. The table's CHECK refusing the
      address is ``invalid_email``, and the caller's transaction carries on.
    - ``admitted`` queues the email. So does ``admitted_before`` with
      *resend*, which answers ``resent``: an email to that address is queued,
      whether this call queued it or one was already waiting.
    - ``admitted_before`` without *resend*, and ``signed_up``, queue nothing.

    It never raises for an outcome: callers route on the answer.
    """
    address = email.strip().lower() if isinstance(email, str) else None
    if (
        address is None
        or len(address) > vocabulary.MAX_EMAIL_LENGTH
        or not storable(address)
    ):
        logger.info("admission: invalid_email, refused before the door")
        return "invalid_email"
    if not claim_base:
        logger.info("admission: not_configured, no web origin for the claim link")
        return "not_configured"
    try:
        async with conn.begin_nested():
            outcome = (
                await conn.execute(
                    text("SELECT fn_signup_admit(:email)"), {"email": address}
                )
            ).scalar_one()
    except DBAPIError as exc:
        if constraint_violated(exc, _EMAIL_CHECK):
            logger.info("admission: invalid_email, refused by the table's CHECK")
            return "invalid_email"
        raise
    if outcome == "admitted" or (outcome == "admitted_before" and resend):
        job_id = await jobs.enqueue(
            conn,
            kind="send_email",
            # NULL, by constraint: `send_email` is a system kind.
            workspace_id=None,
            serialization_key=f"email:admission:{ref(address)}",
            # The link's base, never a token: the worker draws the token as
            # it sends, and stores only its hash (`email_sender`).
            payload={
                "v": 1,
                "to": address,
                "template": TEMPLATE,
                "params": {"address": address, "claim_base": claim_base},
            },
            # `bulk`: an admission's email waits out a spent day rather than
            # dropping it.
            lane="bulk",
            unless_pending=resend,
        )
        answer = "admitted" if outcome == "admitted" else "resent"
        logger.info(
            "admission: %s (job %s)", answer, job_id or "already waiting, not queued"
        )
        return answer
    logger.info("admission: %s", outcome)
    return outcome


async def store_link(session, *, to: str, token: str) -> bool:
    """Store the claim link an admission email has just carried to *to*: its
    token's hash and `emailed_at` together, replacing an earlier link
    (`fn_signup_link`). False when the spot was taken between the admission and
    the send. The worker calls it only after the provider accepted the email.
    Runs in the caller's transaction."""
    return (
        await session.execute(
            text("SELECT fn_signup_link(:to, :h)"),
            {"to": to, "h": sessions.token_hash(token)},
        )
    ).scalar_one()


async def claim(conn, *, token: object) -> bool:
    """Spend the claim link whose token is *token* (`fn_signup_claim`): true
    once, and false for a used, replaced or unknown link, or one whose address
    an account already holds. A token longer than 256 characters, or not
    URL-safe, is refused before any statement. Runs in the caller's
    transaction; the sign-in callback is its caller."""
    if not wellformed(token):
        logger.info("admission claim: refused, not a token")
        return False
    claimed = (
        await conn.execute(
            text("SELECT fn_signup_claim(:h)"), {"h": sessions.token_hash(token)}
        )
    ).scalar_one()
    logger.info("admission claim: %s", "spent" if claimed else "refused")
    return claimed
