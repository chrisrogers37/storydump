"""Gate — prompt production against the real machinery (#790, #942).

The transition-matrix edges, driven end to end: a due `scheduled` intent gains
its card in the same transaction as its transition; the intent is actionable
on the web queue the moment it is prompted (every workspace has web access,
#1033), so it advances to
`awaiting_approval` in the same pass whether or not a card was delivered, and
a card that failed on every push surface leaves it actionable rather than
failing it; a workspace with no push binding at all still reaches
`awaiting_approval` and simply gets no card; a workspace that cannot publish
by API gets no Post-now button; and the whole flow is idempotent — a second
sweep changes nothing.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.services.target import prompts, unit_of_work
from src.services.target.work_loop import WorkerConfig
from tests.scripts.conftest import async_url, seed_workspace_chain

pytestmark = [pytest.mark.integration]


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
            return await prompts.sweep_due_prompts(
                session, limit=50, late_seconds=WorkerConfig().planned_late_seconds
            )


class TestDueScheduledGainsItsCard:
    async def test_sweep_prompts_transitions_and_enqueues_in_one_pass(
        self, world_db, sync_conn
    ):
        chain, binding = _seed_world(sync_conn, "w3due")
        engine = create_async_engine(async_url(world_db))
        try:
            counts = await _sweep(engine)
            assert counts["prompted"] == 1
            # The web is a surface: the intent is actionable in the SAME pass,
            # before the card has been delivered anywhere.
            assert counts["advanced"] == 1
            assert _intent_state(sync_conn, chain["intent"]) == "awaiting_approval"
            cards = _cards(sync_conn, chain["intent"])
            assert len(cards) == 1 and cards[0][1] == "pending"
            payload = cards[0][2]
            assert "Posted myself" in str(payload)
            assert "Post now" not in str(payload), "api flag is off in this world"
            # The card IS the photo: the payload names the
            # media for the transport to fetch, and captions it with the slot.
            with sync_conn.cursor() as cur:
                cur.execute(
                    "SELECT m.provider_file_ref, m.source_id, i.workspace_id"
                    "  FROM post_intents i JOIN media_items m ON m.id = i.media_item_id"
                    " WHERE i.id = %s",
                    (chain["intent"],),
                )
                ref, source_id, ws = cur.fetchone()
            assert payload["v"] == 2
            assert payload["media"]["ref"] == ref
            assert payload["media"]["source_id"] == str(source_id)
            assert payload["media"]["workspace_id"] == str(ws)
            assert (
                payload["caption"].startswith("📸 ") and "Slot:" in payload["caption"]
            )

            again = await _sweep(engine)
            assert again == {"prompted": 0, "advanced": 0, "approved": 0}, (
                "idempotent: no double card"
            )
            assert len(_cards(sync_conn, chain["intent"])) == 1
        finally:
            await engine.dispose()

    async def test_api_enabled_workspace_gets_the_post_now_button(
        self, world_db, sync_conn
    ):
        chain, binding = _seed_world(sync_conn, "w3api", api_enabled=True)
        engine = create_async_engine(async_url(world_db))
        try:
            await _sweep(engine)
            payload = _cards(sync_conn, chain["intent"])[0][2]
            assert "Post now" in str(payload), "a tap publishes"
        finally:
            await engine.dispose()


class TestTheWebIsASurface:
    """`prompt_pending → awaiting_approval` on "prompt delivered on ≥ 1 binding,
    or workspace has web access". Every workspace has web access by
    construction (the web queue, #1033), so delivery evidence does not gate
    the edge."""

    async def test_delivery_after_the_fact_changes_nothing(self, world_db, sync_conn):
        chain, binding = _seed_world(sync_conn, "w3adv")
        engine = create_async_engine(async_url(world_db))
        try:
            await _sweep(engine)
            assert _intent_state(sync_conn, chain["intent"]) == "awaiting_approval"
            card_id = _cards(sync_conn, chain["intent"])[0][0]
            with sync_conn.cursor() as cur:  # the sender's outcome, minimally
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "UPDATE channel_outbox SET state = 'sent',"
                    " external_message_ref = 'tg-1' WHERE id = %s",
                    (card_id,),
                )
            sync_conn.commit()

            assert (await _sweep(engine))["advanced"] == 0
            assert _intent_state(sync_conn, chain["intent"]) == "awaiting_approval"
        finally:
            await engine.dispose()

    async def test_every_card_failed_leaves_the_intent_actionable(
        self, world_db, sync_conn
    ):
        """The web queue (#1033) is reachable regardless of what Telegram did
        with the card, so the `prompt_pending → failed` edge has no producer —
        the intent waits for a person, or for `approval_ttl` to expire it."""
        chain, binding = _seed_world(sync_conn, "w3fail")
        engine = create_async_engine(async_url(world_db))
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
            assert counts == {"prompted": 0, "advanced": 0, "approved": 0}
            assert _intent_state(sync_conn, chain["intent"]) == "awaiting_approval"
        finally:
            await engine.dispose()

    async def test_a_workspace_without_push_bindings_reaches_awaiting_approval(
        self, world_db, sync_conn
    ):
        """The Google-only workspace — no Telegram binding, ever. Its intent is
        prompted to `awaiting_approval` for the web queue (#1033), with no
        card."""
        chain = seed_workspace_chain(sync_conn, "w3nobind")
        engine = create_async_engine(async_url(world_db))
        try:
            counts = await _sweep(engine)
            assert counts == {"prompted": 1, "advanced": 1, "approved": 0}
            assert _intent_state(sync_conn, chain["intent"]) == "awaiting_approval"
            assert _cards(sync_conn, chain["intent"]) == [], "no binding, no card"
            assert (await _sweep(engine)) == {
                "prompted": 0,
                "advanced": 0,
                "approved": 0,
            }
        finally:
            await engine.dispose()
