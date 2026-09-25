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

A ring that cannot be built is none of those: no account's remedy fixes a
missing key. :class:`~src.services.target.oauth_states.RingUnavailable`
propagates instead, and the adapter maps it to the code-0 retryable — nothing
left the process, so the ladder may retry and the account is left as it is.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target.ig_login_oauth import PROVIDER
from src.services.target.oauth_states import ring
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
    # A `moved` row is the tombstone an account leaves behind when it is
    # re-connected elsewhere (PA-1); its credential is dead by construction.
    "   AND a.state NOT IN ('disabled', 'moved')"
)
#: Active first, freshest first — on BOTH paths, so a workspace holding an
#: older row for the same ref never wins by table order.
_ORDER = " ORDER BY (c.state = :usable) DESC, c.expires_at DESC NULLS LAST LIMIT 1"


class IgCredentialDead(StorydumpError):
    """No usable Instagram token for the account; the message names why."""


async def token_for_account(
    engine, provider_account_ref: str, *, workspace_id: str
) -> str:
    """The active Instagram token the account ``provider_account_ref`` posts
    as IN *workspace_id*, or :class:`IgCredentialDead`.

    *workspace_id* is REQUIRED (#1369). There used to be a second path for
    callers that had none: a bare `async_sessionmaker`, no GUCs, no
    `a.workspace_id` predicate — so row-level security had nothing to key on
    and `_ORDER`'s "active first, freshest first" chose across every tenant.

    It was reachable rather than impossible. `uq_ig_account_live` is unique on
    `(workspace_id, provider_account_ref)`, so two workspaces may hold the same
    real Instagram account, and `usage_precheck`'s own note says a quota
    reading is "shared across duplicate workspace rows of one real account" —
    the design anticipates exactly the state that would have made this return
    a stranger's token. It had not happened yet: measured in production on
    2026-09-22, no `provider_account_ref` lived in more than one workspace.
    Latent by luck, not by construction.

    The one caller that had no workspace to pass now has one — the publish
    pipeline's usage pre-check, which was the only Meta read in that pipeline
    not already naming `ctx.workspace_id`. So the branch is gone rather than
    guarded: a tenancy hole closed by a signature is closed for callers that
    have not been written yet."""
    ref = str(provider_account_ref)
    if not workspace_id:
        raise ValueError("workspace_id is required — the read is tenant-scoped")
    # Built before the read and outside the `try` below: a ring that cannot be
    # built is this process's configuration (`RingUnavailable`), not this
    # account's credential, and must never reach the pipeline as a dead token.
    keys = ring()
    params: dict = {
        "ref": ref,
        "provider": PROVIDER,
        "usable": USABLE_STATE,
        "ws": str(workspace_id),
    }
    sql = _SELECT + " AND a.workspace_id = :ws" + _ORDER
    uow = unit_of_work(engine, str(workspace_id), actor_kind="system")
    async with uow.begin() as session:
        row = (await session.execute(text(sql), params)).mappings().first()

    who = f"Instagram account {ref} in workspace {workspace_id}"
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
        return keys.decrypt(row["encrypted_payload"])
    except Exception as exc:
        raise IgCredentialDead(
            f"{PROVIDER} credential for {who} could not be decrypted by any ring entry"
        ) from exc
