"""Policy writes under actual runtime roles, with CAS and atomic audit."""

import asyncio
import importlib

import pytest
from sqlalchemy import text

from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as observations

pytestmark = [pytest.mark.integration]
archive_engine = foundation.archive_engine
origin = foundation.origin
uow = observations.uow


def service():
    assert importlib.util.find_spec("src.services.target.source_policy"), (
        "central policy service is missing"
    )
    return importlib.import_module("src.services.target.source_policy")


def update(revision=1, age=90, credited=False, unknown=False):
    return dict(
        schema_version=1,
        expected_revision=revision,
        minimum_age_days=age,
        include_credited=credited,
        include_unknown=unknown,
    )


def update_v2(revision=1, *, slides="all", age=90):
    return dict(
        update(revision=revision, age=age), schema_version=2, carousel_slides=slides
    )


async def get(engine, origin, sid):
    async with uow(engine, origin) as conn:
        return await service().get_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=sid,
            capability=foundation.capability(),
        )


async def put(engine, origin, sid, body):
    async with uow(engine, origin) as conn:
        return await service().put_policy(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=sid,
            body=body,
            capability=foundation.capability(),
        )


async def test_saved_defaults_reconnect_and_runtime_default_change(
    archive_engine, origin, monkeypatch
):
    p = service()
    src = await foundation.ensure(archive_engine, origin, foundation.authorization())
    view = await get(archive_engine, origin, src["source_id"])
    assert view["revision"] == 1 and view["policy"]["minimum_age_days"] == 90
    await put(archive_engine, origin, src["source_id"], update(age=3, credited=True))
    old = p.policy_defaults
    monkeypatch.setattr(p, "policy_defaults", lambda: dict(old(), minimum_age_days=200))
    same = await foundation.ensure(archive_engine, origin, foundation.authorization())
    assert same["source_id"] == src["source_id"]
    view = await get(archive_engine, origin, src["source_id"])
    assert view["revision"] == 2 and view["policy"]["minimum_age_days"] == 3
    assert view["effective"]["minimum_age_days"] == 31


async def test_saved_versions_and_legacy_put_preserve_first_choice(
    archive_engine, origin
):
    p = service()
    src = await foundation.ensure(archive_engine, origin, foundation.authorization())
    sid = src["source_id"]
    initial = await get(archive_engine, origin, sid)
    assert initial["schema_version"] == 2
    assert initial["policy"]["carousel_slides"] == "all"
    assert "carousel_slides" in initial["capabilities"]["editable"]
    first = await put(archive_engine, origin, sid, update_v2(slides="first"))
    assert first["revision"] == 2 and first["policy"]["carousel_slides"] == "first"
    legacy = await put(archive_engine, origin, sid, update(revision=2, age=91))
    assert legacy["schema_version"] == 2 and legacy["revision"] == 3
    assert legacy["policy"]["carousel_slides"] == "first"
    async with uow(archive_engine, origin) as conn:
        saved = (
            await conn.execute(
                text(
                    "SELECT schema_version,carousel_slides FROM instagram_source_policies WHERE source_id=:sid"
                ),
                {"sid": sid},
            )
        ).one()
        assert saved == (2, "first")
        audit = (
            (
                await conn.execute(
                    text(
                        "SELECT detail FROM audit_events WHERE entity_kind='instagram_source_policy'"
                    ),
                )
            )
            .scalars()
            .all()
        )
        changes = {entry["revision"]: entry["changed"] for entry in audit}
        assert changes[2]["carousel_slides"] == {"from": "all", "to": "first"}
        assert "carousel_slides" not in changes[3]
        await conn.execute(
            text(
                "UPDATE instagram_source_policies SET schema_version=1 WHERE source_id=:sid"
            ),
            {"sid": sid},
        )
    assert (await get(archive_engine, origin, sid))["policy"][
        "carousel_slides"
    ] == "first"
    async with uow(archive_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_source_policies SET schema_version=3 WHERE source_id=:sid"
            ),
            {"sid": sid},
        )
    with pytest.raises(p.PolicyRefused) as error:
        await get(archive_engine, origin, sid)
    assert error.value.reason == "unsupported_source_policy"


async def test_policy_v2_stale_revision_and_unsupported_choice(archive_engine, origin):
    p = service()
    src = await foundation.ensure(archive_engine, origin, foundation.authorization())
    sid = src["source_id"]
    await put(archive_engine, origin, sid, update_v2(slides="first"))
    with pytest.raises(p.PolicyRefused) as stale:
        await put(archive_engine, origin, sid, update_v2(slides="all"))
    assert stale.value.reason == "policy_conflict" and stale.value.current_revision == 2
    for body in (
        update_v2(revision=2, slides="middle"),
        dict(update_v2(revision=2), schema_version=3),
    ):
        with pytest.raises(p.PolicyRefused) as invalid:
            await put(archive_engine, origin, sid, body)
        assert invalid.value.reason == "unsupported_source_policy"
    assert (await get(archive_engine, origin, sid))["policy"][
        "carousel_slides"
    ] == "first"


async def test_concurrent_cas_has_one_winner_and_revision_on_conflict(
    archive_engine, origin
):
    p = service()
    src = await foundation.ensure(archive_engine, origin, foundation.authorization())
    results = await asyncio.gather(
        put(archive_engine, origin, src["source_id"], update(age=1)),
        put(archive_engine, origin, src["source_id"], update(age=2)),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, p.PolicyRefused)]
    assert len(winners) == len(losers) == 1
    assert winners[0]["revision"] == 2
    assert losers[0].reason == "policy_conflict" and losers[0].current_revision == 2


async def test_policy_audit_preserves_asset_evidence_and_source_epoch(
    archive_engine, origin
):
    service()
    src = await observations.enabled(archive_engine, origin)
    await observations.observe(
        archive_engine, origin, src, observations.bundle(attribution="not_detected")
    )
    async with uow(archive_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE instagram_media_assets SET validation_state='supported',validation_generation_id=data_generation_id,validation_observation_revision=observation_revision,validation_fingerprint='v1',validated_at=now()"
            )
        )
        before = dict(
            (await conn.execute(text("SELECT * FROM instagram_media_assets")))
            .mappings()
            .one()
        )
        epoch = (
            await conn.execute(
                text("SELECT source_use_epoch FROM media_sources WHERE id=:sid"),
                dict(sid=src["source_id"]),
            )
        ).scalar_one()
    await put(archive_engine, origin, src["source_id"], update(age=0, unknown=True))
    async with uow(archive_engine, origin) as conn:
        after = dict(
            (await conn.execute(text("SELECT * FROM instagram_media_assets")))
            .mappings()
            .one()
        )
        assert after == before
        assert (
            await conn.execute(
                text("SELECT source_use_epoch FROM media_sources WHERE id=:sid"),
                dict(sid=src["source_id"]),
            )
        ).scalar_one() == epoch
        audit = (
            (
                await conn.execute(
                    text(
                        "SELECT entity_id,actor_kind,detail FROM audit_events WHERE entity_kind='instagram_source_policy'"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert (
            str(audit["entity_id"]) == src["source_id"]
            and audit["actor_kind"] == "system"
        )
        assert audit["detail"]["revision"] == 2


async def test_caught_audit_failure_rolls_back_policy_and_allows_retry(
    archive_engine, origin, monkeypatch
):
    p = service()
    src = await foundation.ensure(archive_engine, origin, foundation.authorization())
    from src.services.target import audit

    original = audit.record

    async def fail(*args, **kw):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit, "record", fail)
    async with uow(archive_engine, origin) as conn:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await p.put_policy(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=src["source_id"],
                body=update(age=0),
                capability=foundation.capability(),
            )
        await conn.execute(
            text("UPDATE workspaces SET name='unrelated' WHERE id=:ws"),
            dict(ws=str(origin["ws"])),
        )
    monkeypatch.setattr(audit, "record", original)
    assert (await get(archive_engine, origin, src["source_id"]))["revision"] == 1
    assert (await put(archive_engine, origin, src["source_id"], update(age=0)))[
        "revision"
    ] == 2


async def test_missing_wrong_provider_activation_and_foreign_are_closed(
    archive_engine, origin, sync_conn
):
    import uuid
    from tests.scripts.conftest import seed_workspace_chain

    p = service()
    src = await foundation.ensure(archive_engine, origin, foundation.authorization())
    foreign = seed_workspace_chain(sync_conn, "foreign-policy")
    for sid, cap, reason in [
        (str(uuid.uuid4()), foundation.capability(), "source_not_found"),
        (str(origin["src"]), foundation.capability(), "unsupported_source_provider"),
        (src["source_id"], None, "archive_not_activated"),
        (str(foreign["src"]), foundation.capability(), "source_not_found"),
    ]:
        async with uow(archive_engine, origin) as conn:
            with pytest.raises(p.PolicyRefused) as error:
                await p.get_policy(
                    conn, workspace_id=str(origin["ws"]), source_id=sid, capability=cap
                )
            assert error.value.reason == reason


async def test_safe_source_projection_preserves_drive_and_current_generation_coverage(
    archive_engine, origin
):
    from src.services.target import workspaces

    src = await observations.enabled(archive_engine, origin)
    await observations.observe(archive_engine, origin, src, observations.bundle())
    async with uow(archive_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE media_sources SET config=config||jsonb_build_object('cursor','secret-cursor','signed_url','secret-url','folder_ref','drive-folder','folder_name','Drive name') WHERE workspace_id=:ws"
            ),
            dict(ws=str(origin["ws"])),
        )
        rows = await workspaces.list_sources(conn, workspace_id=str(origin["ws"]))
    archive = next(x for x in rows if str(x["id"]) == src["source_id"])
    assert archive["coverage"]["observed"] == archive["coverage"]["represented"] == 1
    assert archive["coverage"]["awaiting_reobservation"] == 0
    assert archive["destination_scope"] == "origin_only" and str(
        archive["origin"]["id"]
    ) == str(origin["iga"])
    assert archive["policy_capabilities"] == {
        "editable": [
            "minimum_age_days",
            "include_credited",
            "include_unknown",
            "carousel_slides",
        ]
    }
    assert archive["policy"]["policy"]["carousel_slides"] == "all"
    assert archive["display_name"]
    drive = next(x for x in rows if str(x["id"]) == str(origin["src"]))
    assert (
        drive["folder_ref"] == "drive-folder" and drive["folder_name"] == "Drive name"
    )
    assert drive["destination_scope"] == "workspace_wide" and drive["coverage"] is None
    import json

    projection = json.dumps(rows, default=str)
    for secret in (
        "secret-cursor",
        "secret-url",
        "test-ciphertext",
        "encrypted_payload",
        "parent_media_id",
        "permalink",
        "credential_status",
    ):
        assert secret not in projection
    await put(archive_engine, origin, src["source_id"], update_v2(slides="first"))
    async with uow(archive_engine, origin) as conn:
        changed = next(
            x
            for x in await workspaces.list_sources(conn, workspace_id=str(origin["ws"]))
            if str(x["id"]) == src["source_id"]
        )
    assert changed["policy"]["policy"]["carousel_slides"] == "first"
    async with uow(archive_engine, origin) as conn:
        await conn.execute(
            text("UPDATE media_sources SET last_sync_success_at=now() WHERE id=:sid"),
            dict(sid=src["source_id"]),
        )
    g2 = await foundation.ensure(archive_engine, origin, foundation.authorization())
    async with uow(archive_engine, origin) as conn:
        archive = next(
            x
            for x in await workspaces.list_sources(conn, workspace_id=str(origin["ws"]))
            if str(x["id"]) == src["source_id"]
        )
    assert archive["coverage"]["observed"] is None
    assert archive["coverage"]["represented"] is None
    assert archive["coverage"]["awaiting_reobservation"] == 1
    assert str(archive["coverage"]["generation_id"]) == g2["generation_id"]
    assert archive["coverage"]["oldest_observed_at"] is None


async def test_source_projection_batches_policy_and_coverage_in_one_query(
    archive_engine, origin
):
    from src.services.target import workspaces

    await foundation.ensure(archive_engine, origin, foundation.authorization())

    class ObservedExecutor:
        def __init__(self, conn):
            self.conn = conn
            self.calls = 0

        async def execute(self, *args, **kw):
            self.calls += 1
            return await self.conn.execute(*args, **kw)

    async with uow(archive_engine, origin) as conn:
        executor = ObservedExecutor(conn)
        rows = await workspaces.list_sources(executor, workspace_id=str(origin["ws"]))
        assert executor.calls == 1 and len(rows) == 2
        assert (
            next(x for x in rows if x["provider"] == "instagram_archive")["coverage"][
                "observed"
            ]
            is None
        )
        assert (
            next(x for x in rows if x["provider"] == "instagram_archive")["policy"][
                "policy"
            ]["carousel_slides"]
            == "all"
        )


async def test_policy_http_surface_with_real_tenant_roles_and_membership(
    archive_engine, origin, sync_conn, tmp_path
):
    import json
    import uuid
    import httpx
    from src.api.app import create_app
    from src.api.principal import Principal, current_principal
    from tests.scripts.conftest import seed_workspace_chain

    src = await foundation.ensure(archive_engine, origin, foundation.authorization())
    foreign = seed_workspace_chain(sync_conn, "policy-http-foreign")
    with sync_conn.cursor() as cur:
        cur.execute("INSERT INTO users DEFAULT VALUES RETURNING id")
        member = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role) VALUES(%s,%s,'member')",
            (origin["ws"], member),
        )
    sync_conn.commit()
    app = create_app(engine=archive_engine, env={})
    app.state.archive_capability = foundation.capability()

    def actor(user):
        app.dependency_overrides[current_principal] = lambda: Principal(
            session_id=str(uuid.uuid4()), user_id=str(user)
        )

    actor(origin["user"])
    url = f"/api/v1/workspaces/{origin['ws']}/sources/{src['source_id']}/policy"
    records = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
    ) as client:

        async def record(method, **kw):
            response = await client.request(
                method, url, headers={"Origin": "https://api.example.test"}, **kw
            )
            records.append(
                dict(method=method, status=response.status_code, body=response.json())
            )
            return response

        saved = await record("GET")
        assert saved.status_code == 200 and saved.json()["revision"] == 1
        assert saved.json()["schema_version"] == 2
        assert saved.json()["policy"]["carousel_slides"] == "all"
        written = await record("PUT", json=update(age=0, credited=True))
        assert (
            written.status_code == 200
            and written.json()["policy"]["minimum_age_days"] == 0
        )
        assert written.json()["effective"]["minimum_age_days"] == 31
        conflict = await record("PUT", json=update(age=5))
        assert conflict.status_code == 409 and conflict.json()["current_revision"] == 2
        switched = await record("PUT", json=update_v2(revision=2, slides="first"))
        assert switched.status_code == 200
        assert switched.json()["policy"]["carousel_slides"] == "first"
        preserved = await record("PUT", json=update(revision=3, age=7))
        assert preserved.status_code == 200
        assert preserved.json()["policy"]["carousel_slides"] == "first"
        actor(member)
        assert (await record("GET")).status_code == 200
        assert (await record("PUT", json=update(2))).status_code == 403
        actor(foreign["user"])
        assert (await record("GET")).status_code == 404
        actor(origin["user"])
        app.state.archive_capability = None
        assert (await record("GET")).status_code == 503
    app.dependency_overrides.clear()
    (tmp_path / "p1c-policy-http.json").write_text(json.dumps(records, indent=2) + "\n")
