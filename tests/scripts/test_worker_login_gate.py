"""The worker's tenant-less sweeps answer under `svc_worker` (#751, part 2).

Measured on 2026-09-21, and again under review: the worker issues no direct
DELETE and touches none of the tables its role cannot see; what blocks its
switch is six paths that open a session with an EMPTY tenant and the `system`
actor and then run SQL on policy-covered tables — the outbox sender sweep,
the prompt sweep, the settled-card sweep, the stranded-source alert, the
reconciler's container poll and the reconciler's ladder count — and one
scope leak: `plan_slot` runs the prompt sweep inside a TENANT job's
transaction, and a sweep that left the session under the last workspace it
prompted would fence that job's own finalization. Under the owner login they
all work because the owner bypasses row-level security; under `svc_worker`
the reads see nothing, and the first switch of the API showed what that
looks like in production. This gate runs each path as `svc_worker` against a
seeded estate of three workspaces and expects what the owner gets — asserting
the EFFECT under the workspace's own tenant, never the count a sweep reports
— beside a plain read that expects nothing and the owner actor as the
control. Every sweep runs in a transaction that is rolled back, so each login
sees the same estate.
"""

from __future__ import annotations

import asyncio

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src import worker as worker_mod
from src.services.target import (
    jobs,
    media_sync,
    prompts,
    unit_of_work,
    work_loop,
)
from tests.scripts.conftest import (
    _scratch,
    as_user,
    async_url,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = pytest.mark.integration

_TABLES = (
    "post_intents",
    "channel_bindings",
    "channel_outbox",
    "media_sources",
    "jobs",
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """Three workspaces, seeded as the migration actor.

    `ws` (A) has a bound Telegram group, a pending outbox row on it, a due
    `scheduled` intent, a media source stranded in `error`, an ambiguous
    intent carrying a container id and one recorded check, an ended (skipped)
    intent whose approval card is still live on the group, and a leased
    `plan_slot` job. `other` (B) has an OLDER due `scheduled` intent, so a
    sweep bounded to one story prompts B's, not A's. `third` (C) has an intent
    in `prompt_pending` and nothing due, so the sweep's advance phase must
    claim a workspace its prompting phase never touched."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            a = seed_workspace_chain(conn, "wl")
            b = seed_workspace_chain(conn, "wl2")
            c = seed_workspace_chain(conn, "wl3")
            ws, iga, src = a["ws"], a["iga"], a["src"]
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                    " VALUES (%s, 'telegram_group', '-1000000000002') RETURNING id",
                    (ws,),
                )
                binding = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO channel_outbox (workspace_id, binding_id, kind,"
                    " payload, state)"
                    " VALUES (%s, %s, 'notification', '{\"v\": 1}'::jsonb, 'pending')",
                    (ws, binding),
                )
                cur.execute(
                    "UPDATE media_sources SET state = 'error' WHERE id = %s", (src,)
                )
                # one LIVE intent per (workspace, media item, account): the
                # ambiguous one and the ended one get their own media items
                cur.execute(
                    "INSERT INTO media_items"
                    " (workspace_id, source_id, content_hash, file_name, media_kind,"
                    "  provider_file_ref)"
                    " VALUES (%s, %s, 'hash-wl-2', 'g.jpg', 'image', 'ref-wl-2'),"
                    "        (%s, %s, 'hash-wl-3', 'h.jpg', 'image', 'ref-wl-3')"
                    " RETURNING id",
                    (ws, src, ws, src),
                )
                media2, media3 = (r[0] for r in cur.fetchall())
                # polled once, its rung elapsed: the ladder is at step 1
                cur.execute(
                    "INSERT INTO post_intents (workspace_id, ig_account_id,"
                    " media_item_id, provider_account_ref, approval_mode,"
                    " schedule_slot_at, state, published_via, cap_consumed_on,"
                    " publish_step, ig_container_id, last_error)"
                    " VALUES (%s, %s, %s, 'acct-wl', 'manual', now(),"
                    " 'publishing_ambiguous', 'api', current_date,"
                    " 'publish_called', 'ctr-wl-1',"
                    " jsonb_build_object('v', 1, 'evidence', jsonb_build_object("
                    "   'checks', 1, 'last_checked_at', now() - interval '1 hour',"
                    "   'trail', '[]'::jsonb))) RETURNING id",
                    (ws, iga, media2),
                )
                ambiguous = cur.fetchone()[0]
                # an ended story whose card is still live on the group
                cur.execute(
                    "INSERT INTO post_intents (workspace_id, ig_account_id,"
                    " media_item_id, provider_account_ref, approval_mode,"
                    " schedule_slot_at, state)"
                    " VALUES (%s, %s, %s, 'acct-wl', 'manual',"
                    " now() - interval '1 day', 'skipped') RETURNING id",
                    (ws, iga, media3),
                )
                ended = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO channel_outbox (workspace_id, binding_id, intent_id,"
                    " kind, payload, state, external_message_ref)"
                    " VALUES (%s, %s, %s, 'approval_prompt',"
                    " '{\"v\": 1, \"text\": \"card\"}'::jsonb, 'sent', 'msg-wl-3')"
                    " RETURNING id",
                    (ws, binding, ended),
                )
                card = cur.fetchone()[0]
                # the tenant job that runs the prompt sweep, mid-lease
                cur.execute(
                    "INSERT INTO jobs (kind, workspace_id, lane, serialization_key,"
                    " run_at, max_attempts, deadline_at, payload, state, lease_token,"
                    " locked_until)"
                    " VALUES ('plan_slot', %s, 'bulk', 'plan:wl', now(), 3,"
                    " now() + interval '1 hour', '{\"v\": 1}'::jsonb, 'leased',"
                    " gen_random_uuid(), now() + interval '10 minutes')"
                    " RETURNING id, lease_token",
                    (ws,),
                )
                job, lease = cur.fetchone()
                cur.execute(
                    "UPDATE post_intents SET schedule_slot_at = now() - interval '1 hour'"
                    " WHERE id = %s",
                    (b["intent"],),
                )
                cur.execute(
                    "UPDATE post_intents SET state = 'prompt_pending' WHERE id = %s",
                    (c["intent"],),
                )
            conn.commit()
        finally:
            conn.close()
        yield {
            "ws": str(ws),
            "intent": str(a["intent"]),
            "binding": str(binding),
            "ambiguous": str(ambiguous),
            "ended": str(ended),
            "card": str(card),
            "job": str(job),
            "lease": str(lease),
            "other": str(b["ws"]),
            "other_intent": str(b["intent"]),
            "third": str(c["ws"]),
            "third_intent": str(c["intent"]),
            "worker": as_user(db, "svc_worker"),
            "owner": as_user(db, owner_actor),
        }
    finally:
        gen.close()


async def _system_session(dsn: str, fn, check=None):
    """A sweep the way the worker runs it: a session with the EMPTY tenant and
    the `system` actor — then rolled back, so the next login sees the same
    estate. *check* runs before the rollback, on the same connection, and
    claims whatever tenant it reads under: it asserts the EFFECT, not the
    count a sweep reports."""
    engine = create_async_engine(async_url(dsn))
    try:
        async with engine.connect() as c:
            tx = await c.begin()
            await unit_of_work.apply_gucs(c, tenant_id="", actor_kind="system")
            result = await fn(c)
            if check is not None:
                await check(c)
            await tx.rollback()
            return result
    finally:
        await engine.dispose()


async def _claim(c, workspace_id: str) -> None:
    await unit_of_work.apply_gucs(c, tenant_id=workspace_id, actor_kind="system")


async def _state(c, intent_id: str) -> str:
    return (
        await c.execute(
            text("SELECT state FROM post_intents WHERE id = :id"), {"id": intent_id}
        )
    ).scalar()


async def _plain_counts(dsn: str) -> dict:
    engine = create_async_engine(async_url(dsn))
    try:
        async with engine.connect() as c:
            await unit_of_work.apply_gucs(c, tenant_id="", actor_kind="system")
            return {
                t: (await c.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
                for t in _TABLES
            }
    finally:
        await engine.dispose()


class _Meta:
    """The reconciler's provider seam: answers *status* for any container."""

    def __init__(self, status: str = "PUBLISHED"):
        self.status = status

    async def container_status(
        self, container_id, *, provider_account_ref, workspace_id
    ):
        return self.status


async def _poll(dsn: str, intent_id: str, workspace_id: str):
    engine = create_async_engine(async_url(dsn))
    try:
        poll = worker_mod._poll_from(engine, _Meta())
        return await poll(intent_id=intent_id, workspace_id=workspace_id)
    finally:
        await engine.dispose()


def test_the_plain_reads_are_hidden_from_the_worker_login(world):
    """The vacuity guard: the tenant-less session sees nothing under the
    policies, so a sweep that answers is answering through a door."""
    assert _run(_plain_counts(world["worker"])) == {t: 0 for t in _TABLES}
    seen = _run(_plain_counts(world["owner"]))
    assert seen["channel_outbox"] == 2 and seen["post_intents"] == 5
    assert seen["jobs"] == 1


def test_the_sender_sweep_mints_the_delivery_job_as_svc_worker(world):
    assert _run(_system_session(world["owner"], work_loop.ensure_sender_jobs)) == 1
    assert _run(_system_session(world["worker"], work_loop.ensure_sender_jobs)) == 1


def test_the_prompt_sweep_prompts_the_due_stories_and_advances_the_pending_one_as_svc_worker(
    world,
):
    """The count a sweep reports is not the effect: `intent_ledger.transition`
    does not check its rowcount, so a sweep whose writes the policy silently
    filtered would still report `prompted == 2`. The check reads the effect
    under each workspace's own tenant: A's due story moved on and its card is
    in the outbox; C's `prompt_pending` story — a workspace the prompting
    phase never claimed — advanced too."""

    async def prompted(c):
        await _claim(c, world["ws"])
        assert await _state(c, world["intent"]) == "awaiting_approval"
        cards = (
            await c.execute(
                text(
                    "SELECT count(*) FROM channel_outbox"
                    " WHERE binding_id = :b AND kind = 'approval_prompt'"
                    "   AND intent_id = :i"
                ),
                {"b": world["binding"], "i": world["intent"]},
            )
        ).scalar()
        assert cards == 1, cards
        await _claim(c, world["third"])
        assert await _state(c, world["third_intent"]) == "awaiting_approval"

    owner = _run(_system_session(world["owner"], prompts.sweep_due_prompts, prompted))
    assert (owner["prompted"], owner["advanced"]) == (2, 3)
    got = _run(_system_session(world["worker"], prompts.sweep_due_prompts, prompted))
    assert (got["prompted"], got["advanced"]) == (2, 3)


def test_the_prompt_sweep_hands_the_callers_scope_back_so_plan_slot_can_finalize(
    world,
):
    """`plan_slot` runs `sweep_due_prompts(limit=1)` inside its own TENANT
    transaction and then finalizes its job. The oldest due story in the
    estate is B's, so the sweep prompts it under B's claim; left there, the
    finalization — `UPDATE jobs … WHERE id = :id AND lease_token = :token` —
    matches no row under the policies and raises `JobFenced`: every planned
    slot would fail after the switch. The sweep hands the caller's scope
    back, and the finalization lands."""

    async def plan_then_finalize(dsn):
        engine = create_async_engine(async_url(dsn))
        try:
            async with engine.connect() as c:
                tx = await c.begin()
                await _claim(c, world["ws"])
                counts = await prompts.sweep_due_prompts(c, limit=1)
                tenant = (
                    await c.execute(
                        text("SELECT current_setting('app.tenant_id', true)")
                    )
                ).scalar()
                await jobs.finalize_job(c, world["job"], world["lease"], "succeeded")
                await tx.rollback()
                return counts["prompted"], tenant
        finally:
            await engine.dispose()

    assert _run(plan_then_finalize(world["owner"])) == (1, world["ws"])
    assert _run(plan_then_finalize(world["worker"])) == (1, world["ws"])


def test_the_settled_card_sweep_retires_the_ended_storys_card_as_svc_worker(world):
    """The ended story's card loses its buttons: superseded in the outbox, and
    the edit that strips them queued behind it — both written under A's
    tenant, which the check claims to see them."""

    async def sweep(c):
        return await prompts.sweep_settled_cards(c, limit=50)

    async def retired(c):
        await _claim(c, world["ws"])
        state = (
            await c.execute(
                text("SELECT state FROM channel_outbox WHERE id = :id"),
                {"id": world["card"]},
            )
        ).scalar()
        assert state == "superseded", state
        edits = (
            await c.execute(
                text(
                    "SELECT count(*) FROM channel_outbox"
                    " WHERE binding_id = :b AND intent_id = :i"
                    "   AND kind = 'prompt_supersede'"
                ),
                {"b": world["binding"], "i": world["ended"]},
            )
        ).scalar()
        assert edits == 1, edits

    assert _run(_system_session(world["owner"], sweep, retired)) == 1
    assert _run(_system_session(world["worker"], sweep, retired)) == 1


def test_the_stranded_alert_finds_the_source_as_svc_worker(world):
    async def alert(c):
        return await media_sync.alert_stranded_sources(
            c, stale_after_seconds=0, limit=10
        )

    assert _run(_system_session(world["owner"], alert)) == 1
    assert _run(_system_session(world["worker"], alert)) == 1


def test_the_reconciler_poll_reaches_the_container_as_svc_worker(world):
    assert _run(_poll(world["owner"], world["ambiguous"], world["ws"])) == "PUBLISHED"
    assert _run(_poll(world["worker"], world["ambiguous"], world["ws"])) == "PUBLISHED"


def test_the_reconciler_climbs_the_ladder_from_where_it_was_as_svc_worker(world):
    """The ambiguous story has been polled once (`checks = 1` in its evidence,
    its rung elapsed). One sweep with an inconclusive poll must record the
    SECOND check — the count read under the row's tenant, after the claim.
    Read before it, `post_intents` answers nothing to a tenant-less session,
    every step counts as the first, and the ladder never exhausts."""

    async def sweep_once(dsn):
        engine = create_async_engine(async_url(dsn))
        try:
            registry = work_loop.build_registry(
                work_loop.WorkerDeps(
                    poll=worker_mod._poll_from(engine, _Meta("IN_PROGRESS"))
                )
            )
            async with engine.connect() as c:
                tx = await c.begin()
                await unit_of_work.apply_gucs(c, tenant_id="", actor_kind="system")
                await registry["reconcile_ambiguous"](
                    c,
                    {
                        "id": "j-rec",
                        "kind": "reconcile_ambiguous",
                        "workspace_id": None,
                        "payload": {"v": 1},
                    },
                )
                await _claim(c, world["ws"])
                checks = (
                    await c.execute(
                        text(
                            "SELECT (last_error->'evidence'->>'checks')::int"
                            "  FROM post_intents WHERE id = :id"
                        ),
                        {"id": world["ambiguous"]},
                    )
                ).scalar()
                await tx.rollback()
                return checks
        finally:
            await engine.dispose()

    assert _run(sweep_once(world["owner"])) == 2
    assert _run(sweep_once(world["worker"])) == 2
