"""Slide identity and deletion proofs against disposable PostgreSQL."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from src.services.target import (
    past_posts_deletion,
    instagram_catalog,
    instagram_sync,
    intent_ledger,
    source_policy,
    workspaces,
)
from src.services.target.instagram_catalog import PastPostsAsset
from tests.scripts import test_instagram_observations_gate as obs

pytestmark = [pytest.mark.integration]
past_posts_engine = obs.past_posts_engine
origin = obs.origin


def carousel(parent="900", ids=range(100, 110), *, complete=True):
    assets = tuple(
        PastPostsAsset(str(mid), "video" if mid == 105 else "image", position=position)
        for position, mid in enumerate(ids, 1)
    )
    return replace(
        obs.bundle(parent),
        assets=assets,
        slide_count=len(assets),
        slides_complete=complete,
    )


async def assets(engine, chain, parent="900"):
    async with obs.uow(engine, chain) as conn:
        rows = await conn.execute(
            text(
                "SELECT a.asset_media_id,a.position,a.availability,a.exclusion_reason,"
                "a.media_item_id,m.state,m.file_name,m.times_posted,i.slide_count,"
                "i.slides_complete,i.post_ref FROM instagram_media_assets a "
                "JOIN instagram_media_index i ON i.workspace_id=a.workspace_id "
                "AND i.source_id=a.source_id AND i.parent_media_id=a.parent_media_id "
                "LEFT JOIN media_items m ON m.workspace_id=a.workspace_id AND m.id=a.media_item_id "
                "WHERE a.parent_media_id=:parent ORDER BY a.position"
            ),
            {"parent": parent},
        )
        return [dict(row) for row in rows.mappings()]


async def test_deferred_metadata_rewalk_preserves_carousel_identity_and_unknown_count(
    past_posts_engine, origin
):
    source = await obs.enabled(past_posts_engine, origin)
    await obs.observe(past_posts_engine, origin, source, carousel(ids=(100, 101, 102)))
    before = await assets(past_posts_engine, origin)
    ids = {row["asset_media_id"]: row["media_item_id"] for row in before}
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',"
                "validation_generation_id=data_generation_id,"
                "validation_observation_revision=observation_revision,"
                "validation_fingerprint='checked',validated_at=now()"
            )
        )
    deferred = replace(
        obs.bundle("900"),
        assets=(),
        slide_count=0,
        slides_complete=False,
        availability="available",
        exclusion_reason="incomplete_observation",
    )
    await obs.observe(past_posts_engine, origin, source, deferred)
    after = await assets(past_posts_engine, origin)
    assert {row["asset_media_id"]: row["media_item_id"] for row in after} == ids
    assert [row["file_name"] for row in after] == [
        "Instagram post, slide 1",
        "Instagram post, slide 2",
        "Instagram post, slide 3",
    ]
    assert all(row["slide_count"] == 0 and not row["slides_complete"] for row in after)
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM instagram_media_assets WHERE validation_state='pending'"
                )
            )
        ).scalar_one() == 3
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[str(origin["iga"])],
            capability=obs.capability(),
        )
        status = await instagram_sync.status(
            conn,
            context=context,
            source_id=source["source_id"],
            capability=obs.capability(),
        )
        assert (status["slides"], status["carousel_children_truncated"]) == (3, 1)
        for item_id in ids.values():
            media = await workspaces.get_media(
                conn, workspace_id=str(origin["ws"]), media_id=str(item_id)
            )
            assert media["provenance"]["slide"]["count"] is None
            assert media["provenance"]["slide"]["position"] in (1, 2, 3)


async def test_selected_asset_retirement_cas_preserves_siblings_and_restores_identity(
    past_posts_engine, origin
):
    source = await obs.enabled(past_posts_engine, origin)
    await obs.observe(past_posts_engine, origin, source, carousel(ids=(100, 101, 102)))
    before = await assets(past_posts_engine, origin)
    ids = {row["asset_media_id"]: row["media_item_id"] for row in before}
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE media_items SET times_posted=2 WHERE id=:item"),
            {"item": ids["100"]},
        )
        await conn.execute(
            text(
                "INSERT INTO post_locks (workspace_id,media_item_id,kind) VALUES (:ws,:item,'hold')"
            ),
            {"ws": str(origin["ws"]), "item": ids["100"]},
        )

    async def retire(revision, item):
        async with obs.uow(past_posts_engine, origin) as conn:
            return await instagram_catalog.retire_selected_asset(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=source["source_id"],
                generation_id=source["generation_id"],
                observation_revision=revision,
                asset_media_id="100",
                media_item_id=str(item),
                keys=obs.keys(),
            )

    assert not await retire(2, ids["100"])
    assert not await retire(1, ids["101"])
    assert len(await assets(past_posts_engine, origin)) == 3
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_item_tombstones"))
        ).scalar_one() == 0
    assert await retire(1, ids["100"])
    assert not await retire(1, ids["100"])
    after = await assets(past_posts_engine, origin)
    assert [row["asset_media_id"] for row in after] == ["101", "102"]
    assert all(row["state"] == "available" for row in after)
    assert all(not row["slides_complete"] for row in after)
    async with obs.uow(past_posts_engine, origin) as conn:
        removed = await obs.row(
            conn,
            "SELECT state,times_posted FROM media_items WHERE id=:item",
            item=ids["100"],
        )
        assert (removed["state"], removed["times_posted"]) == ("removed", 2)
        assert (
            await conn.execute(
                text("SELECT count(*) FROM post_locks WHERE media_item_id=:item"),
                {"item": ids["100"]},
            )
        ).scalar_one() == 1
        tomb = await obs.row(
            conn,
            "SELECT media_item_id,observation_revision FROM instagram_item_tombstones WHERE source_id=:sid",
            sid=source["source_id"],
        )
        assert str(tomb["media_item_id"]) == str(ids["100"])
        assert tomb["observation_revision"] == 1
    await obs.redact(past_posts_engine, origin, source)
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_media_assets"))
        ).scalar_one() == 0
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM instagram_item_tombstones WHERE media_item_id=:item"
                ),
                {"item": ids["100"]},
            )
        ).scalar_one() == 1
    next_generation = await obs.ensure(past_posts_engine, origin, obs.authorization())
    await obs.observe(
        past_posts_engine, origin, next_generation, carousel(ids=(100, 101, 102))
    )
    restored = await assets(past_posts_engine, origin)
    assert {row["asset_media_id"]: row["media_item_id"] for row in restored} == ids
    assert all(row["state"] == "available" for row in restored)


async def test_ten_slides_reorder_truncation_and_removal_keep_independent_history(
    past_posts_engine, origin
):
    source = await obs.enabled(past_posts_engine, origin)
    first = await obs.observe(past_posts_engine, origin, source, carousel())
    assert first.observation_revision == 1
    initial = await assets(past_posts_engine, origin)
    assert len(initial) == 10
    ids = {row["asset_media_id"]: row["media_item_id"] for row in initial}
    assert len(set(ids.values())) == 10
    assert [row["position"] for row in initial] == list(range(1, 11))
    assert all(row["slide_count"] == 10 and row["slides_complete"] for row in initial)
    assert len({row["post_ref"] for row in initial}) == 1
    assert initial[5]["file_name"] == "Instagram post, slide 6 of 10"
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM instagram_media_index WHERE parent_media_id='900'"
                )
            )
        ).scalar_one() == 1
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[str(origin["iga"])],
            capability=obs.capability(),
        )
        status = await instagram_sync.status(
            conn,
            context=context,
            source_id=source["source_id"],
            capability=obs.capability(),
        )
        assert (
            status["observed"],
            status["represented"],
            status["items"],
            status["slides"],
            status["carousel_children_truncated"],
        ) == (1, 1, 10, 10, 0)
        listed = await workspaces.list_sources(conn, workspace_id=str(origin["ws"]))
        coverage = next(
            row["coverage"] for row in listed if str(row["id"]) == source["source_id"]
        )
        assert (
            coverage["observed"],
            coverage["represented"],
            coverage["items"],
            coverage["slides"],
            coverage["carousel_children_truncated"],
        ) == (1, 1, 10, 10, 0)

    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE media_items SET times_posted=3 WHERE id=:item"),
            {"item": ids["105"]},
        )
        await conn.execute(
            text(
                "INSERT INTO post_locks (workspace_id,media_item_id,kind) VALUES (:ws,:item,'hold')"
            ),
            {"ws": str(origin["ws"]), "item": ids["105"]},
        )

    reordered = await obs.observe(
        past_posts_engine,
        origin,
        source,
        carousel(ids=(109, 108, 107, 106, 105, 104, 103, 102, 101, 100)),
    )
    assert reordered.observation_revision == 2
    after_reorder = await assets(past_posts_engine, origin)
    assert {row["asset_media_id"]: row["media_item_id"] for row in after_reorder} == ids
    assert after_reorder[0]["asset_media_id"] == "109"
    assert after_reorder[4]["times_posted"] == 3
    assert after_reorder[4]["file_name"] == "Instagram post, slide 5 of 10"

    partial = await obs.observe(
        past_posts_engine, origin, source, carousel(ids=(109, 108, 107), complete=False)
    )
    assert partial.observation_revision == 3
    after_partial = await assets(past_posts_engine, origin)
    assert len(after_partial) == 10
    assert [row["position"] for row in after_partial] == list(range(1, 11))
    assert {row["asset_media_id"]: row["media_item_id"] for row in after_partial} == ids
    assert all(not row["slides_complete"] for row in after_partial)
    assert after_partial[0]["file_name"] == "Instagram post, slide 1"
    async with obs.uow(past_posts_engine, origin) as conn:
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[str(origin["iga"])],
            capability=obs.capability(),
        )
        status = await instagram_sync.status(
            conn,
            context=context,
            source_id=source["source_id"],
            capability=obs.capability(),
        )
        assert (
            status["observed"],
            status["items"],
            status["slides"],
            status["carousel_children_truncated"],
        ) == (1, 10, 10, 1)

    complete = await obs.observe(
        past_posts_engine,
        origin,
        source,
        carousel(ids=(109, 108, 107, 106, 104, 103, 102, 101, 100)),
    )
    assert complete.observation_revision == 4
    after_removal = await assets(past_posts_engine, origin)
    assert len(after_removal) == 9
    assert {row["asset_media_id"]: row["media_item_id"] for row in after_removal} == {
        mid: item for mid, item in ids.items() if mid != "105"
    }
    async with obs.uow(past_posts_engine, origin) as conn:
        removed = await obs.row(
            conn,
            "SELECT state,times_posted FROM media_items WHERE id=:item",
            item=ids["105"],
        )
        assert removed["state"] == "removed" and removed["times_posted"] == 3
        assert (
            await conn.execute(
                text("SELECT count(*) FROM post_locks WHERE media_item_id=:item"),
                {"item": ids["105"]},
            )
        ).scalar_one() == 1
        tomb = await obs.row(
            conn,
            "SELECT observation_revision FROM instagram_item_tombstones WHERE media_item_id=:item",
            item=ids["105"],
        )
        assert tomb["observation_revision"] >= 4


async def test_cross_origin_slide_conflict_excludes_only_the_conflicting_asset(
    past_posts_engine, origin
):
    first = await obs.enabled(past_posts_engine, origin)
    other = await obs.second_origin(past_posts_engine, origin)
    own = await obs.observe(past_posts_engine, origin, first, carousel(ids=(100,)))
    assert own.media_item_id
    landed = await obs.observe(
        past_posts_engine, origin, other, carousel("901", ids=(100, 101))
    )
    assert landed.accepted
    found = await assets(past_posts_engine, origin, "901")
    assert found[0]["asset_media_id"] == "100"
    assert found[0]["availability"] == "excluded"
    assert found[0]["exclusion_reason"] == "origin_identity_conflict"
    assert found[0]["media_item_id"] is None
    assert found[1]["media_item_id"] is not None


async def test_unsupported_slide_keeps_history_then_complete_removal_retires_it(
    past_posts_engine, origin
):
    source = await obs.enabled(past_posts_engine, origin)
    await obs.observe(past_posts_engine, origin, source, carousel(ids=(100, 101)))
    initial = {
        row["asset_media_id"]: row["media_item_id"]
        for row in await assets(past_posts_engine, origin)
    }
    unsupported = replace(
        carousel(ids=(100, 101)),
        assets=(
            PastPostsAsset(
                "100",
                "unknown",
                position=1,
                availability="excluded",
                exclusion_reason="unsupported_media",
            ),
            PastPostsAsset("101", "image", position=2),
        ),
    )
    await obs.observe(past_posts_engine, origin, source, unsupported)
    after = await assets(past_posts_engine, origin)
    assert after[0]["media_item_id"] is None
    async with obs.uow(past_posts_engine, origin) as conn:
        item = await obs.row(
            conn, "SELECT state FROM media_items WHERE id=:item", item=initial["100"]
        )
        assert item["state"] == "unsupported"
    await obs.observe(past_posts_engine, origin, source, carousel(ids=(101,)))
    async with obs.uow(past_posts_engine, origin) as conn:
        item = await obs.row(
            conn, "SELECT state FROM media_items WHERE id=:item", item=initial["100"]
        )
        assert item["state"] == "removed"
        tomb = await obs.row(
            conn,
            "SELECT observation_revision FROM instagram_item_tombstones WHERE media_item_id=:item",
            item=initial["100"],
        )
        assert tomb["observation_revision"] >= 3


async def test_landing_statement_count_does_not_grow_with_slide_count(
    past_posts_engine, origin
):
    source = await obs.enabled(past_posts_engine, origin)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(past_posts_engine.sync_engine, "before_cursor_execute", record)
    try:
        await obs.observe(past_posts_engine, origin, source, obs.bundle("800"))
        single = len(statements)
        statements.clear()
        await obs.observe(past_posts_engine, origin, source, carousel("900"))
        ten = len(statements)
    finally:
        event.remove(past_posts_engine.sync_engine, "before_cursor_execute", record)
    assert ten <= single + 1, (single, ten)


async def test_terminal_parent_read_retires_every_known_slide_and_keeps_empty_post(
    past_posts_engine, origin
):
    source = await obs.enabled(past_posts_engine, origin)
    await obs.observe(past_posts_engine, origin, source, carousel())
    await obs.observe(
        past_posts_engine,
        origin,
        source,
        replace(
            obs.bundle("901"),
            assets=(),
            availability="available",
            exclusion_reason="incomplete_observation",
        ),
    )
    capture = SimpleNamespace(
        workspace_id=str(origin["ws"]), source_id=source["source_id"]
    )
    for parent in ("900", "901"):
        async with obs.uow(past_posts_engine, origin) as conn:
            index = await obs.row(
                conn,
                "SELECT observation_revision FROM instagram_media_index WHERE parent_media_id=:parent",
                parent=parent,
            )
            candidate = instagram_sync._ParentCapture(
                parent, source["generation_id"], index["observation_revision"]
            )
            assert await instagram_sync._capture_helper_terminal(
                conn, capture, candidate
            )
    current = await assets(past_posts_engine, origin)
    assert len(current) == 10
    assert all(
        row["availability"] == "unavailable"
        and row["exclusion_reason"] == "asset_unavailable"
        for row in current
    )
    assert all(row["state"] == "removed" for row in current)
    async with obs.uow(past_posts_engine, origin) as conn:
        empty = await obs.row(
            conn,
            "SELECT availability,exclusion_reason FROM instagram_media_index WHERE parent_media_id='901'",
        )
        assert empty["availability"] == "unavailable"
        assert empty["exclusion_reason"] == "asset_unavailable"
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM instagram_media_assets WHERE parent_media_id='901'"
                )
            )
        ).scalar_one() == 0


async def test_late_generation_erasure_and_independent_restore_keep_each_slide_uuid(
    past_posts_engine, origin
):
    g1 = await obs.enabled(past_posts_engine, origin)
    await obs.observe(past_posts_engine, origin, g1, carousel())
    original = {
        row["asset_media_id"]: row["media_item_id"]
        for row in await assets(past_posts_engine, origin)
    }
    await obs.observe(past_posts_engine, origin, g1, carousel(ids=range(100, 109)))
    g2 = await obs.ensure(past_posts_engine, origin, obs.authorization())
    second = await obs.observe(
        past_posts_engine, origin, g2, carousel(ids=range(100, 109))
    )
    assert second.observation_revision > 2
    old = await obs.redact(past_posts_engine, origin, g1)
    assert old.items_redacted == 1
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM instagram_media_assets WHERE data_generation_id=:gid"
                ),
                {"gid": g1["generation_id"]},
            )
        ).scalar_one() == 0
        gone = await obs.row(
            conn,
            "SELECT source_projection_redacted FROM media_items WHERE id=:item",
            item=original["109"],
        )
        assert gone["source_projection_redacted"]
        remaining = await obs.row(
            conn,
            past_posts_deletion._REMAINING,
            **past_posts_deletion._params(
                str(origin["ws"]), g2["source_id"], {g2["generation_id"]}
            ),
            terminal=list(intent_ledger.TERMINAL_STATES),
        )
        assert remaining["assets"] == 9
    await obs.redact(past_posts_engine, origin, g2)
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM instagram_media_assets"))
        ).scalar_one() == 0
        remaining = await obs.row(
            conn,
            past_posts_deletion._REMAINING,
            **past_posts_deletion._params(
                str(origin["ws"]), g2["source_id"], {g2["generation_id"]}
            ),
            terminal=list(intent_ledger.TERMINAL_STATES),
        )
        assert remaining["assets"] == 0
        # A removed slide can carry the highest retained revision even though
        # its asset row is gone. The next parent revision must clear it.
        await conn.execute(
            text(
                "UPDATE instagram_item_tombstones SET observation_revision=41 "
                "WHERE media_item_id=:item"
            ),
            {"item": original["109"]},
        )
    g3 = await obs.ensure(past_posts_engine, origin, obs.authorization())
    restored = await obs.observe(
        past_posts_engine, origin, g3, carousel(ids=range(100, 109))
    )
    assert restored.observation_revision > 41
    current = {
        row["asset_media_id"]: row["media_item_id"]
        for row in await assets(past_posts_engine, origin)
    }
    assert current == {mid: item for mid, item in original.items() if mid != "109"}


def test_runtime_sql_uses_asset_rows_for_file_facts():
    runtime = (
        Path(instagram_catalog.__file__).read_text()
        + Path(instagram_sync.__file__).read_text()
    )
    assert "first_child_id" not in runtime
    assert "cache_generation_id" not in runtime
    assert "cached_eligible" not in runtime
    assert "i.media_item_id" not in runtime
    assert "i.validation_state" not in runtime
    assert "UPDATE instagram_media_index SET validation_" not in runtime
    assert "instagram_media_assets" in workspaces.PAST_POSTS_COVERAGE_SQL
    assert "i.media_item_id" not in workspaces.PAST_POSTS_COVERAGE_SQL
