"""The Instagram access token a publish needs — read by the account it posts
as, refused by name (#1220 step 3, the publish leg).

The pipeline addresses a destination by ``provider_account_ref`` — the real
Meta IG user id the connect callback attached to the row (#1221). The token
lives in ``oauth_credentials`` keyed by ``(workspace_id, ig_account_id)`` with
``provider = 'ig_login'`` (`ig_login_oauth.store_credential`), encrypted under
the key ring; this reader joins through ``ig_accounts`` and decrypts.

Every refusal is a :class:`IgCredentialDead` naming its cause — absent, not
active, expired, undecryptable — because they have different remedies
(connect, re-auth, wait for the refresh leg, rotate the ring). The Graph
adapter maps it to the retryable Meta error so the pipeline's ladder retries
and, exhausted, hands the intent to a human as `review_required` — the same
place a token Meta rejects at the call would land.

Production connects as the owner role with BYPASSRLS (#751): the WHERE binds
the row to its account and workspace, not the tenant GUC. Under the runtime
login the read would need the tenant context the pipeline holds — a seam for
the F.4 switch, named here so it is not rediscovered.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.exceptions.base import StorydumpError
from src.services.target.ig_login_oauth import PROVIDER, ring

USABLE_STATE = "active"


class IgCredentialDead(StorydumpError):
    """No usable Instagram token for the account; the message names why."""


async def token_for_account(engine, provider_account_ref: str) -> str:
    """The active Instagram token for the account ``provider_account_ref``
    posts as, or :class:`IgCredentialDead`."""
    ref = str(provider_account_ref)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT c.encrypted_payload, c.state, c.expires_at,"
                        "       a.state AS account_state"
                        "  FROM ig_accounts a"
                        "  JOIN oauth_credentials c"
                        "    ON c.workspace_id = a.workspace_id"
                        "   AND c.ig_account_id = a.id"
                        "   AND c.provider = :provider"
                        " WHERE a.provider_account_ref = :ref"
                        "   AND a.state <> 'disabled'"
                        " ORDER BY c.updated_at DESC LIMIT 1"
                    ),
                    {"ref": ref, "provider": PROVIDER},
                )
            )
            .mappings()
            .first()
        )
    who = f"Instagram account {ref}"
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
