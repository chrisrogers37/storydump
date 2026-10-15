"""Claim checkpoint warnings expose only phase, exception class and count."""

from datetime import datetime, timezone
import logging

import pytest

from src.services.target import outbox

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
PRIVATE = (
    "synthetic-private-message",
    "synthetic-private-cause",
    "synthetic-private-payload",
)
ROW = {
    "id": "synthetic-private-row",
    "workspace_id": "synthetic-private-workspace",
    "binding_id": "synthetic-private-binding",
    "intent_id": "synthetic-private-intent",
    "payload": {"text": PRIVATE[2]},
}


class Session:
    """Only context and commit: no SQL or engine can run through this fake."""

    def __init__(self, fail_at, error, events):
        self.fail_at, self.error, self.events = fail_at, error, events

    async def __aenter__(self):
        self.events.append("enter")
        if self.fail_at == "enter":
            raise self.error
        return self

    async def commit(self):
        self.events.append("commit")
        if self.fail_at == "commit":
            raise self.error

    async def __aexit__(self, *args):
        self.events.append("close")
        if self.fail_at == "close":
            raise self.error


def poller_for(monkeypatch, *, fail_at=None, error=None, outcome=ROW):
    events = []

    def factory():
        events.append("factory")
        if fail_at == "factory":
            raise error
        return Session(fail_at, error, events)

    async def claim(session, **kwargs):
        events.append("claim")
        if fail_at == "claim":
            raise error
        return outcome

    async def transport(row):
        events.append("transport")
        assert row is ROW
        return "synthetic-receipt"

    async def settle(session, row, **kwargs):
        events.append("settle")
        assert row is ROW
        assert kwargs["receipt"] == "synthetic-receipt"
        assert kwargs["error"] is None
        return {**row, "state": "sent"}

    async def edit(session, row, receipt, *, force):
        events.append("edit")
        assert row is ROW and receipt == "synthetic-receipt" and not force
        return False

    monkeypatch.setattr(outbox, "pace_and_claim", claim)
    monkeypatch.setattr(outbox, "settle", settle)
    monkeypatch.setattr(outbox, "_edit_sent_card", edit)
    return outbox.OutboxPoller(
        factory,
        binding_id="synthetic-private-binding",
        transport=transport,
        clock=lambda: NOW,
        interval_seconds=2,
        chat_limit=20,
        chat_window_seconds=60,
        global_limit=30,
        global_window_seconds=1,
    ), events


@pytest.mark.parametrize("fail_at", ["factory", "enter", "claim", "commit", "close"])
async def test_claim_failure_is_reported_without_private_context(
    monkeypatch, caplog, fail_at
):
    error = ConnectionError(PRIVATE[0])
    error.__cause__ = ValueError(PRIVATE[1])
    error.payload = ROW
    poller, events = poller_for(monkeypatch, fail_at=fail_at, error=error)
    caplog.set_level(logging.WARNING, logger=outbox.__name__)

    for count in (1, 2):
        assert await poller.tick() is None
        assert poller.ticks == count and poller.consecutive_failures == count
        assert len(caplog.records) == count
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert record.name == outbox.__name__
        assert "phase=claim" in record.msg
        assert record.args == ("ConnectionError", count)
        assert (
            f"exception=ConnectionError consecutive_failures={count}"
            in record.getMessage()
        )
        assert record.exc_info is None and record.exc_text is None
        assert record.stack_info is None
        assert not any(isinstance(arg, BaseException) for arg in record.args)
        for marker in (
            *PRIVATE,
            *(value for key, value in ROW.items() if key != "payload"),
        ):
            assert marker not in str(record.__dict__)
            assert marker not in record.getMessage()
            assert marker not in caplog.text
    assert "transport" not in events and "settle" not in events and "edit" not in events
    assert poller.sent == poller.deferred == 0


@pytest.mark.parametrize("recovery", ["paced", "idle", "sent"])
async def test_a_retry_resets_failures_without_an_extra_warning(
    monkeypatch, caplog, recovery
):
    poller, _ = poller_for(
        monkeypatch, fail_at="claim", error=ConnectionError("synthetic")
    )
    caplog.set_level(logging.WARNING, logger=outbox.__name__)
    assert await poller.tick() is None
    assert poller.consecutive_failures == 1
    warnings = list(caplog.records)
    recovered, events = poller_for(
        monkeypatch,
        fail_at="claim" if recovery == "paced" else None,
        error=outbox.OutboxPaced() if recovery == "paced" else None,
        outcome=None if recovery == "idle" else ROW,
    )
    # Keep the real poller and its counters; change only its synthetic session seam.
    poller._session_factory = recovered._session_factory
    poller._transport = recovered._transport
    result = await poller.tick()
    assert poller.ticks == 2 and poller.consecutive_failures == 0
    assert caplog.records == warnings
    assert poller.deferred == int(recovery == "paced")
    assert poller.sent == int(recovery == "sent")
    assert poller.held_for_s == 0
    if recovery == "sent":
        assert result == {**ROW, "state": "sent"}
        assert events == [
            "factory",
            "enter",
            "claim",
            "commit",
            "close",
            "transport",
            "factory",
            "enter",
            "settle",
            "edit",
            "commit",
            "close",
        ]
    else:
        assert result is None
        assert "transport" not in events and "settle" not in events
