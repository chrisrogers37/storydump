"""Candidate/strict selection uses one relation under actual tenant runtime roles."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import pytest
from sqlalchemy import text
from src.services.target import category_mix, media_validation, source_policy
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts.test_source_suitability_gate import activate

past_posts_engine = foundation.past_posts_engine
origin = foundation.origin
pytestmark = pytest.mark.integration


async def set_drive_ratio(conn, chain, ratio):
    await conn.execute(
        text(
            "INSERT INTO category_post_case_mix(workspace_id,source_id,category,ratio) "
            "VALUES(:ws,:sid,'synthetic',:ratio) "
            "ON CONFLICT (workspace_id,source_id) WHERE effective_to IS NULL "
            "AND source_id IS NOT NULL DO UPDATE SET ratio=EXCLUDED.ratio"
        ),
        {"ws": str(chain["ws"]), "sid": str(chain["src"]), "ratio": ratio},
    )


async def metadata(
    engine,
    chain,
    *,
    kind="image",
    attribution="not_detected",
    recent=False,
    carousel=False,
):
    """One indexed post whose item is asset 123: a single post, or with
    `carousel` the first of two slides (123 and 456) and a slide count of 2."""
    from src.services.target.instagram_catalog import PastPostsAsset

    src = await obs.enabled(engine, chain)
    bundle = replace(
        obs.bundle(attribution=attribution),
        assets=(
            PastPostsAsset("123", kind, position=1),
            PastPostsAsset("456", kind, position=2),
        )
        if carousel
        else (PastPostsAsset("123", kind),),
        slide_count=2 if carousel else None,
        original_at=datetime.now(timezone.utc)
        if recent
        else datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    item = await obs.observe(engine, chain, src, bundle)
    async with obs.uow(engine, chain) as conn:
        await activate(conn, chain["ws"])
        await set_drive_ratio(conn, chain, 0)
    return src, item


async def carousel(engine, chain, count=10):
    from src.services.target.instagram_catalog import PastPostsAsset

    src = await obs.enabled(engine, chain)
    await obs.observe(
        engine,
        chain,
        src,
        replace(
            obs.bundle(attribution="not_detected"),
            slide_count=count,
            assets=tuple(
                PastPostsAsset(str(1000 + position), "image", position=position)
                for position in range(1, count + 1)
            ),
        ),
    )
    async with obs.uow(engine, chain) as conn:
        await activate(conn, chain["ws"])
        await set_drive_ratio(conn, chain, 0)
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT asset_media_id,position,media_item_id FROM instagram_media_assets WHERE source_id=:sid ORDER BY position"
                    ),
                    {"sid": src["source_id"]},
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == count and all(row["media_item_id"] for row in rows)
    return src, [dict(row) for row in rows]


async def verdict(conn, chain, src, *, strict=False, intent=None, destination=None):
    result = await source_policy.suitability(
        conn,
        workspace_id=str(chain["ws"]),
        ig_account_id=str(destination or chain["iga"]),
        source_id=src["source_id"],
        existing_intent_id=intent,
        require_validation=strict,
        capability=foundation.capability(),
    )
    return result[0]


async def slide_verdicts(conn, chain, src, *, strict=False):
    return await source_policy.suitability(
        conn,
        workspace_id=str(chain["ws"]),
        ig_account_id=str(chain["iga"]),
        source_id=src["source_id"],
        require_validation=strict,
        capability=foundation.capability(),
    )


async def test_ten_slide_candidates_strict_proof_and_first_policy(
    past_posts_engine, origin
):
    from src.services.target import content_runway

    src, slides = await carousel(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        candidates = await slide_verdicts(conn, origin, src)
        strict = await slide_verdicts(conn, origin, src, strict=True)
        assert len(candidates) == len(strict) == 10
        assert sum(v["suitable"] for v in candidates) == 10
        pool = await category_mix.pool(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            capability=foundation.capability(),
        )
        assert pool.counts[src["source_id"]] == 10
        runway = await content_runway.runway(
            conn, workspace_id=str(origin["ws"]), below_days=7
        )
        assert (
            next(a for a in runway["accounts"] if a["id"] == str(origin["iga"]))[
                "candidates"
            ]
            == 10
        )
        assert sum(v["suitable"] for v in strict) == 0
        assert {v["reason"] for v in strict} == {"validation_pending"}
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='first-only',validated_at=now() WHERE asset_media_id=:asset"
            ),
            {"asset": slides[0]["asset_media_id"]},
        )
        strict = await slide_verdicts(conn, origin, src, strict=True)
        assert len(strict) == 10 and sum(v["suitable"] for v in strict) == 1
        assert {str(v["media_item_id"]) for v in strict if v["suitable"]} == {
            str(slides[0]["media_item_id"])
        }
        await source_policy.put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            body={
                "schema_version": 2,
                "expected_revision": 1,
                "minimum_age_days": 90,
                "include_credited": False,
                "include_unknown": False,
                "carousel_slides": "first",
            },
            capability=foundation.capability(),
        )
        candidates = await slide_verdicts(conn, origin, src)
        assert len(candidates) == 10
        assert sum(v["suitable"] for v in candidates) == 1
        assert [v["reason"] for v in candidates].count("slide_not_first") == 9
        by_id = await source_policy.suitability(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            media_item_id=str(slides[1]["media_item_id"]),
            require_validation=False,
            capability=foundation.capability(),
        )
        assert len(by_id) == 1
        assert str(by_id[0]["media_item_id"]) == str(slides[1]["media_item_id"])
        assert by_id[0]["reason"] == "slide_not_first"
        pool = await category_mix.pool(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            capability=foundation.capability(),
        )
        assert pool.counts[src["source_id"]] == 1
        from src.services.target import versioned_mix

        view = await versioned_mix.resource(conn, workspace_id=str(origin["ws"]))
        destination = next(
            d for d in view["destinations"] if d["account_id"] == str(origin["iga"])
        )
        assert destination["candidates"] == destination["eligible"] == 1
        assert {str(v["media_item_id"]) for v in candidates if v["suitable"]} == {
            str(slides[0]["media_item_id"])
        }
        strict = await slide_verdicts(conn, origin, src, strict=True)
        assert sum(v["suitable"] for v in strict) == 1
        assert [v["reason"] for v in strict].count("slide_not_first") == 9
        rows = (
            await conn.execute(
                text(
                    "SELECT asset_media_id,media_item_id FROM instagram_media_assets ORDER BY position"
                )
            )
        ).all()
        assert [(r[0], r[1]) for r in rows] == [
            (slide["asset_media_id"], slide["media_item_id"]) for slide in slides
        ]
        await conn.execute(
            text(
                "UPDATE media_items SET times_posted=3,last_posted_at=now() WHERE id=:item"
            ),
            {"item": str(slides[1]["media_item_id"])},
        )
        await conn.execute(
            text(
                "INSERT INTO post_locks(workspace_id,media_item_id,ig_account_id,kind,expires_at) VALUES(:ws,:item,:account,'recent',now()+interval '1 day')"
            ),
            {
                "ws": str(origin["ws"]),
                "item": str(slides[1]["media_item_id"]),
                "account": str(origin["iga"]),
            },
        )
        await source_policy.put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            body=dict(
                schema_version=2,
                expected_revision=2,
                minimum_age_days=90,
                include_credited=False,
                include_unknown=False,
                carousel_slides="all",
            ),
            capability=foundation.capability(),
        )
        candidates = await slide_verdicts(conn, origin, src)
        assert len(candidates) == 10 and sum(v["suitable"] for v in candidates) == 9
        assert not any(v["reason"] == "slide_not_first" for v in candidates)
        locked = next(
            v
            for v in candidates
            if str(v["media_item_id"]) == str(slides[1]["media_item_id"])
        )
        assert locked["reason"] == "locked" and locked["warning_locks"] == ["recent"]
        assert (
            await conn.execute(
                text("SELECT times_posted FROM media_items WHERE id=:item"),
                {"item": str(slides[1]["media_item_id"])},
            )
        ).scalar_one() == 3
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM post_locks WHERE media_item_id=:item AND kind='recent'"
                ),
                {"item": str(slides[1]["media_item_id"])},
            )
        ).scalar_one() == 1
        rows = (
            await conn.execute(
                text(
                    "SELECT asset_media_id,media_item_id FROM instagram_media_assets ORDER BY position"
                )
            )
        ).all()
        assert [(r[0], r[1]) for r in rows] == [
            (slide["asset_media_id"], slide["media_item_id"]) for slide in slides
        ]


async def test_per_slide_lock_leaves_carousel_siblings_eligible(
    past_posts_engine, origin
):
    src, slides = await carousel(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "INSERT INTO post_locks(workspace_id,media_item_id,kind,expires_at) VALUES(:ws,:item,'hold',now()+interval '1 day')"
            ),
            {"ws": str(origin["ws"]), "item": str(slides[0]["media_item_id"])},
        )
        candidates = await slide_verdicts(conn, origin, src)
        assert len(candidates) == 10 and sum(v["suitable"] for v in candidates) == 9
        locked = [
            v
            for v in candidates
            if str(v["media_item_id"]) == str(slides[0]["media_item_id"])
        ]
        assert len(locked) == 1 and locked[0]["reason"] == "locked"
        assert all(v["suitable"] for v in candidates if v not in locked)


@pytest.mark.parametrize("kind", ["image", "video"])
async def test_unvalidated_metadata_can_be_candidate_but_never_strict_ready(
    past_posts_engine, origin, kind
):
    src, item = await metadata(past_posts_engine, origin, kind=kind)
    async with obs.uow(past_posts_engine, origin) as conn:
        candidate = await verdict(conn, origin, src)
        strict = await verdict(conn, origin, src, strict=True)
        assert candidate["suitable"] and candidate["reason"] is None
        assert not strict["suitable"] and strict["reason"] == "validation_pending"
        default = await source_policy.suitability(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            source_id=src["source_id"],
            capability=foundation.capability(),
        )
        assert default == [strict]


@pytest.mark.parametrize(
    "recent,attribution,reason",
    [
        (True, "not_detected", "too_recent"),
        (False, "credited", "credited_excluded"),
        (False, "unknown", "attribution_unknown"),
    ],
)
async def test_missing_byte_proof_cannot_hide_later_policy_refusal(
    past_posts_engine, origin, recent, attribution, reason
):
    src, item = await metadata(
        past_posts_engine, origin, recent=recent, attribution=attribution
    )
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (await verdict(conn, origin, src))["reason"] == reason
        assert (await verdict(conn, origin, src, strict=True))[
            "reason"
        ] == "validation_pending"


@pytest.mark.parametrize("reason", ["helper_unavailable", "asset_unavailable"])
async def test_current_captured_failure_blocks_candidates_but_reobservation_clears_it(
    past_posts_engine, origin, reason
):
    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        capture = {
            "generation_id": src["generation_id"],
            "observation_revision": 1,
            "selected_asset_id": "123",
            "state": "validation_pending",
            "reason": reason,
        }
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_capture=CAST(:capture AS jsonb)"
            ),
            {"capture": json.dumps(capture)},
        )
        assert (await verdict(conn, origin, src))["reason"] == reason
    await obs.observe(
        past_posts_engine, origin, src, obs.bundle(attribution="not_detected")
    )
    async with obs.uow(past_posts_engine, origin) as conn:
        assert (await verdict(conn, origin, src))["suitable"]


async def test_candidate_self_context_does_not_hide_other_live_subject(
    past_posts_engine, origin
):
    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        intent = str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,provider_account_ref,approval_mode,schedule_slot_at) VALUES(:ws,:acct,:item,'synthetic','manual',now()) RETURNING id"
                    ),
                    {
                        "ws": str(origin["ws"]),
                        "acct": str(origin["iga"]),
                        "item": item.media_item_id,
                    },
                )
            ).scalar_one()
        )
        assert (await verdict(conn, origin, src))["reason"] == "live_intent"
        assert (await verdict(conn, origin, src, intent=intent))["suitable"]
        assert (await verdict(conn, origin, src, intent=str(origin["intent"])))[
            "reason"
        ] == "live_intent"


async def test_10k_past_posts_slides_and_one_drive_file_have_equal_targets_and_role_query_plan(
    past_posts_engine, origin
):
    src = await obs.enabled(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await activate(conn, origin["ws"])
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio=.5 WHERE source_id=:sid"),
            {"sid": src["source_id"]},
        )
        await set_drive_ratio(conn, origin, Decimal(".5"))
        await conn.execute(
            text(
                "INSERT INTO instagram_media_index(workspace_id,source_id,parent_media_id,data_generation_id,original_at,attribution,slide_count) SELECT :ws,:sid,(100000+n)::text,:gid,'2020-01-01','not_detected',20 FROM generate_series(1,500) n"
            ),
            {
                "ws": str(origin["ws"]),
                "sid": src["source_id"],
                "gid": src["generation_id"],
            },
        )
        await conn.execute(
            text(
                "INSERT INTO media_items(workspace_id,source_id,source_data_generation_id,content_hash,provider_file_ref,file_name,media_kind) SELECT :ws,:sid,:gid,'synthetic-slide-'||n,(200000+n)::text,'x.jpg','image' FROM generate_series(1,10000) n"
            ),
            {
                "ws": str(origin["ws"]),
                "sid": src["source_id"],
                "gid": src["generation_id"],
            },
        )
        await conn.execute(
            text(
                "INSERT INTO instagram_media_assets(workspace_id,source_id,parent_media_id,asset_media_id,data_generation_id,observation_revision,position,media_kind,media_item_id) SELECT i.workspace_id,i.source_id,i.parent_media_id,m.provider_file_ref,i.data_generation_id,i.observation_revision,((m.provider_file_ref::integer-200001)%20)+1,'image',m.id FROM media_items m JOIN instagram_media_index i ON i.workspace_id=m.workspace_id AND i.source_id=m.source_id AND i.parent_media_id=(100001+(m.provider_file_ref::integer-200001)/20)::text WHERE m.workspace_id=CAST(:ws AS uuid) AND m.source_id=CAST(:sid AS uuid)"
            ),
            {"ws": str(origin["ws"]), "sid": src["source_id"]},
        )
        await conn.execute(
            text(
                "INSERT INTO media_items(workspace_id,source_id,content_hash,provider_file_ref,file_name,media_kind) VALUES(:ws,:sid,'drive-one','drive-one','d.jpg','image')"
            ),
            {"ws": str(origin["ws"]), "sid": str(origin["src"])},
        )
        pool = await category_mix.pool(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            capability=foundation.capability(),
        )
        assert pool.weights == {
            str(origin["src"]): Decimal(".5"),
            src["source_id"]: Decimal(".5"),
        }
        assert pool.counts == {str(origin["src"]): 1, src["source_id"]: 10000}
        assert pool.candidates == 10001 and pool.eligible == 1
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[origin["iga"]],
            capability=foundation.capability(),
        )
        plan = (
            await conn.execute(
                text(
                    "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT count(*) FROM ("
                    + source_policy.CANDIDATE_SQL
                    + ") v WHERE suitable"
                ),
                dict(
                    context.binds(),
                    acct=str(origin["iga"]),
                    sid=None,
                    media=None,
                    intent=None,
                ),
            )
        ).scalar_one()
        print(
            "CANDIDATE_SELECTION_EXPLAIN "
            + json.dumps(
                {
                    "role": (
                        await conn.execute(text("SELECT current_user"))
                    ).scalar_one(),
                    "rows": pool.candidates,
                    "plan": plan,
                },
                default=str,
            )
        )


async def selected_intent(conn, chain, item, *, state="scheduled", origin="cadence"):
    intent = str(
        (
            await conn.execute(
                text(
                    "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,provider_account_ref,approval_mode,schedule_slot_at,state,origin,scheduled_by_user_id) VALUES(:ws,:acct,:item,'synthetic','manual',now(),:state,:origin,:by) RETURNING id"
                ),
                {
                    "ws": str(chain["ws"]),
                    "acct": str(chain["iga"]),
                    "item": item.media_item_id,
                    "state": "scheduled",
                    "origin": origin,
                    "by": str(chain["user"]) if origin == "planned" else None,
                },
            )
        ).scalar_one()
    )
    if state != "scheduled":
        from src.services.target import intent_ledger

        await intent_ledger.transition(conn, intent, "prompt_pending")
        if state == "awaiting_approval":
            await intent_ledger.transition(conn, intent, state)
    return intent


async def test_actual_plan_slot_selects_metadata_candidate(
    past_posts_engine, origin, monkeypatch
):
    from src.services.target import scheduler, content_runway
    from unittest.mock import AsyncMock
    import random

    src, item = await metadata(past_posts_engine, origin)
    monkeypatch.setattr(content_runway, "after_mint", AsyncMock())
    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        outcome = await scheduler.execute_plan_slot(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            slot_at=datetime.now(timezone.utc),
            provider_account_ref="synthetic",
            no_media_notice_after_seconds=86400,
            low_runway_days=7,
            rng=random.Random(1),
        )
        assert outcome.intent_id
        assert (
            str(
                (
                    await conn.execute(
                        text("SELECT media_item_id FROM post_intents WHERE id=:id"),
                        {"id": outcome.intent_id},
                    )
                ).scalar_one()
            )
            == item.media_item_id
        )


async def test_consecutive_slots_select_distinct_siblings_of_one_carousel(
    past_posts_engine, origin, monkeypatch
):
    from src.services.target import scheduler, content_runway
    from unittest.mock import AsyncMock
    import random

    src, slides = await carousel(past_posts_engine, origin)
    monkeypatch.setattr(content_runway, "after_mint", AsyncMock())
    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        chosen = []
        for minute in (0, 1):
            outcome = await scheduler.execute_plan_slot(
                conn,
                workspace_id=str(origin["ws"]),
                ig_account_id=str(origin["iga"]),
                slot_at=datetime.now(timezone.utc) + timedelta(minutes=minute),
                provider_account_ref="synthetic",
                no_media_notice_after_seconds=86400,
                low_runway_days=7,
                rng=random.Random(minute),
            )
            assert outcome.intent_id
            chosen.append(
                str(
                    (
                        await conn.execute(
                            text("SELECT media_item_id FROM post_intents WHERE id=:id"),
                            {"id": outcome.intent_id},
                        )
                    ).scalar_one()
                )
            )
        assert chosen[0] != chosen[1]
        assert set(chosen) <= {str(slide["media_item_id"]) for slide in slides}


async def test_automatic_approval_waits_for_byte_proof_then_approves_same_intent(
    past_posts_engine, origin
):
    from src.services.target import auto_approval

    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        await conn.execute(
            text(
                "UPDATE workspaces SET dry_run_mode=true,api_publishing_enabled=true,approval_mode='auto' WHERE id=:ws"
            ),
            {"ws": str(origin["ws"])},
        )
        intent = await selected_intent(conn, origin, item, state="awaiting_approval")
        assert (
            await auto_approval.approve_if_due(
                conn, workspace_id=str(origin["ws"]), intent_id=intent
            )
            is None
        )
        assert (
            await conn.execute(
                text("SELECT state FROM post_intents WHERE id=:id"), {"id": intent}
            )
        ).scalar_one() == "awaiting_approval"
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE kind='publish_pipeline' AND payload->>'intent_id'=:id"
                ),
                {"id": intent},
            )
        ).scalar_one() == 0
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='synthetic',validated_at=now()"
            )
        )
        assert (
            await auto_approval.approve_if_due(
                conn, workspace_id=str(origin["ws"]), intent_id=intent
            )
            == "auto"
        )
        assert (
            await conn.execute(
                text("SELECT state FROM post_intents WHERE id=:id"), {"id": intent}
            )
        ).scalar_one() == "approved"


async def test_prompt_sweep_defers_unverified_selected_intent(
    past_posts_engine, origin
):
    from src.services.target import prompts

    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        intent = await selected_intent(conn, origin, item)
        if (
            await conn.execute(text("SELECT current_user"))
        ).scalar_one() == "svc_ingress":
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(
                DBAPIError, match="permission denied for function fn_prompts_due"
            ):
                await prompts.sweep_due_prompts(
                    conn, late_seconds=3600, limit=100, workspace_id=str(origin["ws"])
                )
            return
        counts = await prompts.sweep_due_prompts(
            conn, late_seconds=3600, limit=100, workspace_id=str(origin["ws"])
        )
        assert (
            await conn.execute(
                text("SELECT state FROM post_intents WHERE id=:id"), {"id": intent}
            )
        ).scalar_one() == "scheduled"
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='synthetic',validated_at=now()"
            )
        )
        counts = await prompts.sweep_due_prompts(
            conn, late_seconds=3600, limit=100, workspace_id=str(origin["ws"])
        )
        assert counts["prompted"] >= 1 and counts["advanced"] >= 1
        assert (
            await conn.execute(
                text("SELECT state FROM post_intents WHERE id=:id"), {"id": intent}
            )
        ).scalar_one() == "awaiting_approval"


async def test_bound_card_identity_cannot_inherit_new_observation(
    past_posts_engine, origin
):
    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        intent = await selected_intent(conn, origin, item)
        await conn.execute(
            text(
                "INSERT INTO instagram_intent_provenance(workspace_id,intent_id,source_id,media_item_id,origin_ig_account_id,selection_generation_id,selection_observation_revision,capture_generation_id,capture_observation_revision,selected_asset_identity,validation_fingerprint,capture_epoch,last_authorized_policy_revision) VALUES(:ws,:intent,:sid,:item,:acct,:gid,1,:gid,1,'123','first',:epoch,1)"
            ),
            dict(
                ws=str(origin["ws"]),
                intent=intent,
                sid=src["source_id"],
                item=item.media_item_id,
                acct=str(origin["iga"]),
                gid=src["generation_id"],
                epoch=src["source_use_epoch"],
            ),
        )
    from src.services.target.instagram_catalog import PastPostsAsset

    await obs.observe(
        past_posts_engine,
        origin,
        src,
        replace(
            obs.bundle(attribution="not_detected"),
            slide_count=1,
            assets=(PastPostsAsset("456", "image"),),
        ),
    )
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='second',validated_at=now()"
            )
        )
        v = await verdict(conn, origin, src, strict=True, intent=intent)
        assert not v["suitable"] and v["reason"] == "selected_asset_changed"


async def test_manual_approval_strict_uses_own_intent(past_posts_engine, origin):
    from src.services.target.commands import Command, CommandRefused
    from src.services.target import command_executors
    from src.services.target.unit_of_work import unit_of_work

    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        intent = await selected_intent(conn, origin, item, state="awaiting_approval")
        await conn.execute(
            text(
                "UPDATE workspaces SET dry_run_mode=true,api_publishing_enabled=true WHERE id=:ws"
            ),
            {"ws": str(origin["ws"])},
        )
    command = Command(
        kind="approve",
        workspace_id=str(origin["ws"]),
        actor_user_id=str(origin["user"]),
        channel="web",
        args={"intent_id": intent},
    )
    async with unit_of_work(
        past_posts_engine,
        str(origin["ws"]),
        actor_kind="user",
        actor_user_id=str(origin["user"]),
        channel="web",
    ).begin() as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.approve(conn, command)
        assert refused.value.reason == "media_unsuitable"
        assert refused.value.facts["source_reason"] == "validation_pending"
        assert (
            await conn.execute(
                text("SELECT state FROM post_intents WHERE id=:id"), {"id": intent}
            )
        ).scalar_one() == "awaiting_approval"
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='synthetic',validated_at=now()"
            )
        )
        answer = await command_executors.approve(conn, command)
        assert answer.outcome == "enqueued"


async def test_candidate_manual_schedule_and_off_cannot_be_overridden(
    past_posts_engine, origin
):
    from src.services.target.commands import Command, CommandRefused
    from src.services.target import command_executors
    from src.services.target.unit_of_work import unit_of_work
    from datetime import timedelta

    src, item = await metadata(past_posts_engine, origin)
    args = {
        "media_item_id": item.media_item_id,
        "ig_account_id": str(origin["iga"]),
        "local_at": (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M"
        ),
        "override_locks": True,
    }
    command = Command(
        kind="schedule_item",
        workspace_id=str(origin["ws"]),
        actor_user_id=str(origin["user"]),
        channel="web",
        args=args,
    )
    async with unit_of_work(
        past_posts_engine,
        str(origin["ws"]),
        actor_kind="user",
        actor_user_id=str(origin["user"]),
        channel="web",
    ).begin() as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"),
            {"sid": src["source_id"]},
        )
        with pytest.raises(CommandRefused) as refused:
            await command_executors.schedule_item(conn, command)
        assert refused.value.reason == "media_unsuitable"
        assert refused.value.facts["source_reason"] == "source_off"
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio=1 WHERE source_id=:sid"),
            {"sid": src["source_id"]},
        )
        answer = await command_executors.schedule_item(conn, command)
        assert answer.outcome == "executed"


async def test_first_policy_refuses_explicit_slide_schedule_and_reschedule(
    past_posts_engine, origin
):
    from src.services.target.commands import Command, CommandRefused
    from src.services.target import command_executors
    from src.services.target.unit_of_work import unit_of_work
    from datetime import timedelta

    src, slides = await carousel(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await source_policy.put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            body=dict(
                schema_version=2,
                expected_revision=1,
                minimum_age_days=90,
                include_credited=False,
                include_unknown=False,
                carousel_slides="first",
            ),
            capability=foundation.capability(),
        )
        intent = await selected_intent(
            conn,
            origin,
            type("Item", (), {"media_item_id": str(slides[1]["media_item_id"])})(),
            origin="planned",
        )
    common = dict(
        workspace_id=str(origin["ws"]),
        actor_user_id=str(origin["user"]),
        channel="web",
    )
    local_at = (datetime.now(timezone.utc) + timedelta(days=2)).strftime(
        "%Y-%m-%d %H:%M"
    )
    commands = (
        Command(
            kind="schedule_item",
            args=dict(
                media_item_id=str(slides[1]["media_item_id"]),
                ig_account_id=str(origin["iga"]),
                local_at=local_at,
            ),
            **common,
        ),
        Command(
            kind="reschedule_item",
            args=dict(intent_id=intent, local_at=local_at),
            **common,
        ),
    )
    async with unit_of_work(
        past_posts_engine,
        str(origin["ws"]),
        actor_kind="user",
        actor_user_id=str(origin["user"]),
        channel="web",
    ).begin() as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        for command, executor in zip(
            commands,
            (command_executors.schedule_item, command_executors.reschedule_item),
        ):
            with pytest.raises(CommandRefused) as refused:
                await executor(conn, command)
            assert refused.value.reason == "media_unsuitable"
            assert refused.value.facts["source_reason"] == "slide_not_first"


async def test_trusted_context_cannot_cross_transaction_boundary(
    past_posts_engine, origin
):
    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        context = await source_policy.suitability_context(
            conn, workspace_id=str(origin["ws"]), capability=foundation.capability()
        )
    async with obs.uow(past_posts_engine, origin) as conn:
        with pytest.raises(RuntimeError, match="suitability_context_expired"):
            await source_policy.suitability(
                conn,
                workspace_id=str(origin["ws"]),
                ig_account_id=str(origin["iga"]),
                require_validation=False,
                context=context,
            )


async def test_destination_projection_keeps_reauth_in_readiness_denominator(
    past_posts_engine, origin
):
    from src.services.target import versioned_mix

    src, item = await metadata(past_posts_engine, origin)
    account = str(origin["iga"])
    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        view = await versioned_mix.resource(conn, workspace_id=str(origin["ws"]))
        destination = view["destinations"][0]
        assert destination["account_id"] == account
        assert (destination["state"], destination["tone"], destination["remedy"]) == (
            "ready",
            "active",
            None,
        )
        assert destination["candidates"] == 1 and destination["eligible"] == 0
        assert view["guidance"]["active"] == view["guidance"]["ready"] == 1
        await conn.execute(
            text("UPDATE ig_accounts SET state='reauth_required' WHERE id=:id"),
            {"id": account},
        )
        view = await versioned_mix.resource(conn, workspace_id=str(origin["ws"]))
        destination = view["destinations"][0]
        assert (destination["state"], destination["tone"], destination["remedy"]) == (
            "needs_reconnect",
            "attention",
            "reconnect",
        )
        assert destination["candidates"] == 0
        assert all(s["effective_share"] == "0" for s in destination["sources"])
        # Still in the denominator: active, not ready, and named as incomplete.
        assert view["guidance"] == {
            "active": 1,
            "ready": 0,
            "intentional": None,
            "incomplete": [
                {
                    "account_id": account,
                    "state": "needs_reconnect",
                    "remedy": "reconnect",
                }
            ],
        }
        await conn.execute(
            text("UPDATE ig_accounts SET state='disabled' WHERE id=:id"),
            {"id": account},
        )
        view = await versioned_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert view["destinations"] == [] and view["guidance"]["active"] == 0


async def test_context_holds_current_credential_and_generation_before_intent(
    past_posts_engine, origin
):
    from sqlalchemy.exc import DBAPIError

    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as first:
        await source_policy.suitability_context(
            first, workspace_id=str(origin["ws"]), capability=foundation.capability()
        )
        for query, key in [
            (
                "SELECT id FROM oauth_credentials WHERE id=:id FOR UPDATE NOWAIT",
                str(origin["credential"]),
            ),
            (
                "SELECT id FROM instagram_past_posts_generations WHERE id=:id FOR UPDATE NOWAIT",
                src["generation_id"],
            ),
        ]:
            async with obs.uow(past_posts_engine, origin) as second:
                with pytest.raises(DBAPIError, match="could not obtain lock"):
                    await second.execute(text(query), {"id": key})


async def test_context_acquired_in_rolled_back_savepoint_is_expired(
    past_posts_engine, origin
):
    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        savepoint = await conn.begin_nested()
        captured = await source_policy.suitability_context(
            conn, workspace_id=str(origin["ws"]), capability=foundation.capability()
        )
        await savepoint.rollback()
        with pytest.raises(RuntimeError, match="suitability_context_expired"):
            await source_policy.suitability(
                conn,
                workspace_id=str(origin["ws"]),
                ig_account_id=str(origin["iga"]),
                require_validation=False,
                context=captured,
            )
        outer = await source_policy.suitability_context(
            conn, workspace_id=str(origin["ws"]), capability=foundation.capability()
        )
        savepoint = await conn.begin_nested()
        assert (
            await source_policy.suitability(
                conn,
                workspace_id=str(origin["ws"]),
                ig_account_id=str(origin["iga"]),
                source_id=src["source_id"],
                require_validation=False,
                context=outer,
            )
        )[0]["suitable"]
        await savepoint.rollback()
        assert (
            await source_policy.suitability(
                conn,
                workspace_id=str(origin["ws"]),
                ig_account_id=str(origin["iga"]),
                source_id=src["source_id"],
                require_validation=False,
                context=outer,
            )
        )[0]["suitable"]


async def test_database_refuses_incoherent_unsupported_byte_proof(
    past_posts_engine, origin
):
    from sqlalchemy.exc import DBAPIError

    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        with pytest.raises(DBAPIError, match="ck_past_posts_asset_validation_evidence"):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "UPDATE instagram_media_assets SET validation_state='unsupported',validation_generation_id=data_generation_id,validation_observation_revision=99,validation_fingerprint='synthetic',validated_at=now()"
                    )
                )
        assert (await verdict(conn, origin, src))["suitable"]
        await obs.stamp_refusal(conn, origin, item, media_validation.MATRIX_VERSION)
        assert (await verdict(conn, origin, src))["reason"] == "unsupported"


@pytest.mark.parametrize(
    "version", ["instagram-story-older", None], ids=["older", "unstamped"]
)
async def test_a_stored_refusal_from_another_matrix_version_is_a_candidate_again(
    past_posts_engine, origin, version
):
    """A stored refusal stamped with an older matrix version, or with none, is
    a candidate again, `validation_pending` in strict mode."""
    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await obs.stamp_refusal(conn, origin, item, version)
        candidate = await verdict(conn, origin, src)
        strict = await verdict(conn, origin, src, strict=True)
    assert candidate["suitable"]
    assert strict["reason"] == "validation_pending"


async def test_pool_rejects_supplied_expired_context_even_before_conversion(
    past_posts_engine, origin
):
    async with obs.uow(past_posts_engine, origin) as conn:
        context = await source_policy.suitability_context(
            conn, workspace_id=str(origin["ws"])
        )
    async with obs.uow(past_posts_engine, origin) as conn:
        with pytest.raises(RuntimeError, match="suitability_context_expired"):
            await category_mix.pool(
                conn,
                workspace_id=str(origin["ws"]),
                ig_account_id=str(origin["iga"]),
                context=context,
            )


@pytest.mark.parametrize("origin_kind", ["planned", "cadence"])
async def test_only_matching_live_planned_intent_can_override_warning_locks(
    past_posts_engine, origin, origin_kind
):
    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        intent = await selected_intent(conn, origin, item, origin=origin_kind)
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='synthetic',validated_at=now()"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO post_locks(workspace_id,media_item_id,kind,expires_at) VALUES(:ws,:item,'skip',now()+interval '1 day')"
            ),
            {"ws": str(origin["ws"]), "item": item.media_item_id},
        )
        v = await verdict(conn, origin, src, strict=True, intent=intent)
        assert v["warning_locks"] == ["skip"]
        assert v["suitable"] == (origin_kind == "planned")
        assert (
            await verdict(conn, origin, src, strict=True, intent=str(origin["intent"]))
        )["reason"] == "live_intent"
        await conn.execute(
            text(
                "INSERT INTO post_locks(workspace_id,media_item_id,kind) VALUES(:ws,:item,'hold')"
            ),
            {"ws": str(origin["ws"]), "item": item.media_item_id},
        )
        assert (await verdict(conn, origin, src, strict=True, intent=intent))[
            "reason"
        ] == "locked"
        await conn.execute(
            text(
                "UPDATE post_locks SET expires_at=now()-interval '1 second' WHERE workspace_id=:ws AND media_item_id=:item AND kind='hold'"
            ),
            {"ws": str(origin["ws"]), "item": item.media_item_id},
        )
        await conn.execute(
            text(
                "UPDATE post_intents SET cancel_requested=true,state='cancelled' WHERE id=:id"
            ),
            {"id": intent},
        )
        assert (await verdict(conn, origin, src, strict=True, intent=intent))[
            "reason"
        ] == "locked"


async def test_auto_slot_does_not_mint_for_reauth_destination(
    past_posts_engine, origin, monkeypatch
):
    from src.services.target import scheduler, content_runway
    from unittest.mock import AsyncMock

    src, item = await metadata(past_posts_engine, origin)
    monkeypatch.setattr(content_runway, "after_mint", AsyncMock())
    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        await conn.execute(
            text("UPDATE ig_accounts SET state='reauth_required' WHERE id=:id"),
            {"id": str(origin["iga"])},
        )
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio=.5 WHERE workspace_id=:ws AND effective_to IS NULL"
            ),
            {"ws": str(origin["ws"])},
        )
        await conn.execute(
            text(
                "INSERT INTO media_items(workspace_id,source_id,content_hash,provider_file_ref,file_name,media_kind) VALUES(:ws,:sid,'synthetic-reauth','synthetic-reauth','x.jpg','image')"
            ),
            {"ws": str(origin["ws"]), "sid": str(origin["src"])},
        )
        assert any(
            v["suitable"]
            for v in await source_policy.suitability(
                conn,
                workspace_id=str(origin["ws"]),
                ig_account_id=str(origin["iga"]),
                source_id=str(origin["src"]),
                require_validation=False,
            )
        )
        from src.services.target import versioned_mix

        view = await versioned_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert view["destinations"][0]["candidates"] == 0
        assert all(
            r["candidates"] == 0 and r["effective_share"] == "0"
            for r in view["destinations"][0]["sources"]
        )
        outcome = await scheduler.execute_plan_slot(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            slot_at=datetime.now(timezone.utc),
            provider_account_ref="synthetic",
            no_media_notice_after_seconds=86400,
            low_runway_days=7,
        )
        assert outcome.intent_id is None


@pytest.mark.parametrize("provider", ["gdrive", "instagram_past_posts"])
async def test_off_to_on_preview_uses_draft_supply_and_rolls_back_every_write(
    past_posts_engine, origin, provider
):
    from src.services.target import versioned_mix

    src, item = await metadata(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        conn.info["past_posts_capability"] = foundation.capability()
        await conn.execute(
            text(
                "INSERT INTO media_items(workspace_id,source_id,content_hash,provider_file_ref,file_name,media_kind) VALUES(:ws,:sid,'synthetic-preview','synthetic-preview','x.jpg','image')"
            ),
            {"ws": str(origin["ws"]), "sid": str(origin["src"])},
        )
        sid = (
            src["source_id"]
            if provider == "instagram_past_posts"
            else str(origin["src"])
        )
        if provider == "instagram_past_posts":
            await conn.execute(
                text(
                    "UPDATE category_post_case_mix SET ratio=CASE WHEN source_id=:sid THEN 0 ELSE 1 END WHERE workspace_id=:ws AND effective_to IS NULL"
                ),
                {"sid": sid, "ws": str(origin["ws"])},
            )
        current = await versioned_mix.resource(conn, workspace_id=str(origin["ws"]))
        query = "SELECT jsonb_build_object('workspace',(SELECT to_jsonb(w) FROM workspaces w WHERE id=:ws),'sources',(SELECT jsonb_agg(to_jsonb(s) ORDER BY s.id) FROM media_sources s WHERE workspace_id=:ws),'mix',(SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM category_post_case_mix x WHERE workspace_id=:ws),'audit',(SELECT count(*) FROM audit_events WHERE workspace_id=:ws),'jobs',(SELECT jsonb_agg(to_jsonb(j) ORDER BY j.id) FROM jobs j WHERE workspace_id=:ws))"
        before = (
            await conn.execute(text(query), {"ws": str(origin["ws"])})
        ).scalar_one()
        draft = await versioned_mix.preview(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "expected_revision": current["revision"],
                "action": "set_enabled",
                "source_id": sid,
                "enabled": True,
            },
        )
        future = next(
            r for r in draft["destinations"][0]["sources"] if r["source_id"] == sid
        )
        assert future["candidates"] == 1 and future["effective_share"] == "0.5"
        assert draft["revision"] == current["revision"]
        assert (
            await conn.execute(text(query), {"ws": str(origin["ws"])})
        ).scalar_one() == before
        assert (
            await versioned_mix.resource(conn, workspace_id=str(origin["ws"]))
            == current
        )
