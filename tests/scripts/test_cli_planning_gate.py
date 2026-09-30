"""Planning a story through the REAL CLI against the real app as the
production role (#1413 phase 5): the plan's verification, end to end.

A person's operator token schedules an item for a time tomorrow, naming the
account by its handle; `storydump planned` lists it; the worker's prompt
sweeper leaves it alone until its time and then serves it, and it waits in
`awaiting_approval`, as any story does. The same token moves it and cancels it, each write an audit row
naming the person over the `cli` channel. A readonly token and a service
identity read what is coming and cannot plan.

The CLI is the one `test_cli_writes_gate.py` drives: a sync client in a worker
thread, bridged onto the app on this test's loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import pytest

from sqlalchemy import text

from src.services.target import prompts, unit_of_work
from src.services.target.vocabulary import (
    EXIT_NOT_AUTHORIZED,
    EXIT_OK,
    EXIT_REFUSED,
    NO_PUSH_BINDING,
)
from src.services.target.work_loop import WorkerConfig
from tests.scripts import test_cli_writes_gate as cli_gate
from tests.scripts.conftest import as_user, ingress_engine
from tests.scripts.test_cli_writes_gate import LoopBridge, _cli, _runtime, _state
from tests.scripts.test_ops_views_gate import _run, _sql
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


def _tomorrow_at(hour: int) -> str:
    """A wall time tomorrow in the workspace's zone, as typed: a whole hour in
    the afternoon, clear of any clock change and always hours ahead (minutes
    added to an aware time can land in a skipped hour)."""
    day = datetime.now(ZoneInfo(ZONE)).date() + timedelta(days=1)
    return f"{day.isoformat()} {hour:02d}:00"


def _audit(dsn, intent_id, event):
    return _sql(
        dsn,
        "SELECT actor_kind, channel FROM audit_events"
        " WHERE entity_id = %s AND detail->>'event' = %s",
        (intent_id, event),
    )


def _serve(world):
    """One pass of the worker's prompt sweeper, as the worker runs it: as
    `svc_worker`, with the worker's own late window and page."""
    cfg = WorkerConfig()

    async def go():
        async with ingress_engine(as_user(world["stream"], "svc_worker")) as engine:
            sessions = unit_of_work.make_session_for(engine)
            async with sessions({}) as session:
                who = (await session.execute(text("SELECT current_user"))).scalar()
                assert who == "svc_worker", who
                return await prompts.sweep_due_prompts(
                    session,
                    late_seconds=cfg.planned_late_seconds,
                    limit=cfg.prompt_sweep_limit,
                )

    return asyncio.run(go())


def test_the_cli_plans_a_story_lists_it_and_at_its_time_it_asks(
    world, people, planning, tmp_path
):
    ws, item = people["ws"], planning["items"][0]
    local_at = _tomorrow_at(12)

    async def plan():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            code, doc = await _cli(
                rt,
                "schedule",
                item,
                "--account",
                HANDLE,
                "--at",
                local_at,
                "--workspace",
                ws,
            )
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
    rows = [r for w in listed["data"]["workspaces"] for r in w["rows"]]
    (entry,) = [r for r in rows if r["id"] == story]
    assert entry["origin"] == "planned"
    assert entry["tz"] == ZONE and entry["account_handle"] == HANDLE
    assert entry["scheduled_by"] and "@" not in entry["scheduled_by"]

    ((slot,),) = _sql(
        world["stream"],
        "SELECT to_char(schedule_slot_at AT TIME ZONE %s, 'YYYY-MM-DD HH24:MI')"
        " FROM post_intents WHERE id = %s",
        (ZONE, story),
    )
    assert slot == local_at, "the time typed, in the account's zone"
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
            code, doc = await _cli(
                rt,
                "schedule",
                item,
                "--account",
                planning["account"],
                "--at",
                _tomorrow_at(13),
                "--workspace",
                ws,
            )
            assert code == EXIT_OK, doc
            story = doc["data"]["result"]["intent_id"]
            later = _tomorrow_at(15)
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
    ((local,),) = _sql(
        world["stream"],
        "SELECT to_char(schedule_slot_at AT TIME ZONE %s, 'YYYY-MM-DD HH24:MI')"
        " FROM post_intents WHERE id = %s",
        (ZONE, story),
    )
    assert (
        local
        == _sql(
            world["stream"],
            "SELECT detail->>'local_at' FROM audit_events"
            " WHERE entity_id = %s AND detail->>'event' = 'rescheduled'",
            (story,),
        )[0][0][:16]
    )
    for event in ("scheduled", "rescheduled", "cancel_requested"):
        assert _audit(world["stream"], story, event) == [("user", "cli")], event
    rows = [r for w in listed["data"]["workspaces"] for r in w["rows"]]
    (entry,) = [r for r in rows if r["id"] == story]
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
            args = ("schedule", item, "--account", HANDLE, "--at", _tomorrow_at(14))
            code, doc = await _cli(rt, *args, "--workspace", ws)
            assert code == EXIT_REFUSED, doc
            assert doc["error"]["reason"] == "locked"
            assert doc["error"]["fix"] == (
                "run it again with --override-locks to schedule it anyway"
            ), "the body said the override gets past it"
            code, doc = await _cli(rt, *args, "--workspace", ws, "--override-locks")
            assert code == EXIT_OK, doc
            assert doc["data"]["result"]["overridden"] == ["skip"]

    _run(main())


def test_a_readonly_token_and_a_service_identity_read_but_cannot_plan(
    world, people, planning, tmp_path
):
    ws = people["ws"]

    async def plan():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            rt = _runtime(people["operator"], bridge, tmp_path)
            code, doc = await _cli(
                rt,
                "schedule",
                planning["items"][3],
                "--account",
                HANDLE,
                "--at",
                _tomorrow_at(17),
                "--workspace",
                ws,
            )
            assert code == EXIT_OK, doc
            return doc["data"]["result"]["intent_id"]

    story = _run(plan())
    ((before,),) = _sql(world["stream"], "SELECT count(*) FROM post_intents")

    async def main():
        async with api_client(world["ingress"]) as (client, engine):
            bridge = LoopBridge(client._transport, asyncio.get_running_loop())
            for secret in (people["readonly"], people["service"]):
                rt = _runtime(secret, bridge, tmp_path)
                code, doc = await _cli(
                    rt,
                    "schedule",
                    planning["items"][0],
                    "--account",
                    planning["account"],
                    "--at",
                    _tomorrow_at(16),
                    "--workspace",
                    ws,
                )
                assert code == EXIT_NOT_AUTHORIZED, doc
                assert doc["error"]["reason"] == "readonly_token"
                code, doc = await _cli(rt, "planned", "--workspace", ws)
                assert code == EXIT_OK, doc
                rows = [r for w in doc["data"]["workspaces"] for r in w["rows"]]
                assert story in {r["id"] for r in rows}, "an empty page is no read"

    _run(main())
    assert _sql(world["stream"], "SELECT count(*) FROM post_intents") == [(before,)]
