"""Archive lifecycle entry points: removal, revocation, refresh rejection and
offboarding, through the common source lock order (#1644).

Each entry point takes the canonical prefix first (`source_state`: the
workspace's sources and case-mix advisory locks, its row, its accounts, then
its sources) and only then any credential, generation, item, intent or job
row. Archive state changes only through the single epoch writer, either
`source_state.apply_archive_transition` itself or a `source_membership` helper
that calls it. Nothing here needs the activation capability: every entry point
closes something, so a rollback can still run it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from src.services.target import readers, source_membership, source_state
from src.services.target import ig_login_oauth as oauth
from src.services.target.instagram_catalog import lock_archive
from src.services.target.meta_callbacks import ORDERING_SKEW_SECONDS, order_evidence
from src.services.target.unit_of_work import unit_of_work
from src.services.target.versioned_mix import allocate_membership


async def remove_account_archive(
    executor, *, workspace_id: str, ig_account_id: str
) -> Optional[source_membership.MembershipResult]:
    """Account removal's archive half, called before the destination disable.

    Takes the whole membership prefix, then retires the archive this account
    originates, if there is one, at the mix's current revision. The disable
    that follows writes the account, credential and intent rows under that
    prefix. Removal moves no origin ownership. The source stays this account's,
    and connecting the account again restores it with its saved Off/On choice.
    """
    ws, account = str(workspace_id), str(ig_account_id)
    await source_state.lock_all_workspace_sources(
        executor, workspace_id=ws, ig_account_ids=[account]
    )
    found = await readers.row(
        executor,
        "SELECT s.id::text AS source_id, w.source_mix_revision AS revision"
        " FROM media_sources s JOIN workspaces w ON w.id = s.workspace_id"
        " WHERE s.workspace_id = :ws AND s.origin_ig_account_id = :account"
        " AND s.provider = 'instagram_archive'",
        ws=ws,
        account=account,
    )
    if found is None:
        return None
    return await source_membership.retire_archive(
        executor,
        workspace_id=ws,
        source_id=found["source_id"],
        expected_mix_revision=found["revision"],
        allocation_writer=allocate_membership,
    )


@dataclass(frozen=True)
class CredentialCapture:
    """What a credential's token was issued under, read before a provider call.

    The source and its current generation are the account's archive, if it has
    one. The authorization is the credential's current verified mapping, if it
    has one. A reauthorization changes the generation, the authorization or
    both. The digest is of the stored ciphertext: an account with no mapping and
    no archive is otherwise the same before and after a reconnect, because
    `store_credential` replaces the token in place under the same credential id,
    and only a new ciphertext tells the two tokens apart.
    """

    credential_id: str
    ig_account_id: str
    source_id: Optional[str]
    generation_id: Optional[str]
    authorization_id: Optional[str]
    digest: str


#: The database's own digest of a credential's stored ciphertext, taken over the
#: stored text so that no copy of the hashing lives in Python. A capture carries
#: it, and `archive_reconciliation` re-checks it under the credential's row lock.
#: It names the credential row `c`.
CIPHERTEXT_DIGEST_SQL = "encode(sha256(convert_to(c.encrypted_payload, 'UTF8')), 'hex')"


async def capture_credential(
    executor, *, workspace_id: str, credential_id: str
) -> Optional[CredentialCapture]:
    """The capture for an `ig_login` credential, or None when there is none."""
    found = await readers.row(
        executor,
        "SELECT c.ig_account_id::text AS account, s.id::text AS source,"
        " s.current_authorization_generation_id::text AS generation,"
        " m.authorization_id::text AS authorization,"
        f" {CIPHERTEXT_DIGEST_SQL} AS digest"
        " FROM oauth_credentials c"
        " LEFT JOIN media_sources s ON s.workspace_id = c.workspace_id"
        " AND s.origin_ig_account_id = c.ig_account_id AND s.provider = 'instagram_archive'"
        " LEFT JOIN meta_identity_mappings m ON m.workspace_id = c.workspace_id"
        " AND m.credential_id = c.id AND m.is_current"
        " WHERE c.workspace_id = :ws AND c.id = :cid AND c.provider = 'ig_login'"
        " AND c.ig_account_id IS NOT NULL",
        ws=str(workspace_id),
        cid=str(credential_id),
    )
    if found is None:
        return None
    return CredentialCapture(
        str(credential_id),
        found["account"],
        found["source"],
        found["generation"],
        found["authorization"],
        found["digest"],
    )


async def credential_rejected(
    executor,
    *,
    workspace_id: str,
    credential_id: str,
    captured: Optional[CredentialCapture],
) -> str:
    """A definitive refresh rejection, bound to what was captured with the token.

    Takes the account's prefix, and its archive's credential and generation
    rows, before `mark_dead`'s credential and account writes. When the capture
    no longer matches, the account was reauthorized after the token was read
    (its mapping, its generation or its stored token changed): the rejection is
    about a token nothing holds now, so nothing flips (`stale`). Otherwise
    `mark_dead` runs unchanged. An archive's state change is written by the
    epoch writer around it, so in-flight captures go stale (`rejected`).
    """
    if captured is None:
        # No `ig_login` row to order: nothing archive-bearing can change.
        await oauth.mark_dead(executor, credential_id=credential_id)
        return "rejected"
    ws = str(workspace_id)
    await source_state.lock_workspace_sources(
        executor, workspace_id=ws, ig_account_ids=[captured.ig_account_id]
    )
    source = await readers.row(
        executor,
        "SELECT id::text AS id FROM media_sources WHERE workspace_id = :ws"
        " AND origin_ig_account_id = :account AND provider = 'instagram_archive'",
        ws=ws,
        account=captured.ig_account_id,
    )
    sid = None if source is None else source["id"]
    if sid is not None:
        await lock_archive(executor, workspace_id=ws, source_id=sid)
    current = await capture_credential(
        executor, workspace_id=ws, credential_id=captured.credential_id
    )
    if current != captured:
        return "stale"
    before = (
        None
        if sid is None
        else await source_state.archive_state(executor, workspace_id=ws, source_id=sid)
    )
    await oauth.mark_dead(executor, credential_id=captured.credential_id)
    if sid is not None:
        await source_state.apply_archive_transition(
            executor,
            workspace_id=ws,
            source_id=sid,
            before=before,
            reason="credential_expired",
        )
    return "rejected"


@dataclass(frozen=True)
class SubjectRevocation:
    """What one deauthorize did: workspaces mapped, and per current
    authorization, revoked, held (ambiguous order) or excluded (newer).

    `mapped` is every (workspace id, account id) pair some mapping names for the
    subject, whatever became of it: the pairs the mapped path decided, which the
    account-reference lookup by `provider_account_ref` must not decide again.
    """

    workspaces: int
    revoked: int
    held: int
    excluded: int
    mapped: frozenset[tuple[str, str]] = frozenset()


#: Each workspace's current authorizations for the subject, classified the way
#: `fn_meta_deletion_receive` classifies a deletion (143) against the order
#: evidence: in scope when the authorization certainly predates the request,
#: excluded when it certainly follows it, held otherwise. With no evidence, an
#: account's only authorization for the subject is in scope; where it has more
#: than one (a reauthorization), every one is held, the older too, because
#: nothing says which the request reached and a boundary never expands to a
#: newer grant for want of evidence. The receive door holds the same ones, and
#: also an account an earlier receipt already scoped, which a deauthorize has
#: no receipts to see. Order evidence from beyond the skew is no evidence,
#: measured by the database's clock.
_CURRENT_AUTHORIZATIONS = (
    "WITH e AS (SELECT CASE WHEN CAST(:issued AS timestamptz)"
    " <= now() + make_interval(secs => CAST(:skew AS int))"
    " THEN CAST(:issued AS timestamptz) END AS issued,"
    " make_interval(secs => CAST(:skew AS int)) AS skew)"
    " SELECT m.ig_account_id::text AS account, m.credential_id::text AS credential,"
    " m.authorization_id::text AS authorization,"
    " CASE WHEN e.issued IS NULL THEN"
    " CASE WHEN (SELECT count(*) FROM meta_identity_mappings o"
    " WHERE o.workspace_id = m.workspace_id AND o.ig_account_id = m.ig_account_id"
    " AND o.app_identity = m.app_identity AND o.subject_ref = m.subject_ref) = 1"
    " THEN 'in_scope' ELSE 'held' END"
    " WHEN COALESCE(m.authorized_at, m.verified_at) <= e.issued - e.skew THEN 'in_scope'"
    " WHEN m.authorized_at >= e.issued + e.skew THEN 'excluded'"
    " ELSE 'held' END AS disposition"
    " FROM meta_identity_mappings m CROSS JOIN e"
    " WHERE m.workspace_id = :ws AND m.app_identity = :app AND m.subject_ref = :subject"
    " AND m.is_current ORDER BY m.ig_account_id"
)


async def revoke_subject_authorizations(
    engine, *, app_identity: str, subject: str, issued_at=None
) -> SubjectRevocation:
    """Deauthorize: revoke each account's current authorization for the subject.

    The tenant-free door names the workspaces (142). Each workspace runs in its
    own transaction, in ascending order, under its own tenant and the `system`
    actor. Each takes its prefix before anything else and classifies under it,
    so a reconnect that lands first is the authorization judged. Only an in-scope
    current authorization is revoked: an archive whose current generation it is
    is fenced (`revocation`: superseded, credential revoked, epoch bumped once);
    any other account's credential is revoked directly. A newer or ambiguous
    authorization is left alone. Nothing is deleted. The result names every
    account a mapping accounts for (`SubjectRevocation.mapped`), so the caller's
    fallback can leave those to this decision.
    """
    issued = order_evidence(issued_at)
    async with engine.begin() as conn:
        mapped = await readers.rows(
            conn,
            "SELECT DISTINCT o_workspace_id::text AS ws,"
            " o_ig_account_id::text AS account"
            " FROM fn_meta_subject_mappings(:app, :subject) ORDER BY 1, 2",
            app=app_identity,
            subject=subject,
        )
    # Ascending, as the door returned them: one transaction per workspace.
    workspaces = list(dict.fromkeys(found["ws"] for found in mapped))
    revoked = held = excluded = 0
    for ws in workspaces:
        async with unit_of_work(engine, ws, actor_kind="system").begin() as session:
            await source_state.lock_all_workspace_sources(session, workspace_id=ws)
            for current in await readers.rows(
                session,
                _CURRENT_AUTHORIZATIONS,
                ws=ws,
                app=app_identity,
                subject=subject,
                issued=issued,
                skew=ORDERING_SKEW_SECONDS,
            ):
                if current["disposition"] == "held":
                    held += 1
                elif current["disposition"] == "excluded":
                    excluded += 1
                else:
                    await _revoke_current(session, ws, current)
                    revoked += 1
    return SubjectRevocation(
        len(workspaces),
        revoked,
        held,
        excluded,
        frozenset((found["ws"], found["account"]) for found in mapped),
    )


async def _revoke_current(session, ws: str, current: dict) -> None:
    archive = await readers.row(
        session,
        "SELECT s.id::text AS source_id, g.id::text AS generation_id,"
        " g.authorization_id::text AS authorization"
        " FROM media_sources s LEFT JOIN instagram_archive_generations g"
        " ON g.workspace_id = s.workspace_id AND g.source_id = s.id"
        " AND g.id = s.current_authorization_generation_id"
        " WHERE s.workspace_id = :ws AND s.origin_ig_account_id = :account"
        " AND s.provider = 'instagram_archive'",
        ws=ws,
        account=current["account"],
    )
    if archive is not None and archive["authorization"] == current["authorization"]:
        await source_membership.fence_generations(
            session,
            workspace_id=ws,
            source_id=archive["source_id"],
            generation_ids=frozenset([archive["generation_id"]]),
            reason="revocation",
        )
        return
    # Not the archive's current generation, or no archive at all. The
    # credential still carries archive state when there is an archive.
    before = (
        None
        if archive is None
        else await source_state.archive_state(
            session, workspace_id=ws, source_id=archive["source_id"]
        )
    )
    await session.execute(
        text(
            "UPDATE oauth_credentials SET state = 'revoked' WHERE workspace_id = :ws"
            " AND id = :credential AND provider = 'ig_login' AND state <> 'revoked'"
        ),
        {"ws": ws, "credential": current["credential"]},
    )
    if archive is not None:
        await source_state.apply_archive_transition(
            session,
            workspace_id=ws,
            source_id=archive["source_id"],
            before=before,
            reason="revocation",
        )


async def offboard_fence(
    executor, *, workspace_id: str
) -> tuple[source_membership.FenceResult, ...]:
    """Offboarding's archive fence, once the drain has finished and before the
    credentials are revoked.

    Under the whole membership prefix, every archive of the workspace is fenced
    for revocation across all of its generations. Its active generation is
    superseded, its origin credential revoked, and its epoch bumped once. That
    stops new admissions and permits before anything else goes. A second run
    finds everything fenced and changes nothing.
    """
    ws = str(workspace_id)
    await source_state.lock_all_workspace_sources(executor, workspace_id=ws)
    archives = await readers.rows(
        executor,
        "SELECT s.id::text AS source_id, array_agg(g.id::text ORDER BY g.id) AS generations"
        " FROM media_sources s JOIN instagram_archive_generations g"
        " ON g.workspace_id = s.workspace_id AND g.source_id = s.id"
        " WHERE s.workspace_id = :ws AND s.provider = 'instagram_archive'"
        " GROUP BY s.id ORDER BY s.id",
        ws=ws,
    )
    fenced = []
    for archive in archives:
        fenced.append(
            await source_membership.fence_generations(
                executor,
                workspace_id=ws,
                source_id=archive["source_id"],
                generation_ids=frozenset(archive["generations"]),
                reason="revocation",
            )
        )
    return tuple(fenced)
