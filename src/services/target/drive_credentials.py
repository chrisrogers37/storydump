"""Resolve a media source's Drive access token (#982).

Kept out of :mod:`google_drive_adapter` on purpose: the adapter is a transport,
and mixing decryption and the credential state machine into it would put three
unrelated failure classes behind one type. This module owns the credential half
and hands the adapter a token or a typed refusal.

## It resolves by SOURCE, never by file id

`oauth_credentials` points AT its owner (`media_source_id`, exclusive with
`ig_account_id` per `ck_credentials_one_owner`), so the source's identity is the
only key that reaches a credential. Resolving from a Drive file id instead would
be a cross-tenant hazard — Drive ids are global, so one workspace's id would
happily select another workspace's credential (astrid, #982).

## TODAY THIS ALWAYS REFUSES, AND THAT IS THE HONEST STATE

**Nothing writes a `gdrive` credential yet.** `ig_login_oauth.store_credential`
binds ``PROVIDER = "ig_login"`` and migration `063`'s header asserts it is the
only INSERT site into `oauth_credentials`. The schema is ready
(`ck_credentials_provider` admits `'gdrive'`; `uq_credential_per_source`), the
writer and the connect flow are not. That is a prerequisite workstream, not a
detail of this one.

So every call here raises :class:`DriveCredentialDead` until that writer lands.
That is deliberately **not** a crash: `media_sync` classifies it persistent, the
source flips to ``error``, the disconnect alert fires once under its `alerted_at`
dedup, and **the job SUCCEEDS** as handled work. A missing credential therefore
surfaces as a visible source in `error` rather than as a poisoned lane — which is
why wiring the real door now is safe even though no credential exists.

## There is no Drive refresh door, and an expired token is not silently renewed

`credential_lifecycle` ships `ig_refresh` and no Google analogue. This module
therefore hands back the stored token as-is and does not attempt a refresh. An
expired one is refused HERE when `expires_at` has passed, rather than being sent
to Google to earn a 401 — same outcome for the source, one less provider call,
and the reason names expiry instead of a generic rejection. A Drive refresh leg
lands behind this same function.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.services.target.media_sync import DriveCredentialDead

logger = logging.getLogger(__name__)

#: `ck_credentials_provider` / `ck_sources_provider`.
PROVIDER = "gdrive"

#: The only state a credential may be used from.
USABLE_STATE = "active"


def _ring():
    """The shipped ring — `07` §3 keeps the env name `ENCRYPTION_KEYS`."""
    from src.utils.encryption import TokenEncryption

    return TokenEncryption()


async def token_for_source(engine, source_id: str, *, workspace_id: str) -> str:
    """The source's Drive access token, or :class:`DriveCredentialDead`.

    Every refusal names which of the four distinguishable causes it is —
    absent, wrong state, expired, undecryptable. They have different remedies
    (connect the source, re-auth, refresh, rotate the ring) and one generic
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
                        "SELECT encrypted_payload, state, expires_at"
                        " FROM oauth_credentials"
                        " WHERE media_source_id = :sid AND provider = :provider"
                    ),
                    {"sid": str(source_id), "provider": PROVIDER},
                )
            )
            .mappings()
            .first()
        )

    if row is None:
        raise DriveCredentialDead(
            f"no {PROVIDER} credential for media source {source_id} — the"
            " source has never been connected (no credential writer exists"
            " yet; see the module header)"
        )
    if row["state"] != USABLE_STATE:
        raise DriveCredentialDead(
            f"{PROVIDER} credential for media source {source_id} is"
            f" {row['state']!r}, not {USABLE_STATE!r} — re-auth required"
        )
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise DriveCredentialDead(
            f"{PROVIDER} credential for media source {source_id} expired at"
            f" {expires_at.isoformat()} and there is no Drive refresh leg to"
            " renew it"
        )
    try:
        return _ring().decrypt(row["encrypted_payload"])
    except Exception as exc:
        # Never log ciphertext, and never guess — `07` §3's fail-closed posture.
        # The state flip that ig_login performs here is deliberately NOT done:
        # this module holds no write grant on the credential, and a read door
        # that mutates on a read is how a transient ring misconfiguration
        # permanently expires every credential it touched.
        raise DriveCredentialDead(
            f"{PROVIDER} credential for media source {source_id} could not be"
            " decrypted by any ring entry"
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
    "token_for_source",
]
