"""W6 gate — the media-sync executors against the real machinery (#942, #982).

The clock's leg 4 nulls `next_sync_at` at mint and ONLY the sync executor
re-arms it, so until now a minted sync job parked forever and its source
dropped out of the due-scan with the parked job as the sole carrier. These
scenarios prove the whole loop on the replayed schema: mint → list through
the drive seam → media_items upserted with the per-workspace dedup the
schema enforces → checkpoint persisted → next_sync_at re-armed with the `05`
baseline → first_ingest_chunk chained while pages remain → the error/probe
state machine (`02` §2: the sync IS the probe).

The drive door is the #982 seam; a scripted stub drives every scenario and
each stub outcome can go red on its own (the controls-can-fail standard).
"""

import json
import uuid
from datetime import datetime, timezone

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target.work_loop import WorkerConfig
from src.worker import compose
from tests.scripts.conftest import seed_workspace_chain
from tests.scripts.test_lineage_lane import run_lane

pytestmark = [pytest.mark.integration]


def _async_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture()
def lane_db(bootstrapped_db):
    run_lane(bootstrapped_db)
    return bootstrapped_db


@pytest.fixture()
def sync_conn(lane_db):
    conn = psycopg2.connect(lane_db)
    yield conn
    conn.close()


def _arm_source(conn, source_id, when="now()"):
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            f"UPDATE media_sources SET next_sync_at = {when} WHERE id = %s",
            (source_id,),
        )
    conn.commit()


def _tick(conn, max_jobs: int = 50) -> None:
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "SELECT fn_clock_tick(%s, %s::interval, %s::jsonb)",
            (max_jobs, "7 days", "{}"),
        )
    conn.commit()


def _source_row(conn, source_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, next_sync_at, sync_checkpoint, last_sync_success_at,"
            "       alerted_at"
            " FROM media_sources WHERE id = %s",
            (source_id,),
        )
        r = cur.fetchone()
    return {
        "state": r[0],
        "next_sync_at": r[1],
        "sync_checkpoint": r[2],
        "last_sync_success_at": r[3],
        "alerted_at": r[4],
    }


def _jobs(conn, kind):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, payload, state FROM jobs WHERE kind = %s ORDER BY created_at",
            (kind,),
        )
        return [{"id": r[0], "payload": r[1], "state": r[2]} for r in cur.fetchall()]


def _items(conn, ws):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT provider_file_ref, content_hash, state FROM media_items"
            " WHERE workspace_id = %s ORDER BY provider_file_ref",
            (ws,),
        )
        return cur.fetchall()


class ScriptedDrive:
    """The #982 seam, scripted. `pages` is a list of (items, next_page_token);
    an exception instance anywhere in the list is RAISED at that call."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def list_changes(self, config, checkpoint):
        self.calls.append({"config": dict(config or {}), "checkpoint": checkpoint})
        step = self.pages.pop(0)
        if isinstance(step, Exception):
            raise step
        items, token = step
        return items, {"v": 1, **({"page_token": token} if token else {})}


def _item(ref, h=None, kind="image"):
    return {
        "ref": ref,
        "name": f"{ref}.jpg",
        "kind": kind,
        "content_hash": h or f"hash-{ref}",
    }


async def _run_once_w6(lane_db, drive):
    engine = create_async_engine(_async_url(lane_db))
    try:
        app = compose(engine=engine, config=WorkerConfig(), env={}, drive=drive)
        wl = next(wl_ for wl_ in app.loops if wl_.lane == "bulk")
        conn = await engine.connect()
        try:
            wl.bind_claim_conn(conn)
            claimed = await wl.run_once()
        finally:
            await conn.close()
        return wl, claimed
    finally:
        await engine.dispose()


class TestBaselineSyncEndToEnd:
    @pytest.mark.asyncio
    async def test_mint_list_upsert_checkpoint_and_rearm(self, lane_db, sync_conn):
        chain = seed_workspace_chain(sync_conn, "w6-base")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        minted = _jobs(sync_conn, "sync_media_source")
        assert len(minted) == 1 and minted[0]["payload"]["reason"] == "baseline"
        assert _source_row(sync_conn, chain["src"])["next_sync_at"] is None, (
            "leg 4 nulls at mint; only the executor re-arms"
        )

        drive = ScriptedDrive([([_item("f1"), _item("f2")], None)])
        wl, claimed = await _run_once_w6(lane_db, drive)
        assert claimed is True and wl.processed == 1

        rows = _items(sync_conn, chain["ws"])
        refs = [r[0] for r in rows]
        assert "f1" in refs and "f2" in refs
        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "active"
        assert src["last_sync_success_at"] is not None
        assert src["sync_checkpoint"] == {"v": 1}
        assert src["next_sync_at"] is not None, "the executor must re-arm"
        eta = (src["next_sync_at"] - datetime.now(timezone.utc)).total_seconds()
        assert 5 * 3600 < eta < 8 * 3600, (
            f"re-arm must be the 05 baseline (6h jittered), got {eta}s"
        )
        assert _jobs(sync_conn, "sync_media_source")[0]["state"] == "succeeded"
        assert drive.calls[0]["checkpoint"] is None or drive.calls[0]["checkpoint"] == {
            "v": 1
        }

    @pytest.mark.asyncio
    async def test_the_workspace_hash_dedup_is_respected_not_violated(
        self, lane_db, sync_conn
    ):
        """A second sync offering the same content hash must not crash on
        uq_media_dedup and must not mint a second row."""
        chain = seed_workspace_chain(sync_conn, "w6-dedup")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        drive = ScriptedDrive([([_item("d1", h="samehash")], None)])
        await _run_once_w6(lane_db, drive)

        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        drive2 = ScriptedDrive([([_item("d1-renamed", h="samehash")], None)])
        wl, _ = await _run_once_w6(lane_db, drive2)
        assert wl.processed == 1
        hashes = [r[1] for r in _items(sync_conn, chain["ws"])]
        assert hashes.count("samehash") == 1, "per-workspace dedup by schema"
        assert _jobs(sync_conn, "sync_media_source")[-1]["state"] == "succeeded"


class TestChunkChaining:
    @pytest.mark.asyncio
    async def test_pages_chain_first_ingest_chunk_and_the_last_rearms(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w6-chain")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)

        drive = ScriptedDrive(
            [
                ([_item("p1")], "tok-2"),
                ([_item("p2")], "tok-3"),
                ([_item("p3")], None),
            ]
        )
        wl, _ = await _run_once_w6(lane_db, drive)
        assert wl.processed == 1
        chunks = _jobs(sync_conn, "first_ingest_chunk")
        assert len(chunks) == 1 and chunks[0]["payload"]["page_token"] == "tok-2", (
            "a page remaining must chain a first_ingest_chunk with the token"
        )
        assert _source_row(sync_conn, chain["src"])["next_sync_at"] is None, (
            "mid-chain must NOT re-arm — the chain is the carrier"
        )

        wl, _ = await _run_once_w6(lane_db, drive)
        chunks = _jobs(sync_conn, "first_ingest_chunk")
        assert len(chunks) == 2 and chunks[1]["payload"]["page_token"] == "tok-3"

        wl, _ = await _run_once_w6(lane_db, drive)
        src = _source_row(sync_conn, chain["src"])
        assert src["next_sync_at"] is not None, "the LAST chunk re-arms"
        assert src["last_sync_success_at"] is not None
        refs = [r[0] for r in _items(sync_conn, chain["ws"])]
        assert {"p1", "p2", "p3"} <= set(refs)
        assert {j["state"] for j in _jobs(sync_conn, "first_ingest_chunk")} == {
            "succeeded"
        }


class TestFailureRouting:
    @pytest.mark.asyncio
    async def test_transient_failure_rides_the_ladder_and_arms_nothing(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w6-flaky")
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        drive = ScriptedDrive([RuntimeError("network blip")])
        wl, claimed = await _run_once_w6(lane_db, drive)
        assert claimed is True
        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "active"
        assert src["next_sync_at"] is None, "the alive job is the carrier"
        job = _jobs(sync_conn, "sync_media_source")[0]
        assert job["state"] == "ready", "on the ladder, not dead, not done"

    @pytest.mark.asyncio
    async def test_persistent_failure_flips_error_alerts_once_and_succeeds(
        self, lane_db, sync_conn
    ):
        from src.services.target.media_sync import DriveSourceGone

        chain = seed_workspace_chain(sync_conn, "w6-gone")
        binding = str(uuid.uuid4())
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO channel_bindings (id, workspace_id, channel, external_ref)"
                " VALUES (%s, %s, 'telegram_group', '-100666001')",
                (binding, chain["ws"]),
            )
        sync_conn.commit()
        _arm_source(sync_conn, chain["src"])
        _tick(sync_conn)
        drive = ScriptedDrive([DriveSourceGone("folder deleted")])
        wl, _ = await _run_once_w6(lane_db, drive)
        assert wl.processed == 1
        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "error", "persistent failure is the error state"
        assert src["alerted_at"] is not None
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM channel_outbox"
                " WHERE binding_id = %s AND kind = 'notification'",
                (binding,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1 and "sync" in rows[0][0]["text"].lower()
        assert _jobs(sync_conn, "sync_media_source")[0]["state"] == "succeeded", (
            "a classified persistent failure is handled work, not a retry loop"
        )

    @pytest.mark.asyncio
    async def test_an_error_source_recovers_to_active_on_a_successful_sync(
        self, lane_db, sync_conn
    ):
        """`02` §2: error → active on successful sync — the sync IS the probe."""
        chain = seed_workspace_chain(sync_conn, "w6-probe")
        with sync_conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "UPDATE media_sources SET state = 'error', alerted_at = now()"
                " WHERE id = %s",
                (chain["src"],),
            )
            cur.execute(
                "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                " run_at, max_attempts, payload)"
                " VALUES ('sync_media_source', %s, 'bulk', %s, now(), 5, %s::jsonb)",
                (
                    chain["ws"],
                    f"src:{chain['src']}",
                    json.dumps(
                        {"v": 1, "source_id": str(chain["src"]), "reason": "demand"}
                    ),
                ),
            )
        sync_conn.commit()
        drive = ScriptedDrive([([_item("back")], None)])
        wl, _ = await _run_once_w6(lane_db, drive)
        assert wl.processed == 1
        src = _source_row(sync_conn, chain["src"])
        assert src["state"] == "active", "recovery is the successful sync itself"
        assert src["alerted_at"] is None, "recovery clears the alert dedup"
