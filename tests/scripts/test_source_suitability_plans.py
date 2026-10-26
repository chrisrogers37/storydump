"""Reproducible 10K fixture; EXPLAIN runs as each real tenant runtime role.

The actual function plan/timing is retained. Its SET search_path prevents SQL
inlining, so an additional plan explains the exact pg_proc body with the same
trusted context binds for visible child/index paths; it is explicitly labelled.
"""

import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from src.services.target import instagram_catalog, source_policy
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_source_suitability_gate as suitability_gate

pytestmark = [pytest.mark.integration]
past_posts_engine = foundation.past_posts_engine
origin = foundation.origin


async def fixture_10k(engine, origin, sync_conn):
    source = await obs.enabled(engine, origin)
    latest = await foundation.ensure(engine, origin, foundation.authorization())
    ws, sid, g1, g2 = (
        origin["ws"],
        source["source_id"],
        source["generation_id"],
        latest["generation_id"],
    )
    with sync_conn.cursor() as cur:
        cur.execute(
            """WITH items AS (
          INSERT INTO media_items(workspace_id,source_id,source_data_generation_id,content_hash,provider_file_ref,file_name,media_kind)
          SELECT %s,%s,CASE WHEN n<=10 THEN %s::uuid ELSE %s::uuid END,
            'fixture-hash-'||n::text,n::text,'fixture.jpg','image' FROM generate_series(1,10000) n
          RETURNING id,provider_file_ref,source_data_generation_id)
          INSERT INTO instagram_media_index(workspace_id,source_id,parent_media_id,data_generation_id,media_item_id,original_at,
            attribution)
          SELECT %s,%s,provider_file_ref,source_data_generation_id,id,
            now()-interval '200 days'-(provider_file_ref::integer%%1000)*interval '1 minute',
            CASE WHEN provider_file_ref::integer%%17=0 THEN 'credited' WHEN provider_file_ref::integer%%19=0 THEN 'unknown' ELSE 'not_detected' END
            FROM items""",
            (ws, sid, g1, g2, ws, sid),
        )
        cur.execute(
            """INSERT INTO instagram_media_assets(workspace_id,source_id,parent_media_id,
              asset_media_id,data_generation_id,observation_revision,position,media_kind,
              media_item_id,validation_state,validation_generation_id,
              validation_observation_revision,validation_fingerprint,validated_at)
            SELECT workspace_id,source_id,parent_media_id,parent_media_id,
              data_generation_id,observation_revision,1,'image',media_item_id,
              'supported',data_generation_id,observation_revision,'fixture-v1',now()
            FROM instagram_media_index WHERE workspace_id=%s AND source_id=%s""",
            (ws, sid),
        )
        cur.execute(
            """INSERT INTO instagram_item_tombstones(workspace_id,source_id,media_item_id,key_version,parent_hmac)
            SELECT workspace_id,source_id,media_item_id,1,md5(parent_media_id)||md5(parent_media_id)
            FROM instagram_media_index WHERE workspace_id=%s AND source_id=%s""",
            (ws, sid),
        )
        cur.execute(
            "INSERT INTO ig_accounts(workspace_id,provider_account_ref) VALUES (%s,'plan-dormant-destination') RETURNING id",
            (ws,),
        )
        other = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO media_source_destinations(workspace_id,source_id,ig_account_id) VALUES (%s,%s,%s)",
            (ws, sid, other),
        )
        for table in (
            "instagram_media_index",
            "instagram_media_assets",
            "media_items",
            "instagram_item_tombstones",
            "media_source_destinations",
            "media_sources",
            "instagram_past_posts_generations",
            "instagram_source_policies",
            "category_post_case_mix",
        ):
            cur.execute("ANALYZE " + table)
    sync_conn.commit()
    async with obs.uow(engine, origin) as conn:
        await suitability_gate.activate(conn, ws)
    return dict(source=latest, old_generation=g1, other_destination=other)


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


async def explain(conn, sql, binds):
    raw = (
        await conn.execute(text("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + sql), binds)
    ).scalar_one()
    return raw[0] if isinstance(raw, list) else json.loads(raw)[0]


async def test_10k_suitability_and_selective_paths_are_bounded(
    past_posts_engine, origin, sync_conn, tmp_path
):
    fixture = await fixture_10k(past_posts_engine, origin, sync_conn)
    sid = fixture["source"]["source_id"]
    records = {}
    async with obs.uow(past_posts_engine, origin) as conn:
        role = (await conn.execute(text("SELECT current_user"))).scalar_one()
        context = await source_policy.suitability_context(
            conn,
            workspace_id=str(origin["ws"]),
            ig_account_ids=[origin["iga"], fixture["other_destination"]],
            capability=foundation.capability(),
        )
        binds = dict(
            context.binds(),
            acct=str(origin["iga"]),
            sid=sid,
            media=None,
            intent=None,
            parent="5000",
            gids=[fixture["old_generation"]],
        )
        item = (
            await conn.execute(
                text(
                    "SELECT media_item_id FROM instagram_media_assets WHERE workspace_id=:ws AND source_id=:sid AND parent_media_id=:parent"
                ),
                binds,
            )
        ).scalar_one()
        digest = (
            await conn.execute(text("SELECT md5(:parent)||md5(:parent)"), binds)
        ).scalar_one()
        binds["digest"] = digest
        binds["versions"] = [1]
        binds["digests"] = [digest]
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM instagram_media_index WHERE workspace_id=:ws AND source_id=:sid"
                ),
                binds,
            )
        ).scalar_one() == 10000
        # Current policy, rather than cached verdicts, still decides on the large fixture.
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM ("
                    + source_policy.SUITABILITY_SQL
                    + ") v WHERE suitable"
                ),
                binds,
            )
        ).scalar_one()
        assert 8000 < count < 9990
        body = (
            await conn.execute(
                text(
                    "SELECT prosrc FROM pg_proc WHERE oid='fn_source_asset_suitability(uuid,uuid,integer,timestamptz,boolean,text,uuid,uuid,uuid,boolean)'::regprocedure"
                )
            )
        ).scalar_one()
        substitutions = {
            "p_workspace_id": "CAST(:ws AS uuid)",
            "p_destination_id": "CAST(:acct AS uuid)",
            "p_resolved_repost_ttl_days": "CAST(:ttl AS integer)",
            "p_evaluated_at": "CAST(:at AS timestamptz)",
            "p_past_posts_enabled": "CAST(:activated AS boolean)",
            "p_matrix_version": "CAST(:matrix AS text)",
            "p_source_id": "CAST(:sid AS uuid)",
            "p_media_item_id": "CAST(:media AS uuid)",
            "p_existing_intent_id": "CAST(:intent AS uuid)",
            "p_require_validation": "true",
        }
        expanded = re.sub(r"\bp_[a-z_]+\b", lambda m: substitutions[m.group()], body)
        queries = {
            "function_pool": "SELECT count(*) FROM ("
            + source_policy.SUITABILITY_SQL
            + ") v WHERE suitable",
            "exact_body_pool": "SELECT count(*) FROM ("
            + expanded
            + ") v(source_id,media_item_id,provider,suitable,base_suitable,reason,data_generation_id,observation_revision,policy_revision,source_use_epoch,effective_minimum_age_days,live_intent_conflict,blocking_locks,warning_locks) WHERE suitable",
            "parent_lookup": "SELECT * FROM instagram_media_index WHERE workspace_id=:ws AND source_id=:sid AND parent_media_id=:parent",
            "item_lookup": "SELECT * FROM instagram_media_assets WHERE workspace_id=:ws AND source_id=:sid AND media_item_id=CAST(:target AS uuid)",
            "time_lookup": "SELECT a.media_item_id FROM instagram_media_index i JOIN instagram_media_assets a ON a.workspace_id=i.workspace_id AND a.source_id=i.source_id AND a.parent_media_id=i.parent_media_id WHERE i.workspace_id=:ws AND i.source_id=:sid AND a.data_generation_id=CAST(:gid AS uuid) AND i.original_at>=CAST(:at AS timestamptz)-interval '200 days 2 minutes' AND i.availability='available' AND a.validation_state='supported'",
            "destination_lookup": "SELECT source_id FROM media_source_destinations WHERE workspace_id=:ws AND ig_account_id=:acct AND source_id=:sid AND state='active'",
            "tombstone_lookup": "SELECT t.* FROM instagram_item_tombstones t JOIN unnest(CAST(:versions AS integer[]),CAST(:digests AS text[])) AS wanted(key_version,parent_hmac) USING(key_version,parent_hmac) WHERE workspace_id=:ws ORDER BY media_item_id",
            "redaction_item_lookup": instagram_catalog._REDACTION_ITEM_LOOKUP,
            "redaction_index_lookup": "SELECT parent_media_id FROM instagram_media_index WHERE workspace_id=:ws AND source_id=:sid AND data_generation_id=ANY(CAST(:gids AS uuid[]))",
        }
        binds.update(target=str(item), gid=fixture["source"]["generation_id"])
        for name, sql in queries.items():
            plan = await explain(conn, sql, binds)
            records[name] = {"query": sql, "binds": binds, "plan": plan}
        point = dict(binds, media=str(item))
        records["function_item"] = {
            "query": source_policy.SUITABILITY_SQL,
            "binds": point,
            "plan": await explain(conn, source_policy.SUITABILITY_SQL, point),
        }
        records["exact_body_item"] = {
            "query": expanded,
            "binds": point,
            "plan": await explain(conn, expanded, point),
        }
        root = Path(os.environ.get("P1C_PLAN_EVIDENCE_DIR", str(tmp_path)))
        root.mkdir(parents=True, exist_ok=True)
        artifact = {
            "role": role,
            "tenant": str(origin["ws"]),
            "fixture_size": 10000,
            "fixture_recipe": str(Path(__file__).resolve()),
            "function_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "note": "function_* are actual calls; exact_body_* expose the same pg_proc query beneath the non-inlined SET search_path boundary",
            "records": records,
        }
        (root / f"p1c-plans-{role}.json").write_text(
            json.dumps(artifact, default=str, indent=2) + "\n"
        )
        assert records["function_pool"]["plan"]["Execution Time"] < 5000
        assert records["function_item"]["plan"]["Execution Time"] < 1000
        expensive = []
        for name in (
            "parent_lookup",
            "item_lookup",
            "time_lookup",
            "tombstone_lookup",
            "redaction_item_lookup",
            "redaction_index_lookup",
        ):
            plan = records[name]["plan"]
            assert plan["Execution Time"] < 1000, name
            if any(
                n.get("Rows Removed by Filter", 0) >= 100
                or n["Node Type"] == "Seq Scan"
                and n["Actual Rows"] >= 100
                for n in nodes(plan["Plan"])
            ):
                expensive.append(name)
        assert not expensive, (
            "selective lookups scan unrelated observations",
            expensive,
        )
        wrong_destination = dict(binds, acct=str(fixture["other_destination"]))
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM ("
                    + source_policy.SUITABILITY_SQL
                    + ") v WHERE suitable"
                ),
                wrong_destination,
            )
        ).scalar_one() == 0
        await source_policy.put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=sid,
            body=dict(
                schema_version=1,
                expected_revision=1,
                minimum_age_days=90,
                include_credited=True,
                include_unknown=False,
            ),
            capability=foundation.capability(),
        )
        await conn.execute(
            text(
                "UPDATE instagram_media_index SET cache_generation_id=data_generation_id,cache_observation_revision=observation_revision,cache_policy_revision=1,cached_eligible=false WHERE source_id=:sid"
            ),
            binds,
        )
        current = (
            (
                await conn.execute(
                    text(
                        "SELECT count(*) AS n,min(policy_revision) AS revision FROM ("
                        + source_policy.SUITABILITY_SQL
                        + ") v WHERE suitable"
                    ),
                    binds,
                )
            )
            .mappings()
            .one()
        )
        assert current["n"] > count and current["revision"] == 2
        assert records["parent_lookup"]["plan"]["Plan"]["Actual Rows"] == 1
        assert records["redaction_index_lookup"]["plan"]["Plan"]["Actual Rows"] == 10
