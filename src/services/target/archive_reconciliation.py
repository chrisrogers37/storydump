"""Existing-account reconciliation: the bounded, idempotent backfill (#1644).

An account connected before the archive went live has no verified identity
mapping (`identity_mappings`) and no archive source. `execute(deps, job)` is the
`reconcile_archive_accounts` executor, the backfill activation owes. It owns its
transactions (register it under `work_loop.own_transactions`, with no session
open), because each account costs one provider read and a transaction never
spans one.

One workspace per job, at most `TURN_LIMIT` accounts per turn. Per account:

1. **Load**, one short transaction: the credential's capture, which carries its
   ciphertext digest, then its token. The capture comes first, so a swap between
   the reads leaves the digest older than the token and step 3 refuses.
2. **Read**, outside any transaction: `ig_login_oauth.read_identity`, one `/me`
   request with no retry loop. The archive's feeds are never read here and no
   job is queued.
3. **Record**, one tenant unit of work that takes the canonical source prefix
   first and asserts the job's lease last. It re-checks everything the read
   depended on, records the mapping, and, only where the account has no archive
   source row at all, calls `connect_archive` as an existing grant: an Off source
   with its generation, origin assignment and v1 policy. Allocation keeps custom
   ratios and moves the mix revision by one. A removed source stays removed.

The database is the authority at step 3, not the candidate list: the list is a
hint (it mirrors `fn_archive_reconcile_jobs`'s discovery for one workspace), and
everything it implied is read again under the locks. A refusal there writes
nothing for that account and the turn moves on, so one account can never starve
its siblings; a second deploy mints a fresh job and tries again.

Outcomes, logged per account: `mapped` (the mapping stands, no source was made),
`identity_mismatch`, `identity_unreadable`, `credential_changed`,
`source_created_off`, `source_exists`, `source_removed_skipped`. The run returns
`continued` (more accounts remain: the job was put back with its cursor) or
`done`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import text

from src.exceptions.base import RefusalError
from src.services.target import (
    archive_lifecycle,
    egress,
    identity_mappings,
    ig_login_oauth,
    jobs,
    oauth_states,
    readers,
    source_membership,
    source_state,
    versioned_mix,
)
from src.services.target.instagram_sources import (
    ArchiveAuthorization,
    ArchiveRefused,
    require_activation,
)
from src.services.target.meta_callbacks import UNCONFIGURED_APP, app_identity
from src.services.target.unit_of_work import poller_session_factory, unit_of_work
from src.utils.datetime_utils import utcnow

logger = logging.getLogger(__name__)

#: Accounts handled per turn. One more is looked at, to know whether to continue.
TURN_LIMIT = 5

#: The namespace of the authorization ids this module derives, `uuid5(DNS,
#: "archive.storydump")`. It must never change: the ids it makes are what keeps a
#: rerun from recording a second mapping for the same credential.
NS_ARCHIVE = uuid.UUID("3844240d-144e-5b98-8f16-ea91fcefd47b")

MAPPED = "mapped"
IDENTITY_MISMATCH = "identity_mismatch"
IDENTITY_UNREADABLE = "identity_unreadable"
CREDENTIAL_CHANGED = "credential_changed"
SOURCE_CREATED_OFF = "source_created_off"
SOURCE_EXISTS = "source_exists"
SOURCE_REMOVED_SKIPPED = "source_removed_skipped"
CONTINUED = "continued"
DONE = "done"

#: One request, no retry: a lost answer is `identity_unreadable` and the next
#: deploy's job asks again.
_READ_POLICY = egress.EgressPolicy(timeout_class="standard", max_attempts=1)

#: What a failed read can raise. `TransactionDisciplineError` is not here: a read
#: made inside a transaction is a bug, never an outcome.
_READ_FAILURES = (
    ig_login_oauth.IgOAuthRefused,
    egress.EgressRefused,
    egress.EgressBudgetExhausted,
    egress.ResponseTooLarge,
)

#: An account of this workspace that the discovery door would pick: active,
#: not a manual placeholder, holding an active `ig_login` credential that has no
#: current mapping or whose account has no archive source row (removed ones
#: count as rows). Ordered by id for the cursor; `:limit` is TURN_LIMIT + 1.
_CANDIDATES = (
    "SELECT a.id::text AS account, c.id::text AS credential"
    " FROM ig_accounts a JOIN oauth_credentials c"
    "   ON c.workspace_id = a.workspace_id AND c.ig_account_id = a.id"
    " WHERE a.workspace_id = :ws AND a.state = 'active'"
    "   AND a.provider_account_ref NOT LIKE 'manual:%'"
    "   AND c.provider = 'ig_login' AND c.state = 'active'"
    "   AND (CAST(:after AS uuid) IS NULL OR a.id > CAST(:after AS uuid))"
    "   AND (NOT EXISTS (SELECT 1 FROM meta_identity_mappings m"
    "         WHERE m.workspace_id = c.workspace_id AND m.credential_id = c.id"
    "           AND m.is_current)"
    "     OR NOT EXISTS (SELECT 1 FROM media_sources s"
    "         WHERE s.workspace_id = a.workspace_id AND s.provider = 'instagram_archive'"
    "           AND s.origin_ig_account_id = a.id))"
    " ORDER BY a.id LIMIT :limit"
)

#: The credential a turn reads and re-checks: an active `ig_login` credential of
#: an active, non-manual account.
_ACTIVE_CREDENTIAL = (
    " FROM oauth_credentials c JOIN ig_accounts a"
    "   ON a.workspace_id = c.workspace_id AND a.id = c.ig_account_id"
    " WHERE c.workspace_id = :ws AND c.id = :cid AND c.provider = 'ig_login'"
    "   AND c.state = 'active' AND a.id = :account AND a.state = 'active'"
    "   AND a.provider_account_ref NOT LIKE 'manual:%'"
)

#: Step 1's guard: the credential is still one of those, so there is something
#: to read. One revoked since the candidate page was read is not read at all.
_ACTIVE = f"SELECT 1 AS active{_ACTIVE_CREDENTIAL}"

#: Step 3's re-check, under the credential's row lock: the same credential (id,
#: state, ciphertext digest) of the same active, non-manual account.
#: `same_account` is the account's own id against the one the token just proved.
_RECHECK = (
    f"SELECT a.provider_account_ref = :user_id AS same_account{_ACTIVE_CREDENTIAL}"
    f"   AND {archive_lifecycle.CIPHERTEXT_DIGEST_SQL} = :digest FOR UPDATE OF c"
)

_CURRENT_MAPPING = (
    "SELECT authorization_id::text AS authorization_id, app_identity, account_ref,"
    " verified_at FROM meta_identity_mappings"
    " WHERE workspace_id = :ws AND credential_id = :cid AND is_current"
)

_SOURCE = (
    "SELECT COALESCE((config->>'removed')::boolean, false) AS removed"
    " FROM media_sources WHERE workspace_id = :ws AND origin_ig_account_id = :account"
    " AND provider = 'instagram_archive'"
)

#: The cursor rides the job's payload, so a turn that continues never visits an
#: account twice, whatever became of it: a refused one stays a candidate for the
#: next deploy's job, not for this one's next turn.
_ADVANCE = (
    "UPDATE jobs SET payload = payload || jsonb_build_object('cursor', CAST(:cursor AS text))"
    " WHERE id = :id"
)


class ReconciliationRefused(RefusalError):
    """payload_invalid: the job's payload is not a version-1 payload naming a
    workspace (and, when it has one, a cursor that is an account id)."""

    _prefix = "archive reconciliation refused"


def legacy_authorization_id(workspace_id, credential_id) -> str:
    """The authorization id of a credential's reconciled mapping: derived, so a
    rerun finds the row it wrote and records nothing."""
    ws, credential = (
        str(uuid.UUID(str(workspace_id))),
        str(uuid.UUID(str(credential_id))),
    )
    return str(uuid.uuid5(NS_ARCHIVE, f"{ws}:{credential}:legacy"))


def _subject(job) -> tuple[str, Optional[str]]:
    payload = job.get("payload") or {}
    try:
        if payload.get("v") != 1:
            raise ValueError("payload version")
        cursor = payload.get("cursor")
        return (
            str(uuid.UUID(str(job["workspace_id"]))),
            None if cursor is None else str(uuid.UUID(str(cursor))),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ReconciliationRefused("payload_invalid") from None


def _uow(engine, workspace_id):
    return unit_of_work(engine, workspace_id, actor_kind="system").begin()


async def _load(engine, ws, account, credential) -> Optional[tuple[str, str]]:
    """Step 1: (ciphertext digest, token), or None when there is nothing to read.

    The digest rides in the capture, which is read before the token, never
    after. A token that cannot be decrypted has already flipped its credential
    `expired` (committed by `load_credential`, through the lock order, bound to
    that capture), which is the same answer: nothing to read.
    """
    try:
        async with poller_session_factory(engine, ws)() as session:
            held = await readers.row(
                session, _ACTIVE, ws=ws, cid=credential, account=account
            )
            if held is None:
                return None
            captured = await archive_lifecycle.capture_credential(
                session, workspace_id=ws, credential_id=credential
            )
            if captured is None:
                return None
            token = await ig_login_oauth.load_credential(
                session, credential_id=credential, captured=captured
            )
    except (ig_login_oauth.CredentialUndecryptable, oauth_states.OAuthStateRefused):
        return None
    return captured.digest, token


async def _record(deps, job, ws, account, credential, digest, identity, app) -> str:
    """Step 3, as one unit of work. Nothing is written unless the credential is
    still the one that was read and the account is still the one it proved."""
    async with _uow(deps.engine, ws) as conn:
        await source_state.lock_all_workspace_sources(
            conn, workspace_id=ws, ig_account_ids=[account]
        )
        held = await readers.row(
            conn,
            _RECHECK,
            ws=ws,
            cid=credential,
            account=account,
            digest=digest,
            user_id=identity.user_id,
        )
        if held is None:
            return CREDENTIAL_CHANGED
        if not held["same_account"]:
            return IDENTITY_MISMATCH
        current = await readers.row(conn, _CURRENT_MAPPING, ws=ws, cid=credential)
        if current is None:
            # Only where nothing is current: a newer authorization's mapping is
            # never demoted by the evidence of an older one.
            await identity_mappings.record(
                conn,
                workspace_id=ws,
                ig_account_id=account,
                credential_id=credential,
                authorization_id=legacy_authorization_id(ws, credential),
                app_identity=app,
                subject_ref=identity.app_scoped_id,
                account_ref=identity.user_id,
                method=identity_mappings.METHOD_RECONCILED_READ,
                authorized_at=None,
                verified_at=utcnow(),
            )
            current = await readers.row(conn, _CURRENT_MAPPING, ws=ws, cid=credential)
        source = await readers.row(conn, _SOURCE, ws=ws, account=account)
        if source is not None:
            outcome = SOURCE_REMOVED_SKIPPED if source["removed"] else SOURCE_EXISTS
        else:
            try:
                await source_membership.connect_archive(
                    conn,
                    workspace_id=ws,
                    origin_ig_account_id=account,
                    authorization=ArchiveAuthorization(
                        current["authorization_id"],
                        current["app_identity"].partition(":")[2],
                        current["account_ref"],
                        current["verified_at"],
                    ),
                    had_prior_grant=True,
                    choice=None,
                    allocation_writer=versioned_mix.allocate_membership,
                    capability=deps.archive_capability,
                )
                outcome = SOURCE_CREATED_OFF
            except ArchiveRefused as exc:
                # The savepoint inside `connect_archive` took its writes back;
                # the mapping, which stands on its own evidence, is kept.
                logger.warning(
                    "reconcile_archive_accounts %s: no source for account %s: %s",
                    job["id"],
                    account,
                    exc.reason,
                )
                outcome = MAPPED
        await jobs.assert_lease(conn, job["id"], job["lease_token"])
    return outcome


async def _reconcile_account(deps, job, ws, candidate) -> str:
    account, credential = candidate["account"], candidate["credential"]
    app = app_identity("INSTAGRAM_APP_SECRET")
    if app == UNCONFIGURED_APP:
        # The deployment names no Instagram app, so nothing a read proved could
        # be mapped: no request is made.
        return IDENTITY_UNREADABLE
    loaded = await _load(deps.engine, ws, account, credential)
    if loaded is None:
        return CREDENTIAL_CHANGED
    digest, token = loaded
    try:
        identity = await ig_login_oauth.read_identity(
            deps.archive_client, token, policy=_READ_POLICY
        )
    except _READ_FAILURES as exc:
        # The refusal's reason or the exception's class, never its message: a
        # transport error's text can carry the request's URL, and the token
        # rides in its query string.
        logger.warning(
            "reconcile_archive_accounts %s: identity read for account %s failed: %s",
            job["id"],
            account,
            getattr(exc, "reason", None) or type(exc).__name__,
        )
        return IDENTITY_UNREADABLE
    if identity.app_scoped_id is None:
        return IDENTITY_UNREADABLE
    return await _record(deps, job, ws, account, credential, digest, identity, app)


async def execute(deps, job) -> str:
    """One turn for one workspace. Needs `deps.engine`, `deps.archive_client`
    (the archive adapter's plain HTTP client) and an active
    `deps.archive_capability`, which it requires before any read or write.

    Returns `continued` when more accounts remain, having put the job back
    (attempt restored, cursor advanced); the caller must then not finalize it.
    Returns `done` otherwise.
    """
    require_activation(deps.archive_capability)
    ws, after = _subject(job)
    async with _uow(deps.engine, ws) as conn:
        page = await readers.rows(
            conn, _CANDIDATES, ws=ws, after=after, limit=TURN_LIMIT + 1
        )
    for candidate in page[:TURN_LIMIT]:
        outcome = await _reconcile_account(deps, job, ws, candidate)
        logger.info(
            "reconcile_archive_accounts %s: account %s -> %s",
            job["id"],
            candidate["account"],
            outcome,
        )
    if len(page) <= TURN_LIMIT:
        return DONE
    async with _uow(deps.engine, ws) as conn:
        await jobs.reschedule_job(
            conn,
            job["id"],
            job["lease_token"],
            run_at=utcnow(),
            restore_attempt=True,
        )
        await conn.execute(
            text(_ADVANCE),
            {"id": str(job["id"]), "cursor": page[TURN_LIMIT - 1]["account"]},
        )
    return CONTINUED
