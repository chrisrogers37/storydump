"""Archive-only writers, exercised under the deployed database identities."""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.services.target.unit_of_work import unit_of_work
from tests.scripts.conftest import (
    as_user,
    async_url,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture(params=("svc_ingress", "svc_worker"))
async def archive_engine(request, world_db, admin_conn):
    set_test_passwords(admin_conn)
    engine = create_async_engine(async_url(as_user(world_db, request.param)))
    try:
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT current_user"))
            ).scalar_one() == request.param
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def origin(sync_conn):
    chain = seed_workspace_chain(sync_conn, "archive-service")
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider, encrypted_payload)"
            " VALUES (%s, %s, 'ig_login', 'test-ciphertext') RETURNING id",
            (chain["ws"], chain["iga"]),
        )
        chain["credential"] = cur.fetchone()[0]
    sync_conn.commit()
    return chain


def authorization():
    from src.services.target.instagram_sources import ArchiveAuthorization

    return ArchiveAuthorization(
        str(uuid.uuid4()),
        "verified-app",
        "verified-subject",
        datetime.now(timezone.utc),
    )


def capability():
    from src.services.target.instagram_sources import ArchiveCapability

    return ArchiveCapability(enabled=True)


async def ensure(engine, origin, event=None, **kw):
    from src.services.target.instagram_sources import ensure_archive_source

    async with unit_of_work(
        engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        return await ensure_archive_source(
            conn,
            workspace_id=str(origin["ws"]),
            origin_ig_account_id=str(origin["iga"]),
            authorization=event,
            capability=capability(),
            **kw,
        )


#: What a finished read leaves on a source: a due time far in the future and an
#: alert already sent. No arm can write either, so a source that still holds
#: them was not touched, and one that does not was armed.
STAMP = datetime(2100, 1, 1, tzinfo=timezone.utc)
ALERTED = datetime(2020, 6, 1, tzinfo=timezone.utc)
UNARMED = {"next_sync_at": None, "alerted_at": None, "due": None}
UNTOUCHED = {"next_sync_at": STAMP, "alerted_at": ALERTED, "due": False}


def assert_armed(fields):
    """An arm's result: a due time that has already passed (not the stamp a
    finished read left) and no alert."""
    assert fields["alerted_at"] is None, fields
    assert fields["next_sync_at"] not in (None, STAMP), fields
    assert fields["due"] is True, fields


async def stamp(engine, origin, source_id):
    async with unit_of_work(
        engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        await conn.execute(
            text(
                "UPDATE media_sources SET next_sync_at = :at, alerted_at = :alerted WHERE id = :id"
            ),
            {"at": STAMP, "alerted": ALERTED, "id": source_id},
        )


async def read_clock(conn, source_id):
    """The source's read clock: when its next sync is due, the alert marker, and
    whether the due time has already passed (None while nothing is due)."""
    return dict(
        (
            await conn.execute(
                text(
                    "SELECT next_sync_at, alerted_at, next_sync_at <= clock_timestamp() AS due"
                    " FROM media_sources WHERE id = :id"
                ),
                {"id": source_id},
            )
        )
        .mappings()
        .one()
    )


async def clock_fields(engine, origin, source_id):
    async with unit_of_work(
        engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        return await read_clock(conn, source_id)


async def switch(engine, origin, source_id, *, on):
    """The source turned On or Off in a transaction of its own: its share of the
    mix changes and the transition is applied. Returns the epoch it ends at."""
    from src.services.target import source_state

    ws = str(origin["ws"])
    async with unit_of_work(engine, ws, actor_kind="system").begin() as conn:
        await source_state.lock_workspace_sources(
            conn,
            workspace_id=ws,
            ig_account_ids=[str(origin["iga"])],
            source_ids=[source_id],
        )
        before = await source_state.archive_state(
            conn, workspace_id=ws, source_id=source_id
        )
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio = :ratio WHERE source_id = :id AND effective_to IS NULL"
            ),
            {"ratio": 1 if on else 0, "id": source_id},
        )
        return await source_state.apply_archive_transition(
            conn,
            workspace_id=ws,
            source_id=source_id,
            before=before,
            reason="enabled" if on else "off",
        )


async def test_default_feature_gate_refuses_before_any_creation(archive_engine, origin):
    from src.services.target.instagram_sources import (
        ArchiveRefused,
        ensure_archive_source,
    )

    async with unit_of_work(
        archive_engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        with pytest.raises(ArchiveRefused) as refused:
            await ensure_archive_source(
                conn,
                workspace_id=str(origin["ws"]),
                origin_ig_account_id=str(origin["iga"]),
            )
        assert refused.value.reason == "archive_not_activated"
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM media_sources WHERE provider = 'instagram_archive'"
                )
            )
        ).scalar_one() == 0


async def test_duplicate_callbacks_keep_one_source_generation_and_policy(
    archive_engine, origin
):
    event = authorization()
    results = await asyncio.gather(
        ensure(archive_engine, origin, event), ensure(archive_engine, origin, event)
    )
    assert results[0]["source_id"] == results[1]["source_id"]
    assert results[0]["generation_id"] == results[1]["generation_id"]
    async with unit_of_work(
        archive_engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        source = (
            (
                await conn.execute(
                    text(
                        "SELECT next_sync_at, source_use_epoch, config FROM media_sources WHERE id = :id"
                    ),
                    {"id": results[0]["source_id"]},
                )
            )
            .mappings()
            .one()
        )
        assert source["next_sync_at"] is None
        assert source["config"] == {"v": 1}
        assert (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar_one() == 0
        assert (
            await conn.execute(
                text(
                    "SELECT ratio FROM category_post_case_mix WHERE source_id = :id AND effective_to IS NULL"
                ),
                {"id": results[0]["source_id"]},
            )
        ).scalar_one() == 0
        assert (
            await conn.execute(
                text("SELECT count(*) FROM instagram_archive_generations")
            )
        ).scalar_one() == 1
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_source_policies"))
        ).scalar_one() == 1


async def test_reauthorization_reuses_source_but_mints_new_generation(
    archive_engine, origin
):
    first = await ensure(archive_engine, origin, authorization())
    second = await ensure(archive_engine, origin, authorization())
    assert first["source_id"] == second["source_id"]
    assert first["generation_id"] != second["generation_id"]
    assert second["source_use_epoch"] == first["source_use_epoch"] + 1
    async with unit_of_work(
        archive_engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        states = (
            await conn.execute(
                text("SELECT id::text, lifecycle FROM instagram_archive_generations")
            )
        ).all()
        assert dict(states) == {
            first["generation_id"]: "superseded",
            second["generation_id"]: "active",
        }
        assert (
            await conn.execute(
                text(
                    "SELECT count(DISTINCT credential_id) FROM instagram_archive_generations"
                )
            )
        ).scalar_one() == 1


async def test_epoch_bumps_only_on_logical_changes_once_per_transaction(
    archive_engine, origin
):
    from src.services.target import source_state

    result = await ensure(archive_engine, origin, authorization())
    ws, sid = str(origin["ws"]), result["source_id"]
    async with unit_of_work(archive_engine, ws, actor_kind="system").begin() as conn:
        await source_state.lock_workspace_sources(
            conn, workspace_id=ws, ig_account_ids=[str(origin["iga"])], source_ids=[sid]
        )
        before = await source_state.archive_state(conn, workspace_id=ws, source_id=sid)
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio = 1 WHERE source_id = :id AND effective_to IS NULL"
            ),
            {"id": sid},
        )
        first = await source_state.apply_archive_transition(
            conn, workspace_id=ws, source_id=sid, before=before, reason="enabled"
        )
        enabled = await source_state.archive_state(conn, workspace_id=ws, source_id=sid)
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio = 0.5 WHERE source_id = :id AND effective_to IS NULL"
            ),
            {"id": sid},
        )
        same = await source_state.apply_archive_transition(
            conn, workspace_id=ws, source_id=sid, before=enabled, reason="ratio_changed"
        )
        assert same == first
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio = 0 WHERE source_id = :id AND effective_to IS NULL"
            ),
            {"id": sid},
        )
        assert (
            await source_state.apply_archive_transition(
                conn, workspace_id=ws, source_id=sid, before=enabled, reason="disabled"
            )
            == first
        )
    assert first == result["source_use_epoch"] + 1


async def test_routine_payload_refresh_is_not_new_lineage_or_epoch(
    archive_engine, origin
):
    from src.services.target import source_state

    result = await ensure(archive_engine, origin, authorization())
    ws, sid = str(origin["ws"]), result["source_id"]
    async with unit_of_work(archive_engine, ws, actor_kind="system").begin() as conn:
        await source_state.lock_workspace_sources(
            conn, workspace_id=ws, ig_account_ids=[str(origin["iga"])], source_ids=[sid]
        )
        before = await source_state.archive_state(conn, workspace_id=ws, source_id=sid)
        await conn.execute(
            text(
                "UPDATE oauth_credentials SET encrypted_payload = 'refreshed' WHERE id = :id"
            ),
            {"id": str(origin["credential"])},
        )
        assert (
            await source_state.apply_archive_transition(
                conn, workspace_id=ws, source_id=sid, before=before, reason="refresh"
            )
            == result["source_use_epoch"]
        )


async def test_a_change_that_makes_a_source_readable_arms_it_after_the_fence_was_minted(
    archive_engine, origin
):
    from src.services.target import source_state

    result = await ensure(archive_engine, origin, authorization())
    ws, sid = str(origin["ws"]), result["source_id"]
    # Born Off, so nothing is armed; turned On, so the first read is.
    assert await clock_fields(archive_engine, origin, sid) == UNARMED
    epoch = await switch(archive_engine, origin, sid, on=True)
    assert epoch == result["source_use_epoch"] + 1
    assert_armed(await clock_fields(archive_engine, origin, sid))
    await stamp(archive_engine, origin, sid)
    async with unit_of_work(archive_engine, ws, actor_kind="system").begin() as conn:
        await source_state.lock_workspace_sources(
            conn, workspace_id=ws, ig_account_ids=[str(origin["iga"])], source_ids=[sid]
        )
        readable = await source_state.archive_state(
            conn, workspace_id=ws, source_id=sid
        )
        assert readable.readable
        # The credential dies: the fence moves, the source is not readable, and
        # nothing is armed on the way down.
        await conn.execute(
            text("UPDATE oauth_credentials SET state = 'expired' WHERE id = :id"),
            {"id": str(origin["credential"])},
        )
        first = await source_state.apply_archive_transition(
            conn,
            workspace_id=ws,
            source_id=sid,
            before=readable,
            reason="credential_expired",
        )
        assert first == epoch + 1
        assert await read_clock(conn, sid) == UNTOUCHED
        # The same transaction reauthorizes it. The fence cannot move twice, but
        # the source is readable again, and that arms its first read all the same.
        dead = await source_state.archive_state(conn, workspace_id=ws, source_id=sid)
        assert not dead.readable
        await conn.execute(
            text("UPDATE oauth_credentials SET state = 'active' WHERE id = :id"),
            {"id": str(origin["credential"])},
        )
        second = await source_state.apply_archive_transition(
            conn, workspace_id=ws, source_id=sid, before=dead, reason="reauthorized"
        )
        assert second == first
        assert_armed(await read_clock(conn, sid))


@pytest.mark.parametrize(
    ("parked", "state", "reason"),
    [
        ("error", "active", None),
        ("paused", "paused", "source_unavailable"),
        ("active", "active", None),
    ],
    ids=["errored", "paused", "active"],
)
async def test_an_errored_archive_comes_back_active_when_it_is_switched_on(
    archive_engine, origin, parked, state, reason
):
    """A persistent sync failure parks an archive in `error`, with the stamp and
    the alert it leaves behind. Switched Off then On it is armed (its read is
    due), but admission needs `active`: an armed source nothing admits is never
    minted, so the switch must bring it back. A paused source is left exactly as
    it is, and the active one is the positive control: nothing else changes."""
    result = await ensure(archive_engine, origin, authorization())
    ws, sid = str(origin["ws"]), result["source_id"]
    async with unit_of_work(archive_engine, ws, actor_kind="system").begin() as conn:
        await conn.execute(
            text(
                "UPDATE workspaces SET source_mix_mode='equal', source_mix_policy_version=1,"
                " source_membership_policy='reset_equal', source_mix_revision=1,"
                " source_mix_conversion_version=1, source_mix_converted_at=now()"
                " WHERE id=:ws"
            ),
            {"ws": ws},
        )
        await conn.execute(
            text(
                "UPDATE media_sources SET state = :state, next_sync_at = :at,"
                " alerted_at = :alerted WHERE id = :id"
            ),
            {"state": parked, "at": STAMP, "alerted": ALERTED, "id": sid},
        )
    await switch(archive_engine, origin, sid, on=True)
    if parked != "paused":
        assert_armed(await clock_fields(archive_engine, origin, sid))
    async with unit_of_work(archive_engine, ws, actor_kind="system").begin() as conn:
        held = (
            await conn.execute(
                text("SELECT state FROM media_sources WHERE id = :id"), {"id": sid}
            )
        ).scalar_one()
        authority = (
            await conn.execute(
                text(
                    "SELECT admitted, reason FROM fn_source_read_authority("
                    "CAST(:ws AS uuid), CAST(:sid AS uuid), true)"
                ),
                {"ws": ws, "sid": sid},
            )
        ).one()
    assert (held, authority.reason, authority.admitted) == (
        state,
        reason,
        reason is None,
    )


async def test_a_routine_refresh_of_an_on_source_leaves_its_read_clock_alone(
    archive_engine, origin
):
    from src.services.target import source_state

    result = await ensure(archive_engine, origin, authorization())
    ws, sid = str(origin["ws"]), result["source_id"]
    epoch = await switch(archive_engine, origin, sid, on=True)
    await stamp(archive_engine, origin, sid)
    async with unit_of_work(archive_engine, ws, actor_kind="system").begin() as conn:
        await source_state.lock_workspace_sources(
            conn, workspace_id=ws, ig_account_ids=[str(origin["iga"])], source_ids=[sid]
        )
        before = await source_state.archive_state(conn, workspace_id=ws, source_id=sid)
        assert before.readable
        await conn.execute(
            text(
                "UPDATE oauth_credentials SET encrypted_payload = 'refreshed' WHERE id = :id"
            ),
            {"id": str(origin["credential"])},
        )
        assert (
            await source_state.apply_archive_transition(
                conn, workspace_id=ws, source_id=sid, before=before, reason="refresh"
            )
            == epoch
        )
    assert await clock_fields(archive_engine, origin, sid) == UNTOUCHED
    # The same stamp is not out of an arm's reach: a change of state replaces it.
    await switch(archive_engine, origin, sid, on=False)
    assert await clock_fields(archive_engine, origin, sid) == UNTOUCHED
    await switch(archive_engine, origin, sid, on=True)
    assert_armed(await clock_fields(archive_engine, origin, sid))


async def test_transition_requires_the_common_source_lock(archive_engine, origin):
    from src.services.target import source_state

    result = await ensure(archive_engine, origin, authorization())
    async with unit_of_work(
        archive_engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        with pytest.raises(RuntimeError, match="source_lock_required"):
            await source_state.apply_archive_transition(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=result["source_id"],
                before=None,
                reason="unsafe",
            )


async def test_lock_helper_rejects_new_account_after_source_lock(
    archive_engine, origin
):
    from src.services.target import source_state

    result = await ensure(archive_engine, origin, authorization())
    async with unit_of_work(
        archive_engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        await source_state.lock_workspace_sources(
            conn, workspace_id=str(origin["ws"]), source_ids=[result["source_id"]]
        )
        with pytest.raises(RuntimeError, match="source_lock_order_violation"):
            await source_state.lock_workspace_sources(
                conn,
                workspace_id=str(origin["ws"]),
                ig_account_ids=[str(origin["iga"])],
            )


@pytest.mark.parametrize("state", ["expired", "revoked", "past_expiry"])
async def test_unusable_origin_credential_cannot_start_a_generation(
    archive_engine, origin, sync_conn, state
):
    from src.services.target.instagram_sources import ArchiveRefused

    with sync_conn.cursor() as cur:
        if state == "past_expiry":
            cur.execute(
                "UPDATE oauth_credentials SET expires_at = now() - interval '1 hour' WHERE id = %s",
                (origin["credential"],),
            )
        else:
            cur.execute(
                "UPDATE oauth_credentials SET state = %s WHERE id = %s",
                (state, origin["credential"]),
            )
    sync_conn.commit()
    with pytest.raises(ArchiveRefused) as refused:
        await ensure(archive_engine, origin, authorization())
    assert refused.value.reason == "archive_credential_unavailable"
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM media_sources WHERE provider = 'instagram_archive'"
        )
        assert cur.fetchone()[0] == 0


@pytest.mark.parametrize(
    "credential_state", ["missing", "revoked", "expired", "past_expiry"]
)
async def test_caught_credential_refusal_cannot_commit_partial_source(
    archive_engine, origin, sync_conn, credential_state
):
    from src.services.target.instagram_sources import (
        ArchiveRefused,
        ensure_archive_source,
    )

    with sync_conn.cursor() as cur:
        if credential_state == "missing":
            cur.execute(
                "DELETE FROM oauth_credentials WHERE id = %s", (origin["credential"],)
            )
        elif credential_state == "past_expiry":
            cur.execute(
                "UPDATE oauth_credentials SET expires_at = now() - interval '1 hour' WHERE id = %s",
                (origin["credential"],),
            )
        else:
            cur.execute(
                "UPDATE oauth_credentials SET state = %s WHERE id = %s",
                (credential_state, origin["credential"]),
            )
    sync_conn.commit()
    event = authorization()
    async with unit_of_work(
        archive_engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        with pytest.raises(ArchiveRefused, match="archive_credential_unavailable"):
            await ensure_archive_source(
                conn,
                workspace_id=str(origin["ws"]),
                origin_ig_account_id=str(origin["iga"]),
                authorization=event,
                capability=capability(),
            )
        # The caller continues unrelated work after catching the refusal.
        await conn.execute(
            text(
                "UPDATE media_sources SET config = config || jsonb_build_object('after_refusal', true) WHERE id = :sid"
            ),
            {"sid": str(origin["src"])},
        )
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT config->>'after_refusal' FROM media_sources WHERE id = %s",
            (origin["src"],),
        )
        assert cur.fetchone()[0] == "true"
        for table in (
            "media_sources",
            "instagram_source_policies",
            "media_source_destinations",
            "category_post_case_mix",
        ):
            where = (
                " AND provider = 'instagram_archive'"
                if table == "media_sources"
                else ""
            )
            cur.execute(
                f"SELECT count(*) FROM {table} WHERE workspace_id = %s{where}",
                (origin["ws"],),
            )
            assert cur.fetchone()[0] == 0, table
        if credential_state == "missing":
            cur.execute(
                "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider, encrypted_payload) VALUES (%s, %s, 'ig_login', 'test-ciphertext')",
                (origin["ws"], origin["iga"]),
            )
        else:
            cur.execute(
                "UPDATE oauth_credentials SET state = 'active', expires_at = NULL WHERE id = %s",
                (origin["credential"],),
            )
    sync_conn.commit()
    result = await ensure(archive_engine, origin, event)
    assert result["created"] is True
    with sync_conn.cursor() as cur:
        for table in (
            "instagram_source_policies",
            "media_source_destinations",
            "category_post_case_mix",
        ):
            cur.execute(
                f"SELECT count(*) FROM {table} WHERE workspace_id = %s AND source_id = %s",
                (origin["ws"], result["source_id"]),
            )
            assert cur.fetchone()[0] == 1, table


async def test_refused_provisioning_does_not_retain_rolled_back_source_locks(
    archive_engine, origin, sync_conn
):
    from src.services.target import source_state
    from src.services.target.instagram_sources import (
        ArchiveRefused,
        ensure_archive_source,
    )

    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_credentials SET state = 'revoked' WHERE id = %s",
            (origin["credential"],),
        )
        cur.execute(
            "INSERT INTO ig_accounts (workspace_id, provider_account_ref) VALUES (%s, 'another-origin') RETURNING id",
            (origin["ws"],),
        )
        other = str(cur.fetchone()[0])
    sync_conn.commit()
    async with unit_of_work(
        archive_engine, str(origin["ws"]), actor_kind="system"
    ).begin() as conn:
        with pytest.raises(ArchiveRefused):
            await ensure_archive_source(
                conn,
                workspace_id=str(origin["ws"]),
                origin_ig_account_id=str(origin["iga"]),
                authorization=authorization(),
                capability=capability(),
            )
        # A rolled-back source lock must not block acquiring an earlier-class row.
        await source_state.lock_workspace_sources(
            conn, workspace_id=str(origin["ws"]), ig_account_ids=[other]
        )


async def test_refused_provisioning_preserves_outer_writes_and_locks(
    archive_engine, origin, sync_conn
):
    from src.services.target import source_state
    from src.services.target.instagram_sources import (
        ArchiveRefused,
        ensure_archive_source,
    )

    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE oauth_credentials SET state = 'revoked' WHERE id = %s",
            (origin["credential"],),
        )
    sync_conn.commit()
    ws, account, source = map(str, (origin["ws"], origin["iga"], origin["src"]))
    async with unit_of_work(archive_engine, ws, actor_kind="system").begin() as conn:
        await source_state.lock_workspace_sources(
            conn, workspace_id=ws, ig_account_ids=[account], source_ids=[source]
        )
        await conn.execute(
            text(
                "UPDATE media_sources SET config = config || jsonb_build_object('outer_write', true) WHERE id = :sid"
            ),
            {"sid": source},
        )
        with pytest.raises(ArchiveRefused):
            await ensure_archive_source(
                conn,
                workspace_id=ws,
                origin_ig_account_id=account,
                authorization=authorization(),
                capability=capability(),
            )
        source_state.require_source_lock(conn, ws, source)
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM media_sources WHERE provider = 'instagram_archive'"
                )
            )
        ).scalar_one() == 0
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT config->>'outer_write' FROM media_sources WHERE id = %s", (source,)
        )
        assert cur.fetchone()[0] == "true"


@pytest.mark.parametrize("actor_mode", ["unset", "empty"])
async def test_source_epoch_missing_actor_refuses_and_savepoint_rolls_back_for_retry(
    archive_engine, origin, actor_mode
):
    from sqlalchemy.exc import DBAPIError
    from src.services.target import source_state
    from src.services.target.unit_of_work import apply_gucs

    source = await ensure(archive_engine, origin, authorization())
    # A new pool proves the genuinely unset GUC case; recycled sessions may
    # expose an empty setting after a previous SET LOCAL transaction.
    engine = (
        create_async_engine(archive_engine.url)
        if actor_mode == "unset"
        else archive_engine
    )
    ws, sid = str(origin["ws"]), source["source_id"]
    try:
        async with unit_of_work(
            engine, ws, actor_kind=None if actor_mode == "unset" else ""
        ).begin() as conn:
            assert (
                await conn.execute(
                    text("SELECT current_setting('app.actor_kind', true)")
                )
            ).scalar_one() == (None if actor_mode == "unset" else "")
            audit_count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                    )
                )
            ).scalar_one()
            with pytest.raises(DBAPIError) as refused:
                async with source_state.source_savepoint(conn):
                    await source_state.lock_workspace_sources(
                        conn, workspace_id=ws, source_ids=[sid]
                    )
                    before = await source_state.archive_state(
                        conn, workspace_id=ws, source_id=sid
                    )
                    await conn.execute(
                        text(
                            "UPDATE category_post_case_mix SET ratio=1 WHERE source_id=:sid"
                        ),
                        {"sid": sid},
                    )
                    await source_state.apply_archive_transition(
                        conn,
                        workspace_id=ws,
                        source_id=sid,
                        before=before,
                        reason="enabled",
                    )
            # Shared writer/default actor + audit table constraints refuse
            # both truly unset and empty actors without synthesizing one.
            assert refused.value.orig.sqlstate in {"42704", "23502", "23514"}
            state = (
                (
                    await conn.execute(
                        text(
                            "SELECT source_use_epoch, source_epoch_transaction_id FROM media_sources WHERE id=:sid"
                        ),
                        {"sid": sid},
                    )
                )
                .mappings()
                .one()
            )
            assert state["source_use_epoch"] == source["source_use_epoch"]
            assert (
                await conn.execute(
                    text(
                        "SELECT ratio FROM category_post_case_mix WHERE source_id=:sid AND effective_to IS NULL"
                    ),
                    {"sid": sid},
                )
            ).scalar_one() == 0
            assert (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                    )
                )
            ).scalar_one() == audit_count
            with pytest.raises(RuntimeError, match="source_lock_required"):
                source_state.require_source_lock(conn, ws, sid)
            # Catch, continue unrelated work, and retry in this transaction.
            await apply_gucs(conn, tenant_id=ws, actor_kind="system", channel="system")
            await conn.execute(
                text(
                    "UPDATE media_sources SET config=config || jsonb_build_object('after_actor_refusal', true) WHERE id=:sid"
                ),
                {"sid": str(origin["src"])},
            )
            async with source_state.source_savepoint(conn):
                await source_state.lock_workspace_sources(
                    conn, workspace_id=ws, source_ids=[sid]
                )
                before = await source_state.archive_state(
                    conn, workspace_id=ws, source_id=sid
                )
                await conn.execute(
                    text(
                        "UPDATE category_post_case_mix SET ratio=1 WHERE source_id=:sid"
                    ),
                    {"sid": sid},
                )
                epoch = await source_state.apply_archive_transition(
                    conn,
                    workspace_id=ws,
                    source_id=sid,
                    before=before,
                    reason="enabled",
                )
                assert epoch == source["source_use_epoch"] + 1
                assert (
                    await source_state.apply_archive_transition(
                        conn,
                        workspace_id=ws,
                        source_id=sid,
                        before=before,
                        reason="enabled",
                    )
                    == epoch
                )
            saved = (
                (
                    await conn.execute(
                        text(
                            "SELECT actor_kind, channel, detail FROM audit_events WHERE entity_kind='instagram_source' ORDER BY id DESC LIMIT 1"
                        )
                    )
                )
                .mappings()
                .one()
            )
            assert saved == {
                "actor_kind": "system",
                "channel": "system",
                "detail": {"reason": "enabled", "source_use_epoch": epoch},
            }
            assert (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                    )
                )
            ).scalar_one() == audit_count + 1
        async with unit_of_work(engine, ws, actor_kind="system").begin() as conn:
            assert (
                await conn.execute(
                    text("SELECT source_use_epoch FROM media_sources WHERE id=:sid"),
                    {"sid": sid},
                )
            ).scalar_one() == epoch
            assert (
                await conn.execute(
                    text(
                        "SELECT config->>'after_actor_refusal' FROM media_sources WHERE id=:sid"
                    ),
                    {"sid": str(origin["src"])},
                )
            ).scalar_one() == "true"
    finally:
        if actor_mode == "unset":
            await engine.dispose()


async def test_epoch_audit_uses_shared_writer_and_preserves_actor_and_null_states_once(
    archive_engine, origin, monkeypatch
):
    from src.services.target import audit, source_state

    source = await ensure(archive_engine, origin, authorization())
    original, calls = audit.record, []

    async def observed(executor, **kw):
        calls.append((executor, kw))
        await original(executor, **kw)

    monkeypatch.setattr(audit, "record", observed)
    ws, sid = str(origin["ws"]), source["source_id"]
    async with unit_of_work(
        archive_engine,
        ws,
        actor_kind="user",
        actor_user_id=str(origin["user"]),
        channel="cli",
    ).begin() as conn:
        await source_state.lock_workspace_sources(
            conn, workspace_id=ws, source_ids=[sid]
        )
        before = await source_state.archive_state(conn, workspace_id=ws, source_id=sid)
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                )
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio=1 WHERE source_id=:sid"),
            {"sid": sid},
        )
        epoch = await source_state.apply_archive_transition(
            conn, workspace_id=ws, source_id=sid, before=before, reason="enabled"
        )
        # Repeating, then changing the logical state again, still audits once.
        assert (
            await source_state.apply_archive_transition(
                conn, workspace_id=ws, source_id=sid, before=before, reason="enabled"
            )
            == epoch
        )
        enabled_state = await source_state.archive_state(
            conn, workspace_id=ws, source_id=sid
        )
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"),
            {"sid": sid},
        )
        assert (
            await source_state.apply_archive_transition(
                conn, workspace_id=ws, source_id=sid, before=enabled_state, reason="off"
            )
            == epoch
        )
        assert len(calls) == 1 and calls[0][0] is conn
        assert calls[0][1] == {
            "workspace_id": ws,
            "entity_kind": "instagram_source",
            "entity_id_sql": ":sid",
            "sid": sid,
            "from_state": None,
            "to_state": None,
            "detail": {"reason": "enabled", "source_use_epoch": epoch},
        }
        saved = (
            (
                await conn.execute(
                    text(
                        "SELECT workspace_id, entity_id, from_state, to_state, actor_kind, actor_user_id, channel, detail "
                        "FROM audit_events WHERE entity_kind='instagram_source' ORDER BY id DESC LIMIT 1"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert str(saved["workspace_id"]) == ws and str(saved["entity_id"]) == sid
        assert saved["from_state"] is None and saved["to_state"] is None
        assert saved["actor_kind"] == "user" and str(saved["actor_user_id"]) == str(
            origin["user"]
        )
        assert saved["channel"] == "cli" and saved["detail"] == {
            "reason": "enabled",
            "source_use_epoch": epoch,
        }
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                )
            )
        ).scalar_one() == count + 1
    async with unit_of_work(archive_engine, ws, actor_kind="system").begin() as conn:
        await source_state.lock_workspace_sources(
            conn, workspace_id=ws, source_ids=[sid]
        )
        before = await source_state.archive_state(conn, workspace_id=ws, source_id=sid)
        assert (
            await source_state.apply_archive_transition(
                conn, workspace_id=ws, source_id=sid, before=before, reason="unchanged"
            )
            == epoch
        )
        assert len(calls) == 1
