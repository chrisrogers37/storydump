"""The harness's world: W workspaces × C sent cards × B bound groups, each
workspace with one linked tapper — the shape `02_api-under-load.md` step 5
names (50 × 20 × 2), built on the suite's own seeders and the tap gate's
card shape, as the migration actor.

Every card is an `awaiting_approval` intent with an `approval_prompt` row
`sent` in each binding under a numeric message ref, exactly what a live card
looks like to the route (the tap names the ref) and to the sender (the
supersede edits it). The clock is pushed a week out so no slot mints a card
mid-run, and the chain's own intent is retired so the prompt sweep has
nothing to send."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

import psycopg2

from tests.scripts.conftest import seed_workspace_chain


@dataclass
class Binding:
    id: str
    chat: str  # Telegram chat id as Telegram sends it (negative for groups)


@dataclass
class Card:
    intent: str
    refs: dict[str, str]  # chat → external_message_ref


@dataclass
class Workspace:
    id: str
    tapper_tg: str
    bindings: list[Binding]
    cards: list[Card]


@dataclass
class World:
    workspaces: list[Workspace] = field(default_factory=list)

    def cards(self) -> list[tuple[Workspace, Card]]:
        return [(w, c) for w in self.workspaces for c in w.cards]


def seed_world(
    stream_dsn: str,
    *,
    workspaces: int,
    cards_per_workspace: int,
    bindings_per_workspace: int = 2,
    tag: str = "load",
    first_chat: int = -100_000_000,
    first_tapper: int = 1_000_000,
) -> World:
    world = World()
    conn = psycopg2.connect(stream_dsn)
    try:
        for i in range(workspaces):
            chain = seed_workspace_chain(conn, f"{tag}-{i}")
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET app.actor_kind = 'migration'")
                cur.execute(
                    "UPDATE workspaces SET api_publishing_enabled = false WHERE id = %s",
                    (chain["ws"],),
                )
                cur.execute(
                    "UPDATE ig_accounts SET next_slot_at = now() + interval '7 days'"
                    " WHERE id = %s",
                    (chain["iga"],),
                )
                # The chain's own `scheduled` intent: pushed a week out so the
                # prompt sweep mints nothing for it mid-run (a terminal flip is
                # not the guard's to allow from `scheduled`).
                cur.execute(
                    "UPDATE post_intents SET schedule_slot_at = now() + interval '7 days'"
                    " WHERE id = %s",
                    (chain["intent"],),
                )
                tapper = str(first_tapper + i)
                cur.execute(
                    "INSERT INTO user_identities"
                    " (user_id, provider, external_id, display_name, verified_at)"
                    " VALUES (%s, 'telegram', %s, %s, now())",
                    (chain["user"], tapper, f"Tapper {i}"),
                )
                bindings = []
                for b in range(bindings_per_workspace):
                    chat = str(first_chat - i * 10 - b)
                    cur.execute(
                        "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                        " VALUES (%s, 'telegram_group', %s) RETURNING id",
                        (chain["ws"], chat),
                    )
                    bindings.append(Binding(id=str(cur.fetchone()[0]), chat=chat))
                cards = []
                for j in range(cards_per_workspace):
                    cur.execute(
                        "INSERT INTO media_items (workspace_id, source_id, content_hash,"
                        " file_name, media_kind, provider_file_ref)"
                        " VALUES (%s, %s, %s, 'f.jpg', 'image', %s) RETURNING id",
                        (
                            chain["ws"],
                            chain["src"],
                            f"hash-{tag}-{i}-{j}",
                            f"ref-{tag}-{i}-{j}",
                        ),
                    )
                    media = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO post_intents (workspace_id, ig_account_id, media_item_id,"
                        " provider_account_ref, approval_mode, schedule_slot_at, state)"
                        " VALUES (%s, %s, %s, %s, 'manual', now(), 'awaiting_approval')"
                        " RETURNING id",
                        (chain["ws"], chain["iga"], media, f"acct-{tag}-{i}"),
                    )
                    intent = str(cur.fetchone()[0])
                    refs = {}
                    for b, binding in enumerate(bindings, start=1):
                        ref = str(b * 1_000_000 + j * 100 + (uuid.uuid4().int % 97))
                        cur.execute(
                            "INSERT INTO channel_outbox (workspace_id, binding_id, kind,"
                            " intent_id, payload, state, external_message_ref)"
                            " VALUES (%s, %s, 'approval_prompt', %s, %s, 'sent', %s)",
                            (
                                chain["ws"],
                                binding.id,
                                intent,
                                json.dumps(
                                    {
                                        "v": 2,
                                        "text": f"📸 f.jpg ({tag}-{i}-{j})\nSlot: soon",
                                        "sent_as": "text",
                                    }
                                ),
                                ref,
                            ),
                        )
                        refs[binding.chat] = ref
                    cards.append(Card(intent=intent, refs=refs))
            world.workspaces.append(
                Workspace(
                    id=str(chain["ws"]),
                    tapper_tg=tapper,
                    bindings=bindings,
                    cards=cards,
                )
            )
    finally:
        conn.close()
    return world
