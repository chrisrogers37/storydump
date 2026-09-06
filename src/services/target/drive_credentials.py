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

## There is no Drive refresh door YET, and an expired token is not silently renewed

`credential_lifecycle` ships `ig_refresh` and no Google analogue, and the
refresh clock is fenced to `ig_login` (`063`) by design: gdrive rows carry
`next_refresh_at = NULL` and are minted on the READ path, not by the clock
(F3 (b)). Read that clause as a deliberate exclusion, never a gap to "fix" by
widening the provider filter — and read it precisely: `063` guards the
refresh-clock query only, one `AND provider = 'ig_login'` inside
`fn_clock_tick`; it never fenced the INSERT into this table (an earlier version
of this docstring said it did, and navi corrected it — the schema admits a
gdrive row wherever a writer lands, which is exactly what the connect leg is). Until that minting lands (P5), an expired access token is refused
HERE when `expires_at` has passed, rather than being sent to Google to earn a
401 — same outcome for the source, one less provider call, and the reason
names expiry instead of a generic rejection. P5 lands behind this same
function, minting from the envelope's refresh token through the egress floor
(F3a (ii)).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.services.target import google_drive_oauth
from src.services.target.ig_login_oauth import ring
from src.services.target.media_sync import DriveCredentialDead

logger = logging.getLogger(__name__)

#: The writer's provider — one spelling, so the read door cannot look for a
#: row under a name the connect leg does not write.
PROVIDER = google_drive_oauth.PROVIDER

#: The only state a credential may be used from.
USABLE_STATE = "active"


async def token_for_source(engine, source_id, *, workspace_id: str) -> str:
    """The source's Drive access token — its WORKSPACE's grant (069) — or
    :class:`DriveCredentialDead`. `source_id` is not consulted: the adapter
    passes it for its own messages, and `None` is the folder browser."""
    return await token_for_workspace(engine, workspace_id)


async def token_for_workspace(engine, workspace_id: str) -> str:
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
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise DriveCredentialDead(
            f"{PROVIDER} credential for {who} expired at {expires_at.isoformat()}"
            " and there is no Drive refresh leg to renew it"
        )
    try:
        plaintext = ring().decrypt(row["encrypted_payload"])
    except Exception as exc:
        # Never log ciphertext, and never guess — `07` §3's fail-closed posture.
        # The state flip that ig_login performs here is deliberately NOT done:
        # this module holds no write grant on the credential, and a read door
        # that mutates on a read is how a transient ring misconfiguration
        # permanently expires every credential it touched.
        raise DriveCredentialDead(
            f"{PROVIDER} credential for {who} could not be decrypted by any ring entry"
        ) from exc
    try:
        return google_drive_oauth.decode_payload(plaintext).access_token
    except google_drive_oauth.DrivePayloadMalformed as exc:
        # A row this module can decrypt but not read is refused by name —
        # never handed to the adapter as a bearer value that is really a blob.
        raise DriveCredentialDead(
            f"{PROVIDER} credential for {who} is not a v1 Drive credential envelope"
        ) from exc


def provider_from_engine(engine):
    """Bind an engine into the adapter's ``token_provider`` shape."""

    async def _token_provider(source_id: str, *, workspace_id: str) -> str:
        return await token_for_source(engine, source_id, workspace_id=workspace_id)

    return _token_provider


__all__ = [
    "PROVIDER",
    "USABLE_STATE",
    "provider_from_engine",
    "token_for_workspace",
    "token_for_source",
]
