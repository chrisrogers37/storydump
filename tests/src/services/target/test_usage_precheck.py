"""L.5 slice 2 — the lazy inline usage pre-check (#915, `02` §8).

Advisory ONLY, behind a default-off flag (the flag lives at the executor
seam; this module is the cache + decision). The binding rules from §8:
in-process cache keyed on provider_account_ref (shared across duplicate
workspace rows of one real account), TTL per `05` (5 min), NO background
refresh, ≤ one usage query per publish attempt, and miss/stale/error ⇒
PROCEED — error 9 remains the arbiter.
"""

from __future__ import annotations

import pytest

from src.services.target.meta_adapter import StubMetaAdapter
from src.services.target.usage_precheck import UsagePrecheck


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestTheDecision:
    @pytest.mark.asyncio
    async def test_under_cap_proceeds_with_exactly_one_read(self):
        """Mutation that reddens: a second read per check, or defer under cap."""
        stub = StubMetaAdapter(quota_usage=3, quota_total=100)
        check = UsagePrecheck(ttl_seconds=300, clock=FakeClock())
        assert await check.check(stub, "igu-1") == "proceed"
        assert len(stub.usage_calls) == 1

    @pytest.mark.asyncio
    async def test_at_or_over_cap_defers(self):
        """The advisory's one job: skip doomed container/transit work when
        Meta's own counter says the publish cannot land.
        Mutation that reddens: compare with > instead of >=."""
        stub = StubMetaAdapter(quota_usage=100, quota_total=100)
        check = UsagePrecheck(ttl_seconds=300, clock=FakeClock())
        assert await check.check(stub, "igu-1") == "defer"

    @pytest.mark.asyncio
    async def test_a_provider_error_proceeds_and_is_not_cached(self):
        """§8 verbatim: miss/stale/error ⇒ proceed — error 9 remains the
        arbiter. And a failed read must not poison the cache: the NEXT check
        re-reads. Mutation that reddens: defer on error, or cache the error."""
        stub = StubMetaAdapter(
            usage_outcomes=["transport", "ok"], quota_usage=1, quota_total=100
        )
        check = UsagePrecheck(ttl_seconds=300, clock=FakeClock())
        assert await check.check(stub, "igu-1") == "proceed"
        assert await check.check(stub, "igu-1") == "proceed"
        assert len(stub.usage_calls) == 2, "the error must not have been cached"


class TestTheCache:
    @pytest.mark.asyncio
    async def test_a_fresh_cache_answers_without_a_second_read(self):
        """§8's load bound (≤ 1 query per attempt; ~520/min eager reading
        struck): within the TTL the cache answers, including the DEFER answer
        — duplicate workspace rows of one real account share the verdict.
        Mutation that reddens: read on every check."""
        stub = StubMetaAdapter(quota_usage=100, quota_total=100)
        clock = FakeClock()
        check = UsagePrecheck(ttl_seconds=300, clock=clock)
        assert await check.check(stub, "igu-1") == "defer"
        clock.t += 299
        assert await check.check(stub, "igu-1") == "defer"
        assert len(stub.usage_calls) == 1

    @pytest.mark.asyncio
    async def test_a_stale_cache_rereads(self):
        """No background refresh exists (§8) — staleness is resolved lazily,
        at the next check. Mutation that reddens: never expiring the cache."""
        stub = StubMetaAdapter(quota_usage=1, quota_total=100)
        clock = FakeClock()
        check = UsagePrecheck(ttl_seconds=300, clock=clock)
        await check.check(stub, "igu-1")
        clock.t += 301
        await check.check(stub, "igu-1")
        assert len(stub.usage_calls) == 2

    @pytest.mark.asyncio
    async def test_the_cache_is_keyed_on_provider_account_ref(self):
        """Two real accounts never share a verdict.
        Mutation that reddens: a global (unkeyed) cache."""
        stub = StubMetaAdapter(quota_usage=1, quota_total=100)
        check = UsagePrecheck(ttl_seconds=300, clock=FakeClock())
        await check.check(stub, "igu-1")
        await check.check(stub, "igu-2")
        assert stub.usage_calls == ["igu-1", "igu-2"]
