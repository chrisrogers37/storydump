"""The archive composition's database halves (#1644).

The access provider is the one piece the composition adds that reads the
database: it re-checks the capture it serves, reads that capture's credential
under the source prefix and decrypts it outside the transaction. It runs as the
API's login and as the worker's (the preview and the sync), so each case here
runs as both. The reaper takes the activation as a parameter, and its
four-argument door is judged here against an expired archive sync. The
default-off half drives the composed, INACTIVE worker's publish executor over a
real archive intent: with no coordinator the intent is refused by name, and
nothing reaches a provider.

Providers are fakes; every token is synthetic.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target import archive_composition as composition
from src.services.target import jobs, oauth_states, scheduler
from src.services.target.media_adapters import MediaCredentialDead, MediaSourceFenceDrop
from src.services.target.unit_of_work import apply_gucs
from src.services.target.work_loop import WorkerConfig
from src.worker import compose
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_preparation_gate as preparation
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_source_read_admission_gate as admission
from tests.scripts.conftest import as_user, async_url, set_test_passwords

pytestmark = [pytest.mark.integration]

archive_engine = foundation.archive_engine
origin = foundation.origin

TOKEN = "synthetic-access-token"


@pytest.fixture
async def worker(world_db, admin_conn):
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(world_db, "svc_worker")))
    try:
        yield engine
    finally:
        await engine.dispose()


def credential(sync_conn, chain, **set):
    """The owner's edit of the origin's credential row."""
    assignments = ", ".join(f"{column} = %s" for column in set)
    with sync_conn.cursor() as cur:
        cur.execute(
            f"UPDATE oauth_credentials SET {assignments} WHERE id = %s",
            (*set.values(), chain["credential"]),
        )
    sync_conn.commit()


async def current(engine, chain, sync_conn):
    """A ready archive, its capture, and the credential holding TOKEN."""
    src = await admission.ready(engine, chain)
    capture = await admission.capture(engine, chain, src)
    assert capture.credential_id == str(chain["credential"]), "the capture binds it"
    credential(sync_conn, chain, encrypted_payload=oauth_states.ring().encrypt(TOKEN))
    return capture


def provider(engine, capture, capability=foundation.capability()):
    return composition.access_provider(engine, capture=capture, capability=capability)


# --- the access provider -----------------------------------------------------------


async def test_the_access_provider_hands_back_the_captured_credentials_token(
    archive_engine, origin, sync_conn
):
    capture = await current(archive_engine, origin, sync_conn)

    access = await provider(archive_engine, capture)(
        capture.source_id, workspace_id=capture.workspace_id
    )

    assert access.account_ref == capture.account_ref
    assert access.token == TOKEN
    assert access.captured_authorization == capture
    assert TOKEN not in repr(access), "the token is never rendered"


async def record_mapping(engine, chain, capture, *, subject, account=None, when=None):
    """One verified (app, subject) mapping for the capture's credential, as the
    connect callback or the reconciliation records it."""
    from datetime import datetime, timezone

    from src.services.target import identity_mappings

    now = when or datetime.now(timezone.utc)
    async with obs.uow(engine, chain) as conn:
        await identity_mappings.record(
            conn,
            workspace_id=capture.workspace_id,
            ig_account_id=str(chain["iga"]),
            credential_id=capture.credential_id,
            authorization_id=str(uuid.uuid4()),
            app_identity="instagram:1234567890",
            subject_ref=subject,
            account_ref=account or capture.account_ref,
            method="oauth_exchange",
            authorized_at=now,
            verified_at=now,
        )


async def test_the_access_provider_reads_the_verified_app_scoped_id_with_the_credential(
    archive_engine, origin, sync_conn
):
    async with obs.uow(archive_engine, origin) as conn:
        await conn.execute(
            text("UPDATE ig_accounts SET provider_account_ref=:ref WHERE id=:id"),
            dict(ref="17841400000000001", id=str(origin["iga"])),
        )
    capture = await current(archive_engine, origin, sync_conn)
    call = (capture.source_id, capture.workspace_id)

    async def read():
        access = await provider(archive_engine, capture)(call[0], workspace_id=call[1])
        assert access.app_scoped_id is None or access.app_scoped_id not in repr(access)
        return access.app_scoped_id

    # Nothing recorded is None, never a guess: ownership then proves itself at `/me`.
    assert await read() is None
    await record_mapping(archive_engine, origin, capture, subject="17841499999999991")
    assert await read() == "17841499999999991"
    # A newer authorization's mapping supersedes it; only the current one is read.
    await record_mapping(archive_engine, origin, capture, subject="17841499999999992")
    assert await read() == "17841499999999992"


async def test_the_access_provider_ignores_a_mapping_that_names_another_account(
    archive_engine, origin, sync_conn
):
    async with obs.uow(archive_engine, origin) as conn:
        await conn.execute(
            text("UPDATE ig_accounts SET provider_account_ref=:ref WHERE id=:id"),
            dict(ref="17841400000000001", id=str(origin["iga"])),
        )
    capture = await current(archive_engine, origin, sync_conn)
    await record_mapping(
        archive_engine,
        origin,
        capture,
        subject="17841499999999991",
        account="17841400000000777",
    )

    access = await provider(archive_engine, capture)(
        capture.source_id, workspace_id=capture.workspace_id
    )

    assert access.app_scoped_id is None, "a mapping for another account proves nothing"


async def test_the_access_provider_reads_nothing_for_a_capture_the_database_no_longer_admits(
    archive_engine, origin, sync_conn
):
    capture = await current(archive_engine, origin, sync_conn)
    call = (capture.source_id, capture.workspace_id)

    # A withdrawn capability, and a credential revoked since the capture: each
    # is a fence drop with the database's own reason, never a token.
    with pytest.raises(MediaSourceFenceDrop) as withdrawn:
        await provider(archive_engine, capture, None)(call[0], workspace_id=call[1])
    assert withdrawn.value.reason == "archive_not_activated"

    credential(sync_conn, origin, state="revoked")
    with pytest.raises(MediaSourceFenceDrop) as revoked:
        await provider(archive_engine, capture)(call[0], workspace_id=call[1])
    assert revoked.value.reason == "credential_unavailable"


async def test_a_credential_no_ring_entry_reads_is_dead_not_echoed(
    archive_engine, origin, sync_conn
):
    capture = await current(archive_engine, origin, sync_conn)
    credential(sync_conn, origin, encrypted_payload="not-a-ring-ciphertext")

    with pytest.raises(MediaCredentialDead) as dead:
        await provider(archive_engine, capture)(
            capture.source_id, workspace_id=capture.workspace_id
        )

    assert "not-a-ring-ciphertext" not in str(dead.value)


# --- the reaper's door ---------------------------------------------------------------


@pytest.mark.parametrize("activated", [True, False])
async def test_the_reaper_judges_an_expired_archive_sync_by_the_activation_it_is_given(
    worker, origin, sync_conn, activated
):
    src = await admission.ready(worker, origin)
    capture = await admission.capture(worker, origin, src)
    async with obs.uow(worker, origin) as conn:
        job = await jobs.enqueue(
            conn,
            kind="sync_media_source",
            workspace_id=str(origin["ws"]),
            serialization_key="src:" + src["source_id"],
            payload=dict(
                v=1, source_id=src["source_id"], source_read_capture=capture.payload()
            ),
        )
        await conn.execute(
            text(
                "UPDATE jobs SET state='leased', lease_token=gen_random_uuid(),"
                " locked_by='dead', locked_until=now() - interval '1 second'"
                " WHERE id=:id"
            ),
            dict(id=job),
        )

    await scheduler.execute_reap_expired(
        worker,
        limit=20,
        approval_ttl_seconds=3600,
        approved_ttl_seconds=3600,
        capability=foundation.capability() if activated else None,
    )

    with sync_conn.cursor() as cur:
        cur.execute("SELECT state, payload FROM jobs WHERE id = %s", (job,))
        state, payload = cur.fetchone()
    sync_conn.commit()
    if activated:
        assert state == "ready" and "source_read_refusal" not in payload
    else:
        assert (state, payload["source_read_refusal"]) == (
            "failed",
            "archive_not_activated",
        )


# --- default off: no coordinator, no archive publish --------------------------------


async def approved_job(engine, chain, intent):
    """The intent approved by its owner, and the leased publish job that
    carries it: what `approve` and the claim leave for the executor."""
    async with obs.uow(engine, chain) as conn:
        for state in ("prompt_pending", "awaiting_approval"):
            await conn.execute(
                text("UPDATE post_intents SET state=:state WHERE id=:id"),
                dict(state=state, id=intent),
            )
        await apply_gucs(
            conn,
            tenant_id=str(chain["ws"]),
            actor_kind="user",
            actor_user_id=str(chain["user"]),
        )
        await conn.execute(
            text(
                "UPDATE post_intents SET state='approved',approved_by_user_id=:user"
                " WHERE id=:id"
            ),
            dict(user=str(chain["user"]), id=intent),
        )
        await apply_gucs(conn, tenant_id=str(chain["ws"]), actor_kind="system")
        return dict(
            (
                await conn.execute(
                    text(
                        "INSERT INTO jobs(workspace_id,kind,lane,serialization_key,payload,"
                        "state,lease_token,locked_by,locked_until,attempts,max_attempts)"
                        " VALUES(:ws,'publish_pipeline','bulk',:key,CAST(:payload AS jsonb),"
                        "'leased',:token,'archive-gate',clock_timestamp()+interval '5 minutes',1,5)"
                        " RETURNING *"
                    ),
                    dict(
                        ws=str(chain["ws"]),
                        key="ig:" + str(chain["iga"]),
                        payload='{"v":1,"intent_id":"' + intent + '"}',
                        token=str(uuid.uuid4()),
                    ),
                )
            )
            .mappings()
            .one()
        )


async def test_an_inactive_worker_refuses_an_archive_intent_by_name(worker, origin):
    _, _, intent = await preparation.selected(worker, origin)
    job = await approved_job(worker, origin, intent)
    # Nothing arms it: an empty env, so no coordinator reaches the pipeline. The
    # other seams are the ones the kind needs to be live, none of them called.
    inactive = compose(
        engine=worker,
        config=WorkerConfig(),
        env={},
        adapters={"gdrive": object()},
        meta=object(),
        transit=object(),
    )
    assert inactive.deps.archive is None

    done = await inactive.registry["publish_pipeline"](None, job)

    assert done is jobs.SELF_FINALIZED
    async with obs.uow(worker, origin) as conn:
        reason, step = (
            await conn.execute(
                text(
                    "SELECT last_error->'error'->>'reason', publish_step"
                    " FROM post_intents WHERE id = :id"
                ),
                dict(id=intent),
            )
        ).one()
        job_state = (
            await conn.execute(
                text("SELECT state FROM jobs WHERE id = :id"), dict(id=job["id"])
            )
        ).scalar_one()
    assert reason == "archive_pipeline_not_configured"
    assert step == "none", "nothing was staged, uploaded or published"
    assert job_state == "review_required", "parked for the owner's review, not retried"
