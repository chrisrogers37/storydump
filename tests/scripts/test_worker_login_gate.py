"""The worker's four tenant-less sweeps answer under `svc_worker` (#751, part 2).

Measured on 2026-09-21: the worker issues no direct DELETE and touches none of
the tables its role cannot see; what blocks its switch is four paths that open
a session with an EMPTY tenant and the `system` actor and then run SQL on
policy-covered tables — the outbox sender sweep, the prompt sweep, the
stranded-source alert and the reconciler's container poll. Under the owner
login they work because the owner bypasses row-level security; under
`svc_worker` the reads see nothing, and the first switch of the API showed
what that looks like in production. This gate runs each path as `svc_worker`
against a seeded estate and expects what the owner gets — one sender job, one
prompt, one alert, the container id — beside a plain read that expects nothing
and the owner actor as the control. Every sweep runs in a transaction that is
rolled back, so each login sees the same estate.
"""

from __future__ import annotations

import asyncio

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src import worker as worker_mod
from src.services.target import media_sync, prompts, unit_of_work, work_loop
from tests.scripts.conftest import (
    _scratch,
    as_user,
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


def _async_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """One workspace with a bound Telegram group, a pending outbox row on it,
    a due `scheduled` intent, a media source stranded in `error`, and an
    ambiguous intent carrying a container id — seeded as the migration actor."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            chain = seed_workspace_chain(conn, "wl")
            ws, iga, src, media = (
                chain["ws"],
                chain["iga"],
                chain["src"],
                chain["media"],
            )
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
                # ambiguous one gets its own media item
                cur.execute(
                    "INSERT INTO media_items"
                    " (workspace_id, source_id, content_hash, file_name, media_kind,"
                    "  provider_file_ref)"
                    " VALUES (%s, %s, 'hash-wl-2', 'g.jpg', 'image', 'ref-wl-2')"
                    " RETURNING id",
                    (ws, src),
                )
                media2 = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO post_intents (workspace_id, ig_account_id,"
                    " media_item_id, provider_account_ref, approval_mode,"
                    " schedule_slot_at, state, published_via, cap_consumed_on,"
                    " publish_step, ig_container_id)"
                    " VALUES (%s, %s, %s, 'acct-wl', 'manual', now(),"
                    " 'publishing_ambiguous', 'api', current_date,"
                    " 'publish_called', 'ctr-wl-1') RETURNING id",
                    (ws, iga, media2),
                )
                ambiguous = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        yield {
            "ws": str(ws),
            "ambiguous": str(ambiguous),
            "worker": as_user(db, "svc_worker"),
            "owner": as_user(db, owner_actor),
        }
    finally:
        gen.close()


async def _system_session(dsn: str, fn):
    """A sweep the way the worker runs it: a session with the EMPTY tenant and
    the `system` actor — then rolled back, so the next login sees the same
    estate."""
    engine = create_async_engine(_async_url(dsn))
    try:
        async with engine.connect() as c:
            tx = await c.begin()
            await unit_of_work.apply_gucs(c, tenant_id="", actor_kind="system")
            result = await fn(c)
            await tx.rollback()
            return result
    finally:
        await engine.dispose()


async def _plain_counts(dsn: str) -> dict:
    engine = create_async_engine(_async_url(dsn))
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
    """The reconciler's provider seam: answers PUBLISHED for any container."""

    async def container_status(
        self, container_id, *, provider_account_ref, workspace_id
    ):
        return "PUBLISHED"


async def _poll(dsn: str, intent_id: str, workspace_id: str):
    engine = create_async_engine(_async_url(dsn))
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
    assert seen["channel_outbox"] == 1 and seen["post_intents"] == 2


def test_the_sender_sweep_mints_the_delivery_job_as_svc_worker(world):
    assert _run(_system_session(world["owner"], work_loop.ensure_sender_jobs)) == 1
    assert _run(_system_session(world["worker"], work_loop.ensure_sender_jobs)) == 1


def test_the_prompt_sweep_prompts_the_due_story_as_svc_worker(world):
    owner = _run(_system_session(world["owner"], prompts.sweep_due_prompts))
    assert owner["prompted"] == 1
    got = _run(_system_session(world["worker"], prompts.sweep_due_prompts))
    assert got["prompted"] == 1


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
