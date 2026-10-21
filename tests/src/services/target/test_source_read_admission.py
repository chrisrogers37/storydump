"""Malformed/inherited captures and diagnostic text fail closed."""

from contextlib import asynccontextmanager

import pytest
from src.services.target import source_read_admission as admission


def test_refusal_reason_is_closed_and_never_accepts_provider_text():
    with pytest.raises(ValueError):
        admission.SourceReadRefused("https://provider.invalid/?token=private", "source")


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        dict(v=1),
        dict(
            v=True,
            workspace_id="ws",
            source_id="sid",
            provider="instagram_past_posts",
            source_use_epoch=True,
            generation_id="g",
            credential_id="c",
            account_ref="account",
        ),
    ],
)
def test_inherited_capture_is_named_missing(value):
    with pytest.raises(admission.SourceReadRefused, match="source_capture_missing"):
        admission.capture_from_payload(value)


def test_boolean_capture_version_is_not_version_one():
    value = dict(
        v=True,
        workspace_id="ws",
        source_id="sid",
        provider="instagram_past_posts",
        source_use_epoch=1,
        generation_id="g",
        credential_id="c",
        account_ref="account",
    )
    with pytest.raises(admission.SourceReadRefused, match="source_capture_missing"):
        admission.capture_from_payload(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", sorted(admission.REASONS))
@pytest.mark.parametrize("hook", ["before_dispatch", "after_return"])
async def test_request_hooks_preserve_every_closed_read_refusal(
    monkeypatch, reason, hook
):
    capture = admission.SourceReadCapture(
        "workspace",
        "source",
        "instagram_past_posts",
        1,
        "generation",
        "credential",
        "account",
    )
    session = object()
    engine, capability = object(), object()
    events = []
    refused = False

    class FakeUnitOfWork:
        @asynccontextmanager
        async def begin(self):
            events.append("begin")
            try:
                yield session
            finally:
                events.append("closed")

    def fake_unit_of_work(actual_engine, workspace_id, *, actor_kind, lock_timeout):
        assert actual_engine is engine
        assert workspace_id == capture.workspace_id
        assert actor_kind == "worker"
        # The sync's admission asks for no bound.
        assert lock_timeout is None
        return FakeUnitOfWork()

    async def fake_check(actual_session, actual_capture, *, capability):
        assert actual_session is session
        assert actual_capture is capture
        assert capability is expected_capability
        events.append("checked")
        if refused:
            raise admission.SourceReadRefused(reason, capture.source_id)
        return capture

    expected_capability = capability
    monkeypatch.setattr(admission, "unit_of_work", fake_unit_of_work)
    monkeypatch.setattr(admission, "check_source_read", fake_check)
    request = admission._RequestAdmission(engine, capture, capability, None)
    if hook == "after_return":
        ticket = await request.before_dispatch()
        assert events == ["begin", "checked", "closed"]
        events.clear()
    refused = True
    with pytest.raises(admission.MediaSourceFenceDrop) as error:
        if hook == "before_dispatch":
            await request.before_dispatch()
        else:
            await request.after_return(ticket)
    assert error.value.reason == reason
    assert error.value.counters.source_fence_drop == 1
    assert error.value.__cause__ is None
    assert events == ["begin", "checked", "closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["before_dispatch", "after_return"])
@pytest.mark.parametrize("bound", [None, "2s"])
async def test_a_provider_may_bound_the_lock_wait_of_each_request_check(
    monkeypatch, hook, bound
):
    """An explicit preview asks for a lock timeout and its two checks per
    request carry it; a provider built without one (the sync's) carries None."""
    capture = admission.SourceReadCapture(
        "workspace", "source", "instagram_past_posts", 1, "g", "c", "account"
    )
    asked = []

    class FakeUnitOfWork:
        @asynccontextmanager
        async def begin(self):
            yield object()

    def fake_unit_of_work(engine, workspace_id, *, actor_kind, lock_timeout):
        asked.append(lock_timeout)
        return FakeUnitOfWork()

    async def fake_check(session, actual_capture, *, capability):
        return capture

    monkeypatch.setattr(admission, "unit_of_work", fake_unit_of_work)
    monkeypatch.setattr(admission, "check_source_read", fake_check)
    options = {} if bound is None else {"lock_timeout": bound}
    provide = admission.admission_provider(object(), capability=object(), **options)
    access = type("Access", (), {"captured_authorization": capture})()
    access.account_ref = capture.account_ref

    request = await provide("source", workspace_id="workspace", access=access)
    ticket = await request.before_dispatch()
    if hook == "after_return":
        await request.after_return(ticket)

    assert asked == [bound] * (1 if hook == "before_dispatch" else 2)
