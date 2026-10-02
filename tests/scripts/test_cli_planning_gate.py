"""Planning a story through the REAL CLI against the real app as the
production role (#1413 phase 5): the plan's verification, end to end.

A person's operator token schedules an item for a time tomorrow, naming the
account by its handle; `storydump planned` lists it; the worker's prompt
sweeper leaves it alone until its time and then serves it, and it waits in
`awaiting_approval`, as any story does. The same token moves it and cancels
it, each write an audit row naming the person over the `cli` channel. A
readonly token and a service identity read what is coming and cannot plan.

The CLI is the one `test_cli_writes_gate.py` drives: a sync client in a worker
thread, bridged onto the app on this test's loop.
"""

from __future__ import annotations

import asyncio

import psycopg2
import pytest

from src.services.target.vocabulary import (
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_REFUSED,
    NO_PUSH_BINDING,
)
from src.services.target.work_loop import WorkerConfig
from tests.scripts import test_cli_writes_gate as cli_gate
from tests.scripts import test_planned_serve_gate as planned_serve
from tests.scripts.test_cli_writes_gate import LoopBridge, _cli, _runtime, _state
from tests.scripts.test_ops_views_gate import _run, _sql
from tests.scripts.test_schedule_verbs_gate import _local
from tests.src.api.conftest import api_client

pytestmark = cli_gate.pytestmark

#: `test_cli_writes_gate`'s world and people, re-registered as this module's
#: own: a workspace made through the API (in America/New_York), an operator
#: and a readonly token of its owner, and a service identity.
world = cli_gate.world
people = cli_gate.people

HANDLE = "planning.studio"
ZONE = "America/New_York"


@pytest.fixture(scope="module")
def planning(world, people):
    """One account with a handle and four items in `people`'s workspace."""
    conn = psycopg2.connect(world["stream"])
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO media_sources (workspace_id, provider, config)"
                " VALUES (%s, 'gdrive', '{\"v\": 1}') RETURNING id",
                (people["ws"],),
            )
            (source,) = cur.fetchone()
            cur.execute(
                "INSERT INTO ig_accounts (workspace_id, provider_account_ref, handle)"
                " VALUES (%s, 'acct-planning', %s) RETURNING id",
                (people["ws"], HANDLE),
            )
            (account,) = cur.fetchone()
            items = []
            for n in range(4):
                cur.execute(
                    "INSERT INTO media_items (workspace_id, source_id, content_hash,"
                    " file_name, media_kind, provider_file_ref)"
                    " VALUES (%s, %s, %s, %s, 'image', %s) RETURNING id",
                    (
                        people["ws"],
                        source,
                        f"h-plan-{n}",
                        f"plan-{n}.jpg",
                        f"r-plan-{n}",
                    ),
                )
                items.append(str(cur.fetchone()[0]))
    finally:
        conn.close()
    return {"account": str(account), "items": items}


def _tomorrow(hour: int) -> str:
    """A whole hour tomorrow in the workspace's zone, as typed: always hours
    ahead, and clear of any clock change."""
    return _local(hour=hour, tz=ZONE)


async def _plan(rt, item: str, account: str, at: str, ws: str, *extra: str):
    return await _cli(
        rt,
        "schedule",
        item,
        "--account",
        account,
        "--at",
        at,
        "--workspace",
        ws,
        *extra,
    )


def _rows(doc) -> list:
    """Every workspace's rows in a `planned` answer."""
    return [r for w in doc["data"]["workspaces"] for r in w["rows"]]


def _local_slot(world, story: str) -> str:
    """The story's time as a wall time in the workspace's zone."""
    ((local,),) = _sql(
        world["stream"],
        "SELECT to_char(schedule_slot_at AT TIME ZONE %s, 'YYYY-MM-DD HH24:MI')"
        " FROM post_intents WHERE id = %s",
        (ZONE, story),
    )
    return local


def _audit(dsn, intent_id, event):
    return _sql(
        dsn,
        "SELECT actor_kind, channel FROM audit_events"
        " WHERE entity_id = %s AND detail->>'event' = %s",
        (intent_id, event),
    )


def _serve(world):
    """One beat of the worker's prompt sweeper, as the worker runs it: as
    `svc_worker`, with its own late window and page."""
    return planned_serve._serve(
        {"owner": world["stream"]}, limit=WorkerConfig().prompt_sweep_limit
    )


def test_the_cli_plans_a_story_lists_it_and_at_its_time_it_asks(
    world, people, planning, tmp_path
):
    ws, item = people["ws"], planning["items"][0]
    local_at = _tomorrow(12)

    async def plan():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            code, doc = await _plan(rt, item, HANDLE, local_at, ws)
            assert code == EXIT_OK, doc
            listed = await _cli(rt, "planned", "--workspace", ws)
            return doc, listed

    doc, (code, listed) = _run(plan())
    result = doc["data"]["result"]
    story = result["intent_id"]
    assert doc["data"]["args"]["ig_account_id"] == planning["account"], (
        "the handle resolved through the account view"
    )
    assert result["tz"] == ZONE and result["local_at"] == f"{local_at}:00"
    assert result["warnings"] == [NO_PUSH_BINDING], "the workspace binds no chat"
    ((origin, state, by),) = _sql(
        world["stream"],
        "SELECT origin, state, scheduled_by_user_id IS NOT NULL FROM post_intents"
        " WHERE id = %s",
        (story,),
    )
    assert (origin, state, by) == ("planned", "scheduled", True)
    assert _audit(world["stream"], story, "scheduled") == [("user", "cli")]

    assert code == EXIT_OK, listed
    (entry,) = [r for r in _rows(listed) if r["id"] == story]
    assert entry["origin"] == "planned"
    assert entry["tz"] == ZONE and entry["account_handle"] == HANDLE
    assert entry["scheduled_by"] and "@" not in entry["scheduled_by"]

    assert _local_slot(world, story) == local_at, "the time typed, in its zone"
    _serve(world)
    assert _state(world["stream"], story) == "scheduled", "not served before its time"

    # its time comes (the slot moved into the past stands in for the wait)
    _sql(
        world["stream"],
        "UPDATE post_intents SET schedule_slot_at = now() - interval '10 seconds'"
        " WHERE id = %s",
        (story,),
    )
    counts = _serve(world)
    assert counts["prompted"] >= 1, counts
    assert _state(world["stream"], story) == "awaiting_approval"


def test_the_cli_moves_and_cancels_a_planned_story(world, people, planning, tmp_path):
    ws, item = people["ws"], planning["items"][1]

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            code, doc = await _plan(rt, item, planning["account"], _tomorrow(13), ws)
            assert code == EXIT_OK, doc
            story = doc["data"]["result"]["intent_id"]
            later = _tomorrow(15)
            code, doc = await _cli(
                rt, "reschedule", story, "--at", later, "--workspace", ws
            )
            assert code == EXIT_OK, doc
            assert doc["data"]["result"]["local_at"] == f"{later}:00"
            code, doc = await _cli(rt, "cancel", story, "--workspace", ws)
            assert code == EXIT_OK, doc
            code, listed = await _cli(rt, "planned", "--workspace", ws)
            assert code == EXIT_OK, listed
            return story, listed

    story, listed = _run(main())
    ((moved_to,),) = _sql(
        world["stream"],
        "SELECT detail->>'local_at' FROM audit_events"
        " WHERE entity_id = %s AND detail->>'event' = 'rescheduled'",
        (story,),
    )
    assert _local_slot(world, story) == moved_to[:16]
    for event in ("scheduled", "rescheduled", "cancel_requested"):
        assert _audit(world["stream"], story, event) == [("user", "cli")], event
    (entry,) = [r for r in _rows(listed) if r["id"] == story]
    assert entry["cancel_requested"] is True


def test_a_lock_holds_the_cli_back_until_overridden(world, people, planning, tmp_path):
    ws, item = people["ws"], planning["items"][2]
    _sql(
        world["stream"],
        "INSERT INTO post_locks (workspace_id, media_item_id, kind, expires_at)"
        " VALUES (%s, %s, 'skip', now() + interval '7 days')",
        (ws, item),
    )

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            args = (rt, item, HANDLE, _tomorrow(14), ws)
            code, doc = await _plan(*args)
            assert code == EXIT_REFUSED, doc
            assert doc["error"]["reason"] == "locked"
            assert doc["error"]["fix"] == (
                "run it again with --override-locks to schedule it anyway"
            ), "the body said the override gets past it"
            code, doc = await _plan(*args, "--override-locks")
            assert code == EXIT_OK, doc
            assert doc["data"]["result"]["overridden"] == ["skip"]

    _run(main())


def test_a_readonly_token_and_a_service_identity_read_but_cannot_plan(
    world, people, planning, tmp_path
):
    ws = people["ws"]
    ((before,),) = _sql(world["stream"], "SELECT count(*) FROM post_intents")

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            operator = _runtime(people["operator"], bridge, tmp_path)
            code, doc = await _plan(
                operator, planning["items"][3], HANDLE, _tomorrow(17), ws
            )
            assert code == EXIT_OK, doc
            story = doc["data"]["result"]["intent_id"]
            for secret in (people["readonly"], people["service"]):
                rt = _runtime(secret, bridge, tmp_path)
                code, doc = await _plan(
                    rt, planning["items"][0], planning["account"], _tomorrow(16), ws
                )
                assert code == EXIT_NOT_AUTHORIZED, doc
                assert doc["error"]["reason"] == "readonly_token"
                code, doc = await _cli(rt, "planned", "--workspace", ws)
                assert code == EXIT_OK, doc
                assert story in {r["id"] for r in _rows(doc)}, (
                    "an empty page is no read"
                )

    _run(main())
    ((after,),) = _sql(world["stream"], "SELECT count(*) FROM post_intents")
    assert after == before + 1, "the operator's story is the only one planned"
