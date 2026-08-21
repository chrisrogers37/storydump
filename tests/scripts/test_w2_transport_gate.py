"""W2 gate — outbox delivery through the assembled worker (#942 W2).

The unit tier proves the transport and the wiring shapes; this proves the
cycle on the real machinery: the sweep mints exactly one live sender job per
pending binding, the interactive lane claims it, the hold drains the queue
through the injected transport to the binding's own chat ref, rows land
`sent` with real refs, the job finalizes, and a re-sweep on a drained outbox
mints nothing. The dead-credential path is the shitpost-alpha lesson: an auth
failure marks the row ambiguous (the outbox's own recovery lane) and the
worker survives.
"""

import uuid

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.channels.telegram_transport import TelegramAuthDead
from src.services.target import unit_of_work
from src.services.target.work_loop import WorkerConfig, ensure_sender_jobs
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


def _seed_binding_with_pending(sync_conn, name: str, *, rows: int = 2):
    chain = seed_workspace_chain(sync_conn, name)
    binding = str(uuid.uuid4())
    with sync_conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO channel_bindings (id, workspace_id, channel, external_ref)"
            " VALUES (%s, %s, 'telegram_group', %s)",
            (binding, chain["ws"], f"-100{name[-3:]}42"),
        )
        for i in range(rows):
            cur.execute(
                "INSERT INTO channel_outbox (workspace_id, binding_id, kind, payload)"
                " VALUES (%s, %s, 'approval_prompt',"
                " jsonb_build_object('v', 1, 'text', %s))",
                (chain["ws"], binding, f"card {i} for {name}"),
            )
    sync_conn.commit()
    return chain, binding


class _FakeTransport:
    """for_chat-shaped; records (chat_ref, text) and returns rising refs."""

    def __init__(self, fail_with=None):
        self.sent = []
        self.fail_with = fail_with
        self.auth_failures = 0

    def for_chat(self, external_ref):
        async def send(row):
            if self.fail_with is not None:
                if isinstance(self.fail_with, TelegramAuthDead):
                    self.auth_failures += 1
                raise self.fail_with
            self.sent.append((external_ref, row["payload"]["text"]))
            return f"msg-{len(self.sent)}"

        return send

    async def probe(self):
        return "gate_bot"

    async def aclose(self):
        pass


async def _sweep(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        async with session.begin():
            await unit_of_work.apply_gucs(session, tenant_id="", actor_kind="system")
            minted = await ensure_sender_jobs(session)
    return minted


async def _run_interactive_once(lane_db, engine, transport, *, config=None):
    app = compose(
        engine=engine, config=config or WorkerConfig(), env={}, transport=transport
    )
    wl = next(wl_ for wl_ in app.loops if wl_.lane == "interactive")
    conn = await engine.connect()
    try:
        wl.bind_claim_conn(conn)
        claimed = await wl.run_once()
    finally:
        await conn.close()
    return wl, claimed


class TestDeliveryEndToEnd:
    async def test_sweep_mints_once_hold_drains_rows_land_sent_job_finalizes(
        self, lane_db, sync_conn
    ):
        chain, binding = _seed_binding_with_pending(sync_conn, "w2send", rows=2)
        engine = create_async_engine(_async_url(lane_db))
        try:
            assert await _sweep(engine) == 1, "one pending binding, one sender job"
            assert await _sweep(engine) == 0, "a live job blocks a duplicate mint"

            transport = _FakeTransport()
            cfg = WorkerConfig(poller_interval_seconds=0.05, sender_hold_seconds=10.0)
            wl, claimed = await _run_interactive_once(
                lane_db, engine, transport, config=cfg
            )

            assert claimed is True and wl.processed == 1
            assert len(transport.sent) == 2
            with sync_conn.cursor() as cur:
                cur.execute(
                    "SELECT external_ref FROM channel_bindings WHERE id = %s",
                    (binding,),
                )
                bound_ref = cur.fetchone()[0]
            assert {ref for ref, _ in transport.sent} == {bound_ref}, (
                "cards must go to the binding's own external ref"
            )
            with sync_conn.cursor() as cur:
                cur.execute(
                    "SELECT state, external_message_ref FROM channel_outbox"
                    " WHERE binding_id = %s ORDER BY created_at",
                    (binding,),
                )
                rows = cur.fetchall()
            assert [r[0] for r in rows] == ["sent", "sent"]
            assert all(r[1] for r in rows)
            with sync_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM jobs WHERE state = 'leased'")
                assert cur.fetchone()[0] == 0
            assert await _sweep(engine) == 0, "a drained outbox mints nothing"
        finally:
            await engine.dispose()


class TestDeadCredentialMidRun:
    async def test_an_auth_dead_send_marks_the_row_ambiguous_and_the_worker_survives(
        self, lane_db, sync_conn
    ):
        chain, binding = _seed_binding_with_pending(sync_conn, "w2dead", rows=1)
        engine = create_async_engine(_async_url(lane_db))
        try:
            await _sweep(engine)
            transport = _FakeTransport(fail_with=TelegramAuthDead("getMe: 401"))
            cfg = WorkerConfig(poller_interval_seconds=0.05, sender_hold_seconds=2.0)
            wl, claimed = await _run_interactive_once(
                lane_db, engine, transport, config=cfg
            )

            assert claimed is True and wl.processed == 1, (
                "the hold ends and the job finalizes; the ROW carries the state"
            )
            assert transport.auth_failures >= 1
            with sync_conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM channel_outbox WHERE binding_id = %s",
                    (binding,),
                )
                assert cur.fetchone()[0] == "ambiguous"
                cur.execute("SELECT count(*) FROM jobs WHERE state = 'leased'")
                assert cur.fetchone()[0] == 0
        finally:
            await engine.dispose()


class TestReMintThroughTheRealSweeper:
    """The claim navi refuted, proven the honest way this time: rows enqueued
    AFTER the worker is up get their sender job from the PERIODIC sweeper —
    not from a test helper calling the mint function directly — and the
    sweeper is still alive at the end. Positive controls throughout: sweeps
    and mints are counters whose absence of movement is a failure, never a
    silence."""

    async def test_rows_enqueued_after_startup_are_delivered_by_the_live_worker(
        self, lane_db, sync_conn
    ):
        import asyncio

        from src.worker import run

        chain, binding = _seed_binding_with_pending(sync_conn, "w2remint", rows=0)
        transport = _FakeTransport()
        cfg = WorkerConfig(
            lease_seconds=10.0,
            claim_idle_seconds=0.1,
            clock_interval_seconds=5.0,
            heartbeat_interval_seconds=0.5,
            sender_sweep_seconds=0.3,
            poller_interval_seconds=0.05,
            sender_hold_seconds=3.0,
        )
        engine = create_async_engine(_async_url(lane_db))
        app = compose(engine=engine, config=cfg, env={}, transport=transport)
        stop = asyncio.Event()
        runner = asyncio.create_task(run(app, stop=stop))
        try:
            await asyncio.sleep(1.0)  # worker up; outbox empty; sweeps ticking
            with sync_conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "INSERT INTO channel_outbox (workspace_id, binding_id, kind,"
                    " payload) VALUES (%s, %s, 'approval_prompt',"
                    " jsonb_build_object('v', 1, 'text', 'late card'))",
                    (chain["ws"], binding),
                )
            sync_conn.commit()

            deadline = asyncio.get_running_loop().time() + 12.0
            while not transport.sent and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.2)
        finally:
            # Wait for the third sweep on its own bounded clock before
            # stopping: the >=3 assert discriminates died-after-one-pass
            # (#958's regression, which pins sweeps at 1 forever) from a
            # loaded host where each sweep's real DB query outlasts the
            # 0.3s interval — a fixed post-hoc count conflates the two.
            sweep_deadline = asyncio.get_running_loop().time() + 10.0
            while (
                app.sweeper is not None
                and app.sweeper.sweeps < 3
                and asyncio.get_running_loop().time() < sweep_deadline
            ):
                await asyncio.sleep(0.2)
            stop.set()
            await asyncio.wait_for(runner, timeout=20.0)

        assert transport.sent, "the late row was never delivered — re-mint is broken"
        assert app.sweeper is not None and app.sweeper.sweeps >= 3, (
            "the sweeper must still be sweeping, not dead after one pass"
        )
        assert app.sweeper.mints >= 1, "the sender job must come from the SWEEPER"
        with sync_conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM channel_outbox WHERE binding_id = %s", (binding,)
            )
            assert cur.fetchone()[0] == "sent"
            cur.execute("SELECT count(*) FROM jobs WHERE state = 'leased'")
            assert cur.fetchone()[0] == 0
