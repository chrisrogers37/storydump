"""Origin-owned archive catalog writers, dormant until `archive_activation` arms a process.

The activation capability is injected by a trusted composition root
(`archive_composition`), which has one wherever the archive's key ring loads.
Without it, every archive admission fails before writing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from src.exceptions import StorydumpError
from src.services.target import readers, source_state


@dataclass(frozen=True)
class ArchiveCapability:
    enabled: bool = False


@dataclass(frozen=True)
class ArchiveAuthorization:
    """Identity from a verified OAuth event, never from editable policy JSON."""

    authorization_id: str
    app_id: str
    subject_id: str
    authorized_at: datetime


class ArchiveRefused(StorydumpError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def is_activated(capability: ArchiveCapability | None) -> bool:
    return isinstance(capability, ArchiveCapability) and capability.enabled is True


def require_activation(capability: ArchiveCapability | None) -> None:
    if not is_activated(capability):
        raise ArchiveRefused("archive_not_activated")


async def ensure_archive_source(
    executor,
    *,
    workspace_id: str,
    origin_ig_account_id: str,
    authorization: ArchiveAuthorization | None = None,
    capability: ArchiveCapability | None = None,
) -> dict:
    """Materialize one source, origin assignment and v1 policy, initially Off.

    This runs in the caller's tenant transaction alongside destination/credential
    landing. Its caller, `source_membership`, applies the validated allocation in
    that same UoW. A refused bundle rolls back even if the caller catches the
    refusal and commits its surrounding transaction. Replayed authorizations
    change nothing; a new authorization gets a new data generation even when the
    encrypted credential row was replaced in place.
    """
    require_activation(capability)
    async with source_state.source_savepoint(executor):
        return await _ensure_archive_source(
            executor,
            workspace_id=workspace_id,
            origin_ig_account_id=origin_ig_account_id,
            authorization=authorization,
        )


async def _ensure_archive_source(
    executor,
    *,
    workspace_id: str,
    origin_ig_account_id: str,
    authorization: ArchiveAuthorization | None,
) -> dict:
    ws, account = str(workspace_id), str(origin_ig_account_id)
    await source_state.lock_workspace_sources(
        executor, workspace_id=ws, ig_account_ids=[account]
    )
    origin = await readers.row(
        executor,
        "SELECT id, state, provider_account_ref FROM ig_accounts WHERE workspace_id = :ws AND id = :account",
        ws=ws,
        account=account,
    )
    if (
        origin is None
        or origin["state"] != "active"
        or origin["provider_account_ref"].startswith("manual:")
    ):
        raise ArchiveRefused("archive_origin_unavailable")
    found = await readers.row(
        executor,
        "SELECT id FROM media_sources WHERE workspace_id = :ws AND origin_ig_account_id = :account AND provider = 'instagram_archive'",
        ws=ws,
        account=account,
    )
    created = found is None
    if created:
        found = await readers.row(
            executor,
            "INSERT INTO media_sources (workspace_id, provider, config, origin_ig_account_id) VALUES (:ws, 'instagram_archive', jsonb_build_object('v', 1), :account) RETURNING id",
            ws=ws,
            account=account,
        )
    source_id = str(found["id"])
    await source_state.lock_workspace_sources(
        executor, workspace_id=ws, ig_account_ids=[account], source_ids=[source_id]
    )
    credential = await readers.row(
        executor,
        "SELECT id, state, (expires_at IS NULL OR expires_at > now()) AS unexpired FROM oauth_credentials WHERE workspace_id = :ws AND ig_account_id = :account AND provider = 'ig_login' ORDER BY id FOR UPDATE",
        ws=ws,
        account=account,
    )
    if (
        credential is None
        or credential["state"] != "active"
        or not credential["unexpired"]
    ):
        raise ArchiveRefused("archive_credential_unavailable")
    generations = await readers.rows(
        executor,
        "SELECT id, authorization_id, lifecycle FROM instagram_archive_generations WHERE workspace_id = :ws AND source_id = :sid ORDER BY id FOR UPDATE",
        ws=ws,
        sid=source_id,
    )
    before = await source_state.archive_state(
        executor, workspace_id=ws, source_id=source_id
    )
    current = before.generation_id
    replay = authorization is not None and any(
        str(g["authorization_id"]) == str(authorization.authorization_id)
        for g in generations
    )
    if created:
        from src.services.target.source_policy import materialize_policy

        await materialize_policy(executor, workspace_id=ws, source_id=source_id)
        await executor.execute(
            text(
                "INSERT INTO media_source_destinations (workspace_id, source_id, ig_account_id) VALUES (:ws, :sid, :account)"
            ),
            {"ws": ws, "sid": source_id, "account": account},
        )
        await executor.execute(
            text(
                "INSERT INTO category_post_case_mix (workspace_id, source_id, category, ratio) VALUES (:ws, :sid, 'Instagram archive', 0)"
            ),
            {"ws": ws, "sid": source_id},
        )
    if authorization is not None and not replay:
        await executor.execute(
            text(
                "UPDATE instagram_archive_generations SET lifecycle = 'superseded' WHERE workspace_id = :ws AND source_id = :sid AND lifecycle = 'active'"
            ),
            {"ws": ws, "sid": source_id},
        )
        generation = await readers.row(
            executor,
            "INSERT INTO instagram_archive_generations (workspace_id, source_id, origin_ig_account_id, credential_id, authorization_id, app_id, subject_id, authorized_at)"
            " VALUES (:ws, :sid, :account, :credential, :authorization, :app, :subject, :at) RETURNING id",
            ws=ws,
            sid=source_id,
            account=account,
            credential=str(credential["id"]),
            authorization=str(authorization.authorization_id),
            app=authorization.app_id,
            subject=authorization.subject_id,
            at=authorization.authorized_at,
        )
        current = str(generation["id"])
        await executor.execute(
            text(
                "UPDATE media_sources SET current_authorization_generation_id = :gid, state = 'active', config = config - 'removed' WHERE workspace_id = :ws AND id = :sid"
            ),
            {"ws": ws, "sid": source_id, "gid": current},
        )
        await executor.execute(
            text(
                "UPDATE media_source_destinations SET state = 'active' WHERE workspace_id = :ws AND source_id = :sid AND ig_account_id = :account"
            ),
            {"ws": ws, "sid": source_id, "account": account},
        )
    epoch = await source_state.apply_archive_transition(
        executor,
        workspace_id=ws,
        source_id=source_id,
        before=before,
        reason="created" if created else "reauthorized",
    )
    return {
        "source_id": source_id,
        "created": created,
        "generation_id": current,
        "source_use_epoch": epoch,
    }
