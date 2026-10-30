"""Setwise past-posts progress through the actual ingress/worker tenant roles."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.services.target import instagram_sync, media_validation, source_policy
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_source_read_admission_gate as admission
from tests.scripts import test_source_suitability_gate as suitable

pytestmark = [pytest.mark.integration]
past_posts_engine = foundation.past_posts_engine
origin = foundation.origin


class RecordedExecutor:
    def __init__(self, conn):
        self.conn, self.calls = conn, []

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return await self.conn.execute(statement, params)


async def read(engine, origin, capture, *, interval=86400):
    async with obs.uow(engine, origin) as conn:
        recorded = RecordedExecutor(conn)
        result = await instagram_sync.progress(
            recorded,
            capture=capture,
            destination_id=str(origin["iga"]),
            capability=foundation.capability(),
            expected_progress_interval_seconds=interval,
        )
        return result, recorded.calls


async def seed_10k(conn, origin, old, current):
    binds = dict(
        ws=str(origin["ws"]),
        sid=current["source_id"],
        old=old["generation_id"],
        gid=current["generation_id"],
        recent=datetime.now(timezone.utc) + timedelta(days=1),
        matrix=media_validation.MATRIX_VERSION,
    )
    # Setwise fixture setup, no per-parent writer/query loop. One index-only
    # unsupported group proves coverage does not depend on a media projection.
    await conn.execute(
        text("""
        INSERT INTO media_items(workspace_id,source_id,provider_file_ref,
          content_hash,media_kind,file_name,source_data_generation_id)
        SELECT :ws,:sid,n::text,'instagram_past_posts:media:'||n,'image','fixture.jpg',
          CASE WHEN n<=2000 THEN CAST(:old AS uuid) ELSE CAST(:gid AS uuid) END
        FROM generate_series(1,10000) n WHERE n NOT BETWEEN 8001 AND 9000
    """),
        binds,
    )
    await conn.execute(
        text("""
        INSERT INTO instagram_media_index(workspace_id,source_id,parent_media_id,
          data_generation_id,media_item_id,original_at,attribution,availability,
          exclusion_reason)
        SELECT :ws,:sid,n::text,
          CASE WHEN n<=2000 THEN CAST(:old AS uuid) ELSE CAST(:gid AS uuid) END,
          m.id,CASE WHEN n<=1000 THEN '1900-01-01'::timestamptz
            WHEN n<=2000 THEN '2100-01-01'::timestamptz
            WHEN n BETWEEN 3001 AND 4000 THEN CAST(:recent AS timestamptz)
            WHEN n BETWEEN 8001 AND 9000 THEN '2000-01-01'::timestamptz
            ELSE '2020-01-01'::timestamptz END,
          CASE WHEN n BETWEEN 4001 AND 5000 THEN 'credited'
            WHEN n BETWEEN 5001 AND 6000 THEN 'unknown' ELSE 'not_detected' END,
          CASE WHEN n BETWEEN 7001 AND 8000 THEN 'unavailable'
            WHEN n BETWEEN 8001 AND 9000 THEN 'excluded' ELSE 'available' END,
          CASE WHEN n BETWEEN 8001 AND 9000 THEN 'unsupported_media' END
        FROM generate_series(1,10000) n LEFT JOIN media_items m
          ON m.workspace_id=CAST(:ws AS uuid) AND m.source_id=CAST(:sid AS uuid)
          AND m.provider_file_ref=n::text
    """),
        binds,
    )
    # The asset-level unsupported group carries the current matrix version in
    # its capture, as preparation stamps a refusal: an unstamped one is judged
    # again.
    await conn.execute(
        text("""
        INSERT INTO instagram_media_assets(workspace_id,source_id,parent_media_id,
          asset_media_id,data_generation_id,observation_revision,position,media_kind,
          availability,media_item_id,validation_state,validation_generation_id,
          validation_observation_revision,validation_fingerprint,validated_at,
          validation_capture)
        SELECT i.workspace_id,i.source_id,i.parent_media_id,i.parent_media_id,
          i.data_generation_id,i.observation_revision,1,'image',i.availability,
          i.media_item_id,
          CASE WHEN i.parent_media_id::integer>8000 THEN 'pending'
            WHEN i.parent_media_id::integer BETWEEN 6001 AND 7000 THEN 'unsupported'
            ELSE 'supported' END,
          CASE WHEN i.parent_media_id::integer<=8000 THEN i.data_generation_id END,
          CASE WHEN i.parent_media_id::integer<=8000 THEN i.observation_revision END,
          CASE WHEN i.parent_media_id::integer<=8000 THEN 'fixture' END,
          CASE WHEN i.parent_media_id::integer<=8000 THEN now() END,
          CASE WHEN i.parent_media_id::integer BETWEEN 6001 AND 7000
            THEN jsonb_build_object('generation_id',i.data_generation_id::text,
              'observation_revision',i.observation_revision,
              'selected_asset_id',i.parent_media_id,'state','unsupported',
              'matrix_version',CAST(:matrix AS text)) END
        FROM instagram_media_index i WHERE i.workspace_id=CAST(:ws AS uuid)
          AND i.source_id=CAST(:sid AS uuid) AND i.media_item_id IS NOT NULL
    """),
        binds,
    )

    return binds["recent"]


async def test_10k_progress_is_setwise_current_and_policy_fresh(
    past_posts_engine, origin
):
    old = await admission.ready(past_posts_engine, origin)
    old_capture = await admission.capture(past_posts_engine, origin, old)
    current = await foundation.ensure(
        past_posts_engine, origin, foundation.authorization()
    )
    capture = await admission.capture(past_posts_engine, origin, current)
    empty, empty_calls = await read(past_posts_engine, origin, capture)
    assert empty["observed"] == empty["represented"] == empty["eligible"] == 0
    assert empty["indexed"] == 0 and not empty["ready"]
    assert empty["connected"] and empty["enabled"] and empty["usable"]
    assert empty["indexing"] and empty["partial"] and not empty["stalled"]
    assert not empty["needs_reconnect"]
    async with obs.uow(past_posts_engine, origin) as conn:
        recent = await seed_10k(conn, origin, old, current)
    result, calls = await read(past_posts_engine, origin, capture)
    assert len(calls) == len(empty_calls) < 20, "progress must not query per parent"
    assert result["generation_id"] == current["generation_id"]
    assert result["policy_revision"] == 1
    assert result["observed"] == result["indexed"] == 8000
    assert result["represented"] == 7000 and result["ready"]
    assert result["items"] == 7000 and result["slides"] == 0
    assert result["carousel_children_truncated"] == 0
    assert result["eligible"] == 1000 and result["awaiting_reobservation"] == 2000
    assert result["exclusions"] == {
        "too_recent": 1000,
        "credited_excluded": 1000,
        "attribution_unknown": 1000,
        "unsupported": 2000,
        "unavailable": 1000,
        "validation_pending": 1000,
    }
    assert sum(result["exclusions"].values()) + result["eligible"] == result["observed"]
    assert datetime.fromisoformat(result["coverage_from"]) == datetime(
        2000, 1, 1, tzinfo=timezone.utc
    )
    assert datetime.fromisoformat(result["coverage_to"]) == recent
    assert datetime.fromisoformat(result["as_of"]).tzinfo is not None
    async with obs.uow(past_posts_engine, origin) as conn:
        identities = (
            await conn.execute(
                text(
                    "SELECT array_agg(id ORDER BY id) FROM media_items WHERE source_id=:sid"
                ),
                dict(sid=current["source_id"]),
            )
        ).scalar_one()
        await source_policy.put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=current["source_id"],
            capability=foundation.capability(),
            body=dict(
                schema_version=1,
                expected_revision=1,
                minimum_age_days=90,
                include_credited=True,
                include_unknown=False,
            ),
        )
    revised, _ = await read(past_posts_engine, origin, old_capture)
    assert revised["generation_id"] == current["generation_id"]
    assert revised["policy_revision"] == 2 and revised["eligible"] == 2000
    assert revised["represented"] == 7000 and revised["observed"] == 8000
    assert "credited_excluded" not in revised["exclusions"]
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (
            identities
            == (
                await conn.execute(
                    text(
                        "SELECT array_agg(id ORDER BY id) FROM media_items WHERE source_id=:sid"
                    ),
                    dict(sid=current["source_id"]),
                )
            ).scalar_one()
        )
        plans = []
        for sql, params in calls:
            if "FOR UPDATE" not in sql and "pg_advisory" not in sql:
                plan = (
                    await conn.execute(
                        text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql), params
                    )
                ).scalar_one()
                plans.append(dict(sql=sql, plan=plan))
        assert plans and all(p["plan"][0]["Plan"]["Actual Loops"] == 1 for p in plans)
        # -s in the bounded evidence gate preserves complete actual plans; CI
        # can capture this normally. There is no wall-time/index-choice threshold.
        print(
            "PAST_POSTS_PROGRESS_EXPLAIN="
            + json.dumps(
                dict(
                    empty_query_count=len(empty_calls),
                    query_count=len(calls),
                    plans=plans,
                ),
                default=str,
            )
        )


async def test_progress_counts_posts_items_slides_and_truncated_carousel(
    past_posts_engine, origin
):
    from src.services.target.instagram_catalog import PastPostsAsset

    src = await admission.ready(past_posts_engine, origin)
    capture = await admission.capture(past_posts_engine, origin, src)
    await obs.observe(
        past_posts_engine,
        origin,
        src,
        replace(
            obs.bundle(attribution="not_detected"),
            slide_count=10,
            assets=tuple(
                PastPostsAsset(str(1000 + position), "image", position=position)
                for position in range(1, 11)
            ),
        ),
    )
    await obs.observe(
        past_posts_engine,
        origin,
        src,
        replace(
            obs.bundle(parent="456", attribution="not_detected"),
            slide_count=2,
            slides_complete=False,
            assets=(
                PastPostsAsset("2001", "image", position=1),
                PastPostsAsset("2002", "image", position=2),
            ),
        ),
    )
    result, _ = await read(past_posts_engine, origin, capture)
    assert result["observed"] == result["indexed"] == 2
    assert result["represented"] == 2
    assert result["items"] == result["slides"] == 12
    assert result["carousel_children_truncated"] == 1
    assert result["eligible"] == 0


async def test_status_stalled_anchor_and_off_reconnect_boundaries(
    past_posts_engine, origin, sync_conn
):
    src, _ = await suitable.ready(past_posts_engine, origin)
    capture = await admission.capture(past_posts_engine, origin, src)
    async with obs.uow(past_posts_engine, origin) as conn:
        now = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
        envelope = instagram_sync._envelope(None, capture)
        envelope["recent"]["complete"] = True
        envelope["pending_helpers"] = ["124", "125"]
        envelope["deferred_helpers"] = ["126"]
        envelope["progress"]["recent"] = {
            "last_committed_at": (now - timedelta(days=3)).isoformat()
        }
        await conn.execute(
            text(
                "UPDATE media_sources SET sync_checkpoint=CAST(:cp AS jsonb) WHERE id=:sid"
            ),
            dict(cp=json.dumps(envelope), sid=src["source_id"]),
        )
    fresh_epoch, _ = await read(past_posts_engine, origin, capture)
    assert fresh_epoch["ready"] and fresh_epoch["usable"] and fresh_epoch["partial"]
    assert fresh_epoch["pending_helpers"] == 3
    assert (
        "pending_validation" not in envelope and "pending_validation" not in fresh_epoch
    )
    assert not fresh_epoch["stalled"], "fresh On/reconnect epoch has time to progress"
    # Disposable fixture aging only. Status reads use both real runtime roles;
    # no DB clock is mocked and no production timestamp/audit is modified.
    with sync_conn.cursor() as cur:
        cur.execute(
            "UPDATE media_sources SET created_at=%s WHERE id=%s",
            (now - timedelta(days=3), src["source_id"]),
        )
        cur.execute(
            "UPDATE audit_events SET created_at=%s WHERE entity_kind='instagram_source' AND entity_id=%s",
            (now - timedelta(days=3), src["source_id"]),
        )
    sync_conn.commit()
    stalled, _ = await read(past_posts_engine, origin, capture)
    assert stalled["stalled"] and stalled["enabled"] and stalled["indexing"]
    relaxed, _ = await read(past_posts_engine, origin, capture, interval=2 * 86400)
    assert not relaxed["stalled"], "diagnostic uses two configured intervals"
    async with obs.uow(past_posts_engine, origin) as conn:
        envelope["progress"]["recent"]["last_committed_at"] = now.isoformat()
        envelope["pending_helpers"] = []
        envelope["deferred_helpers"] = []
        envelope["history"] = dict(
            complete=True,
            checkpoint=dict(
                v=2,
                upper=2000000000,
                until=1999000000,
                window_start=1991224000,
                boundary=[],
                seen=10000,
                more=False,
                ceiling_reached=True,
            ),
        )
        await conn.execute(
            text(
                "UPDATE media_sources SET sync_checkpoint=CAST(:cp AS jsonb) WHERE id=:sid"
            ),
            dict(cp=json.dumps(envelope), sid=src["source_id"]),
        )
    capped, _ = await read(past_posts_engine, origin, capture)
    assert capped["partial"] and capped["ready"] and not capped["stalled"]
    assert not capped["indexing"], "capped complete scan retains partial coverage"

    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"),
            dict(sid=src["source_id"]),
        )
    off, _ = await read(past_posts_engine, origin, capture)
    assert off["connected"] and not off["enabled"] and not off["usable"]
    assert not off["ready"] and not off["indexing"] and not off["stalled"]
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE oauth_credentials SET state='revoked' WHERE id=:id"),
            dict(id=str(origin["credential"])),
        )
    reconnect, _ = await read(past_posts_engine, origin, capture)
    assert reconnect["needs_reconnect"] and not reconnect["connected"]
    assert (
        not reconnect["usable"]
        and not reconnect["indexing"]
        and not reconnect["stalled"]
    )
