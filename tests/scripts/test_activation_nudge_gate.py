"""The activation nudge's door and latch, against PostgreSQL (106, #1481).

`fn_activation_stalled(p_since, p_stall, p_limit)` lists who is owed the
nudge: active people who signed up since `p_since`, have an email and were
never nudged, whose first missing stage is 3 (Instagram), 4 (a folder) or 5 (a
first approval, only once a card was offered), and who have done nothing for
`p_stall`. It reads `fn_activation_stages`, the stage definition it shares with
104's funnel, which `test_activation_funnel_gate.py` holds to its old counts.

One estate, seeded through the stream login with every timestamp relative to
`now()`, each person with an address unless the name says otherwise:

- AT_INSTAGRAM: a workspace 99 hours old and nothing since (stage 3, owed);
- AT_INSTAGRAM_FRESH: the same, 19 hours old (not idle yet);
- AT_FOLDER: an account connected 110 hours ago and no folder (stage 4, owed);
- AT_APPROVAL_CARD: a folder 140 hours ago and a card offered 130 hours ago,
  never approved (stage 5, owed);
- AT_APPROVAL_NO_CARD: a folder 100 hours ago and no card ever offered (the
  product's stall, never mailed);
- AT_APPROVAL_FRESH_CARD: a folder 140 hours ago and a first card 10 hours ago
  (the idle clock starts at the card);
- NO_WORKSPACE: signed up 100 hours ago and nothing else (stage 2, never mailed);
- NUDGED, NO_EMAIL, DISABLED and EARLY: stage 3 and idle, but already nudged,
  without an address, disabled, or signed up before the window.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import psycopg2
import psycopg2.errors
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import activation_nudge, unit_of_work
from tests.scripts.conftest import (
    _scratch,
    as_user,
    async_url,
    replay_advertised_stream,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: The sweep's read with the worker's defaults: 30 days of sign-ups, 72 hours idle.
STALLED = (
    "SELECT o_user_id, o_stage FROM fn_activation_stalled("
    "now() - interval '30 days', interval '72 hours', %s)"
)

ORIGIN = "https://app.example"


def _person(cur, name: str, *, ago: str, email: bool = True, **extra) -> str:
    cur.execute(
        "INSERT INTO users (primary_email, state, activation_nudge_at, created_at)"
        " VALUES (%s, %s, %s, now() - %s::interval) RETURNING id",
        (
            f"{name}@example.com" if email else None,
            extra.get("state", "active"),
            extra.get("nudged_at"),
            ago,
        ),
    )
    return str(cur.fetchone()[0])


def _workspace(cur, owner: str, name: str, *, ago: str) -> str:
    """A workspace and its owner row, the pair `ct_workspaces_owner_at_insert`
    requires by commit."""
    cur.execute(
        "INSERT INTO workspaces (name, created_at) VALUES (%s, now() - %s::interval)"
        " RETURNING id",
        (name, ago),
    )
    ws = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role)"
        " VALUES (%s, %s, 'owner')",
        (ws, owner),
    )
    return ws


def _instagram(cur, ws: str, ref: str, *, ago: str) -> None:
    cur.execute(
        "INSERT INTO ig_accounts (workspace_id, provider_account_ref, created_at)"
        " VALUES (%s, %s, now() - %s::interval)",
        (ws, ref, ago),
    )


def _folder(cur, ws: str, *, ago: str) -> None:
    cur.execute(
        "INSERT INTO media_sources (workspace_id, provider, config, created_at)"
        " VALUES (%s, 'gdrive', '{\"v\": 1}', now() - %s::interval)",
        (ws, ago),
    )


def _card(cur, ws: str, *, ago: str) -> None:
    """A story entering `awaiting_approval`, in the shape `trg_intent_audit`
    writes (no detail): a card was offered."""
    cur.execute(
        "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
        " from_state, to_state, actor_kind, created_at)"
        " VALUES (%s, 'post_intent', %s, 'prompt_pending', 'awaiting_approval',"
        " 'system', now() - %s::interval)",
        (ws, str(uuid.uuid4()), ago),
    )


def _set_up(cur, name: str, *, ago: str, ig=None, folder=None, card=None) -> str:
    """A person 1 hour older than their workspace, then whichever stages are given."""
    person = _person(cur, name, ago=f"{int(ago.split()[0]) + 1} hours")
    ws = _workspace(cur, person, f"nudge-{name}", ago=ago)
    if ig:
        _instagram(cur, ws, f"acct-nudge-{name}", ago=ig)
    if folder:
        _folder(cur, ws, ago=folder)
    if card:
        _card(cur, ws, ago=card)
    return person


def _seed(conn) -> dict:
    """The estate in the module docstring, committed."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.actor_kind = 'migration'")
        people = {
            "AT_INSTAGRAM": _set_up(cur, "instagram", ago="99 hours"),
            "AT_INSTAGRAM_FRESH": _set_up(cur, "instagram-fresh", ago="19 hours"),
            "AT_FOLDER": _set_up(cur, "folder", ago="120 hours", ig="110 hours"),
            "AT_APPROVAL_CARD": _set_up(
                cur,
                "card",
                ago="160 hours",
                ig="150 hours",
                folder="140 hours",
                card="130 hours",
            ),
            "AT_APPROVAL_NO_CARD": _set_up(
                cur, "no-card", ago="120 hours", ig="110 hours", folder="100 hours"
            ),
            "AT_APPROVAL_FRESH_CARD": _set_up(
                cur,
                "fresh-card",
                ago="160 hours",
                ig="150 hours",
                folder="140 hours",
                card="10 hours",
            ),
            "NO_WORKSPACE": _person(cur, "no-workspace", ago="100 hours"),
        }
        for name, extra in (
            ("NUDGED", {"nudged_at": "2026-01-01T00:00:00Z"}),
            ("NO_EMAIL", {"email": False}),
            ("DISABLED", {"state": "disabled"}),
        ):
            email = extra.pop("email", True)
            person = _person(cur, name.lower(), ago="100 hours", email=email, **extra)
            _workspace(cur, person, f"nudge-{name.lower()}", ago="99 hours")
            people[name] = person
        early = _person(cur, "early", ago="40 days")
        _workspace(cur, early, "nudge-early", ago="40 days")
        people["EARLY"] = early
    conn.commit()
    return people


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """The replayed stream and the seeded estate, once for the module. The
    tests that write run inside a transaction they roll back."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            people = _seed(conn)
        finally:
            conn.close()
        yield {
            "people": people,
            "worker": as_user(db, "svc_worker"),
            "ingress": as_user(db, "svc_ingress"),
        }
    finally:
        gen.close()


def _read(dsn: str, sql: str, params=None) -> list[tuple]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def test_the_door_lists_exactly_who_is_owed_a_nudge(world):
    p = world["people"]
    rows = _read(world["worker"], STALLED, (50,))
    assert {(str(u), stage) for u, stage in rows} == {
        (p["AT_INSTAGRAM"], 3),
        (p["AT_FOLDER"], 4),
        (p["AT_APPROVAL_CARD"], 5),
    }


def test_the_most_recent_stalls_come_first_and_the_limit_is_the_callers(world):
    p = world["people"]
    ordered = [str(u) for u, _ in _read(world["worker"], STALLED, (50,))]
    assert ordered == [p["AT_INSTAGRAM"], p["AT_FOLDER"], p["AT_APPROVAL_CARD"]]
    assert [str(u) for u, _ in _read(world["worker"], STALLED, (1,))] == ordered[:1]


def test_only_the_worker_login_may_call_the_door(world):
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        _read(world["ingress"], STALLED, (50,))


@pytest.mark.parametrize("login", ["worker", "ingress"])
def test_the_shared_stage_helper_is_no_door(world, login):
    """`fn_activation_stages` returns ids and timestamps across every tenant.
    Only the two doors, which run as its owner, may call it."""
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        _read(
            world[login],
            "SELECT * FROM fn_activation_stages(now() - interval '30 days')",
        )


async def _as_worker(dsn: str, check) -> None:
    """A sweep the way the worker runs one: the EMPTY tenant and the `system`
    actor. Rolled back, so every test sees the seeded estate."""
    engine = create_async_engine(async_url(dsn))
    try:
        async with engine.connect() as c:
            tx = await c.begin()
            await unit_of_work.apply_gucs(c, tenant_id="", actor_kind="system")
            await check(c)
            await tx.rollback()
    finally:
        await engine.dispose()


def test_the_sweep_queues_one_email_per_person_and_latches_each(world):
    p = world["people"]

    async def sweep(c):
        return await activation_nudge.sweep_stalled(
            c,
            since_days=30,
            stall_seconds=72 * 3600,
            limit=50,
            web_app_origin=ORIGIN + "/",
        )

    async def check(c):
        assert await sweep(c) == 3
        payloads = (
            await c.execute(
                text(
                    "SELECT workspace_id, serialization_key, lane, payload"
                    "  FROM jobs WHERE kind = 'send_email'"
                )
            )
        ).all()
        by_key = {key: (ws, lane, body) for ws, key, lane, body in payloads}
        expected = {
            p["AT_INSTAGRAM"]: (
                "instagram",
                "/dashboard/settings?tab=accounts",
                "instagram",
            ),
            p["AT_FOLDER"]: (
                "folder",
                "/dashboard/settings?tab=integrations",
                "folder",
            ),
            p["AT_APPROVAL_CARD"]: ("approval", "/dashboard/queue", "card"),
        }
        assert set(by_key) == {f"email:nudge:{u}" for u in expected}
        for user, (step, path, name) in expected.items():
            ws, lane, body = by_key[f"email:nudge:{user}"]
            assert ws is None and lane == "bulk"
            # jsonb arrives decoded or as text depending on the driver's codecs.
            body = json.loads(body) if isinstance(body, str) else body
            assert body == {
                "v": 1,
                "to": f"{name}@example.com",
                "template": "activation_nudge",
                "params": {"step": step, "link": ORIGIN + path},
            }
        latched = (
            (
                await c.execute(
                    text(
                        "SELECT id FROM users WHERE activation_nudge_at > now() - interval '1 minute'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {str(u) for u in latched} == set(expected)
        # The latch: a second sweep in the same transaction owes nobody anything.
        assert await sweep(c) == 0

    asyncio.run(_as_worker(world["worker"], check))
