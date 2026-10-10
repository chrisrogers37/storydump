"""`bindings` owns the chat-type mapping, and everything derived from it.

`_CHAT_TYPES` is the declared home ("**It lives here, and that placement is
the point**"). Two modules kept a hand-written `("group", "supergroup")`
beside it (#1325 audit, TD-B20); they now derive it, so a group-shaped chat
type added to the mapping reaches every reader at once.
"""

from __future__ import annotations

import pytest

from src.services.target import bindings, channel_bind, membership_sync
from tests.src.services.target.test_provisioning import _ScriptedExecutor


class TestTheGroupChatTypesDeriveFromTheMapping:
    def test_the_group_half_is_exactly_what_it_was(self):
        assert bindings.GROUP_CHAT_TYPES == ("group", "supergroup")

    def test_every_member_maps_to_the_group_channel(self):
        assert bindings.GROUP_CHAT_TYPES
        for chat_type in bindings.GROUP_CHAT_TYPES:
            assert bindings.channel_for_chat_type(chat_type) == "telegram_group"

    def test_nothing_outside_it_maps_to_the_group_channel(self):
        for chat_type, channel in bindings._CHAT_TYPES.items():
            if channel == "telegram_group":
                assert chat_type in bindings.GROUP_CHAT_TYPES
            else:
                assert chat_type not in bindings.GROUP_CHAT_TYPES

    def test_the_two_re_derivations_are_now_the_same_object(self):
        assert channel_bind.GROUP_CHAT_TYPES is bindings.GROUP_CHAT_TYPES
        assert membership_sync.GROUP_CHAT_TYPES is bindings.GROUP_CHAT_TYPES


class _Recorder:
    """A scripted executor: records each statement and reports *rowcounts*
    in order."""

    def __init__(self, *rowcounts):
        self.rowcounts = list(rowcounts)
        self.statements = []

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), params))
        result = type("_Result", (), {})()
        result.rowcount = self.rowcounts.pop(0)
        return result


class TestRevokeForWorkspace:
    """An admin removes a group (`07` §13). The database half — RLS, the
    cross-tenant refusal, the queue really ending superseded — is the gate's
    (`tests/scripts/test_channel_bindings_writer.py`)."""

    async def test_revokes_then_supersedes_the_queue_in_one_transaction(self):
        session = _Recorder(1, 3)
        moved = await bindings.revoke_for_workspace(
            session, workspace_id="ws-1", binding_id="b-1"
        )
        assert moved is True
        (revoke_sql, p1), (supersede_sql, p2) = session.statements
        assert "UPDATE channel_bindings SET state = 'revoked'" in revoke_sql
        assert "workspace_id = :ws" in revoke_sql
        assert "state <> 'revoked'" in revoke_sql
        assert "UPDATE channel_outbox SET state = 'superseded'" in supersede_sql
        assert "workspace_id = :ws" in supersede_sql
        assert "state IN ('pending', 'ambiguous')" in supersede_sql
        assert p1 == p2 == {"ws": "ws-1", "b": "b-1"}

    async def test_nothing_moved_touches_no_queue(self):
        session = _Recorder(0)
        moved = await bindings.revoke_for_workspace(
            session, workspace_id="ws-1", binding_id="b-1"
        )
        assert moved is False
        assert len(session.statements) == 1, "a queue was touched for no binding"


class TestBindWritesOnlyAChatTypeItsCallerNames:
    """The writer's own rule: the mapping knows every bindable chat type, and
    a binding is written only for a type the caller names. That is a group's
    by default, and a private chat's only when asked for. The database half,
    the asked-for row really written beside a group's, is the gate's
    (`tests/scripts/test_channel_bindings_writer.py`)."""

    #: `bind`'s two statements for a chat nobody holds: no revoked row to
    #: bring back, then the insert reporting a new row.
    FRESH = ((0, None), (1, (True,)))

    async def test_a_private_chat_is_refused_before_the_database_sees_it(self):
        session = _ScriptedExecutor()
        with pytest.raises(bindings.BindingRefused) as refused:
            await bindings.bind(
                session, workspace_id="ws-1", chat_type="private", external_ref="42"
            )
        assert refused.value.reason == "chat_type_not_accepted"
        assert session.statements == [], "a refused bind reached the database"

    async def test_a_caller_that_names_a_private_chat_gets_it_bound(self):
        session = _ScriptedExecutor(*self.FRESH)
        outcome = await bindings.bind(
            session,
            workspace_id="ws-1",
            chat_type="private",
            external_ref="42",
            accepts=("private",),
        )
        assert outcome == bindings.BOUND
        (_, looked_up), (insert_sql, inserted) = session.statements
        assert "INSERT INTO channel_bindings" in insert_sql
        assert looked_up == inserted == {"ws": "ws-1", "ch": "telegram_dm", "ref": "42"}

    async def test_naming_a_private_chat_does_not_admit_a_group(self):
        """*accepts* selects, it does not widen: a caller that binds private
        chats cannot bind a group through the same call."""
        session = _ScriptedExecutor()
        with pytest.raises(bindings.BindingRefused) as refused:
            await bindings.bind(
                session,
                workspace_id="ws-1",
                chat_type="supergroup",
                external_ref="-10042",
                accepts=("private",),
            )
        assert refused.value.reason == "chat_type_not_accepted"
        assert session.statements == []

    @pytest.mark.parametrize("chat_type", bindings.GROUP_CHAT_TYPES)
    async def test_a_group_is_bound_without_asking(self, chat_type):
        session = _ScriptedExecutor(*self.FRESH)
        outcome = await bindings.bind(
            session, workspace_id="ws-1", chat_type=chat_type, external_ref="-10042"
        )
        assert outcome == bindings.BOUND
        assert session.statements[-1][1] == {
            "ws": "ws-1",
            "ch": "telegram_group",
            "ref": "-10042",
        }
