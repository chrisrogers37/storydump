"""An item's link (#1413 phase 7), EXECUTED — as `svc_ingress`, through the
real port, on the replayed target schema.

What only PostgreSQL can say, and so is pinned here rather than in
`tests/src/services/target/test_set_item_link.py`:

- **The login may write it.** `svc_ingress` holds UPDATE on `media_items`
  (057), so the link needs no DDL.
- **Only this workspace's item.** Another workspace's item is `not_found`
  and keeps its link: the predicate and the tenant policy both stand.
- **Every change names the person** in `audit_events`, with the item's own
  state on both sides, since a link is not a state.
"""

from __future__ import annotations

import pytest

from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    set_test_passwords,
)
from tests.scripts.test_schedule_verbs_gate import (
    _item,
    _member,
    _sql,
    _workspace,
    refusal,
    run,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

LINK = "https://example.com/spring-sale"


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"owner": owner, "ingress": as_user(owner, "svc_ingress")}
    finally:
        gen.close()


def _link_of(world, item):
    ((link,),) = _sql(world, "SELECT link_url FROM media_items WHERE id = %s", (item,))
    return link


def _audits(world, item):
    return _sql(
        world,
        "SELECT actor_kind, actor_user_id::text, channel, from_state, to_state,"
        "       detail FROM audit_events"
        " WHERE entity_kind = 'media_item' AND entity_id = %s ORDER BY id",
        (item,),
    )


class TestSetItemLink:
    def test_a_member_sets_it_and_the_audit_names_them(self, world):
        w = _workspace(world, "link-set")
        item = _item(world, w)
        member = _member(world, w)

        result = run(
            world, w, "set_item_link", user=member, media_item_id=item, link_url=LINK
        )

        assert result.outcome == "executed"
        assert result.data == {"media_item_id": item, "link_url": LINK}
        assert _link_of(world, item) == LINK
        ((actor, by, channel, from_state, to_state, detail),) = _audits(world, item)
        assert (actor, by, channel) == ("user", member, "cli")
        assert from_state == to_state == "available"
        assert detail == {"v": 1, "event": "link_set", "link_url": LINK}

    def test_null_clears_it(self, world):
        w = _workspace(world, "link-clear")
        item = _item(world, w)

        run(world, w, "set_item_link", media_item_id=item, link_url=LINK)
        run(world, w, "set_item_link", media_item_id=item, link_url=None)

        assert _link_of(world, item) is None
        events = [detail["event"] for *_, detail in _audits(world, item)]
        assert events == ["link_set", "link_cleared"]

    def test_an_item_that_is_not_the_workspaces_is_not_found_and_keeps_its_link(
        self, world
    ):
        mine = _workspace(world, "link-mine")
        theirs = _workspace(world, "link-theirs")
        their_item = _item(world, theirs)
        run(world, theirs, "set_item_link", media_item_id=their_item, link_url=LINK)

        err = refusal(
            world,
            mine,
            "set_item_link",
            media_item_id=their_item,
            link_url="https://example.com/someone-else",
        )

        assert err.reason == "not_found"
        assert err.facts == {"missing": "item"}
        assert _link_of(world, their_item) == LINK
        assert len(_audits(world, their_item)) == 1

    def test_a_refused_link_writes_nothing(self, world):
        w = _workspace(world, "link-refused")
        item = _item(world, w)

        err = refusal(
            world,
            w,
            "set_item_link",
            media_item_id=item,
            link_url="http://example.com/spring-sale",
        )

        assert err.reason == "invalid_args"
        assert _link_of(world, item) is None
        assert _audits(world, item) == []
