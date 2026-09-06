"""W6 — the media-sync executors (#942, `01` H4, `04` S.4, seam: #982).

Two executors over one shared body. `sync_media_source` consumes the clock's
baseline mints (leg 4 nulls `next_sync_at` at mint and ONLY this executor
re-arms it — the alive job is the sole carrier until then) and the future
demand/pre-slot producers. `first_ingest_chunk` is chained by the sync itself
while the drive door reports more pages (`05`: chunks of 200 files), so a
large first ingest is bounded per job rather than one unbounded crawl.

## The drive door (#982) is consumed, never implemented here

The seam is `01` :76's port signature — ``list_changes(config, checkpoint) →
(items, checkpoint')`` — duck-typed on ``deps.drive``, plus a keyword-only
``source_id``. Items carry the adapter's canonical stable ref (D37: the Drive
file id, never a path), name, kind, and content hash.

``source_id`` is additive to `01` :76 and, since 069 (`07` §15), names the
folder being synced rather than the credential: a Drive credential is the
WORKSPACE's, so the token is `workspace_id`'s grant and the source id is what
the adapter's messages and the sync's own writes are about. A lookup by Drive
file id inside the adapter would still be a cross-tenant hazard (astrid, #982).
Keyword-only so the two-argument shape stays legible.

## Failure routing (`02` §2's source state machine)

- **Classified persistent** (:class:`DriveSourceGone`,
  :class:`DriveCredentialDead` — the folder is gone or the credential is
  definitively dead): the source flips to ``error``, the alert fires once
  under the ``alerted_at`` dedup through the workspace's own bindings, and
  the JOB SUCCEEDS — handled work, not a retry loop. An ``error`` source
  leaves the baseline due-scan by predicate.
- **Everything else** raises and rides the lane's ladder; the source stays
  ``active`` with ``next_sync_at`` NULL — the alive job carries the re-arm.
- **Recovery is the successful sync itself** (`02` :463 — "the pre-slot or
  on-demand sync IS the probe"): a demand-minted sync that succeeds flips
  ``error → active`` and clears ``alerted_at``.

Dedup is the schema's, not this module's: ``uq_media_dedup (workspace_id,
content_hash)`` — the insert skips conflicts rather than pre-checking, so
two syncs offering the same bytes cannot race a duplicate in.
"""

from __future__ import annotations

import logging
import random
from typing import Optional, Any

from sqlalchemy import text

from src.exceptions.base import StorydumpError

logger = logging.getLogger(__name__)

#: `05` :55 — sync baseline "every 6 h jittered"; the jitter keeps a fleet of
#: sources from thundering the same tick.
BASELINE_SECONDS = 6 * 3600
JITTER_SECONDS = 45 * 60

#: `05` :55 — "first-ingest chunks of 200 files". This is the #982 door's
#: PAGING CONTRACT, documented here for the consumer's reader — deliberately
#: NOT enforced by truncation: a door that overruns it must still have every
#: item processed, because silently dropping a tail the door already returned
#: is data loss wearing a bound's clothing. Enforcement, if ever needed,
#: belongs in the door.
CHUNK_ITEM_BOUND = 200

_ALLOWED_KINDS = frozenset({"image", "video"})


class DriveSourceGone(StorydumpError):
    """The provider says the configured folder/root no longer exists."""


class DriveCredentialDead(StorydumpError):
    """The provider definitively rejected the source's credential."""


async def sync_media_source(deps, session, job) -> str:
    payload = job.get("payload") or {}
    return await _run_sync(deps, job, page_token=None, reason=payload.get("reason"))


async def first_ingest_chunk(deps, session, job) -> str:
    payload = job.get("payload") or {}
    return await _run_sync(
        deps, job, page_token=payload.get("page_token"), reason="chunk"
    )


async def rearm_after_connect(
    session, *, workspace_id: str, source_id: Optional[str] = None
) -> int:
    """F4 (a): make a source eligible again, in the CALLER's transaction.

    Called beside `google_drive_oauth.store_credential` so that "a credential
    now exists" and "this source is eligible again" are ONE fact. Atomicity is
    the whole case for (a): two statements in two transactions can drift, and
    the drift is invisible — a credentialed source that never syncs looks
    exactly like one that has nothing wrong with it.

    ## Why all three columns, and why `next_sync_at` is the one that matters

    The clock's leg 4 selects `state = 'active' AND next_sync_at IS NOT NULL
    AND next_sync_at <= now()` (`063:143`) — a CONJUNCTION, and both halves
    were unsatisfied for a stranded source. `state` alone is not enough:
    `media_sources.next_sync_at` has no default and the only other writer of a
    non-NULL value is this module's own post-sync re-arm, so a source that has
    never completed a sync is `NULL` there whatever its state. Setting state
    without the cursor produces an `active` source that is permanently not due
    — the same silence in a different column.

    `alerted_at = NULL` retires the #1061 disconnect alert in the same breath.
    That is what stops this module's `alert_stranded_sources` beat re-alerting
    a source that has just recovered: the beat scans `state = 'error'`, so a
    row this statement has touched no longer matches, and because both run as
    a single statement inside their own transaction neither can observe the
    other mid-decision.

    Deliberately NOT filtered on the prior state. A reconnect is the explicit
    undo of whatever came before — `error` from a fault, `paused` from a
    deliberate disconnect (F5 (a)) — and a filter would silently no-op on
    exactly the paused case a user is trying to reverse. Scoped by
    `workspace_id` as well as id: RLS is inert under the deployed owner role
    (#751), so the WHERE clause is what actually binds the row to its tenant.

    Since 069 (`07` §15) the grant is the WORKSPACE's, so a reconnect names no
    source: with `source_id` None every `gdrive` source of the workspace is
    re-armed — one grant, every folder eligible again. A single source is
    still re-armed alone when a folder is picked or re-added under a grant.

    Returns the number of rows moved; 0 means nothing matched, which the
    caller may treat as it likes.
    """
    if source_id is None:
        # A folder the admin REMOVED stays removed: `pause_media_source` marks
        # it (`config.removed`), and a reconnect revives only what a dead
        # grant or a disconnect had paused (review of #1246 — without this a
        # reconnect an hour later resurrected every removed folder).
        result = await session.execute(
            text(
                "UPDATE media_sources"
                "   SET state = 'active', alerted_at = NULL, next_sync_at = now()"
                " WHERE workspace_id = :ws AND provider = 'gdrive'"
                "   AND NOT COALESCE((config->>'removed')::boolean, false)"
            ),
            {"ws": str(workspace_id)},
        )
    else:
        # Picked (or picked again): the removal marker clears with the re-arm,
        # in the same statement, so the two cannot drift.
        result = await session.execute(
            text(
                "UPDATE media_sources"
                "   SET state = 'active', alerted_at = NULL, next_sync_at = now(),"
                "       config = config - 'removed'"
                " WHERE id = :s AND workspace_id = :ws"
            ),
            {"s": str(source_id), "ws": str(workspace_id)},
        )
    return int(result.rowcount or 0)


async def alert_stranded_sources(
    session, *, stale_after_seconds: int, limit: int
) -> int:
    """Re-alert every source stranded in ``error``. Returns rows alerted.

    ## The defect this closes (#1061): stuckness caused the silence

    The persistent branch above sets ``state = 'error'`` and enqueues one
    notification. Recovery to ``active`` happens only on the last page of a
    SUCCESSFUL sync, and `fn_clock_tick` enqueues only ``state = 'active'``
    sources. So an errored source is never scheduled again, that branch never
    runs again, and **no second alert ever fires**. A workspace whose Drive
    access was revoked gets one message and then silence indefinitely — which
    reads exactly like a healthy workspace.

    ## This fixes the SILENCE, deliberately not the stuckness

    A source dead for good reasons — folder deleted, access revoked — is
    allowed to stay dead. It is not allowed to be QUIET about it. So nothing
    here re-arms a source, enqueues a sync, or touches `next_sync_at`; no
    provider call happens on this path at all.

    **Re-arming is fork F4 (a) and belongs to the connect/reconnect flow**,
    which does it in the same transaction as the credential write. Two things
    racing to re-arm one row is the failure this scope split exists to avoid.

    **Widening the clock to retry `error` sources on a backoff was F4 (b) and
    lost.** It changes a due-scan every provider shares, and permanently
    re-polls sources that are dead on purpose. Do not reach for it here.

    ## `paused` is the acknowledgement, and it is why this is not noise

    A recurring alert about a deliberately-dead source would be the same
    recurring noise (b) was rejected for, so there has to be a way to say
    "yes, I know". There already is: `ck_sources_state` admits
    ``('active','paused','error')`` and this scans ``error`` only. Moving a
    source a human has decided about to ``paused`` stops the alerts without
    resurrecting it and without a schema change. Silence then means somebody
    chose it, which is the property the current behaviour destroys.

    ## The F4 seam — one statement, not read-then-write

    Selection and the `alerted_at` stamp are a SINGLE ``UPDATE … RETURNING``,
    and the notifications ride the same transaction. That is what makes the
    connect flow safe to run concurrently: it sets ``state='active',
    alerted_at=NULL``, so a row it has already cleared cannot match this
    predicate, and a row this statement has locked forces the connect flow's
    UPDATE to re-evaluate after commit. A read-then-write here would reopen
    exactly that window.

    **One bounded staleness remains and is not worth more machinery.** If a
    reconnect commits immediately after this transaction, one already-enqueued
    alert still sends for a source that is now healthy. It is a single message,
    it names a real state the source was in moments earlier, and suppressing it
    would mean reaching into the outbox — more risk than the message costs.

    `alerted_at IS NULL` is included so a row stranded before this code existed
    is picked up rather than skipped forever; that column is nullable and rows
    written by paths that never stamped it are the realistic case.
    """
    # Local imports, matching `_run_sync` below: these modules reach back into
    # this one, so a module-level import is a cycle.
    from src.services.target import outbox, prompts

    rows = (
        (
            await session.execute(
                text(
                    "UPDATE media_sources SET alerted_at = now()"
                    " WHERE id IN ("
                    "   SELECT id FROM media_sources"
                    "    WHERE state = 'error'"
                    "      AND (alerted_at IS NULL"
                    "           OR alerted_at < now() - make_interval(secs => :age))"
                    "    ORDER BY alerted_at NULLS FIRST"
                    "    LIMIT :lim"
                    " ) RETURNING id, workspace_id"
                ),
                {"age": float(stale_after_seconds), "lim": int(limit)},
            )
        )
        .mappings()
        .all()
    )

    for row in rows:
        workspace_id = str(row["workspace_id"])
        bindings = await prompts.push_bindings(session, workspace_id)
        for binding_id in bindings:
            await outbox.enqueue(
                session,
                workspace_id=workspace_id,
                binding_id=binding_id,
                kind="notification",
                payload={
                    "v": 1,
                    "text": (
                        "⚠️ This workspace's Drive source is still disconnected"
                        " and has not synced since it failed. Reconnect it to"
                        " resume syncing, or pause it if this is intended."
                    ),
                },
            )
    if rows:
        logger.warning(
            "stranded-source sweep: re-alerted %d source(s) in error", len(rows)
        )
    return len(rows)


async def _run_sync(deps, job, *, page_token, reason) -> str:
    from src.services.target import prompts
    from src.services.target.work_loop import poller_session_factory

    payload = job.get("payload") or {}
    source_id = str(payload["source_id"])
    workspace_id = str(job["workspace_id"])
    factory = poller_session_factory(deps.engine, workspace_id)

    # Phase 1 — read the source, own transaction, committed before the door.
    async with factory() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT config, sync_checkpoint, state FROM media_sources"
                        " WHERE id = :s"
                    ),
                    {"s": source_id},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        logger.warning("sync %s: source %s has no row", job["id"], source_id)
        return "missing"
    checkpoint: Any = (
        {"v": 1, "page_token": page_token} if page_token else row["sync_checkpoint"]
    )

    # Phase 2 — the provider door, outside any transaction.
    try:
        items, new_checkpoint = await deps.drive.list_changes(
            dict(row["config"] or {}),
            checkpoint,
            source_id=source_id,
            workspace_id=workspace_id,
        )
    except (DriveSourceGone, DriveCredentialDead) as exc:
        async with factory() as s:
            await s.execute(
                text(
                    # A folder paused meanwhile (removed, or its grant
                    # disconnected) is NOT flipped to error, and NOT alerted
                    # about: the person chose that, and `paused` is what keeps
                    # the stranded-source beat honest (review of #1246).
                    "UPDATE media_sources SET state = 'error', alerted_at = now()"
                    " WHERE id = :s AND state <> 'paused'"
                    " RETURNING id"
                ),
                {"s": source_id},
            )
            flipped = (
                await s.execute(
                    text("SELECT state FROM media_sources WHERE id = :s"),
                    {"s": source_id},
                )
            ).scalar()
            bindings = (
                await prompts.push_bindings(s, workspace_id)
                if flipped == "error"
                else []
            )
            for binding_id in bindings:
                from src.services.target import outbox

                await outbox.enqueue(
                    s,
                    workspace_id=workspace_id,
                    binding_id=binding_id,
                    kind="notification",
                    payload={
                        "v": 1,
                        "text": (
                            "⚠️ Media sync failed for this workspace's Drive"
                            f" source: {exc}. Syncing is paused until the"
                            " source is reconnected or repaired."
                        ),
                    },
                )
            await s.commit()
        logger.warning(
            "sync %s: source %s classified persistent (%s) — state=error,"
            " alert dedup stamped",
            job["id"],
            source_id,
            type(exc).__name__,
        )
        return "source-error"

    # Phase 3 — upsert + checkpoint + chain-or-rearm, one transaction.
    kept = skipped_kind = 0
    async with factory() as s:
        for item in items:
            if item.get("kind") not in _ALLOWED_KINDS:
                skipped_kind += 1
                continue
            result = await s.execute(
                text(
                    "INSERT INTO media_items (workspace_id, source_id,"
                    " content_hash, file_name, media_kind, mime_type,"
                    " provider_file_ref)"
                    " VALUES (:ws, :src, :hash, :name, :kind, :mime, :ref)"
                    " ON CONFLICT ON CONSTRAINT uq_media_dedup DO NOTHING"
                ),
                {
                    "ws": workspace_id,
                    "src": source_id,
                    "hash": item["content_hash"],
                    "name": item.get("name") or item["ref"],
                    "kind": item["kind"],
                    # `.get`, not `[...]`: the column is nullable and an adapter
                    # that cannot know the content type must be able to say so.
                    # Absent stays NULL — the same row this wrote before — so a
                    # port without the key is unaffected rather than crashing.
                    "mime": item.get("mime_type"),
                    "ref": item["ref"],
                },
            )
            kept += result.rowcount
        next_token = (new_checkpoint or {}).get("page_token")
        await s.execute(
            text(
                "UPDATE media_sources SET sync_checkpoint = CAST(:cp AS jsonb)"
                " WHERE id = :s"
            ),
            {"cp": _json(new_checkpoint), "s": source_id},
        )
        if next_token:
            # More pages: chain the next chunk and do NOT re-arm — the chain
            # is the carrier. The serialized key orders it after this job.
            await s.execute(
                text(
                    "INSERT INTO jobs (kind, workspace_id, lane,"
                    " serialization_key, run_at, max_attempts, payload)"
                    " VALUES ('first_ingest_chunk', :ws, 'bulk', :key, now(), 5,"
                    " CAST(:p AS jsonb))"
                ),
                {
                    "ws": workspace_id,
                    "key": f"src:{source_id}",
                    "p": _json(
                        {"v": 1, "source_id": source_id, "page_token": next_token}
                    ),
                },
            )
        else:
            # The last page: success stamps, probe recovery, baseline re-arm.
            jitter = random.uniform(-JITTER_SECONDS, JITTER_SECONDS)
            await s.execute(
                text(
                    "UPDATE media_sources SET"
                    "  state = 'active',"
                    "  alerted_at = NULL,"
                    "  last_sync_success_at = now(),"
                    "  next_sync_at = now() + make_interval(secs => :secs)"
                    # Removed or disconnected while this job ran: the success
                    # stamp must not un-pause it (review of #1246).
                    " WHERE id = :s AND state <> 'paused'"
                ),
                {"secs": BASELINE_SECONDS + jitter, "s": source_id},
            )
        await s.commit()
    logger.info(
        "sync %s: source %s reason=%s kept=%d skipped_kind=%d chained=%s",
        job["id"],
        source_id,
        reason,
        kept,
        skipped_kind,
        bool(next_token),
    )
    return "chained" if next_token else "synced"


def _json(value) -> str:
    import json

    return json.dumps(value if value is not None else {"v": 1})
