"""`bindings` owns the chat-type mapping, and everything derived from it.

`_CHAT_TYPES` is the declared home ("**It lives here, and that placement is
the point**"). Two modules kept a hand-written `("group", "supergroup")`
beside it (#1325 audit, TD-B20); they now derive it, so a group-shaped chat
type added to the mapping reaches every reader at once.
"""

from __future__ import annotations

from src.services.target import bindings, channel_bind, membership_sync


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
