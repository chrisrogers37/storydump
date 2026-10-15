"""A group's migration notice (#743): which updates are one, and what the
handler decides from the resolver's answer. What PostgreSQL does with the move
— the policy, the audit trigger, the unique key — is the gate's
(`tests/scripts/test_channel_bindings_writer.py`,
`TestAGroupThatBecameASupergroupKeepsItsWorkspace`)."""

from __future__ import annotations

import pytest

from src.services.target import bindings, chat_migration
from src.services.target.tenant_resolution import ResolvedTenant, TenantResolutionError


def _message(**fields) -> dict:
    return {"update_id": 1, "message": {"message_id": 5, "date": 1790000000, **fields}}


class TestWhichUpdatesAreAMigration:
    def test_the_old_groups_notice_names_old_then_new(self):
        update = _message(
            chat={"id": -4012345678, "type": "group"}, migrate_to_chat_id=-1009876543210
        )
        assert chat_migration.migration_of(update) == ("-4012345678", "-1009876543210")

    def test_the_new_supergroups_notice_names_old_then_new(self):
        update = _message(
            chat={"id": -1009876543210, "type": "supergroup"},
            migrate_from_chat_id=-4012345678,
        )
        assert chat_migration.migration_of(update) == ("-4012345678", "-1009876543210")

    @pytest.mark.parametrize(
        "update",
        [
            _message(chat={"id": -100777, "type": "supergroup"}, text="hello"),
            _message(chat={"id": 42, "type": "private"}, text="/start"),
            _message(migrate_to_chat_id=-1009876543210),  # no chat to retire
            {"update_id": 1, "callback_query": {"id": "q", "data": "v1:skip:x"}},
            {"update_id": 1, "edited_message": {"chat": {"id": -1}}},
        ],
        ids=["group-speech", "dm", "chatless", "tap", "edit"],
    )
    def test_anything_else_is_not(self, update):
        assert chat_migration.migration_of(update) is None


@pytest.fixture
def seams(monkeypatch):
    """The resolver, the GUC statement and the writer, recorded in order."""
    seen: dict = {"calls": [], "resolves_to": None, "follows": True}

    async def resolve_chat(conn, channel, ref):
        seen["calls"].append(("resolve", channel, ref))
        if isinstance(seen["resolves_to"], Exception):
            raise seen["resolves_to"]
        return seen["resolves_to"]

    async def apply_gucs(conn, **gucs):
        seen["calls"].append(("gucs", gucs))

    async def follow_or_retire(conn, *, binding_id, successor):
        seen["calls"].append(("follow", binding_id, successor))
        if isinstance(seen["follows"], Exception):
            raise seen["follows"]
        return seen["follows"]

    monkeypatch.setattr(chat_migration.tenant_resolution, "resolve_chat", resolve_chat)
    monkeypatch.setattr(chat_migration.unit_of_work, "apply_gucs", apply_gucs)
    monkeypatch.setattr(chat_migration.bindings, "follow_or_retire", follow_or_retire)
    return seen


BOUND = ResolvedTenant(workspace_id="ws-1", channel_binding_id="b-1", via="chat")


class TestFollowingTheChat:
    async def test_the_binding_follows_under_its_own_tenant_as_the_system(self, seams):
        seams["resolves_to"] = BOUND
        result = await chat_migration.follow(None, old_ref="-401", new_ref="-100401")
        assert (result.outcome, result.handled, result.reply) == (
            chat_migration.FOLLOWED,
            True,
            None,
        )
        assert seams["calls"] == [
            ("resolve", "telegram_group", "-401"),
            (
                "gucs",
                {"tenant_id": "ws-1", "actor_kind": "system", "channel": "telegram"},
            ),
            ("follow", "b-1", "-100401"),
        ]

    async def test_a_successor_held_elsewhere_is_reported_as_retired(self, seams):
        seams["resolves_to"], seams["follows"] = BOUND, False
        result = await chat_migration.follow(None, old_ref="-401", new_ref="-100401")
        assert (result.outcome, result.handled) == (chat_migration.RETIRED, True)

    @pytest.mark.parametrize("reason", ["unknown_binding", "revoked_binding"])
    async def test_an_old_id_nothing_holds_writes_nothing(self, seams, reason):
        """The second notice of every pair lands here: the first already moved
        the binding. A named no-op, never a raise — the delivery is admitted."""
        seams["resolves_to"] = TenantResolutionError(reason)
        result = await chat_migration.follow(None, old_ref="-401", new_ref="-100401")
        assert (result.outcome, result.handled) == (f"migration_{reason}", False)
        assert seams["calls"] == [("resolve", "telegram_group", "-401")]

    async def test_a_successor_the_writer_refuses_is_named_not_raised(self, seams):
        seams["resolves_to"] = BOUND
        seams["follows"] = bindings.BindingRefused("external_ref_malformed")
        result = await chat_migration.follow(None, old_ref="-401", new_ref="x")
        assert (result.outcome, result.handled) == (
            "migration_external_ref_malformed",
            False,
        )
