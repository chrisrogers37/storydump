"""L.5 slice 2 — the stub/sandbox Meta adapter and the error taxonomy (#915).

The pipeline never parses provider error dicts: the adapter raises TYPED
errors and the executor routes on type. The taxonomy IS the routing table,
so each class is pinned to the `02` §8 / R8 behavior it selects:

- ``MetaCapDeferral`` (code 9) → defer to the next slot, no quarantine.
- ``MetaRetryableError`` → definitive non-effect, retry on the job ladder.
- ``MetaTerminalError`` → definitive permanent failure → fail + refund.
- anything NOT a MetaError (transport loss) → AMBIGUOUS → park, zero retries.

Mutation discipline: each test's docstring names the mutation that reddens it.
"""

from __future__ import annotations

import pytest

from src.services.target.meta_adapter import (
    MetaCapDeferral,
    MetaError,
    MetaLostResponse,
    MetaRetryableError,
    MetaTerminalError,
    StubMetaAdapter,
    classify_error,
)


class TestTheErrorTaxonomy:
    def test_code_9_is_the_cap_deferral(self):
        """`02` §8: error 9 is Meta's publish cap — a cap, not a fault.
        Mutation that reddens: route 9 into the retryable set."""
        assert classify_error(9) is MetaCapDeferral

    @pytest.mark.parametrize("code", [4, 17, 2])
    def test_rate_and_transient_codes_are_retryable(self, code):
        """Meta answered — the effect definitively did not happen — and says
        try later (4/17 rate limits, 2 temporary). R8: definitive non-effects
        may retry on the job ladder.
        Mutation that reddens: drop any from the retryable set."""
        assert classify_error(code) is MetaRetryableError

    def test_known_permanent_media_errors_are_terminal(self):
        """9004 (cannot parse the uploaded file, per the legacy taxonomy) can
        never succeed by retrying — terminal, fail + refund.
        Mutation that reddens: drop 9004 from the terminal set."""
        assert classify_error(9004) is MetaTerminalError

    def test_an_unknown_code_defaults_to_retryable_not_terminal(self):
        """The SAFE default for an unknown-but-definitive provider error is
        retryable: the G5 poison ladder (attempts exhausted → review_required
        → a human) bounds a persistent unknown, while defaulting to terminal
        would refund + permanently fail intents on any new error code Meta
        mints. Mutation that reddens: default to terminal."""
        assert classify_error(999999) is MetaRetryableError

    def test_every_class_is_a_meta_error_carrying_its_code(self):
        """The executor discriminates AMBIGUOUS from DEFINITIVE by
        `isinstance(exc, MetaError)` — a class outside the hierarchy would be
        parked as ambiguous. And the code rides on the instance for evidence.
        Mutation that reddens: unhook a class from MetaError."""
        for cls in (MetaCapDeferral, MetaRetryableError, MetaTerminalError):
            assert issubclass(cls, MetaError)
        err = MetaCapDeferral(code=9, message="cap hit")
        assert err.code == 9
        assert "cap hit" in str(err)


class TestTheStubAdapter:
    @pytest.mark.asyncio
    async def test_create_container_returns_distinct_ids_and_records_the_call(self):
        """The stub is the sandbox until M.3 AND the gate's counting
        instrument (the L.3 lesson: a stub that records every invocation).
        Mutation that reddens: stop recording, or return a constant id."""
        stub = StubMetaAdapter()
        c1 = await stub.create_container(
            "igu-1", media_url="https://u/1", media_kind="image"
        )
        c2 = await stub.create_container(
            "igu-1", media_url="https://u/2", media_kind="image"
        )
        assert c1 != c2
        assert [c["media_url"] for c in stub.create_calls] == [
            "https://u/1",
            "https://u/2",
        ]

    @pytest.mark.asyncio
    async def test_containers_become_finished_after_the_configured_polls(self):
        """ready_after_polls models Meta's async container processing: the
        readiness poll must actually poll.
        Mutation that reddens: return FINISHED unconditionally."""
        stub = StubMetaAdapter(ready_after_polls=2)
        cid = await stub.create_container(
            "igu-1", media_url="https://u", media_kind="image"
        )
        assert await stub.container_status(cid) == "IN_PROGRESS"
        assert await stub.container_status(cid) == "IN_PROGRESS"
        assert await stub.container_status(cid) == "FINISHED"

    @pytest.mark.asyncio
    async def test_publish_scripts_run_in_order_and_count_calls(self):
        """publish outcomes are a SCRIPT so a test can arm error-9-then-ok
        (the deferral-then-next-slot-success shape). The count is the
        at-most-once instrument. Mutation that reddens: not consuming the
        script, or not raising the armed outcome."""
        stub = StubMetaAdapter(publish_outcomes=["error_9", "ok"])
        cid = await stub.create_container(
            "igu-1", media_url="https://u", media_kind="image"
        )
        with pytest.raises(MetaCapDeferral):
            await stub.publish("igu-1", cid)
        media_id = await stub.publish("igu-1", cid)
        assert media_id.startswith("media-")
        assert len(stub.publish_calls) == 2

    @pytest.mark.asyncio
    async def test_transport_loss_is_typed_but_is_not_an_answer(self):
        """The ambiguity discriminator: a lost response raises
        MetaLostResponse, which is deliberately OUTSIDE the MetaError
        hierarchy — MetaErrors are definitive ANSWERS, and a stub that wrapped
        transport loss in one would silently turn every park-proof green the
        wrong way. Mutation that reddens: make MetaLostResponse a MetaError,
        or raise a MetaError for 'transport'."""
        stub = StubMetaAdapter(publish_outcomes=["transport"])
        cid = await stub.create_container(
            "igu-1", media_url="https://u", media_kind="image"
        )
        with pytest.raises(MetaLostResponse) as exc_info:
            await stub.publish("igu-1", cid)
        assert not isinstance(exc_info.value, MetaError)

    @pytest.mark.asyncio
    async def test_retryable_and_terminal_outcomes_raise_their_types(self):
        """Mutation that reddens: cross-wire the outcome→type table."""
        stub = StubMetaAdapter(publish_outcomes=["retryable", "terminal"])
        cid = await stub.create_container(
            "igu-1", media_url="https://u", media_kind="image"
        )
        with pytest.raises(MetaRetryableError):
            await stub.publish("igu-1", cid)
        with pytest.raises(MetaTerminalError):
            await stub.publish("igu-1", cid)

    @pytest.mark.asyncio
    async def test_usage_reports_the_configured_quota_and_counts(self):
        """The §8 pre-check's one read. Mutation that reddens: not recording
        usage calls (the pre-check cache proof counts them)."""
        stub = StubMetaAdapter(quota_usage=97, quota_total=100)
        u = await stub.usage("igu-1")
        assert u == {"quota_usage": 97, "quota_total": 100}
        assert stub.usage_calls == ["igu-1"]

    @pytest.mark.asyncio
    async def test_container_status_can_be_scripted_to_error(self):
        """The dead-container path (readiness ERROR/EXPIRED) needs arming.
        Mutation that reddens: ignore status_script."""
        stub = StubMetaAdapter(status_script=["ERROR"])
        cid = await stub.create_container(
            "igu-1", media_url="https://u", media_kind="image"
        )
        assert await stub.container_status(cid) == "ERROR"
