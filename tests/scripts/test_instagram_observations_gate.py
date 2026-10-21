"""Past-posts bundle landing and scoped erasure through actual tenant roles."""

import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from src.services.target.unit_of_work import unit_of_work
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts.test_instagram_sources_gate import (
    authorization,
    capability,
    ensure,
)
from tests.scripts.conftest import seed_workspace_chain

pytestmark = [pytest.mark.integration]
past_posts_engine = foundation.past_posts_engine
origin = foundation.origin


def bundle(parent="123", **kw):
    from src.services.target.instagram_catalog import (
        PastPostsAsset,
        PastPostsObservation,
    )

    return PastPostsObservation(
        parent_media_id=parent,
        original_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        assets=(PastPostsAsset(selected_asset_id=parent, media_kind="image"),),
        **kw,
    )


def keys(version=1, retained=True):
    from src.services.target.instagram_catalog import TombstoneKeys

    mapping = {version: bytes([version]) * 32}
    if retained:
        mapping[1] = b"\x01" * 32
    return TombstoneKeys(current_version=version, keys=mapping)


def uow(engine, chain):
    return unit_of_work(engine, str(chain["ws"]), actor_kind="system").begin()


async def enabled(engine, chain):
    src = await ensure(engine, chain, authorization())
    async with uow(engine, chain) as conn:
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio = 1 WHERE source_id = :sid"),
            {"sid": src["source_id"]},
        )
    return src


async def observe(engine, chain, source, observation, ring=None, **kw):
    from src.services.target.instagram_catalog import write_observation

    async with uow(engine, chain) as conn:
        return await write_observation(
            conn,
            workspace_id=str(chain["ws"]),
            source_id=source["source_id"],
            generation_id=source["generation_id"],
            captured_epoch=source["source_use_epoch"],
            observation=observation,
            keys=ring or keys(),
            capability=capability(),
            **kw,
        )


async def row(conn, sql, **params):
    return (await conn.execute(text(sql), params)).mappings().one()


async def redact(engine, chain, source, ring=None):
    from src.services.target.instagram_catalog import redact_generations

    async with uow(engine, chain) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_past_posts_generations SET lifecycle = 'deletion_pending' WHERE id = :gid"
            ),
            {"gid": source["generation_id"]},
        )
        return await redact_generations(
            conn,
            workspace_id=str(chain["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            keys=ring or keys(),
            capability=capability(),
        )


async def test_complete_replacement_clears_optional_fields_and_keeps_history(
    past_posts_engine, origin
):
    from src.services.target.instagram_catalog import PastPostsAsset

    first = await enabled(past_posts_engine, origin)
    observation = replace(
        bundle(
            permalink="https://www.instagram.com/p/abc/",
            like_count=8,
            comments_count=3,
            attribution="credited",
        ),
        assets=(PastPostsAsset("123", "video", "video/mp4", 99),),
    )
    landed = await observe(past_posts_engine, origin, first, observation)
    async with uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE media_items SET times_posted = 4, last_posted_at = now() WHERE id = :id"
            ),
            {"id": landed.media_item_id},
        )
        await conn.execute(
            text(
                "INSERT INTO post_locks (workspace_id, media_item_id, kind) VALUES (:ws, :id, 'hold')"
            ),
            {"ws": str(origin["ws"]), "id": landed.media_item_id},
        )
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported', validation_generation_id=data_generation_id, validation_observation_revision=observation_revision, validation_fingerprint='checked', validated_at=now()"
            )
        )
    second = await ensure(past_posts_engine, origin, authorization())
    landed2 = await observe(past_posts_engine, origin, second, bundle())
    assert landed2.media_item_id == landed.media_item_id
    assert landed2.observation_revision == 2
    async with uow(past_posts_engine, origin) as conn:
        index = await row(conn, "SELECT * FROM instagram_media_index")
        assert str(index["data_generation_id"]) == second["generation_id"]
        assert index["attribution"] == "unknown"
        for key in (
            "permalink",
            "like_count",
            "comments_count",
        ):
            assert index[key] is None, key
        asset = await row(conn, "SELECT * FROM instagram_media_assets")
        assert asset["validation_state"] == "pending"
        for key in (
            "validation_generation_id",
            "validation_observation_revision",
            "validation_fingerprint",
            "validated_at",
        ):
            assert asset[key] is None, key
        item = await row(
            conn, "SELECT * FROM media_items WHERE id = :id", id=landed.media_item_id
        )
        assert item["times_posted"] == 4 and item["last_posted_at"] is not None
        assert item["media_kind"] == "image"
        for key in (
            "mime_type",
            "file_size",
            "thumbnail_url",
            "caption",
            "custom_metadata",
        ):
            assert item[key] is None, key
        assert (
            await conn.execute(
                text("SELECT count(*) FROM post_locks WHERE media_item_id = :id"),
                {"id": landed.media_item_id},
            )
        ).scalar_one() == 1
    cleanup = await redact(past_posts_engine, origin, first)
    assert cleanup.observations_deleted == 0 and cleanup.items_redacted == 0


async def test_a_past_posts_item_is_stored_under_a_neutral_name_never_its_graph_id(
    past_posts_engine, origin
):
    """The stored name is what the Telegram card and the missed-story notice say,
    so a past-posts post is named for where it came from, not by Instagram's id
    (`provider_file_ref` keeps the id). Landing names it so, and a landing over a
    row stored as `Instagram <Graph id>` renames it."""
    from src.services.target import vocabulary

    first = await enabled(past_posts_engine, origin)
    landed = await observe(past_posts_engine, origin, first, bundle("123"))

    async def stored():
        async with uow(past_posts_engine, origin) as conn:
            found = await row(
                conn,
                "SELECT file_name, provider_file_ref FROM media_items WHERE id = :id",
                id=landed.media_item_id,
            )
        return found["file_name"], found["provider_file_ref"]

    assert await stored() == (vocabulary.PAST_POSTS_ITEM_NAME, "123")
    assert vocabulary.PAST_POSTS_ITEM_NAME == "Instagram post"
    async with uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE media_items SET file_name = 'Instagram 123' WHERE id = :id"),
            {"id": landed.media_item_id},
        )
    assert await stored() == ("Instagram 123", "123"), "the id-bearing name, as stored"
    second = await ensure(past_posts_engine, origin, authorization())
    again = await observe(past_posts_engine, origin, second, bundle("123"))
    assert again.media_item_id == landed.media_item_id
    assert await stored() == (vocabulary.PAST_POSTS_ITEM_NAME, "123")


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("epoch", "past_posts_stale_capture"),
        ("off", "past_posts_source_off"),
        ("removed", "past_posts_source_unavailable"),
        ("credential", "past_posts_credential_unavailable"),
        ("expired", "past_posts_credential_unavailable"),
        ("origin", "past_posts_origin_unavailable"),
        ("binding", "past_posts_source_unavailable"),
        ("generation", "past_posts_stale_capture"),
    ],
)
async def test_stale_or_unavailable_capture_drops_without_writes(
    past_posts_engine, origin, mutation, reason
):
    src = await enabled(past_posts_engine, origin)
    if mutation == "epoch":
        src = {**src, "source_use_epoch": src["source_use_epoch"] + 1}
    elif mutation == "generation":
        await ensure(past_posts_engine, origin, authorization())
    else:
        queries = {
            "off": "UPDATE category_post_case_mix SET ratio = 0",
            "removed": "UPDATE media_sources SET config = config || jsonb_build_object('removed', true) WHERE provider='instagram_past_posts'",
            "credential": "UPDATE oauth_credentials SET state='revoked'",
            "expired": "UPDATE oauth_credentials SET expires_at=now()-interval '1 second'",
            "origin": "UPDATE ig_accounts SET state='disabled'",
            "binding": "UPDATE media_source_destinations SET state='retired'",
        }
        async with uow(past_posts_engine, origin) as conn:
            await conn.execute(text(queries[mutation]))
    result = await observe(past_posts_engine, origin, src, bundle())
    assert not result.accepted and result.reason == reason
    async with uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_media_index"))
        ).scalar_one() == 0


async def test_invalid_partial_parse_does_not_relabel_but_optional_absence_can_land(
    past_posts_engine, origin
):
    from src.services.target.instagram_sources import PastPostsRefused

    first = await enabled(past_posts_engine, origin)
    item = await observe(past_posts_engine, origin, first, bundle(like_count=9))
    second = await ensure(past_posts_engine, origin, authorization())
    for changes in (
        {"original_at": None},
        {"original_at": datetime(2020, 1, 1)},
        {"parent_media_id": "bad"},
        {"like_count": -1},
        {"comments_count": True},
        {"permalink": "https://cdn.example/image?token=secret"},
    ):
        with pytest.raises(PastPostsRefused, match="invalid_past_posts_observation"):
            await observe(
                past_posts_engine, origin, second, replace(bundle(), **changes)
            )
    async with uow(past_posts_engine, origin) as conn:
        current = await row(conn, "SELECT * FROM instagram_media_index")
        assert str(current["data_generation_id"]) == first["generation_id"]
        assert current["like_count"] == 9
    landed = await observe(
        past_posts_engine,
        origin,
        second,
        replace(
            bundle(),
            assets=(),
            availability="excluded",
            exclusion_reason="incomplete_observation",
        ),
    )
    assert landed.media_item_id is None
    assert landed.observation_revision == 2
    async with uow(past_posts_engine, origin) as conn:
        prior = await row(
            conn, "SELECT state FROM media_items WHERE id=:id", id=item.media_item_id
        )
        assert prior["state"] == "removed"


async def test_excluded_observation_gains_first_item_and_policy_never_rejects_raw_data(
    past_posts_engine, origin
):
    src = await enabled(past_posts_engine, origin)
    excluded = replace(
        bundle(),
        assets=(),
        availability="excluded",
        exclusion_reason="unsupported_media",
    )
    first = await observe(past_posts_engine, origin, src, excluded)
    assert first.accepted and first.media_item_id is None
    async with uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_source_policies SET minimum_age_days=36500, include_credited=false, include_unknown=false"
            )
        )
    second = await observe(
        past_posts_engine, origin, src, bundle(attribution="credited")
    )
    third = await observe(past_posts_engine, origin, src, bundle())
    assert second.accepted and second.media_item_id is not None
    assert (
        third.media_item_id == second.media_item_id and third.observation_revision == 3
    )


async def second_origin(engine, chain):
    async with uow(engine, chain) as conn:
        account = (
            await conn.execute(
                text(
                    "INSERT INTO ig_accounts (workspace_id, provider_account_ref, handle) VALUES (:ws, 'other-provider-ref', 'other-handle') RETURNING id"
                ),
                {"ws": str(chain["ws"])},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO oauth_credentials (workspace_id, ig_account_id, provider, encrypted_payload) VALUES (:ws, :id, 'ig_login', 'fake')"
            ),
            {"ws": str(chain["ws"]), "id": str(account)},
        )
    other = {**chain, "iga": str(account)}
    return await enabled(engine, other)


async def test_cross_origin_conflict_is_successful_index_only_and_drive_namespace_distinct(
    past_posts_engine, origin
):
    first = await enabled(past_posts_engine, origin)
    incoming = await second_origin(past_posts_engine, origin)
    one = await observe(past_posts_engine, origin, first, bundle())
    conflict = await observe(past_posts_engine, origin, incoming, bundle())
    assert conflict.accepted and conflict.reason == "origin_identity_conflict"
    assert conflict.media_item_id is None
    async with uow(past_posts_engine, origin) as conn:
        own = await row(
            conn,
            "SELECT source_id, content_hash FROM media_items WHERE id=:id",
            id=one.media_item_id,
        )
        assert str(own["source_id"]) == first["source_id"]
        assert own["content_hash"] == "instagram_past_posts:media:123"
        # A raw-ID or byte hash from Drive does not occupy the past-posts namespace.
        await conn.execute(
            text(
                "INSERT INTO media_items (workspace_id, source_id, content_hash, file_name, media_kind, provider_file_ref) VALUES (:ws, :src, '999', 'drive.jpg', 'image', '999')"
            ),
            {"ws": str(origin["ws"]), "src": str(origin["src"])},
        )
    assert (
        await observe(past_posts_engine, origin, first, bundle("999"))
    ).media_item_id


async def test_redaction_restores_only_from_fresh_generation_using_retained_hmac_key(
    past_posts_engine, origin
):
    first = await enabled(past_posts_engine, origin)
    original = await observe(past_posts_engine, origin, first, bundle())
    untouched = await observe(past_posts_engine, origin, first, bundle("789"))
    async with uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE media_items SET times_posted=2 WHERE id=:id"),
            {"id": original.media_item_id},
        )
    erased = await redact(past_posts_engine, origin, first)
    assert erased.observations_deleted == 2 and erased.items_redacted == 2
    assert set(erased.media_item_ids) == {
        original.media_item_id,
        untouched.media_item_id,
    }
    again = await redact(past_posts_engine, origin, first)
    assert again.observations_deleted == again.items_redacted == 0
    async with uow(past_posts_engine, origin) as conn:
        item = await row(
            conn, "SELECT * FROM media_items WHERE id=:id", id=original.media_item_id
        )
        assert item["source_projection_redacted"] is True
        assert item["source_data_generation_id"] is None and item["times_posted"] == 2
        assert item["provider_file_ref"] == f"redacted:{original.media_item_id}"
        assert item["thumbnail_url"] is None and item["state"] == "removed"
        tomb = await row(
            conn,
            "SELECT * FROM instagram_item_tombstones WHERE media_item_id=:id",
            id=original.media_item_id,
        )
        assert tomb["parent_hmac"] == keys().digest(1, "123")
        assert "parent_media_id" not in tomb and tomb["key_version"] == 1
    stale = await observe(past_posts_engine, origin, first, bundle())
    assert not stale.accepted
    second = await ensure(past_posts_engine, origin, authorization())
    # Reauthorization itself does not restore any data.
    async with uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_media_index"))
        ).scalar_one() == 0
    restored = await observe(past_posts_engine, origin, second, bundle(), ring=keys(2))
    assert (
        restored.media_item_id == original.media_item_id
        and restored.observation_revision == 2
    )
    other = await second_origin(past_posts_engine, origin)
    assert (
        await observe(past_posts_engine, origin, other, bundle("789"), ring=keys(2))
    ).reason == "origin_identity_conflict"
    cleanup = await redact(past_posts_engine, origin, first, ring=keys(2))
    assert cleanup.items_redacted == 0


async def tombstones(conn):
    return [
        (r["media_item_id"], r["key_version"], r["observation_revision"])
        for r in (
            await conn.execute(
                text(
                    "SELECT media_item_id::text, key_version, observation_revision"
                    " FROM instagram_item_tombstones ORDER BY media_item_id"
                )
            )
        ).mappings()
    ]


async def test_an_item_erased_again_after_a_restore_keeps_one_tombstone_at_the_newer_revision(
    past_posts_engine, origin
):
    first = await enabled(past_posts_engine, origin)
    original = await observe(past_posts_engine, origin, first, bundle())
    await redact(past_posts_engine, origin, first)
    async with uow(past_posts_engine, origin) as conn:
        assert await tombstones(conn) == [(original.media_item_id, 1, 1)]
    second = await ensure(past_posts_engine, origin, authorization())
    restored = await observe(past_posts_engine, origin, second, bundle())
    assert restored.media_item_id == original.media_item_id
    assert restored.observation_revision == 2

    erased = await redact(past_posts_engine, origin, second)

    # The tombstone the first erasure left is the one the second finds: merged,
    # at the revision the restore reached, not a second row and not a refusal.
    assert erased.items_redacted == 1
    async with uow(past_posts_engine, origin) as conn:
        assert await tombstones(conn) == [(original.media_item_id, 1, 2)]


async def test_items_that_share_a_parent_merge_into_one_tombstone_at_the_highest_revision(
    past_posts_engine, origin
):
    first = await enabled(past_posts_engine, origin)
    original = await observe(past_posts_engine, origin, first, bundle())
    again = await observe(past_posts_engine, origin, first, bundle())
    assert again.observation_revision == 2
    # A second shared item for the same parent, as a dedup race could leave one:
    # its own hash and no index row, so it reads as revision 1. Its id sorts first,
    # so the lower revision is written before the higher one.
    twin = "00000000-0000-0000-0000-000000000001"
    async with uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "INSERT INTO media_items (id, workspace_id, source_id, source_data_generation_id,"
                " content_hash, provider_file_ref, file_name, media_kind, mime_type, file_size,"
                " state, last_listed_at) SELECT :twin, workspace_id, source_id,"
                " source_data_generation_id, 'twin:' || content_hash, provider_file_ref, file_name,"
                " media_kind, mime_type, file_size, state, last_listed_at FROM media_items"
                " WHERE id = :id"
            ),
            {"twin": twin, "id": original.media_item_id},
        )

    erased = await redact(past_posts_engine, origin, first)

    # One statement per row, as a loop made it: the second row meets the first's
    # tombstone and raises it to its own revision; neither is refused.
    assert (erased.observations_deleted, erased.items_redacted) == (1, 2)
    async with uow(past_posts_engine, origin) as conn:
        assert await tombstones(conn) == [(twin, 1, 2)]


async def test_missing_historical_key_refuses_atomically_and_retry_recovers(
    past_posts_engine, origin
):
    from src.services.target.instagram_catalog import write_observation
    from src.services.target.instagram_sources import PastPostsRefused

    first = await enabled(past_posts_engine, origin)
    original = await observe(past_posts_engine, origin, first, bundle())
    await redact(past_posts_engine, origin, first)
    second = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        with pytest.raises(
            PastPostsRefused, match="past_posts_tombstone_key_unavailable"
        ):
            await write_observation(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=second["source_id"],
                generation_id=second["generation_id"],
                captured_epoch=second["source_use_epoch"],
                observation=bundle(),
                keys=keys(2, retained=False),
                capability=capability(),
            )
        await conn.execute(text("UPDATE workspaces SET name='continued'"))
    result = await observe(past_posts_engine, origin, second, bundle(), ring=keys(2))
    assert result.media_item_id == original.media_item_id


async def test_wrong_source_generation_and_foreign_tenant_cannot_land_or_redact(
    past_posts_engine, origin, sync_conn
):
    from src.services.target.instagram_catalog import redact_generations
    from src.services.target.instagram_sources import PastPostsRefused

    own = await enabled(past_posts_engine, origin)
    other = await second_origin(past_posts_engine, origin)
    assert not (
        await observe(
            past_posts_engine,
            origin,
            {**own, "generation_id": other["generation_id"]},
            bundle(),
        )
    ).accepted
    foreign = seed_workspace_chain(sync_conn, "foreign-observation")
    assert not (await observe(past_posts_engine, foreign, own, bundle())).accepted
    await observe(past_posts_engine, origin, own, bundle())
    for chain, source, generation in ((foreign, own, own), (origin, other, own)):
        async with uow(past_posts_engine, chain) as conn:
            with pytest.raises(
                PastPostsRefused, match="past_posts_generation_scope_unavailable"
            ):
                await redact_generations(
                    conn,
                    workspace_id=str(chain["ws"]),
                    source_id=source["source_id"],
                    generation_ids=frozenset([generation["generation_id"]]),
                    keys=keys(),
                    capability=capability(),
                )
    async with uow(past_posts_engine, foreign) as conn:
        result = await conn.execute(
            text("DELETE FROM instagram_media_index WHERE workspace_id=:ws"),
            {"ws": str(origin["ws"])},
        )
        assert result.rowcount == 0
    async with uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_media_index"))
        ).scalar_one() == 1


@pytest.mark.parametrize("projection_state", ["removed", "missing", "redacted"])
async def test_drive_page_cannot_adopt_past_posts_projection_or_redaction_sentinel(
    past_posts_engine, origin, projection_state
):
    from src.services.target import media_sync
    from src.services.target.google_drive_adapter import GoogleDriveAdapter

    source = await enabled(past_posts_engine, origin)
    original = await observe(past_posts_engine, origin, source, bundle())
    if projection_state == "redacted":
        await redact(past_posts_engine, origin, source)
    else:
        async with uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text("UPDATE media_items SET state=:state WHERE id=:id"),
                {"id": original.media_item_id, "state": projection_state},
            )
    async with uow(past_posts_engine, origin) as conn:
        item = await row(
            conn,
            "SELECT content_hash, provider_file_ref, source_projection_redacted FROM media_items WHERE id=:id",
            id=original.media_item_id,
        )

    def factory():
        return uow(past_posts_engine, origin)

    outcome = await media_sync._land_page(
        factory,
        {"id": str(uuid.uuid4())},
        adapters={"gdrive": GoogleDriveAdapter},
        adapter=GoogleDriveAdapter,
        provider="gdrive",
        items=[
            {
                "kind": "image",
                "content_hash": item["content_hash"],
                "ref": "drive-ref",
                "name": "drive.jpg",
            }
        ],
        new_checkpoint={"v": 2},
        stored=None,
        walk="fixture-walk",
        source_id=str(origin["src"]),
        workspace_id=str(origin["ws"]),
        reason="fixture",
    )
    assert outcome == "synced"
    async with uow(past_posts_engine, origin) as conn:
        after = await row(
            conn,
            "SELECT source_id, provider_file_ref, source_projection_redacted FROM media_items WHERE id=:id",
            id=original.media_item_id,
        )
        assert str(after["source_id"]) == source["source_id"]
        assert after["provider_file_ref"] == item["provider_file_ref"]
        assert after["source_projection_redacted"] == item["source_projection_redacted"]


async def test_late_g1_cleanup_erases_unobserved_g1_but_preserves_refreshed_g2(
    past_posts_engine, origin
):
    first = await enabled(past_posts_engine, origin)
    refreshed = await observe(past_posts_engine, origin, first, bundle())
    old = await observe(past_posts_engine, origin, first, bundle("789"))
    second = await ensure(past_posts_engine, origin, authorization())
    await observe(past_posts_engine, origin, second, bundle())
    result = await redact(past_posts_engine, origin, first)
    assert result.media_item_ids == (old.media_item_id,)
    assert result.observations_deleted == result.items_redacted == 1
    async with uow(past_posts_engine, origin) as conn:
        fresh = await row(
            conn,
            "SELECT source_data_generation_id, source_projection_redacted FROM media_items WHERE id=:id",
            id=refreshed.media_item_id,
        )
        assert str(fresh["source_data_generation_id"]) == second["generation_id"]
        assert fresh["source_projection_redacted"] is False
        assert (await row(conn, "SELECT media_item_id FROM instagram_media_assets"))[
            "media_item_id"
        ] == uuid.UUID(refreshed.media_item_id)


async def test_caught_observation_constraint_failure_rolls_back_shared_projection_then_retries(
    past_posts_engine, origin, sync_conn
):
    from src.services.target.instagram_catalog import write_observation
    from sqlalchemy.exc import IntegrityError

    first = await enabled(past_posts_engine, origin)
    original = await observe(past_posts_engine, origin, first, bundle(like_count=7))
    second = await ensure(past_posts_engine, origin, authorization())
    with sync_conn.cursor() as cur:
        cur.execute(
            "CREATE FUNCTION fixture_refuse_observation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture observation refusal' USING ERRCODE='23514'; END $$"
        )
        cur.execute(
            "CREATE TRIGGER fixture_refuse_observation BEFORE UPDATE ON instagram_media_index FOR EACH ROW EXECUTE FUNCTION fixture_refuse_observation()"
        )
    sync_conn.commit()
    async with uow(past_posts_engine, origin) as conn:
        with pytest.raises(IntegrityError, match="fixture observation refusal"):
            await write_observation(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=second["source_id"],
                generation_id=second["generation_id"],
                captured_epoch=second["source_use_epoch"],
                observation=bundle(),
                keys=keys(),
                capability=capability(),
            )
        await conn.execute(text("UPDATE workspaces SET name='observation-continued'"))
    async with uow(past_posts_engine, origin) as conn:
        item = await row(
            conn,
            "SELECT source_data_generation_id FROM media_items WHERE id=:id",
            id=original.media_item_id,
        )
        index = await row(
            conn, "SELECT data_generation_id, like_count FROM instagram_media_index"
        )
        assert (
            str(item["source_data_generation_id"])
            == first["generation_id"]
            == str(index["data_generation_id"])
        )
        assert index["like_count"] == 7
    with sync_conn.cursor() as cur:
        cur.execute("DROP TRIGGER fixture_refuse_observation ON instagram_media_index")
    sync_conn.commit()
    retry = await observe(past_posts_engine, origin, second, bundle())
    assert (
        retry.media_item_id == original.media_item_id
        and retry.observation_revision == 2
    )


async def test_redacted_projection_constraint_refuses_every_provider_field_and_delete_stays_narrow(
    past_posts_engine, origin
):
    from sqlalchemy.exc import IntegrityError, ProgrammingError

    source = await enabled(past_posts_engine, origin)
    original = await observe(past_posts_engine, origin, source, bundle())
    await redact(past_posts_engine, origin, source)
    mutations = (
        "source_data_generation_id=:gid",
        "provider_file_ref='123'",
        "file_name='provider-name'",
        "content_hash='provider-hash'",
        "media_kind='video'",
        "state='available'",
        "mime_type='image/jpeg'",
        "file_size=42",
        "category='provider-category'",
        "folder_path='provider-path'",
        "last_listed_at=now()",
        "title='provider-title'",
        "caption='provider-caption'",
        "generated_caption='derived'",
        "link_url='https://example.test/'",
        "tags=ARRAY['provider']",
        "custom_metadata=jsonb_build_object('provider', true)",
        "thumbnail_url='https://example.test/signed'",
    )
    async with uow(past_posts_engine, origin) as conn:
        for mutation in mutations:
            with pytest.raises(IntegrityError, match="ck_media_redacted_projection"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE media_items SET " + mutation + " WHERE id=:id"),
                        {"id": original.media_item_id, "gid": source["generation_id"]},
                    )
        with pytest.raises(ProgrammingError):
            async with conn.begin_nested():
                await conn.execute(
                    text("DELETE FROM media_items WHERE id=:id"),
                    {"id": original.media_item_id},
                )


def test_tombstone_keys_are_keyed_domain_separated_retained_and_not_printed():
    import hashlib
    import hmac
    from src.services.target.instagram_catalog import TombstoneKeys
    from src.services.target.instagram_sources import PastPostsRefused

    ring = keys(2)
    expected = hmac.new(
        b"\x01" * 32,
        b"storydump:instagram-parent:v1\x00instagram_past_posts:media:123",
        hashlib.sha256,
    ).hexdigest()
    assert (
        expected == "3ec4ce02b4208aae6a72a18fa4970696a5c091fc939fb73c06d0fdc83cf8ae3c"
    )
    assert ring.digest(1, "123") == expected
    assert ring.digest(2, "123") != expected
    assert "keys=" not in repr(ring)
    for mapping in ({}, {1: b"short"}, {0: b"x" * 32}):
        with pytest.raises(
            PastPostsRefused, match="past_posts_tombstone_key_unavailable"
        ):
            TombstoneKeys(1, mapping)


async def test_complete_response_without_optional_asset_is_explicitly_incomplete(
    past_posts_engine, origin
):
    source = await enabled(past_posts_engine, origin)
    result = await observe(
        past_posts_engine, origin, source, replace(bundle(), assets=())
    )
    assert result.accepted and result.media_item_id is None
    assert result.reason == "incomplete_observation"
    async with uow(past_posts_engine, origin) as conn:
        assert (await row(conn, "SELECT availability FROM instagram_media_index"))[
            "availability"
        ] == "excluded"


async def test_raw_asset_source_generation_and_item_relationships_are_database_enforced(
    past_posts_engine, origin
):
    from sqlalchemy.exc import IntegrityError

    first = await enabled(past_posts_engine, origin)
    second = await second_origin(past_posts_engine, origin)
    await observe(past_posts_engine, origin, first, bundle("123"))
    second_item = await observe(past_posts_engine, origin, second, bundle("789"))
    async with uow(past_posts_engine, origin) as conn:
        for gid, item_id in (
            (second["generation_id"], None),
            (first["generation_id"], second_item.media_item_id),
        ):
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "INSERT INTO instagram_media_assets (workspace_id,source_id,"
                            "parent_media_id,asset_media_id,data_generation_id,"
                            "observation_revision,position,media_kind,media_item_id) "
                            "VALUES (:ws,:sid,'123','999',:gid,1,2,'image',:item)"
                        ),
                        {
                            "ws": str(origin["ws"]),
                            "sid": first["source_id"],
                            "gid": gid,
                            "item": item_id,
                        },
                    )
                    await conn.execute(
                        text("SET CONSTRAINTS fk_past_posts_asset_item IMMEDIATE")
                    )


async def test_a_rollback_still_redacts_but_landing_an_observation_needs_the_capability(
    past_posts_engine, origin
):
    """Past-posts rows present, no capability. Erasure closes, so it still runs;
    landing a new observation opens, so it is refused by name and writes nothing."""
    from src.services.target.instagram_catalog import (
        redact_generations,
        write_observation,
    )
    from src.services.target.instagram_sources import PastPostsRefused

    source = await enabled(past_posts_engine, origin)
    landed = await observe(past_posts_engine, origin, source, bundle())
    async with uow(past_posts_engine, origin) as conn:
        with pytest.raises(PastPostsRefused, match="past_posts_not_activated"):
            await write_observation(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=source["source_id"],
                generation_id=source["generation_id"],
                captured_epoch=source["source_use_epoch"],
                observation=bundle("789"),
                keys=keys(),
            )
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_media_index"))
        ).scalar_one() == 1
        await conn.execute(
            text(
                "UPDATE instagram_past_posts_generations SET lifecycle = 'deletion_pending' WHERE id = :gid"
            ),
            {"gid": source["generation_id"]},
        )
        erased = await redact_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            keys=keys(),
        )
    assert erased.observations_deleted == erased.items_redacted == 1
    assert erased.media_item_ids == (landed.media_item_id,)
    async with uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_media_index"))
        ).scalar_one() == 0
        item = await row(
            conn,
            "SELECT source_projection_redacted, source_data_generation_id FROM media_items WHERE id=:id",
            id=landed.media_item_id,
        )
        assert item["source_projection_redacted"] is True
        assert item["source_data_generation_id"] is None
