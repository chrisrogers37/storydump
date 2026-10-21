"""Lifecycle composition with a real SQL allocation fixture, under runtime roles.

The small writer uses exactly two enabled Drive fixtures (70/30), plus one
past-posts source, so it proves transaction composition without reimplementing the
production ratio converter and protocol.
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.scripts import test_instagram_observations_gate as obs
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts import test_source_read_admission_gate as admission
from tests.scripts import test_source_suitability_gate as suitability
from tests.scripts.test_instagram_sources_gate import (
    UNARMED,
    UNTOUCHED,
    assert_armed,
    authorization,
    capability,
    clock_fields,
    ensure,
    stamp,
)
from tests.scripts.test_instagram_observations_gate import uow, row

pytestmark = [pytest.mark.integration]
past_posts_engine = foundation.past_posts_engine
origin = foundation.origin


async def mix_fixture(engine, chain):
    async with uow(engine, chain) as conn:
        await conn.execute(
            text(
                "UPDATE workspaces SET source_mix_revision=8, source_mix_mode='custom', source_mix_policy_version=2, source_membership_policy='explicit', source_mix_conversion_version=1, source_mix_converted_at=now()"
            )
        )
        second = (
            await conn.execute(
                text(
                    "INSERT INTO media_sources (workspace_id, provider, config) VALUES (:ws, 'gdrive', '{}') RETURNING id"
                ),
                {"ws": str(chain["ws"])},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO category_post_case_mix (workspace_id, source_id, category, ratio) VALUES (:ws, :one, 'One', 0.70), (:ws, :two, 'Two', 0.30)"
            ),
            {"ws": str(chain["ws"]), "one": str(chain["src"]), "two": str(second)},
        )
    return (str(chain["src"]), str(second))


async def sql_allocator(conn, request):
    from src.services.target.source_membership import AllocationResult

    # Tests exercise actual SQL writes in the caller's transaction. The fixture
    # skips precision/general allocation; it distributes .50 across two survivors.
    await conn.execute(
        text(
            "UPDATE category_post_case_mix SET ratio=:value WHERE source_id=:sid AND effective_to IS NULL"
        ),
        {
            "sid": request.source_id,
            "value": Decimal("0.20") if request.enabled else Decimal(0),
        },
    )
    if request.reset_to_equal:
        enabled = (
            (
                await conn.execute(
                    text(
                        "SELECT source_id FROM category_post_case_mix WHERE effective_to IS NULL AND ratio>0 ORDER BY source_id"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(enabled) in (1, 2)
        ratio = Decimal(1) / len(enabled)
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio=:ratio WHERE effective_to IS NULL AND ratio>0"
            ),
            {"ratio": ratio},
        )
        await conn.execute(text("UPDATE workspaces SET source_mix_mode='equal'"))
    result = await row(
        conn,
        "UPDATE workspaces SET source_mix_revision=source_mix_revision+1 WHERE id=:ws AND source_mix_revision=:expected RETURNING source_mix_revision, source_mix_mode",
        ws=request.workspace_id,
        expected=request.expected_revision,
    )
    return AllocationResult(
        revision=result["source_mix_revision"], mode=result["source_mix_mode"]
    )


async def connect(
    engine,
    chain,
    *,
    choice=None,
    had_prior_grant=False,
    allocator=sql_allocator,
    event=None,
):
    from src.services.target.source_membership import connect_past_posts

    async with uow(engine, chain) as conn:
        return await connect_past_posts(
            conn,
            workspace_id=str(chain["ws"]),
            origin_ig_account_id=str(chain["iga"]),
            authorization=event or authorization(),
            had_prior_grant=had_prior_grant,
            choice=choice,
            allocation_writer=allocator,
            capability=capability(),
        )


async def snapshot(engine, chain):
    async with uow(engine, chain) as conn:
        ws = await row(
            conn,
            "SELECT source_mix_revision, source_mix_mode FROM workspaces WHERE id=:ws",
            ws=str(chain["ws"]),
        )
        mix = (
            await conn.execute(
                text(
                    "SELECT source_id, ratio FROM category_post_case_mix WHERE effective_to IS NULL ORDER BY source_id"
                )
            )
        ).all()
        return ws, {str(sid): ratio for sid, ratio in mix}


@pytest.mark.parametrize("was_enabled", [False, True])
async def test_removal_resets_only_changed_enabled_membership_and_reconnect_preserves_off(
    past_posts_engine, origin, was_enabled
):
    from src.services.target.source_membership import retire_past_posts

    drives = await mix_fixture(past_posts_engine, origin)
    src = await ensure(past_posts_engine, origin, authorization())
    if was_enabled:
        async with uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text(
                    "UPDATE category_post_case_mix SET ratio=.20 WHERE source_id=:sid"
                ),
                {"sid": src["source_id"]},
            )
    async with uow(past_posts_engine, origin) as conn:
        first = await retire_past_posts(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            expected_mix_revision=8,
            allocation_writer=sql_allocator,
            capability=capability(),
        )
        repeat = await retire_past_posts(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            expected_mix_revision=9,
            allocation_writer=None,
            capability=capability(),
        )
        assert repeat.source_use_epoch == first.source_use_epoch
    ws, mix = await snapshot(past_posts_engine, origin)
    assert ws["source_mix_revision"] == 9
    assert mix[src["source_id"]] == 0
    assert ws["source_mix_mode"] == ("equal" if was_enabled else "custom")
    assert [mix[d] for d in drives] == (
        [Decimal(".50"), Decimal(".50")]
        if was_enabled
        else [Decimal(".70"), Decimal(".30")]
    )
    async with uow(past_posts_engine, origin) as conn:
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "active"
        assert (await row(conn, "SELECT state FROM media_source_destinations"))[
            "state"
        ] == "retired"
    restored = await connect(past_posts_engine, origin)
    assert restored.outcome == "reconnected_preserved" and not restored.history_enabled
    ws2, mix2 = await snapshot(past_posts_engine, origin)
    assert mix2 == mix and ws2 == {**ws, "source_mix_revision": 10}
    async with uow(past_posts_engine, origin) as conn:
        assert (await row(conn, "SELECT state FROM media_source_destinations"))[
            "state"
        ] == "active"
        assert (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar_one() == 0


@pytest.mark.parametrize(
    "prior,expected,enabled,outcome",
    [
        (False, 8, True, "connected_history_on"),
        (False, 7, False, "connected_history_off_mix_conflict"),
        (True, 8, False, "connected_history_off"),
    ],
)
async def test_connection_choice_applies_only_new_grant_and_current_preview(
    past_posts_engine, origin, prior, expected, enabled, outcome
):
    from src.services.target.source_membership import PastPostsChoice

    drives = await mix_fixture(past_posts_engine, origin)
    if enabled:
        async with uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text(
                    "UPDATE category_post_case_mix SET ratio=CASE WHEN source_id=:one THEN 1 ELSE 0 END"
                ),
                {"one": drives[0]},
            )
    result = await connect(
        past_posts_engine,
        origin,
        choice=PastPostsChoice(True, expected),
        had_prior_grant=prior,
    )
    assert result.history_enabled is enabled and result.outcome == outcome
    assert result.mix_revision == 9 and result.mix_reset_applied is enabled
    ws, mix = await snapshot(past_posts_engine, origin)
    if not enabled:
        assert ws["source_mix_mode"] == "custom"
        assert [mix[d] for d in drives] == [Decimal(".70"), Decimal(".30")]
    async with uow(past_posts_engine, origin) as conn:
        assert (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar_one() == 0


async def test_off_creation_preserves_custom_and_replay_preserves_latest_choice(
    past_posts_engine, origin
):
    from src.services.target.source_membership import PastPostsChoice

    drives = await mix_fixture(past_posts_engine, origin)
    event = authorization()
    first = await connect(
        past_posts_engine, origin, choice=PastPostsChoice(False), event=event
    )
    second = await connect(
        past_posts_engine,
        origin,
        choice=PastPostsChoice(True, 8),
        event=event,
        allocator=None,
    )
    assert first.source_id == second.source_id and not second.history_enabled
    ws, mix = await snapshot(past_posts_engine, origin)
    assert ws["source_mix_revision"] == 9 and ws["source_mix_mode"] == "custom"
    assert [mix[d] for d in drives] == [Decimal(".70"), Decimal(".30")]


@pytest.mark.parametrize("mode", ["missing", "fails_after_write"])
async def test_caught_allocation_failure_rolls_back_complete_creation_and_allows_retry(
    past_posts_engine, origin, mode
):
    from src.services.target.source_membership import (
        PastPostsChoice,
        connect_past_posts,
    )
    from src.services.target.instagram_sources import PastPostsRefused

    await mix_fixture(past_posts_engine, origin)

    async def failing(conn, request):
        await sql_allocator(conn, request)
        raise PastPostsRefused("allocation_failed")

    async with uow(past_posts_engine, origin) as conn:
        with pytest.raises(PastPostsRefused):
            await connect_past_posts(
                conn,
                workspace_id=str(origin["ws"]),
                origin_ig_account_id=str(origin["iga"]),
                authorization=authorization(),
                had_prior_grant=False,
                choice=PastPostsChoice(False),
                allocation_writer=None if mode == "missing" else failing,
                capability=capability(),
            )
        await conn.execute(text("UPDATE workspaces SET name='continued'"))
    async with uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM media_sources WHERE provider='instagram_past_posts'"
                )
            )
        ).scalar_one() == 0
        assert (
            await conn.execute(
                text("SELECT count(*) FROM instagram_past_posts_generations")
            )
        ).scalar_one() == 0
        assert (
            await row(conn, "SELECT name, source_mix_revision FROM workspaces")
        ) == {"name": "continued", "source_mix_revision": 8}
    result = await connect(past_posts_engine, origin, choice=PastPostsChoice(False))
    assert result.mix_revision == 9


async def test_caught_allocator_failure_rolls_back_retirement(
    past_posts_engine, origin
):
    from src.services.target.source_membership import retire_past_posts
    from src.services.target.instagram_sources import PastPostsRefused

    await mix_fixture(past_posts_engine, origin)
    src = await ensure(past_posts_engine, origin, authorization())

    async def failing(conn, request):
        await sql_allocator(conn, request)
        raise PastPostsRefused("allocation_failed")

    async with uow(past_posts_engine, origin) as conn:
        with pytest.raises(PastPostsRefused, match="allocation_failed"):
            await retire_past_posts(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=src["source_id"],
                expected_mix_revision=8,
                allocation_writer=failing,
                capability=capability(),
            )
    async with uow(past_posts_engine, origin) as conn:
        source = await row(
            conn,
            "SELECT state, config, source_use_epoch FROM media_sources WHERE id=:sid",
            sid=src["source_id"],
        )
        assert source["state"] == "active" and not source["config"].get("removed")
        assert source["source_use_epoch"] == src["source_use_epoch"]
        assert (await row(conn, "SELECT state FROM media_source_destinations"))[
            "state"
        ] == "active"
        assert (await row(conn, "SELECT source_mix_revision FROM workspaces"))[
            "source_mix_revision"
        ] == 8


async def test_frozen_old_generation_fence_does_not_revoke_independent_current_generation(
    past_posts_engine, origin
):
    from src.services.target.source_membership import fence_generations

    old = await ensure(past_posts_engine, origin, authorization())
    current = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        result = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=old["source_id"],
            generation_ids=frozenset([old["generation_id"]]),
            reason="deletion",
            capability=capability(),
        )
        assert result.source_use_epoch == current["source_use_epoch"]
        assert result.changed_generation_ids == (old["generation_id"],)
    async with uow(past_posts_engine, origin) as conn:
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "active"
        assert (
            await row(
                conn,
                "SELECT lifecycle FROM instagram_past_posts_generations WHERE id=:gid",
                gid=current["generation_id"],
            )
        )["lifecycle"] == "active"
        result = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=old["source_id"],
            generation_ids=frozenset([old["generation_id"]]),
            reason="deletion",
            capability=capability(),
        )
        assert not result.changed_generation_ids


async def test_expiry_changes_availability_without_membership_or_mix_reset(
    past_posts_engine, origin
):
    from src.services.target.source_membership import expire_past_posts_credential

    await mix_fixture(past_posts_engine, origin)
    src = await ensure(past_posts_engine, origin, authorization())
    before = await snapshot(past_posts_engine, origin)
    async with uow(past_posts_engine, origin) as conn:
        first = await expire_past_posts_credential(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            generation_id=src["generation_id"],
            capability=capability(),
        )
    async with uow(past_posts_engine, origin) as conn:
        second = await expire_past_posts_credential(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            generation_id=src["generation_id"],
            capability=capability(),
        )
        assert second.source_use_epoch == first.source_use_epoch
    assert await snapshot(past_posts_engine, origin) == before


async def test_account_removal_retires_without_deleting_source_or_credential(
    past_posts_engine, origin
):
    from src.services.target.source_membership import remove_past_posts_account

    await mix_fixture(past_posts_engine, origin)
    src = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        result = await remove_past_posts_account(
            conn,
            workspace_id=str(origin["ws"]),
            origin_ig_account_id=str(origin["iga"]),
            expected_mix_revision=8,
            allocation_writer=sql_allocator,
            capability=capability(),
        )
        assert result.source_id == src["source_id"]
    async with uow(past_posts_engine, origin) as conn:
        assert (await row(conn, "SELECT state FROM ig_accounts"))["state"] == "disabled"
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "active"


async def test_enable_requires_preview_and_bumps_once_per_composed_transaction(
    past_posts_engine, origin
):
    from src.services.target.source_membership import set_past_posts_enabled
    from src.services.target.instagram_sources import PastPostsRefused

    await mix_fixture(past_posts_engine, origin)
    src = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio=CASE WHEN source_id=:one THEN 1 ELSE 0 END"
            ),
            {"one": str(origin["src"])},
        )
        with pytest.raises(PastPostsRefused, match="mix_conflict"):
            await set_past_posts_enabled(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=src["source_id"],
                enabled=True,
                expected_mix_revision=7,
                allocation_writer=sql_allocator,
                capability=capability(),
            )
        with pytest.raises(PastPostsRefused, match="invalid_past_posts_choice"):
            await set_past_posts_enabled(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=src["source_id"],
                enabled=True,
                expected_mix_revision=None,
                allocation_writer=sql_allocator,
                capability=capability(),
            )
        result = await set_past_posts_enabled(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            enabled=True,
            expected_mix_revision=8,
            allocation_writer=sql_allocator,
            capability=capability(),
        )
        again = await set_past_posts_enabled(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            enabled=False,
            expected_mix_revision=9,
            allocation_writer=sql_allocator,
            capability=capability(),
        )
        assert (
            result.source_use_epoch
            == again.source_use_epoch
            == src["source_use_epoch"] + 1
        )


async def test_replayed_authorization_does_not_restore_retired_source_or_advance_mix(
    past_posts_engine, origin
):
    from src.services.target.source_membership import PastPostsChoice, retire_past_posts

    await mix_fixture(past_posts_engine, origin)
    event = authorization()
    source = await connect(
        past_posts_engine, origin, choice=PastPostsChoice(False), event=event
    )
    async with uow(past_posts_engine, origin) as conn:
        await retire_past_posts(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source.source_id,
            expected_mix_revision=9,
            allocation_writer=sql_allocator,
            capability=capability(),
        )
    before = await snapshot(past_posts_engine, origin)
    # A replay carries no independently authorized new generation and cannot
    # count as restoration, even if a callback caller is accidentally retried.
    await connect(past_posts_engine, origin, event=event, allocator=None)
    assert await snapshot(past_posts_engine, origin) == before


async def one_drive_carries_the_mix(engine, chain, drives):
    """The mix the 50/50 fixture allocator can extend by one more source."""
    async with uow(engine, chain) as conn:
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio=CASE WHEN source_id=:one THEN 1 ELSE 0 END"
            ),
            {"one": drives[0]},
        )


async def turn(engine, chain, source_id, enabled):
    """The mix editor's switch, at the mix's current revision."""
    from src.services.target.source_membership import set_past_posts_enabled

    revision = (await snapshot(engine, chain))[0]["source_mix_revision"]
    async with uow(engine, chain) as conn:
        return await set_past_posts_enabled(
            conn,
            workspace_id=str(chain["ws"]),
            source_id=source_id,
            enabled=enabled,
            expected_mix_revision=revision,
            allocation_writer=sql_allocator,
            capability=capability(),
        )


async def job_count(engine, chain):
    async with uow(engine, chain) as conn:
        return (await conn.execute(text("SELECT count(*) FROM jobs"))).scalar_one()


@pytest.mark.parametrize(
    "prior,expected,enabled,outcome",
    [
        (False, 8, True, "connected_history_on"),
        (False, 7, False, "connected_history_off_mix_conflict"),
        (True, 8, False, "connected_history_off"),
    ],
)
async def test_connecting_arms_the_first_read_only_for_a_source_that_ends_on(
    past_posts_engine, origin, prior, expected, enabled, outcome
):
    from src.services.target.source_membership import PastPostsChoice

    drives = await mix_fixture(past_posts_engine, origin)
    if enabled:
        await one_drive_carries_the_mix(past_posts_engine, origin, drives)
    result = await connect(
        past_posts_engine,
        origin,
        choice=PastPostsChoice(True, expected),
        had_prior_grant=prior,
    )
    assert (result.outcome, result.history_enabled) == (outcome, enabled)
    fields = await clock_fields(past_posts_engine, origin, result.source_id)
    if enabled:
        assert_armed(fields)
    else:
        assert fields == UNARMED
    # An arm only says when the clock may look: it mints nothing itself.
    assert await job_count(past_posts_engine, origin) == 0


async def test_turning_a_source_on_arms_its_read_and_turning_it_off_leaves_the_clock_alone(
    past_posts_engine, origin
):
    drives = await mix_fixture(past_posts_engine, origin)
    await one_drive_carries_the_mix(past_posts_engine, origin, drives)
    source = (await ensure(past_posts_engine, origin, authorization()))["source_id"]
    assert await clock_fields(past_posts_engine, origin, source) == UNARMED

    assert (await turn(past_posts_engine, origin, source, True)).history_enabled
    assert_armed(await clock_fields(past_posts_engine, origin, source))

    # A finished read leaves its stamp, and Off neither clears it nor arms.
    await stamp(past_posts_engine, origin, source)
    assert not (await turn(past_posts_engine, origin, source, False)).history_enabled
    assert await clock_fields(past_posts_engine, origin, source) == UNTOUCHED

    # On again: the stamp is replaced, and the clock may look.
    assert (await turn(past_posts_engine, origin, source, True)).history_enabled
    assert_armed(await clock_fields(past_posts_engine, origin, source))
    assert await job_count(past_posts_engine, origin) == 0


@pytest.mark.parametrize("dead_credential", [False, True])
async def test_reauthorizing_an_on_source_arms_a_read_for_its_new_generation(
    past_posts_engine, origin, dead_credential
):
    from src.services.target.source_membership import (
        PastPostsChoice,
        expire_past_posts_credential,
    )

    drives = await mix_fixture(past_posts_engine, origin)
    await one_drive_carries_the_mix(past_posts_engine, origin, drives)
    first = await connect(past_posts_engine, origin, choice=PastPostsChoice(True, 8))
    assert first.history_enabled
    source = first.source_id

    async def generation():
        async with uow(past_posts_engine, origin) as conn:
            return (
                await row(
                    conn,
                    "SELECT current_authorization_generation_id::text AS gid FROM media_sources WHERE id=:sid",
                    sid=source,
                )
            )["gid"]

    old = await generation()
    await stamp(past_posts_engine, origin, source)
    if dead_credential:
        async with uow(past_posts_engine, origin) as conn:
            await expire_past_posts_credential(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=source,
                generation_id=old,
                capability=capability(),
            )
        # The credential's death arms nothing; the reconnect lands a live one.
        assert await clock_fields(past_posts_engine, origin, source) == UNTOUCHED
        async with uow(past_posts_engine, origin) as conn:
            await conn.execute(text("UPDATE oauth_credentials SET state='active'"))
    again = await connect(past_posts_engine, origin)
    assert (again.outcome, again.history_enabled) == ("reconnected_preserved", True)
    assert await generation() != old
    assert_armed(await clock_fields(past_posts_engine, origin, source))
    assert await job_count(past_posts_engine, origin) == 0


async def test_a_replayed_authorization_leaves_an_on_sources_read_clock_alone(
    past_posts_engine, origin
):
    from src.services.target.source_membership import PastPostsChoice

    drives = await mix_fixture(past_posts_engine, origin)
    await one_drive_carries_the_mix(past_posts_engine, origin, drives)
    event = authorization()
    first = await connect(
        past_posts_engine, origin, choice=PastPostsChoice(True, 8), event=event
    )
    assert first.history_enabled
    await stamp(past_posts_engine, origin, first.source_id)
    replay = await connect(past_posts_engine, origin, event=event, allocator=None)
    assert replay.history_enabled and replay.source_use_epoch == first.source_use_epoch
    assert await clock_fields(past_posts_engine, origin, first.source_id) == UNTOUCHED
    # A new authorization of the same source is a change, and arms.
    await connect(past_posts_engine, origin)
    assert_armed(await clock_fields(past_posts_engine, origin, first.source_id))


async def test_removing_a_source_arms_nothing_and_a_restore_waits_for_the_switch(
    past_posts_engine, origin
):
    from src.services.target.source_membership import PastPostsChoice, retire_past_posts

    drives = await mix_fixture(past_posts_engine, origin)
    await one_drive_carries_the_mix(past_posts_engine, origin, drives)
    first = await connect(past_posts_engine, origin, choice=PastPostsChoice(True, 8))
    source = first.source_id
    assert_armed(await clock_fields(past_posts_engine, origin, source))
    await stamp(past_posts_engine, origin, source)

    revision = (await snapshot(past_posts_engine, origin))[0]["source_mix_revision"]
    async with uow(past_posts_engine, origin) as conn:
        retired = await retire_past_posts(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source,
            expected_mix_revision=revision,
            allocation_writer=sql_allocator,
            capability=capability(),
        )
    assert retired.source_use_epoch == first.source_use_epoch + 1
    assert await clock_fields(past_posts_engine, origin, source) == UNTOUCHED

    # Restored, it comes back Off: a new generation, but nothing to read yet.
    restored = await connect(past_posts_engine, origin)
    assert (restored.outcome, restored.history_enabled) == (
        "reconnected_preserved",
        False,
    )
    assert await clock_fields(past_posts_engine, origin, source) == UNTOUCHED

    # Switched On, it is armed.
    assert (await turn(past_posts_engine, origin, source, True)).history_enabled
    assert_armed(await clock_fields(past_posts_engine, origin, source))
    assert await job_count(past_posts_engine, origin) == 0


async def test_allocator_receives_all_source_and_origin_prefix_locks(
    past_posts_engine, origin
):
    from src.services.target import source_state
    from src.services.target.source_membership import PastPostsChoice

    drives = await mix_fixture(past_posts_engine, origin)

    async def checked(conn, request):
        for sid in (*drives, request.source_id):
            source_state.require_source_lock(conn, str(origin["ws"]), sid)
        return await sql_allocator(conn, request)

    await connect(
        past_posts_engine, origin, choice=PastPostsChoice(False), allocator=checked
    )


async def test_current_generation_revocation_fences_once_without_rewriting_membership(
    past_posts_engine, origin
):
    from src.services.target.source_membership import fence_generations

    await mix_fixture(past_posts_engine, origin)
    source = await ensure(past_posts_engine, origin, authorization())
    saved = await snapshot(past_posts_engine, origin)
    async with uow(past_posts_engine, origin) as conn:
        first = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            reason="revocation",
            capability=capability(),
        )
    async with uow(past_posts_engine, origin) as conn:
        second = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            reason="revocation",
            capability=capability(),
        )
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "revoked"
        assert (
            second.source_use_epoch
            == first.source_use_epoch
            == source["source_use_epoch"] + 1
        )
    assert await snapshot(past_posts_engine, origin) == saved


@pytest.mark.parametrize("version", [True, 2])
async def test_connection_refuses_noninteger_or_unsupported_preview_before_writes(
    past_posts_engine, origin, version
):
    from src.services.target.source_membership import PastPostsChoice
    from src.services.target.instagram_sources import PastPostsRefused

    await mix_fixture(past_posts_engine, origin)
    with pytest.raises(PastPostsRefused, match="invalid_past_posts_choice"):
        await connect(
            past_posts_engine,
            origin,
            choice=PastPostsChoice(False, preview_version=version),
        )
    async with uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM media_sources WHERE provider='instagram_past_posts'"
                )
            )
        ).scalar_one() == 0


@pytest.mark.parametrize("lifecycle", ["deletion_pending", "deleted", "superseded"])
async def test_revocation_owns_current_authorization_after_lifecycle_fence(
    past_posts_engine, origin, lifecycle
):
    from src.services.target.source_membership import fence_generations

    await mix_fixture(past_posts_engine, origin)
    source = await ensure(past_posts_engine, origin, authorization())
    saved = await snapshot(past_posts_engine, origin)
    async with uow(past_posts_engine, origin) as conn:
        deleted = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            reason="deletion",
            capability=capability(),
        )
        if lifecycle != "deletion_pending":
            await conn.execute(
                text(
                    "UPDATE instagram_past_posts_generations SET lifecycle=:state WHERE id=:gid"
                ),
                {"state": lifecycle, "gid": source["generation_id"]},
            )
    async with uow(past_posts_engine, origin) as conn:
        audits_before = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                )
            )
        ).scalar_one()
        revoked = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            reason="revocation",
            capability=capability(),
        )
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "revoked"
        assert revoked.source_use_epoch == deleted.source_use_epoch + 1
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                )
            )
        ).scalar_one() == audits_before + 1
    async with uow(past_posts_engine, origin) as conn:
        repeat = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            reason="revocation",
            capability=capability(),
        )
        assert repeat.source_use_epoch == revoked.source_use_epoch
        assert not repeat.changed_generation_ids
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                )
            )
        ).scalar_one() == audits_before + 1
    assert await snapshot(past_posts_engine, origin) == saved


@pytest.mark.parametrize(
    "lifecycle", ["active", "deletion_pending", "deleted", "superseded"]
)
@pytest.mark.parametrize("authorization_time", ["tied", "backward"])
async def test_stale_revocation_and_replay_preserve_later_authorization_even_when_fenced(
    past_posts_engine, origin, lifecycle, authorization_time
):
    from dataclasses import replace
    from datetime import timedelta
    from src.services.target.instagram_sources import ensure_past_posts_source
    from src.services.target.source_membership import fence_generations

    old_event = authorization()
    new_event = replace(
        authorization(),
        authorized_at=old_event.authorized_at
        - (timedelta(days=1) if authorization_time == "backward" else timedelta(0)),
    )
    # Same transaction ties created_at; both grants reuse the same credential row.
    async with uow(past_posts_engine, origin) as conn:
        kw = dict(
            workspace_id=str(origin["ws"]),
            origin_ig_account_id=str(origin["iga"]),
            capability=capability(),
        )
        old = await ensure_past_posts_source(conn, authorization=old_event, **kw)
        current = await ensure_past_posts_source(conn, authorization=new_event, **kw)
        generations = (
            await conn.execute(
                text(
                    "SELECT credential_id, created_at FROM instagram_past_posts_generations"
                )
            )
        ).all()
        assert len(generations) == 2 and generations[0] == generations[1]
        if lifecycle != "active":
            await fence_generations(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=current["source_id"],
                generation_ids=frozenset([current["generation_id"]]),
                reason="deletion",
                capability=capability(),
            )
            if lifecycle != "deletion_pending":
                await conn.execute(
                    text(
                        "UPDATE instagram_past_posts_generations SET lifecycle=:state WHERE id=:gid"
                    ),
                    {"state": lifecycle, "gid": current["generation_id"]},
                )
    async with uow(past_posts_engine, origin) as conn:
        before = await row(
            conn,
            "SELECT source_use_epoch FROM media_sources WHERE id=:sid",
            sid=current["source_id"],
        )
        await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=old["source_id"],
            generation_ids=frozenset([old["generation_id"]]),
            reason="revocation",
            capability=capability(),
        )
        # Replaying G1 cannot make it own G2's credential, including fenced G2.
        await ensure_past_posts_source(conn, authorization=old_event, **kw)
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "active"
        assert (
            await row(
                conn,
                "SELECT source_use_epoch FROM media_sources WHERE id=:sid",
                sid=current["source_id"],
            )
        ) == before
        assert (
            await row(
                conn,
                "SELECT lifecycle FROM instagram_past_posts_generations WHERE id=:gid",
                gid=current["generation_id"],
            )
        )["lifecycle"] == lifecycle
        # The matching current scope still owns and can revoke the credential.
        await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=current["source_id"],
            generation_ids=frozenset([current["generation_id"]]),
            reason="revocation",
            capability=capability(),
        )
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "revoked"


async def test_caught_restore_failure_preserves_current_authorization_and_retry(
    past_posts_engine, origin
):
    from src.services.target.instagram_sources import PastPostsRefused
    from src.services.target.source_membership import (
        connect_past_posts,
        retire_past_posts,
        fence_generations,
    )

    await mix_fixture(past_posts_engine, origin)
    old_event, new_event = authorization(), authorization()
    source = await ensure(past_posts_engine, origin, old_event)
    async with uow(past_posts_engine, origin) as conn:
        await retire_past_posts(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            expected_mix_revision=8,
            allocation_writer=sql_allocator,
            capability=capability(),
        )

    async def failing(conn, request):
        await sql_allocator(conn, request)
        raise PastPostsRefused("allocation_failed")

    async with uow(past_posts_engine, origin) as conn:
        with pytest.raises(PastPostsRefused, match="allocation_failed"):
            await connect_past_posts(
                conn,
                workspace_id=str(origin["ws"]),
                origin_ig_account_id=str(origin["iga"]),
                authorization=new_event,
                had_prior_grant=True,
                choice=None,
                allocation_writer=failing,
                capability=capability(),
            )
        await conn.execute(text("UPDATE workspaces SET name='continued'"))
    async with uow(past_posts_engine, origin) as conn:
        assert (
            await conn.execute(
                text("SELECT count(*) FROM instagram_past_posts_generations")
            )
        ).scalar_one() == 1
        assert (
            await row(
                conn,
                "SELECT current_authorization_generation_id::text AS gid FROM media_sources WHERE id=:sid",
                sid=source["source_id"],
            )
        )["gid"] == source["generation_id"]
    await ensure(past_posts_engine, origin, old_event)
    restored = await connect(
        past_posts_engine, origin, event=new_event, had_prior_grant=True
    )
    async with uow(past_posts_engine, origin) as conn:
        await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            reason="revocation",
            capability=capability(),
        )
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "active"
        assert (
            await row(
                conn,
                "SELECT current_authorization_generation_id::text AS gid FROM media_sources WHERE id=:sid",
                sid=source["source_id"],
            )
        )["gid"] != source["generation_id"]
        assert restored.mix_revision == 10


async def test_composed_deletion_and_revocation_bump_and_audit_once(
    past_posts_engine, origin
):
    from src.services.target.source_membership import fence_generations

    source = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                )
            )
        ).scalar_one()
        params = dict(
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            capability=capability(),
        )
        deleted = await fence_generations(conn, reason="deletion", **params)
        revoked = await fence_generations(conn, reason="revocation", **params)
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "revoked"
        assert (
            deleted.source_use_epoch
            == revoked.source_use_epoch
            == source["source_use_epoch"] + 1
        )
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE entity_kind='instagram_source'"
                )
            )
        ).scalar_one() == count + 1


async def test_current_authorization_association_is_past_posts_and_source_tenant_scoped(
    past_posts_engine, origin, sync_conn
):
    from sqlalchemy.exc import DBAPIError
    from tests.scripts.test_instagram_catalog_gate import seed_past_posts
    from tests.scripts.conftest import seed_intent_chain, seed_workspace_chain
    from src.services.target.instagram_sources import PastPostsRefused
    from src.services.target.source_membership import fence_generations

    source = await ensure(past_posts_engine, origin, authorization())
    with sync_conn.cursor() as cur:
        same_workspace = seed_past_posts(
            cur,
            {
                "ws": origin["ws"],
                **seed_intent_chain(cur, origin["ws"], "other-origin"),
            },
        )
    sync_conn.commit()
    foreign_chain = seed_workspace_chain(sync_conn, "foreign-authorization")
    with sync_conn.cursor() as cur:
        foreign = seed_past_posts(cur, foreign_chain)
    sync_conn.commit()
    async with uow(past_posts_engine, origin) as conn:
        # Actual runtime UPDATE grants suffice for the association; composite FK
        # rejects a generation from another source or workspace, even if known.
        for gid in (str(same_workspace["generation"]), str(foreign["generation"])):
            with pytest.raises(DBAPIError, match="fk_sources_current_authorization"):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "UPDATE media_sources SET current_authorization_generation_id=:gid WHERE id=:sid"
                        ),
                        {"gid": gid, "sid": source["source_id"]},
                    )
        with pytest.raises(DBAPIError, match="ck_sources_current_authorization"):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "UPDATE media_sources SET current_authorization_generation_id=:gid WHERE id=:sid"
                    ),
                    {"gid": source["generation_id"], "sid": str(origin["src"])},
                )
        hidden = await conn.execute(
            text(
                "UPDATE media_sources SET current_authorization_generation_id=NULL WHERE id=:sid"
            ),
            {"sid": str(foreign["past_posts"])},
        )
        assert hidden.rowcount == 0
        with pytest.raises(
            PastPostsRefused, match="past_posts_generation_scope_unavailable"
        ):
            await fence_generations(
                conn,
                workspace_id=str(foreign["ws"]),
                source_id=str(foreign["past_posts"]),
                generation_ids=frozenset([str(foreign["generation"])]),
                reason="revocation",
                capability=capability(),
            )
        assert (
            await row(
                conn,
                "SELECT state FROM oauth_credentials WHERE id=:id",
                id=str(origin["credential"]),
            )
        )["state"] == "active"
        assert (
            await row(
                conn,
                "SELECT current_authorization_generation_id::text AS gid FROM media_sources WHERE id=:sid",
                sid=source["source_id"],
            )
        )["gid"] == source["generation_id"]


@pytest.mark.parametrize("later_grant", [False, True])
async def test_expiry_owns_current_credential_after_deletion_and_refuses_stale_grant(
    past_posts_engine, origin, later_grant
):
    from src.services.target.instagram_sources import PastPostsRefused
    from src.services.target.source_membership import (
        expire_past_posts_credential,
        fence_generations,
    )

    await mix_fixture(past_posts_engine, origin)
    old = await ensure(past_posts_engine, origin, authorization())
    current = (
        await ensure(past_posts_engine, origin, authorization()) if later_grant else old
    )
    saved = await snapshot(past_posts_engine, origin)
    async with uow(past_posts_engine, origin) as conn:
        deleted = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=current["source_id"],
            generation_ids=frozenset([current["generation_id"]]),
            reason="deletion",
            capability=capability(),
        )
    async with uow(past_posts_engine, origin) as conn:
        params = dict(
            workspace_id=str(origin["ws"]),
            source_id=current["source_id"],
            capability=capability(),
        )
        if later_grant:
            with pytest.raises(PastPostsRefused, match="past_posts_stale_capture"):
                await expire_past_posts_credential(
                    conn, generation_id=old["generation_id"], **params
                )
            assert (await row(conn, "SELECT state FROM oauth_credentials"))[
                "state"
            ] == "active"
        expired = await expire_past_posts_credential(
            conn, generation_id=current["generation_id"], **params
        )
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "expired"
        assert expired.source_use_epoch == deleted.source_use_epoch + 1
    async with uow(past_posts_engine, origin) as conn:
        repeat = await expire_past_posts_credential(
            conn, generation_id=current["generation_id"], **params
        )
        assert repeat.source_use_epoch == expired.source_use_epoch
    assert await snapshot(past_posts_engine, origin) == saved


# Activation gates what opens a past-posts source, never what closes one. After a
# rollback (past-posts rows present, no capability) fences, removal, Off, expiry and
# redaction still work; connecting, turning On and landing an observation still
# refuse. Every closing call below passes NO capability: that is the rollback.


async def fact(engine, chain, sql, **params):
    """One value, as the tenant reads it."""
    async with uow(engine, chain) as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def epoch_of(engine, chain, sid):
    """The source's use epoch as the tenant reads it, not as a helper returned it."""
    return await fact(
        engine,
        chain,
        "SELECT source_use_epoch FROM media_sources WHERE id=:sid",
        sid=sid,
    )


async def generation_states(engine, chain):
    """{generation id: lifecycle} for every past-posts generation of the workspace."""
    async with uow(engine, chain) as conn:
        found = await conn.execute(
            text("SELECT id::text, lifecycle FROM instagram_past_posts_generations")
        )
        return dict(found.all())


async def audit_reasons(engine, chain, sid):
    """The reasons the epoch writer audited for this source, oldest first."""
    async with uow(engine, chain) as conn:
        found = await conn.execute(
            text(
                "SELECT detail->>'reason' FROM audit_events"
                " WHERE entity_kind='instagram_source' AND entity_id=:sid ORDER BY id"
            ),
            {"sid": sid},
        )
        return [reason for (reason,) in found.all()]


@pytest.mark.parametrize(
    "reason,lifecycle,credential",
    [
        ("deletion", "deletion_pending", "active"),
        ("review", "review_hold", "active"),
        ("revocation", "superseded", "revoked"),
    ],
)
async def test_a_rollback_still_fences_a_generation(
    past_posts_engine, origin, reason, lifecycle, credential
):
    from src.services.target.source_membership import fence_generations

    source = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        fenced = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([source["generation_id"]]),
            reason=reason,
        )
    assert fenced.changed_generation_ids == (source["generation_id"],)
    assert fenced.source_use_epoch == source["source_use_epoch"] + 1
    assert await generation_states(past_posts_engine, origin) == {
        source["generation_id"]: lifecycle
    }
    state = await fact(past_posts_engine, origin, "SELECT state FROM oauth_credentials")
    assert state == credential
    assert await audit_reasons(past_posts_engine, origin, source["source_id"]) == [
        "created",
        reason,
    ]


async def test_a_rollback_still_retires_a_past_posts(past_posts_engine, origin):
    from src.services.target.source_membership import retire_past_posts

    await mix_fixture(past_posts_engine, origin)
    source = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        retired = await retire_past_posts(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            expected_mix_revision=8,
            allocation_writer=sql_allocator,
        )
    assert retired.mix_revision == 9
    assert retired.source_use_epoch == source["source_use_epoch"] + 1
    async with uow(past_posts_engine, origin) as conn:
        gone = await row(
            conn,
            "SELECT state, config FROM media_sources WHERE id=:sid",
            sid=source["source_id"],
        )
        assert gone["state"] == "paused" and gone["config"].get("removed") is True
        assert (await row(conn, "SELECT state FROM media_source_destinations"))[
            "state"
        ] == "retired"


async def test_a_rollback_still_removes_a_past_posts_account(past_posts_engine, origin):
    from src.services.target.source_membership import remove_past_posts_account

    await mix_fixture(past_posts_engine, origin)
    source = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        removed = await remove_past_posts_account(
            conn,
            workspace_id=str(origin["ws"]),
            origin_ig_account_id=str(origin["iga"]),
            expected_mix_revision=8,
            allocation_writer=sql_allocator,
        )
    assert removed.source_id == source["source_id"] and removed.mix_revision == 9
    async with uow(past_posts_engine, origin) as conn:
        assert (
            await row(
                conn,
                "SELECT state FROM ig_accounts WHERE id=:id",
                id=str(origin["iga"]),
            )
        )["state"] == "disabled"
        gone = await row(
            conn,
            "SELECT state, config FROM media_sources WHERE id=:sid",
            sid=source["source_id"],
        )
        assert gone["state"] == "paused" and gone["config"].get("removed") is True
        assert (await row(conn, "SELECT state FROM oauth_credentials"))[
            "state"
        ] == "active"


async def test_a_rollback_still_expires_a_credential(past_posts_engine, origin):
    from src.services.target.source_membership import expire_past_posts_credential

    source = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        expired = await expire_past_posts_credential(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_id=source["generation_id"],
        )
    assert expired.source_use_epoch == source["source_use_epoch"] + 1
    state = await fact(past_posts_engine, origin, "SELECT state FROM oauth_credentials")
    assert state == "expired"


async def test_a_rollback_turns_a_past_posts_off_but_never_on(
    past_posts_engine, origin
):
    from src.services.target.instagram_sources import PastPostsRefused
    from src.services.target.source_membership import set_past_posts_enabled

    await mix_fixture(past_posts_engine, origin)
    source = await ensure(past_posts_engine, origin, authorization())
    sid = source["source_id"]
    async with uow(past_posts_engine, origin) as conn:
        await conn.execute(
            text(
                "UPDATE category_post_case_mix SET ratio=CASE WHEN source_id=:one THEN 1 ELSE 0 END"
            ),
            {"one": str(origin["src"])},
        )

    async def switch(enabled, revision, **kw):
        async with uow(past_posts_engine, origin) as conn:
            return await set_past_posts_enabled(
                conn,
                workspace_id=str(origin["ws"]),
                source_id=sid,
                enabled=enabled,
                expected_mix_revision=revision,
                allocation_writer=sql_allocator,
                **kw,
            )

    # Before the rollback the capability is there, and turns the past-posts source On.
    on = await switch(True, 8, capability=capability())
    assert on.history_enabled and on.mix_revision == 9
    # Off closes, so the rollback still turns it off.
    off = await switch(False, 9)
    assert not off.history_enabled and off.mix_revision == 10
    assert off.source_use_epoch == on.source_use_epoch + 1
    saved = await snapshot(past_posts_engine, origin)
    # On opens, so it is refused by name, and so is anything that is not exactly
    # False. Nothing is written.
    for value in (True, "no", None, 0):
        with pytest.raises(PastPostsRefused, match="past_posts_not_activated"):
            await switch(value, 10)
    assert await snapshot(past_posts_engine, origin) == saved
    assert await epoch_of(past_posts_engine, origin, sid) == off.source_use_epoch
    # The control: with the capability back, the same arguments turn it On.
    again = await switch(True, 10, capability=capability())
    assert again.history_enabled and again.mix_revision == 11


async def test_connecting_still_needs_the_capability_and_writes_nothing(
    past_posts_engine, origin
):
    from src.services.target.instagram_sources import PastPostsRefused
    from src.services.target.source_membership import (
        PastPostsChoice,
        connect_past_posts,
    )

    await mix_fixture(past_posts_engine, origin)
    async with uow(past_posts_engine, origin) as conn:
        # The refusal comes first, ahead of any validation of the choice: a
        # deployment without the capability answers one way whatever was sent.
        for choice in (PastPostsChoice(False), PastPostsChoice(True, None), None):
            with pytest.raises(PastPostsRefused, match="past_posts_not_activated"):
                await connect_past_posts(
                    conn,
                    workspace_id=str(origin["ws"]),
                    origin_ig_account_id=str(origin["iga"]),
                    authorization=authorization(),
                    had_prior_grant=False,
                    choice=choice,
                    allocation_writer=sql_allocator,
                )
        await conn.execute(text("UPDATE workspaces SET name='continued'"))
    async with uow(past_posts_engine, origin) as conn:
        sources = await conn.execute(
            text(
                "SELECT count(*) FROM media_sources WHERE provider='instagram_past_posts'"
            )
        )
        generations = await conn.execute(
            text("SELECT count(*) FROM instagram_past_posts_generations")
        )
        assert (sources.scalar_one(), generations.scalar_one()) == (0, 0)
        assert (
            await row(conn, "SELECT name, source_mix_revision FROM workspaces")
        ) == {"name": "continued", "source_mix_revision": 8}
    # The control: with the capability, the same call lands the source Off.
    landed = await connect(past_posts_engine, origin, choice=PastPostsChoice(False))
    assert landed.outcome == "connected_history_off" and landed.mix_revision == 9


async def test_a_review_hold_closes_admission_and_suitability_and_nothing_else(
    past_posts_engine, origin
):
    from src.services.target.source_membership import fence_generations
    from src.services.target.source_read_admission import SourceReadRefused

    src, item = await suitability.ready(past_posts_engine, origin)
    # The control: before the hold the source is admitted and its item suitable,
    # so what follows is the hold's doing and not a missing prerequisite.
    admitted = await admission.capture(past_posts_engine, origin, src)
    assert admitted.generation_id == src["generation_id"]
    assert (await suitability.verdict(past_posts_engine, origin, src))["suitable"]
    saved = await snapshot(past_posts_engine, origin)

    async with uow(past_posts_engine, origin) as conn:
        held = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=src["source_id"],
            generation_ids=frozenset([src["generation_id"]]),
            reason="review",
        )
    assert held.changed_generation_ids == (src["generation_id"],)
    assert held.source_use_epoch == admitted.source_use_epoch + 1

    with pytest.raises(SourceReadRefused) as refused:
        await admission.capture(past_posts_engine, origin, src)
    assert refused.value.reason == "generation_unavailable"
    excluded = await suitability.verdict(past_posts_engine, origin, src)
    assert not excluded["suitable"] and excluded["reason"] == "generation_unavailable"

    # Only the generation's lifecycle moved: the credential, the mix and the
    # observed data are as they were. A hold leaves the data unavailable, not gone.
    assert await generation_states(past_posts_engine, origin) == {
        src["generation_id"]: "review_hold"
    }
    assert await snapshot(past_posts_engine, origin) == saved
    state = await fact(past_posts_engine, origin, "SELECT state FROM oauth_credentials")
    assert state == "active"
    async with uow(past_posts_engine, origin) as conn:
        kept = await row(
            conn,
            "SELECT (SELECT count(*) FROM instagram_media_index) AS indexed,"
            " source_projection_redacted AS redacted FROM media_items WHERE id=:id",
            id=item.media_item_id,
        )
    assert (kept["indexed"], kept["redacted"]) == (1, False)
    assert (await audit_reasons(past_posts_engine, origin, src["source_id"]))[-1] == (
        "review"
    )


@pytest.mark.parametrize(
    "start,reason,end,changes,credential",
    [
        ("active", "review", "review_hold", True, "active"),
        ("superseded", "review", "review_hold", True, "active"),
        ("review_hold", "review", "review_hold", False, "active"),
        # A deletion in flight is never softened to a hold.
        ("deletion_pending", "review", "deletion_pending", False, "active"),
        ("deleted", "review", "deleted", False, "active"),
        # A deletion decision takes over a hold, and a supersession.
        ("review_hold", "deletion", "deletion_pending", True, "active"),
        ("superseded", "deletion", "deletion_pending", True, "active"),
        # A revocation revokes the current credential but never softens a hold.
        ("review_hold", "revocation", "review_hold", False, "revoked"),
        ("superseded", "revocation", "superseded", False, "revoked"),
        ("active", "revocation", "superseded", True, "revoked"),
    ],
)
async def test_a_fence_only_ever_strengthens_a_generations_lifecycle(
    past_posts_engine, origin, start, reason, end, changes, credential
):
    from src.services.target.source_membership import fence_generations

    source = await ensure(past_posts_engine, origin, authorization())
    gid = source["generation_id"]
    if start != "active":
        async with uow(past_posts_engine, origin) as conn:
            await conn.execute(
                text(
                    "UPDATE instagram_past_posts_generations SET lifecycle=:state WHERE id=:gid"
                ),
                {"state": start, "gid": gid},
            )
    async with uow(past_posts_engine, origin) as conn:
        fenced = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=source["source_id"],
            generation_ids=frozenset([gid]),
            reason=reason,
        )
    assert fenced.changed_generation_ids == ((gid,) if changes else ())
    assert await generation_states(past_posts_engine, origin) == {gid: end}
    state = await fact(past_posts_engine, origin, "SELECT state FROM oauth_credentials")
    assert state == credential


async def test_an_unknown_fence_reason_is_refused_by_name_and_changes_nothing(
    past_posts_engine, origin
):
    from src.services.target.instagram_sources import PastPostsRefused
    from src.services.target.source_membership import fence_generations

    source = await ensure(past_posts_engine, origin, authorization())
    for reason in ("", "delete", "expired", None):
        async with uow(past_posts_engine, origin) as conn:
            with pytest.raises(PastPostsRefused, match="invalid_past_posts_fence"):
                await fence_generations(
                    conn,
                    workspace_id=str(origin["ws"]),
                    source_id=source["source_id"],
                    generation_ids=frozenset([source["generation_id"]]),
                    reason=reason,
                )
    assert await generation_states(past_posts_engine, origin) == {
        source["generation_id"]: "active"
    }
    assert await audit_reasons(past_posts_engine, origin, source["source_id"]) == [
        "created"
    ]


async def test_holding_an_older_generation_leaves_the_current_one_untouched(
    past_posts_engine, origin
):
    from src.services.target.source_membership import fence_generations

    old = await ensure(past_posts_engine, origin, authorization())
    current = await ensure(past_posts_engine, origin, authorization())
    async with uow(past_posts_engine, origin) as conn:
        held = await fence_generations(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=old["source_id"],
            generation_ids=frozenset([old["generation_id"]]),
            reason="review",
        )
    assert held.changed_generation_ids == (old["generation_id"],)
    # Nothing a reader can see changed, so the epoch did not move.
    assert held.source_use_epoch == current["source_use_epoch"]
    assert await generation_states(past_posts_engine, origin) == {
        old["generation_id"]: "review_hold",
        current["generation_id"]: "active",
    }
    state = await fact(past_posts_engine, origin, "SELECT state FROM oauth_credentials")
    assert state == "active"


async def test_marking_deleted_closes_only_the_frozen_scope_and_never_bumps_the_epoch(
    past_posts_engine, origin
):
    from src.services.target.source_membership import (
        fence_generations,
        mark_generations_deleted,
    )

    old = await ensure(past_posts_engine, origin, authorization())
    current = await ensure(past_posts_engine, origin, authorization())
    ws, sid = str(origin["ws"]), old["source_id"]
    scope = frozenset([old["generation_id"]])
    async with uow(past_posts_engine, origin) as conn:
        await fence_generations(
            conn,
            workspace_id=ws,
            source_id=sid,
            generation_ids=scope,
            reason="deletion",
        )
    audited = await audit_reasons(past_posts_engine, origin, sid)

    async with uow(past_posts_engine, origin) as conn:
        marked = await mark_generations_deleted(
            conn, workspace_id=ws, source_id=sid, generation_ids=scope
        )
    assert marked.changed_generation_ids == (old["generation_id"],)
    assert marked.source_use_epoch == current["source_use_epoch"]
    assert await generation_states(past_posts_engine, origin) == {
        old["generation_id"]: "deleted",
        current["generation_id"]: "active",
    }
    # No fence, so no epoch bump and no audit row of its own.
    assert await epoch_of(past_posts_engine, origin, sid) == current["source_use_epoch"]
    assert await audit_reasons(past_posts_engine, origin, sid) == audited

    # A replay is a no-op, not an error: the cleanup that crashed after this
    # step runs it again.
    async with uow(past_posts_engine, origin) as conn:
        again = await mark_generations_deleted(
            conn, workspace_id=ws, source_id=sid, generation_ids=scope
        )
    assert again.changed_generation_ids == ()
    assert again.source_use_epoch == marked.source_use_epoch
    assert await epoch_of(past_posts_engine, origin, sid) == current["source_use_epoch"]


@pytest.mark.parametrize("member", ["active", "superseded", "review_hold"])
async def test_marking_deleted_refuses_a_generation_that_was_never_fenced_for_deletion(
    past_posts_engine, origin, member
):
    from src.services.target.instagram_sources import PastPostsRefused
    from src.services.target.source_membership import (
        fence_generations,
        mark_generations_deleted,
    )

    old = await ensure(past_posts_engine, origin, authorization())  # now superseded
    current = await ensure(past_posts_engine, origin, authorization())  # active
    ws, sid = str(origin["ws"]), old["source_id"]
    # `fenced` is the one in the frozen scope that was fenced for deletion;
    # `member` is the other, in the lifecycle under test.
    fenced, unfenced = {
        "active": (old, current),
        "superseded": (current, old),
        "review_hold": (current, old),
    }[member]

    async def fence(generation, reason):
        async with uow(past_posts_engine, origin) as conn:
            await fence_generations(
                conn,
                workspace_id=ws,
                source_id=sid,
                generation_ids=frozenset([generation["generation_id"]]),
                reason=reason,
            )

    await fence(fenced, "deletion")
    if member == "review_hold":
        await fence(unfenced, "review")
    states = await generation_states(past_posts_engine, origin)
    assert states == {
        fenced["generation_id"]: "deletion_pending",
        unfenced["generation_id"]: member,
    }

    async with uow(past_posts_engine, origin) as conn:
        with pytest.raises(
            PastPostsRefused, match="past_posts_generation_scope_unavailable"
        ):
            await mark_generations_deleted(
                conn,
                workspace_id=ws,
                source_id=sid,
                generation_ids=frozenset(
                    [fenced["generation_id"], unfenced["generation_id"]]
                ),
            )
    # All or nothing: the fenced generation was not closed by a refused call.
    assert await generation_states(past_posts_engine, origin) == states
    # The control: the fenced generation alone is closed.
    async with uow(past_posts_engine, origin) as conn:
        done = await mark_generations_deleted(
            conn,
            workspace_id=ws,
            source_id=sid,
            generation_ids=frozenset([fenced["generation_id"]]),
        )
    assert done.changed_generation_ids == (fenced["generation_id"],)
    assert (await generation_states(past_posts_engine, origin))[
        fenced["generation_id"]
    ] == "deleted"


async def test_marking_deleted_refuses_an_empty_unknown_or_foreign_scope(
    past_posts_engine, origin, sync_conn
):
    import uuid

    from src.services.target.instagram_sources import PastPostsRefused
    from src.services.target.source_membership import (
        fence_generations,
        mark_generations_deleted,
    )
    from tests.scripts.conftest import seed_workspace_chain
    from tests.scripts.test_instagram_catalog_gate import seed_past_posts

    mine = await ensure(past_posts_engine, origin, authorization())
    other = await obs.second_origin(past_posts_engine, origin)
    foreign_chain = seed_workspace_chain(sync_conn, "foreign-mark")
    with sync_conn.cursor() as cur:
        foreign = seed_past_posts(cur, foreign_chain)
        cur.execute(
            "UPDATE instagram_past_posts_generations SET lifecycle='deletion_pending' WHERE id=%s",
            (foreign["generation"],),
        )
    sync_conn.commit()
    ws = str(origin["ws"])
    # Every candidate is deletion_pending, so scope is the only reason to refuse.
    for source in (mine, other):
        async with uow(past_posts_engine, origin) as conn:
            await fence_generations(
                conn,
                workspace_id=ws,
                source_id=source["source_id"],
                generation_ids=frozenset([source["generation_id"]]),
                reason="deletion",
            )
    cases = {
        "empty": (mine["source_id"], frozenset()),
        "unknown": (mine["source_id"], frozenset([str(uuid.uuid4())])),
        "another origin's": (mine["source_id"], frozenset([other["generation_id"]])),
        "another tenant's": (
            mine["source_id"],
            frozenset([str(foreign["generation"])]),
        ),
        "another tenant's source": (
            str(foreign["past_posts"]),
            frozenset([str(foreign["generation"])]),
        ),
    }
    for name, (source_id, scope) in cases.items():
        async with uow(past_posts_engine, origin) as conn:
            with pytest.raises(
                PastPostsRefused, match="past_posts_generation_scope_unavailable"
            ):
                await mark_generations_deleted(
                    conn, workspace_id=ws, source_id=source_id, generation_ids=scope
                )
        assert await generation_states(past_posts_engine, origin) == {
            mine["generation_id"]: "deletion_pending",
            other["generation_id"]: "deletion_pending",
        }, name
    # The controls: each generation closes under its own source.
    for source in (mine, other):
        async with uow(past_posts_engine, origin) as conn:
            done = await mark_generations_deleted(
                conn,
                workspace_id=ws,
                source_id=source["source_id"],
                generation_ids=frozenset([source["generation_id"]]),
            )
        assert done.changed_generation_ids == (source["generation_id"],)
    with sync_conn.cursor() as cur:
        cur.execute(
            "SELECT lifecycle FROM instagram_past_posts_generations WHERE id=%s",
            (foreign["generation"],),
        )
        assert cur.fetchone()[0] == "deletion_pending"


async def test_marking_deleted_waits_behind_the_canonical_source_prefix(
    past_posts_engine, origin
):
    from src.services.target import source_state
    from src.services.target.source_membership import (
        fence_generations,
        mark_generations_deleted,
    )

    source = await ensure(past_posts_engine, origin, authorization())
    ws, sid, gid = str(origin["ws"]), source["source_id"], source["generation_id"]
    async with uow(past_posts_engine, origin) as conn:
        await fence_generations(
            conn,
            workspace_id=ws,
            source_id=sid,
            generation_ids=frozenset([gid]),
            reason="deletion",
        )

    async def mark():
        async with uow(past_posts_engine, origin) as conn:
            return await mark_generations_deleted(
                conn, workspace_id=ws, source_id=sid, generation_ids=frozenset([gid])
            )

    async with uow(past_posts_engine, origin) as holder:
        await source_state.lock_workspace_sources(holder, workspace_id=ws)
        marking = asyncio.create_task(mark())
        done, _ = await asyncio.wait({marking}, timeout=1.0)
        assert not done, "mark_generations_deleted ran without the source prefix"
    closed = await asyncio.wait_for(marking, timeout=30)
    assert closed.changed_generation_ids == (gid,)


async def test_a_rollback_can_still_delete_a_generation_end_to_end(
    past_posts_engine, origin
):
    """Fence, erase, close: the three steps a deletion cleanup runs, in that order
    and in separate transactions, none given the capability. The past-posts source ends with
    no observation, a redacted item, a deleted generation and ONE epoch bump."""
    from src.services.target.instagram_catalog import redact_generations
    from src.services.target.source_membership import (
        fence_generations,
        mark_generations_deleted,
    )

    source = await obs.enabled(past_posts_engine, origin)
    landed = await obs.observe(past_posts_engine, origin, source, obs.bundle())
    ws, sid = str(origin["ws"]), source["source_id"]
    scope = frozenset([source["generation_id"]])
    async with uow(past_posts_engine, origin) as conn:
        fenced = await fence_generations(
            conn,
            workspace_id=ws,
            source_id=sid,
            generation_ids=scope,
            reason="deletion",
        )
    async with uow(past_posts_engine, origin) as conn:
        erased = await redact_generations(
            conn,
            workspace_id=ws,
            source_id=sid,
            generation_ids=scope,
            keys=obs.keys(),
        )
    async with uow(past_posts_engine, origin) as conn:
        closed = await mark_generations_deleted(
            conn, workspace_id=ws, source_id=sid, generation_ids=scope
        )
    assert (erased.observations_deleted, erased.items_redacted) == (1, 1)
    assert erased.media_item_ids == (landed.media_item_id,)
    assert closed.changed_generation_ids == (source["generation_id"],)
    assert fenced.source_use_epoch == source["source_use_epoch"] + 1
    assert closed.source_use_epoch == fenced.source_use_epoch
    assert await epoch_of(past_posts_engine, origin, sid) == fenced.source_use_epoch
    assert await generation_states(past_posts_engine, origin) == {
        source["generation_id"]: "deleted"
    }
    assert await audit_reasons(past_posts_engine, origin, sid) == [
        "created",
        "deletion",
    ]
    async with uow(past_posts_engine, origin) as conn:
        kept = await row(
            conn,
            "SELECT (SELECT count(*) FROM instagram_media_index) AS indexed,"
            " source_projection_redacted AS redacted FROM media_items WHERE id=:id",
            id=landed.media_item_id,
        )
    assert (kept["indexed"], kept["redacted"]) == (0, True)
