"""The Instagram access token a publish needs — read by the account it posts
as, within the workspace that owns the intent, refused by name (#1220 step 3,
the publish leg).

The pipeline addresses a destination by ``provider_account_ref`` — the real
Meta IG user id the connect callback attached to the row (#1221). The token
lives in ``oauth_credentials`` keyed by ``(workspace_id, ig_account_id)`` with
``provider = 'ig_login'`` (`ig_login_oauth.store_credential`), encrypted under
the key ring; this reader joins through ``ig_accounts`` and decrypts.

**The workspace is part of the key.** The same real Instagram account may be
connected in more than one workspace (`uq_ig_account_live` allows it), each
with its own credential row; a read keyed on the account alone could pick
another tenant's row — a revoked one, failing a healthy publish with the wrong
remedy, or a live one, posting on another tenant's token. With
``workspace_id`` the read runs as that tenant (`unit_of_work`, system actor),
which is also what the F.4 runtime login's row policies require. Without it
(the quota precheck seam carries no workspace) the read prefers an active row.

Every refusal is a :class:`IgCredentialDead` naming its cause — absent, not
active, expired, undecryptable — because they have different remedies
(connect, re-auth, wait for the refresh leg, rotate the ring). The Graph
adapter maps it to the retryable Meta error with code 190, which the pipeline
hands straight to a human as `review_required` with the reason on the intent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.exceptions.base import StorydumpError
from src.services.target.ig_login_oauth import PROVIDER, ring
from src.services.target.unit_of_work import unit_of_work

USABLE_STATE = "active"

_SELECT = (
    "SELECT c.encrypted_payload, c.state, c.expires_at,"
    "       a.state AS account_state"
    "  FROM ig_accounts a"
    "  JOIN oauth_credentials c"
    "    ON c.workspace_id = a.workspace_id"
    "   AND c.ig_account_id = a.id"
    "   AND c.provider = :provider"
    " WHERE a.provider_account_ref = :ref"
    "   AND a.state <> 'disabled'"
)


class IgCredentialDead(StorydumpError):
    """No usable Instagram token for the account; the message names why."""


async def token_for_account(
    engine, provider_account_ref: str, *, workspace_id: Optional[str] = None
) -> str:
    """The active Instagram token for the account ``provider_account_ref``
    posts as — in *workspace_id* when given — or :class:`IgCredentialDead`."""
    ref = str(provider_account_ref)
    params: dict = {"ref": ref, "provider": PROVIDER}
    if workspace_id is not None:
        sql = _SELECT + " AND a.workspace_id = :ws LIMIT 1"
        params["ws"] = str(workspace_id)
        uow = unit_of_work(engine, str(workspace_id), actor_kind="system")
        async with uow.begin() as session:
            row = (await session.execute(text(sql), params)).mappings().first()
    else:
        sql = (
            _SELECT
            + " ORDER BY (c.state = :usable) DESC, c.expires_at DESC NULLS LAST"
            + " LIMIT 1"
        )
        params["usable"] = USABLE_STATE
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            row = (await session.execute(text(sql), params)).mappings().first()

    who = f"Instagram account {ref}"
    if workspace_id is not None:
        who += f" in workspace {workspace_id}"
    if row is None:
        raise IgCredentialDead(
            f"no {PROVIDER} credential for {who} — connect Instagram for this destination"
        )
    if row["state"] != USABLE_STATE:
        raise IgCredentialDead(
            f"{PROVIDER} credential for {who} is {row['state']!r}, not"
            f" {USABLE_STATE!r} — re-auth required"
        )
    if row["account_state"] == "reauth_required":
        raise IgCredentialDead(f"{who} is reauth_required — reconnect it")
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise IgCredentialDead(
            f"{PROVIDER} credential for {who} expired at {expires_at.isoformat()}"
            " — the refresh leg re-mints it, or reconnect"
        )
    try:
        return ring().decrypt(row["encrypted_payload"])
    except Exception as exc:
        raise IgCredentialDead(
            f"{PROVIDER} credential for {who} could not be decrypted by any ring entry"
        ) from exc
