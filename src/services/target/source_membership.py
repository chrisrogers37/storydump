"""Archive lifecycle composition over an injected allocation writer.

The OAuth callback reaches these seams through `connect_choice`, and
`archive_lifecycle`, `archive_reconciliation`, `archive_deletion` and
`versioned_mix` call them too. Mix writes require the injected versioned
writer in the SAME tenant transaction; there is deliberately no fallback to
the legacy rounded category-mix writer. Destination/credential landing must
wrap connect_archive in its own enclosing savepoint so a caught refusal also
rolls back that caller-owned OAuth work.

Activation gates what opens an archive, never what closes one. Connecting a
source and turning it On need the injected capability; fencing a generation,
retiring or removing a source, turning it Off and expiring its credential do
not, so a rollback (archive rows present, capability gone) can still stop and
clear what it started. The closing helpers still accept a `capability` argument,
so a caller need not branch on it, but they ignore it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import text

from src.services.target import readers, source_state
from src.services.target.instagram_catalog import lock_archive
from src.services.target.instagram_sources import (
    ArchiveAuthorization,
    ArchiveRefused,
    ensure_archive_source,
    require_activation,
)


@dataclass(frozen=True)
class ArchiveChoice:
    use_history: bool
    expected_mix_revision: int | None = None
    preview_version: int = 1


@dataclass(frozen=True)
class AllocationRequest:
    """The allocation writer must apply this once, advance revision and return
    persisted state.

    source_set_changed records an Off-only add/retirement/restoration too.
    reset_to_equal is true ONLY when enabled membership changed. Otherwise all
    other ratios and the saved mode must survive. The caller holds canonical
    locks; the writer may not commit, acquire earlier locks, or enqueue work.
    """

    workspace_id: str
    source_id: str
    enabled: bool
    source_set_changed: bool
    reset_to_equal: bool
    expected_revision: int


@dataclass(frozen=True)
class AllocationResult:
    revision: int
    mode: str


AllocationWriter = Callable[[object, AllocationRequest], Awaitable[AllocationResult]]


@dataclass(frozen=True)
class MembershipResult:
    source_id: str
    source_use_epoch: int
    history_enabled: bool
    mix_revision: int | None
    mix_reset_applied: bool = False
    outcome: str | None = None


@dataclass(frozen=True)
class FenceResult:
    source_id: str
    source_use_epoch: int
    changed_generation_ids: tuple[str, ...] = ()


def _revision(value):
    if type(value) is not int or value < 0:
        raise ArchiveRefused("invalid_archive_choice")


def _choice(choice):
    if (
        not isinstance(choice, ArchiveChoice)
        or type(choice.use_history) is not bool
        or type(choice.preview_version) is not int
        or choice.preview_version != 1
    ):
        raise ArchiveRefused("invalid_archive_choice")
    if choice.use_history or choice.expected_mix_revision is not None:
        _revision(choice.expected_mix_revision)


async def _lock_membership(executor, ws, accounts=()):
    # Allocation may touch every source; acquire the entire prefix before any
    # archive credential/generation locks (including ensure's reentrant call).
    await source_state.lock_all_workspace_sources(
        executor, workspace_id=ws, ig_account_ids=accounts
    )


async def _mix(executor, ws):
    found = await readers.row(
        executor,
        "SELECT source_mix_revision AS revision, source_mix_mode AS mode FROM workspaces WHERE id=:ws",
        ws=ws,
    )
    if found is None:
        raise ArchiveRefused("archive_source_unavailable")
    return found


async def _allocate(executor, writer, request):
    if writer is None or type(request.expected_revision) is not int:
        raise ArchiveRefused("archive_allocation_unavailable")
    result = await writer(executor, request)
    saved = await _mix(executor, request.workspace_id)
    enabled = await readers.row(
        executor,
        "SELECT ratio > 0 AS enabled FROM category_post_case_mix WHERE workspace_id=:ws AND source_id=:sid AND effective_to IS NULL",
        ws=request.workspace_id,
        sid=request.source_id,
    )
    if (
        not isinstance(result, AllocationResult)
        or result.revision != request.expected_revision + 1
        or result.mode not in ("equal", "custom")
        or saved != {"revision": result.revision, "mode": result.mode}
        or enabled is None
        or enabled["enabled"] != request.enabled
        or request.reset_to_equal
        and result.mode != "equal"
    ):
        raise ArchiveRefused("archive_allocation_contract_failed")
    return result


async def connect_archive(
    executor,
    *,
    workspace_id: str,
    origin_ig_account_id: str,
    authorization: ArchiveAuthorization,
    had_prior_grant: bool,
    choice: ArchiveChoice | None,
    allocation_writer: AllocationWriter | None,
    capability=None,
) -> MembershipResult:
    """Apply trusted pre-OAuth state; an existing source's saved choice wins.

    had_prior_grant must be captured from persisted account state BEFORE
    credential landing, never from client purpose text. Thus a handle-only
    first authorization is new; pre-rollout grants are Off.
    """
    require_activation(capability)
    if type(had_prior_grant) is not bool:
        raise ArchiveRefused("invalid_archive_choice")
    ws, account = str(workspace_id), str(origin_ig_account_id)
    async with source_state.source_savepoint(executor):
        await _lock_membership(executor, ws, [account])
        existing = await readers.row(
            executor,
            "SELECT id, COALESCE((config->>'removed')::boolean, false) AS removed FROM media_sources "
            "WHERE workspace_id=:ws AND origin_ig_account_id=:account AND provider='instagram_archive'",
            ws=ws,
            account=account,
        )
        if existing is None and not had_prior_grant:
            _choice(choice)
        mix = await _mix(executor, ws)
        result = await ensure_archive_source(
            executor,
            workspace_id=ws,
            origin_ig_account_id=account,
            authorization=authorization,
            capability=capability,
        )
        sid = result["source_id"]
        before = await source_state.archive_state(
            executor, workspace_id=ws, source_id=sid
        )
        reset = False
        revision = mix["revision"]
        if existing is not None:
            outcome = "reconnected_preserved"
            if existing["removed"] and not before.removed:
                allocation = await _allocate(
                    executor,
                    allocation_writer,
                    AllocationRequest(ws, sid, before.enabled, True, False, revision),
                )
                revision = allocation.revision
        else:
            requested = not had_prior_grant and choice.use_history
            stale = requested and choice.expected_mix_revision != revision
            enable = requested and not stale
            allocation = await _allocate(
                executor,
                allocation_writer,
                AllocationRequest(ws, sid, enable, True, enable, revision),
            )
            revision = allocation.revision
            reset = enable
            outcome = (
                "connected_history_off_mix_conflict"
                if stale
                else "connected_history_on"
                if enable
                else "connected_history_off"
            )
        epoch = await source_state.apply_archive_transition(
            executor,
            workspace_id=ws,
            source_id=sid,
            before=before,
            reason="connection_choice",
        )
        after = await source_state.archive_state(
            executor, workspace_id=ws, source_id=sid
        )
        return MembershipResult(sid, epoch, after.enabled, revision, reset, outcome)


async def retire_archive(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    expected_mix_revision: int,
    allocation_writer: AllocationWriter | None,
    capability=None,
) -> MembershipResult:
    """Remove a source: it stops every use, and needs no capability to do it."""
    _revision(expected_mix_revision)
    ws, sid = str(workspace_id), str(source_id)
    async with source_state.source_savepoint(executor):
        await _lock_membership(executor, ws)
        locked = await lock_archive(executor, workspace_id=ws, source_id=sid)
        if locked is None:
            raise ArchiveRefused("archive_source_unavailable")
        before = await source_state.archive_state(
            executor, workspace_id=ws, source_id=sid
        )
        mix = await _mix(executor, ws)
        if mix["revision"] != expected_mix_revision:
            raise ArchiveRefused("mix_conflict")
        if before.removed:
            return MembershipResult(
                sid, int(locked[0]["source_use_epoch"]), before.enabled, mix["revision"]
            )
        await executor.execute(
            text(
                "UPDATE media_sources SET state='paused', config=config || jsonb_build_object('removed', true) WHERE workspace_id=:ws AND id=:sid"
            ),
            {"ws": ws, "sid": sid},
        )
        await executor.execute(
            text(
                "UPDATE media_source_destinations SET state='retired' WHERE workspace_id=:ws AND source_id=:sid"
            ),
            {"ws": ws, "sid": sid},
        )
        allocation = await _allocate(
            executor,
            allocation_writer,
            AllocationRequest(
                ws, sid, False, True, before.enabled, expected_mix_revision
            ),
        )
        epoch = await source_state.apply_archive_transition(
            executor, workspace_id=ws, source_id=sid, before=before, reason="retired"
        )
        return MembershipResult(sid, epoch, False, allocation.revision, before.enabled)


async def remove_archive_account(
    executor,
    *,
    workspace_id: str,
    origin_ig_account_id: str,
    expected_mix_revision: int,
    allocation_writer: AllocationWriter | None,
    capability=None,
) -> MembershipResult:
    """Archive-bearing account scope; other account-removal work remains caller-owned.

    Removal, like retire_archive, needs no capability.
    """
    ws, account = str(workspace_id), str(origin_ig_account_id)
    async with source_state.source_savepoint(executor):
        await _lock_membership(executor, ws, [account])
        source = await readers.row(
            executor,
            "SELECT id FROM media_sources WHERE workspace_id=:ws AND origin_ig_account_id=:account AND provider='instagram_archive'",
            ws=ws,
            account=account,
        )
        if source is None:
            raise ArchiveRefused("archive_source_unavailable")
        result = await retire_archive(
            executor,
            workspace_id=ws,
            source_id=str(source["id"]),
            expected_mix_revision=expected_mix_revision,
            allocation_writer=allocation_writer,
        )
        await executor.execute(
            text(
                "UPDATE ig_accounts SET state='disabled' WHERE workspace_id=:ws AND id=:account"
            ),
            {"ws": ws, "account": account},
        )
        return result


async def set_archive_enabled(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    enabled: bool,
    expected_mix_revision: int,
    allocation_writer: AllocationWriter | None,
    capability=None,
) -> MembershipResult:
    """Turn a source On or Off. Only an explicit Off survives a rollback.

    On admits reads, so it needs the capability. Anything that is not exactly
    False, a malformed value included, is treated as an On request: a closing
    exemption is never inferred from a value the caller got wrong.
    """
    if enabled is not False:
        require_activation(capability)
    _revision(expected_mix_revision)
    if type(enabled) is not bool:
        raise ArchiveRefused("invalid_archive_choice")
    ws, sid = str(workspace_id), str(source_id)
    async with source_state.source_savepoint(executor):
        await _lock_membership(executor, ws)
        locked = await lock_archive(executor, workspace_id=ws, source_id=sid)
        if locked is None:
            raise ArchiveRefused("archive_source_unavailable")
        before = await source_state.archive_state(
            executor, workspace_id=ws, source_id=sid
        )
        if before.removed:
            raise ArchiveRefused("archive_source_unavailable")
        mix = await _mix(executor, ws)
        if mix["revision"] != expected_mix_revision:
            raise ArchiveRefused("mix_conflict")
        if before.enabled == enabled:
            return MembershipResult(
                sid, int(locked[0]["source_use_epoch"]), enabled, mix["revision"]
            )
        allocation = await _allocate(
            executor,
            allocation_writer,
            AllocationRequest(ws, sid, enabled, False, True, expected_mix_revision),
        )
        epoch = await source_state.apply_archive_transition(
            executor,
            workspace_id=ws,
            source_id=sid,
            before=before,
            reason="enabled" if enabled else "off",
        )
        return MembershipResult(sid, epoch, enabled, allocation.revision, True)


def _current_authorization(locked):
    """Current credential ownership is independent of generation eligibility."""
    source, credentials, generations = locked
    return next(
        (
            g
            for g in generations
            if g["id"] == source["current_authorization_generation_id"]
            and any(c["id"] == g["credential_id"] for c in credentials)
        ),
        None,
    )


#: Per fence reason: the lifecycle it leaves a generation in, and the lifecycles
#: already at least that fenced. A fence only ever strengthens, so a deletion in
#: flight is never softened to a hold, nor a hold to a supersession. `review`
#: holds a generation unavailable and intact, pending verification of its scope.
_FENCES = {
    "deletion": ("deletion_pending", ("deletion_pending", "deleted")),
    "review": ("review_hold", ("review_hold", "deletion_pending", "deleted")),
    "revocation": (
        "superseded",
        ("superseded", "review_hold", "deletion_pending", "deleted"),
    ),
}


async def fence_generations(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    generation_ids: frozenset[str],
    reason: str,
    capability=None,
) -> FenceResult:
    """Freeze only the verified immutable scope, never a later G2 by credential ID.

    A fence closes, so it needs no capability: after a rollback the archive rows
    still exist and must still be stoppable. Only `revocation` touches the
    credential; `deletion` and `review` leave it alone.
    """
    if reason not in _FENCES:
        raise ArchiveRefused("invalid_archive_fence")
    target, fenced = _FENCES[reason]
    ws, sid = str(workspace_id), str(source_id)
    scope = frozenset(str(g) for g in generation_ids)
    async with source_state.source_savepoint(executor):
        locked = await lock_archive(executor, workspace_id=ws, source_id=sid)
        if (
            locked is None
            or not scope
            or not scope <= {str(g["id"]) for g in locked[2]}
        ):
            raise ArchiveRefused("archive_generation_scope_unavailable")
        before = await source_state.archive_state(
            executor, workspace_id=ws, source_id=sid
        )
        changed = []
        for generation in locked[2]:
            gid = str(generation["id"])
            if gid not in scope or generation["lifecycle"] in fenced:
                continue
            await executor.execute(
                text(
                    "UPDATE instagram_archive_generations SET lifecycle=:state WHERE workspace_id=:ws AND source_id=:sid AND id=:gid"
                ),
                {"state": target, "ws": ws, "sid": sid, "gid": gid},
            )
            changed.append(gid)
        current = _current_authorization(locked)
        if (
            reason == "revocation"
            and current is not None
            and str(current["id"]) in scope
        ):
            await executor.execute(
                text(
                    "UPDATE oauth_credentials SET state='revoked' WHERE workspace_id=:ws AND id=:credential AND provider='ig_login' AND state <> 'revoked'"
                ),
                {"ws": ws, "credential": str(current["credential_id"])},
            )
        epoch = await source_state.apply_archive_transition(
            executor, workspace_id=ws, source_id=sid, before=before, reason=reason
        )
        return FenceResult(sid, epoch, tuple(sorted(changed)))


async def mark_generations_deleted(
    executor, *, workspace_id: str, source_id: str, generation_ids: frozenset[str]
) -> FenceResult:
    """deletion_pending -> deleted for a frozen scope, after its data is erased.

    Closes the lifecycle only: the caller redacts first. No epoch bump, because a
    deletion_pending generation was already unavailable and nothing a reader or a
    permit can see changes. Needs no capability. A generation already deleted is
    a replay and is left alone. Any other lifecycle in the scope means the fence
    was skipped, so the whole call is refused, as redact_generations refuses it,
    and nothing changes.
    """
    ws, sid = str(workspace_id), str(source_id)
    scope = frozenset(str(g) for g in generation_ids)
    async with source_state.source_savepoint(executor):
        locked = await lock_archive(executor, workspace_id=ws, source_id=sid)
        lifecycles = {str(g["id"]): g["lifecycle"] for g in locked[2]} if locked else {}
        if not scope or any(
            lifecycles.get(gid) not in ("deletion_pending", "deleted") for gid in scope
        ):
            raise ArchiveRefused("archive_generation_scope_unavailable")
        changed = sorted(gid for gid in scope if lifecycles[gid] == "deletion_pending")
        for gid in changed:
            await executor.execute(
                text(
                    "UPDATE instagram_archive_generations SET lifecycle='deleted' WHERE workspace_id=:ws AND source_id=:sid AND id=:gid AND lifecycle='deletion_pending'"
                ),
                {"ws": ws, "sid": sid, "gid": gid},
            )
        return FenceResult(sid, int(locked[0]["source_use_epoch"]), tuple(changed))


async def expire_archive_credential(
    executor, *, workspace_id: str, source_id: str, generation_id: str, capability=None
) -> FenceResult:
    """Availability-only expiry, bound to the generation observed by refresh.

    Expiry closes reads, so it needs no capability.
    """
    ws, sid = str(workspace_id), str(source_id)
    async with source_state.source_savepoint(executor):
        locked = await lock_archive(executor, workspace_id=ws, source_id=sid)
        if locked is None:
            raise ArchiveRefused("archive_source_unavailable")
        before = await source_state.archive_state(
            executor, workspace_id=ws, source_id=sid
        )
        current = _current_authorization(locked)
        if current is None or str(current["id"]) != str(generation_id):
            raise ArchiveRefused("archive_stale_capture")
        await executor.execute(
            text(
                "UPDATE oauth_credentials SET state='expired' WHERE workspace_id=:ws AND "
                "id=:credential AND provider='ig_login' AND state='active'"
            ),
            {"ws": ws, "credential": str(current["credential_id"])},
        )
        epoch = await source_state.apply_archive_transition(
            executor,
            workspace_id=ws,
            source_id=sid,
            before=before,
            reason="credential_expired",
        )
        return FenceResult(sid, epoch)
