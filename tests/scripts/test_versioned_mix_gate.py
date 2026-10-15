"""Versioned allocation/conversion in the caller transaction and real tenant roles."""

from contextlib import asynccontextmanager
from decimal import Decimal, localcontext
import json
import uuid
import httpx
import pytest
from sqlalchemy import text
from tests.scripts import test_archive_preview_gate as preview_gate
from tests.scripts import test_instagram_sources_gate as foundation
from tests.scripts.test_instagram_observations_gate import uow, row
from src.api.app import create_app
from src.api.principal import Principal, current_principal
from src.services.target import category_mix

archive_engine = foundation.archive_engine
origin = foundation.origin
#: The API's own login (`svc_ingress`), for the tests that drive the real app.
ingress = preview_gate.ingress
pytestmark = pytest.mark.integration


def record():
    return {
        "v": 1,
        "run_id": "synthetic-conversion",
        "enabled": True,
        "api": {
            "sha": "a" * 40,
            "source_mix_protocol_max": 2,
            "expanded_schema": True,
            "both_protocols": True,
        },
        "worker": {
            "sha": "b" * 40,
            "source_mix_protocol_max": 2,
            "expanded_schema": True,
            "both_protocols": True,
        },
        "termination_evidence": "synthetic:terminated",
        "leases_evidence": "synthetic:exhausted",
        "lease_disposition": "exhausted",
        "rollback_releases": [
            {
                "sha": "c" * 40,
                "source_mix_protocol_max": 2,
                "expanded_schema": True,
                "both_protocols": True,
            }
        ],
    }


def evidence(tmp_path, change=None):
    from src.services.target.mix_conversion import load_evidence

    data = record()
    if change:
        change(data)
    path = tmp_path / "run.json"
    path.write_text(json.dumps(data))
    return load_evidence(str(path), service="api", release_sha="a" * 40)


@pytest.mark.parametrize(
    "bad", ["missing", "disabled", "lease", "rollback", "sha", "max", "bool"]
)
def test_incomplete_operator_record_never_opens_conversion(tmp_path, bad):
    def change(data):
        if bad == "missing":
            data.pop("termination_evidence")
        if bad == "disabled":
            data["enabled"] = False
        if bad == "lease":
            data["lease_disposition"] = "unknown"
        if bad == "rollback":
            data["rollback_releases"] = []
        if bad == "sha":
            data["api"]["sha"] = "d" * 40
        if bad == "max":
            data["worker"]["source_mix_protocol_max"] = 1
        if bad == "bool":
            data["api"]["source_mix_protocol_max"] = True

    assert evidence(tmp_path, change) is None


async def add(conn, chain, ratios):
    ids = [str(chain["src"])]
    for i in range(len(ratios) - 1):
        ids.append(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO media_sources(workspace_id,provider,config) VALUES(:ws,'gdrive',jsonb_build_object('folder_ref',CAST(:n AS text))) RETURNING id"
                        ),
                        {"ws": str(chain["ws"]), "n": str(i)},
                    )
                ).scalar_one()
            )
        )
    for sid, ratio in zip(ids, ratios):
        if ratio is not None:
            await conn.execute(
                text(
                    "INSERT INTO category_post_case_mix(workspace_id,source_id,category,ratio) VALUES(:ws,:sid,'synthetic',:ratio)"
                ),
                {"ws": str(chain["ws"]), "sid": sid, "ratio": Decimal(ratio)},
            )
    return ids


@pytest.mark.parametrize(
    "ratios,mode",
    [
        ([None, None, None], "equal"),
        ([".7000", ".3000", "0"], "custom"),
        (["0", "0"], "equal"),
        (["1", None], "custom"),
    ],
)
async def test_conversion_atomic_decimal_and_retry(
    archive_engine, origin, tmp_path, ratios, mode
):
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, ratios)
        first = await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        repeat = await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=None
        )
        assert first["revision"] == repeat["revision"] == 1
        assert first["mode"] == mode and first["protocol_version"] == 2
        saved = {r["source_id"]: Decimal(r["ratio"]) for r in first["rows"]}
        with localcontext() as context:
            context.prec = 50
            assert sum(saved.values()) == (
                Decimal(0) if all(r == "0" for r in ratios) else Decimal(1)
            )
        for sid, old in zip(ids, ratios):
            assert (saved[sid] > 0) == (old != "0")
        if ratios[0] == ".7000":
            assert saved[ids[0]] == Decimal(".7000")
        assert all(isinstance(r["ratio"], str) for r in first["rows"])
        with pytest.raises(category_mix.MixProtocolRequired):
            await category_mix.set_mix(
                conn, workspace_id=str(origin["ws"]), mix=[], by_user_id=None
            )
        assert (
            await row(
                conn,
                "SELECT count(*) AS n FROM audit_events WHERE detail->>'run_id'='synthetic-conversion'",
            )
        )["n"] == 1


async def test_missing_drain_refuses_without_mutation_even_if_caught(
    archive_engine, origin
):
    async with uow(archive_engine, origin) as conn:
        with pytest.raises(category_mix.MixConversionUnavailable):
            await category_mix.convert_workspace(
                conn, workspace_id=str(origin["ws"]), evidence=None
            )
        assert (await row(conn, "SELECT source_mix_revision FROM workspaces"))[
            "source_mix_revision"
        ] is None
        assert (await row(conn, "SELECT count(*) AS n FROM category_post_case_mix"))[
            "n"
        ] == 0


async def test_custom_stale_and_membership_actions_preserve_off(
    archive_engine, origin, tmp_path
):
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".7", ".3", "0"])
        await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        with pytest.raises(category_mix.MixConflict) as stale:
            await category_mix.mutate(
                conn,
                workspace_id=str(origin["ws"]),
                body={
                    "protocol_version": 2,
                    "action": "save_custom",
                    "expected_revision": 0,
                    "rows": [],
                },
                by_user_id=None,
            )
        assert stale.value.current_revision == 1
        with pytest.raises(category_mix.MixInvalid) as incomplete:
            await category_mix.mutate(
                conn,
                workspace_id=str(origin["ws"]),
                body={
                    "protocol_version": 2,
                    "action": "save_custom",
                    "expected_revision": 1,
                    "rows": [],
                },
                by_user_id=None,
            )
        assert incomplete.value.reason == "source_set"
        result = await category_mix.mutate(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "action": "reset_equal",
                "expected_revision": 1,
            },
            by_user_id=None,
        )
        assert {r["source_id"]: r["ratio"] for r in result["rows"]} == {
            ids[0]: "0.5",
            ids[1]: "0.5",
            ids[2]: "0",
        }
        result = await category_mix.mutate(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "action": "set_enabled",
                "expected_revision": 2,
                "source_id": ids[0],
                "enabled": False,
            },
            by_user_id=None,
        )
        assert {r["source_id"]: r["ratio"] for r in result["rows"]} == {
            ids[0]: "0",
            ids[1]: "1",
            ids[2]: "0",
        }
        result = await category_mix.mutate(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "action": "set_enabled",
                "expected_revision": 3,
                "source_id": ids[1],
                "enabled": False,
            },
            by_user_id=None,
        )
        assert (
            all(r["ratio"] == "0" for r in result["rows"]) and result["revision"] == 4
        )


async def test_real_allocator_off_add_preserves_custom_and_enabled_add_resets(
    archive_engine, origin, tmp_path
):
    from src.services.target.source_membership import AllocationRequest

    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".7", ".3", None])
        # Convert the first two with all-explicit custom intent.
        await conn.execute(
            text(
                "UPDATE media_sources SET config=config || jsonb_build_object('removed',true) WHERE id=:sid"
            ),
            {"sid": ids[2]},
        )
        await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        await conn.execute(
            text("UPDATE media_sources SET config=config - 'removed' WHERE id=:sid"),
            {"sid": ids[2]},
        )
        result = await category_mix.allocate_membership(
            conn, AllocationRequest(str(origin["ws"]), ids[2], False, True, False, 1)
        )
        assert result.revision == 2 and result.mode == "custom"
        view = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert [Decimal(r["ratio"]) for r in view["rows"] if r["enabled"]] == [
            Decimal(".7"),
            Decimal(".3"),
        ]
        result = await category_mix.allocate_membership(
            conn, AllocationRequest(str(origin["ws"]), ids[2], True, False, True, 2)
        )
        assert result.revision == 3 and result.mode == "equal"
        view = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert all(Decimal(r["ratio"]) > 0 for r in view["rows"])


async def test_converter_crash_rolls_back_inside_caught_caller_transaction(
    archive_engine, origin, tmp_path, monkeypatch
):
    from src.services.target import audit

    async with uow(archive_engine, origin) as conn:
        await add(conn, origin, [None, None])

        async def fail(*args, **kwargs):
            raise RuntimeError("synthetic crash")

        monkeypatch.setattr(audit, "record", fail)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            await category_mix.convert_workspace(
                conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
            )
        assert (await row(conn, "SELECT source_mix_revision FROM workspaces"))[
            "source_mix_revision"
        ] is None
        assert (await row(conn, "SELECT count(*) AS n FROM category_post_case_mix"))[
            "n"
        ] == 0


async def test_hybrid_50000_items_empty_automatic_stays_positive(
    archive_engine, origin, tmp_path
):
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, ["1", None])
        await conn.execute(
            text(
                "INSERT INTO media_items(workspace_id,source_id,content_hash,file_name,media_kind,provider_file_ref) SELECT :ws,:sid,'synthetic-'||n,'x.jpg','image','synthetic-'||n FROM generate_series(1,49999) n"
            ),
            {"ws": str(origin["ws"]), "sid": ids[0]},
        )
        value = await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        automatic = next(r for r in value["rows"] if r["source_id"] == ids[1])
        assert Decimal(automatic["ratio"]) > 0 and Decimal(
            automatic["ratio"]
        ) < Decimal(".0001")
        assert value["mode"] == "custom"


async def test_workspace_creation_compatibility_and_explicit_v2_barrier(
    archive_engine, origin, tmp_path
):
    import uuid
    from src.services.target import workspaces
    from src.services.target.unit_of_work import unit_of_work

    for explicit, admitted in [(False, False), (True, False), (True, True)]:
        ws = str(uuid.uuid4())
        async with unit_of_work(
            archive_engine, ws, actor_kind="system"
        ).begin() as conn:
            if explicit and not admitted:
                with pytest.raises(category_mix.MixConversionUnavailable):
                    await workspaces.create_workspace(
                        conn,
                        owner_user_id=str(origin["user"]),
                        name="synthetic-new",
                        workspace_id=ws,
                        protocol_version=2,
                    )
                assert (await row(conn, "SELECT count(*) AS n FROM workspaces"))[
                    "n"
                ] == 0
                continue
            await workspaces.create_workspace(
                conn,
                owner_user_id=str(origin["user"]),
                name="synthetic-new",
                workspace_id=ws,
                protocol_version=2 if explicit else None,
                conversion_evidence=evidence(tmp_path) if admitted else None,
            )
            saved = await row(
                conn,
                "SELECT source_mix_mode,source_mix_revision,source_mix_converted_at FROM workspaces",
            )
            if admitted:
                assert (
                    saved["source_mix_mode"] == "equal"
                    and saved["source_mix_revision"] == 0
                    and saved["source_mix_converted_at"] is not None
                )
            else:
                assert all(v is None for v in saved.values())


async def test_preview_never_changes_scd_or_revision(archive_engine, origin, tmp_path):
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".7", ".3", "0"])
        await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        value = await category_mix.preview(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "action": "set_enabled",
                "source_id": ids[2],
                "enabled": True,
                "expected_revision": 1,
            },
        )
        assert value["revision"] == 1 and value["mode"] == "equal"
        assert all(Decimal(r["ratio"]) > 0 for r in value["rows"])
        saved = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        assert saved["mode"] == "custom" and saved["revision"] == 1
        assert (
            next(r["ratio"] for r in saved["rows"] if r["source_id"] == ids[2]) == "0"
        )


async def test_versioned_drive_source_add_remove_and_off_only_preserve_custom(
    archive_engine, origin, tmp_path
):
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".7", ".3"])
        await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        added = await category_mix.connect_drive(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "expected_revision": 1,
                "folder_ref": "synthetic-off",
                "enabled": False,
            },
        )
        assert added["mix"]["revision"] == 2 and added["mix"]["mode"] == "custom"
        assert {
            r["source_id"]: r["ratio"] for r in added["mix"]["rows"] if r["enabled"]
        } == {ids[0]: "0.7", ids[1]: "0.3"}
        removed = await category_mix.remove_drive(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=added["source_id"],
            body={"protocol_version": 2, "expected_revision": 2},
        )
        assert removed["mix"]["revision"] == 3 and removed["mix"]["mode"] == "custom"
        added = await category_mix.connect_drive(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "expected_revision": 3,
                "folder_ref": "synthetic-on",
                "enabled": True,
            },
        )
        assert added["mix"]["revision"] == 4 and added["mix"]["mode"] == "equal"
        assert all(Decimal(r["ratio"]) > 0 for r in added["mix"]["rows"])


async def test_real_archive_allocator_epoch_changes_only_enablement(
    archive_engine, origin, tmp_path
):
    from src.services.target.source_membership import connect_archive, ArchiveChoice

    async with uow(archive_engine, origin) as conn:
        await add(conn, origin, ["1"])
        await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
    async with uow(archive_engine, origin) as conn:
        connected = await connect_archive(
            conn,
            workspace_id=str(origin["ws"]),
            origin_ig_account_id=str(origin["iga"]),
            authorization=foundation.authorization(),
            had_prior_grant=False,
            choice=ArchiveChoice(True, 1),
            allocation_writer=category_mix.allocate_membership,
            capability=foundation.capability(),
        )
        assert (
            connected.mix_revision == 2
            and connected.history_enabled
            and connected.mix_reset_applied
        )
        before = (
            await row(
                conn,
                "SELECT source_use_epoch FROM media_sources WHERE id=:sid",
                sid=connected.source_id,
            )
        )["source_use_epoch"]
        view = await category_mix.resource(conn, workspace_id=str(origin["ws"]))
        await category_mix.mutate(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "expected_revision": 2,
                "action": "save_custom",
                "rows": [
                    {"source_id": r["source_id"], "ratio": r["ratio"]}
                    for r in view["rows"]
                ],
            },
            by_user_id=None,
        )
        assert (
            await row(
                conn,
                "SELECT source_use_epoch FROM media_sources WHERE id=:sid",
                sid=connected.source_id,
            )
        )["source_use_epoch"] == before


@asynccontextmanager
async def mix_api(engine, user, *, capability):
    """The real app over *engine*, with *user* signed in. *capability* is what a
    composition root would inject: None is production's, and the rollback's."""
    app = create_app(engine=engine, env={})
    app.state.archive_capability = capability
    app.dependency_overrides[current_principal] = lambda: Principal(
        session_id=str(uuid.uuid4()), user_id=str(user)
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://api.example.test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def test_a_withdrawn_capability_turns_an_archive_off_but_never_on_over_http(
    ingress, origin, tmp_path
):
    """The rollback: archive rows exist and the deployment no longer injects the
    capability. Off closes, so an archive row's Off through the real routes, as
    the API's own role, still works, and so does its preview. On opens: it is a
    named 503 on the PUT and on its preview and writes nothing, a malformed value
    included (a closing exemption is never inferred from a value the client got
    wrong). With the capability back, a wrong value is a 400 and the same On body
    is the allowed path. Never the bare 500 an unhandled refusal was."""
    from src.services.target.source_membership import ArchiveChoice, connect_archive

    ws = str(origin["ws"])
    async with uow(ingress, origin) as conn:
        await add(conn, origin, ["1"])
        await category_mix.convert_workspace(
            conn, workspace_id=ws, evidence=evidence(tmp_path)
        )
    async with uow(ingress, origin) as conn:
        connected = await connect_archive(
            conn,
            workspace_id=ws,
            origin_ig_account_id=str(origin["iga"]),
            authorization=foundation.authorization(),
            had_prior_grant=False,
            choice=ArchiveChoice(True, 1),
            allocation_writer=category_mix.allocate_membership,
            capability=foundation.capability(),
        )
    archive = connected.source_id
    assert (connected.history_enabled, connected.mix_revision) == (True, 2)

    async def saved():
        """The mix revision and the archive's current ratio, as the tenant reads them."""
        async with uow(ingress, origin) as conn:
            revision = await row(
                conn,
                "SELECT source_mix_revision AS n FROM workspaces WHERE id=:ws",
                ws=ws,
            )
            ratio = await row(
                conn,
                "SELECT ratio FROM category_post_case_mix WHERE workspace_id=:ws"
                " AND source_id=:sid AND effective_to IS NULL",
                ws=ws,
                sid=archive,
            )
        return revision["n"], ratio["ratio"]

    before = await saved()
    assert before == (2, Decimal("0.5"))
    mix = f"/api/v1/workspaces/{ws}/category-mix"
    off = {
        "protocol_version": 2,
        "action": "set_enabled",
        "source_id": archive,
        "enabled": False,
        "expected_revision": 2,
    }

    on = {**off, "enabled": True, "expected_revision": 3}
    refusal = {"detail": "archive_not_activated", "reason": "archive_not_activated"}

    async with mix_api(ingress, origin["user"], capability=None) as client:
        # The editor still loads: archive rows are listed, the archive is not available.
        listed = await client.get(mix)
        assert listed.status_code == 200, listed.text
        assert listed.json()["archive_available"] is False

        # Off closes: its preview answers and saves nothing, then the PUT turns it off.
        previewed = await client.post(f"{mix}/preview", json=off)
        assert previewed.status_code == 200, previewed.text
        assert previewed.json()["revision"] == 2
        assert await saved() == before
        done = await client.put(mix, json=off)
        assert done.status_code == 200, done.text
        assert done.json()["revision"] == 3
        switched = next(r for r in done.json()["rows"] if r["source_id"] == archive)
        assert (switched["enabled"], switched["ratio"]) == (False, "0")
        assert await saved() == (3, Decimal(0))

        # On opens: refused by name, on the PUT and on the preview, and so is a
        # value that is not exactly False. Nothing is written.
        for body in (on, {**on, "enabled": "no"}, {**on, "enabled": None}):
            for refused in (
                await client.put(mix, json=body),
                await client.post(f"{mix}/preview", json=body),
            ):
                assert refused.status_code == 503, refused.text
                assert refused.json() == refusal
        assert await saved() == (3, Decimal(0))

    async with mix_api(
        ingress, origin["user"], capability=foundation.capability()
    ) as client:
        wrong = await client.put(mix, json={**on, "enabled": "no"})
        assert wrong.status_code == 400, wrong.text
        assert wrong.json() == {
            "detail": "invalid_archive_choice",
            "reason": "invalid_archive_choice",
        }
        assert await saved() == (3, Decimal(0))

        previewed = await client.post(f"{mix}/preview", json=on)
        assert previewed.status_code == 200, previewed.text
        assert previewed.json()["revision"] == 3
        assert await saved() == (3, Decimal(0))

        done = await client.put(mix, json=on)
        assert done.status_code == 200, done.text
        assert done.json()["revision"] == 4
        switched = next(r for r in done.json()["rows"] if r["source_id"] == archive)
        assert (switched["enabled"], Decimal(switched["ratio"])) == (
            True,
            Decimal("0.5"),
        )
    assert await saved() == (4, Decimal("0.5"))


@pytest.mark.parametrize("requested", [True, False, None])
async def test_removed_drive_readd_honors_requested_membership(
    archive_engine, origin, tmp_path, requested
):
    """Same-folder restoration is an add; it preserves stable source identity."""
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".7", ".3"])
        await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        added = await category_mix.connect_drive(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "expected_revision": 1,
                "folder_ref": "synthetic-restored",
                "enabled": False,
            },
        )
        sid = added["source_id"]
        removed = await category_mix.remove_drive(
            conn,
            workspace_id=str(origin["ws"]),
            source_id=sid,
            body={"protocol_version": 2, "expected_revision": added["mix"]["revision"]},
        )
        body = {
            "protocol_version": 2,
            "expected_revision": removed["mix"]["revision"],
            "folder_ref": "synthetic-restored",
        }
        if requested is not None:
            body["enabled"] = requested
        restored = await category_mix.connect_drive(
            conn,
            workspace_id=str(origin["ws"]),
            body=body,
        )
        assert restored["source_id"] == sid and restored["created"] is False
        assert restored["mix"]["revision"] == removed["mix"]["revision"] + 1
        rows = {r["source_id"]: r for r in restored["mix"]["rows"]}
        assert set(rows) == {*ids, sid}
        if requested is False:
            assert restored["mix"]["mode"] == "custom"
            assert rows[sid]["enabled"] is False and rows[sid]["ratio"] == "0"
            assert {key: rows[key]["ratio"] for key in ids} == dict(
                zip(ids, ["0.7", "0.3"])
            )
        else:
            assert restored["mix"]["mode"] == "equal"
            assert all(r["enabled"] for r in rows.values())
            with localcontext() as context:
                context.prec = 50
                ratios = [Decimal(r["ratio"]) for r in rows.values()]
                assert sum(ratios) == 1
                assert max(ratios) - min(ratios) < Decimal("1e-48")


@pytest.mark.parametrize("was_enabled", [True, False])
async def test_connected_drive_reconnect_preserves_saved_membership(
    archive_engine, origin, tmp_path, was_enabled
):
    """A grant/idempotent connect is not a removed-folder membership addition."""
    async with uow(archive_engine, origin) as conn:
        ids = await add(conn, origin, [".7", ".3", "0"])
        sid = ids[0] if was_enabled else ids[2]
        await conn.execute(
            text(
                'UPDATE media_sources SET config=config || \'{"folder_ref":"synthetic-connected"}\'::jsonb WHERE id=:sid'
            ),
            {"sid": sid},
        )
        saved = await category_mix.convert_workspace(
            conn, workspace_id=str(origin["ws"]), evidence=evidence(tmp_path)
        )
        connected = await category_mix.connect_drive(
            conn,
            workspace_id=str(origin["ws"]),
            body={
                "protocol_version": 2,
                "expected_revision": saved["revision"],
                "folder_ref": "synthetic-connected",
                "enabled": not was_enabled,
            },
        )
        assert connected["source_id"] == sid and connected["created"] is False
        assert connected["mix"]["revision"] == saved["revision"]
        assert connected["mix"]["mode"] == saved["mode"] == "custom"
        assert {r["source_id"]: r["ratio"] for r in connected["mix"]["rows"]} == {
            r["source_id"]: r["ratio"] for r in saved["rows"]
        }
