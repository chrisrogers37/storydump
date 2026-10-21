"""Authorized upload persistence only, under both real tenant roles."""

import asyncio
import importlib.util
import json
from dataclasses import asdict
import uuid

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.services.target.transit import PastPostsUploadEvidence
from tests.scripts import test_instagram_catalog_gate as catalog
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts.test_instagram_observations_gate import uow, row

pytestmark = pytest.mark.integration
past_posts_engine = foundation.past_posts_engine
origin = catalog.past_posts
evidence = catalog.evidence


def service():
    assert importlib.util.find_spec("src.services.target.instagram_artifacts"), (
        "upload persistence missing"
    )
    from src.services.target import instagram_artifacts

    return instagram_artifacts


@pytest.fixture
def subject(sync_conn, origin):
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,provider_account_ref,approval_mode,schedule_slot_at) VALUES(%s,%s,%s,'synthetic-account','manual',now()+interval '1 year') RETURNING id",
            (origin["ws"], origin["iga"], origin["item"]),
        )
        intent = cur.fetchone()[0]
    sync_conn.commit()
    return {**origin, "intent": str(intent)}


def admission(s, **changes):
    values = dict(
        workspace_id=str(s["ws"]),
        source_id=str(s["past_posts"]),
        data_generation_id=str(s["generation"]),
        observation_revision=1,
        media_item_id=str(s["item"]),
        intent_id=s["intent"],
        capture_epoch=0,
        policy_revision=1,
        selected_asset_identity="111",
        validation_fingerprint="f" * 64,
        admitted_asset_ref=f"ws/{s['ws']}/" + uuid.uuid4().hex,
        admitted_resource_type="image",
        upload_attempt_id=str(uuid.uuid4()),
    )
    values.update(changes)
    return values


def created(a, **changes):
    values = dict(
        public_id=a["admitted_asset_ref"],
        asset_id="a" * 32,
        version=123,
        resource_type=a["admitted_resource_type"],
        delivery_type="authenticated",
        signature="b" * 40,
    )
    values.update(changes)
    return PastPostsUploadEvidence(**values)


async def snapshot(conn, s, ref):
    return await row(
        conn,
        "SELECT admitted_asset_ref,admitted_resource_type,outcome,created_asset_ref,creation_evidence,settled_at,cleanup_state FROM instagram_artifacts WHERE workspace_id=:ws AND artifact_ref=:ref",
        ws=str(s["ws"]),
        ref=ref,
    )


async def test_matching_creation_and_exact_replay_are_atomic(
    past_posts_engine, subject
):
    m = service()
    a = admission(subject)
    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **a)
        pending = await snapshot(conn, subject, ref)
        assert (
            pending["outcome"] == "unsettled"
            and pending["created_asset_ref"] is None
            and pending["creation_evidence"] is None
        )
        await m.record_created(
            conn, workspace_id=a["workspace_id"], artifact_ref=ref, evidence=created(a)
        )
        first = await snapshot(conn, subject, ref)
        await m.record_created(
            conn, workspace_id=a["workspace_id"], artifact_ref=ref, evidence=created(a)
        )
        assert await snapshot(conn, subject, ref) == first
        assert first["outcome"] == "settled" and first["settled_at"] is not None
        assert (
            first["created_asset_ref"] == a["admitted_asset_ref"]
            and first["cleanup_state"] == "pending"
        )
        assert set(first["creation_evidence"]) == {
            "public_id",
            "asset_id",
            "version",
            "resource_type",
            "delivery_type",
            "signature",
        }
        assert (
            await m.mark_uncertain(
                conn, workspace_id=a["workspace_id"], artifact_ref=ref
            )
            is False
        )


async def test_uncertainty_blocks_second_attempt_and_late_success_owns_cleanup(
    past_posts_engine, subject, sync_conn
):
    m = service()
    a = admission(subject)
    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **a)
        assert await m.mark_uncertain(
            conn, workspace_id=a["workspace_id"], artifact_ref=ref
        )
        with pytest.raises(m.ArtifactRefused, match="upload_already_pending"):
            await m.admit_upload(conn, **admission(subject))
        state = await snapshot(conn, subject, ref)
        assert (
            state["outcome"] == "uncertain"
            and state["settled_at"] is None
            and state["created_asset_ref"] is None
        )
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE instagram_past_posts_generations SET lifecycle='deletion_pending' WHERE id=%s",
            (subject["generation"],),
        )
        cur.execute(
            "UPDATE media_sources SET source_use_epoch=source_use_epoch+1 WHERE id=%s",
            (subject["past_posts"],),
        )
    sync_conn.commit()
    async with uow(past_posts_engine, subject) as conn:
        await m.record_created(
            conn, workspace_id=a["workspace_id"], artifact_ref=ref, evidence=created(a)
        )
        state = await snapshot(conn, subject, ref)
        assert state["outcome"] == "settled" and state["cleanup_state"] == "pending"
        assert (await row(conn, "SELECT count(*) n FROM instagram_intent_provenance"))[
            "n"
        ] == 0
        assert (
            await row(conn, "SELECT count(*) n FROM instagram_operation_provenance")
        )["n"] == 0
        assert (
            await row(
                conn,
                "SELECT publish_step,transit_asset_ref FROM post_intents WHERE id=:intent",
                intent=subject["intent"],
            )
        )["publish_step"] == "none"


@pytest.mark.parametrize(
    "change",
    [
        {"public_id": "ws/11111111-1111-1111-1111-111111111111/" + "a" * 32},
        {"resource_type": "video"},
        {"delivery_type": "upload"},
        {"version": True},
        {"version": 2**63},
        {"signature": "https://unsafe.example"},
    ],
)
async def test_conflicting_or_invalid_creation_never_settles(
    past_posts_engine, subject, change
):
    m = service()
    a = admission(subject)
    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **a)
        before = await snapshot(conn, subject, ref)
        with pytest.raises(m.ArtifactRefused):
            await m.record_created(
                conn,
                workspace_id=a["workspace_id"],
                artifact_ref=ref,
                evidence=created(a, **change),
            )
        assert await snapshot(conn, subject, ref) == before
        await m.record_created(
            conn, workspace_id=a["workspace_id"], artifact_ref=ref, evidence=created(a)
        )
        with pytest.raises(m.ArtifactRefused):
            await m.record_created(
                conn,
                workspace_id=a["workspace_id"],
                artifact_ref=ref,
                evidence=created(a, asset_id="c" * 32),
            )


async def test_foreign_tenant_completion_and_missing_tenant_are_denied(
    past_posts_engine, subject
):
    m = service()
    a = admission(subject)
    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **a)
    other = {"ws": str(uuid.uuid4())}
    async with uow(past_posts_engine, other) as conn:
        with pytest.raises(m.ArtifactRefused, match="artifact_missing"):
            await m.record_created(
                conn,
                workspace_id=a["workspace_id"],
                artifact_ref=ref,
                evidence=created(a),
            )
    async with past_posts_engine.begin() as conn:
        with pytest.raises(m.ArtifactRefused, match="artifact_missing"):
            await m.record_created(
                conn,
                workspace_id=a["workspace_id"],
                artifact_ref=ref,
                evidence=created(a),
            )


@pytest.mark.parametrize(
    "column,value",
    [
        ("admitted_asset_ref", "ws/11111111-1111-1111-1111-111111111111/" + "a" * 32),
        ("admitted_resource_type", "video"),
    ],
)
async def test_admitted_identity_is_database_immutable(
    past_posts_engine, subject, column, value
):
    m = service()
    a = admission(subject)
    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **a)
        with pytest.raises(DBAPIError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        f"UPDATE instagram_artifacts SET {column}=:value WHERE artifact_ref=:ref"
                    ),
                    dict(value=value, ref=ref),
                )
        assert (await snapshot(conn, subject, ref))["admitted_asset_ref"] == a[
            "admitted_asset_ref"
        ]


@pytest.mark.parametrize(
    "assignment",
    [
        "outcome='settled',settled_at=now()",
        "cleanup_state='no_asset'",
        "cleanup_state='destroyed'",
    ],
)
async def test_uncertain_cannot_be_settled_or_cleaned_by_absence_or_timestamp(
    past_posts_engine, subject, assignment
):
    m = service()
    a = admission(subject)
    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **a)
        with pytest.raises(DBAPIError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        f"UPDATE instagram_artifacts SET {assignment} WHERE artifact_ref=:ref"
                    ),
                    dict(ref=ref),
                )
        assert (await snapshot(conn, subject, ref))["outcome"] == "unsettled"


async def test_mark_destroyed_only_moves_a_settled_attempt_whose_cleanup_is_pending(
    past_posts_engine, subject
):
    m = service()
    a = admission(subject)
    ids = dict(workspace_id=a["workspace_id"])

    async def destroyed_at(conn, ref):
        found = await row(
            conn,
            "SELECT destroyed_at FROM instagram_artifacts WHERE artifact_ref=:ref",
            ref=ref,
        )
        return found["destroyed_at"]

    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **a)
        # Unsettled: the upload may still land, so there is nothing to destroy yet.
        assert await m.mark_destroyed(conn, artifact_ref=ref, **ids) is False
        before = await snapshot(conn, subject, ref)
        assert before["cleanup_state"] == "pending"
        assert await destroyed_at(conn, ref) is None
        await m.record_created(conn, artifact_ref=ref, evidence=created(a), **ids)
    # Another tenant cannot see it, so cannot move it.
    async with uow(past_posts_engine, {"ws": str(uuid.uuid4())}) as conn:
        assert await m.mark_destroyed(conn, artifact_ref=ref, **ids) is False
    async with uow(past_posts_engine, subject) as conn:
        assert (await snapshot(conn, subject, ref))["cleanup_state"] == "pending"
        # Settled and pending: moved, once.
        assert await m.mark_destroyed(conn, artifact_ref=ref, **ids) is True
        assert (await snapshot(conn, subject, ref))["cleanup_state"] == "destroyed"
        stamped = await destroyed_at(conn, ref)
        assert stamped is not None
        # A second caller finds it done and neither counts it nor restamps it.
        assert await m.mark_destroyed(conn, artifact_ref=ref, **ids) is False
        assert await destroyed_at(conn, ref) == stamped


async def test_concurrent_admissions_have_one_database_winner(
    past_posts_engine, subject
):
    m = service()

    async def insert():
        try:
            async with uow(past_posts_engine, subject) as conn:
                assert (
                    await conn.execute(text("SHOW transaction_isolation"))
                ).scalar_one() == "read committed"
                return await m.admit_upload(conn, **admission(subject))
        except m.ArtifactRefused as exc:
            return exc

    results = await asyncio.gather(insert(), insert())
    assert sum(isinstance(x, str) for x in results) == 1
    assert (
        sum(
            isinstance(x, m.ArtifactRefused) and x.reason == "upload_already_pending"
            for x in results
        )
        == 1
    )


def test_database_already_blocks_second_legacy_unresolved_attempt(sync_conn, evidence):
    with pytest.raises(psycopg2.errors.UniqueViolation):
        with sync_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO instagram_artifacts(workspace_id,source_id,data_generation_id,observation_revision,media_item_id,intent_id,kind,capture_epoch,policy_revision,selected_asset_identity,validation_fingerprint,upload_attempt_id,admitted_at,outcome) SELECT workspace_id,source_id,data_generation_id,observation_revision,media_item_id,intent_id,kind,capture_epoch,policy_revision,selected_asset_identity,validation_fingerprint,gen_random_uuid(),admitted_at,outcome FROM instagram_artifacts WHERE artifact_ref=%s",
                (evidence["artifact"],),
            )
    sync_conn.rollback()


@pytest.mark.parametrize(
    "field,value", [("asset_id", 123), ("signature", int("1" * 40))]
)
async def test_database_creation_evidence_requires_typed_identity_fields(
    past_posts_engine, subject, field, value
):
    m = service()
    a = admission(subject)
    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **a)
        invalid = asdict(created(a))
        invalid[field] = value
        with pytest.raises(DBAPIError):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "UPDATE instagram_artifacts SET outcome='settled',settled_at=now(),created_asset_ref=:created,creation_evidence=CAST(:e AS jsonb) WHERE artifact_ref=:ref"
                    ),
                    dict(
                        created=a["admitted_asset_ref"], e=json.dumps(invalid), ref=ref
                    ),
                )
        assert (await snapshot(conn, subject, ref))["outcome"] == "unsettled"


@pytest.fixture
def second_subject(sync_conn, subject):
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ig_accounts(workspace_id,provider_account_ref) VALUES(%s,%s) RETURNING id",
            (subject["ws"], "artifact-second-" + uuid.uuid4().hex),
        )
        account = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,provider_account_ref,approval_mode,schedule_slot_at) VALUES(%s,%s,%s,'synthetic-account','manual',now()+interval '2 years') RETURNING id",
            (subject["ws"], account, subject["item"]),
        )
        intent = cur.fetchone()[0]
    sync_conn.commit()
    return {**subject, "intent": str(intent)}


@pytest.mark.parametrize("settled", [False, True])
async def test_admitted_reference_has_one_lifetime_cleanup_owner(
    past_posts_engine, subject, second_subject, settled
):
    m = service()
    first = admission(subject)
    async with uow(past_posts_engine, subject) as conn:
        ref = await m.admit_upload(conn, **first)
        if settled:
            await m.record_created(
                conn,
                workspace_id=first["workspace_id"],
                artifact_ref=ref,
                evidence=created(first),
            )
    async with uow(past_posts_engine, second_subject) as conn:
        with pytest.raises(m.ArtifactRefused, match="admitted_reference_conflict"):
            await m.admit_upload(
                conn,
                **admission(
                    second_subject, admitted_asset_ref=first["admitted_asset_ref"]
                ),
            )
        assert (
            await row(
                conn,
                "SELECT count(*) n FROM instagram_artifacts WHERE admitted_asset_ref=:ref",
                ref=first["admitted_asset_ref"],
            )
        )["n"] == 1


async def test_concurrent_reference_reuse_has_one_cleanup_owner(
    past_posts_engine, subject, second_subject
):
    m = service()
    shared = admission(subject)["admitted_asset_ref"]

    async def insert(s):
        try:
            async with uow(past_posts_engine, s) as conn:
                return await m.admit_upload(
                    conn, **admission(s, admitted_asset_ref=shared)
                )
        except m.ArtifactRefused as exc:
            return exc

    results = await asyncio.gather(insert(subject), insert(second_subject))
    assert sum(isinstance(x, str) for x in results) == 1
    assert (
        sum(
            isinstance(x, m.ArtifactRefused)
            and x.reason == "admitted_reference_conflict"
            for x in results
        )
        == 1
    )
