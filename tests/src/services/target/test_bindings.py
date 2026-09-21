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
