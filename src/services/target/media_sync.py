"""W6 — the media-sync executors (#942, `01` H4, `04` S.4, seam: #982).

Two executors over one shared body. `sync_media_source` consumes the clock's
baseline mints (leg 4 nulls `next_sync_at` at mint and ONLY this executor
re-arms it — the alive job is the sole carrier until then) and the future
demand/pre-slot producers. `first_ingest_chunk` is chained by the sync itself
while the drive door reports more pages (`05`: chunks of 200 files), so a
large first ingest is bounded per job rather than one unbounded crawl.

## The drive door (#982) is consumed, never implemented here

The seam is `01` :76's port signature — ``list_changes(config, checkpoint) →
(items, checkpoint')`` — duck-typed on ``deps.drive``. Production composes
``None`` until #982's real door lands, which parks both kinds loudly (the
`media_fetch` posture: wiring a test fake into production is not
composition). Items carry the adapter's canonical stable ref (D37: the Drive
file id, never a path), name, kind, and content hash.

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
from typing import Any

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
            dict(row["config"] or {}), checkpoint
        )
    except (DriveSourceGone, DriveCredentialDead) as exc:
        async with factory() as s:
            await s.execute(
                text(
                    "UPDATE media_sources SET state = 'error', alerted_at = now()"
                    " WHERE id = :s"
                ),
                {"s": source_id},
            )
            bindings = await prompts.push_bindings(s, workspace_id)
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
                    " content_hash, file_name, media_kind, provider_file_ref)"
                    " VALUES (:ws, :src, :hash, :name, :kind, :ref)"
                    " ON CONFLICT ON CONSTRAINT uq_media_dedup DO NOTHING"
                ),
                {
                    "ws": workspace_id,
                    "src": source_id,
                    "hash": item["content_hash"],
                    "name": item.get("name") or item["ref"],
                    "kind": item["kind"],
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
                    " WHERE id = :s"
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
