"""Source scanning authority is independent of candidate eligibility."""

import importlib.util
import pytest
from sqlalchemy import text
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts.test_source_suitability_gate import activate

pytestmark = [pytest.mark.integration]
archive_engine = foundation.archive_engine
origin = foundation.origin


def service():
    assert importlib.util.find_spec("src.services.target.source_read_admission"), (
        "source read authority missing"
    )
    from src.services.target import source_read_admission

    return source_read_admission


async def ready(engine, origin):
    src = await obs.enabled(engine, origin)
    async with obs.uow(engine, origin) as conn:
        await activate(conn, origin["ws"])
    return src


async def capture(engine, origin, src, capability=True):
    async with obs.uow(engine, origin) as conn:
        return await service().capture_source_read(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            capability=foundation.capability() if capability else None,
        )


async def test_empty_on_archive_admits_without_item_or_pool(archive_engine, origin):
    service()
    src = await ready(archive_engine, origin)
    c = await capture(archive_engine, origin, src)
    assert c.generation_id == src["generation_id"] and c.credential_id == str(
        origin["credential"]
    )
    assert c.source_use_epoch > 0


@pytest.mark.parametrize(
    "sql,reason",
    [
        (
            "UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid",
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
            "UPDATE oauth_credentials SET expires_at=clock_timestamp()-interval '1 second' WHERE id=:credential",
            "credential_unavailable",
        ),
        (
            "UPDATE instagram_archive_generations SET lifecycle='deletion_pending' WHERE id=:gid",
            "generation_unavailable",
        ),
        # 144: a generation held for review is unavailable, not destroyed.
        (
            "UPDATE instagram_archive_generations SET lifecycle='review_hold' WHERE id=:gid",
            "generation_unavailable",
        ),
        (
            "UPDATE media_source_destinations SET state='retired' WHERE source_id=:sid",
            "origin_unavailable",
        ),
        (
            "UPDATE workspaces SET source_mix_conversion_version=2 WHERE id=:ws",
            "archive_not_activated",
        ),
        (
            "UPDATE workspaces SET source_mix_revision=NULL WHERE id=:ws",
            "archive_not_activated",
        ),
        (
            "UPDATE workspaces SET source_mix_mode=NULL WHERE id=:ws",
            "archive_not_activated",
        ),
    ],
)
async def test_current_authority_refuses_closed(archive_engine, origin, sql, reason):
    s = service()
    src = await ready(archive_engine, origin)
    async with obs.uow(archive_engine, origin) as conn:
        await conn.execute(
            text(sql),
            dict(
                sid=src["source_id"],
                gid=src["generation_id"],
                credential=str(origin["credential"]),
                ws=str(origin["ws"]),
            ),
        )
    with pytest.raises(s.SourceReadRefused) as refused:
        await capture(archive_engine, origin, src)
    assert (
        refused.value.reason == reason and refused.value.source_id == src["source_id"]
    )


async def test_feature_gate_and_drive_off_control(archive_engine, origin):
    s = service()
    src = await ready(archive_engine, origin)
    with pytest.raises(s.SourceReadRefused, match="archive_not_activated"):
        await capture(archive_engine, origin, src, False)
    async with obs.uow(archive_engine, origin) as conn:
        drive = await s.capture_source_read(
            conn, workspace_id=str(origin["ws"]), source_id=str(origin["src"])
        )
    assert drive.provider == "gdrive" and drive.generation_id is None


async def test_off_on_and_reused_credential_g2_reject_old_capture(
    archive_engine, origin
):
    s = service()
    src = await ready(archive_engine, origin)
    old = await capture(archive_engine, origin, src)
    from src.services.target import source_state

    for ratio in (0, 1):
        async with obs.uow(archive_engine, origin) as conn:
            await source_state.lock_all_workspace_sources(
                conn, workspace_id=str(origin["ws"])
            )
            before = await source_state.archive_state(
                conn, workspace_id=str(origin["ws"]), source_id=src["source_id"]
            )
            await conn.execute(
                text(
                    "UPDATE category_post_case_mix SET ratio=:ratio WHERE source_id=:sid"
                ),
                dict(ratio=ratio, sid=src["source_id"]),
            )
            await source_state.apply_archive_transition(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=src["source_id"],
                before=before,
                reason="test",
            )
    async with obs.uow(archive_engine, origin) as conn:
        with pytest.raises(s.SourceReadRefused, match="source_epoch_changed"):
            await s.check_source_read(conn, old, capability=foundation.capability())
    current = await capture(archive_engine, origin, src)
    g2 = await foundation.ensure(archive_engine, origin, foundation.authorization())
    assert g2["generation_id"] != current.generation_id
    async with obs.uow(archive_engine, origin) as conn:
        with pytest.raises(s.SourceReadRefused):
            await s.check_source_read(conn, current, capability=foundation.capability())


async def test_request_hook_requires_original_capture_and_commits_each_check(
    archive_engine, origin
):
    s = service()
    from src.services.target.instagram_archive_adapter import ArchiveAccess
    from src.services.target.media_adapters import MediaSourceFenceDrop

    src = await ready(archive_engine, origin)
    c = await capture(archive_engine, origin, src)
    provider = s.admission_provider(archive_engine, capability=foundation.capability())
    with pytest.raises(MediaSourceFenceDrop) as refused:
        await provider(
            src["source_id"],
            workspace_id=str(origin["ws"]),
            access=ArchiveAccess(c.account_ref, "unused"),
        )
    assert refused.value.reason == "source_capture_missing"
    hook = await provider(
        src["source_id"],
        workspace_id=str(origin["ws"]),
        access=ArchiveAccess(c.account_ref, "unused", c),
    )
    ticket = await hook.before_dispatch()
    await hook.after_return(ticket)
    async with obs.uow(archive_engine, origin) as conn:
        await conn.execute(
            text("UPDATE category_post_case_mix SET ratio=0 WHERE source_id=:sid"),
            dict(sid=src["source_id"]),
        )
    with pytest.raises(MediaSourceFenceDrop) as refused:
        await hook.after_return(ticket)
    assert refused.value.reason == "source_off"
