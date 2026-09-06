"""Resolve a media source's Drive access token (#982).

Kept out of :mod:`google_drive_adapter` on purpose: the adapter is a transport,
and mixing decryption and the credential state machine into it would put three
unrelated failure classes behind one type. This module owns the credential half
and hands the adapter a token or a typed refusal.

## It resolves by WORKSPACE, never by file id — and no longer by source

Since 069 (`07` §15, #1165) a `gdrive` credential is the WORKSPACE's: one
Google grant, every folder under it, and the row names no owner column. So a
source's token is its workspace's grant, and the source id plays no part in the
lookup (it is kept in the provider's signature because the adapter names the
source it is syncing). Resolving from a Drive file id instead would be a
cross-tenant hazard — Drive ids are global, so one workspace's id would happily
select another workspace's credential (astrid, #982).

## The writer, and the envelope it writes

:mod:`google_drive_oauth` (the Drive connect leg) writes the row and owns the
payload shape — a versioned envelope carrying both tokens, and why (F3 (b)).
It is decoded here through the writer's own :func:`google_drive_oauth.decode_payload`
so the two modules cannot drift, and a payload that is not a v1 envelope is
refused by name, never sent onward as a bearer. Until P5 mints from the
refresh token, this door hands back the connect-time access token.

A source with no credential still raises :class:`DriveCredentialDead`, and
that is deliberately **not** a crash: `media_sync` classifies it persistent,
the source flips to ``error``, the disconnect alert fires once under its
`alerted_at` dedup, and **the job SUCCEEDS** as handled work — a visible
source in `error` rather than a poisoned lane.

## The refresh door lives HERE, on the read path (P5, F3 (b), #1247)

`credential_lifecycle` ships `ig_refresh` and no Google analogue, and the
refresh clock is fenced to `ig_login` (`063`) by design: gdrive rows carry
`next_refresh_at = NULL` and are minted on the READ path, not by the clock.
That is this module's `_refresh`: an access token at or past its expiry (less
`REFRESH_SKEW`) is exchanged for a fresh one with the stored refresh token,
written in place, and handed back; Google's `invalid_grant` flips the row to
`expired`; anything transient refuses this one read and leaves the row
standing. The process needs `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — the
API has them, the worker must too.
"""

from __future__ import annotations

import logging
from typing import Optional
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from src.config.settings import settings
from src.services.target import egress, google_drive_oauth
from src.services.target.drive_adapter import DriveLostResponse, DriveRetryableError
from src.services.target.ig_login_oauth import ring
from src.services.target.media_sync import DriveCredentialDead
from src.services.target.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

#: The writer's provider — one spelling, so the read door cannot look for a
#: row under a name the connect leg does not write.
PROVIDER = google_drive_oauth.PROVIDER

#: The only state a credential may be used from.
USABLE_STATE = "active"

#: Refresh this long before the access token's stated expiry: a folder listing
#: pages on one token under a 30-second budget per page, and a token handed
#: out with seconds left dies inside the request it was minted for.
REFRESH_SKEW = timedelta(minutes=5)


async def token_for_source(
    engine, source_id, *, workspace_id: str, fresh: bool = False
) -> str:
    """The source's Drive access token — its WORKSPACE's grant (069) — or
    :class:`DriveCredentialDead`. `source_id` is not consulted: the adapter
    passes it for its own messages, and `None` is the folder browser.
    `fresh` forces a refresh — the adapter's one retry after Google refused a
    token the door thought live."""
    return await token_for_workspace(engine, workspace_id, fresh=fresh)


async def token_for_workspace(engine, workspace_id: str, *, fresh: bool = False) -> str:
    """The workspace's Drive access token, or :class:`DriveCredentialDead`.

    Every refusal names which of the four distinguishable causes it is —
    absent, wrong state, expired, undecryptable. They have different remedies
    (connect Drive, re-auth, refresh, rotate the ring) and one generic
    "credential unavailable" would send whoever reads the alert to guess.
    """
    # Workspace-scoped, not a bare session: `oauth_credentials` is under RLS,
    # so without the tenant GUC the read returns nothing and "no credential"
    # becomes indistinguishable from "cannot see the credential" — the
    # unreachable-vs-empty collapse, inside a security boundary.
    from src.services.target.work_loop import poller_session_factory

    session_factory = poller_session_factory(engine, str(workspace_id))
    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        # BOTH the tenant GUC (RLS, when live) and the WHERE:
                        # production connects as the owner role with BYPASSRLS
                        # (`app.py`), so this predicate is what binds the row to
                        # its workspace — the same convention every other
                        # query on this table keeps (review of #1246).
                        "SELECT encrypted_payload, state, expires_at"
                        " FROM oauth_credentials"
                        " WHERE workspace_id = :ws AND provider = :provider"
                        "   AND ig_account_id IS NULL AND media_source_id IS NULL"
                    ),
                    {"ws": str(workspace_id), "provider": PROVIDER},
                )
            )
            .mappings()
            .first()
        )

    who = f"workspace {workspace_id}"
    if row is None:
        raise DriveCredentialDead(
            f"no {PROVIDER} credential for {who} — never connected: Google Drive"
            " has not been set up for this workspace"
        )
    if row["state"] != USABLE_STATE:
        raise DriveCredentialDead(
            f"{PROVIDER} credential for {who} is {row['state']!r}, not"
            f" {USABLE_STATE!r} — re-auth required"
        )
    try:
        plaintext = ring().decrypt(row["encrypted_payload"])
    except Exception as exc:
        # Never log ciphertext, and never guess — `07` §3's fail-closed posture.
        # The state flip that ig_login performs here is deliberately NOT done:
        # a read door that mutates on a read is how a transient ring
        # misconfiguration permanently expires every credential it touched.
        raise DriveCredentialDead(
            f"{PROVIDER} credential for {who} could not be decrypted by any ring entry"
        ) from exc
    try:
        payload = google_drive_oauth.decode_payload(plaintext)
    except google_drive_oauth.DrivePayloadMalformed as exc:
        # A row this module can decrypt but not read is refused by name —
        # never handed to the adapter as a bearer value that is really a blob.
        raise DriveCredentialDead(
            f"{PROVIDER} credential for {who} is not a v1 Drive credential envelope"
        ) from exc
    expires_at = row["expires_at"]
    stale = (
        expires_at is not None
        and expires_at <= datetime.now(timezone.utc) + REFRESH_SKEW
    )
    if fresh or stale:
        # P5, F3 (b) (#1247): the ACCESS token expires hourly; the grant does
        # not. Mint here, on the read path — the one place the plan put it.
        return await _refresh(
            engine, str(workspace_id), payload, seen=row["encrypted_payload"]
        )
    return payload.access_token


async def _refresh(engine, workspace_id: str, payload, *, seen: str) -> str:
    """Mint a fresh access token from the refresh token and write it in place.

    The provider call sits between two transactions (the read above has
    committed; the write below is its own unit of work), never inside one.
    The outcomes are TYPED for the two callers' routing, not merely named:

    - a fresh token → written in place, returned;
    - Google's ``invalid_grant`` → the grant is GONE (D31's definitive class):
      the row goes `expired`, so the card and the picker say reconnect, and
      :class:`DriveCredentialDead` is raised — the sync's persistent branch;
    - anything transient (Google 429/5xx, a malformed answer) →
      :class:`DriveRetryableError` — the job ladder retries, the API answers
      unavailable, and NOTHING is written or flipped, so one bad minute at
      Google cannot strand a folder in `error` with a reconnect alert;
    - no answer at all → :class:`DriveLostResponse`, as the adapter says it;
    - a process without the client credentials, or a client Google rejects →
      :class:`DriveRetryableError` naming the variables. Configuration is the
      operator's to fix and is said in the log, never to a tenant.

    `seen` is the ciphertext the read found: both writes compare-and-swap on
    it, so a RECONNECT that landed during the Google round-trip is never
    overwritten with a stale envelope, nor flipped `expired` by a refusal of
    the token it replaced. Zero rows moved = someone else wrote first; the
    minted token is still good for this one call.
    """
    who = f"workspace {workspace_id}"
    client_id, client_secret = settings.GOOGLE_CLIENT_ID, settings.GOOGLE_CLIENT_SECRET
    if not client_id or not client_secret:
        missing = [
            name
            for name, value in (
                ("GOOGLE_CLIENT_ID", client_id),
                ("GOOGLE_CLIENT_SECRET", client_secret),
            )
            if not value
        ]
        raise DriveRetryableError(
            f"{PROVIDER} credential for {who} needs a refresh and this process cannot"
            f" perform one: {', '.join(missing)} unset — set them on this service (the"
            " API and the worker both read Drive)"
        )
    try:
        async with httpx.AsyncClient() as client:
            grant = await google_drive_oauth.refresh_access_token(
                client,
                refresh_token=payload.refresh_token,
                client_id=client_id,
                client_secret=client_secret,
            )
    except google_drive_oauth.DriveOAuthRefused as exc:
        if exc.reason == "grant_revoked":
            await _mark_expired(engine, workspace_id, seen=seen)
            raise DriveCredentialDead(
                f"{PROVIDER} credential for {who}: Google no longer honours its refresh"
                " token (invalid_grant) — marked expired; reconnect Google Drive"
            ) from exc
        if exc.reason == "client_misconfigured":
            logger.error(
                "drive refresh for %s refused by Google: the client id/secret are not"
                " this project's — fix the service's configuration",
                who,
            )
            raise DriveRetryableError(
                f"{PROVIDER} refresh for {who} refused by Google: GOOGLE_CLIENT_ID /"
                " GOOGLE_CLIENT_SECRET are not this project's — fix the service's"
                " configuration; the stored grant stands"
            ) from exc
        raise DriveRetryableError(
            f"{PROVIDER} refresh for {who} failed transiently ({exc.reason}); the"
            " stored grant stands and the next read will try again"
        ) from exc
    except (egress.EgressBudgetExhausted, httpx.TransportError) as exc:
        raise DriveLostResponse(
            f"{PROVIDER} refresh for {who} got no answer from Google's token endpoint"
        ) from exc
    await _store_refreshed(engine, workspace_id, grant, seen=seen)
    logger.info("drive credential for %s refreshed on the read path", who)
    return grant.access_token


async def _store_refreshed(engine, workspace_id: str, grant, *, seen: str) -> None:
    """The new envelope in place — same row, same id, `07` §2's "no window
    where the workspace has zero credentials". Compare-and-swap on the
    ciphertext the read saw (Fernet output is unique per encryption): a row
    reconnected, revoked or expired meanwhile is left exactly as it is."""
    uow = unit_of_work(engine, str(workspace_id), actor_kind="system")
    async with uow.begin() as session:
        result = await session.execute(
            text(
                "UPDATE oauth_credentials"
                "   SET encrypted_payload = :payload, expires_at = :exp"
                " WHERE workspace_id = :ws AND provider = :provider"
                "   AND ig_account_id IS NULL AND media_source_id IS NULL"
                "   AND state = 'active' AND encrypted_payload = :seen"
            ),
            {
                "ws": str(workspace_id),
                "provider": PROVIDER,
                "payload": ring().encrypt(google_drive_oauth.encode_payload(grant)),
                "exp": grant.expires_at,
                "seen": seen,
            },
        )
        if not result.rowcount:
            # Someone wrote first — a reconnect, a revoke, an expiry. Said,
            # because a silent no-op here is the class of thing nobody finds.
            logger.warning(
                "drive credential for workspace %s changed while its refresh was in"
                " flight; the row was left as it is",
                workspace_id,
            )


async def _mark_expired(engine, workspace_id: str, *, seen: str) -> None:
    """D31's definitive class: Google said the grant is gone. `expired`, so
    `drive_status` reads reconnect and the picker refuses by that name — but
    only the envelope Google refused: a reconnect that landed meanwhile is a
    different grant and stays `active`."""
    uow = unit_of_work(engine, str(workspace_id), actor_kind="system")
    async with uow.begin() as session:
        await session.execute(
            text(
                "UPDATE oauth_credentials SET state = 'expired'"
                " WHERE workspace_id = :ws AND provider = :provider"
                "   AND ig_account_id IS NULL AND media_source_id IS NULL"
                "   AND state = 'active' AND encrypted_payload = :seen"
            ),
            {"ws": str(workspace_id), "provider": PROVIDER, "seen": seen},
        )


def provider_from_engine(engine):
    """Bind an engine into the adapter's ``token_provider`` shape."""

    async def _token_provider(
        source_id: Optional[str], *, workspace_id: str, fresh: bool = False
    ) -> str:
        return await token_for_source(
            engine, source_id, workspace_id=workspace_id, fresh=fresh
        )

    return _token_provider


__all__ = [
    "PROVIDER",
    "USABLE_STATE",
    "provider_from_engine",
    "token_for_workspace",
    "token_for_source",
]
