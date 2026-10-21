"""One past-posts fence writer, under the common source lock order.

Enter before holding an intent/job lease or any later row lock. A consumer
that locks those first breaks the order past-posts activation relies on. Locks
are transaction scoped; provider I/O belongs after this transaction commits.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text

from src.services.target import audit, readers


@dataclass(frozen=True)
class PastPostsState:
    enabled: bool
    removed: bool
    generation_id: str | None
    credential_state: str | None
    assigned: bool

    @property
    def readable(self) -> bool:
        """On, kept, under a current generation and a live credential, assigned.

        This arms a read and nothing more: the clock and the job each check
        admission themselves (`fn_source_read_authority`).
        """
        return (
            self.enabled
            and not self.removed
            and self.generation_id is not None
            and self.credential_state == "active"
            and self.assigned
        )


def _locks(executor):
    sync = getattr(executor, "sync_session", None)
    if sync is None:
        sync = executor.sync_connection
    transaction = sync.get_transaction()
    if transaction is None:
        raise RuntimeError("source_lock_requires_transaction")
    current = executor.info.get("past_posts_source_locks")
    if current is None or current[0] is not transaction:
        current = (transaction, {})
        executor.info["past_posts_source_locks"] = current
    return current[1]


@asynccontextmanager
async def source_savepoint(executor):
    """Roll back a refused source bundle and its newly acquired lock records.

    PostgreSQL releases locks acquired after a savepoint on rollback. Mirror
    that in the helper's bookkeeping while preserving locks held on entry.
    """
    held = _locks(executor)
    snapshot = {
        ws: {kind: set(ids) for kind, ids in rows.items()} for ws, rows in held.items()
    }
    try:
        async with executor.begin_nested():
            yield
    except BaseException:
        held.clear()
        held.update(snapshot)
        raise


async def lock_workspace_sources(
    executor, *, workspace_id: str, ig_account_ids=(), source_ids=()
) -> None:
    """Lock sources advisory, case mix advisory, workspace, accounts, sources.

    Reentrant acquisition can reuse held rows; it cannot acquire new accounts
    after a source row. Credential/generation and item locks follow this helper.
    The caller may not enter with a later lock already held.
    """
    ws = str(workspace_id)
    accounts = sorted({str(value) for value in ig_account_ids})
    sources = sorted({str(value) for value in source_ids})
    held = _locks(executor).setdefault(ws, {"accounts": set(), "sources": set()})
    if held["sources"] and set(accounts) - held["accounts"]:
        raise RuntimeError("source_lock_order_violation")
    await executor.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"sources:{ws}"},
    )
    await executor.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"case_mix:{ws}"},
    )
    await executor.execute(
        text("SELECT id FROM workspaces WHERE id = :ws FOR UPDATE"), {"ws": ws}
    )
    for table, ids in (("ig_accounts", accounts), ("media_sources", sources)):
        if ids:
            await executor.execute(
                text(
                    f"SELECT id FROM {table} WHERE workspace_id = :ws AND id = ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE"
                ),
                {"ws": ws, "ids": ids},
            )
    held["accounts"].update(accounts)
    held["sources"].update(sources)


async def lock_all_workspace_sources(
    executor, *, workspace_id: str, ig_account_ids=()
) -> None:
    """Lock every source and account before any credential/generation lock.

    A later destination projection must never add a lower account after sources.
    """
    ws = str(workspace_id)
    await lock_workspace_sources(executor, workspace_id=ws)
    sources = await readers.rows(
        executor,
        "SELECT id, origin_ig_account_id FROM media_sources WHERE workspace_id = :ws ORDER BY id",
        ws=ws,
    )
    accounts = await readers.rows(
        executor, "SELECT id FROM ig_accounts WHERE workspace_id=:ws ORDER BY id", ws=ws
    )
    origins = {
        str(s["origin_ig_account_id"]) for s in sources if s["origin_ig_account_id"]
    }
    await lock_workspace_sources(
        executor,
        workspace_id=ws,
        ig_account_ids=origins
        | {str(a["id"]) for a in accounts}
        | {str(a) for a in ig_account_ids},
        source_ids=[str(s["id"]) for s in sources],
    )


def require_source_lock(executor, workspace_id: str, source_id: str) -> None:
    held = _locks(executor).get(str(workspace_id), {})
    if str(source_id) not in held.get("sources", ()):
        raise RuntimeError("source_lock_required")


async def past_posts_state(
    executor, *, workspace_id: str, source_id: str
) -> PastPostsState | None:
    require_source_lock(executor, workspace_id, source_id)
    row = await readers.row(
        executor,
        "SELECT COALESCE(x.ratio > 0, false) AS enabled,"
        " COALESCE((s.config->>'removed')::boolean, false) AS removed,"
        " g.id AS generation_id, c.state AS credential_state,"
        " EXISTS (SELECT 1 FROM media_source_destinations d WHERE d.workspace_id = s.workspace_id"
        " AND d.source_id = s.id AND d.ig_account_id = s.origin_ig_account_id AND d.state = 'active') AS assigned"
        " FROM media_sources s LEFT JOIN category_post_case_mix x"
        " ON x.workspace_id = s.workspace_id AND x.source_id = s.id AND x.effective_to IS NULL"
        " LEFT JOIN instagram_past_posts_generations g ON g.workspace_id = s.workspace_id"
        " AND g.source_id = s.id AND g.id = s.current_authorization_generation_id AND g.lifecycle = 'active'"
        " LEFT JOIN oauth_credentials c ON c.workspace_id = s.workspace_id"
        " AND c.ig_account_id = s.origin_ig_account_id AND c.provider = 'ig_login'"
        " WHERE s.workspace_id = :ws AND s.id = :sid AND s.provider = 'instagram_past_posts'",
        ws=str(workspace_id),
        sid=str(source_id),
    )
    if row is None:
        return None
    return PastPostsState(
        bool(row["enabled"]),
        bool(row["removed"]),
        str(row["generation_id"]) if row["generation_id"] else None,
        row["credential_state"],
        bool(row["assigned"]),
    )


async def apply_past_posts_transition(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    before: PastPostsState | None,
    reason: str,
) -> int:
    """Compare logical state, bump once per source/transaction, audit atomically.

    Ratios changing between positive values and encrypted token replacement do
    not alter this state. The transaction stamp prevents SCD replacement or a
    composed reconnect/restore from minting several fences for one operation.
    A change that leaves the source readable also arms its read, whether or not
    this transaction's fence was already minted: `next_sync_at` becomes now, and
    the clock mints the sync once it admits the source. A source a persistent
    failure parked in `error` comes back `active` with it, because admission
    needs `active` and an armed source that is never admitted is never read;
    any other state, `paused` included, is left as it is.
    """
    after = await past_posts_state(
        executor, workspace_id=workspace_id, source_id=source_id
    )
    if after is None:
        raise ValueError("past_posts_source_not_found")
    params = {"ws": str(workspace_id), "sid": str(source_id)}
    if after != before:
        changed = await readers.row(
            executor,
            "UPDATE media_sources SET source_use_epoch = source_use_epoch + 1,"
            " source_epoch_transaction_id = txid_current()"
            " WHERE workspace_id = :ws AND id = :sid AND provider = 'instagram_past_posts'"
            " AND source_epoch_transaction_id IS DISTINCT FROM txid_current() RETURNING source_use_epoch",
            **params,
        )
        if after.readable:
            await executor.execute(
                text(
                    "UPDATE media_sources SET next_sync_at = clock_timestamp(), alerted_at = NULL,"
                    " state = CASE WHEN state = 'error' THEN 'active' ELSE state END"
                    " WHERE workspace_id = :ws AND id = :sid AND provider = 'instagram_past_posts'"
                ),
                params,
            )
        if changed is not None:
            epoch = int(changed["source_use_epoch"])
            await audit.record(
                executor,
                workspace_id=str(workspace_id),
                entity_kind="instagram_source",
                entity_id_sql=":sid",
                sid=str(source_id),
                from_state=None,
                to_state=None,
                detail={"reason": reason, "source_use_epoch": epoch},
            )
            return epoch
    row = await readers.row(
        executor,
        "SELECT source_use_epoch FROM media_sources WHERE workspace_id = :ws AND id = :sid",
        **params,
    )
    return int(row["source_use_epoch"])
