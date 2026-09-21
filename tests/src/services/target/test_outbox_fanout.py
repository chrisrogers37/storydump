"""`outbox.fanout_notification` — the envelope every customer notice rides in.

`06` §5 routes every customer-visible failure to "the workspace's bindings",
and the loop that does it had been written out at each producer. Now that the
producers go through this door, the door is where the `{v, text}` envelope,
the `notification` kind and the str() coercions are pinned — a change to any
of them is one edit here and one failing test, not a silent divergence
between the sync sweep, the reauth prompt and the pipeline.
"""

from __future__ import annotations

import pytest

from src.services.target import outbox


@pytest.fixture()
def enqueued(monkeypatch):
    """Capture every `enqueue` call `fanout_notification` makes."""
    calls: list[dict] = []

    async def enqueue(session, **kw):
        calls.append(kw)
        return f"row-{len(calls)}"

    monkeypatch.setattr(outbox, "enqueue", enqueue)
    return calls


@pytest.mark.unit
async def test_one_row_per_binding_and_the_count_is_returned(enqueued):
    written = await outbox.fanout_notification(
        object(), workspace_id="ws-1", bindings=["b-1", "b-2", "b-3"], text="hi"
    )
    assert written == 3
    assert [c["binding_id"] for c in enqueued] == ["b-1", "b-2", "b-3"]


@pytest.mark.unit
async def test_the_envelope_is_exactly_v1_and_text(enqueued):
    await outbox.fanout_notification(
        object(), workspace_id="ws-1", bindings=["b-1"], text="⚠️ something broke"
    )
    assert enqueued[0]["payload"] == {"v": 1, "text": "⚠️ something broke"}
    assert enqueued[0]["kind"] == "notification"


@pytest.mark.unit
async def test_no_bindings_writes_nothing_and_returns_zero(enqueued):
    """The producer decides what "nobody to tell" means; this is the
    zero-iteration loop the copies had, not a silence the door invents."""
    assert (
        await outbox.fanout_notification(
            object(), workspace_id="ws-1", bindings=[], text="hi"
        )
        == 0
    )
    assert enqueued == []


@pytest.mark.unit
async def test_the_ids_are_coerced_to_str(enqueued):
    """Callers pass a UUID or a row value as readily as a string."""
    import uuid

    ws, intent = uuid.uuid4(), uuid.uuid4()
    await outbox.fanout_notification(
        object(), workspace_id=ws, bindings=["b-1"], text="hi", intent_id=intent
    )
    assert enqueued[0]["workspace_id"] == str(ws)
    assert enqueued[0]["intent_id"] == str(intent)


@pytest.mark.unit
async def test_no_intent_stays_null(enqueued):
    """A notification that belongs to no story must not borrow one."""
    await outbox.fanout_notification(
        object(), workspace_id="ws-1", bindings=["b-1"], text="hi"
    )
    assert enqueued[0]["intent_id"] is None
