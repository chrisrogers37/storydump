"""An item's link to add by hand (#1413 phase 7, F10 (a)), at the SQL seam:
the statement `set_item_link` sends and its audit row, and the refusals
decided in Python before the database is asked (the item's id, and the link's
scheme, characters and length).

What only PostgreSQL can say (the tenant's policy, the login's grant) is the
gate's: `tests/scripts/test_set_item_link_gate.py`.
"""

from __future__ import annotations

import json

import pytest

from src.services.target.commands import Command, CommandRefused  # noqa: I001 — the port first: the registry cycle
from src.services.target import command_executors, vocabulary
from tests.src.services.target.test_provisioning import _ScriptedExecutor

WS = "7a1c3d5e-0000-4000-8000-000000000001"
USER = "7a1c3d5e-0000-4000-8000-000000000002"
ITEM = "7a1c3d5e-0000-4000-8000-00000000000b"
LINK = "https://example.com/spring-sale"
BASE = "https://example.com/"


def _link(**args) -> Command:
    return Command(
        kind="set_item_link",
        workspace_id=WS,
        actor_user_id=USER,
        channel="cli",
        args={"media_item_id": ITEM, **args},
    )


def _found(state: str = "available") -> _ScriptedExecutor:
    """The item is the workspace's: the write returns its state, then the audit lands."""
    return _ScriptedExecutor((1, {"state": state}), (1, None))


class TestTheLinkIsWritten:
    async def test_on_the_workspaces_own_item_with_an_audit_row(self):
        ex = _found()
        result = await command_executors.set_item_link(ex, _link(link_url=LINK))

        assert result.outcome == "executed"
        assert result.data == {"media_item_id": ITEM, "link_url": LINK}
        sql, params = ex.statements[0]
        assert "UPDATE media_items SET link_url = :link" in sql
        assert "WHERE id = :item AND workspace_id = :ws" in sql
        assert params == {"link": LINK, "item": ITEM, "ws": WS}
        audit_sql, audit = ex.statements[1]
        assert "INSERT INTO audit_events" in audit_sql
        assert audit["entity_kind"] == "media_item"
        assert audit["item"] == ITEM
        # A link is not a state: the row carries the item's own on both sides.
        assert audit["from_state"] == audit["to_state"] == "available"
        assert json.loads(audit["detail"]) == {
            "v": 1,
            "event": "link_set",
            "link_url": LINK,
        }

    async def test_null_clears_it_and_the_audit_says_so(self):
        ex = _found()
        result = await command_executors.set_item_link(ex, _link(link_url=None))

        assert result.data == {"media_item_id": ITEM, "link_url": None}
        assert ex.statements[0][1]["link"] is None
        assert json.loads(ex.statements[1][1]["detail"]) == {
            "v": 1,
            "event": "link_cleared",
            "link_url": None,
        }

    async def test_without_the_spaces_around_it(self):
        ex = _found()
        await command_executors.set_item_link(ex, _link(link_url=f"  {LINK}\n"))
        assert ex.statements[0][1]["link"] == LINK

    async def test_whatever_case_its_scheme_is_written_in(self):
        ex = _found()
        await command_executors.set_item_link(
            ex, _link(link_url="HTTPS://Example.com/a")
        )
        assert ex.statements[0][1]["link"] == "HTTPS://Example.com/a"

    async def test_at_exactly_the_cap(self):
        at_cap = BASE + "a" * (vocabulary.LINK_URL_MAX - len(BASE))
        ex = _found()
        await command_executors.set_item_link(ex, _link(link_url=at_cap))
        assert ex.statements[0][1]["link"] == at_cap

    async def test_an_item_that_is_not_the_workspaces_is_not_found_and_not_audited(
        self,
    ):
        ex = _ScriptedExecutor((0, None))
        with pytest.raises(CommandRefused) as refused:
            await command_executors.set_item_link(ex, _link(link_url=LINK))
        assert refused.value.reason == "not_found"
        assert refused.value.facts == {"missing": "item"}
        assert len(ex.statements) == 1


class TestRefusedBeforeTheDatabaseIsAsked:
    @pytest.mark.parametrize(
        "link_url",
        [
            "http://example.com/spring-sale",
            "javascript:alert(1)",
            "ftp://example.com/file",
            "example.com/spring-sale",
            "https://",
            "https:///no-host",
            "https://example.com/spring sale",
            # A line break inside would put a second line on the card.
            "https://example.com/a\nb",
            "https://example.com/a\tb",
            "",
            "   ",
        ],
    )
    async def test_anything_but_an_https_link(self, link_url):
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.set_item_link(ex, _link(link_url=link_url))
        assert refused.value.reason == "invalid_args"
        assert ex.statements == []

    async def test_one_character_over_the_cap(self):
        over = BASE + "a" * (vocabulary.LINK_URL_MAX - len(BASE) + 1)
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.set_item_link(ex, _link(link_url=over))
        assert refused.value.reason == "invalid_args"
        assert ex.statements == []

    async def test_no_link_url_at_all_rather_than_reading_it_as_a_clear(self):
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.set_item_link(ex, _link())
        assert refused.value.reason == "invalid_args"
        assert ex.statements == []

    async def test_a_link_url_that_is_not_text(self):
        ex = _ScriptedExecutor()
        with pytest.raises(CommandRefused) as refused:
            await command_executors.set_item_link(ex, _link(link_url=42))
        assert refused.value.reason == "invalid_args"
        assert ex.statements == []

    async def test_an_item_id_that_is_not_one(self):
        ex = _ScriptedExecutor()
        command = Command(
            kind="set_item_link",
            workspace_id=WS,
            actor_user_id=USER,
            channel="cli",
            args={"media_item_id": "not-an-id", "link_url": LINK},
        )
        with pytest.raises(CommandRefused) as refused:
            await command_executors.set_item_link(ex, command)
        assert refused.value.reason == "invalid_args"
        assert ex.statements == []
