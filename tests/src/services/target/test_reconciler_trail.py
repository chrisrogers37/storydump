"""The ambiguous-publish ladder's trail keeps a dead container's own reason:
Meta's `status` text, the whole of its answer for a container it would not
process (`instagram_graph.ContainerStatus`)."""

from __future__ import annotations

from src.services.target import reconciler
from src.services.target.instagram_graph import ContainerStatus


async def test_a_dead_containers_reason_rides_the_trail(monkeypatch):
    ended = []

    async def terminalize(conn, *, intent_id, state, trail):
        ended.append((state, trail))

    monkeypatch.setattr(reconciler, "_terminalize", terminalize)
    reason = "Error: Media upload has failed with error code 2207026"
    outcome = await reconciler.reconcile_intent(
        None,
        intent_id="i",
        workspace_id="ws",
        status_code=ContainerStatus("ERROR", reason),
    )
    assert outcome == "failed"
    assert ended == [
        ("failed", [{"status_code": "ERROR", "check": 1, "status": reason}])
    ]


async def test_a_plain_status_adds_nothing(monkeypatch):
    ended = []

    async def terminalize(conn, *, intent_id, state, trail):
        ended.append(trail)

    monkeypatch.setattr(reconciler, "_terminalize", terminalize)
    await reconciler.reconcile_intent(
        None, intent_id="i", workspace_id="ws", status_code="ERROR"
    )
    assert ended == [[{"status_code": "ERROR", "check": 1}]]
