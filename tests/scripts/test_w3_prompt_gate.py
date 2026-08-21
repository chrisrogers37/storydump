"""W3 gate — prompt production against the real machinery (#790, #942).

The `02` §4 matrix edges, driven end to end: a due `scheduled` intent gains
its card in the same transaction as its transition; delivery advances it to
`awaiting_approval` (the sweep as the correctness backstop); a card that
failed on every surface fails the intent; a workspace that cannot publish by
API gets no Post-now button; and the whole flow is idempotent — a second
sweep changes nothing.
"""

import uuid

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.services.target import prompts, unit_of_work
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


def _seed_world(sync_conn, name: str, *, api_enabled: bool = False):
    chain = seed_workspace_chain(sync_conn, name)
    binding = str(uuid.uuid4())
    with sync_conn.cursor() as cur:
        cur.execute("SET app.actor_kind = 'migration'")
        cur.execute(
            "INSERT INTO channel_bindings (id, workspace_id, channel, external_ref)"
            " VALUES (%s, %s, 'telegram_group', %s)",
            (binding, chain["ws"], f"-100{abs(hash(name)) % 10000}"),
        )
        if api_enabled:
            cur.execute(
                "UPDATE workspaces SET api_publishing_enabled = true WHERE id = %s",
                (chain["ws"],),
            )
    sync_conn.commit()
    return chain, binding


def _intent_state(conn, intent_id):
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM post_intents WHERE id = %s", (str(intent_id),))
        return cur.fetchone()[0]


def _cards(conn, intent_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state, payload FROM channel_outbox WHERE intent_id = %s",
            (str(intent_id),),
        )
        return cur.fetchall()


async def _sweep(engine, tenant_id=""):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        async with session.begin():
            await unit_of_work.apply_gucs(
                session, tenant_id=str(tenant_id), actor_kind="system"
            )
            return await prompts.sweep_due_prompts(session, limit=50)


class TestDueScheduledGainsItsCard:
    async def test_sweep_prompts_transitions_and_enqueues_in_one_pass(
        self, lane_db, sync_conn
    ):
        chain, binding = _seed_world(sync_conn, "w3due")
        engine = create_async_engine(_async_url(lane_db))
        try:
            counts = await _sweep(engine)
            assert counts["prompted"] == 1
            assert _intent_state(sync_conn, chain["intent"]) == "prompt_pending"
            cards = _cards(sync_conn, chain["intent"])
            assert len(cards) == 1 and cards[0][1] == "pending"
            payload = cards[0][2]
            assert "Posted myself" in str(payload)
            assert "Post now" not in str(payload), "api flag is off in this world"

            again = await _sweep(engine)
            assert again["prompted"] == 0, "idempotent: no double card"
            assert len(_cards(sync_conn, chain["intent"])) == 1
        finally:
            await engine.dispose()

    async def test_api_enabled_workspace_gets_the_post_now_button(
        self, lane_db, sync_conn
    ):
        chain, binding = _seed_world(sync_conn, "w3api", api_enabled=True)
        engine = create_async_engine(_async_url(lane_db))
        try:
            await _sweep(engine)
            payload = _cards(sync_conn, chain["intent"])[0][2]
            assert "Post now" in str(payload), "Fork 1: a tap publishes"
        finally:
            await engine.dispose()


class TestDeliveryAdvancesTheIntent:
    async def test_a_sent_card_moves_prompt_pending_to_awaiting_approval(
        self, lane_db, sync_conn
    ):
        chain, binding = _seed_world(sync_conn, "w3adv")
        engine = create_async_engine(_async_url(lane_db))
        try:
            await _sweep(engine)
            card_id = _cards(sync_conn, chain["intent"])[0][0]
            with sync_conn.cursor() as cur:  # the sender's outcome, minimally
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "UPDATE channel_outbox SET state = 'sent',"
                    " external_message_ref = 'tg-1' WHERE id = %s",
                    (card_id,),
                )
            sync_conn.commit()

            counts = await _sweep(engine)
            assert counts["advanced"] == 1
            assert _intent_state(sync_conn, chain["intent"]) == "awaiting_approval"
            assert (await _sweep(engine))["advanced"] == 0
        finally:
            await engine.dispose()

    async def test_every_card_failed_fails_the_intent(self, lane_db, sync_conn):
        chain, binding = _seed_world(sync_conn, "w3fail")
        engine = create_async_engine(_async_url(lane_db))
        try:
            await _sweep(engine)
            with sync_conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "UPDATE channel_outbox SET state = 'failed' WHERE intent_id = %s",
                    (str(chain["intent"]),),
                )
            sync_conn.commit()

            counts = await _sweep(engine)
            assert counts["failed_no_surface"] == 1
            assert _intent_state(sync_conn, chain["intent"]) == "failed"
        finally:
            await engine.dispose()


class TestNoSurfaceStaysScheduled:
    async def test_a_workspace_without_push_bindings_is_not_transitioned(
        self, lane_db, sync_conn
    ):
        chain = seed_workspace_chain(sync_conn, "w3nobind")
        engine = create_async_engine(_async_url(lane_db))
        try:
            counts = await _sweep(engine)
            assert counts["prompted"] == 0
            assert _intent_state(sync_conn, chain["intent"]) == "scheduled"
        finally:
            await engine.dispose()
