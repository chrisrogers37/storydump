"""An item's Auto-approve flag, EXECUTED — as `svc_ingress`,
through the real port, on the replayed target schema.

What only PostgreSQL can say, and so is pinned here rather than in
`tests/src/services/target/test_command_executors.py`:

- **The login may write it.** `svc_ingress` holds UPDATE on `media_items`
  (`09_grants.sql`), so approval rules' column needs no grant of its own.
- **A member may set it**: the port's gate admits the `member` floor.
- **Only this workspace's item.** Under the tenant's policy another
  workspace's item is `not_found`, and it keeps its flag.
- **A removed item is refused, and nothing is written.** The UPDATE skips
  a removed item, and the read after its miss names the refusal `locked`.
- **Every change names the person** in `audit_events`, with the item's own
  state on both sides, since a flag is not a state.
"""

from __future__ import annotations

import asyncio

import pytest

from src.services.target import commands
from src.services.target.commands import CommandRefused

from tests.scripts.conftest import in_tenant, ingress_world
from tests.scripts.test_set_item_link_gate import _audits
from tests.scripts.test_schedule_verbs_gate import (
    _command,
    _item,
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


def _flag_of(world, item):
    ((flag,),) = _sql(
        world, "SELECT auto_approve FROM media_items WHERE id = %s", (item,)
    )
    return flag


class TestSetItemAutoApprove:
    def test_the_write_names_its_workspace_even_without_row_level_security(self, world):
        """The UPDATE names its workspace itself — the predicate is the first
        fence — so a login that bypasses the policies, the tables' owner here
        as a script would be, still cannot flag another workspace's item. The
        read after its miss names it too, so the item is `not_found`, never
        `locked`. Under `svc_ingress` the policy would hide a missing
        predicate; this arm runs with the policies out."""
        mine = _workspace(world, "flag-owner-mine")
        theirs = _workspace(world, "flag-owner-theirs")
        their_item = _item(world, theirs)
        ((owner_role,),) = _sql(world, "SELECT current_user")
        command = _command(
            mine,
            "set_item_auto_approve",
            mine["user"],
            {"media_item_id": their_item, "auto_approve": True},
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
        assert _flag_of(world, their_item) is False

    def test_a_member_sets_it_and_the_audit_names_them(self, world):
        w = _workspace(world, "flag-set")
        item = _item(world, w)
        member = _member(world, w)

        result = run(
            world,
            w,
            "set_item_auto_approve",
            user=member,
            media_item_id=item,
            auto_approve=True,
        )

        assert result.outcome == "executed"
        assert result.data == {"media_item_id": item, "auto_approve": True}
        assert _flag_of(world, item) is True
        ((actor, by, channel, from_state, to_state, detail),) = _audits(world, item)
        assert (actor, by, channel) == ("user", member, "cli")
        assert from_state == to_state == "available"
        assert detail == {"v": 1, "event": "auto_approve", "on": True}

    def test_false_clears_it(self, world):
        w = _workspace(world, "flag-clear")
        item = _item(world, w)

        run(world, w, "set_item_auto_approve", media_item_id=item, auto_approve=True)
        run(world, w, "set_item_auto_approve", media_item_id=item, auto_approve=False)

        assert _flag_of(world, item) is False
        assert [detail["on"] for *_, detail in _audits(world, item)] == [True, False]

    def test_an_item_that_is_not_the_workspaces_is_not_found_and_keeps_its_flag(
        self, world
    ):
        mine = _workspace(world, "flag-mine")
        theirs = _workspace(world, "flag-theirs")
        their_item = _item(world, theirs)
        run(
            world,
            theirs,
            "set_item_auto_approve",
            media_item_id=their_item,
            auto_approve=True,
        )

        err = refusal(
            world,
            mine,
            "set_item_auto_approve",
            media_item_id=their_item,
            auto_approve=False,
        )

        assert err.reason == "not_found"
        assert err.facts == {"missing": "item"}
        assert _flag_of(world, their_item) is True
        assert len(_audits(world, their_item)) == 1

    def test_a_removed_item_is_refused_and_keeps_its_flag(self, world):
        w = _workspace(world, "flag-removed")
        item = _item(world, w, state="removed")

        err = refusal(
            world, w, "set_item_auto_approve", media_item_id=item, auto_approve=True
        )

        assert err.reason == "locked"
        assert err.facts == {"in_the_way": ["item_removed"], "overridable": False}
        assert _flag_of(world, item) is False
        assert _audits(world, item) == []
