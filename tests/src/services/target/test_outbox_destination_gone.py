"""`outbox.deliver` on a DEFINITIVE refusal: the destination is gone. Not the
ambiguous case — the row fails and the caller learns to retire the binding
(#1240 review: a kicked bot is not a dead token)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services.target import outbox


@pytest.fixture()
def floor(monkeypatch):
    seen = {"left": [], "ambiguous": []}

    async def recover_stranded(session, *, binding_id):
        return []

    async def increment(session, **kw):
        return 1

    async def claim_next(session, *, binding_id):
        return {
            "id": "row-1",
            "binding_id": binding_id,
            "payload": {"v": 1, "text": "hi"},
        }

    async def _leave_sending(session, outbox_id, to_state, **extra):
        seen["left"].append((outbox_id, to_state, extra))

    async def mark_ambiguous(session, *, outbox_id):
        seen["ambiguous"].append(outbox_id)

    monkeypatch.setattr(outbox, "recover_stranded", recover_stranded)
    monkeypatch.setattr(outbox, "increment", increment)
    monkeypatch.setattr(outbox, "claim_next", claim_next)
    monkeypatch.setattr(outbox, "_leave_sending", _leave_sending)
    monkeypatch.setattr(outbox, "mark_ambiguous", mark_ambiguous)
    return seen


def _kwargs(transport):
    return dict(
        binding_id="b-1",
        transport=transport,
        now=datetime.now(timezone.utc),
        chat_limit=10,
        chat_window_seconds=60,
        global_limit=100,
        global_window_seconds=60,
    )


async def test_a_gone_destination_fails_the_row_and_says_so(floor):
    async def transport(row):
        raise outbox.DestinationGone("kicked", migrate_to=None)

    result = await outbox.deliver(object(), **_kwargs(transport))
    assert result["state"] == "failed" and result["destination_gone"] is True
    assert result["migrate_to"] is None
    assert floor["left"] == [("row-1", "failed", {})]
    assert floor["ambiguous"] == []


async def test_a_moved_destination_carries_the_successor(floor):
    async def transport(row):
        raise outbox.DestinationGone("upgraded", migrate_to="-1009999")

    result = await outbox.deliver(object(), **_kwargs(transport))
    assert result["destination_gone"] and result["migrate_to"] == "-1009999"


async def test_a_lost_response_is_still_the_ambiguous_case(floor):
    async def transport(row):
        raise RuntimeError("timeout")

    result = await outbox.deliver(object(), **_kwargs(transport))
    assert result["state"] == "ambiguous"
    assert floor["ambiguous"] == ["row-1"] and floor["left"] == []
