"""A folder's approval position (#1477), EXECUTED — as `svc_ingress`,
through the real port, on the replayed target schema.

What only PostgreSQL can say, and so is pinned here rather than in
`tests/src/services/target/test_command_executors.py`:

- **The login may write it.** `svc_ingress` holds UPDATE on `media_sources`
  (057), so 110's column needs no grant of its own.
- **Only an admin may set it**: a member is refused at the port's gate,
  and the folder keeps its position.
- **Only this workspace's folder.** Under the tenant's policy another
  workspace's folder is `not_found`, and it keeps its position; with the
  policies out, the UPDATE's own predicate still confines the write.
- **NULL is the workspace's position,** written as NULL, and read back by the
  serve step's `COALESCE` (`auto_approval._FACTS`).
- **Every change names the person** in `audit_events`, with the folder's own
  state on both sides, since a position is not a state.
"""

from __future__ import annotations

import asyncio

import pytest

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import commands, provisioning
from src.services.target.commands import CommandRefused
from tests.scripts.conftest import in_tenant, ingress_world
from tests.scripts.test_schedule_verbs_gate import (
    _command,
    _member,
    _sql,
    _workspace,
    refusal,
    run,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


#: The gates' shared world (`tests/scripts/conftest.py`), re-registered as this
#: module's own: a replayed schema, as its owner and as `svc_ingress`.
world = ingress_world


def _position_of(world, source):
    ((mode,),) = _sql(
        world, "SELECT approval_mode FROM media_sources WHERE id = %s", (source,)
    )
    return mode


def _audits(world, source):
    return _sql(
        world,
        "SELECT actor_kind, actor_user_id::text, channel, from_state, to_state,"
        "       detail FROM audit_events"
        " WHERE entity_kind = 'media_source' AND entity_id = %s ORDER BY id",
        (source,),
    )


class TestSetSourceApproval:
    def test_an_admin_sets_a_position_and_clears_it_with_an_audit_row_each(self, world):
        w = _workspace(world, "src-pos")

        result = run(
            world, w, "set_source_approval", source_id=w["src"], approval_mode="auto"
        )
        assert result.outcome == "executed"
        assert _position_of(world, w["src"]) == "auto"

        run(world, w, "set_source_approval", source_id=w["src"], approval_mode=None)
        assert _position_of(world, w["src"]) is None, "NULL is the workspace's"

        audits = _audits(world, w["src"])
        assert [row[5] for row in audits] == [
            {"v": 1, "event": "approval_mode", "from": None, "to": "auto"},
            {"v": 1, "event": "approval_mode", "from": "auto", "to": None},
        ]
        for actor_kind, actor, channel, from_state, to_state, _ in audits:
            assert (actor_kind, actor, channel) == ("user", w["user"], "cli")
            assert from_state == to_state == "active"

    def test_a_member_is_refused_at_the_floor_and_the_folder_keeps_its_position(
        self, world
    ):
        w = _workspace(world, "src-member")
        member = _member(world, w)

        with pytest.raises(TenantResolutionError) as refused:
            run(
                world,
                w,
                "set_source_approval",
                user=member,
                source_id=w["src"],
                approval_mode="auto",
            )

        assert refused.value.reason == "insufficient_role"
        assert _position_of(world, w["src"]) is None
        assert _audits(world, w["src"]) == []

    def test_another_workspaces_folder_is_not_found_and_keeps_its_position(self, world):
        mine = _workspace(world, "src-mine")
        theirs = _workspace(world, "src-theirs")
        run(
            world,
            theirs,
            "set_source_approval",
            source_id=theirs["src"],
            approval_mode="returning",
        )

        err = refusal(
            world,
            mine,
            "set_source_approval",
            source_id=theirs["src"],
            approval_mode="auto",
        )

        assert err.reason == "not_found"
        assert err.facts == {"missing": "source"}
        assert _position_of(world, theirs["src"]) == "returning"
        assert len(_audits(world, theirs["src"])) == 1

    def test_the_write_names_its_workspace_even_without_row_level_security(self, world):
        """The UPDATE names its workspace itself — the predicate is the first
        fence — so a login that bypasses the policies, the tables' owner here
        as a script would be, still cannot set another workspace's folder."""
        mine = _workspace(world, "src-owner-mine")
        theirs = _workspace(world, "src-owner-theirs")
        ((owner_role,),) = _sql(world, "SELECT current_user")
        command = _command(
            mine,
            "set_source_approval",
            mine["user"],
            {"source_id": theirs["src"], "approval_mode": "auto"},
        )
        with pytest.raises(CommandRefused) as err:
            asyncio.run(
                in_tenant(
                    world["owner"],
                    mine["ws"],
                    mine["user"],
                    lambda session: commands.execute(session, command),
                    channel="cli",
                    expect_user=owner_role,
                )
            )
        assert err.value.reason == "not_found"
        assert _position_of(world, theirs["src"]) is None

    def test_a_removed_folder_loses_its_position_and_takes_no_new_one(self, world):
        """A folder on YOLO, removed and picked again
        months later, would come back on YOLO with nobody asked. Its position
        goes with the removal, and a stale page cannot set one on it."""
        w = _workspace(world, "src-removed")
        run(world, w, "set_source_approval", source_id=w["src"], approval_mode="auto")

        removed = asyncio.run(
            in_tenant(
                world["ingress"],
                w["ws"],
                w["user"],
                lambda session: provisioning.pause_media_source(
                    session, workspace_id=w["ws"], source_id=w["src"]
                ),
            )
        )
        assert removed is True
        assert _position_of(world, w["src"]) is None, "it follows the workspace"

        err = refusal(
            world, w, "set_source_approval", source_id=w["src"], approval_mode="auto"
        )
        assert err.reason == "not_found"
        assert _position_of(world, w["src"]) is None
        audits = _audits(world, w["src"])
        assert [row[5] for row in audits] == [
            {"v": 1, "event": "approval_mode", "from": None, "to": "auto"},
            {
                "v": 1,
                "event": "approval_mode",
                "from": "auto",
                "to": None,
                "reason": "removed",
            },
        ], "the removal says it cleared the position, and the stale write wrote nothing"
