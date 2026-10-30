"""One invoker suitability relation; current persisted evidence beats caches."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from src.services.target import media_validation, source_policy
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts.conftest import seed_workspace_chain

pytestmark = [pytest.mark.integration]
past_posts_engine = foundation.past_posts_engine
origin = foundation.origin


def required():
    assert hasattr(source_policy, "suitability"), (
        "central SQL suitability wrapper is missing"
    )


async def activate(conn, ws):
    await conn.execute(
        text(
            "UPDATE workspaces SET source_mix_mode='equal',source_mix_policy_version=1,source_membership_policy='reset_equal',source_mix_revision=1,source_mix_conversion_version=1,source_mix_converted_at=now() WHERE id=:ws"
        ),
        dict(ws=str(ws)),
    )


async def ready(engine, origin, attribution="not_detected"):
    required()
    src = await obs.enabled(engine, origin)
    item = await obs.observe(engine, origin, src, obs.bundle(attribution=attribution))
    async with obs.uow(engine, origin) as conn:
        await activate(conn, origin["ws"])
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='v1',validated_at=now()"
            )
        )
    return src, item


async def verdict(engine, origin, src, cap=True, **kw):
    async with obs.uow(engine, origin) as conn:
        rows = await source_policy.suitability(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(kw.pop("destination", origin["iga"])),
            source_id=src["source_id"],
            capability=foundation.capability() if cap else None,
            **kw,
        )
        assert len(rows) == 1
        return rows[0]


async def test_own_origin_valid_asset_ignores_stale_cached_verdict(
    past_posts_engine, origin
):
    src, item = await ready(past_posts_engine, origin)
    v = await verdict(past_posts_engine, origin, src)
    assert v["suitable"] and v["base_suitable"] and v["reason"] is None
    assert str(v["media_item_id"]) == item.media_item_id and v["policy_revision"] == 1


async def test_saved_policy_versions_one_and_two_share_suitability_authority(
    past_posts_engine, origin
):
    src, _ = await ready(past_posts_engine, origin)
    for version in (1, 2, 3):
        async with obs.uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text(
                    "UPDATE instagram_source_policies SET schema_version=:version WHERE source_id=:sid"
                ),
                {"version": version, "sid": src["source_id"]},
            )
        result = await verdict(past_posts_engine, origin, src)
        assert result["suitable"] is (version in (1, 2))
        assert result["reason"] == (
            None if version in (1, 2) else "unsupported_source_policy"
        )


@pytest.mark.parametrize(
    "sql,reason",
    [
        (
            "UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid",
            "source_off",
        ),
        (
            "UPDATE category_post_case_mix SET effective_to=now() WHERE source_id=:sid",
            "source_off",
        ),
        (
            "UPDATE media_sources SET config=jsonb_build_object('removed',true) WHERE id=:sid",
            "source_unavailable",
        ),
        (
            "UPDATE oauth_credentials SET state='revoked' WHERE id=:credential",
            "credential_unavailable",
        ),
        (
            "UPDATE oauth_credentials SET expires_at=now()-interval '1 second' WHERE id=:credential",
            "credential_unavailable",
        ),
        (
            "UPDATE instagram_past_posts_generations SET lifecycle='deletion_pending' WHERE id=:gid",
            "generation_unavailable",
        ),
        # past-posts lifecycle cleanup: a generation held for review is unavailable, not destroyed.
        (
            "UPDATE instagram_past_posts_generations SET lifecycle='review_hold' WHERE id=:gid",
            "generation_unavailable",
        ),
        (
            "UPDATE instagram_media_index SET availability='excluded',exclusion_reason='origin_identity_conflict' WHERE source_id=:sid",
            "identity_conflict_or_deleted",
        ),
        (
            "UPDATE media_sources SET current_authorization_generation_id=NULL WHERE id=:sid",
            "generation_unavailable",
        ),
        (
            "UPDATE instagram_media_index SET availability='unavailable' WHERE source_id=:sid",
            "unavailable",
        ),
        (
            "UPDATE instagram_media_assets SET availability='unavailable',exclusion_reason='asset_unavailable' WHERE source_id=:sid",
            "unavailable",
        ),
        (
            "UPDATE instagram_media_assets SET availability='excluded',exclusion_reason='origin_identity_conflict' WHERE source_id=:sid",
            "identity_conflict_or_deleted",
        ),
        (
            "UPDATE instagram_media_assets SET validation_state='unsupported',"
            "validation_capture=jsonb_build_object("
            "'generation_id',data_generation_id::text,"
            "'observation_revision',observation_revision,"
            "'selected_asset_id',asset_media_id,'state','unsupported',"
            f"'matrix_version','{media_validation.MATRIX_VERSION}')"
            " WHERE source_id=:sid",
            "unsupported",
        ),
        (
            "UPDATE instagram_media_assets SET validation_state='pending',validation_generation_id=NULL,validation_observation_revision=NULL,validation_fingerprint=NULL,validated_at=NULL WHERE source_id=:sid",
            "validation_pending",
        ),
        (
            "UPDATE instagram_media_index SET original_at=now()-interval '89 days' WHERE source_id=:sid",
            "too_recent",
        ),
        (
            "UPDATE media_source_destinations SET state='retired' WHERE source_id=:sid",
            "destination_not_allowed",
        ),
    ],
)
async def test_denied_matrix_is_database_authority(
    past_posts_engine, origin, sql, reason
):
    src, item = await ready(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(sql),
            dict(
                sid=src["source_id"],
                gid=src["generation_id"],
                credential=str(origin["credential"]),
            ),
        )
    v = await verdict(past_posts_engine, origin, src)
    assert not v["suitable"] and v["reason"] == reason


async def test_index_only_post_keeps_one_explicit_exclusion_row(
    past_posts_engine, origin
):
    src, _ = await ready(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("DELETE FROM instagram_media_assets WHERE source_id=:sid"),
            {"sid": src["source_id"]},
        )
    v = await verdict(past_posts_engine, origin, src)
    assert v["media_item_id"] is None
    assert not v["suitable"] and v["reason"] == "validation_pending"


@pytest.mark.parametrize(
    "attribution,flag,reason",
    [
        ("credited", "include_credited", "credited_excluded"),
        ("unknown", "include_unknown", "attribution_unknown"),
    ],
)
async def test_attribution_permissions_are_independent_current_policy(
    past_posts_engine, origin, attribution, flag, reason
):
    src, item = await ready(past_posts_engine, origin, attribution)
    assert (await verdict(past_posts_engine, origin, src))["reason"] == reason
    async with obs.uow(past_posts_engine, origin) as conn:
        body = dict(
            schema_version=1,
            expected_revision=1,
            minimum_age_days=90,
            include_credited=False,
            include_unknown=False,
        )
        other = "include_unknown" if flag == "include_credited" else "include_credited"
        body[other] = True
        await source_policy.put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            body=body,
            capability=foundation.capability(),
        )
    assert (await verdict(past_posts_engine, origin, src))["reason"] == reason
    async with obs.uow(past_posts_engine, origin) as conn:
        body.update(expected_revision=2)
        body[flag] = True
        await source_policy.put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            body=body,
            capability=foundation.capability(),
        )
        await conn.execute(
            text(
                "UPDATE instagram_media_index SET cache_policy_revision=1,cached_eligible=false"
            )
        )
    v = await verdict(past_posts_engine, origin, src)
    assert v["suitable"] and v["policy_revision"] == 3


async def test_g2_never_treats_retained_g1_as_current(past_posts_engine, origin):
    src, item = await ready(past_posts_engine, origin)
    g2 = await foundation.ensure(past_posts_engine, origin, foundation.authorization())
    assert g2["generation_id"] != src["generation_id"]
    assert (await verdict(past_posts_engine, origin, src))[
        "reason"
    ] == "awaiting_reobservation"


async def test_dormant_nonorigin_binding_denies_and_drive_remains_workspacewide(
    past_posts_engine, origin, sync_conn
):
    src, item = await ready(past_posts_engine, origin)
    with sync_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ig_accounts(workspace_id,provider_account_ref) VALUES (%s,'other-origin') RETURNING id",
            (origin["ws"],),
        )
        other = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO media_source_destinations(workspace_id,source_id,ig_account_id) VALUES (%s,%s,%s)",
            (origin["ws"], src["source_id"], other),
        )
    sync_conn.commit()
    assert (await verdict(past_posts_engine, origin, src, destination=other))[
        "reason"
    ] == "destination_not_allowed"
    async with obs.uow(past_posts_engine, origin) as conn:
        drive = await source_policy.suitability(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(other),
            source_id=str(origin["src"]),
        )
        assert len(drive) == 1 and drive[0]["suitable"]


async def test_past_posts_activation_and_conversion_fail_closed_without_changing_drive(
    past_posts_engine, origin
):
    src, item = await ready(past_posts_engine, origin)
    assert (await verdict(past_posts_engine, origin, src, cap=False))[
        "reason"
    ] == "past_posts_not_activated"
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE workspaces SET source_mix_converted_at=NULL WHERE id=:ws"),
            dict(ws=str(origin["ws"])),
        )
    assert (await verdict(past_posts_engine, origin, src))[
        "reason"
    ] == "past_posts_not_activated"


async def test_self_intent_context_and_lock_facts_leave_duplicate_arbiter_intact(
    past_posts_engine, origin
):
    src, item = await ready(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        intent = (
            await conn.execute(
                text(
                    "INSERT INTO post_intents(workspace_id,ig_account_id,media_item_id,provider_account_ref,approval_mode,schedule_slot_at) VALUES (:ws,:acct,:item,'fixture','manual',now()) RETURNING id"
                ),
                dict(
                    ws=str(origin["ws"]),
                    acct=str(origin["iga"]),
                    item=item.media_item_id,
                ),
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO post_locks(workspace_id,media_item_id,ig_account_id,kind,expires_at) VALUES (:ws,:item,:acct,'recent',now()+interval '1 day')"
            ),
            dict(
                ws=str(origin["ws"]), acct=str(origin["iga"]), item=item.media_item_id
            ),
        )
    v = await verdict(past_posts_engine, origin, src)
    assert (
        v["live_intent_conflict"]
        and v["reason"] == "live_intent"
        and v["base_suitable"]
    )
    v = await verdict(past_posts_engine, origin, src, existing_intent_id=str(intent))
    assert (
        not v["live_intent_conflict"]
        and v["warning_locks"] == ["recent"]
        and v["reason"] == "locked"
    )
    assert v["base_suitable"] and v["blocking_locks"] == []
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE post_locks SET expires_at=now()-interval '1 second'")
        )
    assert (
        await verdict(past_posts_engine, origin, src, existing_intent_id=str(intent))
    )["suitable"]


@pytest.mark.parametrize(
    "signature",
    [
        "fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,text)",
        "fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,uuid,uuid,uuid,boolean,text)",
    ],
)
async def test_invoker_grants_and_foreign_arguments_cannot_read(
    past_posts_engine, origin, sync_conn, signature
):
    src, item = await ready(past_posts_engine, origin)
    foreign = seed_workspace_chain(sync_conn, "suitability-foreign")
    async with obs.uow(past_posts_engine, origin) as conn:
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[origin["iga"]],
            capability=foundation.capability(),
        )
        binds = dict(
            context.binds(), acct=str(foreign["iga"]), sid=None, media=None, intent=None
        )
        assert (
            not (
                await conn.execute(
                    text(source_policy.SUITABILITY_SQL),
                    dict(binds, ws=str(foreign["ws"])),
                )
            )
            .mappings()
            .all()
        )
        catalog = (
            (
                await conn.execute(
                    text(
                        "SELECT prosecdef,provolatile::text AS provolatile,proconfig,has_function_privilege(current_user,oid,'EXECUTE') AS can_execute,NOT EXISTS (SELECT 1 FROM aclexplode(coalesce(proacl,acldefault('f',proowner))) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE') AS public_denied FROM pg_proc WHERE oid=to_regprocedure(:signature)"
                    ),
                    {"signature": signature},
                )
            )
            .mappings()
            .one()
        )
        assert (
            not catalog["prosecdef"]
            and catalog["provolatile"] == "s"
            and catalog["can_execute"]
            and catalog["public_denied"]
        )
        assert "search_path=pg_catalog, public, pg_temp" in catalog["proconfig"]


async def test_context_owns_db_time_and_holds_workspace_ttl_through_decision(
    past_posts_engine, origin
):
    required()
    src, item = await ready(past_posts_engine, origin)
    started = asyncio.Event()
    done = asyncio.Event()

    async def change():
        async with obs.uow(past_posts_engine, origin) as conn:
            started.set()
            await conn.execute(
                text("UPDATE workspaces SET repost_ttl_days=180 WHERE id=:ws"),
                dict(ws=str(origin["ws"])),
            )
        done.set()

    async with obs.uow(past_posts_engine, origin) as conn:
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[origin["iga"]],
            capability=foundation.capability(),
        )
        assert (
            context.resolved_repost_ttl_days == 30
            and context.evaluated_at.tzinfo is not None
        )
        task = asyncio.create_task(change())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not done.is_set()
        rows = (
            (
                await conn.execute(
                    text(source_policy.SUITABILITY_SQL),
                    dict(
                        context.binds(),
                        acct=str(origin["iga"]),
                        sid=src["source_id"],
                        media=None,
                        intent=None,
                    ),
                )
            )
            .mappings()
            .all()
        )
        assert rows[0]["effective_minimum_age_days"] == 90
    await asyncio.wait_for(task, 3)
    assert (await verdict(past_posts_engine, origin, src))[
        "effective_minimum_age_days"
    ] == 181


@pytest.mark.parametrize("raw", [None, 0, 45, 120, -2])
async def test_exact_age_boundary_uses_locked_workspace_ttl(
    past_posts_engine, origin, raw
):
    from src.services.target.intent_ledger import resolve_repost_ttl_days

    src, item = await ready(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE workspaces SET repost_ttl_days=:ttl WHERE id=:ws"),
            dict(ttl=raw, ws=str(origin["ws"])),
        )
        await source_policy.put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            body=dict(
                schema_version=1,
                expected_revision=1,
                minimum_age_days=0,
                include_credited=False,
                include_unknown=False,
            ),
            capability=foundation.capability(),
        )
    age = max(0, resolve_repost_ttl_days(raw) + 1)
    async with obs.uow(past_posts_engine, origin) as conn:
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[origin["iga"]],
            capability=foundation.capability(),
        )
        query = text(source_policy.SUITABILITY_SQL)
        binds = dict(
            context.binds(),
            acct=str(origin["iga"]),
            sid=src["source_id"],
            media=None,
            intent=None,
        )
        boundary = context.evaluated_at - timedelta(days=age)
        await conn.execute(
            text(
                "UPDATE instagram_media_index SET original_at=:at WHERE source_id=:sid"
            ),
            dict(at=boundary, sid=src["source_id"]),
        )
        assert (await conn.execute(query, binds)).mappings().one()["suitable"]
        await conn.execute(
            text(
                "UPDATE instagram_media_index SET original_at=:at WHERE source_id=:sid"
            ),
            dict(at=boundary + timedelta(microseconds=1), sid=src["source_id"]),
        )
        v = (await conn.execute(query, binds)).mappings().one()
        assert (
            not v["suitable"]
            and v["reason"] == "too_recent"
            and v["effective_minimum_age_days"] == age
        )


async def test_index_only_exclusion_and_pending_observations_have_named_verdicts(
    past_posts_engine, origin
):
    from src.services.target.instagram_catalog import PastPostsObservation

    src = await obs.enabled(past_posts_engine, origin)
    await obs.observe(
        past_posts_engine,
        origin,
        src,
        PastPostsObservation(
            "888",
            obs.bundle().original_at,
            availability="excluded",
            exclusion_reason="unsupported_media",
        ),
    )
    async with obs.uow(past_posts_engine, origin) as conn:
        await activate(conn, origin["ws"])
    v = await verdict(past_posts_engine, origin, src)
    assert v["media_item_id"] is None and v["reason"] == "unsupported"


async def test_unknown_scope_denies_even_if_schema_is_extended_without_authority(
    past_posts_engine, origin, sync_conn
):
    src, item = await ready(past_posts_engine, origin)
    with sync_conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE instagram_source_policies DROP CONSTRAINT ck_past_posts_policy_modes"
        )
        cur.execute(
            "UPDATE instagram_source_policies SET destination_scope='future' WHERE source_id=%s",
            (src["source_id"],),
        )
    sync_conn.commit()
    try:
        assert (await verdict(past_posts_engine, origin, src))[
            "reason"
        ] == "unsupported_source_policy"
    finally:
        with sync_conn.cursor() as cur:
            cur.execute(
                "UPDATE instagram_source_policies SET destination_scope='origin_only' WHERE source_id=%s",
                (src["source_id"],),
            )
            cur.execute(
                "ALTER TABLE instagram_source_policies ADD CONSTRAINT ck_past_posts_policy_modes CHECK(eligibility='supported' AND ranking='none' AND destination_scope='origin_only')"
            )
        sync_conn.commit()


@pytest.mark.parametrize(
    "field", ["source_mix_policy_version", "source_mix_conversion_version"]
)
@pytest.mark.parametrize("future", [2, 999])
async def test_unknown_mix_versions_deny_past_posts_and_preserve_drive(
    past_posts_engine, origin, field, future
):
    src, item = await ready(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        # Columns are a closed test parameter set, never caller input.
        await conn.execute(
            text(f"UPDATE workspaces SET {field}=:version WHERE id=:ws"),
            dict(version=future, ws=str(origin["ws"])),
        )
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[origin["iga"]],
            capability=foundation.capability(),
        )
        assert not context.past_posts_enabled, (
            "future versions are schema-valid but not understood by this reader"
        )
        past_posts = await source_policy.suitability(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            source_id=src["source_id"],
            capability=foundation.capability(),
        )
        assert (
            past_posts[0]["reason"] == "past_posts_not_activated"
            and not past_posts[0]["suitable"]
        )
        drive = await source_policy.suitability(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_id=str(origin["iga"]),
            source_id=str(origin["src"]),
            existing_intent_id=str(origin["intent"]),
        )
        assert len(drive) == 1 and drive[0]["suitable"] and drive[0]["base_suitable"]


async def test_partial_conversion_without_revision_is_not_activated(
    past_posts_engine, origin
):
    src, item = await ready(past_posts_engine, origin)
    async with obs.uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text("UPDATE workspaces SET source_mix_revision=NULL WHERE id=:ws"),
            dict(ws=str(origin["ws"])),
        )
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[origin["iga"]],
            capability=foundation.capability(),
        )
        assert not context.past_posts_enabled
