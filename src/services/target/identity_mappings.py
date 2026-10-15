"""Verified Meta identity mappings: which person a Meta callback names.

A Meta callback's `signed_request` names an app-scoped id (`user_id`), which is
not the account id `ig_accounts.provider_account_ref` holds and is not derivable
from it. The only proof of which account an app-scoped id belongs to is the
OAuth exchange itself: the long-lived token it produced, read once at `/me`
(`ig_login_oauth.read_identity`), says both. One row of `meta_identity_mappings`
(`meta_identity_mappings`) records that, bound to the credential and the authorization it was
verified for. A row is evidence: it never changes, and only its `is_current`
flag falls, when a newer authorization replaces it.

The connect callback writes the row where the archive is live, in the same unit
of work as the credential and under the source prefix it already holds. The
bounded reconciliation of older credentials writes `reconciled_read` rows
through the same writer. Reading is the callbacks' own: `fn_meta_subject_mappings`
and `fn_meta_deletion_receive`, tenant-free doors (`meta_identity_mappings`, `Meta deletion receipt functions`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.services.target import readers
from src.services.target.instagram_sources import ArchiveAuthorization, ArchiveRefused
from src.services.target.meta_callbacks import UNCONFIGURED_APP

METHOD_OAUTH_EXCHANGE = "oauth_exchange"
METHOD_RECONCILED_READ = "reconciled_read"

#: The credential's current mapping falls unless this (credential,
#: authorization) is already recorded, so a replay that records nothing can
#: never demote a newer authorization's row.
_FLIP = (
    "UPDATE meta_identity_mappings SET is_current = false"
    " WHERE workspace_id = :ws AND credential_id = :credential AND is_current"
    "   AND NOT EXISTS (SELECT 1 FROM meta_identity_mappings n"
    "                    WHERE n.workspace_id = :ws AND n.credential_id = :credential"
    "                      AND n.authorization_id = :authorization)"
    " RETURNING authorization_id"
)

_INSERT = (
    "INSERT INTO meta_identity_mappings (workspace_id, ig_account_id, credential_id,"
    " authorization_id, app_identity, subject_ref, account_ref, method, authorized_at,"
    " verified_at)"
    " VALUES (:ws, :account, :credential, :authorization, :app, :subject, :account_ref,"
    "         :method, :authorized_at, :verified_at)"
    " ON CONFLICT (workspace_id, credential_id, authorization_id) DO NOTHING"
    " RETURNING authorization_id"
)


async def record(
    executor,
    *,
    workspace_id: str,
    ig_account_id: str,
    credential_id: str,
    authorization_id: str,
    app_identity: str,
    subject_ref: str,
    account_ref: str,
    method: str,
    authorized_at: Optional[datetime],
    verified_at: datetime,
) -> bool:
    """Record one mapping and make it the credential's current one: True when a
    row was written, False when this (credential, authorization) was already
    recorded (a reconciliation retry), which changes nothing.

    In the caller's unit of work, which claims the workspace as its tenant: the
    runtime logins write their own tenant's rows and never delete them. The
    previous current row is flipped first, as the one-current-per-credential
    index requires; the database's own checks judge the row's shape.
    """
    await readers.rows(
        executor,
        _FLIP,
        ws=str(workspace_id),
        credential=str(credential_id),
        authorization=str(authorization_id),
    )
    written = await readers.row(
        executor,
        _INSERT,
        ws=str(workspace_id),
        account=str(ig_account_id),
        credential=str(credential_id),
        authorization=str(authorization_id),
        app=app_identity,
        subject=subject_ref,
        account_ref=account_ref,
        method=method,
        authorized_at=authorized_at,
        verified_at=verified_at,
    )
    return written is not None


async def record_oauth_exchange(
    executor,
    *,
    workspace_id: str,
    ig_account_id: str,
    credential_id: str,
    authorization: ArchiveAuthorization,
    app_identity: str,
    app_scoped_id: Optional[str],
) -> None:
    """The connect callback's mapping, from the exchange that just succeeded.

    `authorization` is the one the archive generation is bound to, so the
    mapping and the generation share an authorization id and the callback's
    time (`authorized_at`, which is also when the exchange verified). Its
    `subject_id` is the account's own id. The person's app-scoped id is the one
    the identity read proved.

    Refuses `archive_identity_unverified` when there is nothing to map: the
    read carried no app-scoped id, or the deployment names no Instagram app.
    The caller is in the landing's unit of work, so the refusal rolls the whole
    landing back (`history_setup_failed`).
    """
    if not app_scoped_id or app_identity == UNCONFIGURED_APP:
        raise ArchiveRefused("archive_identity_unverified")
    await record(
        executor,
        workspace_id=workspace_id,
        ig_account_id=ig_account_id,
        credential_id=credential_id,
        authorization_id=authorization.authorization_id,
        app_identity=app_identity,
        subject_ref=app_scoped_id,
        account_ref=authorization.subject_id,
        method=METHOD_OAUTH_EXCHANGE,
        authorized_at=authorization.authorized_at,
        verified_at=authorization.authorized_at,
    )
